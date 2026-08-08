"""
smc_session.py

Single merged session class for the SMC portal (smc.smcegy.com), combining
everything that used to live separately in:
  - Extract_Decrees_Details_New_Account.py   (login, decree list, decree
    details, death status)
  - Extract_All_Items_Data_From_SMC_New_Account_1.py  (receipts index,
    receipt details/items, billed/unsubmitted procedure items)

Both original scripts logged into the exact same account and used
near-identical login code, so this just keeps ONE session/login instead of
two, and is the piece daily_sync.py imports.

CREDENTIALS: no longer hardcoded. Set these as environment variables
(GitHub Actions secrets in production):
    SMC_USERNAME
    SMC_PASSWORD

Some pipeline steps (decree-list export, request-status export) must run
under a SECOND, separate SMC account because that account can see data this
one can't (and vice-versa). To support that without duplicating this whole
file, SMCSession() now optionally takes an explicit username/password pair.
If you don't pass any, it falls back to SMC_USERNAME / SMC_PASSWORD exactly
like before -- so every existing caller (daily_sync.py) keeps working
unchanged. For the second account, set:
    SMC_USERNAME_2
    SMC_PASSWORD_2
(or pass whatever credentials you like straight into the constructor).
"""

import os
import re
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://smc.smcegy.com"
USERNAME = os.environ.get("SMC_USERNAME", "")
PASSWORD = os.environ.get("SMC_PASSWORD", "")

# Second account, used only by pipeline steps that explicitly ask for it.
USERNAME_2 = os.environ.get("SMC_USERNAME_2", "")
PASSWORD_2 = os.environ.get("SMC_PASSWORD_2", "")


