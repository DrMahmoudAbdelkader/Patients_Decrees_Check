#!/usr/bin/env python3
"""
extract_clinic_list.py

Cleans up the "Clinic List Detail - by Status" export from the hospital
portal (the report that looks like a printed page dumped into Excel:
dozens of empty columns, repeated titles/dates on every page, and
labels that don't line up with their values).

WHAT IT DOES
------------
The source file is really a stack of small "pages" glued together.
Each page repeats the same print header (title, facility name, date,
time, page number), then a "Clinic:" label with the clinic name, then
a set of column headers, then the actual patient rows, then a couple
of blank rows before the next page starts.

This script:
  1. Walks down the sheet once.
  2. Whenever it sees the "Clinic" label, remembers the clinic name
     that follows it -- every row after that (until the next "Clinic"
     label) belongs to that clinic.
  3. Whenever it sees a row whose "Serial No." cell is an actual
     number (not text, not blank), it treats that row as one patient
     record and pulls the 7 data fields out of their fixed columns.
  4. Writes everything into one flat, clean table -- one row per
     patient/appointment, no blank rows, no repeated headers, no
     merged cells.

WHY FIXED COLUMNS WORK HERE
----------------------------
Even though the layout looks chaotic, it's a *printed template*
exported to Excel, so the same fields always land in the same
columns on every page (verified across all 7 pages of the sample
file: column D is always Serial No., H is always the ID Number,
etc.). If the site changes its report template later, update the
COLUMNS dict below -- everything else in the script will keep working.

HOW TO USE
----------
    python3 extract_clinic_list.py input_file.xlsx [more_files.xlsx ...]

For each input file, this writes a companion file named
"<original_name>_clean.xlsx" next to it, with one row per patient
record and normal, filterable columns.

If a future export from the site doesn't produce the fields you
expect, run the script with --debug to print out what it found in
each column so you can see what changed.
"""

import sys
import argparse
from pathlib import Path

import openpyxl
from openpyxl.utils import get_column_letter


# ---------------------------------------------------------------------------
# CONFIG -- adjust here if the site's export layout ever changes.
#
# Each entry is: output column name -> the column LETTER where that field's
# value starts in the raw sheet (the value sits in a merged cell that starts
# at this column). These were mapped by hand from the sample export -- see
# the note on "Patient File No." below.
# ---------------------------------------------------------------------------
COLUMNS = {
    "Serial No.":          "D",
    "National ID Number":  "H",
    # This field has no header of its own in the source report -- the
    # "Patient Name" header sits a few columns further right and its data
    # cell is always empty (name isn't populated in this export). The
    # number that actually lands here (e.g. 2497, 6270 ...) behaves like an
    # internal patient/file number. Relabel this if you know it as
    # something else in your system (e.g. "MRN").
    "Patient File No.":    "M",
    "Appointment Number":  "U",
    "Appointment Date":    "Y",
    "User":                "AC",
    "Old Medical No.":     "AI",
    # Present as a header in the template but never populated in the
    # sample file. Left here so it gets picked up automatically if a
    # future export starts filling it in.
    "Status":              "AF",
}

CLINIC_LABEL_COL = "E"      # cell that literally contains the text "Clinic"
CLINIC_VALUE_COL = "J"      # the clinic name sits a few columns to the right


def col_to_idx(letter: str) -> int:
    return openpyxl.utils.column_index_from_string(letter)


def extract_sheet(ws, debug=False):
    """Return a list of dict rows extracted from one worksheet."""
    clinic_label_idx = col_to_idx(CLINIC_LABEL_COL)
    clinic_value_idx = col_to_idx(CLINIC_VALUE_COL)
    field_idx = {name: col_to_idx(letter) for name, letter in COLUMNS.items()}
    serial_idx = field_idx["Serial No."]

    records = []
    current_clinic = None

    for r in range(1, ws.max_row + 1):
        label_cell = ws.cell(row=r, column=clinic_label_idx).value
        if isinstance(label_cell, str) and label_cell.strip() == "Clinic":
            clinic_val = ws.cell(row=r, column=clinic_value_idx).value
            if clinic_val not in (None, ""):
                current_clinic = str(clinic_val).strip()
                if debug:
                    print(f"row {r}: clinic -> {current_clinic}")
            continue

        serial_val = ws.cell(row=r, column=serial_idx).value
        is_data_row = False
        if isinstance(serial_val, (int, float)):
            is_data_row = True
        elif isinstance(serial_val, str) and serial_val.strip().isdigit():
            is_data_row = True
        if not is_data_row:
            continue

        row_data = {"Clinic": current_clinic}
        for name, idx in field_idx.items():
            val = ws.cell(row=r, column=idx).value
            row_data[name] = val
        records.append(row_data)
        if debug:
            print(f"row {r}: data -> {row_data}")

    return records


