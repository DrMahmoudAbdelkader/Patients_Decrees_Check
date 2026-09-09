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

LAYER 2 -- CROSS-DRUG CLINICAL EXCLUSIVITY (new)
----------------------------------------------------
The grouping above only ever compares a decree against OTHER decrees
of the exact same treatment_plan_name, so it cannot catch the case
where two DIFFERENT treatment plans are medically mutually exclusive
-- e.g. an anthracycline-based decree and a taxane-based decree can
both independently come out of Layer 1 as "current" (each is the
newest in its own group), even though a breast-cancer patient is never
actually on both cytotoxic backbones at once; the later-issued one
simply means the protocol was switched.

Layer 2 runs after Layer 1, and ONLY looks at whichever decrees Layer
1 already marked 'current'. It re-groups those survivors by the
catalog's exclusivity_group column (decree_medication_catalog ->
patient_decree_value's catalog cache), and within any group with more
than one 'current' survivor, keeps the one with the latest
issuing_date and demotes every other member of that group to
'superseded_by_protocol_shift' -- a status distinct from plain
'superseded' so you can tell "renamed/renewed same drug" apart from
"protocol was changed to a different drug in the same clinical slot"
at a glance.

exclusivity_group is deliberately opt-in per catalog row (NULL by
default): only treatment plans you've explicitly reviewed and tagged
as mutually exclusive participate in Layer 2. Zoladex, bone-modifying
agents, HER2-targeted therapy running alongside a chemo backbone,
supportive/antiemetic decrees, and anything not yet reviewed are left
ungrouped and pass through Layer 2 unchanged -- exactly like before
this revision.

Callers (lookup_patient_decree_value.py / queue_value_left_scan.py)
are responsible for computing real_value_left (needs the
Enhanced-Monitor pending-value join, a different data source) and for
comparing it against average_dose_value / the 500 EGP supportive floor
-- this module only supplies the classification + catalog fields those
comparisons need.

DYNAMIC (DEPOT+PARTNER) CUTOFF -- new
----------------------------------------------------
Some decrees bundle two dispensing cadences into one decree span --
the canonical case being Zoladex ("زولاديكس"): a depot shot due every
3 months, PLUS a monthly oral hormonal partner drug, inside the same
6-month decree. A single static average_dose_value can never be
correct for these: set it to depot+partner and every visit that only
needs the monthly partner drug is falsely flagged insufficient; set it
to partner-only and every visit that actually needs the depot shot is
falsely flagged as fine to come in.

evaluate_dose_coverage() now takes an optional `value_left_history`
argument -- the decree's own real_value_left time series, one point
per daily scan (already captured, unmodified, by
decree_value_left_daily_scan; see get_value_left_history() below). For
a decree with depot_interval_months set in the catalog, coverage is
checked against a cutoff DERIVED from that history rather than a fixed
number: the series' own day-over-day drops tell you when the last
depot shot actually happened (a big drop) versus a partner-only refill
(a small drop), so "is a depot shot due at the next visit" is answered
from the decree's real consumption pattern, not from calendar math off
issuing_date alone (which drifts as soon as a visit runs early/late).
Every ordinary decree (depot_interval_months IS NULL) is completely
unaffected and keeps using average_dose_value / SUPPORTIVE_VALUE_FLOOR
exactly as before.
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
VALUE_LEFT_HISTORY_TABLE = "decree_value_left_daily_scan"
SUPPORTIVE_VALUE_FLOOR = 500  # EGP -- your Q4 rule

# A day-over-day drop in real_value_left bigger than this fraction of
# a decree's own depot_value is treated as "a depot shot happened that
# day" rather than "a partner-only refill happened that day". 0.5 = a
# drop of more than half the depot's own value -- comfortably above a
# partner-only drop (Zoladex: depot ~5600, partner ~400, so a
# partner-only drop is ~7% of depot_value, nowhere near this line) and
# comfortably below a full depot+partner drop, so ordinary noise (a
# late/early visit, a small correction) can't flip the classification.
DEPOT_DROP_FRACTION = 0.5

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
            "decree_description,treatment_plan_name,reception_display_name,is_cycles,average_dose_value,is_supportive,"
            "exclusivity_group,financial_review_scope,depot_value,depot_interval_months,partner_value",
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


def _resolve_cross_drug_exclusivity(decrees: List[Dict]) -> None:
    """LAYER 2 -- mutates in place. Only ever looks at decrees Layer 1
    already marked 'current'. Re-groups those survivors by
    exclusivity_group (a catalog field, opt-in per treatment plan --
    see this module's docstring) and, within any group with more than
    one 'current' survivor, keeps the most recently issued one and
    demotes every other member of that group to
    'superseded_by_protocol_shift'.

    A decree whose catalog row has no exclusivity_group (the default)
    is left completely untouched here, whatever its status -- this
    pass can only ever demote a 'current', never promote or touch
    anything else.
    """
    current = [d for d in decrees if d.get("regimen_status") == "current"]

    groups: Dict[str, List[Dict]] = {}
    for d in current:
        group = d.get("exclusivity_group")
        if not group:
            continue
        groups.setdefault(group, []).append(d)

    for group, members in groups.items():
        if len(members) <= 1:
            continue
        # sort by issuing_date desc; missing date sorts last (oldest),
        # same convention as Layer 1 -- an unknown date should never
        # accidentally look like the newest one.
        members_sorted = sorted(members, key=lambda d: d.get("issuing_date") or "", reverse=True)
        for old in members_sorted[1:]:
            old["regimen_status"] = "superseded_by_protocol_shift"


