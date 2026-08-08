"""
daily_sync.py — Decree Renewal daily pipeline entry point.

Runs, once a day (via GitHub Actions):
  1. Download the "Clinic List Detail - by Status" queue report for
     TODAY + DATE_OFFSET_DAYS (default 15) from the HMIS webreport,
     parse it into clean rows, keep only the outpatient clinics that
     matter for this workflow.
  2. Log in to SMC, pull every issued decree (+ death status) for each
     distinct patient found in step 1.
  3. Pull every dispensed/billed item for every decree found in step 2.
  4. Upsert all three result sets into Supabase.

!! REDESIGN NOTE (mapping tables now dynamic, Supabase-backed) !!
Raw-text -> unique-name normalization is no longer an Excel VLOOKUP.
decree_name_map.py now loads decree_name_map / item_name_map straight
from Supabase, and a raw value can be in one of three states:
    mapped   -> normal row, decree_unique_name / unique_item_name set
    pending  -> row is KEPT (not dropped) with unique_name = NULL, and
                the raw text is upserted into the map table so it shows
                up in needs-review.js. The instant someone maps it
                in-app, needs-review.js promotes this exact row (and any
                sibling rows with the same raw text) directly -- no
                waiting for tomorrow's run.
    ignored  -> row is excluded entirely, same as the old #N/A behavior,
                and its raw text is never re-touched.
This replaced the old rule where an unmatched row was reduced to just
its description string and everything else about it (value, date,
status...) was thrown away -- see the redesign brief for why.

Run with --dry-run first and check the CSVs it writes to ./dry_run_output/
against a handful of patients you already know the answer for, before
ever running this for real (no --dry-run) against the Supabase tables.

Usage:
    python daily_sync.py --dry-run
    python daily_sync.py                      # writes to Supabase
    python daily_sync.py --date-offset-days 15 --patients-file ids.txt
"""

import os
import re
import csv
import sys
import time
import logging
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

import queue_extractor as qx
import queue_parser as qp
import smc_session as smc
import supabase_client as sb
from decree_name_map import normalize_decree_name, normalize_item_name, get_decree_map, get_item_map

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Outpatient clinics this workflow applies to (matches modules/decree-renewal.js)
ALLOWED_CLINICS = {
    'oncology5', 'day care clinic', 'pain clinic', 'out patient pharmacy',
    'oncology2', 'daycare unit', 'oncology1', 'hematology',
    'day care pharmcy', 'daycare pharmacy3', 'out patient pharmacy2',
    'day care unit 2', 'daycare pharmacy',
}

DELAY_BETWEEN_PATIENTS = 0.5   # seconds, be polite to the SMC server
DELAY_BETWEEN_DECREES = 0.3


# =====================================================================
# STEP 1 — Queue
# =====================================================================
def fetch_and_parse_queue(date_ddmmyyyy: str) -> list:
    """Downloads + parses the queue report for a single day. Returns
    clean record dicts (Clinic, National ID Number, Patient File No., ...)."""
    date_slash = date_ddmmyyyy.replace('-', '/')
    session = qx.requests.Session()

    logging.info(f"Downloading queue report for {date_ddmmyyyy} (report={qx.REPORT_CODE})...")
    filename, content = qx.fetch_report(session, qx.REPORT_CODE, date_slash, date_slash)

    logging.info("Verifying server used the requested date range...")
    qx.verify_report_date_range(content, date_slash, date_slash)

    logging.info("Parsing raw report into clean rows...")
    records = qp.extract_records_from_workbook_bytes(content)
    logging.info(f"Parsed {len(records)} raw queue rows.")
    return records


def build_queue_rows(raw_records: list, appointment_date_iso: str) -> list:
    """Filters to the allowed clinics and shapes rows for decree_queue_data."""
    rows = []
    for rec in raw_records:
        clinic = (rec.get("Clinic") or "").strip()
        if clinic.lower() not in ALLOWED_CLINICS:
            continue
        national_id = str(rec.get("National ID Number") or "").strip()
        if not national_id:
            continue
        rows.append({
            "clinic": clinic,
            "mr_code": str(rec.get("Patient File No.") or "").strip() or None,
            "national_id": national_id,
            "appointment_number": str(rec.get("Appointment Number") or "").strip() or None,
            "appointment_date": appointment_date_iso,
            "user": str(rec.get("User") or "").strip() or None,
            "old_medical_no": str(rec.get("Old Medical No.") or "").strip() or None,
        })
    return rows


