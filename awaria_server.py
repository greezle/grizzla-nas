#!/usr/bin/env python3
"""Awaria server for the print farm (gcode-nas).

Receives failure/repair events from the printers (M9206), keeps them in
SQLite and serves a maintenance dashboard. Python stdlib only - no
dependencies. Runs behind nginx (Basic auth + proxy on /awaria/).

Event JSON (POST /awaria/api/event):
  {"host": "D6", "action": "AWARIA-BLOKADA|AWARIA|NOTATKA|NAPRAWIONO",
   "category": 1, "label": "Zapchana dysza / ekstruder",
   "detail": "...", "ptime": "2026-07-05 20:00:00", "seq": 123}
host+seq (when seq is present) deduplicates retransmissions from the
printers' offline queues. Server time is authoritative (printers may
lack NTP); ptime is stored as advisory only.
"""

import html
import json
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = "/var/lib/awaria/awaria.db"
# Telemetry is big, expendable and constantly written - it lives on the SSD
# (dot-dir keeps it out of sight in the SMB share), not on the Pi's SD card.
TELEMETRY_DB = "/srv/gcode/.telemetry/telemetry.db"
STATIC_DIR = "/var/lib/awaria/static"  # vendored JS (SortableJS), no internet needed
LISTEN = ("127.0.0.1", 8081)
METRICS_PORT = 8514  # firmware metrics stream (UDP syslog, see metric_handlers.cpp)
# temperature history: one sample per 2 s per printer, kept 4 days
# (single tier; long chart ranges are decimated at query time)
FINE_EVERY_S = 2
FINE_KEEP_S = 4 * 86400
# a print session whose printer went silent this long is considered over
# (RESET button / power cut / unplugged mid-print)
STALE_PRINT_S = 300
# off-site backup freshness: gcode-nas-backup (the 3B) writes this stamp
# after each successful nightly pull; the dashboard warns when it goes stale,
# so a quietly dead backup Pi cannot go unnoticed for months
OFFSITE_STAMP = "/var/lib/awaria/offsite_backup_stamp"
OFFSITE_MAX_AGE_S = 50 * 3600  # nightly cadence + generous slack

ACTIONS_OPEN = ("AWARIA-BLOKADA", "AWARIA")
ACTIONS = ACTIONS_OPEN + ("NOTATKA", "NAPRAWIONO")

db_lock = threading.Lock()

# live connectivity of the printers, maintained by ping_worker()
online_lock = threading.Lock()
ONLINE = {}  # hostname -> bool

SUBNET_PREFIX = "192.168.68."
FARM_HOST_RE = re.compile(
    r"^[A-Za-z]\d{1,2}$")  # section+cell hostnames, e.g. E6


def ping_ip(ip):
    return subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def resolve_mdns(ips):
    """ip -> hostname via reverse mDNS (printers announce '<host>.local';
    the LAN has no regular DNS). Unanswered addresses are simply absent."""
    result = {}
    for i in range(0, len(ips), 24):
        chunk = ips[i:i + 24]
        try:
            out = subprocess.run(["avahi-resolve", "--address", *chunk],
                                 capture_output=True,
                                 text=True,
                                 timeout=30).stdout
        except (subprocess.TimeoutExpired, OSError):
            continue
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1]:
                result[parts[0]] = parts[1].removesuffix(".local")
    return result


def ping_worker():
    """Connectivity + discovery daemon. Every 30 s pings the known printer
    IPs for the live map; every ~4 min additionally sweeps the whole subnet
    and reverse-mDNS-resolves new responders, so printers get their IPs
    registered without any firmware support or manual entry."""
    cycle = 0
    while True:
        if cycle % 8 == 0:
            candidates = [f"{SUBNET_PREFIX}{n}" for n in range(1, 255)]
            with ThreadPoolExecutor(max_workers=48) as pool:
                alive = [
                    ip for ip, ok in zip(candidates,
                                         pool.map(ping_ip, candidates)) if ok
                ]
            with db_lock, open_db() as db:
                known_hosts = {
                    r["hostname"]
                    for r in db.execute("SELECT hostname FROM printers")
                }
                known_ips = {
                    r["last_ip"]
                    for r in db.execute(
                        "SELECT last_ip FROM printers WHERE last_ip IS NOT NULL"
                    )
                }
                unknown = [ip for ip in alive if ip not in known_ips]
                for ip, host in resolve_mdns(unknown).items():
                    # only farm-scheme hostnames (or already-known ones) - the
                    # subnet also has PCs, phones, the NAS itself...
                    if FARM_HOST_RE.match(host) or host in known_hosts:
                        db.execute(
                            "INSERT OR IGNORE INTO printers(hostname) VALUES (?)",
                            (host, ))
                        db.execute(
                            "UPDATE printers SET last_ip=? WHERE hostname=?",
                            (ip, host))
                db.commit()

        with db_lock, open_db() as db:
            targets = [(r["hostname"], r["last_ip"]) for r in db.execute(
                "SELECT hostname, last_ip FROM printers WHERE last_ip IS NOT NULL"
            )]
        with live_lock:
            IP2HOST.clear()
            IP2HOST.update({ip: host for host, ip in targets})
        results = {}
        if targets:
            with ThreadPoolExecutor(max_workers=16) as pool:
                for (host,
                     _), ok in zip(targets,
                                   pool.map(lambda t: ping_ip(t[1]), targets)):
                    results[host] = ok
        with online_lock:
            ONLINE.clear()
            ONLINE.update(results)
        cycle += 1
        time.sleep(30)


def is_online(host):
    with online_lock:
        return ONLINE.get(host, False)


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
            track_print_sessions(host, values["print_filename"])
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
                    db.execute("UPDATE print_log SET ended_at=? WHERE id=?",
                               (ended, row["id"]))
                    LAST_FILE.pop(row["hostname"],
                                  None)  # a comeback opens a fresh session
                    closed_any = True
            if closed_any:
                db.commit()


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
        db.execute(
            "UPDATE printers SET telemetry_since=? WHERE hostname=? AND telemetry_since IS NULL",
            (now_str(), host))
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
    now = now_str()
    with db_lock, open_db() as db:
        if prev is None and fname:
            # server (re)start mid-print: adopt a matching open session
            row = db.execute(
                "SELECT id FROM print_log WHERE hostname=? AND file=?"
                " AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                (host, fname)).fetchone()
            if row:
                return
        db.execute(
            "UPDATE print_log SET ended_at=? WHERE hostname=? AND ended_at IS NULL",
            (now, host))
        if fname:
            db.execute(
                "INSERT INTO print_log(hostname, file, started_at) VALUES (?,?,?)",
                (host, fname, now))
        db.commit()


def live_of(host, max_age=90):
    """Latest telemetry of a printer, or None when stale/absent."""
    with live_lock:
        entry = LIVE.get(host)
        if not entry or time.time() - entry.get("updated", 0) > max_age:
            return None
        return dict(entry["values"]), entry["updated"]


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def open_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db():
    with db_lock, open_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            hostname TEXT NOT NULL,
            received_at TEXT NOT NULL,
            printer_time TEXT,
            action TEXT NOT NULL,
            category INTEGER NOT NULL,
            label TEXT,
            detail TEXT,
            seq INTEGER
        );
        CREATE UNIQUE INDEX IF NOT EXISTS events_dedupe
            ON events(hostname, seq) WHERE seq IS NOT NULL;
        CREATE TABLE IF NOT EXISTS failures (
            id INTEGER PRIMARY KEY,
            hostname TEXT NOT NULL,
            category INTEGER NOT NULL,
            label TEXT,
            detail TEXT,
            blocking INTEGER NOT NULL DEFAULT 0,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            closed_by TEXT,
            repair_note TEXT
        );
        CREATE INDEX IF NOT EXISTS failures_open
            ON failures(hostname, category) WHERE closed_at IS NULL;
        CREATE TABLE IF NOT EXISTS error_defs (
            id INTEGER PRIMARY KEY,       -- persisted on printers, never reused
            label TEXT NOT NULL,
            severity INTEGER NOT NULL DEFAULT 1,  -- 0 crit / 1 ask / 2 note
            print_ctx INTEGER NOT NULL DEFAULT 0, -- attach file + sheet info
            hidden INTEGER NOT NULL DEFAULT 0,    -- not offered in the menu
            position INTEGER NOT NULL DEFAULT 100,
            questions TEXT NOT NULL DEFAULT '[]'  -- [{text, answers:[{text, severity|null}]}]
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS printers (
            hostname TEXT PRIMARY KEY,
            built_on TEXT,                -- date of build
            note TEXT,
            last_ip TEXT,                 -- source IP of the last report (for pings)
            last_seen TEXT
        );
        CREATE TABLE IF NOT EXISTS printer_flags (
            id INTEGER PRIMARY KEY,
            hostname TEXT NOT NULL,
            text TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#607d8b'
        );
        CREATE TABLE IF NOT EXISTS components (   -- spare parts / maintenance actions
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 100
        );
        CREATE TABLE IF NOT EXISTS maintenance (   -- replacements / maintenance done
            id INTEGER PRIMARY KEY,
            hostname TEXT NOT NULL,
            component_id INTEGER,                  -- NULL = free-form action
            action TEXT,
            done_at TEXT NOT NULL,
            failure_id INTEGER                     -- repair this belongs to, optional
        );
        CREATE TABLE IF NOT EXISTS print_log (     -- print sessions from telemetry
            id INTEGER PRIMARY KEY,
            hostname TEXT NOT NULL,
            file TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );
        CREATE INDEX IF NOT EXISTS print_log_time ON print_log(started_at);
        -- year-scale usage reports query per printer + time range
        CREATE INDEX IF NOT EXISTS print_log_host_time ON print_log(hostname, started_at);
        CREATE INDEX IF NOT EXISTS failures_host_time ON failures(hostname, opened_at);
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            kind TEXT NOT NULL,        -- failure / repair / note / overheat / gcode_update
            hostname TEXT,
            text TEXT NOT NULL,
            link TEXT,                 -- click target; NULL = info-only
            dismissed INTEGER NOT NULL DEFAULT 0
        );
        """)
        for column in ("last_ip TEXT", "last_seen TEXT",
                       "telemetry_since TEXT"):  # migrations
            try:
                db.execute(f"ALTER TABLE printers ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass
        if not db.execute("SELECT 1 FROM components LIMIT 1").fetchone():
            db.executemany(
                "INSERT INTO components(name, position) VALUES (?,?)", [
                    ("Dysza", 10),
                    ("Heatbreak", 20),
                    ("Ekstruder / koła podające", 30),
                    ("Pasek X", 40),
                    ("Pasek Y", 50),
                    ("Pręty / łożyska", 60),
                    ("Wentylator hotendu", 70),
                    ("Wentylator wydruku", 80),
                    ("Termistor", 90),
                    ("Grzałka", 100),
                    ("Czujnik filamentu", 110),
                    ("Smarowanie prętów", 120),
                    ("Napięcie pasków", 130),
                ])
        if not db.execute("SELECT 1 FROM error_defs LIMIT 1").fetchone():
            seed_error_defs(db)
        db.commit()


def seed_error_defs(db):
    """The original firmware-builtin list; ids are already persisted in
    printer EEPROMs, so they must stay exactly like this."""
    q = json.dumps
    rows = [
        (1, "Zapchana dysza / ekstruder", 0, 1, 0, 10,
         q([{
             "text":
             "Na której warstwie zapchała się dysza?",
             "answers": [{
                 "text": "Na 1. - 2."
             }, {
                 "text": "Późniejszej"
             }, {
                 "text": "Nie wiadomo"
             }]
         }])),
        (13, "Niedolany wydruk", 0, 1, 0, 20, "[]"),
        (14, "Bąble / nadmierne nitki na wydruku", 0, 1, 0, 30, "[]"),
        (2, "Problem z wentylatorem", 1, 0, 0, 40,
         q([{
             "text":
             "Jaki jest problem z wentylatorem?",
             "answers": [{
                 "text": "Nie działa",
                 "severity": 0
             }, {
                 "text": "Hałasuje",
                 "severity": 1
             }]
         }])),
        (3, "Czujnik filamentu", 1, 0, 0, 50,
         q([{
             "text":
             "Jaki problem z czujnikiem?",
             "answers": [{
                 "text": "Ładowanie"
             }, {
                 "text": "Brak końca"
             }, {
                 "text": "Fałszywy"
             }]
         }])),
        (4, "Problem z grzaniem", 0, 0, 0, 60, "[]"),
        (5, "Oś / pasek X (głowica)", 1, 0, 0, 70, "[]"),
        (12, "Oś / pasek Y (stół)", 1, 0, 0, 80, "[]"),
        (6, "Płyta / powierzchnia druku", 1, 0, 0, 90, "[]"),
        (7, "Uszkodzenie mechaniczne", 1, 0, 0, 100, "[]"),
        (8, "Hałas / dziwne dźwięki", 2, 0, 0, 110, "[]"),
        (9, "Inna awaria", 1, 0, 0, 120, "[]"),
        (10, "Błąd krytyczny (reset)", 0, 0, 1, 130, "[]"),
        (11, "Niedziałający wentylator", 0, 0, 1, 140,
         "[]"),  # deprecated, hidden
    ]
    db.executemany(
        "INSERT INTO error_defs(id, label, severity, print_ctx, hidden, position, questions)"
        " VALUES (?,?,?,?,?,?,?)", rows)
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('catalog_seq', '1')")
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('next_error_id', '15')"
    )


def render_catalog(db):
    """Text format parsed by the printers (common/awaria_catalog.hpp)."""
    seq = db.execute(
        "SELECT value FROM meta WHERE key='catalog_seq'").fetchone()
    lines = ["version 1", "seq %s" % (seq["value"] if seq else "1")]
    for d in db.execute("SELECT * FROM error_defs ORDER BY position, id"):
        flags = (1 if d["print_ctx"] else 0) | (2 if d["hidden"] else 0)
        lines.append("e %d %d %d %s" %
                     (d["id"], d["severity"], flags, d["label"]))
        try:
            questions = json.loads(d["questions"])
        except json.JSONDecodeError:
            questions = []
        for question in questions[:2]:
            text = str(question.get("text", "")).strip()
            answers = question.get("answers", [])[:3]
            if not text or len(answers) < 2:
                continue
            lines.append("q %s" % text)
            for a in answers:
                sev = a.get("severity")
                lines.append("a %s %s" % (sev if sev in (0, 1, 2) else "-",
                                          str(a.get("text", "")).strip()))
    lines.append("end")
    return "\n".join(lines) + "\n"


def notify(db, kind, text, hostname=None, link=None):
    db.execute(
        "INSERT INTO notifications(created_at, kind, hostname, text, link)"
        " VALUES (?,?,?,?,?)", (now_str(), kind, hostname, text, link))


def handle_event(data, client_ip=None):
    """Apply one printer event; returns (http_status, response_dict)."""
    host = str(data.get("host") or "").strip()[:32]
    action = str(data.get("action") or "").strip()
    try:
        category = int(data.get("category"))
    except (TypeError, ValueError):
        return 400, {"ok": False, "error": "bad category"}
    label = str(data.get("label") or "")[:80]
    detail = str(data.get("detail") or "")[:200]
    ptime = str(data.get("ptime") or "")[:24] or None
    seq = data.get("seq")
    seq = int(seq) if seq is not None else None

    if not host:
        return 400, {"ok": False, "error": "missing host"}
    if action not in ACTIONS:
        return 400, {"ok": False, "error": "bad action"}

    now = now_str()
    with db_lock, open_db() as db:
        db.execute("INSERT OR IGNORE INTO printers(hostname) VALUES (?)",
                   (host, ))
        db.execute(
            "UPDATE printers SET last_seen=?, last_ip=COALESCE(?, last_ip) WHERE hostname=?",
            (now, client_ip, host))
        if seq is not None:
            dup = db.execute("SELECT 1 FROM events WHERE hostname=? AND seq=?",
                             (host, seq)).fetchone()
            if dup:
                return 200, {"ok": True, "dup": True}

        db.execute(
            "INSERT INTO events(hostname, received_at, printer_time, action,"
            " category, label, detail, seq) VALUES (?,?,?,?,?,?,?,?)",
            (host, now, ptime, action, category, label, detail, seq))

        if action in ACTIONS_OPEN:
            blocking = 1 if action == "AWARIA-BLOKADA" else 0
            row = db.execute(
                "SELECT id, blocking FROM failures WHERE hostname=? AND"
                " category=? AND closed_at IS NULL",
                (host, category)).fetchone()
            if row:
                db.execute(
                    "UPDATE failures SET blocking=MAX(blocking,?), detail=?,"
                    " label=? WHERE id=?",
                    (blocking, detail, label, row["id"]))
                failure_id = row["id"]
            else:
                failure_id = db.execute(
                    "INSERT INTO failures(hostname, category, label, detail,"
                    " blocking, opened_at) VALUES (?,?,?,?,?,?)",
                    (host, category, label, detail, blocking, now)).lastrowid
            notify(db, "failure",
                   f"{host}: {'BLOKADA' if blocking else 'awaria'} — {label}",
                   host, f"/awaria/failure/{failure_id}")
        elif action == "NAPRAWIONO":
            open_row = db.execute(
                "SELECT id FROM failures WHERE hostname=? AND category=?"
                " AND closed_at IS NULL ORDER BY id DESC LIMIT 1",
                (host, category)).fetchone()
            db.execute(
                "UPDATE failures SET closed_at=?, closed_by='drukarka'"
                " WHERE hostname=? AND category=? AND closed_at IS NULL",
                (now, host, category))
            notify(
                db, "repair", f"{host}: naprawiono — {label}", host,
                f"/awaria/failure/{open_row['id']}"
                if open_row else f"/awaria/printer/{urllib.parse.quote(host)}")
        elif action == "NOTATKA":
            notify(
                db, "note", f"{host}: notatka — {label}" +
                (f" ({detail.splitlines()[0]})" if detail else ""), host,
                f"/awaria/printer/{urllib.parse.quote(host)}")
        db.commit()
    return 200, {"ok": True}


# ---------------------------------------------------------------- dashboard

CSS = """
body { font-family: system-ui, sans-serif; margin: 0; background: #f2f2f2; color: #111; }
header { background: #1a1a1a; color: #fff; padding: 10px 20px; display: flex; align-items: center; gap: 16px; }
header h1 { font-size: 20px; margin: 0; }
header a { color: #ffb700; text-decoration: none; }
#burger { background: none; border: none; color: #fff; font-size: 22px; cursor: pointer; padding: 2px 6px;
  transition: transform .2s ease; }
#burger:hover { transform: scale(1.2); }
#drawer { position: fixed; top: 0; left: 0; bottom: 0; width: 240px; background: #1a1a1a; color: #fff;
  transform: translateX(-100%); transition: transform .28s cubic-bezier(.22,.9,.32,1); z-index: 20; padding-top: 10px; }
#drawer.open { transform: translateX(0); box-shadow: 3px 0 14px rgba(0,0,0,.45); }
#drawer a { display: block; color: #fff; text-decoration: none; padding: 12px 20px; font-size: 15px;
  transition: background .15s ease, padding-left .15s ease; }
