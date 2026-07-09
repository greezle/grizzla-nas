"""HTTP routing. Telemetry/SSE endpoints run outside db_lock; pages are
rendered under it but sent after releasing it."""
import json
import os
import queue
import re
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler

from awaria.config import STATIC_DIR, FARM_HOST_RE
from awaria.db import db_lock, open_db, now_str, now_pair, to_epoch_or_none
from awaria.services import bus
from awaria.services.catalog import render_catalog, save_def, reorder_defs
from awaria.services.failures import handle_event
from awaria.services.telemetry import (history_columns, samples_columns)
from awaria.web.pages import (page, render_home, render_printer,
                              render_failure, render_components,
                              render_history, render_stats, render_defs_list,
                              render_def_form, render_telemetry)


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

    def serve_stream(self):
        """SSE: pushes {"kind": ...} nudges; the browser re-fetches whatever
        changed. Runs on its own connection thread and never touches db_lock,
        so a thousand idle streams could not stall event ingestion. The 25 s
        keepalive comment doubles as dead-socket detection."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = bus.subscribe()
        try:
            self.wfile.write(b'retry: 3000\n\ndata: {"kind": "hello"}\n\n')
            self.wfile.flush()
            while True:
                try:
                    chunk = "data: %s\n\n" % json.dumps(q.get(timeout=25),
                                                        ensure_ascii=False)
                except queue.Empty:
                    chunk = ": ping\n\n"
                self.wfile.write(chunk.encode())
                self.wfile.flush()
        except OSError:
            pass  # client closed the tab / left the network
        finally:
            bus.unsubscribe(q)

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

            if path == "/awaria/api/stream":
                return self.serve_stream()

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
                now, now_ts = now_pair()
                db.execute(
                    "UPDATE printers SET last_seen=?, last_seen_ts=?,"
                    " last_ip=COALESCE(?, last_ip) WHERE hostname=?",
                    (now, now_ts, self.headers.get("X-Forwarded-For"),
                     printer))
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
                    now, now_ts = now_pair()
                    for comp_id in form.get("component", []):
                        db.execute(
                            "INSERT INTO maintenance(hostname, component_id,"
                            " done_at, done_ts, failure_id) VALUES (?,?,?,?,?)",
                            (f["hostname"], int(comp_id), now, now_ts, fid))
                    if action := field("action")[:120]:
                        db.execute(
                            "INSERT INTO maintenance(hostname, action,"
                            " done_at, done_ts, failure_id) VALUES (?,?,?,?,?)",
                            (f["hostname"], action, now, now_ts, fid))
                    if "close" in form:
                        db.execute(
                            "UPDATE failures SET closed_at=?, closed_ts=?,"
                            " closed_by='panel'"
                            " WHERE id=? AND closed_at IS NULL",
                            (now, now_ts, fid))
                    db.commit()
                bus.publish("failures", f["hostname"])
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
                            "INSERT INTO maintenance(hostname, component_id,"
                            " done_at, done_ts) VALUES (?,?,?,?)",
                            (host, int(field("component_id", "0")), done_at,
                             to_epoch_or_none(done_at)))
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
