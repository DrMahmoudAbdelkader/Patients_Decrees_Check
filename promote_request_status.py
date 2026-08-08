"""
promote_request_status.py — normalizes today's
decree_request_status_daily_export rows (written by
request_status_sync.py) against request_name_map, so decree-renewal.js
can join "was a request already placed for this patient+treatment" by
(patient_id, decree_unique_name).

Unlike promote_hospital_decrees.py, this one is NOT blocked — Patient_ID
is already captured by request_status_sync.py (CITIZENSSN). No merge
into decree_issued_decrees happens here; this table stays a standalone
export, joined client-side.

Run as a step AFTER request_status_sync.py in the daily workflow.
"""

import os
import sys
import logging
import argparse

sys.path.insert(0, os.path.dirname(__file__))

import supabase_client as sb
from decree_name_map import normalize_request_text, get_request_map

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def fetch_unresolved_rows() -> list:
    return sb.fetch_all(
        "decree_request_status_daily_export",
        "request_number,patient_id,requested_decree_original_description,decree_unique_name",
        filters="decree_unique_name=is.null",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = fetch_unresolved_rows()
    logging.info(f"{len(rows)} request row(s) with no decree_unique_name resolved yet.")
    if not rows:
        return

    pending_texts = set()
    to_update = []

    for row in rows:
        raw_text = (row.get("requested_decree_original_description") or "").strip()
        unique_name, action = normalize_request_text(raw_text)
        if action == "mapped":
            to_update.append((row["request_number"], unique_name))
        elif action == "pending" and raw_text:
            pending_texts.add(raw_text)
        # 'ignored' -> leave decree_unique_name NULL forever, nothing to do.

    if args.dry_run:
        logging.info(f"(dry-run) would resolve {len(to_update)} request(s), "
                     f"queue {len(pending_texts)} pending request description(s) for review.")
        return

    for request_number, unique_name in to_update:
        sb.patch("decree_request_status_daily_export",
                 f"request_number=eq.{request_number}",
                 {"decree_unique_name": unique_name})
    if to_update:
        logging.info(f"Resolved {len(to_update)} request row(s) to a decree_unique_name.")

    get_request_map().push_pending(pending_texts)


if __name__ == "__main__":
    main()