def _classify_regimens(decrees: List[Dict]) -> None:
    """Mutates each decree dict in place, adding regimen_status:
    'current' | 'superseded' | 'previous_cycle' | 'unmapped' |
    'superseded_by_protocol_shift'.

    LAYER 1 (unchanged): grouped by treatment_plan_name; newest
    issuing_date per group wins 'current'. A decree with no
    issuing_date sorts as the OLDEST in its group (never wins
    'current' over a decree with a known date) -- an unknown date
    should never accidentally look like the newest one.

    LAYER 2 (new): see _resolve_cross_drug_exclusivity() above -- runs
    after Layer 1, resolves mutually-exclusive drugs in different
    treatment_plan_name groups (e.g. anthracycline vs taxane) that
    Layer 1 alone can't compare against each other.
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

    _resolve_cross_drug_exclusivity(decrees)


def get_patient_decree_value_details(session: smc.SMCSession, national_id: str) -> List[Dict]:
    """
    `session` must already be logged in. Returns one dict per decree
    found on the patient's SMC decrees page:
        decree_number, decree_description, decree_total_value,
        decree_value_left_website, decree_status, issuing_date,
        decree_due_period_days, decree_expiry_date,
        treatment_plan_name, reception_display_name, is_cycles,
        average_dose_value, is_supportive, exclusivity_group,
        financial_review_scope, depot_value, depot_interval_months,
        partner_value, regimen_status

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
                'reception_display_name': catalog_entry.get('reception_display_name'),
                'is_cycles': bool(catalog_entry.get('is_cycles')),
                'average_dose_value': catalog_entry.get('average_dose_value'),
                'is_supportive': bool(catalog_entry.get('is_supportive')),
                'exclusivity_group': catalog_entry.get('exclusivity_group'),
                'financial_review_scope': catalog_entry.get('financial_review_scope') or 'in_scope',
                'depot_value': catalog_entry.get('depot_value'),
                'depot_interval_months': catalog_entry.get('depot_interval_months'),
                'partner_value': catalog_entry.get('partner_value'),
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


def get_value_left_history(decree_number: str) -> List[Dict]:
    """Returns this decree's own real_value_left time series --
    [{'scan_date': iso, 'real_value_left': float}, ...] sorted oldest
    to newest -- read straight from decree_value_left_daily_scan,
    which already upserts on (scan_date, patient_id, decree_number)
    and therefore already IS an append-only per-day series; nothing
    new is written here, this only reads what queue_value_left_scan.py
    has been accumulating.

    Returns [] if the table/decree has no history yet (e.g. a
    brand-new decree that hasn't been through a daily scan) -- callers
    must treat that the same as "can't infer a depot schedule yet".
    """
    try:
        rows = sb.fetch_all(
            VALUE_LEFT_HISTORY_TABLE,
            "scan_date,real_value_left",
            f"decree_number=eq.{decree_number}&order=scan_date.asc",
        )
    except Exception as e:
        logging.warning(f"[value-left] could not load history for decree {decree_number}: {e}")
        return []
    return [r for r in rows if r.get("real_value_left") is not None]


def infer_depot_events(value_left_history: List[Dict], depot_value: float) -> List[Dict]:
    """Walks a decree's real_value_left series day-over-day and
    classifies every drop as 'depot_plus_partner' (a big drop -- the
    depot shot was dispensed, alongside or without the partner drug
    that same visit) or 'partner_only' (a small drop -- only the
    monthly partner drug was dispensed). See DEPOT_DROP_FRACTION above
    for the threshold. A day with no drop (or an increase, e.g. a
    correction) is skipped -- it's not a dispensing event.
    """
    events = []
    threshold = depot_value * DEPOT_DROP_FRACTION
    for prev, nxt in zip(value_left_history, value_left_history[1:]):
        drop = (prev.get("real_value_left") or 0) - (nxt.get("real_value_left") or 0)
        if drop <= 0:
            continue
        kind = "depot_plus_partner" if drop >= threshold else "partner_only"
        events.append({"date": nxt.get("scan_date"), "drop": drop, "kind": kind})
    return events


def _months_between(start_iso: str, end_iso: str) -> float:
    """Approximate whole-plus-fractional months between two ISO dates,
    good enough for a "is a depot dose roughly due" check -- not
    calendar-exact, deliberately simple."""
    d0 = datetime.strptime(start_iso, "%Y-%m-%d")
    d1 = datetime.strptime(end_iso, "%Y-%m-%d")
    return (d1 - d0).days / 30.4


