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

     REVISION: the site itself caps a patient at 2 concurrently OPEN
     requests, and a request only counts as "open" while its status
     is one of the known non-final ones (see REQUEST_FINAL_STATUSES /
     _is_pending_status below) -- everything else (final decision,
     administrative letter, cancelled, ...) is resolved and no longer
     blocks a new request. The previous version fetched with NO status
     filter at all and kept only the first row Postgres happened to
     return, so (a) a long-closed request could get shown as "the"
     pending one instead of a genuinely open one, and (b) a patient
     with two simultaneously open requests only ever surfaced one of
     them. fetch_patient_pending_requests() now fetches every request
     row for the patient ONCE (not per-decree -- the 2-request cap is
     patient-wide, not decree-specific), filters to the ones that are
     actually still open, and keeps at most the 2 most recent --
     matching the site's own limit exactly. The result is written both
     as the original flat scalar columns (first/most-recent open
     request, for anything still reading those) AND as a new
     `pending_requests` jsonb array with up to 2 entries, so the app
     can show both without guessing which one "the" request is.

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
import sys
import csv
import time
import logging
import argparse
from typing import Optional
from datetime import datetime, timedelta, timezone

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

DELAY_BETWEEN_PATIENTS = 0.5
# Between each live SendRequestStatusJson / Details / _PrintLetters GET
# made per-patient below -- same politeness delay request_status_sync.py
# uses for the same endpoints.
DELAY_BETWEEN_REQUESTS = 0.3

# The 4 statuses that mean a request is RESOLVED (final decision either
# way, cancelled, or converted to an administrative letter) -- exact
# text as it comes back from the site. Everything else -- including any
# status text not in this list, e.g. a new one the site adds later --
# is treated as still OPEN (see _is_pending_status). That's
# deliberately the conservative direction: an unrecognized status
# should never silently hide a request that might still be active.
REQUEST_FINAL_STATUSES = {
    "قرار نهائى",
    "خطاب ادارى",
    "إلغاء بناءاً على طلب المريض أو مندوب المستشفي",
    "قرار ملغي",
}

# The maximum number of concurrently open requests the site itself
# allows per patient -- once a patient has this many open requests,
# no new one can be submitted for them until one resolves.
MAX_OPEN_REQUESTS_PER_PATIENT = 2


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mark_run(request_id, status, error_message=None, scan_date_iso=None,
             row_count=None, flagged_count=None):
    """
    Flips decree_daily_scan_runs.status for an app-triggered run.
    A no-op whenever request_id is falsy -- i.e. every cron/plain
    manual-dispatch run, which never passes --request-id and so never
    touches this table at all.
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
    sb.patch(RUNS_TABLE, f"id=eq.{request_id}", body)


def build_daycare_queue_rows(raw_records: list, appointment_date_iso: str,
                              resolver: HmisIdResolver = None) -> list:
    """Same shaping (and Medical No. -> national ID resolution) as
    daily_sync.build_queue_rows(), but filtered to DAYCARE_CLINICS
    instead of the full ALLOWED_CLINICS set."""
    rows = build_queue_rows(raw_records, appointment_date_iso, resolver=resolver)
    return [r for r in rows if r["clinic"].lower() in DAYCARE_CLINICS]


def _is_pending_status(status: Optional[str]) -> bool:
    """A request counts as still OPEN unless its status is one of the
    known final ones. None/blank status (shouldn't normally happen,
    but data can be messy) is also treated as open -- conservative on
    purpose, see REQUEST_FINAL_STATUSES above."""
    if not status:
        return True
    return status.strip() not in REQUEST_FINAL_STATUSES


def fetch_patient_requests_live(session: smc.SMCSession, patient_id: str, today_iso: str = None) -> list:
    """
    !! THE fresh-extraction entry point !!
    A single live hit against /smc/Reports/SendRequestStatusJson,
    filtered by SsnNumber=patient_id (StartDate pinned far in the past,
    "2015-01-01", so no request of any age is missed) -- every request
    this patient has EVER had, with its CURRENT status and submission
    date, straight off the live site, right now. No Supabase table is
    read anywhere in this function. Callers (fetch_patient_pending_requests /
    fetch_patient_admin_letter_notices) both consume THIS SAME result
    (pass it in as `live_requests` to avoid a redundant second hit per
    patient) and filter it down for their own purpose. Never raises --
    returns [] on any HTTP/parse failure so one patient's site hiccup
    can't take down the whole scan.
    """
    today_iso = today_iso or cairo_today_iso()
    url = f"{BASE_URL}/smc/Reports/SendRequestStatusJson"
    payload = {
        'CitizenName': '',
        'StartDate': rss._smc_datetime_str("2015-01-01"),
        'EndDate': rss._smc_datetime_str(today_iso, end_of_day=True),
        'SsnNumber': patient_id,
        'RequestNumber': '',
        'RequestStatusId': '',
        'SystemUserId': '',
    }
    try:
        resp = session.session.post(url, data=payload, timeout=30)
    except Exception as e:
        logging.error(f"[live-requests] {patient_id}: SendRequestStatusJson failed ({e})")
        return []
    if resp.status_code != 200:
        logging.warning(f"[live-requests] {patient_id}: SendRequestStatusJson HTTP {resp.status_code}")
        return []
    try:
        raw = resp.json()
    except ValueError:
        raw = resp.text

    out = []
    for rec in rss._parse_send_request_status_json(raw):
        request_number = rss._clean_id(rec.get('REQUESTID'))
        if not request_number:
            continue
        out.append({
            "request_number": request_number,
            "request_status": (rec.get('STATUSARABICNAME') or '').strip() or None,
            "request_date": rss._parse_dotnet_date(rec.get('REQUESTDATE'), fallback_iso=None),
        })
    return out


def fetch_patient_pending_requests(session: smc.SMCSession, patient_id: str,
                                    live_requests: list = None) -> list:
    """
    Returns up to MAX_OPEN_REQUESTS_PER_PATIENT (2) currently-OPEN
    requests for this patient, most recent first, each as a dict:
        {request_number, request_status, request_unique_name,
         request_original_description, request_date}
    or an empty list if the patient has none open right now.

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
    (request_status_sync.get_treatment_plan) for each of the (at most 2)
    open requests found -- never from a previous run's saved value.
    """
    rows = live_requests if live_requests is not None else fetch_patient_requests_live(session, patient_id)
    open_rows = [r for r in rows if _is_pending_status(r.get("request_status"))]
    # Most recent request_date first; a missing date sorts last rather
    # than crashing the comparison.
    open_rows.sort(key=lambda r: r.get("request_date") or "", reverse=True)
    open_rows = open_rows[:MAX_OPEN_REQUESTS_PER_PATIENT]

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
    or redirect rather than a granted decree) is, per
    queue_value_left_scan.REQUEST_FINAL_STATUSES, already treated as a
    RESOLVED/closed request and so never appears in the pending list at
    all -- which is correct for "is a NEW request needed", but silently
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
                  admin_letter_lookback_days: int = None) -> list:
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
    live_requests = fetch_patient_requests_live(session, patient_id)

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
        # New: the full (up to 2) list of currently open requests for
        # this patient, so the app can show BOTH instead of guessing
        # which one is "the" pending request. Same list on every row
        # for this patient -- it's patient-level info, not per-decree.
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
                              "to request_date if unparsed) is within this many days of today (default 20, "
                              "or $ADMIN_LETTER_LOOKBACK_DAYS).")
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
                                         admin_letter_lookback_days=args.admin_letter_lookback_days))
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
