#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bmt_billing_tracker.py -- BMT (bone marrow transplant) State-Fund billing tracker
=============================================================================
Runs in the SAME GitHub workflow run as icu_billing_tracker.py (own job, see
icu-billing-tracker.yml) and re-uses that file's SMC plumbing (smc_request, receipts parser,
work-day arithmetic, Supabase helpers).

WHAT IT TRACKS
  Every BMT admission (department "زرع النخاع") of a State-Fund patient
  (patient_admissions.financial_category_id = 4) discharged on/after BMT_TRACK_FROM
  (default 2026-07-01).

  A BMT bundle = TWO issued decrees on the patient's SMC page:
        125,000  ("... لزرع نخاع ذاتي (قرار اول)")
      + 100,000  ("... لزرع نخاع ذاتي (قرار ثاني)")      = 225,000
  The user gets BMT_BILL_DAYS (7) working days after discharge (Fridays skipped, like ICU)
  to finish the billing documents and create the bill(s) on SMC so BOTH decrees are
  fully consumed.

  NO request is ever investigated. The script goes straight to the decrees:
    POST /smc/Decrees/DecreesSearch (NationalID = patient)   -> every decree of the patient
    GET  /smc/DecreeTreatmentProcedure/Create/{decree}       -> procedure text (role check only)
    POST /smc//HospDecreeReceipts/GetHospitalDecreeReceipts  -> bills (receipts) of a decree

HOW A DECREE IS PICKED
  * decree type = state fund ("نفقة دولة") whose procedure text mentions bone marrow ("نخاع"); the text also says which
    one it is: "قرار اول" -> first, "قرار ثاني" -> second. NO date window and NO value rule: the decree may have been
    issued any time (even before check-in) and have any value (only side decrees below BMT_FALLBACK_MIN_VALUE are
    not read). With several candidates (readmission) each admission takes the decree dated nearest to its check-in.

WHEN IS A DECREE "CONSUMED" / THE PATIENT "BILLED"
  * counted receipts (status code NOT starting with R) of the decree reach its value.
  * billed date of a decree = registration date of the receipt that reached the value.
  * patient billed = BOTH decrees consumed; the billing date = the later of the two.
    One decree consumed only = still unbilled (the whole bundle stays in the accrual).
  * closed rows are never re-read; open rows are re-read on every run until closed.

MONTHLY ACCOUNTING (computed in the module and logged here from the same rows)
  * month of a patient = month of discharge.
  * bundle value = sum of the two decree values (nominal 125,000 + 100,000 for a missing decree).
  * accrual roll-forward per month = carried in + discharged this month - billed this month
    = carried out (unbilled, tracked until both decrees are consumed).

CHANGES (v2)
  * SYNC WITH THE ADMISSIONS MODULE: every run reconciles ALL rows of bmt_stay_tracking against the live
    patient_admissions rows. A row whose admission was deleted, re-opened (check-out cleared), moved before
    BMT_TRACK_FROM, or whose financial category is no longer the state fund (4) is marked
    excluded_reason/excluded_at and disappears from the audit; it comes back automatically (and is re-read) if
    the category is corrected back. Corrected check-in/check-out dates force a re-read of closed rows too.
  * DECREES BY DESCRIPTION: one decree of a BMT bundle is always 100,000; the other is usually 125,000 but may be
    any value (e.g. 70,000). When a role is not found by its regular value, the patient's other state-fund
    decrees (value >= BMT_FALLBACK_MIN_VALUE) inside the admission window are read and the one whose procedure
    text mentions "نخاع" is taken (decree_source = 'description'). Each patient stores decrees_count (1 or 2 -
    one means the second decree is still to be issued) and decrees_total.
  * EXCEPTIONS: a patient with BOTH decrees issued whose total is below 225,000 is an exception: tracked and
    documented, but excluded from the accrual.
  * PER-DECREE DETAIL: consumed value, remaining value, number of receipts, first/last receipt date and days from
    discharge, per decree; plus decree wait / billing work split used for bottleneck analysis.
  * VALUE LEDGER: money-based monthly accrual (225,000 per non-exception discharge, reduced by the receipts
    registered each month, with the origin month of every billed amount) next to the patient-count roll-forward.

Usage:
    python bmt_billing_tracker.py            # normal run
    python bmt_billing_tracker.py --selftest # offline logic tests, no network