def extract_records_from_workbook_bytes(xlsx_bytes: bytes, debug=False) -> list:
    """
    Same extraction as clean_workbook(), but operates on in-memory bytes
    (the raw report content returned by queue_extractor.fetch_report())
    instead of a file on disk. Used by the daily Supabase sync pipeline.
    Returns a list of dict rows with keys: Clinic, Serial No.,
    National ID Number, Patient File No., Appointment Number,
    Appointment Date, User, Old Medical No., Status.
    """
    import io
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    all_records = []
    for ws in wb.worksheets:
        all_records.extend(extract_sheet(ws, debug=debug))
    return all_records


def clean_workbook(src_path: Path, debug=False) -> Path:
    wb = openpyxl.load_workbook(src_path, data_only=True)

    all_records = []
    for ws in wb.worksheets:
        recs = extract_sheet(ws, debug=debug)
        if len(wb.worksheets) > 1:
            for rec in recs:
                rec["Source Sheet"] = ws.title
        all_records.extend(recs)

    if not all_records:
        print(f"WARNING: no patient rows found in {src_path.name}. "
              f"The layout may not match COLUMNS in the script -- try --debug.")

    # Build column order
    columns = ["Clinic"] + list(COLUMNS.keys())
    if len(wb.worksheets) > 1:
        columns.append("Source Sheet")

    out_wb = openpyxl.Workbook()
    out_ws = out_wb.active
    out_ws.title = "Clean Data"

    # Header row
    for c, name in enumerate(columns, start=1):
        cell = out_ws.cell(row=1, column=c, value=name)
        cell.font = openpyxl.styles.Font(bold=True)

    # Data rows
    for r, rec in enumerate(all_records, start=2):
        for c, name in enumerate(columns, start=1):
            out_ws.cell(row=r, column=c, value=rec.get(name))

    # Reasonable column widths
    for c, name in enumerate(columns, start=1):
        out_ws.column_dimensions[get_column_letter(c)].width = max(14, len(name) + 2)

    out_ws.freeze_panes = "A2"

    out_path = src_path.with_name(src_path.stem + "_clean.xlsx")
    out_wb.save(out_path)
    print(f"{src_path.name}: extracted {len(all_records)} records -> {out_path.name}")
    return out_path


def clean_pasted_path(raw: str) -> str:
    """Strip quotes/whitespace people commonly paste around a path."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1]
    return raw.strip()


def prompt_for_files() -> list:
    files = []
    print("Paste the path to the Excel file (or drag it into this window), then press Enter.")
    print("You can paste multiple files one at a time. Press Enter on an empty line when done.")
    while True:
        raw = input("File path: " if not files else "File path (or Enter to run): ")
        raw = clean_pasted_path(raw)
        if not raw:
            break
        path = Path(raw)
        if not path.exists():
            print(f"  Not found: {raw} -- try again.")
            continue
        files.append(raw)
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", help="Input .xlsx file(s) from the site")
    parser.add_argument("--debug", action="store_true",
                         help="Print every row as it's parsed (for troubleshooting)")
    args = parser.parse_args()

    file_list = args.files
    if not file_list:
        file_list = prompt_for_files()
        if not file_list:
            print("No file given -- nothing to do.")
            return

    for f in file_list:
        path = Path(f)
        if not path.exists():
            print(f"SKIP: {f} not found")
            continue
        clean_workbook(path, debug=args.debug)

    input("\nDone. Press Enter to close...")


if __name__ == "__main__":
    main()
