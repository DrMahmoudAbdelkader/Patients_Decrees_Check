"""
HMIS Queue Extractor  —  v6b  (REPORT-PATHWAY + CLEAN-TABLE + AJAX DATE FIX)
====================================================================
This version builds on v5 (which already fixed the *source* of the data
by replaying the built-in "Export Excel" report flow instead of
scraping the live queue screen — see the long explanation below if
you're new to this).

FIX in this revision — the AJAX "dateSelect" round trip
---------------------------------------------------------
A real run showed the script correctly PUT '20/07/2026' into the
fromDate_input field of the final POST body — yet the server still
generated the report using its own default date for both From and To.

Root cause: the date fields are PrimeFaces/JSF calendar widgets. The
date you see typed into the box is a client-side display value only —
it is NOT bound to the server-side report parameters by an ordinary
form submit. It only gets bound when the calendar's JS widget fires a
dedicated partial-AJAX request for the "dateSelect" event (this is the
very first POST in the original captured traffic: javax.faces.source=
...fromDate, javax.faces.behavior.event=dateSelect). Skip that round
trip and the server just keeps whatever date is already in its
(stateful) ViewState, no matter what raw value is in the final submit.

`sync_date_field()` now replays that AJAX event for both the From and
To fields before the final export submit, and folds the fresh
javax.faces.ViewState token the server returns back into the request —
so the final submit carries the server's up-to-date state instead of
the stale one from the initial page load.

What's in v6 (still present)
------------------------------
v5 saved the report's raw .xlsx AS-IS. That raw file is the correct
DATA, but it is a printable "letterhead" report, not a table you can
pivot/filter in Excel: it has a title block, then repeats, for every
Clinic -> Resource -> Doctor combination:

    Clinic  <name>                              Date  <dd/mm/yyyy>
    Resource <id> <name>                        Day   <weekday>
    Doctor  <id> <name>
    Medical No. | Time | Slots | Patient Name | Financial Cat. | Sex | Birth Date
                                | Financial Category (full text, row below)
    <patient data row>
    <financial-category full text, own row>
    ... (repeated per patient) ...
    <total slots>
    Total No. of Patient
    (blank rows, then next Clinic/Resource/Doctor block)

...ending in a single "Grand Total" row and a "Powered By D.M.S." footer.
The report also paginates for print (repeats a "Date :"/"Time :"/
"Page X of N" header block mid-block on multi-page runs) — the parser
rejects those lines specifically so they're never miscounted as
patients.

`parse_clinic_report()` walks that structure and turns it into ONE
flat row per patient, with the Clinic/Date/Resource/Doctor context
repeated on every row, ready for Excel filtering/pivoting:

    Date | Day | Clinic | Resource ID | Resource Name | Doctor ID |
    Doctor Name | Medical No. | Time | Slots | Patient Name |
    Financial Cat. Code | Financial Category | Sex | Birth Date

It cross-checks itself against the report's own "Total No. of Patient"
per block and the "Grand Total" cell, printing a warning on any
mismatch. `verify_report_date_range()` separately checks the server's
own From/To header against what was requested and aborts loudly (with
the raw file still saved for inspection) on any mismatch, rather than
silently saving data for the wrong range.

Output
------
For each run you get TWO files in OUTPUT_DIR:
  1. <daterange>_<report>.xlsx            <- the raw file exactly as the
                                              server generated it (kept
                                              for audit / re-parsing)
  2. <daterange>_<report>_CLEAN.xlsx       <- the flat table described
                                              above — this is the one
                                              you actually work with

pip install requests openpyxl
"""

import os
import re
import sys
import html as html_module
from datetime import datetime
import requests
from openpyxl import load_workbook, Workbook
from openpyxl.utils import get_column_letter

# ═════════════════════════════════════════════════════════════════
# CONFIGURATION – edit these before running
# ═════════════════════════════════════════════════════════════════

HOST            = "41.33.24.254:8080"
WEBREPORT_BASE  = f"http://{HOST}/WebReport-JWEB"

# CHANGED for the decree-renewal pipeline: "Clinic List Detail - BY STATUS"
# (outpat_clnc_lst_det_sts_j) — the report whose fixed-column layout
# queue_parser.py is built to read (has the National ID column). The
# original "outpat_clnc_lst_det_j" report used by v6 of this script does
# NOT carry a national ID column, which is why it's no longer used here.
REPORT_CODE     = "outpat_clnc_lst_det_sts_j"
LANG            = "L"
HSCD            = "01"                          # hospital/branch code

