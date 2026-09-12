"""
queue_value_left_scan.py — daily automated "value left" coverage scan.

Runs once a day (via GitHub Actions cron, separate from the on-demand
lookup workflow and from daily_sync.py's own renewal pipeline) OR
on-demand, triggered from the app via the trigger-daily-scan Edge
Function + decree_daily_scan_runs tracking table (see
sql/decree_daily_scan_runs_schema.sql) -- same pattern as
lookup_patient_decree_value.py's request_id/status flip, just for a
whole-queue batch instead of one patient. When --request-id is NOT
given (the cron path), every mark_run() call below is a no-op, so
cron behavior is completely unchanged from before this revision.

  1. Pull the queue report for TOMORROW (today + 1 day) and keep only
     the rows whose Clinic name is a daycare-style clinic -- see
     DAYCARE_CLINICS below. Reuses daily_sync.py's own
     fetch_and_parse_queue()/build_queue_rows() so the parsing logic
     never drifts from the existing renewal pipeline's.

  2. For every distinct patient found, run the exact same extraction
     used by the on-demand lookup (patient_decree_value.
     get_patient_decree_value_details) -- which now also classifies
     every decree as 'current' / 'superseded' / 'previous_cycle' /
     'unmapped' by treatment_plan_name (see that module's own
     docstring) -- and the exact same pending-unsubmitted join used
     there (lookup_patient_decree_value.fetch_pending_unsubmitted_value)
     -- so this script's numbers can never silently disagree with the
     module a user would check by hand.

  3. Per your Q3 answer, the question this scan answers is narrowly
     "will this patient be able to dispense ONE dose tomorrow" -- not
     a full-course projection. That check
     (patient_decree_value.evaluate_dose_coverage) is only ever run
     against a decree's 'current' regimen -- a superseded/previous-cycle
     decree is written to the table too (per your Q5: keep, don't drop,
     just label clearly) but never drives needs_attention on its own.

  4. Per your Q4 answer: for anything flagged needs_attention, also
     check decree_request_status_daily_export (already normalized by
     promote_request_status.py) for a matching pending request, AND
     surface what treatment plan that existing request is actually
     for and its current status -- so the output distinguishes
     "already requested Drug X, status: <status>" from "no request on
     file yet -> needs a new decree request raised."

     !! REVISION 2 (2026-09-12, see decree_requests_inspction_mannually.har) !!
     REVISION above assumed the site caps a patient at 2 concurrently
     OPEN requests. A manual HAR capture on a real long-treated patient
     disproved this: that patient had 4 simultaneously open requests
     (one 'تم التسجيل', three 'لجنة طبية') at once, with no sign of any
     submission block. There is NO confirmed cap -- fetch_patient_
     pending_requests() now returns EVERY currently-open request for the
     patient, not just the 2 most recent. A request only counts as
     "open" if its status is an EXACT match for one of the 8 known
     pending statuses (PENDING_REQUEST_STATUSES / _is_pending_status
     below) -- this used to be the inverse (everything not explicitly
     final counted as open), which let unrecognized/uncatalogued status
     text on old, actually-resolved requests leak through as false
     positives. fetch_patient_pending_requests()
     fetches every request row for the patient ONCE (not per-decree --
     open-request state is patient-wide, not decree-specific) and
     filters to the ones that are actually still open. The result is
     written both as the original flat scalar columns (most-recent open
     request, for anything still reading those) AND as the
     `pending_requests` jsonb array (now uncapped), so the app can show
     the true count without a wrong assumption truncating it.

     That same HAR capture also caught the actual root cause of a
     separate, more serious bug: this scan's live fetch was built
     against /smc/Reports/SendRequestStatusJson filtered by SsnNumber=
     <patient_id> -- an endpoint/filter combination that was never
     actually verified to work per-patient (request_status_sync.py's
     own use of it always sends SsnNumber='' and filters client-side
     afterward). For the captured patient it silently returned nothing
     at all, even though that patient genuinely had 4 open requests and
     a recent admin letter sitting right there on the live site --
     matching exactly the "returns none completely" symptom reported.
     fetch_patient_requests_live() below now uses
     POST /smc/Requests/GetRequests + nationalId=<patient_id> instead --
     the same endpoint the original
     Extract_All_Decree_Requests_And_Admin_Letters_Unified_Script.py's
     get_patient_requests_map() already used, confirmed correct against
     that HAR -- with full pagination (a single heavily-treated patient
     can have hundreds of historical requests across many pages; the
     captured example had 250 across 10).

  5. Upsert one row per (scan_date, patient_id, decree_number) into
     decree_value_left_daily_scan (decree_number is '' for a patient
     with zero decrees on file -- see the note on that sentinel value
     in the schema/previous version of this script).

  6. If --request-id was given (app-triggered run), flip that row in
     decree_daily_scan_runs to 'done' (with scan_date + row/flag
     counts) or 'error' (with a message) so the frontend's poll loop
     knows when to stop and what to show.

!! ASSUMPTIONS TO CONFIRM / ADJUST !!
  - DAYCARE_CLINICS: every clinic name in daily_sync.py's own
    ALLOWED_CLINICS that contains "day care" or "daycare". Edit the
    set below directly if that's not the exact list you mean.
  - has_pending_request matching: joined on (patient_id,
    decree_unique_name) against decree_request_status_daily_export,
    where decree_unique_name there comes from request_name_map (a
    SEPARATE mapping table, keyed on the request's own free-text
    treatment-plan field -- not decree_medication_catalog, which is
    keyed on the DECREE's raw description). These are two independently
    curated naming systems; a match only happens if both ended up
    using the same treatment_plan_name text. If you notice real
    pending requests not being detected here, the likely cause is a
    naming mismatch between the two maps rather than a bug in this
    join -- worth eyeballing once real data comes through.
  - A decree with regimen_status == 'unmapped' (raw description isn't
    in decree_medication_catalog at all yet) is written with
    needs_attention = True and a note, rather than silently skipped --
    an unrecognized decree should never disappear from the report.
"""

