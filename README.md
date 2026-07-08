# grizzla-nas

Server side of the GRIZZLA print farm — everything that runs on the
Raspberry Pi `gcode-nas` (`192.168.68.114`). The printer firmware lives in
its own repository:
[Prusa-Firmware-MK3.5-Bondtech-LGX](https://github.com/greezle/Prusa-Firmware-MK3.5-Bondtech-LGX).

## What runs here

- **`awaria_server.py`** — the *GRIZZLA panel serwisowy*: failure/repair
  database with instant notifications, farm map with live print progress,
  error-catalog wizard (synced to the printers), spare-parts & maintenance
  history per printer, telemetry collector (temperatures, printed files,
  versions — UDP port 8514), temperature history charts, usage statistics,
  printer auto-discovery (subnet ping + reverse mDNS). Python stdlib only.
- **`gcode-publish`** — g-code release publisher (v4): engineers edit the
  SMB `master/` share and save `UPDATES.xlsx` (or drop a `PUBLISH` flag);
  releases are versioned, immutable and differentially backed up (only
  removed/replaced content is preserved, hardlinked — additions cost nothing).
- **`static/`** — vendored frontend libraries (SortableJS, uPlot); the LAN
  needs no internet at runtime.
- **`deploy/`** — systemd units, timers and the nginx site, as installed.

Deployment, data locations and the printer↔server protocols are documented
in [DEPLOY.md](DEPLOY.md).