# ---- DATE RANGE TO EXTRACT (only used when running this file directly) ----
DATE_FROM = "30-08-2026"  # dd-mm-yyyy, inclusive
DATE_TO   = "30-08-2026"  # dd-mm-yyyy, inclusive

# ---- Optional: restrict to one physician/resource. Leave both blank for ALL. ----
RESOURCE_ID   = ""
RESOURCE_NAME = ""

TIMEOUT = 60
OUTPUT_DIR = r"D:\Queue_DMS_Data"

# ═════════════════════════════════════════════════════════════════
# HTTP CONSTANTS
# ═════════════════════════════════════════════════════════════════

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/150.0.0.0 Safari/537.36")

NAV_GET_HEADERS = {
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8,"
               "application/signed-exchange;v=b3;q=0.7"),
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": _UA,
}

FORM_POST_HEADERS = {
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8,"
               "application/signed-exchange;v=b3;q=0.7"),
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "max-age=0",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": f"http://{HOST}",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": _UA,
}

# Headers for the JSF/PrimeFaces partial-AJAX "dateSelect" replay (see
# sync_date_field() below for why this round trip is required).
AJAX_POST_HEADERS = {
    "Accept": "application/xml, text/xml, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Faces-Request": "partial/ajax",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": f"http://{HOST}",
    "User-Agent": _UA,
}

INPUT_RE  = re.compile(r'<input\b([^>]*?)/?>', re.I)
SELECT_RE = re.compile(r'<select\b([^>]*)>(.*?)</select>', re.I | re.S)
NAME_RE   = re.compile(r'name\s*=\s*"([^"]*)"')
VALUE_RE  = re.compile(r'value\s*=\s*"([^"]*)"')
TYPE_RE   = re.compile(r'type\s*=\s*"([^"]*)"', re.I)
CHECKED_RE = re.compile(r'\bchecked\s*=\s*"checked"', re.I)
OPTION_SELECTED_RE = re.compile(r'<option\s+value="([^"]*)"\s+selected="selected"', re.I)
OPTION_FIRST_RE    = re.compile(r'<option\s+value="([^"]*)"', re.I)

# Matches: mojarra.jsfcljs(document.getElementById('form'),{'ID':'ID'},'')
#          ...<input type="button" ... value="Export Excel" />
EXPORT_BTN_RE = re.compile(
    r"jsfcljs\(document\.getElementById\('(\w+)'\),\{'([^']+)':'[^']+'\}"
    r"[^)]*\)[^<]*<input[^>]*value=\"Export (\w+)\"",
    re.I,
)


# ═════════════════════════════════════════════════════════════════
# FORM-FIELD REPLAY ENGINE  (unchanged from v5 — this part was already
# validated byte-for-byte against your captured browser POST)
# ═════════════════════════════════════════════════════════════════

def extract_form_fields(html_text):
    """
    Pull every submittable <input>/<select> field out of the page, in
    document order, preserving duplicates (JSF renders the same
    javax.faces.ViewState hidden field once per naming-container form,
    and the real browser submits it twice — we replicate that exactly).

    Buttons are skipped (they're added explicitly via find_export_button).
    Unchecked checkboxes/radios are skipped (browsers never submit them).
    """
    fields = []

    for m in INPUT_RE.finditer(html_text):
        attrs = m.group(1)
        name_m = NAME_RE.search(attrs)
        if not name_m:
            continue
        name = html_module.unescape(name_m.group(1))

        typ_m = TYPE_RE.search(attrs)
        typ = typ_m.group(1).lower() if typ_m else "text"
        if typ == "button":
            continue
        if typ in ("checkbox", "radio") and not CHECKED_RE.search(attrs):
            continue

        val_m = VALUE_RE.search(attrs)
        value = html_module.unescape(val_m.group(1)) if val_m else ""
        fields.append([name, value])

    for m in SELECT_RE.finditer(html_text):
        attrs, body = m.groups()
        name_m = NAME_RE.search(attrs)
        if not name_m:
            continue
        name = html_module.unescape(name_m.group(1))
        opt_m = OPTION_SELECTED_RE.search(body) or OPTION_FIRST_RE.search(body)
        value = html_module.unescape(opt_m.group(1)) if opt_m else ""
        fields.append([name, value])

    return fields


