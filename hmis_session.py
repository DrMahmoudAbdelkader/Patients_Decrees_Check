"""
hmis_session.py

Ported from the Decree-renewal pipeline's HMIS-lookup Supabase Edge
Function (the "hmis-patient-lookup" function's index.ts), so the Python
queue pipeline can resolve a patient itself instead of calling that
function over HTTP. Both do the exact same three-step HMIS Central
Index flow (search -> render results -> open patient) against the
same host (41.33.24.254:8080) queue_extractor.py already talks to --
but this is a DIFFERENT sub-app on that host (/hmis-menu + /HMIS,
"Central Index / patient search") from the report portal
(/WebReport-JWEB) queue_extractor.py uses, and this one DOES require a
real login, unlike the report export.

WHY THIS EXISTS
-----------------
The "Clinic List Detail" queue report (outpat_clnc_lst_det_j) only
gives you each patient's internal HMIS "Medical No." (his_mr) -- not
their 14-digit national ID, which is what every downstream step in
this pipeline (SMC lookups, decree_queue_data, ...) keys off. This
module logs into HMIS's Central Index screen and looks a Medical No.
up directly, returning (among other fields) the national ID.

Don't call get_patient_by_mr() per-patient without a cache in front of
it -- each lookup is several HTTP round trips against a single
hospital box (login is once per HMISSession, but the search/render/
open sequence runs fresh every call). See hmis_id_resolver.py, which
wraps this with an economy_patient_registry cache so a patient seen on
a previous day isn't re-scraped every run.

CREDENTIALS (GitHub Actions secrets in production):
    HMIS_USERNAME
    HMIS_PASSWORD

SITE CONFIG (deployment-specific, not secret -- override only if this
hospital's setup changes; defaults match the same deployment
queue_extractor.py and the hmis-patient-lookup edge function target):
    HOSPITAL_CODE     (default '01')
    HMIS_BRANCH_ID    (default '1')
    HMIS_BASE         (default 'http://41.33.24.254:8080/HMIS')
    HMIS_MENU_BASE    (default 'http://41.33.24.254:8080/hmis-menu')
    HMIS_HOME_PATH    (default '/faces/templates/home_basic.xhtml')
    HMIS_MODULE_CODE  (default 'UE1J' -- base64('PMI'), the
                       "Registration" tile that contains Central Index)

Unlike the edge function, there's no HMIS_STATIC_COOKIE debug escape
hatch here -- that was a manual-testing convenience for hand-capturing
a session; the automated pipeline always does a real login.
"""

import os
import re
import base64
import logging
import html as html_module
from typing import Dict, Optional

import requests

from cairo_date import cairo_today_slash

USERNAME = os.environ.get("HMIS_USERNAME", "")
PASSWORD = os.environ.get("HMIS_PASSWORD", "")

DEFAULT_HOSPITAL = os.environ.get("HOSPITAL_CODE", "01")
HMIS_BRANCH_ID = os.environ.get("HMIS_BRANCH_ID", "1")
HMIS_BASE = os.environ.get("HMIS_BASE", "http://41.33.24.254:8080/HMIS")
HMIS_MENU_BASE = os.environ.get("HMIS_MENU_BASE", "http://41.33.24.254:8080/hmis-menu")
HMIS_HOME_PATH = os.environ.get("HMIS_HOME_PATH", "/faces/templates/home_basic.xhtml")
HMIS_MODULE_CODE = os.environ.get("HMIS_MODULE_CODE", "UE1J")

TIMEOUT = 30


def _origin(url: str) -> str:
    m = re.match(r'^(https?://[^/]+)', url)
    return m.group(1) if m else url


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _decode_html(value: str) -> str:
    return html_module.unescape(value or "")


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _extract_input_value(html_text: str, id_or_name: str) -> str:
    """Pulls a hidden/text <input>'s current `value` out of rendered
    HTML by id or name -- mirrors index.ts's extractInputValue()."""
    escaped = re.escape(id_or_name)
    for attr in ("id", "name"):
        m = re.search(rf'<input[^>]*\b{attr}=["\']{escaped}["\'][^>]*>', html_text, re.I)
        if m:
            vm = re.search(r'\bvalue=["\']([^"\']*)["\']', m.group(0), re.I)
            return vm.group(1) if vm else ""
    return ""