"""

import os
import re
import sys
import time
import logging
import argparse
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import icu_billing_tracker as icu      # SMC plumbing + helpers shared with the ICU tracker
import smc_session as smc

from icu_billing_tracker import (add_work_days, work_days_between, ar2en, digits, to_date, to_float, iso,
                                 norm_ar, now_iso, today_cairo, upsert_uniform, fetch_in, EPS)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("bmt_billing_tracker")

# ---------------------------------------------------------------------
# Configuration (all overridable from the workflow's env)
# ---------------------------------------------------------------------
TRACK_FROM = os.environ.get("BMT_TRACK_FROM") or os.environ.get("ICU_TRACK_FROM") or "2026-07-01"
BMT_DEPARTMENT_NAME = os.environ.get("BMT_DEPARTMENT_NAME", "زرع النخاع")     # departments.name_ar (admissions.js SUPPORTED_DEPTS.BMT)
FIN_CATEGORY_ID = int(os.environ.get("BMT_FIN_CATEGORY_ID", "4"))              # state fund
FIRST_VALUE = float(os.environ.get("BMT_FIRST_DECREE_VALUE", "125000"))
SECOND_VALUE = float(os.environ.get("BMT_SECOND_DECREE_VALUE", "100000"))
BILL_DAYS_AFTER_DISCHARGE = int(os.environ.get("BMT_BILL_DAYS", "7"))           # working days, Fridays skipped
FIRST_LOOKBACK_DAYS = int(os.environ.get("BMT_FIRST_LOOKBACK_DAYS", "270"))     # FIRST (125,000) decree is issued when the transplant is approved, i.e. MONTHS before check-in (observed Mar-Jun for Jul-Sep admissions)
CONTEXT_DAYS = int(os.environ.get("BMT_CONTEXT_DAYS", "120"))                   # earlier BMT admissions still loaded (they bound the decree window)
SEARCH_WINDOW_DAYS = int(os.environ.get("BMT_SEARCH_WINDOW_DAYS", "175"))      # SMC screen refuses > 6 months between dateFrom and dateTo
SEARCH_MAX_PAGES = int(os.environ.get("BMT_SEARCH_MAX_PAGES", "12"))
DECREE_SOURCE = os.environ.get("BMT_DECREE_SOURCE", "1")                        # decreeSource value the SMC decree screen sends
STANDARD_BUNDLE = float(os.environ.get("BMT_STANDARD_BUNDLE", "225000"))        # accrual per patient; a patient with both decrees issued but a lower total is an exception
ACCRUE_EXCEPTIONS = os.environ.get("BMT_ACCRUE_EXCEPTIONS", "1") != "0"         # 1 = a patient is accrued at his ACTUAL decree total (exceptions included); 0 = exceptions accrue nothing
FALLBACK_MIN_VALUE = float(os.environ.get("BMT_FALLBACK_MIN_VALUE", "20000"))   # decrees below this are never taken by description (1,000 / 5,000 side decrees)
FALLBACK_MAX_PAGES = int(os.environ.get("BMT_FALLBACK_MAX_PAGES", "12"))         # decree pages read per patient while looking for the description match
DESC_KEYWORD = os.environ.get("BMT_DESC_KEYWORD", "نخاع")                        # words that mark a bone-marrow decree ("زرع نخاع")
REQUEST_DELAY = icu.REQUEST_DELAY

TRACKING_TABLE = "bmt_stay_tracking"
RECEIPTS_TABLE = "bmt_stay_receipts"
RUNS_TABLE = "bmt_tracker_runs"

ROLES = ("first", "second")
ROLE_VALUE = {"first": FIRST_VALUE, "second": SECOND_VALUE}
ROLE_COL = {"first": "dec1", "second": "dec2"}


# =====================================================================
# Pure helpers
# =====================================================================
def role_of_value(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    for role, v in ROLE_VALUE.items():
        if abs(float(value) - v) < 0.5:
            return role
    return None


def text_role(procedure_text: Optional[str]) -> Optional[str]:
    """'... لزرع نخاع ذاتي (قرار ثاني)' -> 'second', '(قرار اول)' -> 'first', else None."""
    n = norm_ar(procedure_text)
    if re.search(r"ثان[يى]", n):
        return "second"
    if re.search(r"(?<![\u0621-\u064A])(ال)?اول(?![\u0621-\u064A])", n):
        return "first"
    return None


def is_state_fund_decree(d: dict) -> bool:
    t = norm_ar(d.get("decree_type"))
    return (not t) or ("دولة" in t)


def month_of(d: Optional[date]) -> Optional[str]:
    return d.strftime("%Y-%m") if d else None


def first_of_month(m: Optional[str]) -> Optional[str]:
    return f"{m}-01" if m else None


# =====================================================================
# SMC HTML parsing
# =====================================================================
def parse_decree_search(html: str) -> Tuple[List[dict], Optional[int]]:
    """Rows of the decree search table + total page count ('Page 1 of 10')."""
    pages = None
    m = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", html or "")
    if m:
        pages = int(m.group(2))
    soup = BeautifulSoup(html or "", "html.parser")
    table = soup.find("table", {"id": "requestTable"})
    rows: List[dict] = []
    if not table:
        return rows, pages
    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 10:
            continue
        link = tds[0].find("a")
        num = digits(link.get_text(strip=True) if link else tds[0].get_text(strip=True))
        if not num:
            continue

        def cell(i):
            return re.sub(r"\s+", " ", tds[i].get_text(" ", strip=True))
        rows.append({"decree_number": num, "patient_name": cell(1), "patient_nid": digits(cell(2)),
                     "decree_date": to_date(ar2en(cell(3))), "decree_type": cell(4),
                     "decree_value": to_float(cell(5)), "discount_pct": to_float(cell(6)),
                     "remaining": to_float(cell(7)), "closed_text": cell(8), "status_text": cell(9)})
    return rows, pages


# =====================================================================
# SMC I/O
# =====================================================================
def search_windows(oldest_check_in: date, today: date) -> List[Tuple[date, date]]:
    """SMC's screen accepts at most 6 months between dateFrom and dateTo (observed: with a national id the server
    returned decrees outside the window anyway). Chain windows from the oldest check-in up to tomorrow."""
    out, start = [], oldest_check_in - timedelta(days=max(7, FIRST_LOOKBACK_DAYS))
    while True:
        end = min(start + timedelta(days=SEARCH_WINDOW_DAYS), today + timedelta(days=1))
        out.append((start, end))
        if end >= today + timedelta(days=1):
            return out
        start = end


def fetch_decrees_by_nid(sess, nid: str, windows: List[Tuple[date, date]]) -> Optional[List[dict]]:
    """Every decree SMC lists for the patient. None = could not be read (caller keeps the previous result)."""
    url = f"{smc.BASE_URL}/smc/Decrees/DecreesSearch"
    found: Dict[str, dict] = {}
    for w_i, (d_from, d_to) in enumerate(windows):
        for page in range(1, SEARCH_MAX_PAGES + 1):
            payload = {"decreeID": "", "NationalID": nid, "PatientName": "", "dateFrom": d_from.isoformat(),
                       "dateTo": d_to.isoformat(), "Retrieved": "N", "decreeStatus": "", "decreeSource": DECREE_SOURCE,
                       "stoppedDecree": "N", "page": str(page)}
            resp = icu.smc_request(sess, "POST", url, data=payload,
                                   headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                            "X-Requested-With": "XMLHttpRequest",
                                            "Referer": f"{smc.BASE_URL}/smc/Decrees"})
            if resp is None:
                return None
            rows, pages = parse_decree_search(resp.text)
            fresh = [r for r in rows if r["patient_nid"] == nid and r["decree_number"] not in found]
            for r in fresh:
                found[r["decree_number"]] = r
            if not fresh or (pages is not None and page >= pages):
                break
            time.sleep(REQUEST_DELAY)
        # the server ignores the date window when a national id is given: one window already returned everything
        if any(r["decree_date"] and (r["decree_date"] < d_from or r["decree_date"] > d_to) for r in found.values()):
            break
    return list(found.values())


# =====================================================================
# Matching a patient's decrees to his BMT admissions   (pure)
# =====================================================================
def has_bone_marrow_text(d: dict) -> bool:
    return DESC_KEYWORD in norm_ar(d.get("procedure_text") or "")


def bmt_candidates(decrees: List[dict], used: set, need_text: bool) -> List[dict]:
    """State-fund decrees of the patient that may be a BMT decree. NO date window and NO value rule: a BMT decree is
    recognised only by its description. FALLBACK_MIN_VALUE only keeps the small side decrees (1,000 / 5,000 ...) out so
    that their pages are not read. need_text=True -> only decrees whose page text mentions bone marrow ("نخاع")."""
    out = []
    for d in decrees:
        v = d.get("decree_value")
        if v is None or not d.get("decree_date") or float(v) < FALLBACK_MIN_VALUE or not is_state_fund_decree(d) or d["decree_number"] in used:
            continue
        if need_text and not has_bone_marrow_text(d):
            continue
        out.append(d)
    return out


def match_bmt_decrees(admissions: List[dict], decrees: List[dict],
                      pinned: Optional[Dict[int, Dict[str, dict]]] = None,
                      use_description: bool = True) -> Dict[int, dict]:
    """admissions: ALL BMT admissions of ONE patient. decrees: everything SMC lists for him (procedure_text read).
    pinned: {admission_id: {role: decree dict}} already known from earlier runs (never re-matched).
    A decree is a BMT decree when it is state fund and its description mentions bone marrow ("زرع نخاع"); the description
    also says which one it is: "قرار اول" -> first, "قرار ثاني" -> second. Dates and values do not matter. When several
    decrees carry the same role (readmission / second transplant) each admission takes the one dated nearest to its
    check-in. A bone-marrow decree without any first/second wording fills the role still free (100,000 -> second, else first).
    -> {admission_id: {"first": dec|None, "second": dec|None, "notes": [..]}}  (new matches carry source='description')"""
    pinned = pinned or {}
    adms = sorted(admissions, key=lambda s: (s["check_in_date"], s["check_out_date"] or date.max, s["admission_id"]))
    used = {d["decree_number"] for per in pinned.values() for d in per.values() if d}
    out: Dict[int, dict] = {}
    for s in adms:
        res = {"first": None, "second": None, "notes": []}
        for role in ROLES:
            known = (pinned.get(s["admission_id"]) or {}).get(role)
            if known:
                res[role] = known
                continue
            pool = [d for d in bmt_candidates(decrees, used, True) if text_role(d.get("procedure_text")) == role]
            if not pool:
                continue
            pool.sort(key=lambda d: (abs((d["decree_date"] - s["check_in_date"]).days), d["decree_date"], d["decree_number"]))
            res[role] = dict(pool[0], source="description")
            used.add(pool[0]["decree_number"])
            if len(pool) > 1:
                res["notes"].append(f"{len(pool)} bone-marrow decrees worded as '{'قرار اول' if role == 'first' else 'قرار ثاني'}' - the one dated "
                                    f"nearest to check-in ({pool[0]['decree_number']}) is used")
        out[s["admission_id"]] = res
    for s in adms:                                           # second pass: bone-marrow decrees without first/second wording
        res = out[s["admission_id"]]
        for role in [r for r in ROLES if not res[r]]:
            pool = [d for d in bmt_candidates(decrees, used, True) if text_role(d.get("procedure_text")) is None
                    and (role_of_value(d["decree_value"]) in (None, role))]
            if not pool:
                continue
            pool.sort(key=lambda d: (abs((d["decree_date"] - s["check_in_date"]).days), d["decree_date"], d["decree_number"]))
            res[role] = dict(pool[0], source="description")
            used.add(pool[0]["decree_number"])
            res["notes"].append(f"{role} decree {pool[0]['decree_number']} has no first/second wording - role taken from its value")
    for s in adms:
        res = out[s["admission_id"]]
        for role in ROLES:
            if not res[role]:
                n_read = sum(1 for d in decrees if d.get("procedure_text") is not None and d.get("decree_value") is not None
                             and float(d["decree_value"]) >= FALLBACK_MIN_VALUE and is_state_fund_decree(d))
                res["notes"].append(f"no state-fund decree of this patient says bone marrow + '{'قرار اول' if role == 'first' else 'قرار ثاني'}' "
                                    f"({n_read} state-fund decree(s) of value >= {FALLBACK_MIN_VALUE:,.0f} read)")
        f, sc = res["first"], res["second"]
        if f and sc and sc.get("decree_date") and f.get("decree_date") and sc["decree_date"] < f["decree_date"]:
            res["notes"].append("the 'second' decree is dated before the 'first' decree")
    return out


def decrees_to_read(admissions: List[dict], decrees: List[dict], matches: Dict[int, dict],
                    pinned: Optional[Dict[int, Dict[str, dict]]] = None) -> List[dict]:
    """The decree pages that still have to be read (procedure text unknown) because some admission lacks a decree.
    Every state-fund decree >= FALLBACK_MIN_VALUE is read, whatever its date; capped at FALLBACK_MAX_PAGES (oldest first)."""
    pinned = pinned or {}
    used = {d["decree_number"] for per in pinned.values() for d in per.values() if d}
    for m in matches.values():
        used |= {m[r]["decree_number"] for r in ROLES if m.get(r)}
    if all((matches.get(s["admission_id"]) or {}).get("first") and (matches.get(s["admission_id"]) or {}).get("second") for s in admissions):
        return []
    todo = [d for d in bmt_candidates(decrees, used, False) if d.get("procedure_text") is None]
    return sorted(todo, key=lambda d: (d["decree_date"], d["decree_number"]))[:FALLBACK_MAX_PAGES]


# =====================================================================
# PURE EVALUATION (no I/O) -- the single source of BMT workflow logic
# =====================================================================
def _consumption(receipts: List[dict], value: float) -> Tuple[bool, Optional[date], float]:
    """(consumed, date the counted receipts first reached `value`, counted total)."""
    cum, reached_on, reached = 0.0, None, False
    for rc in sorted([r for r in receipts if r.get("counted", True)],
                     key=lambda r: (r.get("registered_on") or date.max, str(r.get("receipt_id")))):
        cum += float(rc.get("value") or 0)
        if not reached and cum >= value - EPS:
            reached, reached_on = True, rc.get("registered_on")
    return reached, reached_on, round(cum, 2)


def evaluate_bmt(stay: dict, decs: Dict[str, Optional[dict]], receipts: Dict[str, List[dict]], today: date) -> dict:
    """stay: {check_out_date}. decs: {"first": dec|None, "second": dec|None} (decree_number/date/value/procedure_text/source).
    receipts: {decree_number: [receipt dicts]}. Returns the flat row stored in bmt_stay_tracking."""
    co: date = stay["check_out_date"]
    due = add_work_days(co, BILL_DAYS_AFTER_DISCHARGE)
    notes: List[str] = []
    row: dict = {"discharge_month": first_of_month(month_of(co)), "due_date": due}
    billed_dates: List[date] = []
    consumed_n = present_n = 0
    billed_sum = bundle = present_total = 0.0
    decree_dates: List[date] = []
    first_receipts: List[date] = []
    for role in ROLES:
        p, d = ROLE_COL[role], decs.get(role)
        val = float(d["decree_value"]) if d and d.get("decree_value") is not None else ROLE_VALUE[role]
        bundle += val
        row.update({f"{p}_number": d["decree_number"] if d else None, f"{p}_date": d.get("decree_date") if d else None,
                    f"{p}_value": val if d else None, f"{p}_procedure": d.get("procedure_text") if d else None,
                    f"{p}_source": (d.get("source") or "value") if d else None,
                    f"{p}_billed": None, f"{p}_billed_on": None, f"{p}_remaining": None, f"{p}_receipts_n": None,
                    f"{p}_first_receipt_on": None, f"{p}_last_receipt_on": None, f"{p}_days_to_bill": None})
        if not d:
            continue
        present_n += 1
        present_total += val
        if d.get("decree_date"):
            decree_dates.append(d["decree_date"])
        rcs = [r for r in receipts.get(d["decree_number"], []) if r.get("counted", True)]
        ok, on, total = _consumption(rcs, val)
        consumed_value = min(total, val)
        row[f"{p}_billed"] = consumed_value if ok else total
        row[f"{p}_remaining"] = round(max(0.0, val - consumed_value), 2)
        row[f"{p}_receipts_n"] = len(rcs)
        dates = sorted(r["registered_on"] for r in rcs if r.get("registered_on"))
        if dates:
            row[f"{p}_first_receipt_on"], row[f"{p}_last_receipt_on"] = dates[0], dates[-1]
            first_receipts.append(dates[0])
        billed_sum += consumed_value
        if ok:
            if on is None:
                on = today
                notes.append(f"decree {d['decree_number']}: consumed but the receipt date is unreadable - today is used")
            consumed_n += 1
            billed_dates.append(on)
            row[f"{p}_billed_on"] = on
            row[f"{p}_days_to_bill"] = (on - co).days
        if total > val + EPS:
            notes.append(f"decree {d['decree_number']}: receipts ({total:g}) exceed the decree value ({val:g})")
        tr = text_role(d.get("procedure_text"))
        if d.get("procedure_text") is not None:
            if tr and tr != role:
                notes.append(f"decree {d['decree_number']} (value {val:g}) is worded as "
                             f"'{'قرار اول' if tr == 'first' else 'قرار ثاني'}' - check the pairing")
            if DESC_KEYWORD not in norm_ar(d.get("procedure_text")):
                notes.append(f"decree {d['decree_number']}: procedure text does not mention bone marrow")

    fully = present_n == 2 and consumed_n == 2
    billed_on = max(billed_dates) if fully else None
    if fully:
        delay = work_days_between(due, billed_on)
    else:
        delay = work_days_between(due, today)
    if present_n == 0:
        stage = "awaiting_decrees"
    elif present_n == 1:
        stage = "decree_missing"
    elif fully:
        stage = "closed"
    elif consumed_n == 1 or billed_sum > EPS:
        stage = "partially_billed"
    else:
        stage = "awaiting_billing"

    # exception = both decrees issued but their total is below the standard 225,000 -> tracked, never accrued
    is_exception = present_n == 2 and present_total < STANDARD_BUNDLE - EPS
    if present_n == 2 and present_total > STANDARD_BUNDLE + EPS:
        notes.append(f"decrees total {present_total:g} is above the standard {STANDARD_BUNDLE:g} - accrual stays at the standard value")
    # bottleneck split: discharge -> (decrees issued) -> ready -> first receipt -> last receipt
    last_decree = max(decree_dates) if present_n == 2 and len(decree_dates) == 2 else None
    ready = max(co, last_decree) if last_decree else None
    ref = billed_on or today
    if delay > 0:
        late_cause = "decree_wait" if (ready is None or ref <= add_work_days(ready, BILL_DAYS_AFTER_DISCHARGE)) else "billing"
    else:
        late_cause = None
    row.update({
        "bundle_value": round(bundle, 2), "billed_value": round(billed_sum, 2),
        "remaining_value": round(max(0.0, bundle - billed_sum), 2),
        "decrees_count": present_n, "decrees_total": round(present_total, 2) if present_n else None,
        "is_exception": is_exception, "accrual_value": 0.0 if (is_exception and not ACCRUE_EXCEPTIONS) else round(bundle, 2),
        "last_decree_date": last_decree, "ready_date": ready,
        "decree_wait_days": (ready - co).days if ready else None,
        "billing_work_days": (billed_on - ready).days if (billed_on and ready) else None,
        "first_receipt_on": min(first_receipts) if first_receipts else None,
        "late_cause": late_cause,
        "billed_on": billed_on, "billing_month": first_of_month(month_of(billed_on)),
        "days_to_bill": (billed_on - co).days if billed_on else None,
        "delay_days": delay, "is_late": delay > 0,
        "status": "closed" if fully else "open", "stage": stage,
        "notes": "; ".join(notes) or None,
    })
    return row


# =====================================================================
# Reconciliation with the admissions module  (pure)
# =====================================================================
def reconcile_tracking(tracked: List[dict], live_by_id: Dict[int, dict], track_from: date) -> Dict[int, Optional[str]]:
    """tracked: rows of bmt_stay_tracking. live_by_id: current patient_admissions rows (admission_id -> stay).
    -> {admission_id: reason it must leave the audit | None if it still belongs}."""
    out: Dict[int, Optional[str]] = {}
    for t in tracked:
        aid = t["admission_id"]
        s = live_by_id.get(aid)
        if s is None:
            reason = "admission no longer found among the BMT admissions (deleted, moved or without a national id)"
        elif not s.get("check_out_date"):
            reason = "patient is no longer discharged (check-out was cleared in the admissions module)"
        elif s["check_out_date"] < track_from:
            reason = f"check-out was corrected to {s['check_out_date']}, before the tracking start {track_from}"
        elif not qualifies(s):
            c = s.get("financial_category_id")
            reason = f"financial category was changed to {c if c is not None else 'none'} (state fund = {FIN_CATEGORY_ID})"
        else:
            reason = None
        out[aid] = reason
    return out


# =====================================================================
# Monthly accounting (same algorithm as the module's JS)  (pure)
# =====================================================================
def _eff_billed(r: dict) -> Optional[date]:
    b = r.get("billed_on")
    return None if not b else max(b, r["check_out_date"])


def rollforward(rows: List[dict], today: date) -> List[dict]:
    """rows: {check_out_date, billed_on|None, bundle_value}. One dict per month, first discharge month .. today's month.
    Exceptions (is_exception) are not accrued and are skipped here."""
    rows = [r for r in rows if not r.get("is_exception")]
    if not rows:
        return []
    months, m = [], month_of(min(r["check_out_date"] for r in rows))
    cur = month_of(today)
    while m <= cur:
        months.append(m)
        y, mo = int(m[:4]), int(m[5:])
        m = f"{y + (mo == 12)}-{(mo % 12) + 1:02d}"
    out = []
    for m in months:
        start = date(int(m[:4]), int(m[5:]), 1)
        opening = [r for r in rows if r["check_out_date"] < start and (not _eff_billed(r) or _eff_billed(r) >= start)]
        new = [r for r in rows if month_of(r["check_out_date"]) == m]
        billed = [r for r in rows if _eff_billed(r) and month_of(_eff_billed(r)) == m]
        by_origin: Dict[str, List[dict]] = {}
        for r in billed:
            by_origin.setdefault(month_of(r["check_out_date"]), []).append(r)
        v = lambda xs: round(sum(float(x.get("accrual_value") or x.get("bundle_value") or 0) for x in xs), 2)
        out.append({"month": m,
                    "opening_n": len(opening), "opening_value": v(opening),
                    "new_n": len(new), "new_value": v(new),
                    "billed_n": len(billed), "billed_value": v(billed),
                    "billed_by_origin": {o: (len(x), v(x)) for o, x in sorted(by_origin.items())},
                    "closing_n": len(opening) + len(new) - len(billed),
                    "closing_value": round(v(opening) + v(new) - v(billed), 2)})
    return out


def _month_end(m: str) -> date:
    y, mo = int(m[:4]), int(m[5:])
    return (date(y + (mo == 12), (mo % 12) + 1, 1) - timedelta(days=1))


def _slots(r: dict) -> List[dict]:
    """The decrees an admission accrues: every ISSUED decree at its real value + a nominal slot for each decree not issued yet
    (125,000 for a missing first, 100,000 for a missing second). Sum of the slots = the patient's accrual."""
    if "dec1_number" not in r and "dec2_number" not in r:             # legacy row: a single lump accrual
        v = float(r.get("accrual_value") or 0)
        return [{"key": None, "value": v, "issued_on": None, "issued": True}] if v > EPS else []
    out = []
    for role in ROLES:
        p = ROLE_COL[role]
        if r.get(f"{p}_number") and r.get(f"{p}_value") is not None:
            out.append({"key": str(r[f"{p}_number"]), "value": float(r[f"{p}_value"]),
                        "issued_on": to_date(r.get(f"{p}_date")) if r.get(f"{p}_date") else None, "issued": True})
        else:
            out.append({"key": None, "value": ROLE_VALUE[role], "issued_on": None, "issued": False})
    if r.get("is_exception") and not ACCRUE_EXCEPTIONS:
        return []
    return out