_DDMMYYYY_RE = re.compile(r'^\d{1,2}/\d{1,2}/\d{4}$')


def find_date_range_fields(fields):
    """
    Locate the report's OWN From/To date inputs — NOT the unrelated
    fromDate_input/toDate_input that live inside the generic patient-
    search panel on the same page (that panel's date fields are always
    blank on page load; the report's own date fields come pre-populated
    with a real dd/mm/yyyy default, which is what we key off of here —
    far more reliable than guessing by a sibling field's name, which
    silently picked the WRONG field on a real run: the request that
    went out used today's date for both From and To instead of the
    requested range, and nothing errored because the guessed field
    name just didn't match anything and got silently appended as an
    unused extra field instead of overriding the real one).
    """
    value_by_name = {}
    for n, v in fields:
        value_by_name.setdefault(n, v)  # first occurrence wins

    from_names = [n for n in value_by_name if n.endswith(':fromDate_input')]
    to_names   = [n for n in value_by_name if n.endswith(':toDate_input')]

    # Prefer pairs that (a) share the same naming-container prefix and
    # consecutive indices (…:1:fromDate_input / …:2:toDate_input, the
    # pattern seen in every capture so far), AND (b) are BOTH currently
    # populated with a real date — that's the report's own always-visible
    # date range picker, not the blank patient-search panel fields.
    candidates = []
    for fn in from_names:
        m = re.match(r'^(.+):(\d+):fromDate_input$', fn)
        if not m:
            continue
        prefix, idx = m.group(1), int(m.group(2))
        tn = f"{prefix}:{idx + 1}:toDate_input"
        if tn in to_names:
            fv, tv = value_by_name[fn], value_by_name[tn]
            populated = bool(_DDMMYYYY_RE.match(fv or "")) and bool(_DDMMYYYY_RE.match(tv or ""))
            candidates.append((populated, prefix, fn, tn))

    # Populated candidates first; if several, prefer the one whose prefix
    # also owns a physcResourceId field (extra corroborating signal).
    candidates.sort(key=lambda c: (not c[0]), reverse=False)
    for populated, prefix, fn, tn in candidates:
        if populated:
            print(f"   -> Date fields detected: {fn}={value_by_name[fn]!r}  "
                  f"{tn}={value_by_name[tn]!r}")
            return fn, tn, f"{prefix}:0:physcResourceId", f"{prefix}:0:physcResourceName"

    # No populated pair found — fall back to ANY from/to pair with matching
    # prefix+consecutive index, even if currently blank, rather than guessing
    # a field name that may not exist at all.
    if candidates:
        populated, prefix, fn, tn = candidates[0]
        print(f"   !! No pre-populated From/To pair found; falling back to "
              f"{fn} / {tn} (currently blank). Verify the result's own "
              f"From/To header after this run.")
        return fn, tn, f"{prefix}:0:physcResourceId", f"{prefix}:0:physcResourceName"

    raise RuntimeError(
        "Could not find ANY fromDate_input/toDate_input field pair with "
        "consecutive indices on the report page. The page structure has "
        "changed — send an updated HAR capture of the manual export."
    )


def find_export_button(html_text, label="Excel"):
    """Find the (form_id, field_name) for the 'Export <label>' button."""
    for form_id, field_name, btn_label in EXPORT_BTN_RE.findall(html_text):
        if btn_label.lower() == label.lower():
            return form_id, field_name
    return None, None


def set_field(fields, name, value):
    """Set value for every occurrence of `name` in the fields list (in place)."""
    hit = False
    for pair in fields:
        if pair[0] == name:
            pair[1] = value
            hit = True
    return hit


# ═════════════════════════════════════════════════════════════════
# REPORT FETCH
# ═════════════════════════════════════════════════════════════════

VIEWSTATE_UPDATE_RE = re.compile(
    r'<update\s+id="javax\.faces\.ViewState"[^>]*>\s*<!\[CDATA\[(.*?)\]\]>\s*</update>',
    re.S,
)


