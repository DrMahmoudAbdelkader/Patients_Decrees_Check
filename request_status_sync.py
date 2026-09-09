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
          REQUESTDATE             -> Request_Date (parsed from the .NET
                                     `/Date(169...)/ ` epoch-ms token via
                                     _parse_dotnet_date() -- this is each
                                     request's OWN submission date, not
                                     the day the script ran; see the
                                     LOOKBACK_DAYS note below for why
                                     that distinction now matters)
          SITEARABICNAME          -> not used here
      StartDate/EndDate are sent as "M/D/YYYY 12:00:00 AM" / "... 11:59:59
      PM" (no zero padding), matching exactly what the page's own JS
      sends — NOT YYYY-MM-DD.

      IMPORTANT — this now queries a LOOKBACK_DAYS-day WINDOW (default 15
      days ending today), not just "today". A request's status keeps
      changing after it's submitted, but it only ever matches a
      date-filtered query on the days within that filter, so a
      single-day-only query (the old behavior) permanently freezes a
      request's status at whatever it was on its creation day once that
      day has passed -- this was the root cause of the daily-scan
      module's pending-request statuses drifting out of sync with the
      live site. See the DEFAULT_LOOKBACK_DAYS comment further down.

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
    python request_status_sync.py                    # writes to Supabase, refreshes last 15 days
    python request_status_sync.py --lookback-days 30  # wider refresh window
    python request_status_sync.py --date 2026-08-08 --dry-run   # exact single day (backfill use)
"""

import os
import re
import csv
import sys
import time
import json
import html
import logging
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

import smc_session as smc
import supabase_client as sb
from cairo_date import cairo_today_iso, CAIRO_TZ
import admin_letter_lookup as letters

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BASE_URL = smc.BASE_URL
DELAY_BETWEEN_REQUESTS = 0.3   # seconds, between /Requests/Details/{n} calls

SUPABASE_TABLE = "decree_request_status_daily_export"    # <-- adjust to your real table name
SUPABASE_CONFLICT_KEY = "request_number"                  # <-- adjust to your real unique key

# !! BUG FIX (root cause of stale/"ancient" statuses in the daily-scan
# module and decree-value-left.js) !!
# This script used to query SendRequestStatusJson for StartDate=EndDate
# ="today" ONLY. A request's status keeps changing for days/weeks after
# it was first submitted (تم التسجيل -> توصية مبدئية -> لجنة طبية -> ...
# -> قرار نهائى), but a request only ever matches a "today"-only date
# filter on the single day it was CREATED. Every day after that, the
# request silently drops out of the query results entirely, so its row
# in decree_request_status_daily_export (upserted by request_number)
# never gets touched again -- it stays frozen at whatever status it had
# on day one, even after it's actually been resolved on the real site.
# queue_value_left_scan.py's fetch_patient_pending_requests() then reads
# that same frozen status and can keep reporting a request as "still
# open" indefinitely, out of sync with a fresh manual pull.
#
# Fix: re-query a rolling LOOKBACK_DAYS-day window every run (default
# 15, matching the window used to manually cross-check this), not just
# today. Any request whose submission date still falls in that window
# gets its status re-fetched and upserted with today's real value,
# self-healing the frozen-status problem within LOOKBACK_DAYS days of
# it occurring. A request still open past that window will need a
# larger --lookback-days (or a one-off --date backfill) to refresh --
# see the CLI args in main().
DEFAULT_LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", 15))

# !! NEW FEATURE — admin-letter response text, appended (not
# substituted) alongside the pending-requests info the app already
# shows !!
# "خطاب ادارى" / "خطاب إداري" (an administrative letter) is one of the
# FINAL statuses in queue_value_left_scan.REQUEST_FINAL_STATUSES -- it
# means the request did NOT result in a decree; it's usually a decline
# or a redirect. When one of these shows up within the last
# ADMIN_LETTER_LOOKBACK_DAYS days (relative to the day this script
# runs), that's a strong, recent signal worth surfacing: it likely
# explains why a patient's decree coverage looks like it "should" exist
# but doesn't. Per spec, this is ADDITIVE -- it must never remove or
# replace the existing up-to-2 pending-requests display, only add a
# clearly-separate "here's what the admin letter actually said" piece
# alongside/after it. See fetch_admin_letters_for_window() below and
# ADMIN_LETTER_TABLE / decree_admin_letter_details.
ADMIN_LETTER_TABLE = "decree_admin_letter_details"
DEFAULT_ADMIN_LETTER_LOOKBACK_DAYS = int(os.environ.get("ADMIN_LETTER_LOOKBACK_DAYS", 10))


# =====================================================================
# STEP 1 — request status list (SendRequestStatusJson)
# =====================================================================
def _smc_datetime_str(date_iso: str, end_of_day: bool = False) -> str:
    """'2026-08-08' -> '8/8/2026 12:00:00 AM' -- the exact format the
    page's own JS sends for StartDate/EndDate (no zero-padding).
    `end_of_day=True` gives '... 11:59:59 PM' for use as EndDate so a
    same-day request isn't excluded by an exact-midnight boundary."""
    d = datetime.strptime(date_iso, "%Y-%m-%d")
    if end_of_day:
        return f"{d.month}/{d.day}/{d.year} 11:59:59 PM"
    return f"{d.month}/{d.day}/{d.year} 12:00:00 AM"


