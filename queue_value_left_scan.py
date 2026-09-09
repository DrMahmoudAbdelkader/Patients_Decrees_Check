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
from admin_letter_lookup import is_admin_letter_status
from cairo_date import cairo_today_iso

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# See "ASSUMPTIONS TO CONFIRM" above.
DAYCARE_CLINICS = {c for c in ALLOWED_CLINICS if 'day care' in c or 'daycare' in c}

RESULTS_TABLE = "decree_value_left_daily_scan"
REQUEST_STATUS_TABLE = "decree_request_status_daily_export"
RUNS_TABLE = "decree_daily_scan_runs"
# NEW — written by request_status_sync.py's Step 4 (see that script for
# the fetch side). Read-only here: this scan only surfaces what's
# already there, filtered again by recency at READ time (not just at
# write time) so a notice naturally disappears once it ages out, with
# no separate cleanup/delete job needed.
ADMIN_LETTER_TABLE = "decree_admin_letter_details"
DEFAULT_ADMIN_LETTER_LOOKBACK_DAYS = int(os.environ.get("ADMIN_LETTER_LOOKBACK_DAYS", 10))

DELAY_BETWEEN_PATIENTS = 0.5

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


def fetch_patient_pending_requests(patient_id: str) -> list:
    """
    Returns up to MAX_OPEN_REQUESTS_PER_PATIENT (2) currently-OPEN
    requests for this patient, most recent first, each as a dict:
        {request_number, request_status, request_unique_name,
         request_original_description, request_date}
    or an empty list if the patient has none open right now.

    Fetched ONCE per patient (not per-decree, unlike the old
    fetch_pending_request_for_patient) since the open-request cap is
    patient-wide, not tied to any one decree -- see the naming-mismatch
    caveat in this script's top docstring for why a specific decree
    can't always be matched to a specific request by name alone.
    """
    rows = sb.fetch_all(
        REQUEST_STATUS_TABLE,
        "request_number,request_status,decree_unique_name,requested_decree_original_description,request_date",
        filters=f"patient_id=eq.{patient_id}",
    )
    open_rows = [r for r in rows if _is_pending_status(r.get("request_status"))]
    # Most recent request_date first; a missing date sorts last rather
    # than crashing the comparison.
    open_rows.sort(key=lambda r: r.get("request_date") or "", reverse=True)
    open_rows = open_rows[:MAX_OPEN_REQUESTS_PER_PATIENT]
    return [
        {
            "request_number": r.get("request_number"),
            "request_status": r.get("request_status"),
            "request_unique_name": r.get("decree_unique_name"),
            "request_original_description": r.get("requested_decree_original_description"),
            "request_date": r.get("request_date"),
        }
        for r in open_rows
    ]


def fetch_patient_admin_letter_notices(patient_id: str, admin_letter_lookback_days: int = None) -> list:
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

    Recency is re-checked HERE (not just at write time in
    request_status_sync.py) against admin_letter_lookback_days (default
    10, or $ADMIN_LETTER_LOOKBACK_DAYS) so a notice naturally stops
    showing once it's old news, without a separate cleanup job.
    """
    lookback_days = admin_letter_lookback_days if admin_letter_lookback_days is not None else DEFAULT_ADMIN_LETTER_LOOKBACK_DAYS
    rows = sb.fetch_all(
        ADMIN_LETTER_TABLE,
        "request_number,request_date,request_status,treatment_plan,committee_date,response_text",
        filters=f"patient_id=eq.{patient_id}",
    )
    if not rows:
        return []
    today_iso = cairo_today_iso()
    cutoff_iso = (datetime.strptime(today_iso, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    recent = [r for r in rows if r.get("request_date") and cutoff_iso <= r["request_date"] <= today_iso]
    recent.sort(key=lambda r: r.get("request_date") or "", reverse=True)
    return [
        {
            "request_number": r.get("request_number"),
            "request_date": r.get("request_date"),
            "request_status": r.get("request_status"),
            "treatment_plan": r.get("treatment_plan"),
            "committee_date": r.get("committee_date"),
            "response_text": r.get("response_text"),
        }
        for r in recent
    ]


def scan_patient(patient_id: str, scan_date_iso: str, session: smc.SMCSession,
                  pending_categories: set, clinic: str = None) -> list:
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
    patient_pending = fetch_patient_pending_requests(patient_id)
    has_any_pending = len(patient_pending) > 0
    # Kept for anything still reading the old flat scalar columns --
    # the most recent open request, or all-None if there isn't one.
    first_pending = patient_pending[0] if patient_pending else {}
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
        # NEW, purely additive (see fetch_patient_admin_letter_notices
        # docstring): recent admin-letter responses for this patient --
        # shown ALONGSIDE pending_requests, never instead of it, as the
        # last piece of context ("here's what happened last time").
        "admin_letter_notices": fetch_patient_admin_letter_notices(patient_id),
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
                                         clinic=clinic_by_patient.get(pid)))
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