def sync_date_field(session, post_url, referer, fields, input_field_name, value):
    """
    Replays the browser's AJAX "dateSelect" event for a calendar field.

    THIS IS THE PIECE THAT WAS MISSING. Your last run proved it: the
    script correctly PUT '20/07/2026' into the fromDate_input field in
    the final POST body, but the server still generated the report
    using its own default date (27/07/2026) for both From and To.

    That's because these are PrimeFaces/JSF calendar widgets: the date
    you see typed in the box is only a client-side display value. It
    does NOT get bound to the server-side report parameters through an
    ordinary form submit. It's only bound when the calendar's JS widget
    fires a dedicated partial-AJAX request for the "dateSelect" event —
    exactly the very first POST in your original captured traffic
    (javax.faces.source=...fromDate, javax.faces.behavior.event=
    dateSelect). Skip that round trip, and the server just keeps
    whatever date was already in its ViewState — no matter what raw
    value you send afterwards, which is exactly the symptom you hit.

    This function fires that AJAX event for one date field, then reads
    the fresh javax.faces.ViewState token out of the server's partial-
    response XML and folds it back into `fields` (mutated in place) so
    the next request — whether another dateSelect or the final export
    submit — carries the server's up-to-date state instead of the
    stale one from the initial page load.

    Returns True if the server acknowledged with an updated ViewState,
    False otherwise (a warning is also printed in that case).
    """
    component_id = (input_field_name[:-len("_input")]
                     if input_field_name.endswith("_input") else input_field_name)

    set_field(fields, input_field_name, value)

    ajax_fields = [
        ["javax.faces.partial.ajax", "true"],
        ["javax.faces.source", component_id],
        ["javax.faces.partial.execute", component_id],
        ["javax.faces.behavior.event", "dateSelect"],
        ["javax.faces.partial.event", "dateSelect"],
    ] + list(fields)

    headers = dict(AJAX_POST_HEADERS)
    headers["Referer"] = referer

    resp = session.post(post_url, headers=headers, data=ajax_fields, timeout=TIMEOUT)
    resp.raise_for_status()

    m = VIEWSTATE_UPDATE_RE.search(resp.text)
    if not m:
        print(f"   !! AJAX dateSelect for {input_field_name}={value} did not "
              f"return an updated ViewState (response started with: "
              f"{resp.text[:150]!r}). The date change may not register "
              f"server-side — check the final report's own From/To header.")
        return False

    new_viewstate = m.group(1)
    for pair in fields:
        if pair[0] == "javax.faces.ViewState":
            pair[1] = new_viewstate
    return True

def looks_like_report_form(html_text):
    return ('javax.faces.ViewState' in html_text
            and 'fromDate_input' in html_text
            and 'txtUsername' not in html_text)  # not a login page