def value_ledger(rows: List[dict], receipts: List[dict], today: date) -> List[dict]:
    """MONEY-based accrual on the patient's REAL decree total.
    rows: tracking rows {admission_id, check_out_date, dec1_/dec2_ number/value/date, closed_on|None}.
    receipts: {admission_id, decree_number, registered_on, value, counted}.
    A patient accrues (in his discharge month) the value of his issued decrees + the nominal value of a decree still to be
    issued. Counted receipts reduce the balance of THEIR decree (capped at that decree's value) in the month they were
    registered (a receipt dated before discharge counts in the discharge month). A manually/script closed patient bills the
    remaining balance on closed_on.
    Per month: opening + new - billed = closing; closing is split by origin month and into balance on ISSUED decrees vs
    balance on decrees NOT YET ISSUED (nominal); counts: patients with a balance, patients whose balance reached 0."""
    prepared = []
    for r in rows:
        sl = _slots(r)
        if sl:
            prepared.append((r, sl))
    if not prepared:
        return []
    by_adm: Dict[int, List[dict]] = {}
    for rc in receipts:
        if rc.get("counted", True) and rc.get("value"):
            by_adm.setdefault(rc["admission_id"], []).append(rc)
    ev: List[dict] = []                      # {adm, month, origin, amt, slot}
    for r, sl in prepared:
        left = [x["value"] for x in sl]
        origin = month_of(r["check_out_date"])
        for rc in sorted(by_adm.get(r["admission_id"], []), key=lambda x: (x.get("registered_on") or today, str(x.get("receipt_id")))):
            i = next((k for k, x in enumerate(sl) if x["key"] and x["key"] == str(rc.get("decree_number") or "")), None)
            if i is None:                                                 # decree unknown (legacy rows): first issued slot with a balance
                i = next((k for k, x in enumerate(sl) if x["issued"] and left[k] > EPS), None)
            if i is None:
                continue
            amt = min(float(rc["value"]), left[i])
            if amt <= EPS:
                continue
            left[i] -= amt
            ev.append({"adm": r["admission_id"], "month": month_of(max(rc.get("registered_on") or today, r["check_out_date"])),
                       "origin": origin, "amt": amt, "slot": i})
        if r.get("closed_on"):
            for i, x in enumerate(left):
                if x > EPS:
                    ev.append({"adm": r["admission_id"], "month": month_of(max(r["closed_on"], r["check_out_date"])),
                               "origin": origin, "amt": x, "slot": i})
    months, m = [], month_of(min(r["check_out_date"] for r, _ in prepared))
    cur = month_of(today)
    while m <= cur:
        months.append(m)
        y, mo = int(m[:4]), int(m[5:])
        m = f"{y + (mo == 12)}-{(mo % 12) + 1:02d}"

    def balance(r, sl, upto: str):
        """(issued balance, pending balance) of one admission at the end of month `upto`."""
        end = _month_end(upto)
        iss = pen = 0.0
        for i, x in enumerate(sl):
            b = x["value"] - sum(e["amt"] for e in ev if e["adm"] == r["admission_id"] and e["slot"] == i and e["month"] <= upto)
            if b <= EPS:
                continue
            if x["issued"] and (x["issued_on"] is None or x["issued_on"] <= end):
                iss += b
            else:
                pen += b
        return iss, pen

    out, prev_open = [], set()
    for m in months:
        new_rows = [(r, sl) for r, sl in prepared if month_of(r["check_out_date"]) == m]
        new = sum(x["value"] for _, sl in new_rows for x in sl)
        opening = sum(x["value"] for r, sl in prepared if month_of(r["check_out_date"]) < m for x in sl) \
            - sum(e["amt"] for e in ev if e["month"] < m)
        billed_ev = [e for e in ev if e["month"] == m]
        by_origin: Dict[str, float] = {}
        for e in billed_ev:
            by_origin[e["origin"]] = round(by_origin.get(e["origin"], 0.0) + e["amt"], 2)
        billed = sum(e["amt"] for e in billed_ev)
        iss_t = pen_t = 0.0
        open_now, open_by_origin = set(), {}
        for r, sl in prepared:
            if month_of(r["check_out_date"]) > m:
                continue
            i_, p_ = balance(r, sl, m)
            if i_ + p_ > EPS:
                open_now.add(r["admission_id"])
                o = month_of(r["check_out_date"])
                n_, v_ = open_by_origin.get(o, (0, 0.0))
                open_by_origin[o] = (n_ + 1, round(v_ + i_ + p_, 2))
            iss_t += i_
            pen_t += p_
        new_ids = {r["admission_id"] for r, _ in new_rows}
        closed_now = (prev_open | new_ids) - open_now
        out.append({"month": m, "opening_value": round(opening, 2), "new_n": len(new_rows), "new_value": round(new, 2),
                    "billed_value": round(billed, 2),
                    "billed_from_this_month": by_origin.get(m, 0.0),
                    "billed_from_previous_months": round(billed - by_origin.get(m, 0.0), 2),
                    "billed_by_origin": dict(sorted(by_origin.items())),
                    "closing_value": round(opening + new - billed, 2),
                    "closing_n": len(open_now), "closed_n": len(closed_now),
                    "closing_issued": round(iss_t, 2), "closing_pending": round(pen_t, 2),
                    "closing_by_origin": dict(sorted(open_by_origin.items()))})
        prev_open = open_now
    return out


