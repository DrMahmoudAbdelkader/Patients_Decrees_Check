"""
patient_decree_value.py

Shared helper: given ONE patient's national ID and an already-logged-in
SMCSession, extract every issued decree on their SMC decrees page,
shaped for the "value left" feature -- both the on-demand module
(lookup_patient_decree_value.py, triggered from
modules/decree-value-left.js) and the daily automated scan
(queue_value_left_scan.py) import and call the exact same function
here, so their column mapping / date parsing / regimen classification
can never drift apart into two silently-different answers.

DELIBERATELY reuses daily_sync.py's already-verified pieces instead of
re-deriving them:
    extract_row_data()      -- cols[5]/cols[7]/cols[8] mapping on the
                                requestTable list page (see that
                                function's own docstring for the "how
                                do we know this" note)
    parse_due_period_days() -- Decree_Text_Col6 ('المدة') -> integer days
    parse_smc_date()        -- dd/mm/yyyy -> ISO
    _to_number()            -- strips currency formatting -> float
Importing daily_sync.py is safe here: everything in it that touches
the network lives under `if __name__ == "__main__":`, so importing it
as a module runs none of that.

This module adds two things daily_sync.py doesn't need:
    decree_expiry_date = issuing_date + decree_due_period_days
    current / superseded / previous-cycle regimen classification
    (see below)

REGIMEN CLASSIFICATION (per your Q1/Q2/Q5 answers)
----------------------------------------------------
Every decree is looked up in decree_medication_catalog (the live
Supabase table -- Excel is only ever the offline source used to
populate/update that table, never read at match time) by its
description, normalized via normalize_decree_description() on both
sides to strip formatting noise (extra whitespace, Arabic tashkeel,
alef-hamza variants) that would otherwise cause a real match to be
missed. This gives: treatment_plan_name (the simple name shown to
users), is_cycles, average_dose_value, is_supportive.

Decrees are then grouped by (patient, treatment_plan_name) -- NOT by
raw description, since cycle decrees of the same treatment
deliberately have different raw text ("قرار اول" vs "قرار ثاني" etc.)
but the SAME treatment_plan_name. Whichever decree in a group has the
latest issuing_date is 'current'; every older sibling in that group is
'previous_cycle' (if is_cycles) or 'superseded' (if not). This is
exactly daily_sync.py/decree-renewal.js's own approach -- no attempt
is made to parse the "قرار اول/ثاني/..." ordinal text itself; recency
by date already gives the right answer for both cyclical and flat
repeats, confirmed against decree-renewal.js's production logic.

Per your Q2 answer, the richer "last-3-dispensing-visits define the
active plan" gate from decree-renewal.js is DELIBERATELY NOT applied
here -- it needs an extra per-decree scrape (dispensed items) that's
too slow for an on-the-spot lookup a user is waiting on. Every decree
found on the page is eligible for classification; only recency within
its own treatment_plan_name group decides current vs.
superseded/previous-cycle.

A decree whose raw description isn't in the catalog at all gets
regimen_status = 'unmapped' and is left out of any grouping (it can't
be compared against siblings it can't be identified as belonging to).

Callers (lookup_patient_decree_value.py / queue_value_left_scan.py)
are responsible for computing real_value_left (needs the
Enhanced-Monitor pending-value join, a different data source) and for
comparing it against average_dose_value / the 500 EGP supportive floor
-- this module only supplies the classification + catalog fields those
comparisons need.
"""

import logging
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import smc_session as smc
import supabase_client as sb
from daily_sync import extract_row_data, parse_due_period_days, parse_smc_date, _to_number
from cairo_date import cairo_today_iso

CATALOG_TABLE = "decree_medication_catalog"
SUPPORTIVE_VALUE_FLOOR = 500  # EGP -- your Q4 rule

_catalog_cache: Optional[Dict[str, Dict]] = None

# Arabic tashkeel/diacritics (harakat, tanween, shadda, sukun, etc.) --
# stripped because the SMC website is inconsistent about including them
# while your catalog rows (typed by hand) usually aren't.
_ARABIC_DIACRITICS_RE = re.compile(r"[\u0610-\u061A\u064B-\u065F\u06D6-\u06DC\u06DF-\u06E8\u06EA-\u06ED\u0670]")
_TATWEEL_RE = re.compile(r"\u0640")  # ـ elongation character
_ZERO_WIDTH_RE = re.compile(r"[\u200B-\u200F\uFEFF]")
_WHITESPACE_RE = re.compile(r"\s+")
_ALEF_VARIANTS_RE = re.compile(r"[\u0622\u0623\u0625\u0671]")  # آ أ إ ٱ -> ا


