"""
request_status_sync.py — daily "sent request status" export.

Single-account pipeline (secondary SMC account — SMC_USERNAME_2 /
SMC_PASSWORD_2, same account hospital_decrees_sync.py uses for its list
step), run once a day (via GitHub Actions), Cairo time. Everything below
happens in ONE session/login — no account switch, unlike
hospital_decrees_sync.py.

  STEP 1
      The page at /smc/Reports/SendRequestStatus is just a shell — it does
      NOT render the results table server-side. Its own JS fires a POST to
          /smc/Reports/SendRequestStatusJson
      with CitizenName / StartDate / EndDate / SsnNumber / RequestNumber /
      RequestStatusId / SystemUserId, gets back a JSON array (the response
      body can come back as a JSON-encoded STRING that still needs a
      second json.loads — the page's own JS literally does
      `JSON.parse(JSON.parse(...))` via `DataGlobal = success;
      JSON.parse(DataGlobal)`, so this script mirrors that), and builds the
      table client-side from these fields per record:
          REQUESTID              -> Request_Number  (confirmed: this is the
                                     same number used in
                                     /smc/Requests/Details/{REQUESTID})
          CITIZENFULLNAMEARABIC  -> Patient_Name
          CITIZENSSN              -> Patient_ID (14-digit national ID)
          STATUSARABICNAME        -> Request_Status (captured now — used by
                                     decree-renewal.js's "request placed?"
                                     column, joined via request_name_map)
          REQUESTDATE, SITEARABICNAME -> not used here
      StartDate/EndDate must be sent as "M/D/YYYY 12:00:00 AM" (no zero
      padding), matching exactly what the page's own JS sends — NOT
      YYYY-MM-DD. Request_Date in our output is still stamped with the
      target date we queried for rather than parsed from REQUESTDATE
      (which comes back as a .NET `/Date(169...)/ ` JSON date token, not a
      plain string) — same reasoning as hospital_decrees_sync.py.

  STEP 2 (same session, same account)
      For every Request_Number found in step 1, GET
      /smc/Requests/Details/{request_number} and pull the free-text
      "خطة العلاج" / TREATMENTPLAN textarea -- this is the
      Requested_Decree_Original_Description column.

  STEP 3
      Push 5 columns to Supabase:
          Request_Number | Patient_Name | Request_Date | Patient_ID | Requested_Decree_Original_Description

Run with --dry-run first and open dry_run_output/request_status.csv next to
a manual search on the site for the same day before trusting this for real.

Usage:
    python request_status_sync.py --dry-run
    python request_status_sync.py                    # writes to Supabase
    python request_status_sync.py --date 2026-08-08 --dry-run
"""

import os
import csv
import sys
import time
import json
import logging
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

import smc_session as smc
import supabase_client as sb
from cairo_date import cairo_today_iso

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BASE_URL = smc.BASE_URL
DELAY_BETWEEN_REQUESTS = 0.3   # seconds, between /Requests/Details/{n} calls

SUPABASE_TABLE = "decree_request_status_daily_export"    # <-- adjust to your real table name
SUPABASE_CONFLICT_KEY = "request_number"                  # <-- adjust to your real unique key


# =====================================================================
# STEP 1 — request status list (SendRequestStatusJson)
# =====================================================================
def _smc_datetime_str(date_iso: str) -> str:
    """'2026-08-08' -> '8/8/2026 12:00:00 AM' -- the exact format the
    page's own JS sends for StartDate/EndDate (no zero-padding)."""
    d = datetime.strptime(date_iso, "%Y-%m-%d")
    return f"{d.month}/{d.day}/{d.year} 12:00:00 AM"


def _clean_id(value) -> str:
    """
    !! BUG FIX (confirmed via manual Charles capture) !!
    SendRequestStatusJson's ASP.NET serializer emits numeric ID columns
    (REQUESTID, CITIZENSSN) as JSON floats -- e.g. 67227949.0 instead of
    67227949. json.loads() then hands back a Python float, and the old
    `str(rec.get('REQUESTID') or '')` turned that into the literal string
    '67227949.0'. Every /smc/Requests/Details/{request_number} lookup
    then 404'd, because the site's own Details page only accepts the
    plain integer (confirmed: .../Details/67227949 -> 200,
    .../Details/67227949.0 -> 404, in the same captured session).
    This strips a trailing '.0' off any whole-number float/string before
    it's used anywhere -- in the URL, in the output rows, or as a dict
    key -- so it never leaks downstream again.
    """
    if value is None:
        return ''
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    text = str(value).strip()
    if text.endswith('.0') and text[:-2].lstrip('-').isdigit():
        return text[:-2]
    return text


def _parse_send_request_status_json(raw):
    """Unwraps the response body regardless of whether the server sent a
    real JSON array or a JSON-encoded string containing one (the page's
    own JS unconditionally does a second JSON.parse, so mirror that)."""
    data = raw
    for _ in range(2):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                return []
        else:
            break
    return data if isinstance(data, list) else []