_DOTNET_DATE_RE = re.compile(r"/Date\((-?\d+)\)/")


def _parse_dotnet_date(value, fallback_iso: str = None):
    """ASP.NET JSON serializes DateTime as '/Date(1699999999000)/'
    (epoch milliseconds). Converts to a plain 'YYYY-MM-DD' string in
    Cairo local time so Request_Date reflects when the request was
    ACTUALLY submitted (needed now that one run's query window can
    span many days) instead of the day the script happened to run.
    Falls back to `fallback_iso` (or None) if parsing fails for any
    reason -- never raises."""
    if not value:
        return fallback_iso
    m = _DOTNET_DATE_RE.search(str(value))
    if not m:
        return fallback_iso
    try:
        epoch_ms = int(m.group(1))
        dt = datetime.fromtimestamp(epoch_ms / 1000, tz=CAIRO_TZ)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return fallback_iso


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


def fetch_request_status_list(session: smc.SMCSession, start_date_iso: str, end_date_iso: str) -> list:
    """Queries SendRequestStatusJson for every request whose submission
    date falls in [start_date_iso, end_date_iso] (inclusive), so a
    request placed days ago but still open gets its CURRENT status
    re-fetched on every run instead of only on its creation day (see
    the DEFAULT_LOOKBACK_DAYS note above for why this matters)."""
    url = f"{BASE_URL}/smc/Reports/SendRequestStatusJson"
    start_dt = _smc_datetime_str(start_date_iso)
    end_dt = _smc_datetime_str(end_date_iso, end_of_day=True)
    payload = {
        'CitizenName': '',
        'StartDate': start_dt,
        'EndDate': end_dt,
        'SsnNumber': '',
        'RequestNumber': '',
        'RequestStatusId': '',
        'SystemUserId': '',
    }
    logging.info(f"Fetching SendRequestStatusJson for {start_date_iso}..{end_date_iso} "
                 f"(StartDate={start_dt}, EndDate={end_dt})...")
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
        # Parse the record's OWN submission date instead of stamping
        # every row with whatever date the script happened to run for
        # -- essential now that one query can span a multi-day window.
        request_date = _parse_dotnet_date(rec.get('REQUESTDATE'), fallback_iso=end_date_iso)
        rows_out.append({
            'request_number': request_number,
            'patient_name': patient_name,
            'patient_id': patient_id,
            'request_status': request_status,
            'request_date': request_date,
        })
    return rows_out