# =====================================================================
# STEP 2 — Issued decrees
# =====================================================================
_DUE_PERIOD_RE = re.compile(r'(\d+)')


def parse_due_period_days(raw_text: str):
    """
    !! BEST-EFFORT — CONFIRM !! Decree_Text_Col6 ('duration'/المدة) has been
    seen as things like a raw day count or a month count depending on the
    decree type. This takes the first number found and, if the text also
    contains 'شهر' (month), multiplies by 30 — otherwise assumes it's
    already in days. Verify against a few real decrees before trusting it.
    """
    if not raw_text:
        return None
    m = _DUE_PERIOD_RE.search(raw_text)
    if not m:
        return None
    n = int(m.group(1))
    if 'شهر' in raw_text:
        return n * 30
    return n


def parse_smc_date(raw_text: str):
    """SMC dates have been seen as dd/mm/yyyy; adjust here if a run shows otherwise."""
    if not raw_text:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw_text.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def extract_row_data(row, patient_id: str) -> dict:
    """
    Ported from Extract_Decrees_Details_New_Account.py::extract_row_data.

    Column positions confirmed manually against the live SMC requestTable
    (cols[5] / cols[7] / cols[8]) — named directly now, no longer generic
    placeholders:
        cols[5] -> Decree_Total_Value
        cols[7] -> Decree_Value_Left
        cols[8] -> Decree_Status
    """
    cols = row.find_all('td')
    if len(cols) < 9:
        return None
    data = {
        'Patient_ID': patient_id,
        'Date': cols[3].text.strip() if len(cols) > 3 else None,
        'Decree_Total_Value': cols[5].text.strip() if len(cols) > 5 else None,
        'Decree_Value_Left': cols[7].text.strip() if len(cols) > 7 else None,
        'Decree_Status': cols[8].text.strip() if len(cols) > 8 else None,
    }
    decree_link = cols[0].find('a')
    data['Decree_Number'] = decree_link.text.strip() if decree_link else (cols[0].text.strip() if cols[0] else None)
    return data


def build_decree_record(row_data: dict, details: dict, death_status: str, death_date):
    """
    Shapes one decree_issued_decrees row. Per the redesign, this NEVER
    returns None for an unmatched description anymore -- only when the
    raw description is confirmed 'ignored' in decree_name_map (a real
    non-medication service you've already told the app to skip). An
    unmatched-but-undecided ('pending') description still gets a full
    row, with decree_unique_name = NULL, so nothing about it is lost.

    Returns (record_or_None, action) where action is
    'mapped' | 'pending' | 'ignored', so the caller can track which raw
    descriptions need to be pushed to decree_name_map as pending.
    """
    raw_description = (details or {}).get('Decree_Text_Col10') or ''
    unique_name, action = normalize_decree_name(raw_description)
    if action == 'ignored':
        return None, action

    return {
        "patient_id": row_data["Patient_ID"],
        "decree_date": parse_smc_date(row_data.get("Date")),
        "decree_total_value": _to_number(row_data.get("Decree_Total_Value")),
        "decree_value_left": _to_number(row_data.get("Decree_Value_Left")),
        "decree_status": row_data.get("Decree_Status"),
        "decree_number": row_data["Decree_Number"],
        "decree_due_period_days": parse_due_period_days((details or {}).get('Decree_Text_Col6')),
        "decree_description": raw_description or None,
        "decree_unique_name": unique_name,  # None while 'pending'
        "decree_expired_status": (details or {}).get('Decree_Expired_Status'),
        "patient_death_status": death_status,
        "patient_death_date": parse_smc_date(death_date) if death_date and death_date != "Date not specified" else None,
    }, action


