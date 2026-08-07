"""
daily_sync.py — Decree Renewal daily pipeline entry point.

Runs, once a day (via GitHub Actions):
  1. Download the "Clinic List Detail - by Status" queue report for
     TODAY + DATE_OFFSET_DAYS (default 25) from the HMIS webreport,
     parse it into clean rows, keep only the outpatient clinics that
     matter for this workflow.
  2. Log in to SMC, pull every issued decree (+ death status) for each
     distinct patient found in step 1.
  3. Pull every dispensed/billed item for every decree found in step 2.
  4. Upsert all three result sets into Supabase.

!! READ BEFORE POINTING THIS AT PRODUCTION !!
Both previously-open items are now resolved:
  (a) decree-list column mapping — cols[5]/cols[7]/cols[8] confirmed as
      Decree_Total_Value / Decree_Value_Left / Decree_Status, named
      directly in extract_row_data() below.
  (b) raw-text -> unique-name normalization — now an exact-match lookup
      against your own two mapping sheets (decree_unique_name_map.xlsx,
      item_unique_name_map.xlsx), see decree_name_map.py. Anything with
      no match in those sheets (#N/A) is EXCLUDED from this pipeline —
      both for decrees and for dispensed/billed items — since that's
      ~99% non-medication services (scans, IR, labs, etc.) this workflow
      doesn't track. Run --dry-run and check
      dry_run_output/raw_decree_descriptions_needing_review.csv and
      raw_item_names_needing_review.csv for anything that should actually
      be in your mapping sheets but is falling through.
Run with --dry-run first and check the CSVs it writes to ./dry_run_output/
against a handful of patients you already know the answer for, before
ever running this for real (no --dry-run) against the Supabase tables.

Usage:
    python daily_sync.py --dry-run
    python daily_sync.py                      # writes to Supabase
    python daily_sync.py --date-offset-days 25 --patients-file ids.txt
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
from decree_name_map import normalize_decree_name, normalize_item_name

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
    Shapes one decree_issued_decrees row — or returns None if the raw
    Decree_Description has no match in decree_name_map.DECREE_NAME_MAP
    (an #N/A in your mapping sheet). Per your instruction, unmatched
    decrees are non-medication services (scans, interventional radiology,
    labs, etc.) and are excluded from this workflow entirely, so the
    caller must skip appending the row (and skip fetching its dispensed
    items) when this returns None.
    """
    raw_description = (details or {}).get('Decree_Text_Col10') or ''
    unique_name = normalize_decree_name(raw_description)
    if unique_name is None:
        return None

    return {
        "patient_id": row_data["Patient_ID"],
        "decree_date": parse_smc_date(row_data.get("Date")),
        "decree_total_value": _to_number(row_data.get("Decree_Total_Value")),
        "decree_value_left": _to_number(row_data.get("Decree_Value_Left")),
        "decree_status": row_data.get("Decree_Status"),
        "decree_number": row_data["Decree_Number"],
        "decree_due_period_days": parse_due_period_days((details or {}).get('Decree_Text_Col6')),
        "decree_description": raw_description or None,
        "decree_unique_name": unique_name,
        "decree_expired_status": (details or {}).get('Decree_Expired_Status'),
        "patient_death_status": death_status,
        "patient_death_date": parse_smc_date(death_date) if death_date and death_date != "Date not specified" else None,
    }


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
    Returns (decree_rows, decree_numbers_by_patient, unmatched_descriptions).

    Decrees whose raw description has no match in the Decree_Unique_Name
    map (build_decree_record() returns None) are excluded here — neither
    added to decree_rows nor to decree_numbers_by_patient, so step 3
    never bothers fetching dispensed items for a decree we're not
    tracking. unmatched_descriptions collects exactly which raw
    descriptions were excluded, for the dry-run review CSV.
    """
    decree_rows = []
    decree_numbers_by_patient = {}
    unmatched_descriptions = set()
    excluded_count = 0

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
            record = build_decree_record(row_data, details, death_status, death_date)
            time.sleep(DELAY_BETWEEN_DECREES)

            if record is None:
                excluded_count += 1
                raw_description = ((details or {}).get('Decree_Text_Col10') or '').strip()
                if raw_description:
                    unmatched_descriptions.add(raw_description)
                continue

            decree_rows.append(record)
            numbers.append(row_data['Decree_Number'])
        decree_numbers_by_patient[pid] = numbers
        time.sleep(DELAY_BETWEEN_PATIENTS)

    if excluded_count:
        logging.info(
            f"[decrees] excluded {excluded_count} decree(s) with no "
            f"Decree_Unique_Name match (non-medication services)."
        )

    return decree_rows, decree_numbers_by_patient, unmatched_descriptions


# =====================================================================
# STEP 3 — Dispensed / billed items
# =====================================================================
def fetch_dispensed_items(session: smc.SMCSession, decree_numbers_by_patient: dict) -> tuple:
    """Returns (dispensed_rows, unmatched_item_names) — see shaping block below."""
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

    # shape for decree_dispensed_items table — items with no match in the
    # Unique_Items_Names map (#N/A) are excluded, same rule as decrees:
    # these are the ~99% non-medication service lines (scans, IR,
    # labs, etc.) this workflow doesn't track.
    shaped = []
    unmatched_items = set()
    excluded_count = 0
    for item in dispensed_rows:
        raw_item_name = (item.get("Item_Name") or "").strip()
        unique_item_name = normalize_item_name(raw_item_name)
        if unique_item_name is None:
            excluded_count += 1
            if raw_item_name:
                unmatched_items.add(raw_item_name)
            continue
        shaped.append({
            "id_number": item["ID_Number"],
            "decree_number": item["Decree_Number"],
            "item_name": item.get("Item_Name"),
            "unique_item_name": unique_item_name,
            "quantity": _to_number(item.get("Quantity")),
            "unit": item.get("Unit"),
            "price": _to_number(item.get("Price")),
            "dispensing_date": parse_smc_date(item.get("Dispensing_Date")),
            "notes": item.get("Notes"),
        })

    if excluded_count:
        logging.info(
            f"[dispensed] excluded {excluded_count} item row(s) with no "
            f"Unique_Items_Names match (non-medication services)."
        )

    kept = [r for r in shaped if r["dispensing_date"]]  # dispensing_date is NOT NULL in the schema
    return kept, unmatched_items


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


def write_review_file(unmatched_descriptions, out_dir):
    """Raw Decree_Description values with no match in decree_unique_name_map.xlsx
    (#N/A) — every decree using one of these was excluded from this run.
    Add the ones that ARE medications to your mapping sheet; leave the rest
    (scans/IR/labs/etc.) out on purpose."""
    path = os.path.join(out_dir, "raw_decree_descriptions_needing_review.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["raw_decree_description"])
        for u in sorted(unmatched_descriptions):
            writer.writerow([u])
    logging.info(f"(dry-run) {len(unmatched_descriptions)} unmatched decree description(s) -> {path}")


def write_items_review_file(unmatched_items, out_dir):
    """Raw Item_Name values with no match in item_unique_name_map.xlsx (#N/A)
    — every dispensed/billed line using one of these was excluded."""
    path = os.path.join(out_dir, "raw_item_names_needing_review.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["raw_item_name"])
        for u in sorted(unmatched_items):
            writer.writerow([u])
    logging.info(f"(dry-run) {len(unmatched_items)} unmatched item name(s) -> {path}")


def push_needs_review(unmatched_descriptions, unmatched_items):
    """
    Upserts every unmatched raw decree description / item name into
    decree_needs_review (see needs_review_schema.sql) so you can query
    Supabase any time to spot a new medication that has no mapping yet
    -- instead of digging through dry-run CSVs or Action artifacts.
    Runs on every real (non-dry-run) invocation, regardless of whether
    anything was actually excluded this run.
    """
    rows = (
        [{"kind": "decree_description", "raw_text": t, "last_seen": _now_iso()}
         for t in sorted(unmatched_descriptions)]
        + [{"kind": "item_name", "raw_text": t, "last_seen": _now_iso()}
           for t in sorted(unmatched_items)]
    )
    if not rows:
        logging.info("[needs_review] nothing unmatched this run.")
        return
    sb.upsert("decree_needs_review", rows, on_conflict="kind,raw_text")
    logging.info(f"[needs_review] upserted {len(rows)} unmatched value(s) for review.")


def _now_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


# =====================================================================
# MAIN
# =====================================================================
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Write CSVs locally instead of writing to Supabase")
    parser.add_argument("--date-offset-days", type=int, default=int(os.environ.get("DATE_OFFSET_DAYS", 25)),
                         help="Extract the queue for TODAY + this many days (default 25). "
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

    decree_rows, decree_numbers_by_patient, unmatched_descriptions = fetch_decrees_for_patients(session, patient_ids)
    logging.info(f"{len(decree_rows)} decree row(s) fetched (medication decrees only).")

    # ---- Step 3: dispensed items ----
    dispensed_rows, unmatched_items = fetch_dispensed_items(session, decree_numbers_by_patient)
    logging.info(f"{len(dispensed_rows)} dispensed/billed item row(s) fetched (medication items only).")

    # ---- Step 4: write ----
    if args.dry_run:
        os.makedirs(args.out_dir, exist_ok=True)
        write_csv(os.path.join(args.out_dir, "queue_data.csv"), queue_rows)
        write_csv(os.path.join(args.out_dir, "issued_decrees.csv"), decree_rows)
        write_csv(os.path.join(args.out_dir, "dispensed_items.csv"), dispensed_rows)
        write_review_file(unmatched_descriptions, args.out_dir)
        write_items_review_file(unmatched_items, args.out_dir)
        with open(os.path.join(args.out_dir, "patient_ids.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(patient_ids))
        logging.info(f"DRY RUN complete — review the CSVs in {args.out_dir} before running for real.")
    else:
        if queue_rows:
            sb.upsert("decree_queue_data", queue_rows, on_conflict="national_id,appointment_number")
        sb.upsert("decree_issued_decrees", decree_rows, on_conflict="decree_number")
        sb.upsert("decree_dispensed_items", dispensed_rows,
                  on_conflict="id_number,decree_number,item_name,dispensing_date,quantity,price")
        push_needs_review(unmatched_descriptions, unmatched_items)
        logging.info("Sync complete.")


if __name__ == "__main__":
    main()
