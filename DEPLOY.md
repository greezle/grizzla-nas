# gcode-nas server — deployment notes

Everything that runs on the Raspberry Pi 4B (`gcode-nas`, wired `192.168.68.114`,
ssh `admin`), so the whole NAS can be rebuilt from this folder.

## Components

| File | Installs to | Purpose |
|---|---|---|
| `awaria_server.py` | `/usr/local/bin/awaria-server` | Failure dashboard + error catalog + telemetry collector + notifications (stdlib Python, no deps) |
| `gcode-publish` | `/usr/local/bin/gcode-publish` | G-code release publisher (v4, differential backups) |
| `static/*` | `/var/lib/awaria/static/` | Vendored JS (SortableJS drag lists, uPlot charts) — no internet at runtime |
| `deploy/awaria.service` | `/etc/systemd/system/` | Dashboard service (`Restart=always`, `MemoryMax=200M`) |
| `deploy/awaria-backup`, `.service`, `.timer` | `/usr/local/bin/`, `/etc/systemd/system/` | Daily 03:30 backup of `awaria.db` to `/srv/gcode/awaria_backups/` (keeps 30) |
| `deploy/gcode-publish.service`, `.path` | `/etc/systemd/system/` | Publish trigger: `PUBLISH` flag file or `UPDATES.xlsx` save in the SMB share |
| `deploy/nginx-gcode.conf` | `/etc/nginx/sites-available/gcode` | `/gcode/` file serving + `/awaria/` proxy, Basic auth `admin:prusa` (`/etc/nginx/.htpasswd`) |

## Data locations

- `/var/lib/awaria/awaria.db` — failures, repairs, printers, flags, components,
  maintenance, print log, notifications, error catalog. **Backed up daily.**
- `/srv/gcode/.telemetry/telemetry.db` — temperature samples (1/2 s per printer,
  4-day retention). On the SSD on purpose (write wear + size); expendable, not backed up.
- `/srv/gcode` — 512 GB SSD (`/dev/sda1` label `gcode-ssd`, fstab by UUID, `nofail`):
  `master/` (SMB share, desired USB contents), `store/<seq>/` (immutable release copies),
  `backups/` (differential release backups), `MANIFEST.txt`, `current.txt`.
- The retired 64 GB pendrive (label `gcode`) holds a frozen 2026-07-06 copy of the library.

## Fresh install

```sh
sudo apt install nginx samba avahi-utils   # avahi = reverse-mDNS printer discovery
sudo install -m 755 awaria_server.py /usr/local/bin/awaria-server
sudo install -m 755 gcode-publish /usr/local/bin/gcode-publish
sudo install -m 755 deploy/awaria-backup /usr/local/bin/awaria-backup
sudo mkdir -p /var/lib/awaria/static && sudo chown admin /var/lib/awaria
sudo install -m 644 static/* /var/lib/awaria/static/
sudo cp deploy/awaria.service deploy/awaria-backup.service deploy/awaria-backup.timer \
        deploy/gcode-publish.service deploy/gcode-publish.path /etc/systemd/system/
sudo cp deploy/nginx-gcode.conf /etc/nginx/sites-available/gcode
sudo ln -sf /etc/nginx/sites-available/gcode /etc/nginx/sites-enabled/gcode
sudo htpasswd -c /etc/nginx/.htpasswd admin        # password: prusa
sudo systemctl daemon-reload
sudo systemctl enable --now awaria awaria-backup.timer gcode-publish.path nginx smbd
```

## Update just the dashboard

```sh
scp server/awaria_server.py admin@192.168.68.114:/tmp/
ssh admin@192.168.68.114 'sudo install -m 755 /tmp/awaria_server.py /usr/local/bin/awaria-server && sudo systemctl restart awaria'
```

## How the printers talk to it

- **G-code sync** (fw M9204/M9205): HTTP GET `/gcode/current.txt`, `MANIFEST.txt`,
  `store/...` with Basic auth. Server address per printer: Settings → Network → G-code Server.
- **Failure events**: POST `/awaria/api/event` (offline-queued on the printer's USB,
  deduped by per-printer `seq`).
- **Error catalog**: GET `/awaria/api/catalog` at boot + after every g-code sync
  (fw ≥ 11237 identifies itself via `X-Printer` header → IP discovery).
- **Telemetry**: UDP metrics stream to port `8514` (fw ≥ 11240 by default:
  temps, printed file, progress, versions; identified by source IP).
- **Discovery**: the server ping-sweeps the /24 and reverse-mDNS-resolves responders
  every ~4 min; farm hostnames are section+cell (`E6`).

Open TODOs live in the AI session memory: off-device backup of the NAS,
phone push notifications (ntfy/Telegram), Excel history import.