def expected_cutoff_for_visit(decree: Dict, visit_date_iso: str,
                               value_left_history: List[Dict]) -> Optional[float]:
    """The dynamic depot+partner cutoff (see this module's docstring).
    Returns None if this decree isn't a combo-cadence decree at all
    (depot_interval_months unset) -- callers fall back to the static
    average_dose_value / SUPPORTIVE_VALUE_FLOOR path in that case.

    Anchors the "when is the next depot dose due" question off the
    LAST DETECTED depot event in this decree's own consumption history
    -- not off issuing_date + fixed calendar math -- so a visit that
    ran early or late doesn't throw the schedule off. Falls back to
    issuing_date only when no depot event has fired yet on this decree
    (e.g. it's brand new and hasn't had its first dispense scanned).
    """
    depot_interval = decree.get("depot_interval_months")
    if not depot_interval:
        return None

    depot_value = decree.get("depot_value") or 0
    partner_value = decree.get("partner_value") or 0

    events = infer_depot_events(value_left_history, depot_value)
    depot_events = [e for e in events if e["kind"] == "depot_plus_partner"]

    anchor = max((e["date"] for e in depot_events), default=None) or decree.get("issuing_date")
    if not anchor:
        # No history and no issuing_date -- can't derive anything;
        # caller should treat this the same as "needs_cutoff_value".
        return None

    months_since_depot = _months_between(anchor, visit_date_iso)
    depot_due = months_since_depot >= depot_interval

    return (depot_value + partner_value) if depot_due else partner_value


def evaluate_dose_coverage(decree: Dict, real_value_left: Optional[float],
                            as_of_iso: Optional[str] = None,
                            value_left_history: Optional[List[Dict]] = None) -> Optional[bool]:
    """
    Single source of truth for the "can this patient dispense ONE MORE
    dose against this decree right now" check (your Q3/Q4 answers) --
    used identically by the on-demand lookup and the daily scan so
    they can never disagree.

    Returns:
        True  -- enough real value left for one more dose, and not expired.
        False -- insufficient: expired, OR real_value_left is below this
                  decree's threshold (see below).
        None  -- can't tell: real_value_left is unknown, OR this is a
                  non-supportive decree with no average_dose_value yet
                  in decree_medication_catalog (needs_cutoff_value) --
                  never guessed at, surface as "no cutoff data" instead
                  of silently defaulting to True or False.

    Threshold used, in order of precedence:
        0. If this decree bundles a depot+partner cadence
           (depot_interval_months is set in the catalog -- e.g.
           Zoladex), the cutoff is DERIVED per-visit from the decree's
           own value_left_history via expected_cutoff_for_visit()
           instead of any fixed number -- see that function and this
           module's docstring. Falls through to steps 1-3 below only
           if that derivation can't produce an answer (no history and
           no issuing_date yet).
        1. decree_medication_catalog's average_dose_value for THIS raw
           description, whenever it's set -- including 0. Your catalog
           carries a different cutoff per decree (most supportive
           decrees are 0, but e.g. the pain/anti-emetic supportive
           decree is deliberately 500), so a per-decree 0 always means
           "no floor, any non-negative real value left is fine",
           regardless of whether the decree is flagged is_supportive.
        2. Only when average_dose_value is unset (None) for a
           supportive decree: the SUPPORTIVE_VALUE_FLOOR fallback,
           so an old/unmapped supportive decree that hasn't had a
           per-decree cutoff filled in yet still gets a sane default
           instead of silently passing.
        3. Only when average_dose_value is unset for a non-supportive
           decree: None (unknown -- needs_cutoff_value, surfaced for a
           human to fill in rather than guessed at).

    Only meaningful for decree['regimen_status'] == 'current' --
    superseded/previous-cycle/superseded_by_protocol_shift decrees
    aren't the ones a patient would actually be dispensed against, so
    callers should skip calling this for those (see
    queue_value_left_scan.py).
    """
    if real_value_left is None:
        return None

    if is_decree_expired(decree.get('decree_expiry_date'), as_of_iso):
        return False

    if decree.get('depot_interval_months'):
        visit_date = as_of_iso or cairo_today_iso()
        history = value_left_history if value_left_history is not None \
            else get_value_left_history(decree.get('decree_number'))
        dynamic_cutoff = expected_cutoff_for_visit(decree, visit_date, history)
        if dynamic_cutoff is not None:
            return real_value_left >= dynamic_cutoff
        # else: no history and no issuing_date to anchor on yet --
        # fall through to the static path below rather than guessing.

    average_dose_value = decree.get('average_dose_value')
    if average_dose_value is not None:
        return real_value_left >= average_dose_value

    if decree.get('is_supportive'):
        return real_value_left > SUPPORTIVE_VALUE_FLOOR

    return None
