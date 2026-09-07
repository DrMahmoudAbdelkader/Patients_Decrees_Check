"""
decree_name_map.py

Mapping tables now live in Supabase (decree_name_map, item_name_map,
request_name_map) instead of the two committed Excel files. Loaded fresh
at the start of every run, so a mapping you add in-app this morning is
picked up by tonight's cron run without touching this repo.

Each table has a `status` column: 'pending' | 'mapped' | 'ignored'.
  - mapped   -> raw_text has a confirmed unique value; used for lookups.
  - ignored  -> raw_text is confirmed irrelevant (non-medication service,
                junk request text, etc.) -- EXCLUDE this row outright,
                same as the old Excel #N/A behavior, and never re-touch
                its times_seen/last_seen.
  - pending  -> seen at least once, no decision yet. Per your "map it ->
                included right away" requirement, rows for a pending raw
                value are NOT dropped anymore -- daily_sync.py keeps them
                with decree_unique_name / unique_item_name = NULL. The
                app's "map it" action (needs-review.js) promotes those
                NULL rows to the chosen unique_name the moment you submit
                it -- see the WHERE-bounded UPDATE in that file. This
                module's job is only to keep the map tables themselves in
                sync: insert new pending rows, bump times_seen/last_seen
                on ones already pending. It never writes 'mapped' or
                'ignored' -- only the app does that.
                
import os
import logging
from datetime import datetime
from datetime import datetime, timezone

import supabase_client as sb



def _now_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class NameMap:
    """One loaded map table (decree_name_map / item_name_map / request_name_map)."""

    def __init__(self, table: str, unique_col: str):
        self.table = table
        self.unique_col = unique_col
        self.mapped = {}      # raw_text -> unique value
        self.ignored = set()  # raw_text
        self.pending = set()  # raw_text already sitting as 'pending' in the table
        self._load()

    def _load(self):
        try:
            rows = sb.fetch_all(self.table, f"raw_text,{self.unique_col},status")
        except Exception as e:
            logging.error(
                f"[{self.table}] could not load from Supabase ({e}) -- treating as "
                f"empty for this run. EVERYTHING will come back 'pending' until this "
                f"is fixed -- rows will still be kept (not dropped), just unmatched."
            )
            rows = []
//   GITHUB_REPO_2      -- e.g. "Patients_Decrees_Check"
//   GITHUB_WORKFLOW_FILE_2 -- "patient-decree-lookup.yml"
//   GITHUB_REF_2       -- branch to dispatch on, e.g. "main"
//
// TEMP DEBUG: this build logs the exact resolved values right before
// the GitHub dispatch call, so a stray trailing space/newline picked
// up when a secret was pasted in becomes visible in the function logs
// (it would otherwise be invisible -- `supabase secrets list` never
// shows values back to you). Remove the console.log once confirmed
// clean.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

    const requestId = inserted.id as string;

    // ---- Step 2: trigger the GitHub Actions workflow ----
    // TEMP DEBUG -- remove once the resolved values are confirmed clean.
    console.log(
        `DEBUG dispatch -> owner="${GITHUB_OWNER_2}" repo="${GITHUB_REPO_2}" file="${GITHUB_WORKFLOW_FILE_2}" ref="${GITHUB_REF_2}"`
    );

    const dispatchUrl = `https://api.github.com/repos/${GITHUB_OWNER_2}/${GITHUB_REPO_2}/actions/workflows/${GITHUB_WORKFLOW_FILE_2}/dispatches`;
    const ghResp = await fetch(dispatchUrl, {
        method: "POST",


def scan_patient(patient_id: str, scan_date_iso: str, session: smc.SMCSession,
                  pending_categories: set) -> list:
                  pending_categories: set, clinic: str = None) -> list:
    """Returns the decree_value_left_daily_scan row(s) for one patient.
    `pending_categories` accumulates every raw decree_description this
    run that came back 'pending' from categorize_decree() (needs a
        return [{
            "scan_date": scan_date_iso,
            "patient_id": patient_id,
            "clinic": clinic,
            # '' not None: Postgres treats every NULL as distinct for
            # uniqueness purposes, so an ON CONFLICT upsert would never
            # match a prior "no decree" row for this patient and would
        out_rows.append({
            "scan_date": scan_date_iso,
            "patient_id": patient_id,
            "clinic": clinic,
            "decree_number": d["decree_number"],
            "decree_description": d.get("decree_description"),
            "treatment_plan_name": d.get("treatment_plan_name"),
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
    for idx, pid in enumerate(patient_ids, 1):
        logging.info(f"[{idx}/{len(patient_ids)}] scanning patient {pid}...")
        try:
            all_rows.extend(scan_patient(pid, target_iso, session, pending_categories))
            all_rows.extend(scan_patient(pid, target_iso, session, pending_categories,
                                         clinic=clinic_by_patient.get(pid)))
        except Exception as e:
            logging.error(f"Failed to scan patient {pid}: {e}")
        time.sleep(DELAY_BETWEEN_PATIENTS)

    if pending_categories:
        get_category_map().push_pending(pending_categories)
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
        os.makedirs(args.out_dir, exist_ok=True)
        write_csv(os.path.join(args.out_dir, "value_left_daily_scan.csv"), all_rows)
        logging.info(f"DRY RUN complete — review the CSV in {args.out_dir} before running for real.")
        push_pending_categories()
        mark_run(request_id, "done", scan_date_iso=target_iso, row_count=len(all_rows), flagged_count=len(flagged))
    else:
        try:
            logging.error(f"Failed to save results: {e}")
            sys.exit(1)
        logging.info(f"Sync complete — {len(all_rows)} row(s) upserted into '{RESULTS_TABLE}'.")
        push_pending_categories()
        mark_run(request_id, "done", scan_date_iso=target_iso, row_count=len(all_rows), flagged_count=len(flagged))
