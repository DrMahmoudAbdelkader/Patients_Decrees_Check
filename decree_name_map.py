"""
decree_name_map.py

Loads your two hand-built VLOOKUP-style mapping sheets and exposes a
lookup function for each:

  1. DECREE_NAME_MAP_XLSX  — two columns: raw/original Decree_Description
     text (as scraped from SMC) -> the unified Decree_Unique_Name you use
     everywhere else (cutoff map, Is_Current_Regimen matching, etc).
  2. ITEM_NAME_MAP_XLSX    — two columns: raw/original dispensed Item_Name
     text -> the unified Unique_Items_Names value.

Both behave exactly like your Excel VLOOKUP: exact match on the trimmed
raw text. If a raw value isn't in the sheet, that's an #N/A — and per
your instruction, rows that come back #N/A are EXCLUDED from the pipeline
entirely (these are ~99% non-medication services you don't want to track
right now: scans, interventional radiology, labs, etc. for decrees; the
equivalent service line items for dispensed items). normalize_decree_name()
and normalize_item_name() therefore return None (not the raw text) when
there's no match — None is the "exclude this row" signal daily_sync.py
checks for.

WHERE THE FILES LIVE
---------------------
By default this looks for two Excel files sitting next to this script:
    pipeline/decree_unique_name_map.xlsx
    pipeline/item_unique_name_map.xlsx
Override with the DECREE_NAME_MAP_XLSX / ITEM_NAME_MAP_XLSX env vars if
you'd rather keep them elsewhere (e.g. downloaded fresh each run instead
of committed to the repo).

EXPECTED SHEET FORMAT
----------------------
First sheet, first row = headers, exactly two data columns. Column
matching is done by HEADER NAME (not position), same convention as the
rest of this pipeline, so column order doesn't matter:
  - the "original" column: any header containing "original", "raw",
    "description", or "name" that ISN'T also a "unique" header
  - the "unique" column: any header containing "unique" or "unified"
If no header matches those hints, it falls back to column A = original,
column B = unique. Empty-file templates with the recommended headers
(Original_Description / Unique_Name and Original_Item_Name /
Unique_Item_Name) ship alongside this script — paste your existing two
mapping sheets' contents into them (or just point the env vars at your
own files).

If a map file is missing, this logs a warning and treats it as an empty
map — i.e. EVERYTHING gets excluded (#N/A for every row) until the file
is in place. That's intentional: better to exclude everything and be
obviously wrong than to silently let unmapped raw text pass through.
"""

import os
import logging

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

DECREE_NAME_MAP_XLSX = os.environ.get(
    "DECREE_NAME_MAP_XLSX",
    os.path.join(_THIS_DIR, "decree_unique_name_map.xlsx"),
)
ITEM_NAME_MAP_XLSX = os.environ.get(
    "ITEM_NAME_MAP_XLSX",
    os.path.join(_THIS_DIR, "item_unique_name_map.xlsx"),
)

_ORIGINAL_HINTS = ("original", "raw", "description", "name")
_UNIQUE_HINTS = ("unique", "unified")


def _pick_columns(header_row):
    """Returns (original_idx, unique_idx) by header text, falling back to (0, 1)."""
    headers = [str(v).strip().lower() if v is not None else "" for v in header_row]
    unique_idx = next((i for i, h in enumerate(headers)
                        if any(hint in h for hint in _UNIQUE_HINTS)), None)
    original_idx = next((i for i, h in enumerate(headers)
                          if any(hint in h for hint in _ORIGINAL_HINTS)
                          and i != unique_idx), None)
    if unique_idx is None:
        unique_idx = 1
    if original_idx is None:
        original_idx = 0
    return original_idx, unique_idx


def _load_two_column_map(path: str) -> dict:
    if not path or not os.path.exists(path):
        logging.warning(
            f"name map file not found: {path} — every raw value will be "
            f"treated as unmatched (#N/A) and excluded until this file exists."
        )
        return {}

    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {}

    original_idx, unique_idx = _pick_columns(rows[0])

    mapping = {}
    for row in rows[1:]:
        if len(row) <= max(original_idx, unique_idx):
            continue
        raw_val = row[original_idx]
        unique_val = row[unique_idx]
        if raw_val is None or unique_val is None:
            continue
        raw_key = str(raw_val).strip()
        unique_clean = str(unique_val).strip()
        if raw_key and unique_clean:
            mapping[raw_key] = unique_clean

    logging.info(f"loaded {len(mapping)} name mapping(s) from {os.path.basename(path)}")
    return mapping


# Loaded once at import time. Re-running the process (e.g. each GitHub
# Action run) picks up whatever is committed to the repo at that time.
DECREE_NAME_MAP = _load_two_column_map(DECREE_NAME_MAP_XLSX)
ITEM_NAME_MAP = _load_two_column_map(ITEM_NAME_MAP_XLSX)


def normalize_decree_name(raw_description: str):
    """Exact-match lookup against DECREE_NAME_MAP. Returns the unified
    Decree_Unique_Name, or None if raw_description isn't in the map
    (#N/A -> exclude this decree, it's a non-medication service)."""
    if not raw_description:
        return None
    return DECREE_NAME_MAP.get(str(raw_description).strip())


def normalize_item_name(raw_item_name: str):
    """Exact-match lookup against ITEM_NAME_MAP. Returns the unified
    Unique_Items_Names value, or None if raw_item_name isn't in the map
    (#N/A -> exclude this dispensed/billed item)."""
    if not raw_item_name:
        return None
    return ITEM_NAME_MAP.get(str(raw_item_name).strip())
