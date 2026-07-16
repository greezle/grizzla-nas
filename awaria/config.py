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


# "Inna awaria" - the catch-all error whose failure comments are mirrored to
# the printer's yellow AWARIA screen (the label alone explains nothing, so
# maintenance annotates: "czekamy na części", "nie ruszać do piątku", ...)
SCREEN_NOTE_ERROR_ID = 9


# byte cap for the note served to printers: the firmware buffer is 160 B and
# the yellow screen fits a few lines; cut happens at a UTF-8 boundary
SCREEN_NOTE_MAX_BYTES = 150
