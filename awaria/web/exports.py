"""Filtered failure exports: CSV for quick looks, XLSX for the farm's
Excel workflows. Both take the same URL query as the browser page, so the
export always matches what the filters show."""
import csv
import io
import time

from awaria.services.failures import failures_select

HEADER = [
    "ID", "Drukarka", "Kategoria", "Blokada", "Otwarta", "Naprawiona",
    "Czas [h]", "Zamknięta przez", "Podczas wydruku", "Szczegóły",
    "Notatka serwisowa", "Komentarze"
]


def _rows(db, query):
    now = int(time.time())
    for f in failures_select(db, query):
        opened = f["opened_ts"] or now
        hours = round(((f["closed_ts"] or now) - opened) / 3600, 1)
        yield [
            f["id"], f["hostname"], f["label"] or "",
            "TAK" if f["blocking"] else "NIE", f["opened_at"] or "",
            f["closed_at"] or "", hours, f["closed_by"] or "",
            f["print_file"] or "", f["detail"] or "", f["repair_note"] or "",
            f["comments_joined"] or ""
        ]


def export_failures_csv(db, query):
    """Semicolon separator + BOM + comma decimals: what Polish Excel expects
    when double-clicking a .csv."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(HEADER)
    for row in _rows(db, query):
        row[6] = str(row[6]).replace(".", ",")
        writer.writerow(row)
    return "﻿" + out.getvalue()


def export_failures_xlsx(db, query):
    """Returns the workbook bytes, or None when openpyxl is unavailable
    (it is on the NAS - the g-code publisher already depends on it)."""
    try:
        import openpyxl
        from openpyxl.utils import get_column_letter
    except ImportError:
        return None
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Awarie"
    ws.append(HEADER)
    for cell in ws[1]:
        cell.font = openpyxl.styles.Font(bold=True)
    for row in _rows(db, query):
        ws.append(row)
    for i, width in enumerate([6, 10, 24, 9, 17, 17, 8, 13, 28, 40, 30, 40],
                              start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