#drawer a:hover { background: #333; padding-left: 28px; }
#drawer .brand { color: #ffb700; font-weight: 700; padding: 10px 20px 16px; border-bottom: 1px solid #333; margin-bottom: 6px; }
#overlay { position: fixed; inset: 0; background: rgba(0,0,0,.35); z-index: 19;
  opacity: 0; visibility: hidden; transition: opacity .25s ease, visibility .25s; }
#overlay.show { opacity: 1; visibility: visible; }
main { animation: page-in .28s ease; }
main.no-anim { animation: none; }
@keyframes page-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
.hidden { display: none; }
.sec-head { display: flex; justify-content: space-between; align-items: center; margin-top: 22px; }
.sec-head h2 { margin: 0; }
.view-toggle { display: flex; border: 1px solid #ccc; border-radius: 6px; overflow: hidden; }
.view-toggle .tab { background: #fff; border: none; padding: 5px 14px; cursor: pointer; font-size: 13px;
  transition: background .15s ease, color .15s ease; }
.view-toggle .tab.active { background: #1a1a1a; color: #ffb700; font-weight: 700; }
table tr { transition: background .15s ease; }
tbody tr:hover td, table tr:hover td { background: #fafafa; }
a { transition: color .15s ease; }
input[type=submit] { cursor: pointer; transition: transform .12s ease; }
input[type=submit]:hover { transform: translateY(-1px); }
.farm-map { display: flex; align-items: flex-start; background: #fff; padding: 16px;
  box-shadow: 0 1px 2px rgba(0,0,0,.15); overflow-x: auto; }
.zone { display: flex; gap: 8px; }             /* tiny spacing between sections */
.zone + .zone { margin-left: 42px; }           /* large spacing ~ half a section */
.sec-label { text-align: center; font-weight: 700; margin-bottom: 4px; }
.sec-grid { display: grid; grid-template-columns: repeat(2, 34px); grid-auto-rows: 34px; gap: 4px; }
.sq { display: flex; align-items: center; justify-content: center; border-radius: 5px; font-size: 12px;
  font-weight: 600; color: #fff; text-decoration: none; transition: transform .12s ease, box-shadow .12s ease;
  position: relative; overflow: hidden; }
.sq .prog { position: absolute; left: 3px; right: 3px; bottom: 2px; height: 4px;
  background: rgba(0,0,0,.30); border-radius: 2px; }
.sq .prog i { display: block; height: 100%; background: #fff; border-radius: 2px;
  animation: prog-pulse 1.6s ease-in-out infinite; }
@keyframes prog-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .45; } }
.map-legend { margin-top: 10px; color: #666; font-size: 13px; display: flex; align-items: center; gap: 6px; }
.map-legend .sq.mini { width: 18px; height: 18px; display: inline-flex; margin-left: 14px; cursor: default; }
.map-legend .sq.mini:first-child { margin-left: 0; }
.map-legend .sq.mini:hover { transform: none; box-shadow: none; }
.sq:hover { transform: scale(1.18); box-shadow: 0 2px 8px rgba(0,0,0,.35); }
.sq.off { background: #c9c9c9; color: #666; }
.sq.ok { background: #2e7d32; }
.sq.degraded { background: #f2c200; color: #111; }
.sq.blocked { background: #d32f2f; animation: pulse 1.8s infinite; }
#tip { position: absolute; background: #1a1a1a; color: #fff; padding: 8px 12px; border-radius: 6px;
  font-size: 13px; box-shadow: 0 4px 14px rgba(0,0,0,.4); opacity: 0; visibility: hidden;
  transition: opacity .15s ease; z-index: 30; pointer-events: none; max-width: 260px; }
#tip.show { opacity: 1; visibility: visible; }
.stats-top { display: flex; gap: 30px; align-items: center; background: #fff; padding: 18px;
  box-shadow: 0 1px 2px rgba(0,0,0,.15); }
.donut { width: 170px; height: 170px; border-radius: 50%; position: relative; flex-shrink: 0; }
.donut-hole { position: absolute; inset: 32px; background: #fff; border-radius: 50%;
  display: flex; flex-direction: column; align-items: center; justify-content: center; }
.donut-hole b { font-size: 26px; }
.donut-hole span { color: #777; font-size: 12px; }
.legend p { margin: 8px 0; font-size: 14.5px; color: #444; }
.legend p .muted, .legend p.muted { font-size: 12.5px; }
.dot { display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin-right: 8px;
  vertical-align: middle; }
.usage-row { display: flex; align-items: center; gap: 12px; padding: 6px 10px; border-radius: 6px;
  transition: opacity .2s ease, background .15s ease; }
.usage-row:hover { background: #f6f6f6; }
.usage-row.excluded { opacity: .35; }
.usage-row .host, .usage-row a.host { min-width: 42px; font-size: 14px; font-weight: 600; }
.usage-row a.host:hover { text-decoration: underline; }
.usage-row > .muted { min-width: 150px; font-size: 12px; text-align: right; white-space: nowrap; }
.usage-row input[type=checkbox] { width: 15px; height: 15px; accent-color: #1a1a1a; flex-shrink: 0; }
.ubar { flex: 1; height: 12px; border-radius: 6px; overflow: hidden; display: flex; background: #ececec; }
.ubar div { transition: width .4s ease; }
.sec-group + .sec-group { margin-top: 20px; }
.sec-toggle { display: flex; align-items: center; gap: 8px; padding: 0 10px 6px; margin-bottom: 4px;
  border-bottom: 1px solid #e6e6e6; cursor: pointer; font-size: 11.5px; text-transform: uppercase;
  letter-spacing: .09em; color: #888; user-select: none; }
.sec-toggle b { font-size: 11.5px; font-weight: 700; color: #666; }
.sec-toggle .muted { font-size: 11.5px; }
.sec-toggle input[type=checkbox] { width: 14px; height: 14px; accent-color: #1a1a1a; }
.sec-toggle:hover { color: #444; }
.donut { transition: background .3s ease; }
.cards { display: flex; gap: 14px; flex-wrap: wrap; align-items: flex-start; }
.card { background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,.15); padding: 12px 16px; flex: 1; min-width: 320px; }
.card h3 { margin: 2px 0 8px; font-size: 14px; text-transform: uppercase; color: #666; }
table.plain td { padding: 4px 8px; }
.chip { display: inline-flex; align-items: center; padding: 2px 10px; border-radius: 10px; color: #fff;
  font-size: 12px; font-weight: 700; margin: 0 3px 3px 0; border: none; vertical-align: middle; }
.chip-x { background: none; border: none; color: #fff; cursor: pointer; font-weight: 700; padding: 0 0 0 6px;
  font-size: 13px; opacity: .75; transition: opacity .15s ease; }
.chip-x:hover { opacity: 1; }
.chip.ghost { opacity: .65; border: 1px dashed rgba(255,255,255,.8); transition: opacity .15s ease; }
.chip.ghost:hover { opacity: 1; }
.chip-btn { background: none; border: none; color: #fff; font-weight: 700; font-size: 12px; cursor: pointer; padding: 0; }
.chips { margin: 6px 0; }
.swatch { width: 22px; height: 22px; border-radius: 5px; border: 1px solid #bbb; cursor: pointer; padding: 0;
  transition: transform .12s ease; }
.swatch:hover { transform: scale(1.2); }
.inline-form { display: flex; gap: 6px; align-items: center; margin: 8px 0; flex-wrap: wrap; }
.inline-form input[type=color] { width: 34px; height: 26px; padding: 1px; border: 1px solid #ccc; }
.checks { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 4px; }
label.check { display: block; padding: 3px 6px; border-radius: 4px; transition: background .15s ease; }
label.check:hover { background: #f0f0f0; }
td.drag { width: 26px; cursor: grab; color: #999; font-size: 18px; text-align: center; user-select: none; }
td.drag:active { cursor: grabbing; }
tr.ghost td { background: #fff3cd; }
tr.drag-active { box-shadow: 0 4px 14px rgba(0,0,0,.25); }
.b-block.alive { animation: pulse 1.8s infinite; }
@keyframes pulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(211,47,47,.55); } 60% { box-shadow: 0 0 0 8px rgba(211,47,47,0); } }
.counts { margin-left: auto; font-size: 15px; }
#bell-wrap { position: relative; margin-left: 14px; }
.counts + #bell-wrap { margin-left: 14px; }
header > #bell-wrap:nth-last-child(1):nth-child(3) { margin-left: auto; } /* no counts -> push right */
#bell { background: none; border: none; color: #fff; cursor: pointer; padding: 4px; position: relative;
  transition: transform .15s ease; }
#bell:hover { transform: scale(1.15); }
#bell-badge { position: absolute; top: -4px; right: -6px; background: #d32f2f; color: #fff;
  font-size: 11px; font-weight: 700; border-radius: 9px; padding: 1px 5px; min-width: 10px; }
#notif-panel { position: absolute; right: 0; top: 34px; width: 400px; max-width: 92vw; background: #fff;
  color: #111; border-radius: 8px; box-shadow: 0 6px 24px rgba(0,0,0,.35); z-index: 40;
  animation: page-in .18s ease; }
#notif-list { max-height: 60vh; overflow-y: auto; }
.notif { display: flex; align-items: flex-start; border-left: 4px solid #999; border-bottom: 1px solid #eee; }
.notif a, .notif > span { flex: 1; padding: 8px 10px; text-decoration: none; color: #111; font-size: 13px; }
.notif a:hover { background: #f6f6f6; }
.notif small { display: block; color: #888; }
.notif .nx { background: none; border: none; color: #999; font-size: 17px; cursor: pointer; padding: 8px 10px;
  transition: color .15s ease; }
.notif .nx:hover { color: #d32f2f; }
.nk-failure { border-left-color: #d32f2f; }
.nk-repair { border-left-color: #2e7d32; }
.nk-overheat { border-left-color: #e65100; }
.nk-gcode_update { border-left-color: #1565c0; }
.nk-note { border-left-color: #f2c200; }
.nempty { padding: 16px; color: #888; text-align: center; }
#notif-clear { display: none; width: 100%; border: none; background: #f2f2f2; padding: 9px; cursor: pointer;
  font-weight: 600; border-radius: 0 0 8px 8px; transition: background .15s ease; }
#notif-clear:hover { background: #e4e4e4; }
.sq .hot { position: absolute; top: 1px; right: 1px; background: #b71c1c; color: #fff; font-size: 9px;
  line-height: 1; padding: 2px 3px; border-radius: 3px; animation: pulse 1.8s infinite; }
.badge { display: inline-block; padding: 1px 9px; border-radius: 9px; font-weight: 600; font-size: 13px; }
.b-block { background: #d32f2f; color: #fff; }
.b-degr { background: #f2c200; color: #111; }
.b-ok { background: #2e7d32; color: #fff; }
main { padding: 16px 20px; max-width: 1200px; margin: 0 auto; }
h2 { font-size: 16px; margin: 22px 0 8px; }
table { border-collapse: collapse; width: 100%; background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,.15); }
th { text-align: left; font-size: 12px; text-transform: uppercase; color: #666; padding: 7px 10px; border-bottom: 2px solid #ddd; }
td { padding: 8px 10px; border-bottom: 1px solid #eee; vertical-align: top; }
tr.blocked td { background: #fdecea; }
tr.degraded td { background: #fdf7e0; }
td.host, a.host { font-weight: 700; font-size: 17px; text-decoration: none; color: #111; }
.detail { color: #555; white-space: pre-line; font-size: 13px; }
.age { white-space: nowrap; }
.muted { color: #888; font-size: 13px; }
form.note { display: flex; gap: 6px; margin-top: 4px; }
form.note input[type=text] { flex: 1; padding: 4px 6px; }
.empty { padding: 30px; text-align: center; color: #777; background: #fff; }
.warnbar { background: #f2c200; color: #111; padding: 9px 14px; font-weight: 600;
  border-radius: 6px; margin-bottom: 10px; box-shadow: 0 1px 2px rgba(0,0,0,.15); }
form.wizard { background: #fff; padding: 14px 18px; box-shadow: 0 1px 2px rgba(0,0,0,.15); }
form.wizard fieldset { margin: 12px 0; border: 1px solid #ccc; }
form.wizard input, form.wizard select { padding: 3px 5px; }
form.wizard table td { border: none; padding: 3px 6px; }
"""


def fmt_age(start, end=None):
    try:
        t0 = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "?"
    t1 = datetime.strptime(end, "%Y-%m-%d %H:%M:%S") if end else datetime.now()
    mins = max(0, int((t1 - t0).total_seconds() // 60))
    if mins < 60:
        return f"{mins} min"
    if mins < 48 * 60:
        return f"{mins // 60} h {mins % 60} min"
    return f"{mins // (24 * 60)} d {(mins // 60) % 24} h"


def e(s):
    return html.escape(str(s if s is not None else ""))


def page(title, body, refresh=None):
    # soft refresh: re-fetch the page and swap <main> + header counts in place
    # (a meta refresh reloaded and "blinked" the whole page); inline scripts in
    # the fresh content are re-created so they execute again
    soft_refresh = f"""<script>
    setInterval(async () => {{
      try {{
        const r = await fetch(location.pathname + location.search);
        if (!r.ok) return;
        const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
        const fresh = doc.querySelector('main');
        const old = document.querySelector('main');
        if (fresh && old) {{
          fresh.classList.add('no-anim');
          old.replaceWith(fresh);
          fresh.querySelectorAll('script').forEach(s => {{
            const n = document.createElement('script');
            if (s.src) {{ n.src = s.src; }} else {{ n.textContent = s.textContent; }}
            s.replaceWith(n);
          }});
        }}
        const nc = doc.querySelector('.counts'), oc = document.querySelector('.counts');
        if (nc && oc) {{ oc.replaceWith(nc); }}
      }} catch (err) {{ /* offline - keep the current view */ }}
    }}, {int(refresh) * 1000});
    </script>""" if refresh else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title><style>{CSS}</style></head><body>
<div id="overlay" onclick="drawer(false)"></div>
<nav id="drawer">
  <div class="brand">GRIZZLA</div>
  <a href="/awaria/">Panel serwisowy</a>
  <a href="/awaria/history">Historia</a>
  <a href="/awaria/stats">Statystyki</a>
  <a href="/awaria/defs">Katalog błędów</a>
  <a href="/awaria/components">Części zamienne</a>
</nav>
<header><button id="burger" onclick="drawer()" title="Menu" aria-label="Menu"
  aria-expanded="false" aria-controls="drawer">&#9776;</button>
<h1><a href="/awaria/">GRIZZLA — panel serwisowy</a></h1>{body[0]}
<div id="bell-wrap">
  <button id="bell" onclick="notifPanel()" title="Powiadomienia" aria-label="Powiadomienia"
    aria-haspopup="true" aria-expanded="false" aria-controls="notif-panel">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/>
      <path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>
    <span id="bell-badge" class="hidden"></span>
  </button>
  <div id="notif-panel" class="hidden">
    <div id="notif-list"></div>
    <button id="notif-clear" onclick="clearNotifs()">Wyczyść wszystkie</button>
  </div>
</div></header>
<main>{body[1]}</main>
<script>
function drawer(open) {{
  const d = document.getElementById('drawer'), o = document.getElementById('overlay');
  const on = open === undefined ? !d.classList.contains('open') : open;
  d.classList.toggle('open', on); o.classList.toggle('show', on);
  if (on) {{ notifPanel(false); }}
  document.getElementById('burger').setAttribute('aria-expanded', on);
}}
function escText(s) {{ const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }}
function notifPanel(open) {{
  const p = document.getElementById('notif-panel');
  const on = open === undefined ? p.classList.contains('hidden') : open;
  p.classList.toggle('hidden', !on);
  if (on) {{ drawer(false); }}
  document.getElementById('bell').setAttribute('aria-expanded', on);
}}
// current-standard dismissal: click anywhere outside closes the panel,
// Escape closes both the panel and the drawer
document.addEventListener('click', ev => {{
  if (!ev.target.closest('#bell-wrap')) {{ notifPanel(false); }}
}});
document.addEventListener('keydown', ev => {{
  if (ev.key === 'Escape') {{ notifPanel(false); drawer(false); }}
}});
async function loadNotifs() {{
  try {{
    const d = await (await fetch('/awaria/api/notifications.json')).json();
    const badge = document.getElementById('bell-badge');
    badge.textContent = d.count > 99 ? '99+' : d.count;
    badge.classList.toggle('hidden', !d.count);
    document.getElementById('notif-clear').style.display = d.count ? 'block' : 'none';
    document.getElementById('notif-list').innerHTML = d.items.length ? d.items.map(n => {{
      const inner = '<small>' + n.created_at.slice(5, 16) + '</small>' + escText(n.text);
      const body = n.link ? '<a href="' + n.link + '">' + inner + '</a>'
                          : '<span>' + inner + '</span>';
      return '<div class="notif nk-' + n.kind + '">' + body +
             '<button class="nx" title="Usuń" onclick="dismissNotif(' + n.id + ')">&times;</button></div>';
    }}).join('') : '<p class="nempty">Brak powiadomień</p>';
  }} catch (err) {{}}
}}
async function dismissNotifs(payload) {{
  await fetch('/awaria/api/notifications/dismiss', {{method: 'POST',
    headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(payload)}});
  loadNotifs();
}}
function dismissNotif(id) {{ dismissNotifs({{id}}); }}
function clearNotifs() {{ dismissNotifs({{all: true}}); }}
loadNotifs();
setInterval(loadNotifs, 15000);
</script>
{soft_refresh}
</body></html>"""


def state_badge_of(f):
    """Current state of a failure row: repaired failures are green, never
    'BLOKADA' (that read as the printer's live state - field confusion on E6)."""
    if f["closed_at"]:
        return '<span class="badge b-ok">NAPRAWIONA</span>'
    return ('<span class="badge b-block">BLOKADA</span>'
            if f["blocking"] else '<span class="badge b-degr">DZIAŁA</span>')


def failure_row(f, show_host=True):
    cls = "" if f["closed_at"] else (
        "blocked" if f["blocking"] else "degraded")
    host_cell = (
        f'<td class="host"><a class="host" href="/awaria/printer/'
        f'{urllib.parse.quote(f["hostname"])}">{e(f["hostname"])}</a></td>'
        if show_host else "")
    closed = f'<div class="muted">naprawiona {e(f["closed_at"])} ({e(f["closed_by"] or "")}), po {fmt_age(f["opened_at"], f["closed_at"])}</div>' if f[
        "closed_at"] else ""
    note = f'<div class="muted">🛠 {e(f["repair_note"])}</div>' if f[
        "repair_note"] else ""
    return f"""<tr class="{cls}">{host_cell}
      <td>{state_badge_of(f)}</td>
      <td><a href="/awaria/failure/{f['id']}"><b>{e(f['label'])}</b></a>
          <div class="detail">{e(f['detail'])}</div>{closed}{note}</td>
      <td class="age">{e(f['opened_at'])}<br><b>{fmt_age(f['opened_at'], f['closed_at'])}</b></td>
    </tr>"""


def flag_chips(db, host, removable=False):
    # each removable chip IS a delete form: a <form> nested in a <span>/<p> is
    # invalid HTML and browsers break it apart (the original "cannot delete
    # RND" bug) - as the chip element itself it is valid and the x works
    chips = []
    for f in db.execute(
            "SELECT * FROM printer_flags WHERE hostname=? ORDER BY id",
        (host, )):
        if removable:
            chips.append(
                f'<form method="post" action="/awaria/printer/{urllib.parse.quote(host)}/flag_del"'
                f' class="chip" style="background:{e(f["color"])}">'
                f'<input type="hidden" name="id" value="{f["id"]}">{e(f["text"])}'
                f'<button class="chip-x" title="Usuń oznaczenie">×</button></form>'
            )
        else:
            chips.append(
                f'<span class="chip" style="background:{e(f["color"])}">{e(f["text"])}</span>'
            )
    return " ".join(chips)


def flag_suggestions(db, host):
    """One-click chips of recently used tags (text+color) not yet on this
    printer, plus recent color swatches for the add-flag form."""
    own = {
        (r["text"], r["color"])
        for r in db.execute(
            "SELECT text, color FROM printer_flags WHERE hostname=?", (host, ))
    }
    recent = db.execute(
        "SELECT text, color, MAX(id) m FROM printer_flags"
        " GROUP BY text, color ORDER BY m DESC LIMIT 12").fetchall()
    quoted = urllib.parse.quote(host)
    tags = "".join(
        f'<form method="post" action="/awaria/printer/{quoted}/flag_add" class="chip ghost"'
        f' style="background:{e(r["color"])}">'
        f'<input type="hidden" name="text" value="{e(r["text"])}">'
        f'<input type="hidden" name="color" value="{e(r["color"])}">'
        f'<button class="chip-btn" title="Dodaj to oznaczenie">+ {e(r["text"])}</button></form>'
        for r in recent if (r["text"], r["color"]) not in own)[:2000]

    colors = db.execute("SELECT color, MAX(id) m FROM printer_flags"
                        " GROUP BY color ORDER BY m DESC LIMIT 8").fetchall()
    swatches = "".join(
        f'<button type="button" class="swatch" style="background:{e(c["color"])}"'
        f' title="{e(c["color"])}" onclick="document.getElementById(\'flagcolor\').value=\'{e(c["color"])}\'"></button>'
        for c in colors)
    return tags, swatches


def render_map(db):
    """Farm layout: 4 zones of sections (left to right N M L | A B C D E |
    G H I | J K), each section = 2 columns x 3 rows, numbered row by row
    (1-2 / 3-4 / 5-6). Hostname = section letter + number, e.g. E6."""
    zones = [["N", "M", "L"], ["A", "B", "C", "D", "E"], ["G", "H", "I"],
             ["J", "K"]]

    info = {}
    for p in db.execute(
            "SELECT hostname FROM printers UNION SELECT DISTINCT hostname FROM events"
    ):
        h = p["hostname"]
        opens = db.execute(
            "SELECT COUNT(*) c, MAX(blocking) b FROM failures"
            " WHERE hostname=? AND closed_at IS NULL", (h, )).fetchone()
        state, cls = (("BLOKADA", "blocked") if opens["b"] else
                      (("USZKODZONA", "degraded") if opens["c"] else
                       ("SPRAWNA", "ok")))
        last = db.execute(
            "SELECT closed_at, label FROM failures WHERE hostname=?"
            " AND closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 1",
            (h, )).fetchone()
        online = is_online(h)
        info[h] = {
            "state":
            state,
            "cls":
            cls if online else "off",
            "open":
            opens["c"],
            "online":
            online,
            "repair":
            f"{last['closed_at'][:10]} — {last['label'] or '?'}"
            if last else None
        }
        if live := live_of(h):
            v = live[0]
            if isinstance(v.get("print_filename"),
                          str) and v["print_filename"]:
                progress = f" ({v['print_progress']:.0f}%)" if isinstance(
                    v.get("print_progress"), float) else ""
                info[h]["file"] = v["print_filename"] + progress
                info[h]["prog"] = v["print_progress"] if isinstance(
                    v.get("print_progress"), float) else 0.0
            temps = []
            if isinstance(v.get("temp_noz"), float):
                temps.append(f"dysza {v['temp_noz']:.0f}°")
            if isinstance(v.get("temp_bed"), float):
                temps.append(f"stół {v['temp_bed']:.0f}°")
            if is_overheated(h):
                mcu = v.get("temp_mcu")
                brd = v.get("temp_brd")
                info[h]["hot"] = True
                temps.append("UWAGA: elektronika " + "/".join(
                    f"{x:.0f}°" for x in (mcu, brd) if isinstance(x, float)))
            if temps:
                info[h]["temps"] = ", ".join(temps)

    def cell(host, n):
        p = info.get(host)
        cls = p["cls"] if p else "off"
        bar = hot = ""
        if p and cls != "off" and "prog" in p:
            width = max(p["prog"], 10)  # a sliver stays visible at 0-10%
            bar = f'<span class="prog"><i style="width:{width:.0f}%"></i></span>'
        if p and p.get("hot"):
            hot = '<b class="hot" title="Wysoka temperatura elektroniki">H</b>'
        return (f'<a class="sq {cls}" href="/awaria/printer/{host}"'
                f' data-host="{host}">{n}{bar}{hot}</a>')

    zone_html = []
    for zone in zones:
        sections = []
        for s in zone:
            cells = "".join(cell(f"{s}{n}", n) for n in range(1, 7))
            sections.append(
                f'<div class="section"><div class="sec-label">{s}</div>'
                f'<div class="sec-grid">{cells}</div></div>')
        zone_html.append(f'<div class="zone">{"".join(sections)}</div>')

    legend = """<div class="map-legend">
      <span class="sq mini ok"></span> sprawna
      <span class="sq mini ok"><span class="prog"><i style="width:55%"></i></span></span> drukuje
      <span class="sq mini degraded"></span> uszkodzona
      <span class="sq mini" style="background:#d32f2f"></span> blokada
      <span class="sq mini off"></span> offline
    </div>"""

    return f"""<div class="farm-map">{''.join(zone_html)}</div>{legend}<div id="tip"></div>
    <script>
    (function() {{
      const P = {json.dumps(info, ensure_ascii=False).replace("<", "\\u003c")};
      const tip = document.getElementById('tip');
      document.querySelectorAll('.sq').forEach(el => {{
        el.addEventListener('mouseenter', () => {{
          const h = el.dataset.host, p = P[h];
          let text = '<b>' + h + '</b><br>';
          if (!p) {{
            text += 'Niepodłączona do sieci (brak zgłoszeń)';
          }} else {{
            text += (p.online ? '🟢 online' : '⚪ offline') + '<br>Stan: ' + p.state
                 + (p.open ? ' (' + p.open + ' otw.)' : '')
                 + (p.file ? '<br>Drukuje: ' + escText(p.file) : '')
                 + (p.temps ? '<br>' + p.temps : '')
                 + '<br>Ostatnia naprawa: ' + escText(p.repair || 'brak');
          }}
          tip.innerHTML = text;
          const r = el.getBoundingClientRect();
          tip.style.left = Math.min(r.left + window.scrollX + 22, window.scrollX + document.documentElement.clientWidth - 280) + 'px';
          tip.style.top = (r.bottom + window.scrollY + 6) + 'px';
          tip.classList.add('show');
        }});
        el.addEventListener('mouseleave', () => tip.classList.remove('show'));
      }});
    }})();
    </script>"""


def offsite_backup_warning():
    """Yellow bar on the home page when the off-device backup goes stale."""
    try:
        with open(OFFSITE_STAMP) as f:
            age = time.time() - float(f.read().strip())
    except (OSError, ValueError):
        age = None
    if age is not None and age < OFFSITE_MAX_AGE_S:
        return ""
    detail = (f"ostatni {age / 86400:.1f} dn. temu"
              if age is not None else "jeszcze nigdy nie wykonany")
    return ('<div class="warnbar">&#9888; Backup off-site nieaktualny — '
            f'{detail} (sprawdź gcode-nas-backup)</div>')


def render_home(db):
    open_f = db.execute("SELECT * FROM failures WHERE closed_at IS NULL"
                        " ORDER BY blocking DESC, opened_at ASC").fetchall()
    n_block = sum(1 for f in open_f if f["blocking"])
    n_degr = len(open_f) - n_block

    counts = (
        f'<div class="counts"><span class="badge b-block{" alive" if n_block else ""}">{n_block} BLOKAD</span> '
        f'<span class="badge b-degr">{n_degr} USZKODZONYCH</span></div>')

    if open_f:
        rows = "\n".join(failure_row(f) for f in open_f)
        failures_html = (
            f'<table><tr><th>Drukarka</th><th>Stan</th>'
            f'<th>Awaria</th><th>Zgłoszona / czas</th></tr>{rows}</table>')
    else:
        failures_html = '<div class="empty">Brak aktywnych awarii 🎉</div>'

    # printers overview: last event + open counts + 30-day blocked downtime
    printers = db.execute(
        "SELECT DISTINCT hostname FROM events ORDER BY hostname").fetchall()
    cutoff = (datetime.now() -
              timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    prows = []
    for p in printers:
        h = p["hostname"]
        opens = db.execute(
            "SELECT COUNT(*) c, MAX(blocking) b FROM failures"
            " WHERE hostname=? AND closed_at IS NULL", (h, )).fetchone()
        last = db.execute(
            "SELECT received_at FROM events WHERE hostname=?"
            " ORDER BY id DESC LIMIT 1", (h, )).fetchone()
        downtime = 0
        for f in db.execute(
                "SELECT opened_at, closed_at FROM failures WHERE hostname=?"
                " AND blocking=1 AND (closed_at IS NULL OR closed_at>?)",
            (h, cutoff)):
            t0 = max(f["opened_at"], cutoff)
            t1 = f["closed_at"] or now_str()
            try:
                downtime += max(0,
                                (datetime.strptime(t1, "%Y-%m-%d %H:%M:%S") -
                                 datetime.strptime(
                                     t0, "%Y-%m-%d %H:%M:%S")).total_seconds())
            except ValueError:
                pass
        state = ('<span class="badge b-block">BLOKADA</span>'
                 if opens["b"] else
                 ('<span class="badge b-degr">USZKODZONA</span>'
                  if opens["c"] else '<span class="badge b-ok">OK</span>'))
        fw = gc = "—"
        if live := live_of(h):
            v = live[0]
            fw = e(v["fw_version"]) if isinstance(
                v.get("fw_version"), str) and v["fw_version"] else "—"
            gc = e(v["gcode_release"]) if isinstance(
                v.get("gcode_release"), str) and v["gcode_release"] else "—"
        prows.append(
            f"""<tr><td class="host"><a class="host" href="/awaria/printer/{urllib.parse.quote(h)}">{e(h)}</a>
            {flag_chips(db, h)}</td>
            <td>{state}</td><td>{opens['c']}</td>
            <td>{downtime / 3600:.1f} h</td>
            <td class="muted">{fw}</td><td class="muted">{gc}</td>
            <td class="muted">{e(last['received_at'] if last else '-')}</td></tr>"""
        )
    printers_html = ('<table><tr><th>Drukarka</th><th>Stan</th><th>Otwarte awarie</th>'
                     '<th>Przestój (30 dni)</th><th>Firmware</th><th>G-code</th><th>Ostatnie zdarzenie</th></tr>'
                     + "\n".join(prows) + "</table>") if prows else \
        '<div class="empty">Żadna drukarka jeszcze nic nie zgłosiła.</div>'

    body = f"""{offsite_backup_warning()}<h2>Aktywne awarie</h2>{failures_html}
    <div class="sec-head"><h2>Drukarki</h2>
      <div class="view-toggle">
        <button class="tab" data-view="map" onclick="setView('map')">Mapa</button>
        <button class="tab" data-view="list" onclick="setView('list')">Lista</button>
      </div>
    </div>
    <div id="view-map">{render_map(db)}</div>
    <div id="view-list">{printers_html}</div>
    <script>
    function setView(v) {{
      localStorage.setItem('printer_view', v);
      document.getElementById('view-map').classList.toggle('hidden', v !== 'map');
      document.getElementById('view-list').classList.toggle('hidden', v !== 'list');
      document.querySelectorAll('.view-toggle .tab').forEach(t =>
        t.classList.toggle('active', t.dataset.view === v));
    }}
    setView(localStorage.getItem('printer_view') || 'map');
    </script>"""
    return page("GRIZZLA — panel serwisowy", (counts, body), refresh=15)


def render_telemetry(host):
    """Inner HTML of the live-telemetry card; also served as a partial for
    the in-place background refresh on the printer page."""
    live = live_of(host)
    if not live:
        return (
            '<p class="muted">Brak danych — drukarka nie wysyła metryk '
            '(wymaga firmware ≥ 11240 albo ręcznego włączenia w Settings → Metrics).</p>'
        )
    v, updated = live
    rows = []
    if isinstance(v.get("print_filename"), str) and v["print_filename"]:
        rows.append(("Drukowany plik", e(v["print_filename"])))
        if isinstance(v.get("print_progress"), float):
            rows.append(("Postęp", f"{v['print_progress']:.0f}%"))
    else:
        rows.append(
            ("Drukowany plik", '<span class="muted">nic nie drukuje</span>'))
    if isinstance(v.get("temp_noz"), float):
        target = f" / {v['ttemp_noz']:.0f}°C" if isinstance(
            v.get("ttemp_noz"), float) and v["ttemp_noz"] else ""
        rows.append(("Dysza", f"{v['temp_noz']:.1f}°C{target}"))
    if isinstance(v.get("temp_bed"), float):
        target = f" / {v['ttemp_bed']:.0f}°C" if isinstance(
            v.get("ttemp_bed"), float) and v["ttemp_bed"] else ""
        rows.append(("Stół", f"{v['temp_bed']:.1f}°C{target}"))
    if isinstance(v.get("temp_brd"), float):
        rows.append(("Płyta xBuddy", f"{v['temp_brd']:.1f}°C"))
    if isinstance(v.get("temp_mcu"), float):
        rows.append(("MCU", f"{v['temp_mcu']:.0f}°C"))
    if isinstance(v.get("fw_version"), str) and v["fw_version"]:
        rows.append(("Firmware", e(v["fw_version"])))
    if isinstance(v.get("gcode_release"), str) and v["gcode_release"]:
        rows.append(("Wydanie g-code", e(v["gcode_release"])))
    return (
        "<table class='plain'>" +
        "".join(f"<tr><td class='muted'>{k}</td><td><b>{val}</b></td></tr>"
                for k, val in rows) +
        f"</table><p class='muted'>aktualizacja {int(time.time() - updated)} s temu</p>"
    )


def history_columns(host):
    """uPlot column arrays: [t, noz, tnoz, bed, tbed, brd]"""
    with live_lock:
        points = list(HISTORY.get(host, ()))
    return [[p[i] for p in points] for i in range(6)]


def render_printer(db, host):
    quoted = urllib.parse.quote(host)
    printer = db.execute("SELECT * FROM printers WHERE hostname=?",
                         (host, )).fetchone()
    opens = db.execute(
        "SELECT COUNT(*) c, MAX(blocking) b FROM failures"
        " WHERE hostname=? AND closed_at IS NULL", (host, )).fetchone()
    state = ('<span class="badge b-block">BLOKADA</span>' if opens["b"] else
             ('<span class="badge b-degr">USZKODZONA</span>'
              if opens["c"] else '<span class="badge b-ok">SPRAWNA</span>'))

    # components with the date of their last replacement on this printer
    comp_rows = []
    components = db.execute(
        "SELECT c.id, c.name, MAX(m.done_at) last FROM components c"
        " LEFT JOIN maintenance m ON m.component_id = c.id AND m.hostname = ?"
        " GROUP BY c.id ORDER BY c.position, c.id", (host, )).fetchall()
    for c in components:
        comp_rows.append(
            f"<tr><td>{e(c['name'])}</td>"
            f"<td>{e(c['last'][:10]) if c['last'] else '<span class=muted>—</span>'}</td></tr>"
        )
    comp_options = "".join(f'<option value="{c["id"]}">{e(c["name"])}</option>'
                           for c in components)

    suggested_tags, color_swatches = flag_suggestions(db, host)
    built_on = printer["built_on"] if printer and printer["built_on"] else ""
    info_html = f"""
    <div class="cards">
      <div class="card">
        <h3>Stan</h3>
        <p style="font-size:18px">{state}</p>
        <div class="chips">Oznaczenia: {flag_chips(db, host, removable=True) or '<span class="muted">brak</span>'}</div>
        {f'<div class="chips muted">Ostatnio używane: {suggested_tags}</div>' if suggested_tags else ''}
        <form method="post" action="/awaria/printer/{quoted}/flag_add" class="inline-form">
          <input name="text" size="10" maxlength="16" placeholder="np. RND" required>
          <input type="color" name="color" id="flagcolor" value="#607d8b" title="Kolor">
          {color_swatches}
          <input type="submit" value="Dodaj oznaczenie">
        </form>
        <form method="post" action="/awaria/printer/{quoted}/update" class="inline-form">
          <label>Data budowy: <input type="date" name="built_on" value="{e(built_on)}"></label>
          <label>Adres IP: <input name="last_ip" size="14" placeholder="auto (mDNS)"
                 value="{e(printer['last_ip'] if printer and printer['last_ip'] else '')}"></label>
          <input type="submit" value="Zapisz">
        </form>
        <p class="muted">{'🟢 online' if is_online(host) else '⚪ offline / brak IP'} —
           IP wykrywane automatycznie (skan sieci + mDNS co ok. 4 min); wpis ręczny nadpisuje.</p>
      </div>
      <div class="card">
        <h3>Telemetria (na żywo)</h3>
        <div id="telemetry-body">{render_telemetry(host)}</div>
      </div>
      <div class="card" style="flex-basis:100%">
        <h3>Temperatury (ostatnie 30 min)</h3>
        <div id="tchart"><p class="muted">Zbieranie danych...</p></div>
      </div>
      <div class="card">
        <h3>Komponenty — ostatnia wymiana / konserwacja</h3>
        <table class="plain">{''.join(comp_rows)}</table>
        <form method="post" action="/awaria/printer/{quoted}/maintenance" class="inline-form">
          <select name="component_id">{comp_options}</select>
          <input type="date" name="done_at" value="{datetime.now().strftime('%Y-%m-%d')}">
          <input type="submit" value="Odnotuj wymianę">
        </form>
      </div>
    </div>"""

    failures = db.execute(
        "SELECT * FROM failures WHERE hostname=?"
        " ORDER BY (closed_at IS NULL) DESC, opened_at DESC LIMIT 200",
        (host, )).fetchall()
    events = db.execute(
        "SELECT * FROM events WHERE hostname=? ORDER BY id DESC LIMIT 300",
        (host, )).fetchall()

    frows = "\n".join(failure_row(f, show_host=False) for f in failures) or ""
    failures_html = (
        f'<table><tr><th>Stan</th><th>Awaria</th><th>Zgłoszona / czas</th></tr>{frows}</table>'
        if failures else '<div class="empty">Brak awarii w historii.</div>')
    erows = "\n".join(
        f"<tr><td class='muted'>{e(ev['received_at'])}</td><td>{e(ev['action'])}</td>"
        f"<td><b>{e(ev['label'])}</b><div class='detail'>{e(ev['detail'])}</div></td></tr>"
        for ev in events)
    events_html = (
        f"<table><tr><th>Odebrano</th><th>Zdarzenie</th><th>Opis</th></tr>{erows}</table>"
        if events else '<div class="empty">Brak zdarzeń.</div>')
    # background refresh of just the telemetry card + chart (a full-page swap
    # would wipe half-filled forms on this page)
    chart_js = f"""
    <link rel="stylesheet" href="/awaria/static/uPlot.min.css">
    <script src="/awaria/static/uPlot.iife.min.js"></script>
    <script>
    (function() {{
      const host = encodeURIComponent({json.dumps(host).replace("<", "\\u003c")});
      let chart = null;
      async function tick() {{
        try {{
          const body = await (await fetch('/awaria/partial/telemetry/' + host)).text();
          document.getElementById('telemetry-body').innerHTML = body;
          const data = await (await fetch('/awaria/api/history/' + host)).json();
          if (data[0] && data[0].length > 1) {{
            const el = document.getElementById('tchart');
            if (!chart) {{
              el.innerHTML = '';
              chart = new uPlot({{
                width: el.clientWidth || 800, height: 260,
                series: [ {{}},
                  {{label: 'Dysza', stroke: '#d32f2f', width: 2}},
                  {{label: 'Dysza cel', stroke: '#d32f2f', dash: [6, 6]}},
                  {{label: 'Stół', stroke: '#1565c0', width: 2}},
                  {{label: 'Stół cel', stroke: '#1565c0', dash: [6, 6]}},
                  {{label: 'Płyta xBuddy', stroke: '#2e7d32'}} ],
              }}, data, el);
              window.addEventListener('resize',
                () => chart && chart.setSize({{width: el.clientWidth, height: 260}}));
            }} else {{
              chart.setData(data);
            }}
          }}
        }} catch (err) {{ /* server unreachable - keep last view */ }}
      }}
      tick();
      setInterval(tick, 5000);
    }})();
    </script>"""

    body = (f"<h2>Drukarka {e(host)}</h2>{info_html}"
            f"<h2>Dziennik awarii</h2>{failures_html}"
            f"<h2>Dziennik zdarzeń</h2>{events_html}{chart_js}")
    return page(f"GRIZZLA — {host}", ("", body))


def render_failure(db, fid):
    f = db.execute("SELECT * FROM failures WHERE id=?", (fid, )).fetchone()
    if not f:
        return None
    quoted_host = urllib.parse.quote(f["hostname"])

    done = db.execute(
        "SELECT m.*, c.name FROM maintenance m LEFT JOIN components c ON c.id = m.component_id"
        " WHERE m.failure_id=? ORDER BY m.id", (fid, )).fetchall()
    done_html = "".join(
        f"<li>{e(m['done_at'][:10])} — <b>{e(m['name'] or m['action'] or '?')}</b>"
        f"{(' <span class=muted>(' + e(m['action']) + ')</span>') if m['name'] and m['action'] else ''}</li>"
        for m in done) or '<li class="muted">nic jeszcze nie odnotowano</li>'

    checkboxes = "".join(
        f'<label class="check"><input type="checkbox" name="component" value="{c["id"]}"> {e(c["name"])}</label>'
        for c in db.execute(
            "SELECT id, name FROM components ORDER BY position, id"))

    status = state_badge_of(f)
    closed_info = (
        f'<p>Naprawiona: <b>{e(f["closed_at"])}</b> ({e(f["closed_by"] or "?")}) '
        f'— czas awarii: <b>{fmt_age(f["opened_at"], f["closed_at"])}</b></p>'
        if f["closed_at"] else
        f'<p>Otwarta od: <b>{e(f["opened_at"])}</b> — trwa <b>{fmt_age(f["opened_at"])}</b></p>'
    )
    close_box = "" if f["closed_at"] else \
        '<p><label class="check"><input type="checkbox" name="close" checked> zamknij awarię (naprawa zakończona)</label></p>'

    body = f"""
    <p><a href="/awaria/printer/{quoted_host}">&larr; Drukarka {e(f['hostname'])}</a></p>
    <h2>Awaria: {e(f['label'])} {status}</h2>
    <div class="card">
      <div class="detail" style="font-size:15px">{e(f['detail'])}</div>
      {closed_info}
    </div>
    <h2>Naprawa</h2>
    <div class="card">
      <p><b>Wykonane czynności:</b></p><ul>{done_html}</ul>
      <form method="post" action="/awaria/failure/{fid}/repair">
        <p><b>Notatka serwisowa:</b><br>
           <textarea name="note" rows="3" style="width:100%">{e(f['repair_note'] or '')}</textarea></p>
        <p><b>Wymienione części / wykonana konserwacja:</b></p>
        <div class="checks">{checkboxes}</div>
        <p>Inne czynności: <input name="action" size="40" maxlength="120"
           placeholder="np. czyszczenie ekstrudera"></p>
        {close_box}
        <p><input type="submit" value="Zapisz naprawę"></p>
      </form>
    </div>"""
    return page(f"GRIZZLA — awaria {f['hostname']}", ("", body))


def render_components(db):
    rows = []
    for c in db.execute(
            "SELECT c.id, c.name, c.position, COUNT(m.id) used FROM components c"
            " LEFT JOIN maintenance m ON m.component_id = c.id"
            " GROUP BY c.id ORDER BY c.position, c.id"):
        delete = (
            "" if c["used"] else
            f'<form method="post" action="/awaria/components/del" class="inline-form">'
            f'<input type="hidden" name="id" value="{c["id"]}">'
            f'<input type="submit" value="usuń"></form>')
        rows.append(f"""<tr data-id="{c['id']}">
            <td class="drag" title="Przeciągnij, aby zmienić kolejność">&#8801;</td>
            <td><form method="post" action="/awaria/components/edit" class="inline-form">
                <input type="hidden" name="id" value="{c['id']}">
                <input name="name" value="{e(c['name'])}" size="30" maxlength="40" required>
                <input type="submit" value="Zapisz">
            </form></td>
            <td class='muted'>{c['used']} wpisów</td><td>{delete}</td></tr>""")
    body = f"""
    <h2>Części zamienne i czynności serwisowe</h2>
    <table id="components"><thead><tr><th></th><th>Nazwa (edytuj i zapisz)</th><th>Użycia</th><th></th></tr></thead>
    <tbody>{''.join(rows)}</tbody></table>
    <form method="post" action="/awaria/components/add" class="inline-form" style="margin-top:10px">
      <input name="name" size="34" maxlength="40" placeholder="nowa część / czynność" required>
      <input type="submit" value="Dodaj">
    </form>
    <p class="muted">Kolejność listy (przeciągnij za &#8801;) obowiązuje we wszystkich formularzach
    napraw. Pozycje z historią wpisów nie mogą być usunięte.</p>
    <script src="/awaria/static/Sortable.min.js"></script>
    <script>
    new Sortable(document.querySelector('#components tbody'), {{
      handle: '.drag', animation: 180, easing: 'cubic-bezier(.22,.9,.32,1)',
      ghostClass: 'ghost', chosenClass: 'drag-active',
      onEnd: () => {{
        const ids = [...document.querySelectorAll('#components tbody tr[data-id]')].map(r => +r.dataset.id);
        fetch('/awaria/components/reorder', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
               body: JSON.stringify({{ids}})}}).then(r => {{ if (!r.ok) location.reload(); }});
      }},
    }});
    </script>"""
    return page("GRIZZLA — części zamienne", ("", body))


def to_epoch(text):
    try:
        return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp())
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------- usage statistics


def effective_seconds(t0, t1, include_weekends):
    """Seconds between two epochs, optionally counting Mon-Fri only."""
    if t1 <= t0:
        return 0
    if include_weekends:
        return t1 - t0
    total = 0
    cur = t0
    while cur < t1:
        d = datetime.fromtimestamp(cur)
        day_end = int(datetime(d.year, d.month, d.day).timestamp()) + 86400
        seg_end = min(day_end, t1)
        if d.weekday() < 5:
            total += seg_end - cur
        cur = seg_end
    return total


def merge_intervals(intervals):
    """Overlapping (start, end) epochs merged, so time is never counted twice."""
    merged = []
    for start, end in sorted(i for i in intervals if i[1] > i[0]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def printer_usage(db, host, t_from, t_to, include_weekends):
    """(printing_s, down_s, active) for one printer in the window; active is
    False when there is nothing to count (not connected, no data) -> skip."""
    now = int(time.time())
    from_str = datetime.fromtimestamp(t_from).strftime("%Y-%m-%d %H:%M:%S")
    to_str = datetime.fromtimestamp(t_to).strftime("%Y-%m-%d %H:%M:%S")

    prints = [
        (max(to_epoch(r["started_at"]), t_from),
         min(to_epoch(r["ended_at"]) if r["ended_at"] else now, t_to))
        for r in db.execute(
            "SELECT started_at, ended_at FROM print_log WHERE hostname=?"
            " AND started_at <= ? AND (ended_at IS NULL OR ended_at >= ?)", (
                host, to_str, from_str))
    ]
    fails = [(
        max(to_epoch(r["opened_at"]), t_from),
        min(to_epoch(r["closed_at"]) if r["closed_at"] else now, t_to)
    ) for r in db.execute(
        "SELECT opened_at, closed_at FROM failures WHERE hostname=? AND blocking=1"
        " AND opened_at <= ? AND (closed_at IS NULL OR closed_at >= ?)", (
            host, to_str, from_str))]

    if not prints and not fails and not is_online(host):
        return 0, 0, False
    printing = sum(
        effective_seconds(a, b, include_weekends)
        for a, b in merge_intervals(prints))
    down = sum(
        effective_seconds(a, b, include_weekends)
        for a, b in merge_intervals(fails))
    return printing, down, True


def fmt_hours(seconds):
    return f"{seconds / 3600:.1f} h"


def render_stats(db, query):
    include_weekends = bool(query.get("weekends")) or not query  # default: on
    rng = (query.get("range") or ["7"])[0]
    now = int(time.time())
    custom_from = (query.get("from") or [""])[0]
    custom_to = (query.get("to") or [""])[0]
    if rng == "custom" and custom_from and custom_to:
        try:
            t_from = int(
                datetime.strptime(custom_from, "%Y-%m-%d").timestamp())
            t_to = int(datetime.strptime(custom_to,
                                         "%Y-%m-%d").timestamp()) + 86400
        except ValueError:
            t_from, t_to = now - 7 * 86400, now
    else:
        days = {"7": 7, "30": 30, "365": 365}.get(rng, 7)
        rng = str(days)
        t_from, t_to = now - days * 86400, now
    t_to = min(t_to, now)

    hosts = {
        r["hostname"]: r["telemetry_since"]
        for r in db.execute(
            "SELECT hostname, telemetry_since FROM printers"
            " UNION SELECT hostname, NULL FROM events WHERE hostname NOT IN (SELECT hostname FROM printers)"
        )
    }

    rows = []
    farm_print = farm_down = farm_total = 0
    for h, telemetry_since in sorted(hosts.items()):
        # only printers that are connected AND currently report their printing
        # status (fw >= 11240 streaming print_filename) enter the statistics -
        # older-firmware printers have no print data and would look 100% idle
        live = live_of(h)
        reports_printing = bool(live) and isinstance(
            live[0].get("print_filename"), str)
        if not is_online(h) or not reports_printing:
            continue
        # the printer's window starts when it began reporting - time before
        # that would otherwise be counted as (fake) idle
        h_from = max(t_from,
                     to_epoch(telemetry_since)) if telemetry_since else t_from
        window = effective_seconds(h_from, t_to, include_weekends)
        if window <= 0:
            continue
        printing, down, _ = printer_usage(db, h, h_from, t_to,
                                          include_weekends)
        printing = min(printing, window)
        down = min(down, max(0, window - printing))
        idle = max(0, window - printing - down)
        farm_print += printing
        farm_down += down
        farm_total += window
        rows.append((h, printing, down, idle))

    def percentages(printing, down, total):
        if total <= 0:
            return 0.0, 0.0, 100.0
        p = printing * 100.0 / total
        d = down * 100.0 / total
        return p, d, max(0.0, 100.0 - p - d)

    donut = f"""
    <div class="stats-top">
      <div class="donut" id="donut">
        <div class="donut-hole"><b id="donut-pct">0%</b><span>druku</span></div>
      </div>
      <div class="legend">
        <p><span class="dot" style="background:#2e7d32"></span> Druk: <b id="lg-print"></b></p>
        <p><span class="dot" style="background:#d32f2f"></span> Awarie (blokada): <b id="lg-down"></b></p>
        <p><span class="dot" style="background:#f2c200"></span> Bezczynność: <b id="lg-idle"></b></p>
        <p class="muted"><span id="lg-count"></span> z {len(rows)} drukarek w sumie — tylko podłączone
        i raportujące status druku; czas każdej liczony od początku jej raportowania</p>
      </div>
    </div>"""

    # group by section (hostname letter prefix), numerically within a section
    def host_key(hostname):
        m = re.match(r"^([A-Za-z]+)(\d*)", hostname)
        return (m.group(1).upper(), int(m.group(2) or 0)) if m else (hostname,
                                                                     0)

    sections = {}
    for h, printing, down, idle in rows:
        sections.setdefault(host_key(h)[0], []).append(
            (h, printing, down, idle))

    groups = []
    for section in sorted(sections):
        printer_rows = []
        for h, printing, down, idle in sorted(sections[section],
                                              key=lambda r: host_key(r[0])):
            p, d, i = percentages(printing, down, printing + down + idle)
            printer_rows.append(
                f"""<div class="usage-row" data-print="{printing}" data-down="{down}" data-idle="{idle}">
              <input type="checkbox" class="p-check" checked title="Uwzględnij w sumie">
              <a class="host" href="/awaria/printer/{urllib.parse.quote(h)}">{e(h)}</a>
              <div class="ubar">
                <div style="width:{p:.2f}%;background:#2e7d32" title="Druk {p:.1f}% ({fmt_hours(printing)})"></div>
                <div style="width:{d:.2f}%;background:#d32f2f" title="Awarie {d:.1f}% ({fmt_hours(down)})"></div>
                <div style="width:{i:.2f}%;background:#f2c200" title="Bezczynność {i:.1f}% ({fmt_hours(idle)})"></div>
              </div>
              <span class="muted">{p:.0f}% druku ({fmt_hours(printing)}){f' · {d:.0f}% awarie' if d >= 0.5 else ''}</span>
            </div>""")
        groups.append(f"""<div class="sec-group">
          <label class="sec-toggle"><input type="checkbox" class="sec-check" checked>
            <b>Sekcja {e(section)}</b> <span class="muted">({len(printer_rows)})</span></label>
          {''.join(printer_rows)}
        </div>""")
    bars_html = "".join(
        groups) or '<div class="empty">Brak danych w wybranym okresie.</div>'

    recalc_js = """
    <script>
    (function() {
      const fmtH = s => (s / 3600).toFixed(1) + ' h';
      function recalc() {
        let p = 0, d = 0, i = 0, n = 0;
        document.querySelectorAll('.usage-row').forEach(row => {
          const on = row.querySelector('.p-check').checked;
          row.classList.toggle('excluded', !on);
          if (!on) { return; }
          p += +row.dataset.print; d += +row.dataset.down; i += +row.dataset.idle; n++;
        });
        const total = p + d + i;
        const pp = total ? p * 100 / total : 0, pd = total ? d * 100 / total : 0;
        document.getElementById('donut').style.background = 'conic-gradient(#2e7d32 0 ' + pp
          + '%, #d32f2f ' + pp + '% ' + (pp + pd) + '%, #f2c200 ' + (pp + pd) + '% 100%)';
        document.getElementById('donut-pct').textContent = pp.toFixed(0) + '%';
        document.getElementById('lg-print').textContent = pp.toFixed(1) + '% (' + fmtH(p) + ')';
        document.getElementById('lg-down').textContent = pd.toFixed(1) + '% (' + fmtH(d) + ')';
        document.getElementById('lg-idle').textContent = (total ? 100 - pp - pd : 100).toFixed(1) + '% (' + fmtH(i) + ')';
        document.getElementById('lg-count').textContent = n;
        // section checkbox states follow their printers (incl. indeterminate)
        document.querySelectorAll('.sec-group').forEach(g => {
          const boxes = [...g.querySelectorAll('.p-check')];
          const checked = boxes.filter(b => b.checked).length;
          const sec = g.querySelector('.sec-check');
          sec.checked = checked === boxes.length;
          sec.indeterminate = checked > 0 && checked < boxes.length;
        });
      }
      document.querySelectorAll('.p-check').forEach(b => b.addEventListener('change', recalc));
      document.querySelectorAll('.sec-check').forEach(sec => sec.addEventListener('change', () => {
        sec.closest('.sec-group').querySelectorAll('.p-check').forEach(b => { b.checked = sec.checked; });
        recalc();
      }));
      recalc();
    })();
    </script>"""

    range_options = "".join(
        f'<option value="{v}"{" selected" if v == rng else ""}>{label}</option>'
        for v, label in (("7", "1 tydzień"), ("30", "1 miesiąc"),
                         ("365", "1 rok"), ("custom", "własny zakres")))
    custom_display = "inline-flex" if rng == "custom" else "none"
    body = f"""
    <div class="sec-head"><h2>Statystyki wykorzystania</h2>
      <form method="get" action="/awaria/stats" class="inline-form">
        <select name="range" onchange="document.getElementById('custom-dates').style.display
            = this.value === 'custom' ? 'inline-flex' : 'none'">{range_options}</select>
        <span id="custom-dates" class="inline-form" style="display:{custom_display}">
          <input type="date" name="from" value="{e(custom_from)}"> —
          <input type="date" name="to" value="{e(custom_to)}"></span>
        <label><input type="checkbox" name="weekends" value="1"
               {"checked" if include_weekends else ""}> uwzględnij weekendy</label>
        <input type="submit" value="Pokaż">
      </form>
    </div>
    {donut}
    <h2>Drukarki</h2>
    <div class="card">{bars_html}</div>
    <p class="muted">Druk = sesje z dziennika wydruków (telemetria, od fw 11240); awarie = czas
    blokad z panelu; reszta okna = bezczynność. Najedź na pasek, aby zobaczyć godziny;
    odznacz drukarki lub całe sekcje, aby przeliczyć sumę na bieżąco.</p>
    {recalc_js}"""
    return page("GRIZZLA — statystyki", ("", body))


def render_history(db, query):
    host = (query.get("host") or [""])[0][:32]
    range_h = (query.get("range") or ["24"])[0]
    range_h = range_h if range_h in ("6", "24", "48", "96") else "24"
    try:
        t_from = int((query.get("from") or ["0"])[0])
        t_to = int((query.get("to") or ["0"])[0])
    except ValueError:
        t_from = t_to = 0
    if not t_from or not t_to:
        t_to = int(time.time())
        t_from = t_to - int(range_h) * 3600

    hosts = [
        r["hostname"] for r in db.execute(
            "SELECT hostname FROM printers UNION SELECT DISTINCT hostname FROM events ORDER BY 1"
        )
    ]
    host_options = '<option value="">— wybierz drukarkę —</option>' + "".join(
        f'<option value="{e(h)}"{" selected" if h == host else ""}>{e(h)}</option>'
        for h in hosts)
    range_options = "".join(
        f'<option value="{v}"{" selected" if v == range_h else ""}>{label}</option>'
        for v, label in (("6", "6 godzin"), ("24", "24 godziny"),
                         ("48", "2 dni"), ("96", "4 dni")))

    if host:
        chart_html = f"""
        <div class="card" style="flex-basis:100%"><h3>Temperatury — {e(host)}</h3>
          <div id="hchart"><p class="muted">Ładowanie...</p></div></div>
        <link rel="stylesheet" href="/awaria/static/uPlot.min.css">
        <script src="/awaria/static/uPlot.iife.min.js"></script>
        <script>
        (async function() {{
          const data = await (await fetch('/awaria/api/samples/{urllib.parse.quote(host)}?from={t_from}&to={t_to}')).json();
          const el = document.getElementById('hchart');
          if (!data[0] || data[0].length < 2) {{ el.innerHTML = '<p class="muted">Brak zapisanych danych w tym okresie.</p>'; return; }}
          el.innerHTML = '';
          new uPlot({{
            width: el.clientWidth || 900, height: 300,
            series: [ {{}},
              {{label: 'Dysza', stroke: '#d32f2f', width: 2}},
              {{label: 'Dysza cel', stroke: '#d32f2f', dash: [6, 6]}},
              {{label: 'Stół', stroke: '#1565c0', width: 2}},
              {{label: 'Stół cel', stroke: '#1565c0', dash: [6, 6]}},
              {{label: 'Płyta xBuddy', stroke: '#2e7d32'}} ],
          }}, data, el);
        }})();
        </script>"""
    else:
        chart_html = '<div class="empty">Wybierz drukarkę, aby zobaczyć wykres temperatur.</div>'

    week_ago = (datetime.now() -
                timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    if host:
        prints = db.execute(
            "SELECT * FROM print_log WHERE started_at>=? AND hostname=?"
            " ORDER BY started_at DESC LIMIT 200",
            (week_ago, host)).fetchall()
    else:
        prints = db.execute(
            "SELECT * FROM print_log WHERE started_at>=?"
            " ORDER BY started_at DESC LIMIT 200", (week_ago, )).fetchall()
    prows = []
    for p in prints:
        start_e = to_epoch(p["started_at"])
        end_e = to_epoch(p["ended_at"]) if p["ended_at"] else int(time.time())
        link = (f'/awaria/history?host={urllib.parse.quote(p["hostname"])}'
                f'&from={start_e - 300}&to={end_e + 300}')
        duration = fmt_age(p["started_at"], p["ended_at"]) if p["ended_at"] else \
            f'<span class="badge b-ok">w trakcie</span> {fmt_age(p["started_at"])}'
        prows.append(f"""<tr>
            <td class="host"><a class="host" href="/awaria/printer/{urllib.parse.quote(p['hostname'])}">{e(p['hostname'])}</a></td>
            <td><b>{e(p['file'])}</b></td>
            <td class="age">{e(p['started_at'])}</td><td>{duration}</td>
            <td><a href="{link}">wykres</a></td></tr>""")
    prints_html = (
        f'<table><tr><th>Drukarka</th><th>Plik</th><th>Start</th><th>Czas</th><th></th></tr>'
        f'{"".join(prows)}</table>' if prows else
        '<div class="empty">Brak zarejestrowanych wydruków w ostatnim tygodniu.</div>'
    )

    body = f"""
    <div class="sec-head"><h2>Historia telemetrii</h2>
      <form method="get" action="/awaria/history" class="inline-form">
        <select name="host">{host_options}</select>
        <select name="range">{range_options}</select>
        <input type="submit" value="Pokaż">
      </form>
    </div>
    {chart_html}
    <h2>Wydruki (ostatnie 7 dni)</h2>{prints_html}
    <p class="muted">Historia temperatur: 1 próbka / {FINE_EVERY_S} s na drukarkę, przechowywana
    {FINE_KEEP_S // 86400} dni; długie zakresy są uśredniane do ~3600 punktów — zawęź zakres
    (albo kliknij "wykres" przy wydruku), aby zobaczyć pełny detal. Wydruki wykrywane z
    telemetrii (wymaga firmware ≥ 11240).</p>"""
    return page("GRIZZLA — historia", ("", body))


# ------------------------------------------------- error catalog wizard

SEVERITY_NAMES = {
    0: "krytyczna (blokuje)",
    1: "operator decyduje",
    2: "tylko notatka"
}
# firmware buffer limits, in BYTES of UTF-8 (Polish letters take 2)
LABEL_MAX_B, QUESTION_MAX_B, ANSWER_MAX_B = 39, 47, 19


def utf8_clamp(text, max_bytes):
    raw = text.strip().encode("utf-8")[:max_bytes]
    while raw:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw[:-1]
    return ""


def bump_catalog_seq(db):
    row = db.execute(
        "SELECT value FROM meta WHERE key='catalog_seq'").fetchone()
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('catalog_seq', ?)",
        (str(int(row["value"]) + 1 if row else 1), ))


def render_defs_list(db):
    seq = db.execute(
        "SELECT value FROM meta WHERE key='catalog_seq'").fetchone()
    rows = []
    for d in db.execute("SELECT * FROM error_defs ORDER BY position, id"):
        try:
            n_questions = len(json.loads(d["questions"]))
        except json.JSONDecodeError:
            n_questions = 0
        badges = []
        if d["print_ctx"]:
            badges.append("plik+podłoże")
        if d["hidden"]:
            badges.append("ukryty")
        rows.append(
            f"""<tr data-id="{d['id']}"{' style="opacity:.5"' if d['hidden'] else ''}>
            <td class="drag" title="Przeciągnij, aby zmienić kolejność">&#8801;</td>
            <td><b>{e(d['label'])}</b></td>
            <td>{e(SEVERITY_NAMES.get(d['severity'], '?'))}</td>
            <td>{n_questions or '-'}</td><td class="muted">{e(', '.join(badges))}</td>
            <td><a href="/awaria/defs/{d['id']}">edytuj</a></td></tr>""")
    body = f"""
    <h2>Katalog błędów (wersja {e(seq['value'] if seq else '1')})
        <a style="float:right;font-weight:400" href="/awaria/defs/new">+ Zdefiniuj nowy błąd</a></h2>
    <table id="defs"><thead><tr><th></th><th>Nazwa</th><th>Ważność</th><th>Pytania</th><th></th><th></th></tr></thead>
    <tbody>{''.join(rows)}</tbody></table>
    <p class="muted">Kolejność listy = kolejność w menu drukarki — przeciągnij wiersze za uchwyt &#8801;.
    Drukarki pobierają katalog przy każdej synchronizacji g-code, przy starcie oraz z menu
    Settings → "Aktualizuj listę awarii". Błędów nie można usuwać (ich numery są zapisane
    w drukarkach) — zamiast tego oznacz je jako ukryte. Limity znaków wynikają z pamięci drukarki.</p>
    <script src="/awaria/static/Sortable.min.js"></script>
    <script>
    new Sortable(document.querySelector('#defs tbody'), {{
      handle: '.drag',
      animation: 180,          // the other rows glide up/down while dragging
      easing: 'cubic-bezier(.22,.9,.32,1)',
      ghostClass: 'ghost',
      chosenClass: 'drag-active',
      onEnd: () => {{
        const ids = [...document.querySelectorAll('#defs tbody tr[data-id]')].map(r => +r.dataset.id);
        fetch('/awaria/defs/reorder', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
               body: JSON.stringify({{ids}})}}).then(r => {{ if (!r.ok) location.reload(); }});
      }},
    }});
    </script>"""
    return page("GRIZZLA — katalog błędów", ("", body))


def render_def_form(db, def_row):
    d = def_row or {
        "id": "",
        "label": "",
        "severity": 1,
        "print_ctx": 0,
        "hidden": 0,
        "position": 100,
        "questions": "[]"
    }
    try:
        questions = json.loads(d["questions"])
    except json.JSONDecodeError:
        questions = []

    def sev_select(name, value, allow_none):
        options = [
            '<option value="-"%s>— bez zmiany —</option>' %
            (" selected" if value is None else "")
        ] if allow_none else []
        for k, label in SEVERITY_NAMES.items():
            options.append('<option value="%d"%s>%s</option>' %
                           (k, " selected" if value == k else "", e(label)))
        return '<select name="%s">%s</select>' % (name, "".join(options))

    q_blocks = []
    for qi in range(2):
        q = questions[qi] if qi < len(questions) else {}
        answers = q.get("answers", [])
        a_rows = []
        for ai in range(3):
            a = answers[ai] if ai < len(answers) else {}
            a_rows.append(f"""<tr><td>Odpowiedź {ai + 1}</td>
                <td><input name="q{qi}_a{ai}_text" maxlength="19" size="22"
                     value="{e(a.get('text', ''))}" placeholder="maks. {ANSWER_MAX_B} bajtów"></td>
                <td>ważność po tej odpowiedzi: {sev_select(f'q{qi}_a{ai}_sev', a.get('severity'), True)}</td></tr>"""
                          )
        q_blocks.append(
            f"""<fieldset><legend>Pytanie {qi + 1} (opcjonalne, min. 2 odpowiedzi)</legend>
            <input name="q{qi}_text" size="52" maxlength="47" value="{e(q.get('text', ''))}"
                   placeholder="treść pytania, maks. {QUESTION_MAX_B} bajtów">
            <table>{''.join(a_rows)}</table></fieldset>""")

    body = f"""
    <h2>{'Edycja błędu #%s' % e(d['id']) if def_row else 'Nowy błąd'}</h2>
    <form method="post" action="/awaria/defs/save" class="wizard">
      <input type="hidden" name="id" value="{e(d['id'])}">
      <p><label>Nazwa (na liście zgłaszania i na ekranie AWARIA):<br>
         <input name="label" size="52" maxlength="39" required value="{e(d['label'])}"></label></p>
      <p><label>Ważność: {sev_select('severity', d['severity'], False)}</label></p>
      <p><label><input type="checkbox" name="print_ctx" {'checked' if d['print_ctx'] else ''}>
         dołącz informacje o wydruku (plik + print sheet)</label><br>
         <label><input type="checkbox" name="hidden" {'checked' if d['hidden'] else ''}>
         ukryty (nie pokazuj w menu zgłaszania)</label></p>
      {''.join(q_blocks)}
      <p>Uwaga: odpowiedzi są przyciskami na ekranie drukarki — im krótsze, tym lepiej.
         Odpowiedź może zmienić ważność zgłoszenia (np. "Nie działa" → krytyczna).</p>
      <p><input type="submit" value="Zapisz i opublikuj nową wersję katalogu">
         <a href="/awaria/defs">anuluj</a></p>
    </form>"""
    return page("AWARIA — definicja błędu", ("", body))


def save_def(db, form):

    def field(name, default=""):
        return (form.get(name) or [default])[0]

    label = utf8_clamp(field("label"), LABEL_MAX_B)
    if not label:
        return None
    severity = int(field("severity", "1"))
    severity = severity if severity in (0, 1, 2) else 1

    questions = []
    for qi in range(2):
        text = utf8_clamp(field(f"q{qi}_text"), QUESTION_MAX_B)
        answers = []
        for ai in range(3):
            a_text = utf8_clamp(field(f"q{qi}_a{ai}_text"), ANSWER_MAX_B)
            if not a_text:
                continue
            a_sev = field(f"q{qi}_a{ai}_sev", "-")
            answer = {"text": a_text}
            if a_sev in ("0", "1", "2"):
                answer["severity"] = int(a_sev)
            answers.append(answer)
        if text and len(answers) >= 2:
            questions.append({"text": text, "answers": answers})

    def_id = field("id")
    values = (label, severity, 1 if "print_ctx" in form else 0,
              1 if "hidden" in form else 0,
              json.dumps(questions, ensure_ascii=False))
    if def_id:
        # position is managed by drag & drop on the list, keep it unchanged
        db.execute(
            "UPDATE error_defs SET label=?, severity=?, print_ctx=?, hidden=?,"
            " questions=? WHERE id=?", values + (int(def_id), ))
    else:
        row = db.execute(
            "SELECT value FROM meta WHERE key='next_error_id'").fetchone()
        new_id = int(row["value"]) if row else 15
        if new_id > 127:
            return None  # firmware slot encoding limit
        position = (db.execute(
            "SELECT COALESCE(MAX(position), 0) + 10 p FROM error_defs").
                    fetchone())["p"]
        db.execute(
            "INSERT INTO error_defs(label, severity, print_ctx, hidden,"
            " questions, position, id) VALUES (?,?,?,?,?,?,?)",
            values + (position, new_id))
        db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('next_error_id', ?)",
            (str(new_id + 1), ))
    bump_catalog_seq(db)
    db.commit()
    return True


def reorder_defs(db, ids):
    known = {r["id"] for r in db.execute("SELECT id FROM error_defs")}
    ids = [i for i in ids if isinstance(i, int) and i in known]
    if len(ids) != len(known):
        return False  # stale list in the browser - reload
    for position, def_id in enumerate(ids):
        db.execute("UPDATE error_defs SET position=? WHERE id=?",
                   ((position + 1) * 10, def_id))
    bump_catalog_seq(db)
    db.commit()
    return True


# ------------------------------------------------------------------ server


class Handler(BaseHTTPRequestHandler):
    server_version = "awaria/1.0"

    def log_message(self, fmt, *args):
        pass  # quiet; nginx has the access log

    def send_page(self, code, content, ctype="text/html; charset=utf-8"):
        data = content.encode() if isinstance(content, str) else content
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code, obj):
        self.send_page(code, json.dumps(obj), "application/json")

    def do_GET(self):
        path = urllib.parse.unquote(self.path.split("?")[0])
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        try:
            m = re.fullmatch(r"/awaria/static/([A-Za-z0-9._-]+)", path)
            if m:
                try:
                    with open(os.path.join(STATIC_DIR, m.group(1)), "rb") as f:
                        data = f.read()
                except OSError:
                    return self.send_page(404, "not found", "text/plain")
                ctype = ("application/javascript"
                         if path.endswith(".js") else "text/css" if
                         path.endswith(".css") else "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return

            # live-telemetry endpoints touch only in-memory state / their own
            # database - they neither need nor should wait for db_lock
            m = re.fullmatch(r"/awaria/partial/telemetry/([^/]{1,32})", path)
            if m:
                return self.send_page(200, render_telemetry(m.group(1)))
            m = re.fullmatch(r"/awaria/api/history/([^/]{1,32})", path)
            if m:
                return self.send_json(200, history_columns(m.group(1)))
            m = re.fullmatch(r"/awaria/api/samples/([^/]{1,32})", path)
            if m:
                try:
                    t_from = int((query.get("from") or ["0"])[0])
                    t_to = int((query.get("to") or ["0"])[0])
                except ValueError:
                    t_from = t_to = 0
                return self.send_json(
                    200, samples_columns(m.group(1), t_from, t_to))

            # pages are rendered while holding db_lock but SENT after
            # releasing it: a slow client draining its response must not
            # stall event ingestion and the workers
            with db_lock, open_db() as db:
                response = self.render_get(db, path, query)
            if response is not None:
                return self.send_page(*response)
            self.send_page(404, "not found", "text/plain")
        except Exception as ex:  # noqa: BLE001 - keep the server alive
            self.send_page(500, f"error: {ex}", "text/plain")

    HTML = "text/html; charset=utf-8"
    JSON = "application/json"

    def render_get(self, db, path, query):
        """Resolve a GET route to (status, content, content-type), or None
        for 404. Runs with db_lock held - must not touch the client socket."""
        if path in ("/awaria", "/awaria/"):
            return 200, render_home(db), self.HTML
        m = re.fullmatch(r"/awaria/printer/([^/]{1,32})", path)
        if m:
            return 200, render_printer(db, m.group(1)), self.HTML
        if path == "/awaria/api/failures.json":
            rows = db.execute(
                "SELECT * FROM failures WHERE closed_at IS NULL"
                " ORDER BY blocking DESC, opened_at").fetchall()
            return 200, json.dumps([dict(r) for r in rows]), self.JSON
        if path == "/awaria/api/catalog":
            # printers identify themselves here at boot + after every
            # g-code sync -> hostname/IP discovery for the ping worker.
            # Only farm-scheme or already-known names are registered: the
            # header is client-supplied and ends up in pages and JS.
            printer = (self.headers.get("X-Printer") or "").strip()[:32]
            if printer and printer != "?" and (
                    FARM_HOST_RE.match(printer)
                    or db.execute("SELECT 1 FROM printers WHERE hostname=?",
                                  (printer, )).fetchone()):
                db.execute(
                    "INSERT OR IGNORE INTO printers(hostname) VALUES (?)",
                    (printer, ))
                db.execute(
                    "UPDATE printers SET last_seen=?, last_ip=COALESCE(?, last_ip)"
                    " WHERE hostname=?",
                    (now_str(), self.headers.get("X-Forwarded-For"), printer))
                db.commit()
            return 200, render_catalog(db), "text/plain; charset=utf-8"
        if path == "/awaria/defs":
            return 200, render_defs_list(db), self.HTML
        if path == "/awaria/defs/new":
            return 200, render_def_form(db, None), self.HTML
        m = re.fullmatch(r"/awaria/defs/(\d+)", path)
        if m:
            row = db.execute("SELECT * FROM error_defs WHERE id=?",
                             (int(m.group(1)), )).fetchone()
            if row:
                return 200, render_def_form(db, row), self.HTML
            return None
        m = re.fullmatch(r"/awaria/failure/(\d+)", path)
        if m:
            if content := render_failure(db, int(m.group(1))):
                return 200, content, self.HTML
            return None
        if path == "/awaria/components":
            return 200, render_components(db), self.HTML
        if path == "/awaria/history":
            return 200, render_history(db, query), self.HTML
        if path == "/awaria/stats":
            return 200, render_stats(db, query), self.HTML
        if path == "/awaria/api/notifications.json":
            items = [
                dict(r) for r in db.execute(
                    "SELECT id, created_at, kind, text, link FROM notifications"
                    " WHERE dismissed=0 ORDER BY id DESC LIMIT 50")
            ]
            count = db.execute(
                "SELECT COUNT(*) c FROM notifications WHERE dismissed=0"
            ).fetchone()["c"]
            return 200, json.dumps({"count": count, "items": items}), self.JSON
        return None

    def do_POST(self):
        path = urllib.parse.unquote(self.path.split("?")[0])
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(length, 64 * 1024))
        try:
            if path == "/awaria/api/event":
                try:
                    data = json.loads(body.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    return self.send_json(400, {
                        "ok": False,
                        "error": "bad json"
                    })
                code, resp = handle_event(data,
                                          self.headers.get("X-Forwarded-For"))
                return self.send_json(code, resp)

            if path == "/awaria/api/notifications/dismiss":
                try:
                    payload = json.loads(body.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    return self.send_json(400, {"ok": False})
                with db_lock, open_db() as db:
                    if payload.get("all"):
                        db.execute(
                            "UPDATE notifications SET dismissed=1 WHERE dismissed=0"
                        )
                    elif isinstance(payload.get("id"), int):
                        db.execute(
                            "UPDATE notifications SET dismissed=1 WHERE id=?",
                            (payload["id"], ))
                    # housekeeping: drop long-dismissed rows
                    cutoff = (datetime.now() -
                              timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
                    db.execute(
                        "DELETE FROM notifications WHERE dismissed=1 AND created_at < ?",
                        (cutoff, ))
                    db.commit()
                return self.send_json(200, {"ok": True})

            if path == "/awaria/defs/reorder":
                try:
                    ids = json.loads(body.decode("utf-8",
                                                 "replace")).get("ids", [])
                except json.JSONDecodeError:
                    return self.send_json(400, {"ok": False})
                with db_lock, open_db() as db:
                    ok = reorder_defs(db, ids)
                return self.send_json(200 if ok else 409, {"ok": ok})

            if path == "/awaria/defs/save":
                form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
                with db_lock, open_db() as db:
                    saved = save_def(db, form)
                self.send_response(303)
                self.send_header(
                    "Location",
                    "/awaria/defs" if saved else "/awaria/defs/new")
                self.end_headers()
                return

            form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))

            def field(name, default=""):
                return (form.get(name) or [default])[0].strip()

            def redirect(location):
                self.send_response(303)
                self.send_header("Location", location)
                self.end_headers()

            m = re.fullmatch(r"/awaria/failure/(\d+)/repair", path)
            if m:
                fid = int(m.group(1))
                with db_lock, open_db() as db:
                    f = db.execute("SELECT * FROM failures WHERE id=?",
                                   (fid, )).fetchone()
                    if not f:
                        return self.send_page(404, "not found", "text/plain")
                    db.execute("UPDATE failures SET repair_note=? WHERE id=?",
                               (field("note")[:500], fid))
                    now = now_str()
                    for comp_id in form.get("component", []):
                        db.execute(
                            "INSERT INTO maintenance(hostname, component_id, done_at, failure_id)"
                            " VALUES (?,?,?,?)",
                            (f["hostname"], int(comp_id), now, fid))
                    if action := field("action")[:120]:
                        db.execute(
                            "INSERT INTO maintenance(hostname, action, done_at, failure_id)"
                            " VALUES (?,?,?,?)",
                            (f["hostname"], action, now, fid))
                    if "close" in form:
                        db.execute(
                            "UPDATE failures SET closed_at=?, closed_by='panel'"
                            " WHERE id=? AND closed_at IS NULL", (now, fid))
                    db.commit()
                return redirect(f"/awaria/failure/{fid}")

            m = re.fullmatch(
                r"/awaria/printer/([^/]{1,32})/(update|flag_add|flag_del|maintenance)",
                path)
            if m:
                host, verb = m.group(1), m.group(2)
                with db_lock, open_db() as db:
                    db.execute(
                        "INSERT OR IGNORE INTO printers(hostname) VALUES (?)",
                        (host, ))
                    if verb == "update":
                        db.execute(
                            "UPDATE printers SET built_on=? WHERE hostname=?",
                            (field("built_on")[:10], host))
                        ip = field("last_ip")
                        if ip == "" or re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}",
                                                    ip):
                            db.execute(
                                "UPDATE printers SET last_ip=NULLIF(?, '') WHERE hostname=?",
                                (ip, host))
                    elif verb == "flag_add" and field("text"):
                        color = field("color", "#607d8b")
                        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                            color = "#607d8b"
                        db.execute(
                            "INSERT INTO printer_flags(hostname, text, color) VALUES (?,?,?)",
                            (host, field("text")[:16], color))
                    elif verb == "flag_del":
                        db.execute(
                            "DELETE FROM printer_flags WHERE id=? AND hostname=?",
                            (int(field("id", "0")), host))
                    elif verb == "maintenance":
                        done_at = field("done_at")[:10] or now_str()[:10]
                        db.execute(
                            "INSERT INTO maintenance(hostname, component_id, done_at)"
                            " VALUES (?,?,?)",
                            (host, int(field("component_id", "0")), done_at))
                    db.commit()
                return redirect(f"/awaria/printer/{urllib.parse.quote(host)}")

            if path == "/awaria/components/add" and field("name"):
                with db_lock, open_db() as db:
                    position = (db.execute(
                        "SELECT COALESCE(MAX(position), 0) + 10 p FROM components"
                    ).fetchone())["p"]
                    db.execute(
                        "INSERT INTO components(name, position) VALUES (?,?)",
                        (field("name")[:40], position))
                    db.commit()
                return redirect("/awaria/components")

            if path == "/awaria/components/edit" and field("name"):
                with db_lock, open_db() as db:
                    db.execute("UPDATE components SET name=? WHERE id=?",
                               (field("name")[:40], int(field("id", "0"))))
                    db.commit()
                return redirect("/awaria/components")

            if path == "/awaria/components/reorder":
                try:
                    ids = json.loads(body.decode("utf-8",
                                                 "replace")).get("ids", [])
                except json.JSONDecodeError:
                    return self.send_json(400, {"ok": False})
                with db_lock, open_db() as db:
                    known = {
                        r["id"]
                        for r in db.execute("SELECT id FROM components")
                    }
                    ids = [i for i in ids if isinstance(i, int) and i in known]
                    if len(ids) != len(known):
                        return self.send_json(409, {"ok": False})
                    for position, comp_id in enumerate(ids):
                        db.execute(
                            "UPDATE components SET position=? WHERE id=?",
                            ((position + 1) * 10, comp_id))
                    db.commit()
                return self.send_json(200, {"ok": True})

            if path == "/awaria/components/del":
                with db_lock, open_db() as db:
                    comp_id = int(field("id", "0"))
                    used = db.execute(
                        "SELECT 1 FROM maintenance WHERE component_id=? LIMIT 1",
                        (comp_id, )).fetchone()
                    if not used:
                        db.execute("DELETE FROM components WHERE id=?",
                                   (comp_id, ))
                        db.commit()
                return redirect("/awaria/components")

            self.send_page(404, "not found", "text/plain")
        except Exception as ex:  # noqa: BLE001
            self.send_page(500, f"error: {ex}", "text/plain")


if __name__ == "__main__":
    init_db()
    open_tdb().close(
    )  # make sure the samples table exists before readers hit it
    threading.Thread(target=ping_worker, daemon=True).start()
    threading.Thread(target=metrics_worker, daemon=True).start()
    threading.Thread(target=telemetry_logger, daemon=True).start()
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