import os
import re
import sys
import csv
import time
import logging
import argparse
from typing import Optional
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))

import smc_session as smc
import supabase_client as sb
from daily_sync import fetch_and_parse_queue, build_queue_rows, ALLOWED_CLINICS
from hmis_id_resolver import HmisIdResolver
from patient_decree_value import get_patient_decree_value_details, evaluate_dose_coverage
from lookup_patient_decree_value import fetch_pending_unsubmitted_value
from decree_category import categorize_decree, get_category_map
from decree_name_map import normalize_request_text
from admin_letter_lookup import is_admin_letter_status, get_letters_for_request
import request_status_sync as rss
from cairo_date import cairo_today_iso

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# See "ASSUMPTIONS TO CONFIRM" above.
DAYCARE_CLINICS = {c for c in ALLOWED_CLINICS if 'day care' in c or 'daycare' in c}

BASE_URL = smc.BASE_URL

RESULTS_TABLE = "decree_value_left_daily_scan"
# !! NO LONGER READ FROM, ONLY WRITTEN TO (best-effort, write-through) !!
# These two used to be where fetch_patient_pending_requests() /
# fetch_patient_admin_letter_notices() got their data FROM -- rows
# populated by request_status_sync.py's own separate daily batch job,
# which only re-checks a rolling window each run. Reading them here
# meant this scan could show a request's status/treatment-plan, or an
# admin letter's existence, from whenever THAT OTHER job last happened
# to touch it -- not from right now. Per spec ("no more counting on any
# extracted decree requests data from any source -- I need it freshly
# extracted from the website"), this scan now fetches every request and
# every admin letter LIVE, on the spot, itself (see
# fetch_patient_requests_live() / fetch_patient_pending_requests() /
# fetch_patient_admin_letter_notices() below). These two table names are
# kept only as write-through targets, so decree-renewal.js / needs-review.js
# (which join against them separately) stay in sync too -- a write
# failure here is logged and swallowed, it never blocks or changes this
# scan's own (live-sourced) decision for the current patient.
REQUEST_STATUS_TABLE = "decree_request_status_daily_export"
ADMIN_LETTER_TABLE = "decree_admin_letter_details"
RUNS_TABLE = "decree_daily_scan_runs"
# Per spec: "any patient with an admin letter prior to the day/date/time
# of extraction by 10 days" -- default is 10, not 20. Still overridable
# via $ADMIN_LETTER_LOOKBACK_DAYS / --admin-letter-lookback-days for
# anyone who genuinely wants a wider window.
DEFAULT_ADMIN_LETTER_LOOKBACK_DAYS = int(os.environ.get("ADMIN_LETTER_LOOKBACK_DAYS", 10))

# How far back fetch_patient_requests_live() walks a patient's paginated
# request history before assuming "nothing pending further back either"
# and stopping early (see that function's own docstring for the
# early-stop mechanics and the assumption behind it). Separate from
# ADMIN_LETTER_LOOKBACK_DAYS above -- that one's about admin-letter
# RECENCY (how old a resulted letter can be and still be worth
# surfacing), this one's about how far back to even look for a pending
# request at all. Must stay >= ADMIN_LETTER_LOOKBACK_DAYS since
# fetch_patient_admin_letter_notices() reuses the same live_requests
# list -- default 30 comfortably covers the default 10-day admin-letter
# window.
DEFAULT_REQUEST_LOOKBACK_DAYS = int(os.environ.get("REQUEST_LOOKBACK_DAYS", 30))

DELAY_BETWEEN_PATIENTS = 0.5
# Between each live SendRequestStatusJson / Details / _PrintLetters GET
# made per-patient below -- same politeness delay request_status_sync.py
# uses for the same endpoints.
DELAY_BETWEEN_REQUESTS = 0.3

# !! BUG FIX (2026-09-12) !! This used to be a BLACKLIST
# (REQUEST_FINAL_STATUSES): "anything NOT one of these 4 exact final
# strings counts as still open." That was deliberately conservative,
# but it backfired in practice -- any status text that isn't an exact
# match for one of the 4 (a rarer/older status like "قرار ملغى
# للتعديل", or any other wording the site uses that was never
# catalogued here) silently falls through as "still pending", which is
# how years-old, long-resolved decree requests were leaking into the
# pending-requests output alongside genuinely open ones.
#
# Switched to a WHITELIST instead: a request only counts as pending if
# its status is an EXACT match for one of these 8 known pending
# statuses (your list). Anything else -- including any status not
# recognized at all -- is treated as resolved/not-pending, and logged
# once per distinct unrecognized value so a genuinely new status added
# by the site later is visible in the logs rather than silently
# mis-classified either way.
PENDING_REQUEST_STATUSES = {
    "تأجيل الطلب لإرفاق ملف الأشعة",
    "محول إلى طبيب آخر",
    "تم فحصه فى لجنة طبية",
    "توصية نهائية",
    "توصية مبدئية",
    "توصية مع عرض لجنة",
    "لجنة طبية",
    "تم التسجيل",
}

# Kept only for reference/back-compat with anything that might still
# import it -- no longer read by _is_pending_status (see above).
REQUEST_FINAL_STATUSES = {
    "قرار نهائى",
    "خطاب ادارى",
    "إلغاء بناءاً على طلب المريض أو مندوب المستشفي",
    "قرار ملغي",
}

# Tracks which unrecognized status strings we've already logged this
# run, so a common-but-uncatalogued status doesn't spam the log once
# per request -- just once per distinct value.
_UNRECOGNIZED_STATUSES_SEEN = set()

# !! REMOVED (2026-09-12) !! There is no confirmed per-patient cap on
# concurrently open requests -- see REVISION 2 in the module docstring
# above. A real patient was found with 4 open at once. Keeping a
# constant here (even a larger guessed number) would just be a new
# unverified assumption in place of the old wrong one, so
# fetch_patient_pending_requests() below no longer truncates at all.