# =====================================================================
# STEP 2 — treatment plan per request (same session)
# =====================================================================
# !! BUG FIX (confirmed against a known-good reference script) !!
# The previous version used BeautifulSoup's textarea.get_text(strip=True),
# which strips each text node individually and joins them back with NO
# separator -- for a multi-line textarea that silently glues the end of
# one line directly onto the start of the next, and it kept the
# textarea's FIRST line, which is a fixed program name (e.g. "علاج مبادرة
# سرطان الثدى"), not part of the actual description at all. Both bugs
# together explain rows coming back with the number extracted fine but
# the description missing/mangled/prefixed with boilerplate. Switched to
# a plain regex over the raw HTML + an explicit line split, matching
# Extract_Decrees_Requests_Descriptions_Modified.py (already verified
# against this exact page) field-for-field: split on real newlines,
# drop the first line when there's more than one, keep the rest.
_TREATMENT_PLAN_RE = re.compile(
    r'<textarea[^>]*id="teatmentPlan"[^>]*>(.*?)</textarea>',  # SMC's own typo, kept as-is
    re.DOTALL | re.IGNORECASE,
)


def get_treatment_plan(session: smc.SMCSession, request_number: str):
    url = f"{BASE_URL}/smc/Requests/Details/{request_number}"
    try:
        resp = session.session.get(url, timeout=30)
        resp.encoding = "utf-8"
    except Exception as e:
        logging.error(f"Error fetching request details for {request_number}: {e}")
        return None
    if resp.status_code != 200:
        logging.warning(f"Request details for {request_number} returned HTTP {resp.status_code}.")
        return None

    match = _TREATMENT_PLAN_RE.search(resp.text)
    if not match:
        return None
    raw = html.unescape(match.group(1)).strip()
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return None
    if len(lines) == 1:
        return lines[0]
    # First line is the fixed program name; everything after it is the
    # actual description.
    return "\n".join(lines[1:])


def fetch_treatment_plans(session: smc.SMCSession, request_numbers: list) -> dict:
    plans = {}
    for i, request_number in enumerate(request_numbers, 1):
        plans[request_number] = get_treatment_plan(session, request_number)
        if i % 25 == 0:
            logging.info(f"Fetched treatment plan for {i}/{len(request_numbers)} requests...")
        time.sleep(DELAY_BETWEEN_REQUESTS)
    return plans


def fetch_known_treatment_plans(request_numbers: list) -> dict:
    """Looks up which of these request_numbers already have a non-null
    requested_decree_original_description saved from a PRIOR run, so
    the (now much wider, lookback-window) STEP 2 only has to hit
    /smc/Requests/Details/{n} for genuinely new requests -- that page's
    free-text treatment-plan field never changes once a request exists,
    only its status does (and status comes from the cheap STEP 1 list
    call, not this one). Without this, widening the query window to
    LOOKBACK_DAYS would re-fetch the same unchanged detail page for
    every still-open request on every single run, multiplying site
    load and run time for no benefit."""
    if not request_numbers:
        return {}
    known = {}
    chunk_size = 200  # keep the PostgREST 'in.(...)' filter URL a sane length
    for i in range(0, len(request_numbers), chunk_size):
        chunk = request_numbers[i:i + chunk_size]
        in_list = ",".join(chunk)
        rows = sb.fetch_all(
            SUPABASE_TABLE,
            "request_number,requested_decree_original_description",
            filters=f"request_number=in.({in_list})&requested_decree_original_description=not.is.null",
        )
        for r in rows:
            known[r["request_number"]] = r["requested_decree_original_description"]
    return known


# =====================================================================
# Output shaping
# =====================================================================
def build_output_rows(list_rows: list, plans: dict) -> list:
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
            # This request's OWN submission date (parsed from REQUESTDATE),
            # not the date the script ran -- see fetch_request_status_list.
            'request_date': row.get('request_date'),
            'patient_id': row.get('patient_id'),
            'requested_decree_original_description': plans.get(request_number),
            'request_status': row.get('request_status'),
        })
    return out