def fetch_report(session, report_code, date_from_ddmmyyyy, date_to_ddmmyyyy,
                  resource_id="", resource_name=""):
    """
    Runs the two-step report-export flow and returns (filename, content_bytes).
    Raises RuntimeError with a diagnostic snippet on failure.
    """
    get_url = f"{WEBREPORT_BASE}/?repCode={report_code}&&lang={LANG}&&hscd={HSCD}"
    print(f"   -> GET  {get_url}")
    r = session.get(get_url, headers=NAV_GET_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    page_html = r.text

    if not looks_like_report_form(page_html):
        snippet = page_html[:1000]
        raise RuntimeError(
            "Response doesn't look like the report parameter page "
            "(no ViewState/date fields found, or it looks like a login "
            "page). First 1000 chars:\n" + snippet
        )

    fields = extract_form_fields(page_html)
    from_key, to_key, res_id_key, res_name_key = find_date_range_fields(fields)

    post_url = f"{WEBREPORT_BASE}/faces/report/HMISReports.xhtml"

    print(f"   -> Replaying dateSelect for {from_key} = {date_from_ddmmyyyy}")
    sync_date_field(session, post_url, get_url, fields, from_key, date_from_ddmmyyyy)

    print(f"   -> Replaying dateSelect for {to_key} = {date_to_ddmmyyyy}")
    sync_date_field(session, post_url, get_url, fields, to_key, date_to_ddmmyyyy)

    if resource_id:
        set_field(fields, res_id_key, resource_id)
    if resource_name:
        set_field(fields, res_name_key, resource_name)

    form_id, export_field = find_export_button(page_html, label="Excel")
    if not export_field:
        raise RuntimeError(
            "Could not find the 'Export Excel' button on the report page. "
            "The page layout may differ from the captured session."
        )
    fields.append([export_field, export_field])

    post_headers = dict(FORM_POST_HEADERS)
    post_headers["Referer"] = get_url

    print(f"   -> POST {post_url}  ({from_key}={date_from_ddmmyyyy}  "
          f"{to_key}={date_to_ddmmyyyy})")
    resp = session.post(post_url, headers=post_headers, data=fields, timeout=TIMEOUT)
    resp.raise_for_status()

    content_disp = resp.headers.get("Content-Disposition", "")
    is_binary_xlsx = resp.content[:2] == b"PK"

    if "attachment" not in content_disp.lower() or not is_binary_xlsx:
        snippet = resp.text[:1000] if not is_binary_xlsx else "<binary, but no attachment header>"
        raise RuntimeError(
            "Report submission didn't return a file download as expected "
            f"(Content-Disposition={content_disp!r}). First part of response:\n"
            + snippet
        )

    fname_m = re.search(r'filename=([^;]+)', content_disp)
    filename = fname_m.group(1).strip() if fname_m else f"{report_code}.xlsx"
    return filename, resp.content


def verify_report_date_range(xlsx_bytes, expected_from_ddmmyyyy, expected_to_ddmmyyyy):
    """
    Reads the "From ... To ..." header the SERVER printed on the returned
    report and compares it to what was actually requested. This is the
    safety net for the exact failure that slipped through before: the
    From-field override silently not applying, so the server ran the
    report against its own default date instead of the requested range,
    with no error anywhere in the chain.

    Returns (actual_from, actual_to). Raises RuntimeError on mismatch.
    """
    import io
    wb = load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    ws = wb.active

    actual_from = actual_to = None
    for r in range(1, min(20, ws.max_row) + 1):
        row_cells = {c: ws.cell(row=r, column=c).value for c in range(1, ws.max_column + 1)}
        row_cells = {c: v for c, v in row_cells.items() if v is not None and str(v).strip() != ""}
        cols = sorted(row_cells)
        for c in cols:
            label = str(row_cells[c]).strip()
            if label == "From":
                later = [c2 for c2 in cols if c2 > c]
                if later:
                    actual_from = str(row_cells[later[0]]).strip()
            if label == "To":
                later = [c2 for c2 in cols if c2 > c]
                if later:
                    actual_to = str(row_cells[later[0]]).strip()
        if actual_from and actual_to:
            break

    if actual_from is None or actual_to is None:
        raise RuntimeError(
            "Could not find the 'From ... To ...' header on the returned "
            "report to verify the date range — check the raw file manually."
        )

    if actual_from != expected_from_ddmmyyyy or actual_to != expected_to_ddmmyyyy:
        raise RuntimeError(
            f"DATE RANGE MISMATCH — you requested From={expected_from_ddmmyyyy} "
            f"To={expected_to_ddmmyyyy}, but the report the server generated "
            f"is actually From={actual_from} To={actual_to}. This means the "
            f"date-field override didn't reach the server correctly (the "
            f"raw file has still been saved so you can inspect it). Do NOT "
            f"use the CLEAN output from this run — it would silently "
            f"describe the wrong date range."
        )

    return actual_from, actual_to


# ═════════════════════════════════════════════════════════════════
# CLEAN-TABLE PARSER  (new in v6)
# ═════════════════════════════════════════════════════════════════

CLEAN_HEADERS = [
    "Date", "Day", "Clinic",
    "Resource ID", "Resource Name",
    "Doctor ID", "Doctor Name",
    "Medical No.", "Time", "Slots", "Patient Name",
    "Financial Cat. Code", "Financial Category",
    "Sex", "Birth Date",
]

HEADER_LABELS = ["Medical No.", "Time", "Slots", "Patient Name",
                  "Financial Cat.", "Sex", "Birth Date"]


def _row_values(ws, r, max_col):
    """Return a dict {col_index: value} of non-empty cells in row r."""
    out = {}
    for c in range(1, max_col + 1):
        v = ws.cell(row=r, column=c).value
        if v is not None and str(v).strip() != "":
            out[c] = v.strip() if isinstance(v, str) else v
    return out


def _label_value(cells, label, search_from=1):
    """
    Given a {col: value} row dict, find the column holding exactly
    `label` (or `label` with a trailing ':' / stripped) at/after
    `search_from`, then return the next non-empty cell value to its
    right (this survives merged cells shifting the value's own
    anchor column by 1-2 cells, which is what the real report does).
    """
    cols = sorted(c for c in cells if c >= search_from)
    for c in cols:
        val = str(cells[c]).strip().rstrip(":").strip()
        if val == label:
            for c2 in cols:
                if c2 > c:
                    return cells[c2]
            return None
    return None


_NUMERIC_RE = re.compile(r'^-?\d+(\.\d+)?$')


def _as_number(v):
    """
    The real report stores ALL cell values as text, including totals
    (e.g. the 'Grand Total' cell holds the string '547', not the int
    547). Treat any int/float OR any numeric-looking string as a number;
    checking only isinstance(v, (int, float)) silently finds nothing on
    the real file and makes the consistency checks a no-op without
    ever raising a warning — which is exactly the kind of silent
    failure this script exists to avoid.
    """
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str) and _NUMERIC_RE.match(v.strip()):
        return float(v.strip()) if "." in v else int(v.strip())
    return None