def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# !! BUG FIX (2026-09-12) !! mark_run("done", ...) is main()'s LAST
# statement, called after every real result (the scan itself, the
# decree_value_left_daily_scan upsert) has already succeeded and been
# logged. Before this fix, a single transient Supabase 504 on THIS
# call alone (sb.patch() raises RuntimeError on any non-2xx response)
# propagated straight out of main() and killed the whole process with
# exit code 1 -- making GitHub Actions report a "failed" run, and
# leaving decree_daily_scan_runs stuck at whatever status it was
# already in (never flipped to "done"), even though every row of real
# data was already safely saved. 504s from PostgREST/Supabase are
# common under load (this same run logged 3 other transient 504s
# earlier, on write-throughs that already have their own try/except)
# and are usually gone on an immediate retry, so:
#   1. Retry the PATCH a few times with a short backoff before giving
#      up, same idea as the other Supabase calls in this file.
#   2. Never let a failure here raise out of mark_run() at all -- log
#      it and return, so a status-flip failure can never turn an
#      otherwise-successful run into a reported failure, and can never
#      mask whatever real error message was being reported.
_MARK_RUN_RETRIES = 3
_MARK_RUN_RETRY_DELAY = 5  # seconds, doubled each attempt


def mark_run(request_id, status, error_message=None, scan_date_iso=None,
             row_count=None, flagged_count=None):
    """
    Flips decree_daily_scan_runs.status for an app-triggered run.
    A no-op whenever request_id is falsy -- i.e. every cron/plain
    manual-dispatch run, which never passes --request-id and so never
    touches this table at all.

    !! NEVER RAISES !! (see BUG FIX note above) -- a transient
    Supabase error here must never crash the caller or turn an
    otherwise-successful scan into a reported failure. Retries a few
    times first; if every attempt fails, logs it clearly (including
    what status this run should have ended up at, for manual
    cleanup) and returns rather than propagating.
    """
    if not request_id:
        return
    body = {"status": status, "completed_at": _now_iso()}
    if error_message:
        body["error_message"] = error_message[:2000]
    if scan_date_iso is not None:
        body["scan_date"] = scan_date_iso
    if row_count is not None:
        body["row_count"] = row_count
    if flagged_count is not None:
        body["flagged_count"] = flagged_count

    delay = _MARK_RUN_RETRY_DELAY
    for attempt in range(1, _MARK_RUN_RETRIES + 1):
        try:
            sb.patch(RUNS_TABLE, f"id=eq.{request_id}", body)
            return
        except Exception as e:
            if attempt < _MARK_RUN_RETRIES:
                logging.warning(
                    f"[{RUNS_TABLE}] patch to '{status}' failed on attempt "
                    f"{attempt}/{_MARK_RUN_RETRIES} ({e}) -- retrying in {delay}s."
                )
                time.sleep(delay)
                delay *= 2
            else:
                logging.error(
                    f"[{RUNS_TABLE}] patch to '{status}' failed after "
                    f"{_MARK_RUN_RETRIES} attempts ({e}) -- request_id={request_id} "
                    f"is stuck at its previous status. The underlying scan data "
                    f"itself is unaffected (this only updates the tracking row); "
                    f"check decree_daily_scan_runs manually if the app's poll "
                    f"loop is now stuck waiting for '{status}'."
                )


def build_daycare_queue_rows(raw_records: list, appointment_date_iso: str,
                              resolver: HmisIdResolver = None) -> list:
    """Same shaping (and Medical No. -> national ID resolution) as
    daily_sync.build_queue_rows(), but filtered to DAYCARE_CLINICS
    instead of the full ALLOWED_CLINICS set."""
    rows = build_queue_rows(raw_records, appointment_date_iso, resolver=resolver)
    return [r for r in rows if r["clinic"].lower() in DAYCARE_CLINICS]


def _is_pending_status(status: Optional[str]) -> bool:
    """A request counts as OPEN only if its status is an EXACT match
    for one of PENDING_REQUEST_STATUSES (see the BUG FIX note above --
    this used to be the inverse: everything not explicitly final was
    treated as open, which let unusual/uncatalogued statuses on old
    requests leak through as false positives).

    None/blank status is NOT treated as pending anymore either --
    blank status has never actually meant "still active" in real data,
    it just means we don't know, and guessing "open" was part of the
    same over-inclusive assumption this fix removes.

    Any non-blank status that's neither in PENDING_REQUEST_STATUSES nor
    REQUEST_FINAL_STATUSES is logged once (not once per request) so an
    actually-new status the site starts using is visible in the logs
    instead of being silently mis-classified in either direction."""
    if not status:
        return False
    status = status.strip()
    if status in PENDING_REQUEST_STATUSES:
        return True
    if status not in REQUEST_FINAL_STATUSES and status not in _UNRECOGNIZED_STATUSES_SEEN:
        _UNRECOGNIZED_STATUSES_SEEN.add(status)
        logging.warning(
            f"[pending-status] unrecognized request status {status!r} -- "
            f"not in PENDING_REQUEST_STATUSES or REQUEST_FINAL_STATUSES, "
            f"treating as NOT pending. Add it to one of those two sets in "
            f"queue_value_left_scan.py if this is a real status the site uses."
        )
    return False


_PAGE_OF_RE = re.compile(r'Page\s+\d+\s+of\s+(\d+)')


