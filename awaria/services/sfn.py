"""Fleet SFN uniformity + the QR sticker library.

QR print-order stickers encode `M23 <SFN path>` (FAT 8.3 short paths), and
SFNs are assigned per drive by whoever writes the file - so the whole farm
must agree on the SFN of every g-code or the stickers stop matching reality.
After every applied g-code release each printer scans its drive and POSTs
the resulting SFN table here (firmware: common/sfn_report.hpp, `M9202 U`).

Model (one reference, everyone else checked against it):
- The FIRST printer to report a given release becomes the reference for it;
  its table replaces `sfn_reference` and the QR PNG library under QR_DIR is
  regenerated for every added/changed mapping.
- An SFN that changed although the file was NOT touched by the release (its
  manifest seq is older) is UNEXPECTED: already-printed stickers for it are
  now wrong. That raises a separate loud warning, every time.
- Every other printer reporting the same release is diffed against the
  reference. ANY difference (missing file, extra file, different SFN) keeps
  the printer's report row on `mismatch` and raises a warning notification -
  again on every report, so a diverged drive cannot fade from view.
- A report for an older release than the reference is `stale` (the printer
  missed the update); also warned.

Report format (plain text, UTF-8):
    release <seq> <date>
    <sfn path>\t<lfn path relative to /usb>
    ...
    end <count>
"""
import os

from awaria import config
from awaria.db import db_lock, now_pair, open_db
from awaria.services.notifications import notify

# keep notification texts readable: name at most this many files inline
_LIST_CAP = 5


def _fmt_list(items):
    shown = ", ".join(sorted(items)[:_LIST_CAP])
    more = len(items) - _LIST_CAP
    return shown + (f" (+{more} innych)" if more > 0 else "")