def _nearby_value(cells, target_col, window=3):
    """Return the value at target_col, or the closest populated column
    within `window` columns either side (absorbs merged-cell offsets)."""
    if target_col in cells:
        return cells[target_col]
    for d in range(1, window + 1):
        if target_col - d in cells:
            return cells[target_col - d]
        if target_col + d in cells:
            return cells[target_col + d]
    return None


def parse_clinic_report(xlsx_bytes):
    """
    Parses the raw "Clinic List Detail (with Status)" report bytes into
    a flat list of patient-record dicts (one per patient booking), plus
    a list of warning strings for any consistency check that failed.
    """
    import io
    wb = load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    ws = wb.active
    max_col = ws.max_column
    max_row = ws.max_row

    records = []
    warnings = []

    ctx = {
        "clinic": None, "date": None,
        "resource_id": None, "resource_name": None, "day": None,
        "doctor_id": None, "doctor_name": None,
    }

    grand_total_reported = None
    block_totals_reported = []
    block_totals_actual = []

    r = 1
    while r <= max_row:
        cells = _row_values(ws, r, max_col)
        if not cells:
            r += 1
            continue

        values_set = set(str(v).strip() for v in cells.values() if isinstance(v, str))

        # --- context lines -------------------------------------------------
        if "Clinic" in values_set:
            ctx["clinic"] = _label_value(cells, "Clinic")
            d = _label_value(cells, "Date")
            if d is not None:
                ctx["date"] = d
            r += 1
            continue

        if "Resource" in values_set:
            cols = sorted(cells)
            # Resource id is the first value after the "Resource" label;
            # Resource name is the next populated value after that.
            resource_id = _label_value(cells, "Resource")
            ctx["resource_id"] = resource_id
            # name = value after the id column
            id_col = None
            for c, v in cells.items():
                if v == resource_id:
                    id_col = c
                    break
            if id_col is not None:
                later = [c for c in cols if c > id_col]
                ctx["resource_name"] = cells[later[0]] if later else None
            day = _label_value(cells, "Day")
            if day is not None:
                ctx["day"] = day
            r += 1
            continue

        if "Doctor" in values_set:
            doctor_id = _label_value(cells, "Doctor")
            ctx["doctor_id"] = doctor_id
            cols = sorted(cells)
            id_col = None
            for c, v in cells.items():
                if v == doctor_id:
                    id_col = c
                    break
            if id_col is not None:
                later = [c for c in cols if c > id_col]
                ctx["doctor_name"] = cells[later[0]] if later else None
            r += 1
            continue

        # --- Grand Total footer ---------------------------------------------
        if "Grand Total" in values_set:
            nums = [n for n in (_as_number(v) for v in cells.values()) if n is not None]
            if nums:
                grand_total_reported = nums[0]
            r += 1
            continue

        # --- per-block patient total -----------------------------------------
        if "Total No. of Patient" in values_set:
            # the numeric total sits on the row ABOVE this label (seen in
            # the captured file: slots-total row, then the label row)
            prev_cells = _row_values(ws, r - 1, max_col)
            nums = [n for n in (_as_number(v) for v in prev_cells.values()) if n is not None]
            if nums:
                block_totals_reported.append((ctx["clinic"], ctx["date"], ctx["doctor_name"], nums[0]))
            r += 1
            continue

        # --- header row: "Medical No." + "Time" + "Patient Name" ... ---------
        if "Medical No." in values_set and "Patient Name" in values_set:
            header_cols = {}
            for c, v in cells.items():
                if isinstance(v, str) and v.strip() in HEADER_LABELS:
                    header_cols[v.strip()] = c

            # There is a sub-header row right below ("Financial Category")
            # that we skip.
            r += 2

            block_patient_count = 0
            while r <= max_row:
                pcells = _row_values(ws, r, max_col)
                pvals = set(str(v).strip() for v in pcells.values() if isinstance(v, str))

                if not pcells:
                    r += 1
                    continue
                if "Total No. of Patient" in pvals:
                    break  # let the outer loop handle the total-line
                if "Clinic" in pvals or "Grand Total" in pvals:
                    break  # malformed / unexpected — bail to outer loop

                # Detect a patient DATA row: must have a numeric Medical No.
                # and an H:MM-shaped Time. This also rejects the repeated
                # print-pagination header block that lands MID-BLOCK on a
                # multi-page report ("Clinic List Detail" / "Date :" /
                # "Time :" / "From ... To ..." / "Page X of 40") — without
                # this, the "Date :"/"Time :" lines' own values (e.g.
                # '27/07/2026', '02.44') fell inside the nearby-column
                # search window for Medical No./Time and got miscounted
                # as extra patients, inflating every block that happened
                # to span a page break.
                med_no_raw = _nearby_value(pcells, header_cols.get("Medical No.", 3))
                time_raw = _nearby_value(pcells, header_cols.get("Time", 6))
                patient_name = _nearby_value(pcells, header_cols.get("Patient Name", 20))

                med_no = str(med_no_raw).strip() if med_no_raw is not None else None
                time_val = str(time_raw).strip() if time_raw is not None else None

                looks_like_data_row = (
                    med_no is not None and re.match(r'^\d+$', med_no)
                    and time_val is not None and re.match(r'^\d{1,2}:\d{2}$', time_val)
                )

                if looks_like_data_row:
                    rec = {
                        "Date": ctx["date"],
                        "Day": ctx["day"],
                        "Clinic": ctx["clinic"],
                        "Resource ID": ctx["resource_id"],
                        "Resource Name": ctx["resource_name"],
                        "Doctor ID": ctx["doctor_id"],
                        "Doctor Name": ctx["doctor_name"],
                        "Medical No.": med_no,
                        "Time": time_val,
                        "Slots": _nearby_value(pcells, header_cols.get("Slots", 14)),
                        "Patient Name": patient_name,
                        "Financial Cat. Code": _nearby_value(pcells, header_cols.get("Financial Cat.", 27)),
                        "Financial Category": None,   # filled from the next row, below
                        "Sex": _nearby_value(pcells, header_cols.get("Sex", 32)),
                        "Birth Date": _nearby_value(pcells, header_cols.get("Birth Date", 39)),
                    }

                    # The row immediately below holds the full-text financial
                    # category, in the same column as Patient Name.
                    fincat_cells = _row_values(ws, r + 1, max_col)
                    name_col = header_cols.get("Patient Name", 20)
                    fincat_val = _nearby_value(fincat_cells, name_col)
                    # guard: don't grab it if it's actually the next block's label row
                    if fincat_val is not None and not set(
                        str(v).strip() for v in fincat_cells.values() if isinstance(v, str)
                    ) & {"Clinic", "Resource", "Doctor", "Total No. of Patient"}:
                        rec["Financial Category"] = fincat_val
                        r += 1  # consume the fin-category row too

                    records.append(rec)
                    block_patient_count += 1

                r += 1

            block_totals_actual.append((ctx["clinic"], ctx["date"], ctx["doctor_name"], block_patient_count))
            continue

        r += 1

    # --- consistency checks --------------------------------------------------
    for (reported, actual) in zip(block_totals_reported, block_totals_actual):
        r_clinic, r_date, r_doc, r_total = reported
        a_clinic, a_date, a_doc, a_total = actual
        if r_total != a_total:
            warnings.append(
                f"Block mismatch — Clinic={r_clinic!r} Date={r_date!r} "
                f"Doctor={r_doc!r}: report says 'Total No. of Patient' = "
                f"{r_total}, but parser extracted {a_total} rows."
            )

    if grand_total_reported is not None:
        parsed_total = len(records)
        if grand_total_reported != parsed_total:
            warnings.append(
                f"GRAND TOTAL mismatch: report's 'Grand Total' cell = "
                f"{grand_total_reported}, but parser extracted "
                f"{parsed_total} patient rows in total. Something was "
                f"missed or double-counted — do not trust this run until "
                f"resolved."
            )
    else:
        warnings.append("Could not find a 'Grand Total' cell to cross-check against.")

    return records, warnings


