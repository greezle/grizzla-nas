"""Printer failure/repair events (M9206 JSON) -> events + failures tables."""
import json
import urllib.parse

from awaria.db import db_lock, open_db, now_pair, session_at
from awaria.services import bus
from awaria.services.notifications import notify


ACTIONS_OPEN = ("AWARIA-BLOKADA", "AWARIA")


ACTIONS = ACTIONS_OPEN + ("NOTATKA", "NAPRAWIONO")


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
    # structured wizard answers - flattened into detail by today's firmware,
    # but accepted as a JSON array as soon as a future build sends them
    answers = data.get("answers")
    answers = (json.dumps(answers, ensure_ascii=False)[:500] if isinstance(
        answers, list) and answers else None)

    if not host:
        return 400, {"ok": False, "error": "missing host"}
    if action not in ACTIONS:
        return 400, {"ok": False, "error": "bad action"}

    now, now_ts = now_pair()
    with db_lock, open_db() as db:
        db.execute("INSERT OR IGNORE INTO printers(hostname) VALUES (?)",
                   (host, ))
        db.execute(
            "UPDATE printers SET last_seen=?, last_seen_ts=?,"
            " last_ip=COALESCE(?, last_ip) WHERE hostname=?",
            (now, now_ts, client_ip, host))
        if seq is not None:
            dup = db.execute("SELECT 1 FROM events WHERE hostname=? AND seq=?",
                             (host, seq)).fetchone()
            if dup:
                return 200, {"ok": True, "dup": True}

        # the print session this report belongs to (running now, or just over)
        session_id = session_at(db, host, now_ts)
        db.execute(
            "INSERT INTO events(hostname, received_at, received_ts,"
            " printer_time, action, category, label, detail, seq,"
            " print_session_id, answers) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (host, now, now_ts, ptime, action, category, label, detail, seq,
             session_id, answers))

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
                    " blocking, opened_at, opened_ts, print_session_id)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (host, category, label, detail, blocking, now, now_ts,
                     session_id)).lastrowid
            notify(db, "failure",
                   f"{host}: {'BLOKADA' if blocking else 'awaria'} — {label}",
                   host, f"/awaria/failure/{failure_id}")
        elif action == "NAPRAWIONO":
            open_row = db.execute(
                "SELECT id FROM failures WHERE hostname=? AND category=?"
                " AND closed_at IS NULL ORDER BY id DESC LIMIT 1",
                (host, category)).fetchone()
            db.execute(
                "UPDATE failures SET closed_at=?, closed_ts=?,"
                " closed_by='drukarka'"
                " WHERE hostname=? AND category=? AND closed_at IS NULL",
                (now, now_ts, host, category))
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
    bus.publish("failures", host)
    return 200, {"ok": True}
