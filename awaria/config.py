"""Deployment constants: paths, addresses, ports."""
import re


DB_PATH = "/var/lib/awaria/awaria.db"


# Telemetry is big, expendable and constantly written - it lives on the SSD
# (dot-dir keeps it out of sight in the SMB share), not on the Pi's SD card.
TELEMETRY_DB = "/srv/gcode/.telemetry/telemetry.db"


STATIC_DIR = "/var/lib/awaria/static"  # vendored JS (SortableJS), no internet needed


LISTEN = ("127.0.0.1", 8081)


METRICS_PORT = 8514  # firmware metrics stream (UDP syslog, see metric_handlers.cpp)


# off-site backup freshness: gcode-nas-backup (the 3B) writes this stamp
# after each successful nightly pull; the dashboard warns when it goes stale,
# so a quietly dead backup Pi cannot go unnoticed for months
OFFSITE_STAMP = "/var/lib/awaria/offsite_backup_stamp"


OFFSITE_MAX_AGE_S = 50 * 3600  # nightly cadence + generous slack


SUBNET_PREFIX = "192.168.68."


FARM_HOST_RE = re.compile(
    r"^[A-Za-z]\d{1,2}$")  # section+cell hostnames, e.g. E6


MANIFEST_PATH = "/srv/gcode/MANIFEST.txt"