def parse_report(text):
    """-> (seq, date, {lfn: sfn}). Raises ValueError on malformed input,
    including a missing/mismatched `end <count>` integrity trailer."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("release "):
        raise ValueError("missing release header")
    head = lines[0].split()
    if len(head) < 3:
        raise ValueError("bad release header")
    try:
        seq = int(head[1])
    except ValueError:
        raise ValueError("bad release seq")
    date = head[2]

    table = {}
    end_count = None
    for line in lines[1:]:
        if not line:
            continue
        if line.startswith("end "):
            try:
                end_count = int(line[4:].strip())
            except ValueError:
                raise ValueError("bad end trailer")
            break
        sfn, sep, lfn = line.partition("\t")
        if not sep or not sfn.startswith("/usb/") or not lfn:
            raise ValueError(f"bad entry line: {line[:80]!r}")
        table[lfn] = sfn
    if end_count is None:
        raise ValueError("missing end trailer (truncated report)")
    if end_count != len(table):
        raise ValueError(f"count mismatch: trailer {end_count}, entries {len(table)}")
    return seq, date, table


def manifest_seqs():
    """{lfn path: last-changed release seq} from the published MANIFEST;
    {} when the manifest is unreadable (then no change can be proven
    expected, which errs on the loud side)."""
    seqs = {}
    try:
        with open(config.MANIFEST_PATH, encoding="utf-8") as f:
            for line in f:
                if not line.startswith("file "):
                    continue
                parts = line.rstrip("\n").split(" ", 3)
                if len(parts) == 4:
                    try:
                        seqs[parts[3]] = int(parts[1])
                    except ValueError:
                        pass
    except OSError:
        pass
    return seqs


def _qr_png_path(lfn):
    stem = lfn.rsplit(".", 1)[0] if "." in lfn.rsplit("/", 1)[-1] else lfn
    return os.path.join(config.QR_DIR, stem + ".png")


def _regen_qr(pairs):
    """(re)renders QR PNGs for [(lfn, sfn)]; returns (done, failed-reason)."""
    try:
        import qrcode
    except ImportError:
        return 0, "brak biblioteki qrcode na serwerze (pip3 install qrcode pillow)"
    done = 0
    for lfn, sfn in pairs:
        out = _qr_png_path(lfn)
        try:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            qrcode.make("M23 " + sfn).save(out)
            done += 1
        except OSError:
            return done, f"zapis {out} nie powiódł się"
    return done, None


def _remove_qr(lfns):
    for lfn in lfns:
        try:
            os.remove(_qr_png_path(lfn))
        except OSError:
            pass


def _load_reference(db):
    return {
        row["lfn"]: row["sfn"]
        for row in db.execute("SELECT lfn, sfn FROM sfn_reference")
    }


def _meta_get(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key, )).fetchone()
    return row["value"] if row else default

def _meta_set(db, key, value):
    db.execute(
        "INSERT INTO meta(key, value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def _record_report(db, hostname, seq, date, count, status, diff):
    now, now_ts = now_pair()
    db.execute(
        "INSERT INTO sfn_reports(hostname, release_seq, release_date,"
        " reported_at, reported_ts, file_count, status, diff)"
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(hostname) DO UPDATE SET release_seq=excluded.release_seq,"
        " release_date=excluded.release_date, reported_at=excluded.reported_at,"
        " reported_ts=excluded.reported_ts, file_count=excluded.file_count,"
        " status=excluded.status, diff=excluded.diff", (
            hostname, seq, date, now, now_ts, count, status, diff))


def _become_reference(db, hostname, seq, date, table):
    """First report of a new release: adopt it, warn about the unexpected,
    regenerate the QR library."""
    old = _load_reference(db)
    mseqs = manifest_seqs()

    added = {lfn for lfn in table if lfn not in old}
    removed = {lfn for lfn in old if lfn not in table}
    changed = {lfn for lfn in table if lfn in old and old[lfn] != table[lfn]}
    # a change is only "expected" when this release actually touched the file
    unexpected = {lfn for lfn in changed if mseqs.get(lfn, seq) != seq}

    if unexpected:
        notify(
            db, "sfn", f"SFN: UWAGA - release {seq} zmienił SFN {len(unexpected)}"
            f" plików, których wydanie NIE dotykało: {_fmt_list(unexpected)}."
            " Wydrukowane naklejki QR tych plików są NIEWAŻNE!", hostname)

    regen = [(lfn, table[lfn]) for lfn in sorted(added | changed)]
    done, fail = _regen_qr(regen)
    _remove_qr(removed)

    db.execute("DELETE FROM sfn_reference")
    now, _ = now_pair()
    db.executemany(
        "INSERT INTO sfn_reference(lfn, sfn, release_seq, updated_at)"
        " VALUES (?,?,?,?)",
        [(lfn, sfn, seq, now) for lfn, sfn in table.items()])
    _meta_set(db, "sfn_ref_release", seq)
    _meta_set(db, "sfn_ref_host", hostname)

    if added or changed or removed:
        text = (f"SFN: referencja release {seq} od {hostname}: {len(added)} nowych,"
                f" {len(changed)} zmienionych, {len(removed)} usuniętych;"
                f" {done}/{len(regen)} kodów QR przegenerowanych")
        if fail:
            text += f". BŁĄD generowania QR: {fail}"
        notify(db, "sfn", text, hostname)
    _record_report(db, hostname, seq, date, len(table), "reference", "")


def _check_against_reference(db, hostname, seq, date, table):
    ref = _load_reference(db)
    missing = {lfn for lfn in ref if lfn not in table}
    extra = {lfn for lfn in table if lfn not in ref}
    wrong = {lfn for lfn in table if lfn in ref and table[lfn] != ref[lfn]}

    if not (missing or extra or wrong):
        _record_report(db, hostname, seq, date, len(table), "match", "")
        return

    parts = []
    if wrong:
        parts.append(f"{len(wrong)} inne SFN: {_fmt_list(wrong)}")
    if missing:
        parts.append(f"{len(missing)} brakuje: {_fmt_list(missing)}")
    if extra:
        parts.append(f"{len(extra)} nadmiarowe: {_fmt_list(extra)}")
    diff = "; ".join(parts)
    # warn on EVERY divergent report - a diverged drive must not fade from view
    notify(
        db, "sfn", f"SFN: {hostname} ODBIEGA od referencji (release {seq}):"
        f" {diff}. Kody QR nie zadziałają na tej drukarce - wgraj bibliotekę"
        " g-code od zera.", hostname)
    _record_report(db, hostname, seq, date, len(table), "mismatch", diff)


def handle_sfn_report(hostname, body):
    """POST /awaria/api/sfn-report handler -> (http code, json dict)."""
    hostname = (hostname or "").strip()
    if not hostname or not config.FARM_HOST_RE.match(hostname):
        return 400, {"ok": False, "error": "bad or missing X-Printer"}
    try:
        seq, date, table = parse_report(body.decode("utf-8", "replace"))
    except ValueError as e:
        return 400, {"ok": False, "error": str(e)}

    with db_lock, open_db() as db:
        ref_release = int(_meta_get(db, "sfn_ref_release", 0) or 0)
        if seq > ref_release or ref_release == 0:
            _become_reference(db, hostname, seq, date, table)
        elif seq == ref_release:
            _check_against_reference(db, hostname, seq, date, table)
        else:
            notify(
                db, "sfn", f"SFN: {hostname} raportuje starszy release {seq}"
                f" (referencja: {ref_release}) - drukarka nie ma aktualnej"
                " biblioteki g-code", hostname)
            _record_report(db, hostname, seq, date, len(table), "stale", "")
        db.commit()
    return 200, {"ok": True}


def sfn_status():
    """GET /awaria/api/sfn-status -> dashboard/automation JSON."""
    with db_lock, open_db() as db:
        reports = [dict(row) for row in db.execute(
            "SELECT hostname, release_seq, release_date, reported_at,"
            " file_count, status, diff FROM sfn_reports ORDER BY hostname")]
        return {
            "reference_release": int(_meta_get(db, "sfn_ref_release", 0) or 0),
            "reference_host": _meta_get(db, "sfn_ref_host"),
            "printers": reports,
        }