def _pct(xs: List[float], q: float) -> float:
    s = sorted(xs)
    return s[max(0, min(len(s) - 1, int(round(q * len(s) + 0.5)) - 1))]


def timing_stats(rows: List[dict], today: date) -> dict:
    """Bottleneck statistics over FULLY BILLED, non-exception admissions.
    rows: evaluate_bmt() output + check_out_date. Each patient's time from discharge to full billing splits into
      decree wait   = discharge -> day both decrees exist (0 when they were issued before the discharge)
      billing work  = that day -> the day the last decree was consumed
    """
    done = [r for r in rows if r.get("status") == "closed" and not r.get("is_exception") and r.get("billed_on")]

    def summ(xs):
        xs = [x for x in xs if x is not None]
        if not xs:
            return None
        return {"n": len(xs), "mean": round(sum(xs) / len(xs), 1), "median": _pct(xs, 0.5), "p90": _pct(xs, 0.9), "max": max(xs)}
    total = [(r["billed_on"] - r["check_out_date"]).days for r in done]
    wait = [r.get("decree_wait_days") for r in done]
    work = [r.get("billing_work_days") for r in done]
    d1 = [r.get("dec1_days_to_bill") for r in done]
    d2 = [r.get("dec2_days_to_bill") for r in done]
    open_rows = [r for r in rows if r.get("status") != "closed" and not r.get("is_exception")]
    late = [r for r in rows if r.get("is_late") and not r.get("is_exception")]
    mw = sum(x for x in wait if x is not None) / max(1, len([x for x in wait if x is not None])) if done else 0
    mk = sum(x for x in work if x is not None) / max(1, len([x for x in work if x is not None])) if done else 0
    return {"billed_n": len(done), "days_discharge_to_full_billing": summ(total), "decree_wait_days": summ(wait),
            "billing_work_days": summ(work), "first_decree_days_to_bill": summ(d1), "second_decree_days_to_bill": summ(d2),
            "on_time_pct": round(100.0 * sum(1 for r in done if not r.get("is_late")) / len(done), 1) if done else None,
            "open_n": len(open_rows), "open_value_left": round(sum(float(r.get("remaining_value") or 0) for r in open_rows), 2),
            "open_by_stage": {s: sum(1 for r in open_rows if r.get("stage") == s) for s in sorted({r.get("stage") for r in open_rows})},
            "late_n": len(late), "late_by_cause": {c: sum(1 for r in late if r.get("late_cause") == c) for c in ("decree_wait", "billing")},
            "bottleneck": ("decree issuance (waiting for the decrees after discharge)" if mw > mk else "billing work after the decrees exist") if done else None}


# =====================================================================
# Supabase I/O
# =====================================================================
def load_bmt_stays() -> List[dict]:
    import supabase_client as sb
    track_from = to_date(TRACK_FROM) or date(2026, 7, 1)
    context_from = track_from - timedelta(days=CONTEXT_DAYS)
    depts = sb.fetch_all("departments", "id,name_ar")
    bmt = next((d for d in depts if (d.get("name_ar") or "").strip() == BMT_DEPARTMENT_NAME), None)
    if not bmt:
        names = ", ".join(repr((d.get("name_ar") or "").strip()) for d in depts)
        raise RuntimeError(f"BMT department '{BMT_DEPARTMENT_NAME}' not found in departments. Available name_ar values: {names}")
    rows = sb.fetch_all(
        "patient_admissions",
        "id,patient_id,check_in_date,check_out_date,departure_type,financial_category_id,patients(name,national_id)",
        f"department_id=eq.{bmt['id']}&or=(check_out_date.is.null,check_out_date.gte.{context_from.isoformat()})")
    stays = []
    for r in rows:
        p = r.get("patients") or {}
        if isinstance(p, list):
            p = p[0] if p else {}
        ci, co = to_date(r.get("check_in_date")), to_date(r.get("check_out_date"))
        nid = digits(p.get("national_id"))
        if not ci:
            continue
        if not nid:
            if co and co >= track_from:
                log.warning(f"BMT admission #{r['id']} skipped: patient has no national id")
            continue
        stays.append({"admission_id": r["id"], "patient_id": r.get("patient_id"), "patient_nid": nid,
                      "patient_name": (p.get("name") or "").strip(), "check_in_date": ci, "check_out_date": co,
                      "departure_type": r.get("departure_type"), "financial_category_id": r.get("financial_category_id"),
                      "in_scope": bool(co and co >= track_from)})
    return stays


def qualifies(stay: dict) -> bool:
    cat = stay.get("financial_category_id")
    return cat is not None and int(cat) == FIN_CATEGORY_ID


# =====================================================================
# Main run
# =====================================================================
def _mask(nid: str) -> str:
    return f"{nid[:3]}…{nid[-3:]}" if len(nid) > 8 else "…"


def drop_from_audit(all_stays: List[dict]) -> int:
    """Admissions module -> audit sync. Every tracked row that no longer belongs (category changed away from the state
    fund, admission deleted / re-opened / moved) is marked excluded_reason + excluded_at; the app hides such rows.
    A row that qualifies again is cleared by the normal refresh (needs_refresh sees excluded_reason)."""
    import supabase_client as sb
    track_from = to_date(TRACK_FROM) or date(2026, 7, 1)
    try:
        tracked = sb.fetch_all(TRACKING_TABLE, "admission_id,patient_nid,patient_name,check_in_date,check_out_date,excluded_reason")
    except Exception as e:                      # table not migrated yet / transient read error: never block the run
        log.warning(f"tracking rows could not be read for the admissions sync: {e}")
        return 0
    if tracked and not all_stays:
        log.warning("admissions sync skipped: no BMT admissions were loaded although tracking rows exist (refusing to drop everything)")
        return 0
    verdict = reconcile_tracking(tracked, {s["admission_id"]: s for s in all_stays}, track_from)
    rows = []
    for t in tracked:
        reason = verdict[t["admission_id"]]
        if reason and t.get("excluded_reason") != reason:
            rows.append({"admission_id": t["admission_id"], "patient_nid": t.get("patient_nid"), "patient_name": t.get("patient_name"),
                         "check_in_date": t.get("check_in_date"), "check_out_date": t.get("check_out_date"),
                         "excluded_reason": reason, "excluded_at": now_iso()})
            log.info(f"   admission #{t['admission_id']} leaves the audit: {reason}")
    if rows:
        upsert_uniform(TRACKING_TABLE, rows, "admission_id")
    log.info(f"admissions sync: {len(rows)} tracked admission(s) removed from the audit")
    return len(rows)


