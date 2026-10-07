"""Steel-sheet Z offsets reported by the printers (fw >= 11268).

Every printer sends an "OFFSETY" event through the regular event queue:
    detail = {"a": <active sheet idx>, "p": "<print file or ''>",
              "s": [["Smooth1", -1.234], ["Smooth2", null], ...]}
at boot, when the operator confirms a repair on the printer, at the end of
every print and (debounced) whenever a sheet offset is edited. The queue is
persistent on the printer, so outages and reconnects deliver the snapshots
late but complete.

Server side: each snapshot is stored, diffed against the previous one per
sheet, and every offset change becomes
  * a row in sheet_offset_changes (with the print it was made during),
  * a "Zmiana offsetu" line in the printer's event trail,
  * an attachment to the repair whose window contains it: from the moment
    the failure was reported until the end of the FIRST print after it was
    closed (capped at 7 days when no print follows). That is what makes
    "Live Z adjusted an hour after clicking repaired" land on that repair.
"""
import json
import urllib.parse

from awaria.db import db_lock, open_db, now_pair, session_at
from awaria.services import bus

ACTION = "OFFSETY"
CHANGE_ACTION = "OFFSET"
CHANGE_LABEL = "Zmiana offsetu"
REASON_TEXT = {
    "boot": "start drukarki",
    "repair": "zakończenie naprawy",
    "print_end": "koniec wydruku",
    "print_abort": "przerwany wydruk",
    "change": "zmiana na drukarce",
}
POST_REPAIR_MAX_S = 7 * 24 * 3600
FIRST_PRINT_GRACE_S = 90
EPS = 0.0005


def parse_snapshot(detail):
    """-> (active_idx, print_file, [(name, z|None), ...]) or None."""
    try:
        d = json.loads(detail)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("s"), list):
        return None
    sheets = []
    for item in d["s"][:8]:
        if not isinstance(item, list) or len(item) != 2:
            return None
        name, z = item
        name = str(name)[:16]
        if z is not None:
            try:
                z = round(float(z), 3)
            except (TypeError, ValueError):
                z = None
        sheets.append((name, z))
    try:
        active = int(d.get("a"))
    except (TypeError, ValueError):
        active = None
    print_file = str(d.get("p") or "")[:120] or None
    return active, print_file, sheets


def fmt_z(z):
    return "niekal." if z is None else f"{z:+.3f}"