def _extract_form_viewstate(html_text: str, form_id: str) -> str:
    """JSF pages can carry SEVERAL independent javax.faces.ViewState
    hidden inputs -- one per <form>. Scopes the search to the <form>
    that matches form_id, exactly like index.ts's extractFormViewState()
    -- grabbing the first ViewState on the page is wrong whenever the
    page has more than one form (this HMIS page has a dozen)."""
    escaped = re.escape(form_id)
    form_m = re.search(rf'<form[^>]*\bid=["\']{escaped}["\'][^>]*>', html_text, re.I)
    if not form_m:
        return ""
    rest = html_text[form_m.end():]
    next_form = re.search(r'<form[^>]*>', rest, re.I)
    region = rest[:next_form.start()] if next_form else rest
    vs = re.search(r'javax\.faces\.ViewState[^>]*\bvalue=["\']([^"\']*)["\']', region, re.I)
    return vs.group(1) if vs else ""


def _extract_partial_viewstate(xml_text: str) -> str:
    """Every PrimeFaces partial-response AJAX reply carries the NEXT
    ViewState in its own <update id="...javax.faces.ViewState..."> block
    -- has to be threaded from response to request for the whole
    search -> render -> open sequence, same as a real browser."""
    m = re.search(
        r'<update id=["\'][^"\']*ViewState[^"\']*["\']>\s*(?:<!\[CDATA\[([\s\S]*?)\]\]>|([\s\S]*?))\s*</update>',
        xml_text, re.I,
    )
    if not m:
        return ""
    return (m.group(1) or m.group(2) or "").strip()


def _extract_edit_patient_source(html_text: str, search_value: str) -> Optional[str]:
    """The "edit patient" icon's component id on a search-results row is
    a JSF auto-generated id (e.g. contentForm:searchForm:j_idt301:0:btnEdtPatient)
    that shifts between sessions -- re-derived fresh from the search-
    results response every call rather than hardcoded. If several rows
    come back (a loose search), prefers the row whose surrounding markup
    mentions the searched value."""
    pattern = re.compile(r'contentForm:searchForm:(j_idt\d+):(\d+):btnEdtPatient')
    matches = list(pattern.finditer(html_text))
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0].group(0)
    for m in matches:
        window_start = max(0, m.start() - 2000)
        nearby = html_text[window_start:m.start()]
        if search_value in nearby:
            return m.group(0)
    return matches[0].group(0)