def run() -> int:
    import supabase_client as sb

    icu.reset_stats()
    started, today = now_iso(), today_cairo()
    all_stays = load_bmt_stays()
    scope = [s for s in all_stays if s["in_scope"]]
    stays = [s for s in scope if qualifies(s)]
    drop_from_audit(all_stays)
    no_cat = sum(1 for s in scope if s.get("financial_category_id") is None)
    log.info(f"{len(scope)} BMT admission(s) discharged since {TRACK_FROM}: {len(stays)} are financial category "
             f"{FIN_CATEGORY_ID} (tracked), {len(scope) - len(stays)} other ({no_cat} with no category set)")
    by: Dict[str, Dict[str, int]] = {}
    for s_ in scope:
        c_ = s_.get("financial_category_id")
        by.setdefault(month_of(s_["check_out_date"]), {}).setdefault("unset" if c_ is None else str(c_), 0)
        by[month_of(s_["check_out_date"])]["unset" if c_ is None else str(c_)] += 1
    for m_ in sorted(by):
        log.info(f"   discharged {m_}: {sum(by[m_].values())} admission(s) by financial_category_id -> "
                 + ", ".join(f"{k}: {v}" for k, v in sorted(by[m_].items())))
    if not stays:
        upsert_uniform(RUNS_TABLE, [{"started_at": started, "finished_at": now_iso(), "ok": True,
                                      "stays_total": 0, "stays_checked": 0, "notes": "no qualifying BMT admissions"}], "id")
        return 0

    adm_ids = [str(s["admission_id"]) for s in stays]
    track_db = {r["admission_id"]: r for r in fetch_in(TRACKING_TABLE, "*", "admission_id", adm_ids)}

    def needs_refresh(s):
        t = track_db.get(s["admission_id"])
        if not t:
            return True
        # the admissions module is the source of truth: corrected dates / departure type / a reinstated row force a re-read
        if (t.get("excluded_reason") or to_date(t.get("check_in_date")) != s["check_in_date"]
                or to_date(t.get("check_out_date")) != s["check_out_date"]
                or (t.get("departure_type") or None) != (s.get("departure_type") or None)):
            return True
        return not (t.get("manual_status") or t.get("status") == "closed")

    refresh = [s for s in stays if needs_refresh(s)]
    log.info(f"{len(refresh)} open admission(s) will be re-checked on SMC")

    errors: Dict[int, List[str]] = {s["admission_id"]: [] for s in refresh}
    rows_out: Dict[int, dict] = {}
    receipts_out: List[dict] = []
    nid_adms: Dict[str, List[dict]] = {}
    for s in all_stays:
        nid_adms.setdefault(s["patient_nid"], []).append(s)

    sess = None
    login_failed = False
    if refresh:
        sess = smc.SMCSession(username=smc.USERNAME, password=smc.PASSWORD)
        sess._portal = "decree"
        t0 = time.time()
        if not sess.login():
            login_failed = True
            icu.STATS["login"]["decree"] = False
            log.error("Decree-portal login failed (SMC_USERNAME) - nothing was read this run.")
        else:
            icu.STATS["login"]["decree"] = True
            log.info("[decree portal] login OK (SMC_USERNAME)")

    def stored_dec(t: Optional[dict], role: str) -> Optional[dict]:
        p = ROLE_COL[role]
        if not t or not t.get(f"{p}_number"):
            return None
        return {"decree_number": t[f"{p}_number"], "decree_date": to_date(t.get(f"{p}_date")),
                "decree_value": t.get(f"{p}_value"), "procedure_text": t.get(f"{p}_procedure"),
                "source": t.get(f"{p}_source") or "value"}

    if sess is not None and not login_failed:
        nids = sorted({s["patient_nid"] for s in refresh})
        for n_i, nid in enumerate(nids, 1):
            if n_i == 1 or n_i % 10 == 0 or n_i == len(nids):
                log.info(f"[decree portal] patient {n_i}/{len(nids)} | {int(time.time() - t0)}s | "
                         f"{icu.STATS['calls'].get('decree', 0)} calls")
            mine = [s for s in refresh if s["patient_nid"] == nid]
            pinned = {s["admission_id"]: {r: stored_dec(track_db.get(s["admission_id"]), r) for r in ROLES}
                      for s in nid_adms[nid]}
            for s in nid_adms[nid]:                       # decrees of closed / waived admissions stay reserved
                pinned.setdefault(s["admission_id"], {})
            need_search = [s for s in mine if not all(pinned[s["admission_id"]].get(r) for r in ROLES)]
            matches: Dict[int, dict] = {}
            if need_search:
                oldest = min(s["check_in_date"] for s in nid_adms[nid])
                found = fetch_decrees_by_nid(sess, nid, search_windows(oldest, today))
                time.sleep(REQUEST_DELAY)
                if found is None:
                    for s in need_search:
                        errors[s["admission_id"]].append("decree search could not be read")
                else:
                    matches = match_bmt_decrees(nid_adms[nid], found, pinned)
                    # a role its regular value (125,000 / 100,000) could not fill: read the patient's other state-fund
                    # decrees and take the one whose description mentions bone marrow ("زرع نخاع")
                    if any(not (matches.get(s["admission_id"]) or {}).get(r) and not pinned[s["admission_id"]].get(r)
                           for s in mine for r in ROLES):
                        for d in decrees_to_read(nid_adms[nid], found, matches, pinned):
                            icu.STATS["decree_pages"] += 1
                            info = icu.fetch_decree(sess, d["decree_number"])
                            time.sleep(REQUEST_DELAY)
                            d["procedure_text"] = (info or {}).get("procedure_text") or ""
                        matches = match_bmt_decrees(nid_adms[nid], found, pinned, use_description=True)
                    if any(not (matches.get(s["admission_id"]) or {}).get(r) and not pinned[s["admission_id"]].get(r)
                           for s in mine for r in ROLES):
                        log.warning(f"   {_mask(nid)}: SMC lists {len(found)} decree(s) -> " + " | ".join(
                            f"{d['decree_number']} {d.get('decree_date')} {d.get('decree_value'):g} [{d.get('decree_type')}]"
                            if d.get("decree_value") is not None else f"{d['decree_number']} {d.get('decree_date')} ? [{d.get('decree_type')}]"
                            for d in sorted(found, key=lambda x: (x.get('decree_date') or date.max)))[:900])
            for s in mine:
                aid = s["admission_id"]
                if errors[aid]:
                    continue
                m = matches.get(aid) or {"first": pinned[aid].get("first"), "second": pinned[aid].get("second"), "notes": []}
                decs: Dict[str, Optional[dict]] = {}
                for role in ROLES:
                    d = m.get(role)
                    if d and d.get("procedure_text") is None:                 # newly matched: read the decree page for the role check
                        icu.STATS["decree_pages"] += 1
                        info = icu.fetch_decree(sess, d["decree_number"])
                        time.sleep(REQUEST_DELAY)
                        if info:
                            d = dict(d, procedure_text=info.get("procedure_text"))
                        else:
                            errors[aid].append(f"decree {d['decree_number']} page not readable")
                    decs[role] = d
                recs: Dict[str, List[dict]] = {}
                for role in ROLES:
                    d = decs.get(role)
                    if not d:
                        continue
                    icu.STATS["receipt_lists"] += 1
                    got = icu.fetch_receipts(sess, d["decree_number"])
                    time.sleep(REQUEST_DELAY)
                    if got is None:
                        errors[aid].append(f"receipts of decree {d['decree_number']} not readable")
                        continue
                    recs[d["decree_number"]] = got
                    for rc in got:
                        receipts_out.append(dict(rc, admission_id=aid, role=role, last_seen_at=now_iso()))
                if errors[aid]:
                    continue
                ev = evaluate_bmt(s, decs, recs, today)
                if m.get("notes"):
                    ev["notes"] = "; ".join(filter(None, [ev.get("notes")] + m["notes"]))
                rows_out[aid] = ev

    # ---------------- write -------------------------------------------------
    base = lambda s: {"admission_id": s["admission_id"], "patient_nid": s["patient_nid"], "patient_name": s["patient_name"],
                      "check_in_date": s["check_in_date"], "check_out_date": s["check_out_date"],
                      "departure_type": s.get("departure_type"), "excluded_reason": None, "excluded_at": None}
    final_rows = []
    for s in refresh:
        aid = s["admission_id"]
        if aid in rows_out:
            final_rows.append(dict(base(s), **rows_out[aid], last_checked_at=now_iso(), last_check_ok=True, error_note=None))
        else:
            r = dict(base(s), last_checked_at=now_iso(), last_check_ok=False,
                     error_note="; ".join(errors[aid]) or "decree portal login failed")
            if aid not in track_db:                      # first sight and unreadable: still show it as an unbilled case
                r.update({"status": "open", "stage": "data_pending", "bundle_value": ROLE_VALUE["first"] + ROLE_VALUE["second"],
                          "discharge_month": first_of_month(month_of(s["check_out_date"])),
                          "due_date": add_work_days(s["check_out_date"], BILL_DAYS_AFTER_DISCHARGE)})
            final_rows.append(r)
    if final_rows:
        upsert_uniform(TRACKING_TABLE, final_rows, "admission_id")
    if receipts_out:
        upsert_uniform(RECEIPTS_TABLE, receipts_out, "receipt_id")

    # ---------------- report ------------------------------------------------
    merged: Dict[int, dict] = {}
    for s in stays:
        t = dict(track_db.get(s["admission_id"]) or {})
        t.update(rows_out.get(s["admission_id"], {}))
        t["check_out_date"] = s["check_out_date"]
        t["billed_on"] = to_date(t.get("billed_on"))
        t["bundle_value"] = float(t.get("bundle_value") or (FIRST_VALUE + SECOND_VALUE))
        t["_manual"] = t.get("manual_status")
        t["check_in_date"] = s["check_in_date"]
        merged[s["admission_id"]] = t
    ok = report_run(stays, merged, errors, today, login_failed)
    upsert_uniform(RUNS_TABLE, [{"started_at": started, "finished_at": now_iso(), "ok": ok == 0,
                                  "stays_total": len(stays), "stays_checked": len(rows_out),
                                  "notes": ("decree portal login failed" if login_failed else
                                            f"{sum(1 for e in errors.values() if e)} admission(s) with read errors")}], "id")
    return ok


