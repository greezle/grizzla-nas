"""Main database: connections, schema, migrations, time and material
helpers. db_lock serializes ALL awaria.db access process-wide."""
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime

from awaria.config import DB_PATH, MANIFEST_PATH


db_lock = threading.Lock()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_pair():
    """(local-time string, epoch) of the same instant. The string columns
    stay authoritative for display; the *_ts epoch twins are what analytics
    and range queries should use (no DST ambiguity, integer compares)."""
    t = time.time()
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S"), int(t)


def to_epoch_or_none(text):
    """Local-time string (datetime or bare date) -> epoch; None when absent
    or unparsable. Used by writers of *_ts and by the backfill migration."""
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    return None


# recognized as a material when it appears as its own path segment - the
# g-code library is organized ".../<model>/<MATERIAL>/<file>.gcode"
MATERIAL_NAMES = {
    "PLA", "PETG", "ASA", "ABS", "TPU", "PCCF", "PC", "PA", "HIPS", "PVB",
    "PP", "FLEX"
}


def _material_token(text):
    """'PETG' -> 'PETG'; flex by shore grade in all its spellings
    ('TPU 95A', 'TPU-95A', bare '95A') -> canonical '95A'. None otherwise."""
    text = text.strip()
    if text in MATERIAL_NAMES:
        return text
    if m := re.fullmatch(r"(?:TPU|TPE)[ -]?(\d{2,3}A)|(\d{2,3}A)", text):
        return m.group(1) or m.group(2)
    return None


def material_of(path):
    """Material of a print from the library's conventions: a material-named
    path segment ('.../PETG/...', '.../95A/...') or a bracket tag in the file
    name itself ('[PETG] AEON JPB-L.gcode'). None = simply unknown (loose
    files, unorganized parts of the library)."""
    if not path:
        return None
    segments = str(path).upper().split("/")
    for seg in segments:
        if token := _material_token(seg):
            return token
    for m in re.finditer(r"\[([^\]]{1,10})\]", segments[-1]):
        if token := _material_token(m.group(1)):
            return token
    return None


_manifest_materials = (None, {})  # (mtime, basename -> material)


def material_of_basename(name):
    """Material of a print reported by basename only (the firmware streams
    just the file name, no path): resolved against the published MANIFEST's
    full paths. A basename that exists under two materials (PLA/ and PETG/
    variants of the same part) is ambiguous and maps to None."""
    global _manifest_materials
    try:
        mtime = os.stat(MANIFEST_PATH).st_mtime_ns
    except OSError:
        return None
    if _manifest_materials[0] != mtime:
        table = {}
        try:
            with open(MANIFEST_PATH, encoding="utf-8") as f:
                for line in f:
                    if not line.startswith("file "):
                        continue
                    parts = line.rstrip("\n").split(" ", 3)
                    if len(parts) < 4:
                        continue
                    base = parts[3].rsplit("/", 1)[-1].lower()
                    material = material_of(parts[3])
                    if base in table and table[base] != material:
                        table[base] = None  # ambiguous across materials
                    else:
                        table[base] = material
        except OSError:
            return None
        _manifest_materials = (mtime, table)
    return _manifest_materials[1].get(str(name).strip().lower())


def material_of_print(fname):
    """Material of a print: from the name itself when it carries a path
    (future firmware), else via the manifest basename lookup."""
    return material_of(fname) or material_of_basename(fname)


SESSION_GRACE_S = 15 * 60  # failures are often reported just after the print


def session_at(db, host, ts):
    """id of the print session running on `host` at epoch `ts` - or one that
    ended up to SESSION_GRACE_S before it (operators report failures right
    after the print stops). None when nothing matches, e.g. printers whose
    firmware predates telemetry and thus have no sessions at all."""
    if ts is None:
        return None
    row = db.execute(
        "SELECT id FROM print_log WHERE hostname=? AND started_ts<=?"
        " AND COALESCE(ended_ts, 1<<62) >= ? ORDER BY id DESC LIMIT 1",
        (host, ts, ts - SESSION_GRACE_S)).fetchone()
    return row["id"] if row else None


def classify_print(db, host, fname):
    """'service' when the printer has an open BLOCKING failure (test prints
    during repairs - the blocked screen's test-print override), 'test' for
    test files by name, 'prod' otherwise. Decided at print start."""
    if db.execute(
            "SELECT 1 FROM failures WHERE hostname=? AND blocking=1"
            " AND closed_at IS NULL LIMIT 1", (host, )).fetchone():
        return "service"
    if "test" in str(fname or "").lower():
        return "test"
    return "prod"


def net_log(db, host, event, detail=None):
    """One connectivity-audit row; the caller commits."""
    now, now_ts = now_pair()
    db.execute(
        "INSERT INTO net_log(at, at_ts, hostname, event, detail)"
        " VALUES (?,?,?,?,?)", (now, now_ts, host, event, detail))


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
        migrate(db)


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


def add_column(db, table, column_def):
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
    except sqlite3.OperationalError:
        pass  # already there (rerun after a crash mid-migration)


