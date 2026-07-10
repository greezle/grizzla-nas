"""Firmware metrics stream (UDP), live state, temperature history,
print-session tracking, overheat watch."""
import os
import re
import socket
import sqlite3
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime

from awaria.config import TELEMETRY_DB, METRICS_PORT
from awaria.db import db_lock, open_db, now_str, now_pair, material_of_print
from awaria.services import bus
from awaria.services.notifications import notify


# temperature history: one sample per 2 s per printer, kept 4 days
# (single tier; long chart ranges are decimated at query time)
FINE_EVERY_S = 2


FINE_KEEP_S = 4 * 86400


# a print session whose printer went silent this long is considered over
# (RESET button / power cut / unplugged mid-print)
STALE_PRINT_S = 300


# live telemetry from the printers' metrics stream (in-memory only)
live_lock = threading.Lock()


LIVE = {}  # hostname -> {"updated": epoch, "values": {metric: value}}


IP2HOST = {}  # refreshed by ping_worker from printers.last_ip


HISTORY = {
}  # hostname -> deque of (epoch, noz, tnoz, bed, tbed, brd) for the charts


HISTORY_LEN = 1800  # ~30 min at the ~1 Hz packet rate


HISTORY_KEYS = ("temp_noz", "ttemp_noz", "temp_bed", "ttemp_bed", "temp_brd")


def metrics_worker():
    """Receives the firmware's UDP metrics stream (RFC5424-ish syslog with
    an influx-like text payload) and keeps the latest values per printer.
    The sender is identified by its source IP (printers.last_ip mapping)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", METRICS_PORT))
    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except OSError:
            continue
        with live_lock:
            host = IP2HOST.get(addr[0])
        if not host:
            continue
        text = data.decode("utf-8", "replace")
        # header: "<pri>1 - <mac> buddy - - - msg=N,tm=...,v=4 " then points
        header_end = text.find(",v=4 ")
        if header_end < 0:
            continue
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
                d = LIVE.get(host, {}).get("values", {}).get("print_dir")
            if fname and isinstance(d, str) and d:
                # fw >= 11244 streams the directory too - full library path
                fname = d.rstrip("/") + "/" + fname
            track_print_sessions(host, fname)
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
                    "SELECT id, hostname FROM print_log WHERE ended_at IS NULL"
            ).fetchall():
                heard = last_heard.get(row["hostname"])
                if heard is None or now - heard > STALE_PRINT_S:
                    ended = datetime.fromtimestamp(heard).strftime("%Y-%m-%d %H:%M:%S") \
                        if heard else now_str()
                    ended_ts = int(heard) if heard else now
                    db.execute(
                        "UPDATE print_log SET ended_at=?, ended_ts=?"
                        " WHERE id=?", (ended, ended_ts, row["id"]))
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


def track_print_sessions(host, fname):
    prev = LAST_FILE.get(host)
    if fname == prev:
        return
    LAST_FILE[host] = fname
    now, now_ts = now_pair()
    with db_lock, open_db() as db:
        if prev and fname and fname.endswith("/" + prev):
            # same print - the directory arrived a moment after the name;
            # upgrade the session identity instead of splitting the session
            db.execute(
                "UPDATE print_log SET file=?, material=COALESCE(?, material)"
                " WHERE hostname=? AND file=? AND ended_at IS NULL",
                (fname, material_of_print(fname), host, prev))
            db.commit()
            return
        if prev is None and fname:
            # server (re)start mid-print: adopt a matching open session
            row = db.execute(
                "SELECT id FROM print_log WHERE hostname=? AND file=?"
                " AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                (host, fname)).fetchone()
            if row:
                return
        db.execute(
            "UPDATE print_log SET ended_at=?, ended_ts=?"
            " WHERE hostname=? AND ended_at IS NULL", (now, now_ts, host))
        if fname:
            db.execute(
                "INSERT INTO print_log(hostname, file, started_at,"
                " started_ts, material) VALUES (?,?,?,?,?)",
                (host, fname, now, now_ts, material_of_print(fname)))
        db.commit()
    bus.publish("printers", host)


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
