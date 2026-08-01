"""Firmware metrics stream (UDP), live state, temperature history,
print-session tracking, overheat watch."""
import os
import re
import socket
import sqlite3
import threading
import traceback
import time
import urllib.parse
from collections import deque
from datetime import datetime

from awaria.config import TELEMETRY_DB, METRICS_PORT
from awaria.db import classify_print, db_lock, net_log, open_db, now_str, now_pair, material_of_print
from awaria.services import bus
from awaria.services.gcode_meta import meta_for
from awaria.services.notifications import notify


# temperature history: one sample per 2 s per printer, kept 4 days
# (single tier; long chart ranges are decimated at query time)
FINE_EVERY_S = 2


FINE_KEEP_S = 4 * 86400


# a print session whose printer went silent this long is considered over
# (RESET button / power cut / unplugged mid-print)
STALE_PRINT_S = 300


# sessions shorter than this are discarded outright - false starts,
# immediate aborts and setup moves, not prints (user rule 2026-08-01)
MIN_PRINT_S = 300


# a print may run this much shorter than the file's estimate and still
# count as finished (observed: real prints finish 4-5% ahead of the
# slicer's estimate); anything shorter was cancelled
FINISH_TOLERANCE = 0.94


# live telemetry from the printers' metrics stream (in-memory only)
live_lock = threading.Lock()


LIVE = {}  # hostname -> {"updated": epoch, "values": {metric: value}}


IP2HOST = {}  # refreshed by ping_worker from printers.last_ip


HISTORY = {
}  # hostname -> deque of (epoch, noz, tnoz, bed, tbed, brd) for the charts


HISTORY_LEN = 1800  # ~30 min at the ~1 Hz packet rate


HISTORY_KEYS = ("temp_noz", "ttemp_noz", "temp_bed", "ttemp_bed", "temp_brd")


# printer MAC <-> hostname, learned from live traffic (single-threaded:
# only the metrics worker touches these)
MAC2HOST = {}
HOST2MAC = {}


def syslog_mac(text):
    """MAC from the metrics syslog header: '<pri>1 - <mac> buddy - - -'."""
    parts = text.split(" ", 3)
    if len(parts) >= 3 and re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}",
                                        parts[2]):
        return parts[2].lower()
    return None


def load_macs():
    with db_lock, open_db() as db:
        for r in db.execute(
                "SELECT hostname, mac FROM printers WHERE mac IS NOT NULL"):
            MAC2HOST[r["mac"]] = r["hostname"]
            HOST2MAC[r["hostname"]] = r["mac"]


def resolve_sender(ip, text):
    """Who is streaming from `ip`? Known addresses map directly; an unknown
    address is adopted via the MAC in the packet header - a printer that
    changed its DHCP address mid-print re-identifies within seconds instead
    of being dropped until its next HTTP check-in."""
    with live_lock:
        host = IP2HOST.get(ip)
    mac = syslog_mac(text)
    if host:
        if mac and HOST2MAC.get(host) != mac:
            HOST2MAC[host] = mac
            MAC2HOST[mac] = host
            with db_lock, open_db() as db:
                db.execute("UPDATE printers SET mac=? WHERE hostname=?",
                           (mac, host))
                db.commit()
        return host
    host = MAC2HOST.get(mac) if mac else None
    if not host:
        return None
    with db_lock, open_db() as db:
        db.execute("INSERT OR IGNORE INTO printers(hostname) VALUES (?)",
                   (host, ))
        old = db.execute("SELECT last_ip FROM printers WHERE hostname=?",
                         (host, )).fetchone()
        db.execute(
            "UPDATE printers SET last_ip=NULL"
            " WHERE last_ip=? AND hostname != ?", (ip, host))
        db.execute("UPDATE printers SET last_ip=? WHERE hostname=?",
                   (ip, host))
        if not old or old["last_ip"] != ip:
            net_log(db, host, "rediscovered",
                    f"{old['last_ip'] if old else '?'} -> {ip} (MAC z telemetrii)")
        db.commit()
    with live_lock:
        IP2HOST[ip] = host
    bus.publish("printers", host)
    return host


