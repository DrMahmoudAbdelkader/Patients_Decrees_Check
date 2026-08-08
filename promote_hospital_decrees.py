"""
promote_hospital_decrees.py — turns today's decree_hospital_daily_export
rows (written by hospital_decrees_sync.py) into real
decree_issued_decrees rows, so the existing client-side renewal logic in
decree-renewal.js sees a patient's freshly-issued decree the same day it
was issued, instead of waiting for that patient to reappear in a future
queue window.

Run as a step AFTER hospital_decrees_sync.py in the daily workflow.

!! FORMERLY BLOCKED, NOW RESOLVED !!
hospital_decrees_sync.py now fills Patient_ID via a DecreesSearch?
decreeID=... lookup for each decree (see that module's step 2b), so the
merge below runs unconditionally. A handful of rows can still come back
with no national ID (SMC lookup failure, decree not indexed, etc.) --
those are the exception now, not the rule, and are still safely skipped
(logged, left un-promoted, retried automatically next run) rather than
guessed at via name matching, which risks colliding two patients who
share a name.
"""

import os
import sys
import logging
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

import supabase_client as sb
from decree_name_map import normalize_decree_name, get_decree_map

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def fetch_pending_export_rows() -> list:
    return sb.fetch_all(
        "decree_hospital_daily_export",
        "decree_number,patient_name,patient_id,issuing_date,decree_description,"
        "decree_unique_name,decree_status,decree_value_left,decree_total_value,"
        "decree_due_period_days,promoted",
        filters="promoted=eq.false",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = fetch_pending_export_rows()
    logging.info(f"{len(rows)} un-promoted row(s) in decree_hospital_daily_export.")
    if not rows:
        return

    pending_descriptions = set()
    to_merge = []
    to_mark_ignored_only = []
    blocked_no_patient_id = 0

    for row in rows:
        raw_description = (row.get("decree_description") or "").strip()
        unique_name, action = normalize_decree_name(raw_description)

        if action == "ignored":
            to_mark_ignored_only.append(row["decree_number"])
            continue

        if action == "pending" and raw_description:
            pending_descriptions.add(raw_description)

        if not row.get("patient_id"):
            blocked_no_patient_id += 1
            # Still update decree_unique_name on the export row itself so
            # it's ready the moment the patient_id fix lands and this
            # script gets re-run over the backlog.
            if not args.dry_run and unique_name != row.get("decree_unique_name"):
                sb.patch("decree_hospital_daily_export",
                         f"decree_number=eq.{row['decree_number']}",
                         {"decree_unique_name": unique_name})
            continue

        to_merge.append({
            "decree_number": row["decree_number"],
            "patient_id": row["patient_id"],
            "decree_date": row["issuing_date"],
            "decree_description": raw_description or None,
            "decree_unique_name": unique_name,
            "decree_status": row.get("decree_status"),
            "decree_value_left": row.get("decree_value_left"),
            "decree_total_value": row.get("decree_total_value"),
            "decree_due_period_days": row.get("decree_due_period_days"),
        })

    if blocked_no_patient_id:
        logging.warning(
            f"{blocked_no_patient_id} row(s) still have no patient_id (the DecreesSearch lookup in "
            f"hospital_decrees_sync.py came back empty for that decree) and CANNOT be merged into "
            f"decree_issued_decrees this run. They stay un-promoted (promoted=false) and will be "
            f"retried automatically next run."
        )

    if args.dry_run:
        logging.info(f"(dry-run) would merge {len(to_merge)} row(s) into decree_issued_decrees, "
                     f"mark {len(to_mark_ignored_only)} 'ignored'-description row(s) as promoted (no-op), "
                     f"and queue {len(pending_descriptions)} pending description(s) for review.")
        return

    if to_merge:
        sb.upsert("decree_issued_decrees", to_merge, on_conflict="decree_number")
        sb.patch("decree_hospital_daily_export",
                 "decree_number=in.(" + ",".join(f'"{r["decree_number"]}"' for r in to_merge) + ")",
                 {"promoted": True})
        logging.info(f"Merged {len(to_merge)} freshly-issued decree(s) into decree_issued_decrees.")

    if to_mark_ignored_only:
        sb.patch("decree_hospital_daily_export",
                 "decree_number=in.(" + ",".join(f'"{n}"' for n in to_mark_ignored_only) + ")",
                 {"promoted": True})
        logging.info(f"Marked {len(to_mark_ignored_only)} row(s) with an 'ignored' description as promoted (skipped).")

    get_decree_map().push_pending(pending_descriptions)


if __name__ == "__main__":
    main()
