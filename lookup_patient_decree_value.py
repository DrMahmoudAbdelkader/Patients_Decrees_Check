"""
lookup_patient_decree_value.py — on-demand "value left" lookup for ONE
patient, triggered by the Supabase Edge Function behind the new
"Decree Value Left" module page (modules/decree-value-left.js).

WHY THIS IS A SEPARATE SCRIPT, NOT A SYNCHRONOUS EDGE FUNCTION CALL
--------------------------------------------------------------------
Scraping a patient's SMC decrees page is one HTTP round-trip per
decree for its details page -- a patient with many decrees can take
well over a minute. Supabase Edge Functions (Deno Deploy) have a
request time limit far shorter than that, and GitHub Actions has no
way to stream a synchronous response back to whoever triggered it
either. So the whole flow is asynchronous, coordinated through two
Supabase tables (see sql/decree_value_lookup_schema.sql):

    1. The Edge Function (supabase/functions/patient-decree-lookup)
       inserts one row into `decree_value_lookup_requests`
       (status='pending') and fires a workflow_dispatch call at
       GitHub for THIS script, passing request_id + patient_id as
       workflow inputs. It returns request_id to the browser
       immediately.
    2. The browser (modules/decree-value-left.js) polls
       `decree_value_lookup_requests` by request_id every couple of
       seconds until status flips to 'done' or 'error'.
    3. THIS script does the actual SMC scrape (via the shared
       patient_decree_value.get_patient_decree_value_details()),
       joins each decree against the app's own `pending_decree_orders`
       / `pending_decree_items` tables (Enhanced Monitor's
       still-to-be-submitted bills) to get that decree's
       pending_unsubmitted_value, writes one row per decree into
       `decree_value_lookup_results` with real_value_left already
       computed, and finally flips
       `decree_value_lookup_requests.status` to 'done' (or 'error'
       with a message, so the page shows a real failure instead of
       spinning forever).

Usage (this is what the GitHub Actions workflow runs):
    python lookup_patient_decree_value.py --request-id <uuid> --patient-id <national_id>
"""

import os
import sys
import logging
import argparse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

import smc_session as smc
import supabase_client as sb
from patient_decree_value import get_patient_decree_value_details, evaluate_dose_coverage

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

REQUESTS_TABLE = "decree_value_lookup_requests"
RESULTS_TABLE = "decree_value_lookup_results"


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_pending_unsubmitted_value(decree_number: str) -> float:
    """
    Sums pending_decree_items.total_value across every
    pending_decree_orders row for this decree_number whose status is
    NOT 'completed' -- i.e. Enhanced Monitor's "still needs
    submission" bucket for that decree. Mirrors exactly the query
    enhanced-monitor.js itself runs in checkExistingServicesForDecree()
    / savePendingDecreeEnhanced(), so this feature's number always
    agrees with what a user would see opening that decree in Enhanced
    Monitor directly.
    """
    rows = sb.fetch_all(
        "pending_decree_orders",
        "id,items:pending_decree_items(total_value)",
        filters=f"decree_number=eq.{decree_number}&status=neq.completed",
    )
    total = 0.0
    for order in rows:
        for item in (order.get("items") or []):
            total += float(item.get("total_value") or 0)
    return total


def mark_request(request_id: str, status: str, error_message: str = None):
    body = {"status": status, "completed_at": _now_iso()}
    if error_message:
        body["error_message"] = error_message[:2000]
    sb.patch(REQUESTS_TABLE, f"id=eq.{request_id}", body)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--patient-id", required=True, help="National ID to search for on SMC")
    args = parser.parse_args()

    logging.info(f"[value-lookup] request {args.request_id} — patient {args.patient_id}")

    session = smc.SMCSession()
    if not session.login():
        mark_request(args.request_id, "error", "SMC login failed.")
        logging.error("SMC login failed — aborting.")
        sys.exit(1)

    try:
        decrees = get_patient_decree_value_details(session, args.patient_id)
    except Exception as e:
        mark_request(args.request_id, "error", f"Extraction failed: {e}")
        logging.error(f"Extraction failed: {e}")
        sys.exit(1)

    if not decrees:
        # A patient with zero decrees on file is a valid, non-error
        # outcome -- don't leave the request stuck at 'pending'.
        mark_request(args.request_id, "done")
        logging.warning(f"No decrees found for patient {args.patient_id} — marking request done with 0 rows.")
        return

    rows = []
    for d in decrees:
        pending_value = fetch_pending_unsubmitted_value(d["decree_number"])
        value_left_website = d.get("decree_value_left_website")
        real_value_left = (
            (value_left_website - pending_value)
            if value_left_website is not None else None
        )
        # Only meaningful for the current decree in its treatment-plan
        # group -- a superseded/previous-cycle decree isn't what the
        # patient would actually be dispensed against next.
        dose_covered = (
            evaluate_dose_coverage(d, real_value_left)
            if d.get("regimen_status") == "current" else None
        )
        rows.append({
            "request_id": args.request_id,
            "decree_number": d["decree_number"],
            "decree_description": d.get("decree_description"),
            "decree_total_value": d.get("decree_total_value"),
            "decree_value_left_website": value_left_website,
            "decree_status": d.get("decree_status"),
            "issuing_date": d.get("issuing_date"),
            "decree_due_period_days": d.get("decree_due_period_days"),
            "decree_expiry_date": d.get("decree_expiry_date"),
            "pending_unsubmitted_value": pending_value,
            "real_value_left": real_value_left,
            "treatment_plan_name": d.get("treatment_plan_name"),
            "reception_display_name": d.get("reception_display_name"),
            "is_cycles": d.get("is_cycles", False),
            "is_supportive": d.get("is_supportive", False),
            "average_dose_value": d.get("average_dose_value"),
            "regimen_status": d.get("regimen_status"),
            "next_dose_covered": dose_covered,
        })

    try:
        sb.upsert(RESULTS_TABLE, rows, on_conflict="request_id,decree_number")
    except Exception as e:
        mark_request(args.request_id, "error", f"Failed to save results: {e}")
        logging.error(f"Failed to save results: {e}")
        sys.exit(1)

    mark_request(args.request_id, "done")
    logging.info(f"[value-lookup] request {args.request_id} done — {len(rows)} decree(s) written.")


if __name__ == "__main__":
    main()