def _parse_patient_html(raw_html: str) -> dict:
    """Extracts every field we need from the single HMIS response HTML
    fragment returned by the 'edit patient' click. Mirrors index.ts's
    parseHmisPatient() field-for-field."""
    html_text = _decode_html(raw_html)

    card_m = re.search(r'<p[^>]*\bid=["\']lnkCardModal["\'][^>]*>([\s\S]*?)</p>', html_text, re.I)
    if not card_m:
        card_m = re.search(
            r'class=["\'][^"\']*\bpatient-data\b[^"\']*["\'][^>]*>\s*<p[^>]*\bclass=["\']left["\'][^>]*>([\s\S]*?)</p>',
            html_text, re.I,
        )
    card_parts = [_clean(p) for p in card_m.group(1).split('/')] if card_m else []

    mr = card_parts[0] if len(card_parts) > 0 else ''
    full_name = card_parts[1] if len(card_parts) > 1 else ''
    sex = card_parts[2] if len(card_parts) > 2 else ''
    age_m = re.search(r'\d+', card_parts[3]) if len(card_parts) > 3 else None
    age = int(age_m.group(0)) if age_m else None

    # Best-effort language split: the observed name is mostly Arabic
    # with one Latin-script token at the end -- a heuristic, not a
    # guarantee, matching index.ts's own caveat on this.
    latin_tokens = re.findall(r"[A-Za-z][A-Za-z.'-]*", full_name)
    english_name = _clean(' '.join(latin_tokens))
    arabic_name = _clean(re.sub(r"[A-Za-z][A-Za-z.'-]*", ' ', full_name))

    national_id = _extract_input_value(html_text, 'contentForm:reg-form:idNumber')
    birth_date = _extract_input_value(html_text, 'contentForm:reg-form:birthDate')

    nat_select_m = re.search(r'<select[^>]*\bnationalityId[^>]*>([\s\S]*?)</select>', html_text, re.I)
    nat_scope = nat_select_m.group(1) if nat_select_m else html_text
    nat_m = re.search(
        r'<option[^>]*\bvalue=["\']([^"\']*)["\'][^>]*\bselected=["\']selected["\'][^>]*>([^<]*)</option>',
        nat_scope, re.I,
    )
    nationality = _clean(nat_m.group(2)) if nat_m else ''

    phone_tag_m = re.search(r'<input[^>]*\bonkeypress=["\']onlyCellPhoneNumber\(event\)["\'][^>]*>', html_text, re.I)
    phone = ''
    if phone_tag_m:
        vm = re.search(r'\bvalue=["\']([^"\']*)["\']', phone_tag_m.group(0), re.I)
        phone = vm.group(1) if vm else ''

    return {
        'mr': mr,
        'national_id': national_id,
        'full_name': full_name,
        'arabic_name': arabic_name,
        'english_name': english_name,
        'sex': sex,
        'age': age,
        'nationality': nationality,
        'birth_date': birth_date,
        'phone': phone,
    }