def migrate_1_epoch_columns(db):
    """Epoch twins of every local-time string column, backfilled. The strings
    remain for display; new writes fill both."""
    for table, columns in (
        ("events", ("received_ts",)),
        ("failures", ("opened_ts", "closed_ts")),
        ("print_log", ("started_ts", "ended_ts")),
        ("maintenance", ("done_ts",)),
        ("notifications", ("created_ts",)),
        ("printers", ("last_seen_ts", "telemetry_since_ts")),
    ):
        for col in columns:
            add_column(db, table, f"{col} INTEGER")
    db.create_function("to_epoch", 1, to_epoch_or_none)
    db.executescript("""
        UPDATE events SET received_ts = to_epoch(received_at);
        UPDATE failures SET opened_ts = to_epoch(opened_at),
                            closed_ts = to_epoch(closed_at);
        UPDATE print_log SET started_ts = to_epoch(started_at),
                             ended_ts = to_epoch(ended_at);
        UPDATE maintenance SET done_ts = to_epoch(done_at);
        UPDATE notifications SET created_ts = to_epoch(created_at);
        UPDATE printers SET last_seen_ts = to_epoch(last_seen),
                            telemetry_since_ts = to_epoch(telemetry_since);
    """)
    db.execute("CREATE INDEX IF NOT EXISTS failures_host_ts"
               " ON failures(hostname, opened_ts)")
    db.execute("CREATE INDEX IF NOT EXISTS print_log_host_ts"
               " ON print_log(hostname, started_ts)")


def migrate_2_sessions_material(db):
    """The job concept: failures and events point at the print session they
    interrupted (print_session_id -> print_log.id), sessions carry the
    material. events.answers is reserved for structured wizard answers
    (JSON array; firmware sends them flattened into detail today)."""
    add_column(db, "events", "print_session_id INTEGER")
    add_column(db, "events", "answers TEXT")
    add_column(db, "failures", "print_session_id INTEGER")
    add_column(db, "print_log", "material TEXT")
    for row in db.execute("SELECT id, file FROM print_log").fetchall():
        db.execute("UPDATE print_log SET material=? WHERE id=?",
                   (material_of_print(row["file"]), row["id"]))
    for table, ts_col in (("events", "received_ts"), ("failures",
                                                      "opened_ts")):
        for row in db.execute(
                f"SELECT id, hostname, {ts_col} AS t FROM {table}").fetchall():
            db.execute(
                f"UPDATE {table} SET print_session_id=? WHERE id=?",
                (session_at(db, row["hostname"], row["t"]), row["id"]))


def migrate_3_net_log(db):
    """Connectivity audit: offline/online transitions (flagged when they
    interrupt a print), DHCP address changes and mDNS re-discoveries,
    telemetry silences - the raw material for diagnosing how healthy the
    farm network is."""
    db.execute("""CREATE TABLE IF NOT EXISTS net_log (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        at_ts INTEGER NOT NULL,
        hostname TEXT NOT NULL,
        event TEXT NOT NULL,
        detail TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS net_log_host_time"
               " ON net_log(hostname, at_ts)")


def migrate_4_printer_mac(db):
    """printers.mac - learned from the telemetry syslog header; the stable
    identity anchor that survives DHCP address churn (the printers stopped
    answering mDNS, so re-discovery rides on the MAC instead)."""
    add_column(db, "printers", "mac TEXT")


def migrate_5_print_kind(db):
    """print_log.kind: 'prod' (counts everywhere), 'service' (started while
    a blocking failure was open - repair test prints), 'test' (test files).
    Logged but excluded from the history list and usage statistics."""
    add_column(db, "print_log", "kind TEXT NOT NULL DEFAULT 'prod'")
    db.execute("UPDATE print_log SET kind='test'"
               " WHERE kind='prod' AND lower(file) LIKE '%test%'")
    db.execute("""UPDATE print_log SET kind='service'
        WHERE kind='prod' AND EXISTS (
            SELECT 1 FROM failures f
            WHERE f.hostname = print_log.hostname AND f.blocking = 1
              AND f.opened_ts <= print_log.started_ts
              AND COALESCE(f.closed_ts, 1 << 62) >= print_log.started_ts)""")


def migrate_6_failure_comments(db):
    """Running commentary on a failure ("czekamy na heatbreak", "naprawa po
    weekendzie"), separate from the final repair_note. For the "Inna awaria"
    catch-all (SCREEN_NOTE_ERROR_ID) the newest comment is also served to the
    printer and shown on its yellow AWARIA screen, where the generic label
    says nothing useful by itself."""
    db.execute("""CREATE TABLE IF NOT EXISTS failure_comments (
        id INTEGER PRIMARY KEY,
        failure_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        created_ts INTEGER,
        text TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS failure_comments_failure"
               " ON failure_comments(failure_id)")


MIGRATIONS = [
    migrate_1_epoch_columns, migrate_2_sessions_material, migrate_3_net_log,
    migrate_4_printer_mac, migrate_5_print_kind, migrate_6_failure_comments
]


def migrate(db):
    row = db.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    version = int(row["value"]) if row else 0
    for number, step in enumerate(MIGRATIONS[version:], start=version + 1):
        step(db)
        db.execute(
            "INSERT OR REPLACE INTO meta(key, value)"
            " VALUES ('schema_version', ?)", (str(number), ))
        db.commit()


def to_epoch(text):
    try:
        return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp())
    except (TypeError, ValueError):
        return 0