def report_run(stays, merged, errors, today, login_failed) -> int:
    log.info("=" * 78)
    log.info("PER-ADMISSION RESULT  (#admission | patient | discharge | due | decrees | billed | state)")
    for s in sorted(stays, key=lambda x: x["check_out_date"]):
        t = merged[s["admission_id"]]
        err = errors.get(s["admission_id"])
        log.info(f"#{s['admission_id']} {_mask(s['patient_nid'])} out {s['check_out_date']} | due {t.get('due_date')} | "
                 f"dec {t.get('dec1_number') or '-'}/{t.get('dec2_number') or '-'} | billed {t.get('billed_value') or 0:g} of "
                 f"{t.get('bundle_value'):g} | {t.get('stage') or 'unchecked'}"
                 f"{' LATE ' + str(t.get('delay_days')) + 'd' if t.get('is_late') else ''}"
                 f"{(' | READ ERRORS: ' + '; '.join(err)) if err else ''}")
    live_rows = []
    for t in merged.values():
        if t.get("_manual") == "waived":
            continue
        t = dict(t)
        t["is_exception"] = bool(t.get("is_exception"))
        if t.get("accrual_value") is None:                      # rows written before the v2 columns existed
            t["accrual_value"] = float(t.get("bundle_value") or STANDARD_BUNDLE)
        if t.get("_manual") == "closed" and not t.get("billed_on"):
            t["billed_on"] = to_date(str(t.get("manual_at") or "")[:10]) or today
        live_rows.append(t)
    exc = [t for t in live_rows if t["is_exception"]]
    if exc:
        log.info(f"EXCEPTIONS (both decrees issued, total below {STANDARD_BUNDLE:,.0f}; tracked; accrued at their real total unless BMT_ACCRUE_EXCEPTIONS=0): " + ", ".join(
            f"#{t['admission_id']} {_mask(str(t.get('patient_nid') or ''))} total {float(t.get('decrees_total') or 0):,.0f}" for t in exc))
    one = [t for t in live_rows if t.get("decrees_count") == 1]
    if one:
        log.info(f"{len(one)} admission(s) have ONE decree only - the second is still to be issued")
    log.info("-" * 78)
    log.info("PATIENT-COUNT VIEW  (patients counted only when BOTH decrees are fully billed; the money ledger below is the accrual)")
    for m in rollforward(live_rows, today):
        log.info(f"{m['month']} | carried in {m['opening_n']}/{m['opening_value']:,.0f} + discharged {m['new_n']}/{m['new_value']:,.0f} "
                 f"- billed {m['billed_n']}/{m['billed_value']:,.0f} = carried out {m['closing_n']}/{m['closing_value']:,.0f}")
        for o, (n, v) in m["billed_by_origin"].items():
            log.info(f"        billed in {m['month']} from {'discharges of this month' if o == m['month'] else 'accrual of ' + o}: {n} / {v:,.0f}")
    try:
        rc_rows = fetch_in(RECEIPTS_TABLE, "admission_id,receipt_id,decree_number,registered_on,value,counted", "admission_id",
                           [str(t["admission_id"]) for t in live_rows])
        for r in rc_rows:
            r["registered_on"] = to_date(r.get("registered_on"))
            r["counted"] = r.get("counted") is not False
        ledger = value_ledger([dict(t, closed_on=(to_date(str(t.get("manual_at") or "")[:10]) if t.get("_manual") == "closed"
                                                  else (t.get("billed_on") if t.get("status") == "closed" else None)))
                               for t in live_rows], rc_rows, today)
        log.info("-" * 78)
        log.info("MONEY ACCRUAL LEDGER  (EGP: each patient accrues his REAL decree total (issued decrees + nominal value of a decree not issued yet); receipts reduce it)")
        for m in ledger:
            log.info(f"{m['month']} | carried in {m['opening_value']:,.0f} + discharged {m['new_n']} pts/{m['new_value']:,.0f} - billed "
                     f"{m['billed_value']:,.0f} (this month's patients {m['billed_from_this_month']:,.0f}, earlier months "
                     f"{m['billed_from_previous_months']:,.0f}) = unbilled accrual {m['closing_value']:,.0f}")
            log.info(f"        patients with a balance {m['closing_n']} | closed this month {m['closed_n']} | balance on ISSUED decrees "
                     f"{m['closing_issued']:,.0f} + on decrees NOT YET ISSUED {m['closing_pending']:,.0f}")
            log.info("        balance by discharge month: " + " | ".join(f"{o}: {n} pts / {v:,.0f}" for o, (n, v) in m["closing_by_origin"].items()))
        if ledger:
            log.info(f"TOTAL UNBILLED ACCRUAL since {TRACK_FROM}: {ledger[-1]['closing_value']:,.0f}")
    except Exception as e:
        log.warning(f"money ledger skipped: {e}")
    ts = timing_stats(live_rows, today)
    log.info("-" * 78)
    log.info(f"TIMING (fully billed, non-exception: {ts['billed_n']}) | discharge->full billing {ts['days_discharge_to_full_billing']} | "
             f"decree wait {ts['decree_wait_days']} | billing work {ts['billing_work_days']}")
    log.info(f"open {ts['open_n']} (value left {ts['open_value_left']:,.0f}) by stage {ts['open_by_stage']} | late {ts['late_n']} by cause "
             f"{ts['late_by_cause']} | on time {ts['on_time_pct']}% | main bottleneck: {ts['bottleneck']}")
    errored = [a for a, e in errors.items() if e]
    if login_failed:
        log.error("PROBLEM: decree-portal login FAILED (SMC_USERNAME / SMC_PASSWORD)")
    if errored:
        log.error(f"PROBLEM: {len(errored)} admission(s) had SMC read errors (their previous results were kept)")
    log.info(f"SMC calls: {icu.STATS['calls']} | failures: {icu.STATS['fails']} {icu.STATS['fail_samples'][:3]}")
    return 1 if (login_failed or errored) else 0


# =====================================================================
# Offline self-test
# =====================================================================
_SEARCH_FIXTURE = """
<table id="requestTable"><tr><th>رقم القرار</th><th>x</th><th>x</th><th>x</th><th>x</th><th>x</th><th>x</th><th>x</th><th>x</th><th>x</th></tr>
<tr><td><a href="/smc/DecreeTreatmentProcedure/Create/202641071969">202641071969</a></td><td>صالح</td><td>27411222401277</td><td>2026-09-07</td><td>نفقة دولة</td><td>100000</td><td>0</td><td>0 </td><td>مغلق</td><td>تم الموافقة</td></tr>
<tr><td><a href="#">202640780795</a></td><td>صالح</td><td>27411222401277</td><td>2026-08-16</td><td>نفقة دولة</td><td>5000</td><td>0</td><td>4427.82</td><td>مغلق</td><td>x</td></tr>
<tr><td><a href="#">202640051000</a></td><td>صالح</td><td>27411222401277</td><td>2026-07-04</td><td>نفقة دولة</td><td>125000</td><td>0</td><td>0</td><td>مغلق</td><td>x</td></tr>
<tr><td><a href="#">202639978716</a></td><td>صالح</td><td>27411222401277</td><td>2026-06-24</td><td>نفقة دولة</td><td>1000</td><td>0</td><td>0</td><td>مفتوح</td><td></td></tr></table>
<div id="myPager">Page 1 of 10 <br /> Total: 250</div>"""