def write_clean_excel(records, out_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Clean Queue Data"
    ws.append(CLEAN_HEADERS)
    for rec in records:
        ws.append([rec.get(h) for h in CLEAN_HEADERS])

    # basic readability: bold header, freeze it, autosize-ish column widths
    for c in range(1, len(CLEAN_HEADERS) + 1):
        ws.cell(row=1, column=c).font = ws.cell(row=1, column=c).font.copy(bold=True)
    ws.freeze_panes = "A2"
    for c, header in enumerate(CLEAN_HEADERS, start=1):
        max_len = len(str(header))
        for rec in records:
            v = rec.get(header)
            if v is not None:
                max_len = max(max_len, len(str(v)))
        ws.column_dimensions[get_column_letter(c)].width = min(max_len + 2, 45)

    wb.save(out_path)


# ═════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════

def to_slash_date(ddmmyyyy_dash):
    d = datetime.strptime(ddmmyyyy_dash, "%d-%m-%Y")
    return d.strftime("%d/%m/%Y")


def main():
    print("=" * 70)
    print("  HMIS Queue Extractor — report-pathway + clean-table (v6)")
    print(f"  Target      : {WEBREPORT_BASE}")
    print(f"  Report code : {REPORT_CODE}")
    print(f"  Date range  : {DATE_FROM} -> {DATE_TO}")
    print("=" * 70)

    date_from = to_slash_date(DATE_FROM)
    date_to   = to_slash_date(DATE_TO)

    session = requests.Session()

    try:
        filename, content = fetch_report(
            session, REPORT_CODE, date_from, date_to,
            resource_id=RESOURCE_ID, resource_name=RESOURCE_NAME,
        )
    except Exception as e:
        print(f"\nXX Extraction failed: {e}")
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    raw_name = f"{DATE_FROM}_to_{DATE_TO}_{filename}"
    raw_path = os.path.join(OUTPUT_DIR, raw_name)
    with open(raw_path, "wb") as f:
        f.write(content)
    print(f"\n-- Saved raw report   ({len(content):,} bytes) -> {raw_path}")

    print("\n-- Verifying the server actually used the requested date range...")
    try:
        actual_from, actual_to = verify_report_date_range(content, date_from, date_to)
        print(f"   OK: report header confirms From={actual_from} To={actual_to}")
    except Exception as e:
        print(f"\nXX {e}")
        sys.exit(1)

    print("\n-- Parsing into a clean flat table...")
    try:
        records, warnings = parse_clinic_report(content)
    except Exception as e:
        print(f"XX Parsing failed: {e}")
        print("   The raw file was still saved above — you can inspect it")
        print("   manually, or send it back to fix the parser.")
        sys.exit(1)

    clean_name = f"{DATE_FROM}_to_{DATE_TO}_{os.path.splitext(filename)[0]}_CLEAN.xlsx"
    clean_path = os.path.join(OUTPUT_DIR, clean_name)
    write_clean_excel(records, clean_path)

    print(f"-- Saved clean table  ({len(records)} patient rows) -> {clean_path}")

    if warnings:
        print("\n!! CONSISTENCY WARNINGS — review before trusting this run:")
        for w in warnings:
            print("   -", w)
    else:
        print("\n-- Consistency check passed: parsed row count matches the")
        print("   report's own per-block and Grand Total figures.")

    print("\nDone.")


if __name__ == "__main__":
    main()