def _same(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < EPS


def attribute_failure(db, host, ts, reason=None, print_file=None):
    """The repair a change made at `ts` belongs to, or None.

    Candidates are this printer's failures reported before `ts`, newest
    first. An open failure takes it. A closed one takes it until the first
    print started after the closing has ended: an end-of-print snapshot of
    that very print always counts (matched by file name - it arrives a
    little after telemetry closed the session), anything else only within a
    short grace after that print ended, or for 7 days when no print has
    followed yet."""
    rows = db.execute(
        "SELECT id, opened_ts, closed_ts FROM failures"
        " WHERE hostname=? AND opened_ts IS NOT NULL AND opened_ts <= ?"
        " ORDER BY opened_ts DESC LIMIT 20", (host, ts)).fetchall()
    for f in rows:
        if f["closed_ts"] is None:
            return f["id"]
        if ts - f["closed_ts"] > POST_REPAIR_MAX_S:
            continue
        first = db.execute(
            "SELECT file, ended_ts FROM print_log WHERE hostname=?"
            " AND started_ts >= ? AND ended_ts IS NOT NULL"
            " ORDER BY started_ts LIMIT 1", (host, f["closed_ts"])).fetchone()
        if first is None:
            return f["id"]
        if (reason or "").startswith("print_") and print_file                 and (first["file"] or "").endswith(print_file):
            return f["id"]
        if ts <= first["ended_ts"] + FIRST_PRINT_GRACE_S:
            return f["id"]
    return None


def handle_offsets_event(data, client_ip=None):
    """Store one snapshot, log the differences. Idempotent: a retransmitted
    snapshot equals the stored one and produces no change rows."""
    host = str(data.get("host") or "").strip()[:32]
    reason = str(data.get("label") or "")[:16]
    snap = parse_snapshot(data.get("detail"))
    if not host:
        return 400, {"ok": False, "error": "missing host"}
    if snap is None:
        return 400, {"ok": False, "error": "bad snapshot"}
    active, print_file, sheets = snap
    now, now_ts = now_pair()
    changed_failures = set()
    with db_lock, open_db() as db:
        db.execute("INSERT OR IGNORE INTO printers(hostname) VALUES (?)",
                   (host, ))
        db.execute(
            "UPDATE printers SET last_seen=?, last_seen_ts=?,"
            " last_ip=COALESCE(?, last_ip) WHERE hostname=?",
            (now, now_ts, client_ip, host))
        prev = db.execute(
            "SELECT sheets, active_idx FROM sheet_offsets WHERE hostname=?"
            " ORDER BY id DESC LIMIT 1", (host, )).fetchone()
        prev_sheets = json.loads(prev["sheets"]) if prev else []
        sheets_json = json.dumps(sheets, ensure_ascii=False)
        if prev and prev["sheets"] == sheets_json \
                and prev["active_idx"] == active and reason == "change":
            # knob turned and turned back, or a retransmission: nothing new
            return 200, {"ok": True, "dup": True}
        db.execute(
            "INSERT INTO sheet_offsets(hostname, at, ts, reason, print_file,"
            " active_idx, sheets) VALUES (?,?,?,?,?,?,?)",
            (host, now, now_ts, reason, print_file, active, sheets_json))
        session_id = session_at(db, host, now_ts)
        for idx, (name, z) in enumerate(sheets):
            old = prev_sheets[idx] if idx < len(prev_sheets) else None
            old_z = old[1] if old else None
            if not prev or _same(old_z, z):
                continue
            failure_id = attribute_failure(db, host, now_ts, reason, print_file)
            db.execute(
                "INSERT INTO sheet_offset_changes(hostname, at, ts, sheet_idx,"
                " sheet_name, old_z, new_z, reason, print_file, failure_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (host, now, now_ts, idx, name, old_z, z, reason, print_file,
                 failure_id))
            detail = f"{name}: {fmt_z(old_z)} → {fmt_z(z)}"
            if print_file:
                detail += f", wydruk: {print_file}"
            if reason in REASON_TEXT:
                detail += f" ({REASON_TEXT[reason]})"
            db.execute(
                "INSERT INTO events(hostname, received_at, received_ts,"
                " printer_time, action, category, label, detail, seq,"
                " print_session_id, answers) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (host, now, now_ts, None, CHANGE_ACTION, 0, CHANGE_LABEL,
                 detail, None, session_id, None))
            if failure_id:
                changed_failures.add(failure_id)
        db.commit()
    bus.publish("printers", host)
    if changed_failures:
        bus.publish("failures", host)
    return 200, {"ok": True}


def latest_offsets(db, host):
    """-> (snapshot row or None, [(idx, name, z, last_change_row|None), ...])."""
    row = db.execute(
        "SELECT * FROM sheet_offsets WHERE hostname=? ORDER BY id DESC LIMIT 1",
        (host, )).fetchone()
    if not row:
        return None, []
    last_changes = {}
    for c in db.execute(
            "SELECT * FROM sheet_offset_changes WHERE hostname=?"
            " ORDER BY id DESC LIMIT 200", (host, )):
        last_changes.setdefault(c["sheet_idx"], c)
    table = [(idx, name, z, last_changes.get(idx))
             for idx, (name, z) in enumerate(json.loads(row["sheets"]))]
    return row, table


def changes_for_failure(db, fid):
    return db.execute(
        "SELECT * FROM sheet_offset_changes WHERE failure_id=? ORDER BY id",
        (fid, )).fetchall()


def change_line(c):
    text = (f"{c['at'][:16]} {c['sheet_name']}: "
            f"{fmt_z(c['old_z'])} → {fmt_z(c['new_z'])}")
    if c["print_file"]:
        text += f" (wydruk: {c['print_file']})"
    return text


def changes_text(db, fid):
    return "; ".join(change_line(c) for c in changes_for_failure(db, fid))


def printer_link(host):
    return f"/awaria/printer/{urllib.parse.quote(host)}"
