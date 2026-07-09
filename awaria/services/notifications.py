"""Dashboard notifications (the bell) + SSE nudge on every insert."""
from awaria.db import now_pair
from awaria.services import bus


def notify(db, kind, text, hostname=None, link=None):
    now, now_ts = now_pair()
    db.execute(
        "INSERT INTO notifications(created_at, created_ts, kind, hostname,"
        " text, link) VALUES (?,?,?,?,?,?)",
        (now, now_ts, kind, hostname, text, link))
    # nudge open dashboards; the row is not committed yet, but every caller
    # commits right after, and the browsers' re-fetch happens later anyway
    bus.publish("notifications")