def _parse_get_requests_page(html_text: str) -> tuple:
    """Parses one page of /smc/Requests/GetRequests's HTML response
    (table id="requestTable", columns: رقم الطلب | الرقم القومى للمريض |
    تاريخ الطلب | اسم المواطن | جهة الإرسال | جهة العلاج | مرحلة الطلب |
    عمليات -- confirmed against decree_requests_inspction_mannually.har).
    Returns (rows, total_pages). Each row is
    {request_number, request_status, request_date} -- request_date here
    is already a plain 'YYYY-MM-DD' string straight out of the page, no
    /Date(...)/ epoch-ms parsing needed (unlike SendRequestStatusJson).
    total_pages comes from the "Page X of Y" text in the #myPager div;
    defaults to 1 if that div isn't found (e.g. a patient with only one
    page of history has no pager at all)."""
    soup = BeautifulSoup(html_text, "html.parser")
    rows = []
    table = soup.find("table", id="requestTable")
    if table:
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 7:
                continue  # header row, or a stray row with no data
            link = cells[0].find("a")
            request_number = (link.get_text(strip=True) if link else cells[0].get_text(strip=True)).strip()
            if not request_number.isdigit():
                continue
            rows.append({
                "request_number": request_number,
                "request_status": cells[6].get_text(strip=True) or None,
                "request_date": cells[2].get_text(strip=True) or None,
            })
    total_pages = 1
    pager = soup.find(id="myPager")
    if pager:
        m = _PAGE_OF_RE.search(pager.get_text())
        if m:
            total_pages = int(m.group(1))
    return rows, total_pages