# =====================================================================
# STEP 4 (NEW) — admin-letter response text for recently-declined requests
# =====================================================================
def find_admin_letter_candidates(output_rows: list, today_iso: str, lookback_days: int) -> list:
    """Rows whose status is an admin-letter status AND whose own
    submission date (already correctly parsed per-row -- see
    build_output_rows) falls within the last `lookback_days` days of
    `today_iso`. Pure/no I/O, easy to unit-check independently of the
    network calls in fetch_admin_letters_for_window()."""
    cutoff = (datetime.strptime(today_iso, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    out = []
    for row in output_rows:
        if not letters.is_admin_letter_status(row.get("request_status")):
            continue
        req_date = row.get("request_date")
        if not req_date or not (cutoff <= req_date <= today_iso):
            continue
        out.append(row)
    return out


def fetch_admin_letters_for_window(session: smc.SMCSession, output_rows: list,
                                    today_iso: str, lookback_days: int) -> list:
    """For every candidate from find_admin_letter_candidates(), fetches
    the actual letter response text/committee date and shapes one
    Supabase row per request_number. Never raises on a single request's
    failure -- one bad fetch shouldn't drop every other admin letter
    found this run."""
    candidates = find_admin_letter_candidates(output_rows, today_iso, lookback_days)
    if not candidates:
        logging.info(f"No admin-letter status found within the last {lookback_days} day(s) — nothing to fetch.")
        return []
    logging.info(f"{len(candidates)} request(s) at an admin-letter status within the last {lookback_days} "
                 f"day(s) — fetching their response text...")

    out_rows = []
    for i, row in enumerate(candidates, 1):
        request_number = row["request_number"]
        try:
            letter_rows = letters.get_letters_for_request(session, request_number, delay=DELAY_BETWEEN_REQUESTS)
        except Exception as e:
            logging.error(f"[admin letters] {request_number}: fetch failed, skipping ({e})")
            continue
        latest = letter_rows[0] if letter_rows else {}
        out_rows.append({
            "request_number": request_number,
            "patient_id": row.get("patient_id"),
            "request_date": row.get("request_date"),
            "request_status": row.get("request_status"),
            "treatment_plan": row.get("requested_decree_original_description"),
            "committee_date": latest.get("committee_date"),
            "response_text": latest.get("response_text"),
            # Full history (rare: >1 letter round for the same request),
            # kept as-is for anyone who needs more than "the latest one".
            "all_letters": letter_rows,
            "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        if i % 10 == 0:
            logging.info(f"  ...{i}/{len(candidates)} admin letter(s) fetched")
    return out_rows


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
                         help="Run for this EXACT single date (YYYY-MM-DD) instead of a rolling window -- "
                              "for one-off backfills, which already loop day-by-day themselves. Disables "
                              "--lookback-days (window collapses to that one day).")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                         help="Normal runs (no --date): re-query every request submitted in the last N days "
                              "(default 15, or $LOOKBACK_DAYS), so open requests get their CURRENT status "
                              "refreshed daily instead of freezing at whatever it was on their creation day.")
    parser.add_argument("--admin-letter-lookback-days", type=int, default=DEFAULT_ADMIN_LETTER_LOOKBACK_DAYS,
                         help="For any request whose status is an admin letter (خطاب ادارى / خطاب إداري) and "
                              "whose submission date is within this many days of today (default 10, or "
                              "$ADMIN_LETTER_LOOKBACK_DAYS), also fetch and save the letter's own response "
                              "text -- ADDITIVE, never replaces the pending-requests info.")
    parser.add_argument("--skip-admin-letters", action="store_true",
                         help="Skip the admin-letter response-text fetch entirely (just the status sync).")
    parser.add_argument("--out-dir", default="./dry_run_output")
    args = parser.parse_args()

    end_date = args.date or cairo_today_iso()
    start_date = args.date or (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=args.lookback_days)).strftime("%Y-%m-%d")
    logging.info(f"Query window (Cairo): {start_date}..{end_date}" +
                 (" (explicit --date, single day)" if args.date else f" (lookback-days={args.lookback_days})"))

    if not smc.USERNAME_2 or not smc.PASSWORD_2:
        logging.error("SMC_USERNAME_2 / SMC_PASSWORD_2 are not set (env vars) — this script needs the second account.")
        sys.exit(1)

    session = smc.SMCSession(username=smc.USERNAME_2, password=smc.PASSWORD_2)
    if not session.login():
        logging.error("Login with secondary SMC account failed — aborting.")
        sys.exit(1)

    # ---- Step 1 ----
    list_rows = fetch_request_status_list(session, start_date, end_date)
    logging.info(f"{len(list_rows)} request row(s) fetched from SendRequestStatusJson for {start_date}..{end_date}.")

    if not list_rows:
        logging.warning("Nothing found for this window. Nothing to do.")
        return

    request_numbers = sorted({r['request_number'] for r in list_rows})

    # ---- Step 2 (same session) ----
    # Skip re-fetching the detail page for requests whose treatment-plan
    # text we already have from a previous run (see
    # fetch_known_treatment_plans docstring) -- only the STILL-NEW
    # request_numbers need the slow per-request GET.
    plans = {}
    if not args.dry_run:
        plans = fetch_known_treatment_plans(request_numbers)
    new_request_numbers = [r for r in request_numbers if r not in plans]
    logging.info(f"{len(plans)} request(s) already have a saved treatment-plan text; "
                 f"fetching detail pages for the other {len(new_request_numbers)}.")
    plans.update(fetch_treatment_plans(session, new_request_numbers))
    missing = [r for r, p in plans.items() if not p]
    if missing:
        logging.warning(f"{len(missing)} request(s) had no treatment-plan text found (kept as NULL): "
                         f"{missing[:10]}" + (" ..." if len(missing) > 10 else ""))

    # ---- Step 3 ----
    output_rows = build_output_rows(list_rows, plans)

    if args.dry_run:
        os.makedirs(args.out_dir, exist_ok=True)
        write_csv(os.path.join(args.out_dir, "request_status.csv"), output_rows)
        logging.info(f"DRY RUN complete — review the CSV in {args.out_dir} before running for real.")
    else:
        sb.upsert(SUPABASE_TABLE, output_rows, on_conflict=SUPABASE_CONFLICT_KEY)
        logging.info(f"Sync complete — {len(output_rows)} row(s) upserted into '{SUPABASE_TABLE}'.")

    # ---- Step 4 (NEW) — admin-letter response text, additive ----
    if not args.skip_admin_letters:
        admin_letter_rows = fetch_admin_letters_for_window(
            session, output_rows, end_date, args.admin_letter_lookback_days
        )
        if args.dry_run:
            write_csv(os.path.join(args.out_dir, "admin_letters.csv"),
                      [{k: v for k, v in r.items() if k != "all_letters"} for r in admin_letter_rows])
        elif admin_letter_rows:
            try:
                sb.upsert(ADMIN_LETTER_TABLE, admin_letter_rows, on_conflict="request_number")
                logging.info(f"Admin-letter sync complete — {len(admin_letter_rows)} row(s) upserted into "
                             f"'{ADMIN_LETTER_TABLE}'.")
            except Exception as e:
                # Step 3 (decree_request_status_daily_export) already
                # committed successfully above -- don't let a schema
                # problem on this newer, separate table (e.g. a column
                # like all_letters not yet migrated in Supabase) take
                # down the exit code / mask that the main status sync
                # worked. Logged loudly instead so it's never silently
                # zero admin letters with no explanation.
                logging.error(
                    f"[{ADMIN_LETTER_TABLE}] upsert FAILED ({e}) -- {len(admin_letter_rows)} admin-letter "
                    f"row(s) found this run were NOT saved. This is very likely a missing/uncached column "
                    f"on {ADMIN_LETTER_TABLE} in Supabase (check for PGRST204 in the message above) -- "
                    f"the main status sync above still completed fine, only this admin-letter piece failed."
                )


if __name__ == "__main__":
    main()
