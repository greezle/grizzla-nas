"""Slicer metadata straight from the g-code library on the SSD.

Every file's footer carries the authoritative estimate and filament
('; estimated printing time (normal mode) = 1h 9m 4s',
'; filament_type = PLA', '; total filament used [g] = 21.58') and the
farm's start g-code selects the print sheet ('M9203 P1'). The scanner
keeps the gcode_meta table in sync (head + tail reads only - the files
run to 100 MB), and the sessions tracker classifies a print as cancelled
when it ran more than 2% shorter than the file says it should have.
"""
import os
import re
import time

from awaria.config import GCODE_MASTER
from awaria.db import db_lock, open_db, now_str


SCAN_EVERY_S = 15 * 60

SHEET_NAMES = {1: "Smooth", 2: "Texture", 3: "Cryo", 4: "Satin"}

EST_RE = re.compile(r"; estimated printing time \(normal mode\) = (.+)")
EST_PART_RE = re.compile(r"(\d+)([dhms])")
FIL_ID_RE = re.compile(r'; filament_settings_id = "(.+)"')
FILAMENT_RE = re.compile(r"; filament_type = (.+)")
FIL_G_RE = re.compile(r"; total filament used \[g\] = ([0-9.]+)")
SHEET_RE = re.compile(r"^M9203 P(\d)", re.M)

# the start g-code (with its M9203) sits BEHIND the embedded thumbnails,
# ~14 KB into a typical file - read generously, it is still head-only
HEAD_BYTES = 256 * 1024
TAIL_BYTES = 32768


def parse_est(text):
    """'1d 2h 3m 4s' -> seconds; None when nothing parses."""
    total = 0
    for n, unit in EST_PART_RE.findall(text):
        total += int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total or None


def parse_gcode(full_path):
    """(est_s, filament, fil_g, sheet) from head+tail of one file."""
    with open(full_path, "rb") as f:
        head = f.read(HEAD_BYTES).decode("utf-8", "replace")
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - TAIL_BYTES))
        tail = f.read().decode("utf-8", "replace")
    est_s = filament = fil_g = sheet = None
    if m := EST_RE.search(tail):
        est_s = parse_est(m.group(1))
    # profile name ("Hello3D 95A") beats the coarse type ("FLEX")
    if m := FIL_ID_RE.search(tail):
        filament = m.group(1).strip()[:24]
    elif m := FILAMENT_RE.search(tail):
        filament = m.group(1).strip()[:24]
    if m := FIL_G_RE.search(tail):
        fil_g = float(m.group(1))
    if m := SHEET_RE.search(head):
        sheet = SHEET_NAMES.get(int(m.group(1)))
    return est_s, filament, fil_g, sheet


def scan_library():
    """One pass: parse new/changed .gcode files, drop rows of deleted ones.
    File I/O happens OUTSIDE db_lock - a long lock once froze the metrics
    worker (2026-08-01); only the final short write transaction takes it."""
    on_disk = {}
    for dirpath, dirnames, filenames in os.walk(GCODE_MASTER):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if not name.lower().endswith(".gcode"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, GCODE_MASTER).replace(os.sep, "/")
            try:
                st = os.stat(full)
            except OSError:
                continue
            on_disk[rel] = (full, st.st_size, st.st_mtime_ns)

    with db_lock, open_db() as db:
        known = {
            r["path"]: (r["size"], r["mtime_ns"])
            for r in db.execute("SELECT path, size, mtime_ns FROM gcode_meta")
        }
    stale = [
        rel for rel, (full, size, mtime) in on_disk.items()
        if known.get(rel) != (size, mtime)
    ]

    parsed = []
    for rel in stale:
        full, size, mtime = on_disk[rel]
        try:
            est_s, filament, fil_g, sheet = parse_gcode(full)
        except OSError:
            continue
        parsed.append(
            (rel, size, mtime, est_s, filament, fil_g, sheet, now_str()))

    gone = [rel for rel in known if rel not in on_disk]
    if parsed or gone:
        with db_lock, open_db() as db:
            db.executemany(
                "INSERT OR REPLACE INTO gcode_meta"
                " (path, size, mtime_ns, est_s, filament, fil_g, sheet,"
                "  scanned_at) VALUES (?,?,?,?,?,?,?,?)", parsed)
            db.executemany("DELETE FROM gcode_meta WHERE path=?",
                           ((rel, ) for rel in gone))
            db.commit()
    return len(parsed), len(gone)


def meta_for(db, fname):
    """gcode_meta row for a print_log file: exact path first, then a
    unique-basename match (pre-11244 sessions logged bare names)."""
    fname = str(fname or "")
    if not fname:
        return None
    row = db.execute("SELECT * FROM gcode_meta WHERE path=?",
                     (fname, )).fetchone()
    if row:
        return row
    base = fname.rsplit("/", 1)[-1].lower()
    matches = [
        r for r in db.execute("SELECT * FROM gcode_meta")
        if r["path"].rsplit("/", 1)[-1].lower() == base
    ]
    return matches[0] if len(matches) == 1 else None


def scanner_worker():
    while True:
        try:
            scan_library()
        except Exception:  # noqa: BLE001 - keep scanning next round
            import traceback
            traceback.print_exc()
        time.sleep(SCAN_EVERY_S)