def metrics_worker():
    """Receives the firmware's UDP metrics stream (RFC5424-ish syslog with
    an influx-like text payload) and keeps the latest values per printer.
    The sender is identified by its source IP (printers.last_ip mapping)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", METRICS_PORT))
    load_macs()
    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except OSError:
            continue
        try:
            handle_metrics_packet(data, addr)
        except Exception:  # noqa: BLE001
            # one malformed packet or a transiently locked database must
            # never kill farm telemetry (outage 2026-08-01: a long backfill
            # held the write lock, the first check-in write raised and this
            # thread silently died - the whole farm went dark)
            traceback.print_exc()


def handle_metrics_packet(data, addr):
    text = data.decode("utf-8", "replace")
    host = resolve_sender(addr[0], text)
    if not host:
        return
    # header: "<pri>1 - <mac> buddy - - - msg=N,tm=...,v=4 " then points
    header_end = text.find(",v=4 ")
    if header_end < 0:
        return
    values = {}
    for line in text[header_end + 5:].split("\n"):
        line = line.strip()
        name_part, _, rest = line.partition(" ")
        if not rest:
            continue
        fields_str = rest.rsplit(" ",
                                 1)[0]  # drop the trailing timestamp diff
        name_tags = name_part.split(",")
        name, tags = name_tags[0], dict(
            t.split("=", 1) for t in name_tags[1:] if "=" in t)
        if tags.get("n", "0") != "0":  # single-tool printers: tool 0 only
            continue
        if m := re.fullmatch(r'v="(.*)"', fields_str):
            values[name] = m.group(1).replace('\\"',
                                              '"').replace("\\\\", "\\")
            continue
        fields = dict(
            f.split("=", 1) for f in fields_str.split(",") if "=" in f)
        raw = fields.get("v", fields.get("value"))
        if raw is None:
            continue
        try:
            values[name] = float(raw.rstrip("i"))
        except ValueError:
            continue
    if values:
        with live_lock:
            entry = LIVE.setdefault(host, {"values": {}})
            entry["values"].update(values)
            entry["updated"] = time.time()
            if any(k in values for k in HISTORY_KEYS):
                v = entry["values"]
                point = tuple(
                    v.get(k) if isinstance(v.get(k), float) else None
                    for k in HISTORY_KEYS)
                now_ts = round(time.time())
                HISTORY.setdefault(host, deque(maxlen=HISTORY_LEN)).append(
                    (now_ts, *point))
                # persisted tier is throttled to one sample per FINE_EVERY_S
                if now_ts - LAST_FINE_TS.get(host, 0) >= FINE_EVERY_S:
                    LAST_FINE_TS[host] = now_ts
                    PENDING_FINE.append((host, now_ts, *point))
    if isinstance(values.get("print_filename"), str):
        mark_telemetry_since(host)
        fname = values["print_filename"]
        with live_lock:
            v_all = LIVE.get(host, {}).get("values", {})
            d = v_all.get("print_dir")
            state = v_all.get("print_state")
        end_state = None
        if isinstance(state, str) and state and state not in ("printing",
                                                              "paused"):
            # fw >= 11247 states are authoritative: a finished/aborted/
            # idle print is over even if a filename still trickles in
            end_state = state
            fname = ""
        if fname and isinstance(d, str) and d:
            # fw >= 11244 streams the directory too - full library path
            fname = d.rstrip("/") + "/" + fname
        track_print_sessions(host, fname, end_state)
    ps = values.get("print_state")
    if isinstance(ps, str) and ps in ("finished", "aborted"):
        # the end state may arrive in a packet without the filename
        # metric - catch up on a session that already closed as unknown
        stamp_late_result(host, ps)
    if isinstance(values.get("gcode_release"), str):
        track_gcode_release(host, values["gcode_release"])
    check_overheat(host, values)


def open_tdb():
    os.makedirs(os.path.dirname(TELEMETRY_DB), exist_ok=True)
    tdb = sqlite3.connect(TELEMETRY_DB)
    tdb.execute("PRAGMA journal_mode=WAL")
    tdb.execute("PRAGMA synchronous=NORMAL")
    tdb.execute("DROP TABLE IF EXISTS samples")  # the old 30 s tier, retired
    tdb.execute("CREATE TABLE IF NOT EXISTS samples_fine ("
                " hostname TEXT NOT NULL, ts INTEGER NOT NULL,"
                " noz REAL, tnoz REAL, bed REAL, tbed REAL, brd REAL,"
                " PRIMARY KEY (hostname, ts)) WITHOUT ROWID")
    return tdb


# sample points queued by metrics_worker, drained in batches by the logger
PENDING_FINE = []


def telemetry_logger():
    """Flushes the sample queue in large, infrequent batches (fewer flash
    write cycles on the SSD than tiny frequent commits). Purges hourly."""
    tdb = open_tdb()
    last_purge = 0
    while True:
        time.sleep(30)
        now = int(time.time())

        with live_lock:
            batch, PENDING_FINE[:] = PENDING_FINE[:], []
        if batch:
            tdb.executemany(
                "INSERT OR REPLACE INTO samples_fine VALUES (?,?,?,?,?,?,?)",
                batch)
            tdb.commit()

        if now - last_purge > 3600:
            last_purge = now
            tdb.execute("DELETE FROM samples_fine WHERE ts < ?",
                        (now - FINE_KEEP_S, ))
            tdb.commit()

        # stale-session watchdog: a printer that stopped streaming mid-print
        # (RESET, power cut, cable out) never reports the print's end - close
        # its session, backdated to the last packet we actually received
        with live_lock:
            last_heard = {
                h: entry.get("updated", 0)
                for h, entry in LIVE.items()
            }
        with db_lock, open_db() as db:
            closed_any = False
            for row in db.execute(
                    "SELECT id, hostname, file, started_ts FROM print_log"
                    " WHERE ended_at IS NULL").fetchall():
                heard = last_heard.get(row["hostname"])
                if heard is None or now - heard > STALE_PRINT_S:
                    ended = datetime.fromtimestamp(heard).strftime("%Y-%m-%d %H:%M:%S") \
                        if heard else now_str()
                    ended_ts = int(heard) if heard else now
                    if ended_ts - (row["started_ts"] or ended_ts) < MIN_PRINT_S:
                        db.execute("DELETE FROM print_log WHERE id=?",
                                   (row["id"], ))
                        LAST_FILE.pop(row["hostname"], None)
                        closed_any = True
                        continue
                    db.execute(
                        "UPDATE print_log SET ended_at=?, ended_ts=?,"
                        " result=? WHERE id=?",
                        (ended, ended_ts,
                         infer_result(row["file"], row["started_ts"],
                                      ended_ts, db), row["id"]))
                    silence = f"{now - int(heard)} s" if heard else "?"
                    net_log(
                        db, row["hostname"], "telemetry_lost_mid_print",
                        f"plik: {row['file']}, cisza od {silence}")
                    LAST_FILE.pop(row["hostname"],
                                  None)  # a comeback opens a fresh session
                    closed_any = True
            if closed_any:
                db.commit()
        if closed_any:
            bus.publish("printers")


def samples_columns(host, t_from, t_to):
    """Chart data for a range, decimated by averaging so the browser gets at
    most ~3600 points regardless of the span."""
    span = max(1, t_to - t_from)
    bucket = ((max(FINE_EVERY_S, span // 3600) + FINE_EVERY_S - 1) //
              FINE_EVERY_S) * FINE_EVERY_S

    tdb = sqlite3.connect(TELEMETRY_DB)
    try:
        rows = tdb.execute(
            "SELECT (ts / ?) * ?, AVG(noz), AVG(tnoz), AVG(bed), AVG(tbed), AVG(brd)"
            " FROM samples_fine WHERE hostname=? AND ts BETWEEN ? AND ?"
            " GROUP BY 1 ORDER BY 1",
            (bucket, bucket, host, t_from, t_to)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    tdb.close()
    return [[r[i] for r in rows] for i in range(6)]


# print-session tracking from the telemetry stream (file name transitions)
LAST_FILE = {}  # hostname -> last seen print_filename


LAST_FINE_TS = {}  # hostname -> ts of the last persisted sample (throttle)


TELEM_SINCE_SET = set(
)  # hosts whose printers.telemetry_since is already stored


def mark_telemetry_since(host):
    """Remembers when a printer FIRST streamed telemetry - the statistics
    clamp each printer's window to this, so time before a printer existed
    (data-wise) is not counted as idle."""
    if host in TELEM_SINCE_SET:
        return
    TELEM_SINCE_SET.add(host)
    with db_lock, open_db() as db:
        db.execute("INSERT OR IGNORE INTO printers(hostname) VALUES (?)",
                   (host, ))
        now, now_ts = now_pair()
        db.execute(
            "UPDATE printers SET telemetry_since=?, telemetry_since_ts=?"
            " WHERE hostname=? AND telemetry_since IS NULL",
            (now, now_ts, host))
        db.commit()


LAST_RELEASE = {}  # hostname -> last seen gcode_release (update notifications)


# electronics overheat watch: (warning, critical) in °C, per metric.
# MCU: firmware itself warns/pauses at 85 and redscreens at 95 - we alert
# earlier, while there is still headroom to react.
OVERHEAT_LIMITS = {
    "temp_mcu": ("MCU", 75.0, 85.0),
    "temp_brd": ("płyta xBuddy", 70.0, 90.0)
}


OVERHEAT_HYSTERESIS = 5.0


OVERHEAT = {}  # hostname -> {metric: level 0/1/2}; guarded by live_lock


def check_overheat(host, values):
    """Escalating-level watch with hysteresis; one notification per escalation."""
    alerts = []
    with live_lock:
        state = OVERHEAT.setdefault(host, {})
        for key, (name, warn, crit) in OVERHEAT_LIMITS.items():
            v = values.get(key)
            if not isinstance(v, float):
                continue
            prev = state.get(key, 0)
            level = 2 if v >= crit else 1 if v >= warn else 0
            if level > prev:
                state[key] = level
                alerts.append((name, v, level))
            elif level < prev and v <= (warn if prev == 1 else
                                        crit) - OVERHEAT_HYSTERESIS:
                state[key] = level
    for name, v, level in alerts:
        with db_lock, open_db() as db:
            notify(
                db, "overheat",
                f"{host}: {'PRZEGRZANIE' if level == 2 else 'wysoka temperatura'}"
                f" — {name} {v:.0f}°C", host,
                f"/awaria/printer/{urllib.parse.quote(host)}")
            db.commit()


def is_overheated(host):
    with live_lock:
        return any(level > 0 for level in OVERHEAT.get(host, {}).values())


def track_gcode_release(host, release):
    prev = LAST_RELEASE.get(host)
    LAST_RELEASE[host] = release
    if prev is None or release == prev or not release:
        return  # first sighting after server start, or no change
    with db_lock, open_db() as db:
        notify(db, "gcode_update",
               f"{host}: zaktualizowano g-code do {release}", host,
               f"/awaria/printer/{urllib.parse.quote(host)}")
        db.commit()


FILE_HOURS_RE = re.compile(
    r"(?<![0-9A-Za-z])(\d{1,2})h(?:\s?(\d{1,2})m?)?(?![0-9A-Za-z])")


def expected_seconds(fname):
    """Sliced print time when the farm's file name carries one
    ('LXS - FB 500g 17h06m.gcode', '...350g 11h00.gcode'); None otherwise."""
    m = FILE_HOURS_RE.search(str(fname or "").rsplit("/", 1)[-1])
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2) or 0) * 60


def learned_seconds(db, fname):
    """Typical duration of this exact file across the fleet's history: the
    farm prints identical files hundreds of times on identical printers, so
    completed runs cluster tightly while aborts scatter below. p70 lands in
    the completed cluster's neighbourhood, the median inside it is the
    estimate. None when history is too thin (< 5 runs) or too scattered."""
    base = str(fname or "").rsplit("/", 1)[-1].lower()
    if not base:
        return None
    durations = sorted(
        r["ended_ts"] - r["started_ts"] for r in db.execute(
            "SELECT file, started_ts, ended_ts FROM print_log"
            " WHERE ended_ts IS NOT NULL AND started_ts IS NOT NULL"
            " AND ended_ts - started_ts >= 600")
        if r["file"].rsplit("/", 1)[-1].lower() == base)
    if len(durations) < 5:
        return None
    p70 = durations[int(len(durations) * 0.7)]
    cluster = [d for d in durations if 0.8 * p70 <= d <= 1.3 * p70]
    if len(cluster) < 3:
        return None
    return cluster[len(cluster) // 2]


def infer_result(fname, started_ts, ended_ts, db=None):
    """Fallback verdict when the stream never witnessed the end (reset,
    network loss, restart gaps): the print either ran about as long as it
    should have, or it did not. Expected duration comes from the g-code
    file itself (slicer estimate scanned into gcode_meta), falling back to
    the time in the file name, then to the file's typical duration in
    fleet history. Farm rule: more than 2% shorter than expected =
    cancelled, end of story. Trailing '?' = inferred, shown as such."""
    if not started_ts or not ended_ts:
        return None
    expected = None
    if db is not None:
        meta = meta_for(db, fname)
        if meta and meta["est_s"]:
            expected = meta["est_s"]
    if expected is None:
        expected = expected_seconds(fname)
    if expected is None and db is not None:
        expected = learned_seconds(db, fname)
    if not expected:
        return None
    actual = ended_ts - started_ts
    return "finished?" if actual >= FINISH_TOLERANCE * expected else "aborted?"


def stamp_late_result(host, state):
    """The authoritative end state can arrive in a later packet than the
    empty filename that closed the session (they are separate metrics) -
    upgrade a just-closed session's unknown or inferred verdict to the
    witnessed one. Repeats harmlessly while the printer shows its end
    screen (first stamp makes it a no-op)."""
    with db_lock, open_db() as db:
        row = db.execute(
            "SELECT id, result FROM print_log WHERE hostname=?"
            " AND ended_ts >= ? ORDER BY id DESC LIMIT 1",
            (host, int(time.time()) - 600)).fetchone()
        if row and row["result"] != state and (
                row["result"] is None or row["result"].endswith("?")):
            db.execute("UPDATE print_log SET result=? WHERE id=?",
                       (state, row["id"]))
            db.commit()


def same_print(stored, incoming):
    """A stored session path and an incoming print_filename mean the same
    print when equal - or when one is the other's basename: the directory
    streams as a separate metric, so the first packet after a server restart
    (empty LIVE) carries the bare file name while the session row already
    holds the full library path. Exact-match adoption here split every
    running print into a fresh session on each deploy (field report
    2026-08-01, all printers 'started 2 min ago')."""
    return (stored == incoming or stored.endswith("/" + incoming)
            or incoming.endswith("/" + stored))


def track_print_sessions(host, fname, end_state=None):
    prev = LAST_FILE.get(host)
    if fname == prev:
        return
    LAST_FILE[host] = fname
    # how the print ended, from the authoritative state: 'finished' and
    # 'aborted' are recorded forever on the session; an 'idle' close (missed
    # end states) and file-to-file jumps stay NULL = unknown
    result = end_state if end_state in ("finished", "aborted") else None
    now, now_ts = now_pair()
    with db_lock, open_db() as db:
        if prev and fname and fname.endswith("/" + prev):
            # same print - the directory arrived a moment after the name;
            # upgrade the session identity instead of splitting the session
            kind = classify_print(db, host, fname)
            db.execute(
                "UPDATE print_log SET file=?, material=COALESCE(?, material),"
                " kind=CASE WHEN ?='prod' THEN kind ELSE ? END"
                " WHERE hostname=? AND file=? AND ended_at IS NULL",
                (fname, material_of_print(fname), kind, kind, host, prev))
            db.commit()
            return
        if prev is None and fname:
            # server (re)start mid-print: adopt a matching open session
            row = db.execute(
                "SELECT id, file FROM print_log WHERE hostname=?"
                " AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                (host, )).fetchone()
            if row and same_print(row["file"], fname):
                return
            row = db.execute(
                "SELECT id, file, ended_ts FROM print_log WHERE hostname=?"
                " AND ended_ts >= ? ORDER BY id DESC LIMIT 1",
                (host, now_ts - 1800)).fetchone()
            if row and not same_print(row["file"], fname):
                row = None
            if row:
                # dropped off the network mid-print and came back: the
                # watchdog-closed session continues instead of splitting
                db.execute(
                    "UPDATE print_log SET ended_at=NULL, ended_ts=NULL,"
                    " result=NULL WHERE id=?", (row["id"], ))
                net_log(db, host, "print_session_reopened",
                        f"przerwa ~{now_ts - row['ended_ts']} s, plik: {fname}")
                db.commit()
                bus.publish("printers", host)
                return
        for open_row in db.execute(
                "SELECT id, file, started_ts FROM print_log"
                " WHERE hostname=? AND ended_at IS NULL", (host, )).fetchall():
            if now_ts - (open_row["started_ts"] or now_ts) < MIN_PRINT_S:
                # too short to be a print - discard, not close
                db.execute("DELETE FROM print_log WHERE id=?",
                           (open_row["id"], ))
                continue
            verdict = result or infer_result(open_row["file"],
                                             open_row["started_ts"], now_ts,
                                             db)
            db.execute(
                "UPDATE print_log SET ended_at=?, ended_ts=?, result=?"
                " WHERE id=?", (now, now_ts, verdict, open_row["id"]))
        if fname:
            db.execute(
                "INSERT INTO print_log(hostname, file, started_at,"
                " started_ts, material, kind) VALUES (?,?,?,?,?,?)",
                (host, fname, now, now_ts, material_of_print(fname),
                 classify_print(db, host, fname)))
        db.commit()
    bus.publish("printers", host)


def live_progress(max_age=90):
    """{hostname: percent} for printers currently printing - the Historia
    list polls this so its progress bars advance without a page reload."""
    out = {}
    cutoff = time.time() - max_age
    with live_lock:
        for host, entry in LIVE.items():
            if entry.get("updated", 0) < cutoff:
                continue
            values = entry["values"]
            pct = values.get("print_progress")
            if values.get("print_filename") and isinstance(pct, float):
                out[host] = pct
    return out


def live_of(host, max_age=90):
    """Latest telemetry of a printer, or None when stale/absent."""
    with live_lock:
        entry = LIVE.get(host)
        if not entry or time.time() - entry.get("updated", 0) > max_age:
            return None
        return dict(entry["values"]), entry["updated"]


def history_columns(host):
    """uPlot column arrays: [t, noz, tnoz, bed, tbed, brd]"""
    with live_lock:
        points = list(HISTORY.get(host, ()))
    return [[p[i] for p in points] for i in range(6)]