def _to_number(text):
    if text in (None, ''):
        return None
    cleaned = re.sub(r'[^\d.\-]', '', str(text))
    try:
        return float(cleaned) if cleaned not in ('', '-', '.') else None
    except ValueError:
        return None


def fetch_decrees_for_patients(session: smc.SMCSession, patient_ids: list) -> tuple:
    """
    Returns (decree_rows, decree_numbers_by_patient, pending_descriptions).

    decree_numbers_by_patient includes decree numbers for BOTH 'mapped'
    and 'pending' rows now (only 'ignored' ones are skipped) -- a
    pending decree's dispensed items still get fetched below, so if/when
    it gets mapped in-app there's already dispensing history sitting
    behind it instead of a second wait for tomorrow's dispensed-items
    pull too.
    """
    decree_rows = []
    decree_numbers_by_patient = {}
    pending_descriptions = set()
    ignored_count = 0

    for idx, pid in enumerate(patient_ids, 1):
        logging.info(f"[decrees] patient {idx}/{len(patient_ids)}: {pid}")
        death_status, death_date = session.get_patient_death_status(pid)
        soup = session.get_patient_decrees(pid)
        if not soup:
            decree_numbers_by_patient[pid] = []
            continue

        table = soup.find('table', {'id': 'requestTable'})
        rows = table.find_all('tr')[1:] if table else []
        numbers = []
        for row in rows:
            row_data = extract_row_data(row, pid)
            if not row_data or not row_data.get('Decree_Number'):
                continue
            details = session.get_decree_details(row_data['Decree_Number'])
            record, action = build_decree_record(row_data, details, death_status, death_date)
            time.sleep(DELAY_BETWEEN_DECREES)

            if action == 'ignored':
                ignored_count += 1
                continue

            if action == 'pending':
                raw_description = ((details or {}).get('Decree_Text_Col10') or '').strip()
                if raw_description:
                    pending_descriptions.add(raw_description)

            decree_rows.append(record)
            numbers.append(row_data['Decree_Number'])
        decree_numbers_by_patient[pid] = numbers
        time.sleep(DELAY_BETWEEN_PATIENTS)

    if ignored_count:
        logging.info(f"[decrees] excluded {ignored_count} decree(s) marked 'ignored' in decree_name_map.")
    if pending_descriptions:
        logging.info(f"[decrees] {len(pending_descriptions)} distinct pending (unmapped) description(s) this run.")

    return decree_rows, decree_numbers_by_patient, pending_descriptions


# =====================================================================
# STEP 3 — Dispensed / billed items
# =====================================================================
def fetch_dispensed_items(session: smc.SMCSession, decree_numbers_by_patient: dict) -> tuple:
    """Returns (dispensed_rows, pending_items) — see shaping block below."""
    dispensed_rows = []
    total_decrees = sum(len(v) for v in decree_numbers_by_patient.values())
    done = 0

    for pid, decree_numbers in decree_numbers_by_patient.items():
        for decree_number in decree_numbers:
            done += 1
            logging.info(f"[dispensed] decree {done}/{total_decrees}: {decree_number} (patient {pid})")

            receipts_soup = session.get_decree_receipts_index(decree_number)
            if receipts_soup:
                for receipt_id in session.extract_receipt_ids(receipts_soup, decree_number):
                    receipt_soup = session.get_receipt_details(receipt_id)
                    if receipt_soup:
                        dispensed_rows.extend(session.extract_receipt_items(receipt_soup, receipt_id, decree_number, pid))
                    time.sleep(DELAY_BETWEEN_DECREES)

            procedures_soup = session.get_decree_procedures(decree_number)
            if procedures_soup:
                dispensed_rows.extend(session.extract_billed_items(procedures_soup, decree_number, pid))

            time.sleep(DELAY_BETWEEN_DECREES)

    # shape for decree_dispensed_items table. Per the redesign, a row
    # whose Item_Name comes back 'ignored' is excluded (same as before);
    # 'pending' rows are KEPT with unique_item_name = NULL.
    shaped = []
    pending_items = set()
    ignored_count = 0
    for item in dispensed_rows:
        raw_item_name = (item.get("Item_Name") or "").strip()
        unique_item_name, action = normalize_item_name(raw_item_name)
        if action == 'ignored':
            ignored_count += 1
            continue
        if action == 'pending' and raw_item_name:
            pending_items.add(raw_item_name)

        shaped.append({
            "id_number": item["ID_Number"],
            "decree_number": item["Decree_Number"],
            "item_name": item.get("Item_Name"),
            "unique_item_name": unique_item_name,  # None while 'pending'
            "quantity": _to_number(item.get("Quantity")),
            "unit": item.get("Unit"),
            "price": _to_number(item.get("Price")),
            "dispensing_date": parse_smc_date(item.get("Dispensing_Date")),
            "notes": item.get("Notes"),
        })

    if ignored_count:
        logging.info(f"[dispensed] excluded {ignored_count} item row(s) marked 'ignored' in item_name_map.")
    if pending_items:
        logging.info(f"[dispensed] {len(pending_items)} distinct pending (unmapped) item name(s) this run.")

    kept = [r for r in shaped if r["dispensing_date"]]  # dispensing_date is NOT NULL in the schema
    return kept, pending_items


