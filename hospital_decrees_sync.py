"""
hospital_decrees_sync.py — daily "hospital decrees issued today" export.

Two-account pipeline, run once a day (via GitHub Actions), Cairo time:

  STEP 1 (secondary SMC account — SMC_USERNAME_2 / SMC_PASSWORD_2)
      POST /smc/Decrees/SearchHospitalDecrees for today's date range,
      sendingSite fixed at 20821, and walk every result page. Each row
      gives us Decree_Number + Patient_Name. The "issuing date" column in
      the page is redundant with the date range we searched, so instead of
      parsing it we just stamp every row with the same target date we
      queried for (see cairo_date.py) — that's the date the row belongs to,
      no matter what free-text format the page happens to print it in.

  STEP 2 (primary/original SMC account — SMC_USERNAME / SMC_PASSWORD)
      For every distinct Decree_Number found in step 1, using ONE session
      logged in with the ORIGINAL account:
        (a) pull the decree's description off
            /smc/DecreeTreatmentProcedure/Create/{decree_number} --
            reusing SMCSession.get_decree_details(), exactly like
            daily_sync.py already does elsewhere in this pipeline.
        (b) !! GAP CLOSED !! pull the patient's national ID by POSTing
            /smc/Decrees/DecreesSearch with ONLY decreeID set (everything
            else blank/default). Confirmed against a live sample: the
            single matching row comes back regardless of dateFrom/dateTo
            (a decree dated months before the queried range still showed
            up), so decreeID alone is the effective filter here --
            dateFrom/dateTo are still sent (the endpoint requires them)
            but are not meaningful filters for this call and are just
            filled with the target date. The row's 3rd column
            (الرقم القومي) is the national ID -- see
            _parse_decrees_search_row() for the full column map, lifted
            straight from a captured sample response.
      This reuses the same session/login as the description pull, so
      there's no extra login cost beyond one more POST per decree.

  STEP 3
      Merge and push 5 columns to Supabase:
          Decree_Number | Patient_Name | Patient_ID | Issuing_Date | Decree_Description
      -- Patient_ID is now filled by step 2(b) above (previously the
      blocking gap that kept promote_hospital_decrees.py from merging
      these rows into decree_issued_decrees).

!! VERIFY BEFORE FIRST REAL RUN !!
The row parsing below (`_parse_decree_table_page`) assumes the column
layout seen in a captured sample response of SearchHospitalDecrees:
    td[0]=serial (hidden), td[1]=checkbox, td[2]=<span id="decreeId">,
    td[3]=patient name, td[4]=decree date.
_parse_decrees_search_row() (step 2b) assumes the column layout of a
captured DecreesSearch-by-decreeID sample:
    td[0]=رقم القرار (link), td[1]=إسم المريض, td[2]=الرقم القومي,
    td[3]=تاريخ القرار, td[4]=نوع القرار, td[5]=قيمة القرار,
    td[6]=الخصم(%), td[7]=المتبقي, td[8]=قرار منتهي؟, td[9]=حالة القرار.
Run with --dry-run first and open dry_run_output/hospital_decrees.csv next
to a manual search on the site for the same day before trusting this for
real, the same way you'd check any other step in this pipeline. Pay
particular attention to the national_id column looking right for a
handful of known patients.

Usage:
    python hospital_decrees_sync.py --dry-run
    python hospital_decrees_sync.py                  # writes to Supabase
    python hospital_decrees_sync.py --date 2026-08-08 --dry-run
"""

import os
import re
import csv
import sys
import time
import logging
import argparse

sys.path.insert(0, os.path.dirname(__file__))

import smc_session as smc
import supabase_client as sb
from cairo_date import cairo_today_iso

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BASE_URL = smc.BASE_URL
SENDING_SITE = "20821"          # fixed, per spec
DELAY_BETWEEN_PAGES = 0.4       # seconds, be polite to the SMC server
DELAY_BETWEEN_DECREES = 0.3

SUPABASE_TABLE = "decree_hospital_daily_export"          # <-- adjust to your real table name
SUPABASE_CONFLICT_KEY = "decree_number"                   # <-- adjust to your real unique key


# =====================================================================
# STEP 1 — hospital decrees list (secondary account)
# =====================================================================
def _build_search_payload(date_iso: str, page: int) -> dict:
    return {
        'decreeID': '',
        'ssn': '',
        'sendingSite': SENDING_SITE,
        'requestCreator': '',
        'dateFrom': date_iso,
        'dateTo': date_iso,
        'hospDecreePrint': '1',
        'orderingBy': '0',
        'requestTreatmentProcedure': '',
        'print': 'N',
        'page': str(page),
    }