def fetch_request_status_list(session: smc.SMCSession, date_iso: str) -> list:
    url = f"{BASE_URL}/smc/Reports/SendRequestStatusJson"
    smc_datetime = _smc_datetime_str(date_iso)
    payload = {
        'CitizenName': '',
        'StartDate': smc_datetime,
        'EndDate': smc_datetime,
        'SsnNumber': '',
        'RequestNumber': '',
        'RequestStatusId': '',
        'SystemUserId': '',
    }
    logging.info(f"Fetching SendRequestStatusJson for {date_iso} (StartDate/EndDate={smc_datetime})...")
    resp = session.session.post(url, data=payload, timeout=30)
    if resp.status_code != 200:
        logging.error(f"SendRequestStatusJson returned HTTP {resp.status_code}.")
        return []

    try:
        raw = resp.json()
    except ValueError:
        raw = resp.text  # server sent a plain string body; parse it ourselves below

    records = _parse_send_request_status_json(raw)

    rows_out = []
    for rec in records:
        request_number = _clean_id(rec.get('REQUESTID'))
        if not request_number:
            continue
        patient_name = (rec.get('CITIZENFULLNAMEARABIC') or '').strip() or None
        patient_id = _clean_id(rec.get('CITIZENSSN')) or None
        request_status = (rec.get('STATUSARABICNAME') or '').strip() or None
        rows_out.append({
            'request_number': request_number,
            'patient_name': patient_name,
            'patient_id': patient_id,
            'request_status': request_status,
        })
    return rows_out


# =====================================================================
# STEP 2 — treatment plan per request (same session)
# =====================================================================
def get_treatment_plan(session: smc.SMCSession, request_number: str):
    url = f"{BASE_URL}/smc/Requests/Details/{request_number}"
    try:
        resp = session.session.get(url, timeout=30)
    except Exception as e:
        logging.error(f"Error fetching request details for {request_number}: {e}")
        return None
    if resp.status_code != 200:
        logging.warning(f"Request details for {request_number} returned HTTP {resp.status_code}.")
        return None

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, 'html.parser')
    textarea = soup.find('textarea', {'id': 'teatmentPlan'})  # SMC's own typo, kept as-is
    if not textarea:
        return None
    text = textarea.get_text(strip=True)
    return text or None


def fetch_treatment_plans(session: smc.SMCSession, request_numbers: list) -> dict:
    plans = {}
    for i, request_number in enumerate(request_numbers, 1):
        plans[request_number] = get_treatment_plan(session, request_number)
        if i % 25 == 0:
            logging.info(f"Fetched treatment plan for {i}/{len(request_numbers)} requests...")
        time.sleep(DELAY_BETWEEN_REQUESTS)
    return plans


# =====================================================================
# Output shaping
# =====================================================================
def build_output_rows(list_rows: list, plans: dict, date_iso: str) -> list:
    out = []
    seen = set()
    for row in list_rows:
        request_number = row['request_number']
        if request_number in seen:
            continue
        seen.add(request_number)
        out.append({
            'request_number': request_number,
            'patient_name': row.get('patient_name'),
            'request_date': date_iso,
            'patient_id': row.get('patient_id'),
            'requested_decree_original_description': plans.get(request_number),
            'request_status': row.get('request_status'),
        })
    return out


def write_csv(path, rows):
    if not rows:
        logging.info(f"(dry-run) nothing to write for {path}")
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    logging.info(f"(dry-run) wrote {len(rows)} row(s) -> {path}")


# =====================================================================
# MAIN
# =====================================================================
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Write a CSV locally instead of writing to Supabase")
    parser.add_argument("--date", default=None,
                         help="Run for this exact date (YYYY-MM-DD) instead of 'today' in Cairo time.")
    parser.add_argument("--out-dir", default="./dry_run_output")
    args = parser.parse_args()

    target_date = args.date or cairo_today_iso()
    logging.info(f"Target date (Cairo): {target_date}" + (" (explicit --date)" if args.date else ""))

    if not smc.USERNAME_2 or not smc.PASSWORD_2:
        logging.error("SMC_USERNAME_2 / SMC_PASSWORD_2 are not set (env vars) — this script needs the second account.")
        sys.exit(1)

    session = smc.SMCSession(username=smc.USERNAME_2, password=smc.PASSWORD_2)
    if not session.login():
        logging.error("Login with secondary SMC account failed — aborting.")
        sys.exit(1)

    # ---- Step 1 ----
    list_rows = fetch_request_status_list(session, target_date)
    logging.info(f"{len(list_rows)} request row(s) fetched from SendRequestStatusJson for {target_date}.")

    if not list_rows:
        logging.warning("Nothing found for this date. Nothing to do.")
        return

    request_numbers = sorted({r['request_number'] for r in list_rows})

    # ---- Step 2 (same session) ----
    plans = fetch_treatment_plans(session, request_numbers)
    missing = [r for r, p in plans.items() if not p]
    if missing:
        logging.warning(f"{len(missing)} request(s) had no treatment-plan text found (kept as NULL): "
                         f"{missing[:10]}" + (" ..." if len(missing) > 10 else ""))

    # ---- Step 3 ----
    output_rows = build_output_rows(list_rows, plans, target_date)

    if args.dry_run:
        os.makedirs(args.out_dir, exist_ok=True)
        write_csv(os.path.join(args.out_dir, "request_status.csv"), output_rows)
        logging.info(f"DRY RUN complete — review the CSV in {args.out_dir} before running for real.")
    else:
        sb.upsert(SUPABASE_TABLE, output_rows, on_conflict=SUPABASE_CONFLICT_KEY)
        logging.info(f"Sync complete — {len(output_rows)} row(s) upserted into '{SUPABASE_TABLE}'.")


if __name__ == "__main__":
    main()