def normalize_decree_description(text: Optional[str]) -> str:
    """Canonical form used as the catalog match key on BOTH sides
    (the live decree_medication_catalog rows loaded from Supabase, and
    the raw description scraped off the SMC website), so a match is
    never lost purely to formatting differences that don't change what
    the decree actually is: NFKC-normalizes, strips zero-width chars
    and Arabic diacritics/tatweel, folds alef-hamza variants to a bare
    alef, collapses all whitespace runs to a single space, and trims.
    This is deliberately NOT a fuzzy/approximate match -- two
    descriptions that differ in real wording still won't match; it
    only neutralizes formatting noise that has no bearing on identity.
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _ZERO_WIDTH_RE.sub("", t)
    t = _ARABIC_DIACRITICS_RE.sub("", t)
    t = _TATWEEL_RE.sub("", t)
    t = _ALEF_VARIANTS_RE.sub("\u0627", t)
    t = _WHITESPACE_RE.sub(" ", t)
    return t.strip()


def _load_catalog() -> Dict[str, Dict]:
    """Loads decree_medication_catalog once per process and caches it
    -- this is called once per decree per patient, so re-fetching the
    (small, ~230-row) catalog every time would be wasteful. Call
    reset_catalog_cache() in a long-lived process if the catalog table
    changes mid-run (not needed for the one-shot CLI scripts here).

    Keyed by normalize_decree_description(decree_description), not the
    raw column value -- this is the live Supabase catalog table (not
    Excel), but an exact-string key is still brittle against harmless
    formatting drift (extra spaces, missing/extra tashkeel, a
    different alef-hamza form) between what you typed into the catalog
    and what the SMC website hands back for the same decree. If two
    catalog rows normalize to the same key, the row with a real
    average_dose_value wins (so a stray unmapped/duplicate row can
    never shadow a properly filled-in one); ties among equally-filled
    rows keep whichever was returned first, and are logged so you can
    clean up the duplicate.
    """
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache
    try:
        rows = sb.fetch_all(
            CATALOG_TABLE,
            "decree_description,treatment_plan_name,is_cycles,average_dose_value,is_supportive",
        )
    except Exception as e:
        logging.error(f"[value-left] could not load {CATALOG_TABLE} ({e}) -- "
                       f"all decrees will come back 'unmapped' this run.")
        rows = []

    cache: Dict[str, Dict] = {}
    collisions = []
    for r in rows:
        raw_desc = r.get("decree_description")
        if not raw_desc:
            continue
        key = normalize_decree_description(raw_desc)
        if not key:
            continue
        existing = cache.get(key)
        if existing is None:
            cache[key] = r
        elif existing.get("decree_description") != raw_desc:
            collisions.append((existing.get("decree_description"), raw_desc))
            # Prefer whichever row actually has a cutoff value, so an
            # unmapped duplicate can't shadow the real one.
            if existing.get("average_dose_value") is None and r.get("average_dose_value") is not None:
                cache[key] = r

    if collisions:
        logging.warning(
            f"[value-left] {len(collisions)} pair(s) of {CATALOG_TABLE} rows normalize to the same "
            f"key (near-duplicate descriptions) -- kept the one with average_dose_value where only "
            f"one had it: {collisions[:5]}" + (" ..." if len(collisions) > 5 else "")
        )

    _catalog_cache = cache
    logging.info(f"[value-left] loaded {len(_catalog_cache)} row(s) from {CATALOG_TABLE}.")
    return _catalog_cache


def reset_catalog_cache():
    global _catalog_cache
    _catalog_cache = None


def _compute_expiry_date(issuing_date_iso: Optional[str], due_period_days: Optional[int]) -> Optional[str]:
    """decree_date + decree_due_period_days. Returns None (never a
    guessed date) if either input is missing -- an unknown period
    should never silently render as "no expiry" via a wrong default."""
    if not issuing_date_iso or due_period_days is None:
        return None
    try:
        d = datetime.strptime(issuing_date_iso, "%Y-%m-%d")
        return (d + timedelta(days=due_period_days)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _classify_regimens(decrees: List[Dict]) -> None:
    """Mutates each decree dict in place, adding regimen_status:
    'current' | 'superseded' | 'previous_cycle' | 'unmapped'.
    Grouped by treatment_plan_name; newest issuing_date per group wins.
    A decree with no issuing_date sorts as the OLDEST in its group
    (never wins 'current' over a decree with a known date) -- an
    unknown date should never accidentally look like the newest one.
    """
    groups: Dict[str, List[Dict]] = {}
    for d in decrees:
        name = d.get("treatment_plan_name")
        if not name:
            d["regimen_status"] = "unmapped"
            continue
        groups.setdefault(name, []).append(d)

    for name, members in groups.items():
        if len(members) == 1:
            members[0]["regimen_status"] = "current"
            continue
        # sort by issuing_date desc; missing date sorts last (oldest)
        members_sorted = sorted(members, key=lambda d: d.get("issuing_date") or "", reverse=True)
        newest = members_sorted[0]
        newest["regimen_status"] = "current"
        for older in members_sorted[1:]:
            older["regimen_status"] = "previous_cycle" if older.get("is_cycles") else "superseded"


def get_patient_decree_value_details(session: smc.SMCSession, national_id: str) -> List[Dict]:
    """
    `session` must already be logged in. Returns one dict per decree
    found on the patient's SMC decrees page:
        decree_number, decree_description, decree_total_value,
        decree_value_left_website, decree_status, issuing_date,
        decree_due_period_days, decree_expiry_date,
        treatment_plan_name, is_cycles, average_dose_value,
        is_supportive, regimen_status

    Never raises for a single malformed row -- it's skipped and
    logged, so one bad decree never blocks the rest of a patient's
    list. Returns [] (not None) if the patient has no decrees page or
    no rows on it, so callers can always safely iterate the result.
    """
    results: List[Dict] = []
    catalog = _load_catalog()
    soup = session.get_patient_decrees(national_id)
    if not soup:
        logging.warning(f"[value-left] no decrees page returned for patient {national_id}.")
        return results

    table = soup.find('table', {'id': 'requestTable'})
    rows = table.find_all('tr')[1:] if table else []

    for row in rows:
        try:
            row_data = extract_row_data(row, national_id)
            if not row_data or not row_data.get('Decree_Number'):
                continue

            decree_number = row_data['Decree_Number']
            details = session.get_decree_details(decree_number) or {}

            description = details.get('Decree_Text_Col10')
            if description in (None, 'Not Found'):
                description = None

            issuing_date = parse_smc_date(row_data.get('Date'))
            due_period_days = parse_due_period_days(details.get('Decree_Text_Col6'))

            catalog_entry = catalog.get(normalize_decree_description(description), {})

            results.append({
                'decree_number': decree_number,
                'decree_description': description,
                'decree_total_value': _to_number(row_data.get('Decree_Total_Value')),
                'decree_value_left_website': _to_number(row_data.get('Decree_Value_Left')),
                'decree_status': row_data.get('Decree_Status'),
                'issuing_date': issuing_date,
                'decree_due_period_days': due_period_days,
                'decree_expiry_date': _compute_expiry_date(issuing_date, due_period_days),
                'treatment_plan_name': catalog_entry.get('treatment_plan_name'),
                'is_cycles': bool(catalog_entry.get('is_cycles')),
                'average_dose_value': catalog_entry.get('average_dose_value'),
                'is_supportive': bool(catalog_entry.get('is_supportive')),
            })
        except Exception as e:
            logging.error(f"[value-left] failed to parse a decree row for patient {national_id}: {e}")
            continue

    _classify_regimens(results)
    return results


def is_decree_expired(decree_expiry_date: Optional[str], as_of_iso: Optional[str] = None) -> Optional[bool]:
    """True/False, or None if decree_expiry_date is unknown (never
    guess an expiry status from missing data)."""
    if not decree_expiry_date:
        return None
    as_of_iso = as_of_iso or cairo_today_iso()
    return decree_expiry_date < as_of_iso


def evaluate_dose_coverage(decree: Dict, real_value_left: Optional[float],
                            as_of_iso: Optional[str] = None) -> Optional[bool]:
    """
    Single source of truth for the "can this patient dispense ONE MORE
    dose against this decree right now" check (your Q3/Q4 answers) --
    used identically by the on-demand lookup and the daily scan so
    they can never disagree.

    Returns:
        True  -- enough real value left for one more dose, and not expired.
        False -- insufficient (expired, OR real_value_left <= 500 for a
                  supportive/pain decree, OR real_value_left < that
                  decree's average_dose_value for a real medication
                  decree).
        None  -- can't tell: real_value_left is unknown, OR this is a
                  non-supportive decree with no average_dose_value yet
                  in decree_medication_catalog (needs_cutoff_value) --
                  never guessed at, surface as "no cutoff data" instead
                  of silently defaulting to True or False.

    Only meaningful for decree['regimen_status'] == 'current' --
    superseded/previous-cycle decrees aren't the ones a patient would
    actually be dispensed against, so callers should skip calling this
    for those (see queue_value_left_scan.py).
    """
    if real_value_left is None:
        return None

    if is_decree_expired(decree.get('decree_expiry_date'), as_of_iso):
        return False

    if decree.get('is_supportive'):
        return real_value_left > SUPPORTIVE_VALUE_FLOOR

    average_dose_value = decree.get('average_dose_value')
    if average_dose_value is None:
        return None
    return real_value_left >= average_dose_value