def _parse_decree_table_page(html: str):
    """Returns (rows, total_pages) for one page of the SearchHospitalDecrees
    response. rows is a list of {'decree_number', 'patient_name'} dicts."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')

    rows = []
    table = soup.find('table', {'id': 'DecreeTable'})
    if table:
        for tr in table.find_all('tr'):
            span = tr.find('span', id='decreeId')
            if not span:
                continue  # header row / stray <tr> with no decree in it
            decree_number = span.get_text(strip=True)
            if not decree_number:
                continue
            cols = tr.find_all('td')
            patient_name = cols[3].get_text(strip=True) if len(cols) > 3 else None
            rows.append({'decree_number': decree_number, 'patient_name': patient_name})

    total_pages = None
    page_info = soup.find(id='pageInfo')
    if page_info:
        m = re.search(r'Page\s+\d+\s+of\s+(\d+)', page_info.get_text())
        if m:
            total_pages = int(m.group(1))

    return rows, total_pages


def fetch_all_hospital_decrees(session: smc.SMCSession, date_iso: str) -> list:
    """Walks every page of SearchHospitalDecrees for the given date (both
    dateFrom and dateTo set to the same day) and returns all rows found."""
    url = f"{BASE_URL}/smc/Decrees/SearchHospitalDecrees"
    all_rows = []
    page = 1
    total_pages = None

    while True:
        logging.info(f"Fetching SearchHospitalDecrees page {page}"
                     + (f" of {total_pages}" if total_pages else "") + "...")
        resp = session.session.post(url, data=_build_search_payload(date_iso, page), timeout=30)
        if resp.status_code != 200:
            logging.error(f"SearchHospitalDecrees page {page} returned HTTP {resp.status_code}; stopping.")
            break

        rows, page_total_pages = _parse_decree_table_page(resp.text)
        if page == 1:
            total_pages = page_total_pages

        if not rows:
            logging.info(f"Page {page} had no decree rows; stopping.")
            break

        all_rows.extend(rows)
        logging.info(f"Page {page}: {len(rows)} row(s) (running total {len(all_rows)}).")

        if total_pages and page >= total_pages:
            break
        page += 1
        time.sleep(DELAY_BETWEEN_PAGES)

    return all_rows


# =====================================================================
# STEP 2 — decree descriptions + national ID (primary/original account)
# =====================================================================
def _parse_decrees_search_row(html: str, decree_number: str):
    """
    Parses a /smc/Decrees/DecreesSearch response filtered by decreeID,
    pulling out the row for `decree_number`. Column layout confirmed
    against a captured sample:
        td[0]=رقم القرار (link text)   td[1]=إسم المريض
        td[2]=الرقم القومي (national ID) -- what we actually need here
        td[3]=تاريخ القرار              td[4]=نوع القرار
        td[5]=قيمة القرار               td[6]=الخصم (%)
        td[7]=المتبقي                   td[8]=قرار منتهي؟
        td[9]=حالة القرار
    Returns a dict (national_id + a couple of bonus fields, harmless to
    keep around) or None if no row matched -- e.g. the decree fell out
    of the portal's search index for some reason.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find('table', {'id': 'requestTable'})
    if not table:
        return None
    for tr in table.find_all('tr'):
        cols = tr.find_all('td')
        if len(cols) < 10:
            continue  # header row, or a stray row with no data
        link = cols[0].find('a')
        row_decree_number = link.get_text(strip=True) if link else cols[0].get_text(strip=True)
        if row_decree_number != decree_number:
            continue  # DecreesSearch should already be filtered to one decree, but double-check
        return {
            'national_id': cols[2].get_text(strip=True) or None,
            'decree_status': cols[9].get_text(strip=True) or None,
            'decree_value_left': cols[7].get_text(strip=True) or None,
        }
    return None


def fetch_national_id_for_decree(session: smc.SMCSession, decree_number: str, date_iso: str):
    """
    POSTs /smc/Decrees/DecreesSearch with only decreeID set. dateFrom/
    dateTo are sent (the endpoint requires the fields) but are NOT a
    meaningful filter for this call -- confirmed against a live sample
    where a decree dated months earlier still came back for a
    narrow 1-day dateFrom/dateTo range. decreeID is what actually
    filters the result.
    """
    url = f"{BASE_URL}/smc/Decrees/DecreesSearch"
    payload = {
        'decreeID': decree_number,
        'NationalID': '',
        'PatientName': '',
        'dateFrom': date_iso,
        'dateTo': date_iso,
        'Retrieved': 'N',
        'decreeStatus': '',
        'decreeSource': '1',
        'stoppedDecree': 'N',
        'page': '1',
    }
    try:
        resp = session.session.post(url, data=payload, timeout=30)
    except Exception as e:
        logging.error(f"DecreesSearch failed for decree {decree_number}: {e}")
        return None
    if resp.status_code != 200:
        logging.warning(f"DecreesSearch for decree {decree_number} returned HTTP {resp.status_code}.")
        return None
    return _parse_decrees_search_row(resp.text, decree_number)