# =====================================================================
# Dry-run output
# =====================================================================
def write_csv(path, rows):
    if not rows:
        logging.info(f"(dry-run) nothing to write for {path}")
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    logging.info(f"(dry-run) wrote {len(rows)} row(s) -> {path}")


def write_review_file(pending_descriptions, out_dir):
    path = os.path.join(out_dir, "raw_decree_descriptions_pending.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["raw_decree_description"])
        for u in sorted(pending_descriptions):
            writer.writerow([u])
    logging.info(f"(dry-run) {len(pending_descriptions)} pending decree description(s) -> {path}")


def write_items_review_file(pending_items, out_dir):
    path = os.path.join(out_dir, "raw_item_names_pending.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["raw_item_name"])
        for u in sorted(pending_items):
            writer.writerow([u])
    logging.info(f"(dry-run) {len(pending_items)} pending item name(s) -> {path}")


# =====================================================================
# MAIN
# =====================================================================
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Write CSVs locally instead of writing to Supabase")
    parser.add_argument("--date-offset-days", type=int, default=int(os.environ.get("DATE_OFFSET_DAYS", 15)),
                         help="Extract the queue for TODAY + this many days (default 15). "
                              "Ignored if --date is given.")
    parser.add_argument("--date", default=None,
                         help="Extract the queue for this EXACT date instead of an offset from today. "
                              "Format: YYYY-MM-DD. Use this to re-pull a specific past or future day "
                              "on demand, e.g. --date 2026-08-15")
    parser.add_argument("--patients-file", default=None,
                         help="Skip step 1 and use this newline-delimited national-ID file instead")
    parser.add_argument("--out-dir", default="./dry_run_output")
    args = parser.parse_args()

    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        target_date = datetime.now() + timedelta(days=args.date_offset_days)
    target_ddmmyyyy = target_date.strftime("%d-%m-%Y")
    target_iso = target_date.strftime("%Y-%m-%d")
    logging.info(f"Target date: {target_iso}"
                 + (" (explicit --date)" if args.date else f" (today + {args.date_offset_days} days)"))

    # ---- Step 1: queue ----
    if args.patients_file:
        with open(args.patients_file, encoding="utf-8") as f:
            patient_ids = [line.strip() for line in f if line.strip()]
        queue_rows = []
        logging.info(f"Using {len(patient_ids)} patient ID(s) from {args.patients_file} (queue step skipped).")
    else:
        raw_records = fetch_and_parse_queue(target_ddmmyyyy)
        queue_rows = build_queue_rows(raw_records, target_iso)
        patient_ids = sorted({r["national_id"] for r in queue_rows})
        logging.info(f"{len(queue_rows)} queue row(s) in allowed clinics -> {len(patient_ids)} distinct patient(s).")

    if not patient_ids:
        logging.warning("No patients found for this date/clinic filter. Nothing to do.")
        return

    # ---- Step 2: decrees ----
    session = smc.SMCSession()
    if not session.login():
        logging.error("SMC login failed — aborting.")
        sys.exit(1)

    decree_rows, decree_numbers_by_patient, pending_descriptions = fetch_decrees_for_patients(session, patient_ids)
    logging.info(f"{len(decree_rows)} decree row(s) fetched (medication decrees, mapped + pending).")

    # ---- Step 3: dispensed items ----
    dispensed_rows, pending_items = fetch_dispensed_items(session, decree_numbers_by_patient)
    logging.info(f"{len(dispensed_rows)} dispensed/billed item row(s) fetched (medication items, mapped + pending).")

    # ---- Step 4: write ----
    if args.dry_run:
        os.makedirs(args.out_dir, exist_ok=True)
        write_csv(os.path.join(args.out_dir, "queue_data.csv"), queue_rows)
        write_csv(os.path.join(args.out_dir, "issued_decrees.csv"), decree_rows)
        write_csv(os.path.join(args.out_dir, "dispensed_items.csv"), dispensed_rows)
        write_review_file(pending_descriptions, args.out_dir)
        write_items_review_file(pending_items, args.out_dir)
        with open(os.path.join(args.out_dir, "patient_ids.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(patient_ids))
        logging.info(f"DRY RUN complete — review the CSVs in {args.out_dir} before running for real.")
    else:
        if queue_rows:
            sb.upsert("decree_queue_data",
                      _dedupe_for_upsert(queue_rows, ("national_id", "appointment_number"), "decree_queue_data"),
                      on_conflict="national_id,appointment_number")
        sb.upsert("decree_issued_decrees",
                  _dedupe_for_upsert(decree_rows, ("decree_number",), "decree_issued_decrees"),
                  on_conflict="decree_number")
        sb.upsert("decree_dispensed_items",
                  _dedupe_for_upsert(
                      dispensed_rows,
                      ("id_number", "decree_number", "item_name", "dispensing_date", "quantity", "price"),
                      "decree_dispensed_items",
                  ),
                  on_conflict="id_number,decree_number,item_name,dispensing_date,quantity,price")
        # Keep decree_name_map / item_name_map in sync with what we saw
        # this run -- inserts brand-new pending raw text, bumps
        # times_seen/last_seen on ones already pending. Never touches a
        # row that's already 'mapped' or 'ignored' (see the DB trigger).
        get_decree_map().push_pending(pending_descriptions)
        get_item_map().push_pending(pending_items)
        logging.info("Sync complete.")


# =====================================================================
# Dedupe guard — Postgres/PostgREST rejects a batch upsert where two
# rows in the SAME request share the same on_conflict key ("ON CONFLICT
# DO UPDATE command cannot affect row a second time", error 21000).
# That's not a hypothetical: e.g. decree_dispensed_items' key
# (id_number, decree_number, item_name, dispensing_date, quantity,
# price) deliberately does not include Receipt_ID, so two rows scraped
# from two different receipts -- or a receipt line and a "billed but
# not yet submitted" procedure-table line -- can land on an identical
# key within one run. Since the schema already treats that key as "one
# row", sending them one-at-a-time would just have the second silently
# overwrite the first anyway; this collapses that before the batch POST
# so it doesn't blow up mid-write. Keeps the LAST occurrence per key
# (closest to "most recently seen this run").
# =====================================================================
def _dedupe_for_upsert(rows: list, key_fields: tuple, table_name: str) -> list:
    if not rows:
        return rows
    by_key = {}
    for row in rows:
        key = tuple(row.get(f) for f in key_fields)
        by_key[key] = row  # last one wins
    deduped = list(by_key.values())
    dropped = len(rows) - len(deduped)
    if dropped:
        logging.warning(
            f"[{table_name}] {dropped} row(s) shared an on_conflict key ({', '.join(key_fields)}) "
            f"with another row in this run's batch -- kept the last occurrence of each, dropped the "
            f"rest before upserting, to avoid Postgres error 21000. If this number looks unexpectedly "
            f"high, it's worth spot-checking whether real distinct events are colliding (e.g. two "
            f"receipts dispensing the same item/qty/price on the same day)."
        )
    return deduped


if __name__ == "__main__":
    main()