class HMISSession:
    """One logged-in HMIS Central Index session. Call login() once,
    then get_patient_by_mr() as many times as needed -- each call
    re-runs the search -> render -> open sequence against the SAME
    session (matching what a browser does looking up several patients
    in a row without navigating away), so you don't pay the login cost
    more than once per run."""

    def __init__(self, username: Optional[str] = None, password: Optional[str] = None):
        self.username = username if username is not None else USERNAME
        self.password = password if password is not None else PASSWORD
        self.session = requests.Session()
        self.tokens: Dict[str, str] = {}

    # ---- login -------------------------------------------------------
    def login(self) -> bool:
        if not self.username or not self.password:
            logging.error("HMIS_USERNAME/HMIS_PASSWORD are not set (env vars).")
            return False

        try:
            # Step 1/2: GET the hmis-menu portal, read the login form's ViewState.
            r = self.session.get(f"{HMIS_MENU_BASE}/", timeout=TIMEOUT)
            r.raise_for_status()
            menu_view_state = _extract_form_viewstate(r.text, 'loginForm')
            if not menu_view_state:
                logging.error(
                    f"HMIS: could not read loginForm's ViewState from hmis-menu "
                    f"(HTTP {r.status_code}). The portal's markup may have changed."
                )
                return False

            # Step 2/2: submit the login form -- a plain form submit, not a
            # JSF partial-ajax postback, same as clicking "Login" in a browser.
            body = {
                'loginForm': 'loginForm',
                'loginForm:txtUsername': self.username,
                'loginForm:encUser': _b64(self.username),
                'loginForm:encL': _b64('L'),
                'loginForm:txtPassword': self.password,
                'loginForm:encUPass': _b64(self.password),
                'loginForm:encBrc': _b64(DEFAULT_HOSPITAL),
                'loginForm:encBri': _b64(HMIS_BRANCH_ID),
                'javax.faces.ViewState': menu_view_state,
                'loginForm:submitOrder': 'loginForm:submitOrder',
            }
            r = self.session.post(
                f"{HMIS_MENU_BASE}/faces/management/login.xhtml",
                data=body,
                headers={
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Origin': _origin(HMIS_MENU_BASE),
                    'Referer': f"{HMIS_MENU_BASE}/",
                },
                timeout=TIMEOUT,
            )
            if r.status_code >= 400:
                logging.error(f"HMIS: hmis-menu login failed: HTTP {r.status_code}: {r.text[:300]}")
                return False

            # Launch the "Registration" module (contains Central Index) the
            # same way the portal's own JS does -- a bare GET with a valid
            # cookie is NOT enough; pg/cimod are per-session tokens minted
            # by this exact POST.
            launch_body = {
                'encu': _b64(self.username),
                'encp': _b64(self.password),
                'encb': _b64(DEFAULT_HOSPITAL),
                'encbid': _b64(HMIS_BRANCH_ID),
                'encl': _b64('L'),
                'encm': HMIS_MODULE_CODE,
            }
            r = self.session.post(
                f"{HMIS_BASE}/",
                data=launch_body,
                headers={
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Origin': _origin(HMIS_BASE),
                    'Referer': f"{HMIS_MENU_BASE}/",
                },
                timeout=TIMEOUT,
            )
            html_text = r.text
            view_state = _extract_form_viewstate(html_text, 'contentForm:searchForm')
            if not view_state:
                logging.error(
                    f"HMIS: logged in, but could not read contentForm:searchForm's ViewState "
                    f"after launching the Registration module (HTTP {r.status_code}). This "
                    f"usually means HMIS_USERNAME/HMIS_PASSWORD, HOSPITAL_CODE, or "
                    f"HMIS_BRANCH_ID are wrong, or HMIS_MODULE_CODE no longer maps to the "
                    f"Registration/Central Index tile."
                )
                return False

            self.tokens = {
                'ci_mode': _extract_input_value(html_text, 'contentForm:searchForm:txtCIMode'),
                'encrypted_page_code': _extract_input_value(
                    html_text, 'contentForm:searchForm:txtCentralIndexEncryptedPageCode'
                ),
                'view_state': view_state,
            }
            return True
        except requests.RequestException as e:
            logging.error(f"HMIS login failed: {e}")
            return False

    # ---- lookup --------------------------------------------------------
    def _build_step_payload(self, search_type: str, search_value: str,
                             source: str, execute: str,
                             render: Optional[str] = None,
                             search_status: Optional[str] = None,
                             patient_id: Optional[str] = None) -> dict:
        """Builds one JSF partial-ajax postback for contentForm:searchForm.
        The manual (browser) flow is actually three of these back to back
        -- click "Search" (runs the query), render the results, then
        click the matching row's "edit patient" icon -- each carrying
        forward the ViewState the previous response handed back."""
        is_mr = search_type == 'mr'
        fields = {
            'javax.faces.partial.ajax': 'true',
            'javax.faces.source': source,
            'javax.faces.partial.execute': execute,
            'javax.faces.behavior.event': 'click',
            'javax.faces.partial.event': 'click',
            'contentForm:searchForm': 'contentForm:searchForm',
            'contentForm:searchForm:errorMsg': '',
            'contentForm:searchForm:msgText': '',
            'contentForm:searchForm:txtCIMode': self.tokens.get('ci_mode', ''),
            'contentForm:searchForm:txtHiddenInputTxt': '',
            'contentForm:searchForm:txtCentralIndexParams': '',
            'contentForm:searchForm:txtCentralIndexSearchStatus': search_status or '',
            'contentForm:searchForm:txtInitialStatus': '0',
            'contentForm:searchForm:txtCentralIndexSuccessStatus': '1',
            'contentForm:searchForm:txtCentralIndexFailedStatus': '-1',
            'contentForm:searchForm:txtCentralIndexInvalidatedStatus': '-2',
            'contentForm:searchForm:txtCentralIndexErrorFlagSamePatient': '',
            'contentForm:searchForm:medicalNo': search_value if is_mr else '',
            'contentForm:searchForm:patfullName': '',
            'contentForm:searchForm:patidNo': '' if is_mr else search_value,
            'contentForm:searchForm:patcellPhone': '',
            'contentForm:searchForm:medicalinsuranceNo': '',
            'contentForm:searchForm:CentralIndexPolicyNumber': '',
            'contentForm:searchForm:firstName': '',
            'contentForm:searchForm:secondName': '',
            'contentForm:searchForm:thirdName': '',
            'contentForm:searchForm:fourthName': '',
            'contentForm:searchForm:motherName': '',
            'contentForm:searchForm:enGender': 'select',
            'contentForm:searchForm:nationalityId': 'select',
            'contentForm:searchForm:phones': '',
            'contentForm:searchForm:fcid': '',
            'contentForm:searchForm:fcname': 'select',
            'contentForm:searchForm:otherMedicalNo': '',
            'contentForm:searchForm:FMREL': 'select',
            'contentForm:searchForm:FMPNo': '',
            'contentForm:searchForm:VMRNo': '',
            'contentForm:searchForm:fromDate': '',
            'contentForm:searchForm:toDate': '',
            'contentForm:searchForm:toAge': '',
            'contentForm:searchForm:fromAgeCode': 'Y',
            'contentForm:searchForm:fromAge': '',
            'contentForm:searchForm:toAgeCode': 'Y',
            'contentForm:searchForm:patientStatus': 'A',
            'contentForm:searchForm:familyId': '',
            'contentForm:searchForm:txtCentralIndexMode': '',
            'contentForm:searchForm:txtCentralIndexEncryptedPageCode': self.tokens.get('encrypted_page_code', ''),
            'contentForm:searchForm:txtCurrentDate': cairo_today_slash(),
            'javax.faces.ViewState': self.tokens.get('view_state', ''),
        }
        if render:
            fields['javax.faces.partial.render'] = render
        if patient_id is not None:
            fields['patientId'] = patient_id
        return fields

    def _post_ajax(self, payload: dict) -> str:
        r = self.session.post(
            f"{HMIS_BASE}{HMIS_HOME_PATH}",
            data=payload,
            headers={
                'Accept': '*/*',
                'Faces-Request': 'partial/ajax',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin': _origin(HMIS_BASE),
                'Referer': f"{HMIS_BASE}{HMIS_HOME_PATH}",
            },
            timeout=TIMEOUT,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"HMIS request failed: HTTP {r.status_code}: {r.text[:300]}")
        return r.text

    def get_patient_by_mr(self, mr: str) -> Optional[dict]:
        """Looks up ONE patient by their internal HMIS Medical No.
        Returns a dict (mr, national_id, full_name, arabic_name,
        english_name, sex, age, nationality, birth_date, phone), or
        None if the search itself genuinely returned no rows. Raises
        on a real request/session error (login expired mid-run, site
        unreachable, unexpected markup, ...) so callers can tell "not
        found" apart from "the lookup itself broke"."""
        if not self.tokens:
            raise RuntimeError("HMISSession.login() must succeed before looking up a patient.")

        # Step 1/3 -- click "Search": actually runs the query server-side.
        step1 = self._post_ajax(self._build_step_payload(
            'mr', mr,
            source='contentForm:searchForm:searchBtn',
            execute='contentForm:searchForm:searchBtn',
        ))
        vs = _extract_partial_viewstate(step1)
        if vs:
            self.tokens['view_state'] = vs

        # Step 2/3 -- render the results list/table for that query.
        step2 = self._post_ajax(self._build_step_payload(
            'mr', mr,
            source='contentForm:searchForm:btnSearchBasic',
            execute='contentForm:searchForm',
            render='contentForm:searchForm:searchList contentForm:searchForm:txtCentralIndex',
        ))
        vs = _extract_partial_viewstate(step2)
        if vs:
            self.tokens['view_state'] = vs

        edit_source = _extract_edit_patient_source(step2, mr)
        if not edit_source:
            # No matching row in the results -- genuinely not found.
            return None

        # Step 3/3 -- click the matching row's "edit patient" icon; this
        # is the click that actually loads the patient-detail fragment.
        step3 = self._post_ajax(self._build_step_payload(
            'mr', mr,
            source=edit_source,
            execute='contentForm:searchForm contentForm:modalsPanel',
            render=('contentForm:inpatient_panel_content contentForm:modalsPanel '
                    'contentForm:searchForm:errorMsg contentForm:errorMerge'),
            search_status='1',
            patient_id=mr,
        ))

        patient = _parse_patient_html(step3)
        if not patient.get('mr') and not patient.get('national_id'):
            return None
        return patient