def fetch_decree_descriptions_and_national_ids(session: smc.SMCSession, decree_numbers: list, date_iso: str) -> tuple:
    """
    One pass over decree_numbers, same session throughout:
      - description via SMCSession.get_decree_details() (unchanged)
      - national_id via the new DecreesSearch-by-decreeID call above
    Returns (descriptions, national_ids), both decree_number -> value dicts.
    """
    descriptions = {}
    national_ids = {}
    for i, decree_number in enumerate(decree_numbers, 1):
        details = session.get_decree_details(decree_number)
        description = None
        if details:
            description = details.get('Decree_Text_Col10')
            if description in (None, 'Not Found'):
                description = None
        descriptions[decree_number] = description

        search_row = fetch_national_id_for_decree(session, decree_number, date_iso)
        national_ids[decree_number] = search_row.get('national_id') if search_row else None

        if i % 25 == 0:
            logging.info(f"Fetched description + national ID for {i}/{len(decree_numbers)} decrees...")
        time.sleep(DELAY_BETWEEN_DECREES)
    return descriptions, national_ids


# =====================================================================
# Output shaping
# =====================================================================
def build_output_rows(list_rows: list, descriptions: dict, national_ids: dict, date_iso: str) -> list:
    out = []
    seen = set()
    for row in list_rows:
        decree_number = row['decree_number']
        if decree_number in seen:
            continue  # SMC pagination can occasionally repeat a row; keep first occurrence
        seen.add(decree_number)
        out.append({
            'decree_number': decree_number,
            'patient_name': row.get('patient_name'),
            'patient_id': national_ids.get(decree_number),  # via DecreesSearch?decreeID=... (gap closed)
            'issuing_date': date_iso,
            'decree_description': descriptions.get(decree_number),
        })
    return out


def write_csv(path, rows):
    if not rows:
        logging.info(f"(dry-run) nothing to write for {path}")
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    logging.info(f"(dry-run) wrote {len(rows)} row(s) -> {path}")


# =====================================================================
# MAIN
# =====================================================================
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Write a CSV locally instead of writing to Supabase")
    parser.add_argument("--date", default=None,
                         help="Run for this exact date (YYYY-MM-DD) instead of 'today' in Cairo time.")
    parser.add_argument("--out-dir", default="./dry_run_output")
    args = parser.parse_args()

    target_date = args.date or cairo_today_iso()
    logging.info(f"Target date (Cairo): {target_date}" + (" (explicit --date)" if args.date else ""))

    # ---- Step 1: list, secondary account ----
    if not smc.USERNAME_2 or not smc.PASSWORD_2:
        logging.error("SMC_USERNAME_2 / SMC_PASSWORD_2 are not set (env vars) — this step needs the second account.")
        sys.exit(1)

    secondary_session = smc.SMCSession(username=smc.USERNAME_2, password=smc.PASSWORD_2)
    if not secondary_session.login():
        logging.error("Login with secondary SMC account failed — aborting.")
        sys.exit(1)

    list_rows = fetch_all_hospital_decrees(secondary_session, target_date)
    logging.info(f"{len(list_rows)} decree row(s) fetched from SearchHospitalDecrees for {target_date}.")

    if not list_rows:
        logging.warning("Nothing found for this date. Nothing to do.")
        return

    decree_numbers = sorted({r['decree_number'] for r in list_rows})

    # ---- Step 2: descriptions, primary/original account ----
    primary_session = smc.SMCSession()  # falls back to SMC_USERNAME / SMC_PASSWORD
    if not primary_session.login():
        logging.error("Login with primary SMC account failed — aborting.")
        sys.exit(1)

    descriptions, national_ids = fetch_decree_descriptions_and_national_ids(primary_session, decree_numbers, target_date)
    missing = [d for d, desc in descriptions.items() if not desc]
    if missing:
        logging.warning(f"{len(missing)} decree(s) had no description found (kept as NULL): {missing[:10]}"
                         + (" ..." if len(missing) > 10 else ""))
    missing_ids = [d for d, nid in national_ids.items() if not nid]
    if missing_ids:
        logging.warning(f"{len(missing_ids)} decree(s) had no national ID found via DecreesSearch (kept as NULL): "
                         f"{missing_ids[:10]}" + (" ..." if len(missing_ids) > 10 else "")
                         + " -- these will stay un-promoted in promote_hospital_decrees.py until resolved.")

    # ---- Step 3: shape + write ----
    output_rows = build_output_rows(list_rows, descriptions, national_ids, target_date)

    if args.dry_run:
        os.makedirs(args.out_dir, exist_ok=True)
        write_csv(os.path.join(args.out_dir, "hospital_decrees.csv"), output_rows)
        logging.info(f"DRY RUN complete — review the CSV in {args.out_dir} before running for real.")
    else:
        sb.upsert(SUPABASE_TABLE, output_rows, on_conflict=SUPABASE_CONFLICT_KEY)
        logging.info(f"Sync complete — {len(output_rows)} row(s) upserted into '{SUPABASE_TABLE}'.")


if __name__ == "__main__":
    main()