class SMCSession:
    """Manages the SMC website session and every API call the pipeline needs.

    By default this logs in with SMC_USERNAME / SMC_PASSWORD (unchanged
    behaviour). Pass username=/password= to use a different account --
    e.g. SMCSession(username=smc.USERNAME_2, password=smc.PASSWORD_2).
    """

    def __init__(self, username: Optional[str] = None, password: Optional[str] = None):
        self.username = username if username is not None else USERNAME
        self.password = password if password is not None else PASSWORD
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'X-Requested-With': 'XMLHttpRequest',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Referer': f'{BASE_URL}/smc/Home/Index',
            'Origin': BASE_URL,
        })
        self.logged_in = False
        self.death_status_cache: Dict[str, Tuple[str, Optional[str]]] = {}

    # ---------------------------------------------------------------
    # Login
    # ---------------------------------------------------------------
    def login(self) -> bool:
        if not self.username or not self.password:
            logging.error("SMC username/password not set for this session (env vars or constructor args).")
            return False
        try:
            logging.info("Logging in to SMC website...")
            login_page = self.session.get(f"{BASE_URL}/smc/Home/Index", timeout=30)
            if login_page.status_code != 200:
                logging.error(f"Failed to load login page: {login_page.status_code}")
                return False

            soup = BeautifulSoup(login_page.text, 'html.parser')
            token_input = soup.find('input', {'name': '__RequestVerificationToken'})
            verification_token = token_input.get('value') if token_input else None

            login_data = {'username': self.username, 'password': self.password}
            if verification_token:
                login_data['__RequestVerificationToken'] = verification_token

            login_response = self.session.post(
                f"{BASE_URL}/smc/Home/Index", data=login_data, timeout=30, allow_redirects=True,
            )

            if "logout" in login_response.text.lower() or "dashboard" in login_response.text.lower():
                self.logged_in = True
                logging.info("Login successful.")
                return True
            logging.error("Login failed - incorrect credentials or site structure changed.")
            return False
        except Exception as e:
            logging.error(f"Login error: {e}")
            return False

    # ---------------------------------------------------------------
    # Decree list / details / death status
    # (from Extract_Decrees_Details_New_Account.py)
    # ---------------------------------------------------------------
    def get_patient_decrees(self, national_id: str) -> Optional[BeautifulSoup]:
        """Fetch decrees table (first page) for a patient."""
        if not self.logged_in:
            logging.error("Not logged in. Call login() first.")
            return None
        try:
            date_to = datetime.now().strftime("%Y-%m-%d")
            date_from = datetime.now().replace(year=datetime.now().year - 1).strftime("%Y-%m-%d")
            api_url = f"{BASE_URL}/smc/Decrees/DecreesSearch"
            payload = {
                'decreeID': '', 'NationalID': national_id, 'PatientName': '',
                'dateFrom': date_from, 'dateTo': date_to, 'Retrieved': 'N',
                'decreeStatus': '', 'decreeSource': '1', 'stoppedDecree': 'N', 'page': '1',
            }
            response = self.session.post(
                api_url, data=payload, timeout=30,
                headers={'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
            )
            if response.status_code == 200:
                return BeautifulSoup(response.text, 'html.parser')
            logging.error(f"API returned {response.status_code} for patient {national_id}")
            return None
        except Exception as e:
            logging.error(f"Error fetching decrees for patient {national_id}: {e}")
            return None

    def extract_decree_numbers(self, soup: BeautifulSoup, patient_id: str) -> List[str]:
        """Decree numbers from the requestTable on the decrees list page."""
        decree_numbers = []
        try:
            if not soup:
                return decree_numbers
            table = soup.find('table', {'id': 'requestTable'})
            if not table:
                logging.warning(f"No requestTable found for patient {patient_id}")
                return decree_numbers
            rows = table.find_all('tr')
            for row in rows[1:]:
                cols = row.find_all('td')
                if len(cols) >= 2:
                    decree_link = cols[0].find('a')
                    if decree_link:
                        decree_numbers.append(decree_link.text.strip())
            return decree_numbers
        except Exception as e:
            logging.error(f"Error extracting decree numbers: {e}")
            return decree_numbers

    def get_first_patient_decree(self, national_id: str) -> Optional[str]:
        soup = self.get_patient_decrees(national_id)
        numbers = self.extract_decree_numbers(soup, national_id) if soup else []
        return numbers[0] if numbers else None

    def get_decree_details_html(self, decree_number: str) -> Optional[str]:
        if not self.logged_in:
            return None
        try:
            details_url = f"{BASE_URL}/smc/DecreeTreatmentProcedure/Create/{decree_number}"
            response = self.session.get(details_url, timeout=30)
            return response.text if response.status_code == 200 else None
        except Exception as e:
            logging.error(f"Error fetching decree details HTML for {decree_number}: {e}")
            return None

    def get_decree_details(self, decree_number: str) -> Optional[Dict]:
        """
        Row-level decree metadata from the decree details page.

        !! FIELD MAPPING TODO !!
        The list-page row (see extract_row_data() in daily_sync.py) only
        gives us Date + Decree_Number + three unlabeled cell values
        (Column_6_Value / Column_8_Value / Column_9_Text). Neither
        original script recorded what those three columns actually are
        (total value? value left? status text?) — that mapping was never
        captured in the code, only the column *positions* were. Before
        trusting output from this pipeline, open the SMC "requestTable"
        for one known patient in a browser next to this code and confirm:
            cols[5] (Column_6_Value)  -> is this Decree_Total_Value?
            cols[7] (Column_8_Value)  -> is this Decree_Value_Left?
            cols[8] (Column_9_Text)   -> is this Decree_Status (e.g. 'مفتوح')?
        and fix the mapping in daily_sync.py's build_decree_record() if not.
        """
        if not self.logged_in:
            return None
        try:
            details_url = f"{BASE_URL}/smc/DecreeTreatmentProcedure/Create/{decree_number}"
            response = self.session.get(details_url, timeout=30)
            if response.status_code != 200:
                logging.warning(f"Could not fetch decree details for {decree_number}: HTTP {response.status_code}")
                return None
            soup = BeautifulSoup(response.text, 'html.parser')
            return self._extract_decree_details_from_page(soup, decree_number)
        except Exception as e:
            logging.error(f"Error fetching decree details for {decree_number}: {e}")
            return None

    def _extract_decree_details_from_page(self, soup: BeautifulSoup, decree_number: str) -> Dict:
        details = {
            'Decree_Number': decree_number,
            'Decree_Text_Col6': None,   # duration / المدة -> likely Decree_Due_Period_Days (needs unit parsing)
            'Decree_Text_Col10': None,  # procedure / الإجراء -> Decree_Description
            'Decree_Expired_Status': None,
        }
        try:
            decree_table = soup.find('table', {'id': 'decreeTable'})
            if not decree_table:
                logging.warning(f"No decreeTable found for decree {decree_number}")
                return details
            rows = decree_table.find_all('tr')
            if len(rows) >= 1:
                first_row_cells = rows[0].find_all(['th', 'td'])
                for idx, cell in enumerate(first_row_cells):
                    cell_text = cell.get_text(strip=True)
                    if 'تاريخ القرار' in cell_text and idx + 1 < len(first_row_cells):
                        date_cell = first_row_cells[idx + 1]
                        style_attr = date_cell.get('style', '')
                        details['Decree_Expired_Status'] = (
                            'Expired' if ('orange' in style_attr.lower() or '#ffa500' in style_attr.lower())
                            else 'Active'
                        )
            if len(rows) >= 2:
                second_row_cells = rows[1].find_all(['th', 'td'])
                for idx, cell in enumerate(second_row_cells):
                    cell_text = cell.get_text(strip=True)
                    if 'المدة' in cell_text and idx + 1 < len(second_row_cells):
                        details['Decree_Text_Col6'] = second_row_cells[idx + 1].get_text(strip=True)
                    elif 'الإجراء' in cell_text and idx + 1 < len(second_row_cells):
                        details['Decree_Text_Col10'] = second_row_cells[idx + 1].get_text(strip=True)
            details.setdefault('Decree_Text_Col6', 'Not Found')
            details.setdefault('Decree_Text_Col10', 'Not Found')
            details.setdefault('Decree_Expired_Status', 'Unknown')
        except Exception as e:
            logging.error(f"Error extracting decree details for {decree_number}: {e}")
        return details

    def get_patient_death_status(self, patient_id: str) -> Tuple[str, Optional[str]]:
        if patient_id in self.death_status_cache:
            return self.death_status_cache[patient_id]
        decree_number = self.get_first_patient_decree(patient_id)
        if not decree_number:
            status, death_date = 'Unknown', None
        else:
            html_content = self.get_decree_details_html(decree_number)
            status, death_date = self._extract_death_status_from_html(html_content) if html_content else ('Unknown', None)
        self.death_status_cache[patient_id] = (status, death_date)
        return status, death_date

    def _extract_death_status_from_html(self, html_content: str) -> Tuple[str, Optional[str]]:
        if not html_content:
            return "Alive", None
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            decree_table = soup.find('table', {'id': 'decreeTable'})
            if decree_table:
                table_text = decree_table.get_text()
                death_indicators = ['متوفاة', 'متوفي', 'توفى', 'توفيت', 'deceased', 'died']
                for indicator in death_indicators:
                    if indicator in table_text.lower():
                        date_match = re.search(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})', table_text)
                        return "Dead", (date_match.group(0) if date_match else "Date not specified")
            return "Alive", None
        except Exception as e:
            logging.error(f"Error extracting death status: {e}")
            return "Alive", None

    # ---------------------------------------------------------------
    # Receipts (dispensed items) + billed/unsubmitted procedure items
    # (from Extract_All_Items_Data_From_SMC_New_Account_1.py)
    # ---------------------------------------------------------------
    def get_decree_receipts_index(self, decree_number: str) -> Optional[BeautifulSoup]:
        if not self.logged_in:
            return None
        try:
            url = f"{BASE_URL}/smc/HospDecreeReceipts/Index/{decree_number}"
            response = self.session.get(url, timeout=30)
            return BeautifulSoup(response.text, 'html.parser') if response.status_code == 200 else None
        except Exception as e:
            logging.debug(f"Error fetching receipts index: {e}")
            return None

    def extract_receipt_ids(self, soup: BeautifulSoup, decree_number: str) -> List[str]:
        receipt_ids = []
        try:
            if not soup:
                return receipt_ids
            table = soup.find('table', {'id': 'hospDecreeReceiptsTable'}) or soup.find('table', class_=re.compile(r'table'))
            if not table:
                return receipt_ids
            detail_links = table.find_all('a', href=re.compile(r'/smc/HospDecreeReceipts/Details/\d+'))
            for link in detail_links:
                m = re.search(r'/Details/(\d+)', link.get('href', ''))
                if m:
                    receipt_ids.append(m.group(1))
            return list(dict.fromkeys(receipt_ids))
        except Exception as e:
            logging.error(f"Error extracting receipt IDs: {e}")
            return receipt_ids

    def get_receipt_details(self, receipt_id: str) -> Optional[BeautifulSoup]:
        if not self.logged_in:
            return None
        try:
            url = f"{BASE_URL}/smc/HospDecreeReceipts/Details/{receipt_id}"
            response = self.session.get(url, timeout=30)
            return BeautifulSoup(response.text, 'html.parser') if response.status_code == 200 else None
        except Exception as e:
            logging.error(f"Error fetching receipt details: {e}")
            return None

    def extract_receipt_items(self, soup: BeautifulSoup, receipt_id: str, decree_number: str, patient_id: str) -> List[Dict]:
        """Items dispensed against a receipt: مدين/بيان/كمية/وحدة/تاريخ الصرف/ملاحظات."""
        items = []
        try:
            if not soup:
                return items
            items_table = None
            for table in soup.find_all('table'):
                for row in table.find_all('tr'):
                    cells = row.find_all('td')
                    if len(cells) >= 5 and re.search(r'\d+', cells[0].get_text(strip=True)):
                        items_table = table
                        break
                if items_table:
                    break
            if not items_table:
                return items

            for row in items_table.find_all('tr'):
                cells = row.find_all('td')
                if row.find('th') or len(cells) < 5:
                    continue
                item_name = cells[1].get_text(strip=True) if len(cells) > 1 else ''
                price_cell = cells[0].get_text(strip=True) if len(cells) > 0 else ''
                if not item_name or not price_cell or 'الاجمالي' in item_name or 'الإجمالي' in item_name:
                    continue
                quantity = cells[2].get_text(strip=True) if len(cells) > 2 else '1'
                unit = cells[3].get_text(strip=True) if len(cells) > 3 and cells[3].get_text(strip=True) else 'N/A'
                dispense_date = cells[4].get_text(strip=True) if len(cells) > 4 else ''
                notes = cells[5].get_text(strip=True) if len(cells) > 5 else ''
                price_clean = re.sub(r'[^\d\.]', '', price_cell) if price_cell else '0'
                quantity_clean = re.sub(r'[^\d\.]', '', quantity) if quantity and re.search(r'\d+', quantity) else '1'
                items.append({
                    'ID_Number': patient_id, 'Decree_Number': decree_number, 'Receipt_ID': receipt_id,
                    'Item_Name': item_name, 'Quantity': quantity_clean, 'Unit': unit,
                    'Price': price_clean, 'Dispensing_Date': dispense_date, 'Notes': notes,
                })
            return items
        except Exception as e:
            logging.error(f"Error extracting receipt items: {e}")
            return items

    def get_decree_procedures(self, decree_number: str) -> Optional[BeautifulSoup]:
        if not self.logged_in:
            return None
        try:
            url = f"{BASE_URL}/smc/DecreeTreatmentProcedure/Create/{decree_number}"
            response = self.session.get(url, timeout=30)
            return BeautifulSoup(response.text, 'html.parser') if response.status_code == 200 else None
        except Exception as e:
            logging.error(f"Error fetching decree procedures: {e}")
            return None

    def extract_billed_items(self, soup: BeautifulSoup, decree_number: str, patient_id: str) -> List[Dict]:
        """Unsubmitted/billed items straight off the decree's procedure table."""
        items = []
        try:
            if not soup:
                return items
            table = soup.find('table', {'id': 'decreeTreatmentProcTable'})
            if not table:
                return items
            tbody = table.find('tbody', {'id': 'decreeTreatmentProcBody'})
            if not tbody:
                return items
            for row in tbody.find_all('tr'):
                cells = row.find_all('td')
                if len(cells) < 4:
                    continue
                item_name = cells[0].get_text(strip=True) if len(cells) > 0 else None
                quantity = cells[1].get_text(strip=True) if len(cells) > 1 else None
                price_cell = cells[2].get_text(strip=True) if len(cells) > 2 else None
                dispense_date = cells[3].get_text(strip=True) if len(cells) > 3 else None
                if not item_name or item_name in ('', 'None'):
                    continue
                price = re.sub(r'[^\d\.]', '', price_cell) if price_cell else '0'
                quantity_clean = re.sub(r'[^\d\.]', '', quantity) if quantity and re.search(r'\d+', quantity) else '1'
                items.append({
                    'ID_Number': patient_id, 'Decree_Number': decree_number, 'Receipt_ID': 'N/A (Billed)',
                    'Item_Name': item_name, 'Quantity': quantity_clean, 'Unit': 'N/A', 'Price': price,
                    'Dispensing_Date': dispense_date if dispense_date else 'Not Specified',
                    'Notes': 'From decree procedures (not yet submitted)',
                })
            return items
        except Exception as e:
            logging.error(f"Error extracting billed items for decree {decree_number}: {e}")
            return items
