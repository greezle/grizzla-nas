"""Awaria server for the print farm (gcode-nas).

Receives failure/repair events from the printers (M9206), keeps them in
SQLite and serves the maintenance dashboard. Python stdlib only - no
dependencies. Runs behind nginx (Basic auth + proxy on /awaria/).

Deployed as a zipapp (build-server.sh) to /usr/local/bin/awaria-server;
`python3 -m awaria` runs it from a source checkout.
"""
import threading

from http.server import ThreadingHTTPServer

from awaria import db
from awaria.config import LISTEN
from awaria.services import printers, telemetry
from awaria.web.routes import Handler


def main():
    db.init_db()
    telemetry.open_tdb().close(
    )  # make sure the samples table exists before readers hit it
    threading.Thread(target=printers.ping_worker, daemon=True).start()
    threading.Thread(target=telemetry.metrics_worker, daemon=True).start()
    threading.Thread(target=telemetry.telemetry_logger, daemon=True).start()
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
