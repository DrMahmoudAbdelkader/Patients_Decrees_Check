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
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

import smc_session as smc
import supabase_client as sb
from daily_sync import fetch_and_parse_queue, build_queue_rows, ALLOWED_CLINICS
from hmis_id_resolver import HmisIdResolver
from patient_decree_value import get_patient_decree_value_details, evaluate_dose_coverage
from lookup_patient_decree_value import fetch_pending_unsubmitted_value

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# See "ASSUMPTIONS TO CONFIRM" above.
DAYCARE_CLINICS = {c for c in ALLOWED_CLINICS if 'day care' in c or 'daycare' in c}

RESULTS_TABLE = "decree_value_left_daily_scan"
REQUEST_STATUS_TABLE = "decree_request_status_daily_export"
RUNS_TABLE = "decree_daily_scan_runs"

DELAY_BETWEEN_PATIENTS = 0.5


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


def fetch_pending_request_for_patient(patient_id: str, treatment_plan_name):
    """
    Returns (request_number, request_status, request_treatment_plan)
    for the first matching row in decree_request_status_daily_export,
    or (None, None, None) if none.

    If treatment_plan_name is None (patient has no decree at all, or
    its raw description isn't in decree_medication_catalog yet), falls
    back to matching on patient_id alone -- still useful signal ("this
    patient has *some* pending request on file"), just not
    decree-specific. See the naming-mismatch caveat in this script's
    top docstring.
    """
    filters = f"patient_id=eq.{patient_id}"
    if treatment_plan_name:
        filters += f"&decree_unique_name=eq.{treatment_plan_name}"
    rows = sb.fetch_all(
        REQUEST_STATUS_TABLE,
        "request_number,request_status,decree_unique_name",
        filters=filters,
    )
    if not rows:
        return None, None, None
    r = rows[0]
    return r.get("request_number"), r.get("request_status"), r.get("decree_unique_name")


def scan_patient(patient_id: str, scan_date_iso: str, session: smc.SMCSession) -> list:
    """Returns the decree_value_left_daily_scan row(s) for one patient."""
    decrees = get_patient_decree_value_details(session, patient_id)

    if not decrees:
        request_number, request_status, request_plan = fetch_pending_request_for_patient(patient_id, None)
        return [{
            "scan_date": scan_date_iso,
            "patient_id": patient_id,
            # '' not None: Postgres treats every NULL as distinct for
            # uniqueness purposes, so an ON CONFLICT upsert would never
            # match a prior "no decree" row for this patient and would
            # just pile up duplicate rows on every re-run. '' is a
            # real, stable value the unique constraint can match on.
            "decree_number": "",
            "decree_description": None,
            "treatment_plan_name": None,
            "regimen_status": None,
            "decree_total_value": None,
            "decree_value_left_website": None,
            "decree_status": None,
            "issuing_date": None,
            "decree_expiry_date": None,
            "pending_unsubmitted_value": 0,
            "real_value_left": None,
            "next_dose_covered": None,
            "has_pending_request": request_number is not None,
            "pending_request_number": request_number,
            "pending_request_status": request_status,
            "pending_request_treatment_plan": request_plan,
            "needs_attention": True,  # no decree at all -> always needs attention
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

        request_number = request_status = request_plan = None
        if needs_attention:
            request_number, request_status, request_plan = fetch_pending_request_for_patient(
                patient_id, d.get("treatment_plan_name")
            )

        out_rows.append({
            "scan_date": scan_date_iso,
            "patient_id": patient_id,
            "decree_number": d["decree_number"],
            "decree_description": d.get("decree_description"),
            "treatment_plan_name": d.get("treatment_plan_name"),
            "regimen_status": d.get("regimen_status"),
            "decree_total_value": d.get("decree_total_value"),
            "decree_value_left_website": website_left,
            "decree_status": d.get("decree_status"),
            "issuing_date": d.get("issuing_date"),
            "decree_expiry_date": d.get("decree_expiry_date"),
            "pending_unsubmitted_value": pending_value,
            "real_value_left": real_left,
            "next_dose_covered": dose_covered,
            "has_pending_request": request_number is not None,
            "pending_request_number": request_number,
            "pending_request_status": request_status,
            "pending_request_treatment_plan": request_plan,
            "needs_attention": needs_attention,
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
    for idx, pid in enumerate(patient_ids, 1):
        logging.info(f"[{idx}/{len(patient_ids)}] scanning patient {pid}...")
        try:
            all_rows.extend(scan_patient(pid, target_iso, session))
        except Exception as e:
            logging.error(f"Failed to scan patient {pid}: {e}")
        time.sleep(DELAY_BETWEEN_PATIENTS)

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
        mark_run(request_id, "done", scan_date_iso=target_iso, row_count=len(all_rows), flagged_count=len(flagged))
    else:
        try:
            sb.upsert(RESULTS_TABLE, all_rows, on_conflict="scan_date,patient_id,decree_number")
        except Exception as e:
            mark_run(request_id, "error", f"Failed to save results: {e}", scan_date_iso=target_iso)
            logging.error(f"Failed to save results: {e}")
            sys.exit(1)
        logging.info(f"Sync complete — {len(all_rows)} row(s) upserted into '{RESULTS_TABLE}'.")
        mark_run(request_id, "done", scan_date_iso=target_iso, row_count=len(all_rows), flagged_count=len(flagged))


if __name__ == "__main__":
    main()