def _selftest():
    D = date
    # --- work days: Monday 2026-10-05 + 7 working days (Fridays skipped) = Tuesday 2026-10-13
    assert add_work_days(D(2026, 10, 5), 7) == D(2026, 10, 13)
    # --- search page parsing (fixture copied from the real SMC response)
    rows, pages = parse_decree_search(_SEARCH_FIXTURE)
    assert pages == 10 and len(rows) == 4, (pages, rows)
    assert rows[0]["decree_number"] == "202641071969" and rows[0]["decree_value"] == 100000.0
    assert rows[0]["decree_date"] == D(2026, 9, 7) and rows[0]["remaining"] == 0.0 and rows[1]["remaining"] == 4427.82
    # --- role helpers
    assert role_of_value(125000) == "first" and role_of_value(100000.0) == "second" and role_of_value(5000) is None
    assert text_role("لزرع نخاع ذاتي (قرار ثاني)") == "second" and text_role("لزرع نخاع ذاتي (قرار اول)") == "first"
    assert text_role("لزرع نخاع ذاتي (قرار أول)") == "first" and text_role("شئ آخر") is None
    # --- matching by DESCRIPTION only (no date window, no value rule)
    st = {"admission_id": 1, "check_in_date": D(2026, 7, 1), "check_out_date": D(2026, 10, 5)}
    T1, T2 = "لزرع نخاع ذاتي (قرار اول)", "لزرع نخاع ذاتي (قرار ثاني)"
    mk = lambda n, dt, v, txt=None, typ="نفقة دولة": {"decree_number": n, "decree_date": dt, "decree_type": typ, "decree_value": v, "procedure_text": txt}
    pat = [mk("F", D(2026, 4, 21), 125000.0, T1), mk("S", D(2026, 5, 31), 100000.0, T2),            # second decree issued BEFORE check-in (real case)
           mk("N1", D(2026, 6, 6), 1000.0, "رعاية مركزة"), mk("N2", D(2026, 8, 23), 10000.0, "رعاية مركزة")]
    m = match_bmt_decrees([dict(st, check_in_date=D(2026, 8, 1))], pat)[1]
    assert m["first"]["decree_number"] == "F" and m["second"]["decree_number"] == "S" and m["first"]["source"] == "description", m
    assert not m["notes"], m["notes"]
    #     any date, any value: a decree issued years earlier / a 70,000 first decree are taken as long as the text says so
    odd = [mk("OLD", D(2024, 1, 5), 70000.0, T1), mk("LATE", D(2027, 3, 1), 100000.0, T2)]
    mo = match_bmt_decrees([st], odd)[1]
    assert mo["first"]["decree_number"] == "OLD" and mo["second"]["decree_number"] == "LATE", mo
    #     text decides, not the value: a 125,000 decree worded "ثاني" is the second
    sw = match_bmt_decrees([st], [mk("A", D(2026, 5, 1), 125000.0, T2), mk("B", D(2026, 5, 2), 100000.0, T1)])[1]
    assert sw["second"]["decree_number"] == "A" and sw["first"]["decree_number"] == "B", sw
    #     no bone-marrow text -> never taken (even with the regular value); not state fund -> never taken; small side decrees ignored
    assert match_bmt_decrees([st], [mk("X", D(2026, 5, 1), 125000.0, "علاج كيماوي")])[1]["first"] is None
    assert match_bmt_decrees([st], [mk("X", D(2026, 5, 1), 125000.0, T1, "تأمين صحي")])[1]["first"] is None
    assert match_bmt_decrees([st], [mk("X", D(2026, 5, 1), 5000.0, T1)])[1]["first"] is None
    #     missing role -> note says why
    only1 = match_bmt_decrees([st], [mk("F", D(2026, 4, 21), 125000.0, T1)])[1]
    assert only1["second"] is None and any("قرار ثاني" in n for n in only1["notes"]), only1
    #     bone-marrow decree without first/second wording: role from its value
    nw = match_bmt_decrees([st], [mk("W", D(2026, 5, 1), 100000.0, "زرع نخاع")])[1]
    assert nw["second"]["decree_number"] == "W" and nw["first"] is None
    #     two admissions of one patient: each takes the decree nearest to its own check-in
    a1 = {"admission_id": 1, "check_in_date": D(2026, 7, 1), "check_out_date": D(2026, 8, 1)}
    a2 = {"admission_id": 2, "check_in_date": D(2026, 12, 1), "check_out_date": D(2027, 1, 5)}
    two = [mk("F1", D(2026, 6, 1), 125000.0, T1), mk("S1", D(2026, 7, 3), 100000.0, T2), mk("F2", D(2026, 11, 20), 125000.0, T1), mk("S2", D(2026, 12, 2), 100000.0, T2)]
    mm = match_bmt_decrees([a2, a1], two)
    assert (mm[1]["first"]["decree_number"], mm[1]["second"]["decree_number"], mm[2]["first"]["decree_number"], mm[2]["second"]["decree_number"]) == ("F1", "S1", "F2", "S2"), mm
    #     pinned decrees are kept and never re-matched
    pn = match_bmt_decrees([st], two, pinned={1: {"first": mk("F2", D(2026, 11, 20), 125000.0, T1)}})[1]
    assert pn["first"]["decree_number"] == "F2" and pn["second"]["decree_number"] == "S1", pn
    #     pages are read for every unread state-fund decree >= the floor, whatever its date
    unread = [mk("U1", D(2025, 1, 1), 125000.0), mk("U2", D(2026, 5, 1), 3500.0), mk("U3", D(2026, 5, 31), 100000.0)]
    assert [d["decree_number"] for d in decrees_to_read([st], unread, match_bmt_decrees([st], unread))] == ["U1", "U3"]
    rows = [mk("202640051000", D(2026, 7, 4), 125000.0)]
    # --- evaluation
    d1 = {"decree_number": "202640051000", "decree_date": D(2026, 7, 4), "decree_value": 125000.0, "procedure_text": "لزرع نخاع ذاتي (قرار اول)"}
    d2 = {"decree_number": "202641071969", "decree_date": D(2026, 9, 7), "decree_value": 100000.0, "procedure_text": "لزرع نخاع ذاتي (قرار ثاني)"}
    rc = lambda i, on, v, ok=True: {"receipt_id": i, "registered_on": on, "value": v, "counted": ok}
    #  a) both consumed on 2026-10-11 (the real HAR case): due 13-10 -> on time, closed
    ev = evaluate_bmt(st, {"first": d1, "second": d2},
                      {d1["decree_number"]: [rc("23291876", D(2026, 10, 11), 125000.0)],
                       d2["decree_number"]: [rc("23291860", D(2026, 10, 11), 100000.0)]}, D(2026, 10, 30))
    assert ev["status"] == "closed" and ev["billed_on"] == D(2026, 10, 11) and ev["bundle_value"] == 225000.0
    assert ev["due_date"] == D(2026, 10, 13) and not ev["is_late"] and ev["delay_days"] == 0 and ev["days_to_bill"] == 6
    assert ev["billing_month"] == "2026-10-01" and ev["discharge_month"] == "2026-10-01" and ev["notes"] is None, ev
    #  b) billed after the deadline: last decree consumed Thu 15-10 -> 2 working days late (14, 15; 16 is Friday)
    ev = evaluate_bmt(st, {"first": d1, "second": d2},
                      {d1["decree_number"]: [rc("1", D(2026, 10, 11), 125000.0)],
                       d2["decree_number"]: [rc("2", D(2026, 10, 12), 40000.0), rc("3", D(2026, 10, 15), 60000.0)]}, D(2026, 10, 30))
    assert ev["status"] == "closed" and ev["billed_on"] == D(2026, 10, 15) and ev["delay_days"] == 2 and ev["is_late"], ev
    #  c) only the first decree consumed -> still open, all 225,000 stays accrued, delay keeps running
    ev = evaluate_bmt(st, {"first": d1, "second": d2},
                      {d1["decree_number"]: [rc("1", D(2026, 10, 11), 125000.0)], d2["decree_number"]: []}, D(2026, 10, 20))
    assert ev["status"] == "open" and ev["stage"] == "partially_billed" and ev["billed_on"] is None
    assert ev["billed_value"] == 125000.0 and ev["remaining_value"] == 100000.0 and ev["delay_days"] == work_days_between(D(2026, 10, 13), D(2026, 10, 20)) == 6
    #  d) a rejected receipt (status R..) does not count
    ev = evaluate_bmt(st, {"first": d1, "second": d2},
                      {d1["decree_number"]: [rc("1", D(2026, 10, 11), 125000.0, ok=False)], d2["decree_number"]: []}, D(2026, 10, 12))
    assert ev["status"] == "open" and ev["billed_value"] == 0 and ev["stage"] == "awaiting_billing" and not ev["is_late"]
    #  e) decrees not issued yet / only one issued
    ev = evaluate_bmt(st, {"first": None, "second": None}, {}, D(2026, 10, 6))
    assert ev["stage"] == "awaiting_decrees" and ev["bundle_value"] == 225000.0 and ev["status"] == "open"
    ev = evaluate_bmt(st, {"first": d1, "second": None}, {d1["decree_number"]: []}, D(2026, 10, 6))
    assert ev["stage"] == "decree_missing"
    #  f) wording of the decree page disagrees with the value -> note
    swapped = dict(d1, procedure_text="لزرع نخاع ذاتي (قرار ثاني)")
    ev = evaluate_bmt(st, {"first": swapped, "second": d2}, {}, D(2026, 10, 6))
    assert "check the pairing" in (ev["notes"] or ""), ev["notes"]
    # --- monthly roll-forward ---------------------------------------------------
    V = 225000.0
    P = lambda co, b: {"check_out_date": co, "billed_on": b, "bundle_value": V}
    data = [P(D(2026, 7, 10), D(2026, 7, 15)),    # July, billed in July
            P(D(2026, 7, 20), D(2026, 8, 3)),     # July, billed in August (accrual of July)
            P(D(2026, 7, 28), None),              # July, still unbilled
            P(D(2026, 8, 5), D(2026, 8, 9)),      # August, billed in August
            P(D(2026, 8, 25), D(2026, 9, 2))]     # August, billed in September
    rf = {m["month"]: m for m in rollforward(data, D(2026, 9, 15))}
    assert list(rf) == ["2026-07", "2026-08", "2026-09"]
    j, a, s = rf["2026-07"], rf["2026-08"], rf["2026-09"]
    assert (j["opening_n"], j["new_n"], j["billed_n"], j["closing_n"]) == (0, 3, 1, 2) and j["closing_value"] == 2 * V
    assert (a["opening_n"], a["new_n"], a["billed_n"], a["closing_n"]) == (2, 2, 2, 2) and a["billed_value"] == 2 * V
    assert a["billed_by_origin"] == {"2026-07": (1, V), "2026-08": (1, V)}, a["billed_by_origin"]
    assert (s["opening_n"], s["new_n"], s["billed_n"], s["closing_n"]) == (2, 0, 1, 1) and s["billed_by_origin"] == {"2026-08": (1, V)}
    #  a bill dated before the discharge counts in the discharge month
    rf = rollforward([P(D(2026, 7, 10), D(2026, 7, 1))], D(2026, 7, 31))
    assert rf[0]["billed_n"] == 1 and rf[0]["closing_n"] == 0
    # --- december -> january roll
    assert [m["month"] for m in rollforward([P(D(2026, 12, 20), None)], D(2027, 1, 5))] == ["2026-12", "2027-01"]
    # --- search windows chain without gaps
    w = search_windows(D(2026, 7, 1), D(2026, 12, 30))
    assert w[0][0] == D(2026, 7, 1) - timedelta(days=max(7, FIRST_LOOKBACK_DAYS)) and w[-1][1] == D(2026, 12, 31) and all(a[1] == b[0] for a, b in zip(w, w[1:]))
    # --- v2: description fallback (one decree 100,000, the other 70,000 -> found by "زرع نخاع") ---------------------
    d100 = {"decree_number": "A100", "decree_date": D(2026, 8, 1), "decree_type": "نفقة دولة", "decree_value": 100000.0}
    d70 = {"decree_number": "A70", "decree_date": D(2026, 7, 5), "decree_type": "نفقة دولة", "decree_value": 70000.0, "procedure_text": "لزرع نخاع ذاتي"}
    d70x = {"decree_number": "B70", "decree_date": D(2026, 7, 6), "decree_type": "نفقة دولة", "decree_value": 70000.0, "procedure_text": "علاج كيماوي"}
    d5k = {"decree_number": "C5", "decree_date": D(2026, 7, 6), "decree_type": "نفقة دولة", "decree_value": 5000.0, "procedure_text": "زرع نخاع"}
    dcr = lambda x: dict(x)
    st2 = {"admission_id": 7, "check_in_date": D(2026, 7, 20), "check_out_date": D(2026, 8, 15)}
    # --- v2: evaluation with a 70,000 decree -> exception, tracked, not accrued
    dd1 = dict(d70, source="description")
    dd2 = dict(d100, procedure_text="لزرع نخاع ذاتي (قرار ثاني)", source="value")
    rc2 = lambda i, on, v: {"receipt_id": i, "registered_on": on, "value": v, "counted": True}
    ev = evaluate_bmt(st2, {"first": dd1, "second": dd2},
                      {"A70": [rc2("1", D(2026, 8, 20), 70000.0)], "A100": [rc2("2", D(2026, 8, 22), 60000.0), rc2("3", D(2026, 8, 25), 40000.0)]}, D(2026, 9, 1))
    assert ev["decrees_count"] == 2 and ev["decrees_total"] == 170000.0 and ev["is_exception"] and ev["accrual_value"] == 170000.0, ev
    assert ev["dec1_source"] == "description" and ev["dec2_source"] == "value" and ev["status"] == "closed"
    assert ev["dec1_remaining"] == 0 and ev["dec2_receipts_n"] == 2 and ev["dec2_first_receipt_on"] == D(2026, 8, 22) and ev["dec2_last_receipt_on"] == D(2026, 8, 25)
    assert ev["dec1_days_to_bill"] == 5 and ev["dec2_days_to_bill"] == 10 and ev["billed_on"] == D(2026, 8, 25)
    # --- v2: standard bundle, decrees issued AFTER the discharge -> decree wait / billing work split, late cause
    e1 = {"decree_number": "E1", "decree_date": D(2026, 7, 4), "decree_value": 125000.0, "procedure_text": "لزرع نخاع ذاتي (قرار اول)"}
    e2 = {"decree_number": "E2", "decree_date": D(2026, 10, 12), "decree_value": 100000.0, "procedure_text": "لزرع نخاع ذاتي (قرار ثاني)"}
    ev = evaluate_bmt(st, {"first": e1, "second": e2}, {"E1": [rc2("1", D(2026, 10, 12), 125000.0)], "E2": [rc2("2", D(2026, 10, 22), 100000.0)]}, D(2026, 10, 30))
    assert not ev["is_exception"] and ev["accrual_value"] == 225000.0 and ev["ready_date"] == D(2026, 10, 12) and ev["decree_wait_days"] == 7
    assert ev["billing_work_days"] == 10 and ev["first_receipt_on"] == D(2026, 10, 12) and ev["is_late"] and ev["late_cause"] == "billing", ev
    ev2 = evaluate_bmt(st, {"first": e1, "second": e2}, {"E1": [rc2("1", D(2026, 10, 12), 125000.0)], "E2": [rc2("2", D(2026, 10, 14), 100000.0)]}, D(2026, 10, 30))
    assert ev2["is_late"] and ev2["late_cause"] == "decree_wait", ev2          # billed within 7 working days of the day the decrees existed
    ev3 = evaluate_bmt(st, {"first": e1, "second": None}, {"E1": []}, D(2026, 10, 30))
    assert ev3["decrees_count"] == 1 and ev3["stage"] == "decree_missing" and ev3["is_late"] and ev3["late_cause"] == "decree_wait" and not ev3["is_exception"]
    # --- v2: admissions sync
    tracked = [{"admission_id": i} for i in (1, 2, 3, 4, 5, 6)]
    live = {1: {"admission_id": 1, "check_out_date": D(2026, 8, 1), "financial_category_id": 4},
            2: {"admission_id": 2, "check_out_date": D(2026, 8, 1), "financial_category_id": 6},       # category corrected -> leaves
            3: {"admission_id": 3, "check_out_date": None, "financial_category_id": 4},                 # check-out cleared
            4: {"admission_id": 4, "check_out_date": D(2026, 6, 20), "financial_category_id": 4},       # moved before tracking start
            5: {"admission_id": 5, "check_out_date": D(2026, 9, 1), "financial_category_id": None}}     # category cleared; 6 deleted
    vd = reconcile_tracking(tracked, live, D(2026, 7, 1))
    assert vd[1] is None and "category" in vd[2] and "discharged" in vd[3] and "before the tracking start" in vd[4] and "category" in vd[5] and "no longer found" in vd[6], vd
    # --- v3: money ledger on the REAL decree total
    D1 = lambda n, v, d: {f"dec1_number": n, "dec1_value": v, "dec1_date": d}
    r1 = dict(admission_id=1, check_out_date=D(2026, 7, 10), dec1_number="A1", dec1_value=125000.0, dec1_date=D(2026, 5, 1),
              dec2_number="A2", dec2_value=100000.0, dec2_date=D(2026, 7, 12))                       # standard, both issued
    r2 = dict(admission_id=2, check_out_date=D(2026, 7, 20), dec1_number="B1", dec1_value=80000.0, dec1_date=D(2026, 6, 1),
              dec2_number=None, dec2_value=None, dec2_date=None)                                      # ONE decree: 80,000 + nominal 100,000 = 180,000
    r3 = dict(admission_id=3, check_out_date=D(2026, 8, 5), dec1_number="C1", dec1_value=125000.0, dec1_date=D(2026, 6, 1),
              dec2_number=None, dec2_value=None, dec2_date=None)                                      # ONE decree: 125,000 + nominal 100,000 = 225,000
    r4 = dict(admission_id=4, check_out_date=D(2026, 8, 6), is_exception=True, dec1_number="E1", dec1_value=75000.0, dec1_date=D(2026, 6, 1),
              dec2_number="E2", dec2_value=100000.0, dec2_date=D(2026, 8, 1))                         # exception, real total 175,000
    rl = lambda a, dn, i, on, v, c=True: {"admission_id": a, "decree_number": dn, "receipt_id": i, "registered_on": on, "value": v, "counted": c}
    recs_l = [rl(1, "A1", "a", D(2026, 7, 15), 125000.0), rl(1, "A2", "b", D(2026, 8, 2), 100000.0),   # patient 1: closed in August
              rl(2, "B1", "c", D(2026, 8, 3), 60000.0), rl(2, "B1", "d", D(2026, 8, 4), 999.0, False),  # rejected ignored
              rl(3, "C1", "e", D(2026, 8, 9), 130000.0),                                            # over-billing capped at THAT decree (125,000)
              rl(4, "E1", "f", D(2026, 8, 9), 75000.0)]
    lg = {m["month"]: m for m in value_ledger([r1, r2, r3, r4], recs_l, D(2026, 9, 10))}
    assert list(lg) == ["2026-07", "2026-08", "2026-09"]
    j = lg["2026-07"]
    assert (j["opening_value"], j["new_value"], j["billed_value"], j["closing_value"]) == (0, 405000, 125000, 280000), j
    assert j["closing_n"] == 2 and j["closed_n"] == 0
    a8 = lg["2026-08"]
    assert a8["opening_value"] == 280000 and a8["new_value"] == 225000 + 175000
    assert a8["billed_value"] == 100000 + 60000 + 125000 + 75000 == 360000, a8
    assert a8["billed_by_origin"] == {"2026-07": 160000, "2026-08": 200000}, a8
    assert a8["closing_value"] == 280000 + 400000 - 360000 == 320000
    # open at end of Aug: p2 (120,000 left: 20,000 on the issued 80k decree + 100,000 nominal), p3 (100,000 nominal), p4 (100,000 on E2)
    assert a8["closing_n"] == 3 and a8["closed_n"] == 1                                              # p1 closed in August
    assert a8["closing_pending"] == 100000 + 100000 and a8["closing_issued"] == 20000 + 100000, a8
    assert a8["closing_by_origin"] == {"2026-07": (1, 120000.0), "2026-08": (2, 200000.0)}, a8
    assert lg["2026-09"]["closing_value"] == 320000
    # manual close bills the rest on the closing day
    lg2 = value_ledger([dict(r2, closed_on=D(2026, 9, 2))], [rl(2, "B1", "c", D(2026, 8, 3), 60000.0)], D(2026, 9, 10))
    assert lg2[-1]["closing_value"] == 0 and lg2[-1]["billed_value"] == 120000
    # legacy lump rows still work
    lg3 = value_ledger([{"admission_id": 9, "check_out_date": D(2026, 8, 1), "accrual_value": 225000.0}], [rl(9, None, "z", D(2026, 8, 9), 50000.0)], D(2026, 8, 20))
    assert lg3[-1]["closing_value"] == 175000
    # --- v2: timing statistics
    tr = [dict(ev, check_out_date=st["check_out_date"]), dict(ev2, check_out_date=st["check_out_date"])]
    ts = timing_stats(tr, D(2026, 10, 30))
    assert ts["billed_n"] == 2 and ts["late_by_cause"] == {"decree_wait": 1, "billing": 1} and ts["days_discharge_to_full_billing"]["max"] == 17
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true", help="run offline logic tests and exit")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        print("bmt selftest OK")
        return 0
    return run()


if __name__ == "__main__":
    sys.exit(main())