def fetch_patient_requests_live(session: smc.SMCSession, patient_id: str, today_iso: str = None,
                                 lookback_days: int = None) -> list:
    """
    !! THE fresh-extraction entry point !!
    !! ENDPOINT FIX (2026-09-12, see decree_requests_inspction_mannually.har) !!
    This used to POST /smc/Reports/SendRequestStatusJson filtered by
    SsnNumber=patient_id. A manual HAR capture on a real patient with 4
    genuinely open requests + a recent admin letter proved that call
    comes back completely empty for a per-patient SsnNumber filter --
    request_status_sync.py's own use of this same endpoint always sends
    SsnNumber='' and filters client-side afterward, so the per-SSN
    server-side filter was never actually verified to work at all. This
    was the root cause of "returns none completely" for a patient who
    plainly has live data.

    Switched to POST /smc/Requests/GetRequests + nationalId=patient_id --
    the exact endpoint
    Extract_All_Decree_Requests_And_Admin_Letters_Unified_Script.py's
    get_patient_requests_map() already used, confirmed correct against
    the same HAR (15 rows on page 1 alone for that patient, statuses and
    dates matching a manual check exactly). fromDate/toDate are still
    sent (the endpoint requires them) but pinned wide open on the
    request itself -- confirmed (same as DecreesSearch-by-decreeID in
    hospital_decrees_sync.py) that the site does NOT actually apply
    fromDate/toDate server-side once a specific nationalId is set (the
    captured request's own fromDate/toDate only spanned two days yet
    still returned requests dated back to May 2026), so those two
    values can't be used to make the SITE do less work -- only to make
    THIS function stop asking for more pages once it has enough.

    !! LOOKBACK WINDOW / EARLY STOP (2026-09-12, for speed) !!
    A pending-status request is only ever meaningful if it's recent --
    per spec, if nothing pending turns up within the last
    lookback_days (default DEFAULT_REQUEST_LOOKBACK_DAYS / 30, see
    $REQUEST_LOOKBACK_DAYS / --request-lookback-days), there's nothing
    pending at all, full stop -- so this no longer needs to walk a
    heavily-treated patient's entire history (the captured example had
    250 requests across 10 pages). The site returns each page already
    sorted newest-request-first (confirmed against the HAR), so this
    walks pages in order and stops -- WITHOUT fetching any further
    pages -- the instant it sees a row older than the cutoff, since
    every row after that point (rest of the current page, and every
    later page) is guaranteed to be even older. Rows with no parseable
    date are kept rather than used to decide the cutoff, same
    conservative-on-missing-data approach used elsewhere in this file.

    !! ASSUMPTION TO CONFIRM !! This assumes a request's status never
    sits at one of PENDING_REQUEST_STATUSES for longer than
    lookback_days after it was filed. If SMC ever has a real backlog
    where a request stays open past that window, this would miss it --
    widen --request-lookback-days (or $REQUEST_LOOKBACK_DAYS) if that
    turns out to happen in practice.

    Never raises -- returns whatever pages were read successfully so
    far on any HTTP/parse failure partway through, rather than losing
    everything already fetched for one bad page.
    """
    if today_iso is None:
        today_iso = cairo_today_iso()
    lookback_days = lookback_days if lookback_days is not None else DEFAULT_REQUEST_LOOKBACK_DAYS
    cutoff_iso = (datetime.strptime(today_iso, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    url = f"{BASE_URL}/smc/Requests/GetRequests"
    base_payload = {
        "requestId": "", "nationalId": patient_id, "REQUESTIMPORTANCEID": "",
        "TreatmentProcdId": "", "patientName": "",
        "fromDate": "01-01-2015", "toDate": datetime.strptime(today_iso, "%Y-%m-%d").strftime("%m-%d-%Y"),
        "recommendHosId": "", "statusId": "", "cancerCase": "false",
        "source": "", "page": "1",
    }
    out = []
    page = 1
    total_pages = None
    while True:
        payload = dict(base_payload, page=str(page))
        try:
            resp = session.session.post(
                url, data=payload, timeout=30,
                headers={"Referer": f"{BASE_URL}/smc/Requests"},
            )
        except Exception as e:
            logging.error(f"[live-requests] {patient_id}: GetRequests page {page} failed ({e})")
            break
        if resp.status_code != 200:
            logging.warning(f"[live-requests] {patient_id}: GetRequests page {page} HTTP {resp.status_code}")
            break
        rows, page_total_pages = _parse_get_requests_page(resp.text)
        if page == 1:
            total_pages = page_total_pages
        if not rows:
            break

        hit_cutoff = False
        for r in rows:
            rdate = r.get("request_date")
            if rdate and rdate < cutoff_iso:
                hit_cutoff = True
                break  # this row and everything after it (rest of this
                       # page, every later page) is older -- stop here
            out.append(r)
        if hit_cutoff:
            logging.info(
                f"[live-requests] {patient_id}: hit {lookback_days}-day cutoff "
                f"({cutoff_iso}) on page {page}/{total_pages} -- stopping early "
                f"({len(out)} row(s) kept)."
            )
            break

        if total_pages and page >= total_pages:
            break
        page += 1
        time.sleep(DELAY_BETWEEN_REQUESTS)
    return out


def fetch_patient_pending_requests(session: smc.SMCSession, patient_id: str,
                                    live_requests: list = None) -> list:
    """
    Returns EVERY currently-OPEN request for this patient, most recent
    first, each as a dict:
        {request_number, request_status, request_unique_name,
         request_original_description, request_date}
    or an empty list if the patient has none open right now.

    !! NO LONGER CAPPED AT 2 (2026-09-12) !! See REVISION 2 in the
    module docstring: a real patient was found with 4 simultaneously
    open requests, disproving the earlier "site caps at 2" assumption.
    There's no substitute cap here either -- guessing a new number would
    just be a new unverified assumption.

    !! FRESH EXTRACTION, NOT A CACHE READ !!
    This used to read decree_request_status_daily_export -- a table
    populated by request_status_sync.py's own separate daily batch job
    (which only re-checks a rolling --lookback-days window each run).
    That meant a request's status AND its treatment-plan text here
    could reflect whenever that OTHER job last happened to refresh it,
    not right now. Every field below instead comes from a live GET/POST
    made during THIS scan: status+date from `live_requests` (see
    fetch_patient_requests_live()), and the treatment-plan text from a
    fresh /smc/Requests/Details/{request_number} GET
    (request_status_sync.get_treatment_plan) for each open request
    found -- never from a previous run's saved value.
    """
    rows = live_requests if live_requests is not None else fetch_patient_requests_live(session, patient_id)
    open_rows = [r for r in rows if _is_pending_status(r.get("request_status"))]
    # Most recent request_date first; a missing date sorts last rather
    # than crashing the comparison.
    open_rows.sort(key=lambda r: r.get("request_date") or "", reverse=True)

    out = []
    for r in open_rows:
        request_number = r["request_number"]
        treatment_plan_text = rss.get_treatment_plan(session, request_number)
        time.sleep(DELAY_BETWEEN_REQUESTS)
        unique_name, _action = normalize_request_text((treatment_plan_text or "").strip())
        out.append({
            "request_number": request_number,
            "request_status": r.get("request_status"),
            "request_unique_name": unique_name,
            "request_original_description": treatment_plan_text,
            "request_date": r.get("request_date"),
        })
    return out


def fetch_patient_admin_letter_notices(session: smc.SMCSession, patient_id: str,
                                        admin_letter_lookback_days: int = None,
                                        live_requests: list = None) -> list:
    """
    ADDITIVE alongside fetch_patient_pending_requests() -- this never
    removes or replaces anything from `pending_requests`. It surfaces a
    SEPARATE signal: any request that recently came back as an
    administrative letter (خطاب ادارى / خطاب إداري -- usually a decline
    or redirect rather than a granted decree) is not one of
    queue_value_left_scan.PENDING_REQUEST_STATUSES, so it's already
    treated as a RESOLVED/closed request and so never appears in the
    pending list at all -- which is correct for "is a NEW request
    needed", but silently
    drops the fact that a request WAS just declined, which is exactly
    the context a reviewer wants right after seeing "no pending
    request" for a decree that still needs attention.

    !! FRESH EXTRACTION, NOT A CACHE READ !!
    This used to read decree_admin_letter_details -- a table populated
    by request_status_sync.py's own separate daily batch pass. Now every
    admin-letter-status request found in `live_requests` (see
    fetch_patient_requests_live(), reused here so this doesn't double
    the SendRequestStatusJson hit already made for
    fetch_patient_pending_requests()) has its actual letter fetched
    fresh, right now, via admin_letter_lookup.get_letters_for_request()
    -- a live GET of the request's Details page + its _PrintLetters
    popup, never a previously-saved value.

    Recency: "resulted within the last N days" is about when the
    committee actually DECIDED (committee_date), not when the request
    was first filed -- so recency compares committee_date against the
    cutoff, falling back to request_date ONLY for the rare letter where
    a committee_date couldn't be parsed at all (better to show a
    possibly-slightly-stale notice than to silently drop it). The
    cutoff itself is anchored to Cairo "now" -- the moment THIS scan is
    running (extraction time) -- not to the patient's appointment date.
    Default window is 10 days (DEFAULT_ADMIN_LETTER_LOOKBACK_DAYS /
    $ADMIN_LETTER_LOOKBACK_DAYS / --admin-letter-lookback-days).
    """
    lookback_days = admin_letter_lookback_days if admin_letter_lookback_days is not None else DEFAULT_ADMIN_LETTER_LOOKBACK_DAYS
    rows = live_requests if live_requests is not None else fetch_patient_requests_live(session, patient_id)
    candidates = [r for r in rows if is_admin_letter_status(r.get("request_status"))]
    if not candidates:
        return []

    today_iso = cairo_today_iso()
    cutoff_iso = (datetime.strptime(today_iso, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    notices = []
    for r in candidates:
        request_number = r["request_number"]
        try:
            letter_rows = get_letters_for_request(session, request_number, delay=DELAY_BETWEEN_REQUESTS)
        except Exception as e:
            logging.error(f"[live-admin-letters] {request_number}: fetch failed, skipping ({e})")
            continue
        latest = letter_rows[0] if letter_rows else {}
        recency_date = latest.get("committee_date") or r.get("request_date")
        if not recency_date or not (cutoff_iso <= recency_date <= today_iso):
            continue  # outside the lookback window (or no usable date at all) -- drop it, don't guess
        treatment_plan_text = rss.get_treatment_plan(session, request_number)
        time.sleep(DELAY_BETWEEN_REQUESTS)
        notices.append({
            "request_number": request_number,
            "request_date": r.get("request_date"),
            "request_status": r.get("request_status"),
            "treatment_plan": treatment_plan_text,
            "committee_date": latest.get("committee_date"),
            "response_text": latest.get("response_text"),
        })
    notices.sort(key=lambda n: n.get("committee_date") or n.get("request_date") or "", reverse=True)
    return notices


def _write_through_request_status(rows: list, patient_id: str) -> None:
    """Best-effort ONLY -- keeps decree_request_status_daily_export in
    sync with what THIS scan just found live, for any other consumer
    (decree-renewal.js etc.) that still reads that table directly. Never
    raises and never affects this scan's own (already live-sourced)
    decision for the current patient -- a failure here is logged and
    swallowed."""
    if not rows:
        return
    try:
        sb.upsert(
            REQUEST_STATUS_TABLE,
            [{
                "request_number": r["request_number"],
                "patient_id": patient_id,
                "request_status": r.get("request_status"),
                "request_date": r.get("request_date"),
                "requested_decree_original_description": r.get("request_original_description"),
                "decree_unique_name": r.get("request_unique_name"),
            } for r in rows],
            on_conflict="request_number",
        )
    except Exception as e:
        logging.error(f"[write-through] {REQUEST_STATUS_TABLE} upsert failed (non-fatal): {e}")


def _write_through_admin_letters(rows: list, patient_id: str) -> None:
    """Same best-effort write-through as _write_through_request_status(),
    for decree_admin_letter_details."""
    if not rows:
        return
    try:
        sb.upsert(
            ADMIN_LETTER_TABLE,
            [{
                "request_number": r["request_number"],
                "patient_id": patient_id,
                "request_date": r.get("request_date"),
                "request_status": r.get("request_status"),
                "treatment_plan": r.get("treatment_plan"),
                "committee_date": r.get("committee_date"),
                "response_text": r.get("response_text"),
                "updated_at": _now_iso(),
            } for r in rows],
            on_conflict="request_number",
        )
    except Exception as e:
        logging.error(f"[write-through] {ADMIN_LETTER_TABLE} upsert failed (non-fatal): {e}")


def scan_patient(patient_id: str, scan_date_iso: str, session: smc.SMCSession,
                  pending_categories: set, clinic: str = None,
                  admin_letter_lookback_days: int = None,
                  request_lookback_days: int = None) -> list:
    """Returns the decree_value_left_daily_scan row(s) for one patient.
    `pending_categories` accumulates every raw decree_description this
    run that came back 'pending' from categorize_decree() (needs a
    human to assign it a category) -- one shared set across the whole
    run, pushed to decree_category_map once at the end of main() (see
    decree_category.py), same push-once-per-run pattern as
    decree_name_map.py's NameMap.push_pending()."""
    decrees = get_patient_decree_value_details(session, patient_id)

    # Fetched ONCE per patient regardless of how many decrees they
    # have or whether any of them need attention -- the open-request
    # cap (and the fact that a request exists at all) is patient-wide
    # info the app wants to show even for a patient whose decrees all
    # look fine right now.
    #
    # Both fetch_patient_pending_requests() and
    # fetch_patient_admin_letter_notices() need `session` now (they
    # each do their own live SMC calls) and both are able to reuse a
    # single SendRequestStatusJson hit instead of each doing their
    # own -- so that one shared live pull happens HERE, once, and is
    # handed to both. Skipping this and calling either function with
    # no `live_requests` would silently double the live hit per
    # patient (still correct, just twice the load on the site).
    # Deliberately NOT passing scan_date_iso (the QUEUE/appointment date,
    # which is tomorrow -- see main()'s date-offset default) as
    # `today_iso` here. fetch_patient_requests_live()'s `today_iso` is
    # the EndDate cutoff for "every request up to right now", and the
    # admin-letter lookback window is explicitly anchored to real
    # extraction-time "now" (see fetch_patient_admin_letter_notices'
    # own docstring), not to the patient's future appointment date.
    # Leaving this unset lets it default to cairo_today_iso() -- actual
    # wall-clock today in Cairo, at the moment this scan runs.
    #
    # !! (2026-09-12) !! The one shared pull now stops early past
    # request_lookback_days (see fetch_patient_requests_live's own
    # docstring) -- so it must use whichever window is WIDER of the
    # two callers' needs, or a longer --admin-letter-lookback-days
    # override could ask fetch_patient_admin_letter_notices() to look
    # for letters further back than this shared pull actually fetched,
    # silently truncating it instead of widening it.
    effective_admin_letter_lookback = (
        admin_letter_lookback_days if admin_letter_lookback_days is not None else DEFAULT_ADMIN_LETTER_LOOKBACK_DAYS
    )
    effective_request_lookback = (
        request_lookback_days if request_lookback_days is not None else DEFAULT_REQUEST_LOOKBACK_DAYS
    )
    live_requests = fetch_patient_requests_live(
        session, patient_id,
        lookback_days=max(effective_request_lookback, effective_admin_letter_lookback),
    )

    patient_pending = fetch_patient_pending_requests(session, patient_id, live_requests=live_requests)
    has_any_pending = len(patient_pending) > 0
    # Kept for anything still reading the old flat scalar columns --
    # the most recent open request, or all-None if there isn't one.
    first_pending = patient_pending[0] if patient_pending else {}

    # NEW, purely additive (see fetch_patient_admin_letter_notices
    # docstring): recent admin-letter responses for this patient --
    # shown ALONGSIDE pending_requests, never instead of it, as the
    # last piece of context ("here's what happened last time").
    admin_letter_notices = fetch_patient_admin_letter_notices(
        session, patient_id, admin_letter_lookback_days=admin_letter_lookback_days, live_requests=live_requests
    )

    # Best-effort write-through so decree-renewal.js / needs-review.js
    # (which still read decree_request_status_daily_export /
    # decree_admin_letter_details directly) see today's freshly-scraped
    # data too, instead of going stale forever now that THIS scan no
    # longer reads those tables itself. Never affects the row(s)
    # returned below -- a write-through failure is logged and
    # swallowed inside each helper, never raised here.
    _write_through_request_status(patient_pending, patient_id)
    _write_through_admin_letters(admin_letter_notices, patient_id)

    pending_scalar_fields = {
        "has_pending_request": has_any_pending,
        "pending_request_number": first_pending.get("request_number"),
        "pending_request_status": first_pending.get("request_status"),
        "pending_request_treatment_plan": first_pending.get("request_unique_name"),
        "pending_request_original_description": first_pending.get("request_original_description"),
        # Full list of every currently open request for this patient
        # (no longer capped at 2 -- see REVISION 2 in the module
        # docstring), so the app can show all of them instead of
        # guessing which one is "the" pending request. Same list on
        # every row for this patient -- it's patient-level info, not
        # per-decree.
        "pending_requests": patient_pending,
        "admin_letter_notices": admin_letter_notices,
    }

    if not decrees:
        return [{
            "scan_date": scan_date_iso,
            "patient_id": patient_id,
            "clinic": clinic,
            # '' not None: Postgres treats every NULL as distinct for
            # uniqueness purposes, so an ON CONFLICT upsert would never
            # match a prior "no decree" row for this patient and would
            # just pile up duplicate rows on every re-run. '' is a
            # real, stable value the unique constraint can match on.
            "decree_number": "",
            "decree_description": None,
            "treatment_plan_name": None,
            "reception_display_name": None,
            "regimen_status": None,
            "decree_total_value": None,
            "decree_value_left_website": None,
            "decree_status": None,
            "issuing_date": None,
            "decree_expiry_date": None,
            "pending_unsubmitted_value": 0,
            "real_value_left": None,
            "next_dose_covered": None,
            "category": None,
            "needs_attention": True,  # no decree at all -> always needs attention
            **pending_scalar_fields,
        }]

    out_rows = []
    for d in decrees:
        pending_value = fetch_pending_unsubmitted_value(d["decree_number"])
        website_left = d.get("decree_value_left_website")
        real_left = (website_left - pending_value) if website_left is not None else None

        is_current = d.get("regimen_status") == "current"
        dose_covered = evaluate_dose_coverage(d, real_left) if is_current else None

        # Only a CURRENT decree drives needs_attention -- a
        # superseded/previous-cycle row is history, not something
        # tomorrow's dispensing decision hinges on. An 'unmapped'
        # decree (not yet in decree_medication_catalog) is flagged so
        # it doesn't silently vanish from the report, per the note
        # above.
        if d.get("regimen_status") == "unmapped":
            needs_attention = True
        elif is_current:
            needs_attention = dose_covered is not True  # False or None (unknown) both need a human look
        else:
            needs_attention = False

        category, category_action = categorize_decree(d.get("decree_description"))
        if category_action == "pending" and d.get("decree_description"):
            pending_categories.add(d["decree_description"])

        out_rows.append({
            "scan_date": scan_date_iso,
            "patient_id": patient_id,
            "clinic": clinic,
            "decree_number": d["decree_number"],
            "decree_description": d.get("decree_description"),
            "treatment_plan_name": d.get("treatment_plan_name"),
            "reception_display_name": d.get("reception_display_name"),
            "regimen_status": d.get("regimen_status"),
            "category": category,
            "decree_total_value": d.get("decree_total_value"),
            "decree_value_left_website": website_left,
            "decree_status": d.get("decree_status"),
            "issuing_date": d.get("issuing_date"),
            "decree_expiry_date": d.get("decree_expiry_date"),
            "pending_unsubmitted_value": pending_value,
            "real_value_left": real_left,
            "next_dose_covered": dose_covered,
            "needs_attention": needs_attention,
            **pending_scalar_fields,
        })
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--date-offset-days", type=int, default=int(os.environ.get("DATE_OFFSET_DAYS", 1)),
                         help="Scan the queue for TODAY + this many days (default 1 = tomorrow).")
    parser.add_argument("--date", default=None,
                         help="Scan this EXACT date instead of an offset from today (YYYY-MM-DD).")
    parser.add_argument("--out-dir", default="./dry_run_output")
    parser.add_argument("--request-id", default=None,
                         help="decree_daily_scan_runs.id (uuid) -- only set when triggered from the app. "
                              "Omit for cron/plain manual runs; every tracking write becomes a no-op.")
    parser.add_argument("--admin-letter-lookback-days", type=int, default=DEFAULT_ADMIN_LETTER_LOOKBACK_DAYS,
                         help="Show an admin-letter notice for any letter whose committee_date (falling back "
                              "to request_date if unparsed) is within this many days of today (default 10, "
                              "or $ADMIN_LETTER_LOOKBACK_DAYS).")
    parser.add_argument("--request-lookback-days", type=int, default=DEFAULT_REQUEST_LOOKBACK_DAYS,
                         help="Stop walking a patient's paginated request history once every request found "
                              "so far is older than this many days (default 30, or $REQUEST_LOOKBACK_DAYS) -- "
                              "a pending-status request outside this window is assumed not to exist, for "
                              "speed on long-treated patients with hundreds of historical requests.")
    args = parser.parse_args()
    request_id = args.request_id

    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        target_date = datetime.now() + timedelta(days=args.date_offset_days)
    target_ddmmyyyy = target_date.strftime("%d-%m-%Y")
    target_iso = target_date.strftime("%Y-%m-%d")
    logging.info(f"Scan date: {target_iso}")
    logging.info(f"Daycare clinic filter: {sorted(DAYCARE_CLINICS)}")

    try:
        raw_records = fetch_and_parse_queue(target_ddmmyyyy)
        hmis_resolver = HmisIdResolver()
        queue_rows = build_daycare_queue_rows(raw_records, target_iso, resolver=hmis_resolver)
    except Exception as e:
        mark_run(request_id, "error", f"Queue fetch failed: {e}", scan_date_iso=target_iso)
        logging.error(f"Queue fetch failed: {e}")
        sys.exit(1)

    patient_ids = sorted({r["national_id"] for r in queue_rows})
    # First clinic seen per patient -- carried into every scan row so the
    # app's clinic column and clinic sort actually have data (this field
    # was previously never written at all).
    clinic_by_patient = {}
    for r in queue_rows:
        clinic_by_patient.setdefault(r["national_id"], r["clinic"])
    logging.info(f"{len(queue_rows)} daycare queue row(s) -> {len(patient_ids)} distinct patient(s).")

    if not patient_ids:
        logging.warning("No daycare-queue patients found for this date. Nothing to do.")
        mark_run(request_id, "done", scan_date_iso=target_iso, row_count=0, flagged_count=0)
        return

    session = smc.SMCSession()
    if not session.login():
        mark_run(request_id, "error", "SMC login failed.", scan_date_iso=target_iso)
        logging.error("SMC login failed — aborting.")
        sys.exit(1)

    all_rows = []
    pending_categories = set()
    for idx, pid in enumerate(patient_ids, 1):
        logging.info(f"[{idx}/{len(patient_ids)}] scanning patient {pid}...")
        try:
            all_rows.extend(scan_patient(pid, target_iso, session, pending_categories,
                                         clinic=clinic_by_patient.get(pid),
                                         admin_letter_lookback_days=args.admin_letter_lookback_days,
                                         request_lookback_days=args.request_lookback_days))
        except Exception as e:
            logging.error(f"Failed to scan patient {pid}: {e}")
        time.sleep(DELAY_BETWEEN_PATIENTS)

    def push_pending_categories():
        # Deliberately runs only AFTER the scan results are safely saved,
        # and never raises: a failure here (e.g. the decree_category_map
        # table missing from Supabase) previously crashed the run after
        # all patients were scraped but BEFORE anything was written --
        # destroying ~10 minutes of work. Now it's logged and swallowed.
        if not pending_categories:
            return
        try:
            get_category_map().push_pending(pending_categories)
        except Exception as e:
            logging.error(f"[decree_category_map] push_pending failed (non-fatal): {e}")

    flagged = [r for r in all_rows if r["needs_attention"]]
    still_needs_request = [r for r in flagged if not r["has_pending_request"]]
    logging.info(
        f"{len(all_rows)} row(s) scanned, {len(flagged)} flagged as needing attention, "
        f"{len(still_needs_request)} of those have NO pending request yet (true gap)."
    )

    if args.dry_run:
        os.makedirs(args.out_dir, exist_ok=True)
        write_csv(os.path.join(args.out_dir, "value_left_daily_scan.csv"), all_rows)
        logging.info(f"DRY RUN complete — review the CSV in {args.out_dir} before running for real.")
        push_pending_categories()
        mark_run(request_id, "done", scan_date_iso=target_iso, row_count=len(all_rows), flagged_count=len(flagged))
    else:
        try:
            sb.upsert(RESULTS_TABLE, all_rows, on_conflict="scan_date,patient_id,decree_number")
        except Exception as e:
            # !! SAFETY NET !!
            # A single missing/uncached column here (Postgres error
            # PGRST204 -- "Could not find the 'X' column ... in the
            # schema cache") used to fail this ONE batch upsert for
            # EVERY patient scanned today, silently freezing the whole
            # decree_value_left_daily_scan table at whatever the last
            # successful run wrote -- no visible error in the app, just
            # stale data that looked like a sync bug. If that's what
            # just happened, retry once with any field(s) not present in
            # the table's schema cache stripped out, so today's real
            # data (pending requests, dose coverage, etc.) still lands
            # even if one newer optional column (e.g. admin_letter_notices)
            # hasn't been migrated in Supabase yet. This is a fallback,
            # not a substitute for actually running the migration --
            # the missing column's data just won't be saved until you do.
            msg = str(e)
            if "PGRST204" in msg or "schema cache" in msg:
                import re as _re
                missing_cols = set(_re.findall(r"'([a-zA-Z_][a-zA-Z0-9_]*)' column", msg))
                if missing_cols:
                    logging.error(
                        f"[{RESULTS_TABLE}] upsert failed because column(s) {sorted(missing_cols)} "
                        f"don't exist yet in Supabase (see error below) -- retrying WITHOUT them so "
                        f"today's run isn't a total loss. Run the matching ALTER TABLE / "
                        f"NOTIFY pgrst, 'reload schema' in Supabase to stop needing this fallback: {msg}"
                    )
                    stripped_rows = [
                        {k: v for k, v in row.items() if k not in missing_cols}
                        for row in all_rows
                    ]
                    try:
                        sb.upsert(RESULTS_TABLE, stripped_rows, on_conflict="scan_date,patient_id,decree_number")
                        logging.warning(
                            f"[{RESULTS_TABLE}] retry succeeded WITHOUT {sorted(missing_cols)} -- "
                            f"today's other data is saved, but that field is missing for every "
                            f"row until the column is added."
                        )
                        mark_run(request_id, "done", scan_date_iso=target_iso,
                                  row_count=len(all_rows), flagged_count=len(flagged))
                        push_pending_categories()
                        return
                    except Exception as e2:
                        mark_run(request_id, "error", f"Failed to save results (retry also failed): {e2}",
                                  scan_date_iso=target_iso)
                        logging.error(f"Retry without {sorted(missing_cols)} also failed: {e2}")
                        sys.exit(1)
            mark_run(request_id, "error", f"Failed to save results: {e}", scan_date_iso=target_iso)
            logging.error(f"Failed to save results: {e}")
            sys.exit(1)
        logging.info(f"Sync complete — {len(all_rows)} row(s) upserted into '{RESULTS_TABLE}'.")
        push_pending_categories()
        mark_run(request_id, "done", scan_date_iso=target_iso, row_count=len(all_rows), flagged_count=len(flagged))


if __name__ == "__main__":
    main()
