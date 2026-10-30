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
  * decree type = state fund ("نفقة دولة"), value exactly 125,000 or 100,000
  * decree date on/after the admission's check-in (BMT_DECREE_SLACK_DAYS earlier is tolerated)
    and before the patient's NEXT BMT admission, if any
  * the earliest unused decree of each value wins; the procedure text ("قرار اول"/"قرار ثاني",
    "نخاع") is only a second check -> a disagreement is written as a note, never silently ignored.

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
DECREE_SLACK_DAYS = int(os.environ.get("BMT_DECREE_SLACK_DAYS", "0"))           # decree dated up to N days BEFORE check-in still accepted
CONTEXT_DAYS = int(os.environ.get("BMT_CONTEXT_DAYS", "120"))                   # earlier BMT admissions still loaded (they bound the decree window)
SEARCH_WINDOW_DAYS = int(os.environ.get("BMT_SEARCH_WINDOW_DAYS", "175"))      # SMC screen refuses > 6 months between dateFrom and dateTo
SEARCH_MAX_PAGES = int(os.environ.get("BMT_SEARCH_MAX_PAGES", "12"))
DECREE_SOURCE = os.environ.get("BMT_DECREE_SOURCE", "1")                        # decreeSource value the SMC decree screen sends
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
    if "ثاني" in n:
        return "second"
    if re.search(r"قرار\s*(ال)?اول", n):
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
    out, start = [], oldest_check_in - timedelta(days=7)
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
def match_bmt_decrees(admissions: List[dict], decrees: List[dict],
                      pinned: Optional[Dict[int, Dict[str, dict]]] = None) -> Dict[int, dict]:
    """admissions: ALL BMT admissions of ONE patient. decrees: everything SMC lists for him.
    pinned: {admission_id: {role: decree dict}} already known from earlier runs (never re-matched).
    -> {admission_id: {"first": dec|None, "second": dec|None, "notes": [..]}}"""
    pinned = pinned or {}
    adms = sorted(admissions, key=lambda s: (s["check_in_date"], s["check_out_date"] or date.max, s["admission_id"]))
    used = {d["decree_number"] for per in pinned.values() for d in per.values() if d}
    cands = [d for d in decrees if d.get("decree_date") and is_state_fund_decree(d) and role_of_value(d.get("decree_value"))]
    out: Dict[int, dict] = {}
    for i, s in enumerate(adms):
        lo = s["check_in_date"] - timedelta(days=DECREE_SLACK_DAYS)
        hi = adms[i + 1]["check_in_date"] if i + 1 < len(adms) else None
        res = {"first": None, "second": None, "notes": []}
        for role in ROLES:
            known = (pinned.get(s["admission_id"]) or {}).get(role)
            if known:
                res[role] = known
                continue
            pool = [d for d in cands if role_of_value(d["decree_value"]) == role and d["decree_number"] not in used
                    and d["decree_date"] >= lo and (hi is None or d["decree_date"] < hi)]
            pool.sort(key=lambda d: (d["decree_date"], d["decree_number"]))
            if pool:
                res[role] = pool[0]
                used.add(pool[0]["decree_number"])
                if len(pool) > 1:
                    res["notes"].append(f"{len(pool)} decrees of value {ROLE_VALUE[role]:g} after check-in - the earliest "
                                        f"({pool[0]['decree_number']}) is used")
        f, sc = res["first"], res["second"]
        if f and sc and sc.get("decree_date") and f.get("decree_date") and sc["decree_date"] < f["decree_date"]:
            res["notes"].append("the 100,000 decree is dated before the 125,000 decree")
        out[s["admission_id"]] = res
    return out


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
    """stay: {check_out_date}. decs: {"first": dec|None, "second": dec|None} (decree_number/date/value/procedure_text).
    receipts: {decree_number: [receipt dicts]}. Returns the flat row stored in bmt_stay_tracking."""
    co: date = stay["check_out_date"]
    due = add_work_days(co, BILL_DAYS_AFTER_DISCHARGE)
    notes: List[str] = []
    row: dict = {"discharge_month": first_of_month(month_of(co)), "due_date": due}
    billed_dates: List[date] = []
    consumed_n = present_n = 0
    billed_sum = bundle = 0.0
    for role in ROLES:
        p, d = ROLE_COL[role], decs.get(role)
        val = float(d["decree_value"]) if d and d.get("decree_value") is not None else ROLE_VALUE[role]
        bundle += val
        row.update({f"{p}_number": d["decree_number"] if d else None, f"{p}_date": d.get("decree_date") if d else None,
                    f"{p}_value": val if d else None, f"{p}_procedure": d.get("procedure_text") if d else None,
                    f"{p}_billed": None, f"{p}_billed_on": None})
        if not d:
            continue
        present_n += 1
        ok, on, total = _consumption(receipts.get(d["decree_number"], []), val)
        row[f"{p}_billed"] = min(total, val) if ok else total
        billed_sum += min(total, val)
        if ok:
            if on is None:
                on = today
                notes.append(f"decree {d['decree_number']}: consumed but the receipt date is unreadable - today is used")
            consumed_n += 1
            billed_dates.append(on)
            row[f"{p}_billed_on"] = on
        if total > val + EPS:
            notes.append(f"decree {d['decree_number']}: receipts ({total:g}) exceed the decree value ({val:g})")
        tr = text_role(d.get("procedure_text"))
        if d.get("procedure_text") is not None:
            if tr and tr != role:
                notes.append(f"decree {d['decree_number']} (value {val:g}) is worded as "
                             f"'{'قرار اول' if tr == 'first' else 'قرار ثاني'}' - check the pairing")
            if "نخاع" not in norm_ar(d.get("procedure_text")):
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
    row.update({
        "bundle_value": round(bundle, 2), "billed_value": round(billed_sum, 2),
        "remaining_value": round(max(0.0, bundle - billed_sum), 2),
        "billed_on": billed_on, "billing_month": first_of_month(month_of(billed_on)),
        "days_to_bill": (billed_on - co).days if billed_on else None,
        "delay_days": delay, "is_late": delay > 0,
        "status": "closed" if fully else "open", "stage": stage,
        "notes": "; ".join(notes) or None,
    })
    return row


# =====================================================================
# Monthly accounting (same algorithm as the module's JS)  (pure)
# =====================================================================
def _eff_billed(r: dict) -> Optional[date]:
    b = r.get("billed_on")
    return None if not b else max(b, r["check_out_date"])


def rollforward(rows: List[dict], today: date) -> List[dict]:
    """rows: {check_out_date, billed_on|None, bundle_value}. One dict per month, first discharge month .. today's month."""
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
        v = lambda xs: round(sum(float(x.get("bundle_value") or 0) for x in xs), 2)
        out.append({"month": m,
                    "opening_n": len(opening), "opening_value": v(opening),
                    "new_n": len(new), "new_value": v(new),
                    "billed_n": len(billed), "billed_value": v(billed),
                    "billed_by_origin": {o: (len(x), v(x)) for o, x in sorted(by_origin.items())},
                    "closing_n": len(opening) + len(new) - len(billed),
                    "closing_value": round(v(opening) + v(new) - v(billed), 2)})
    return out


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


def run() -> int:
    import supabase_client as sb

    icu.reset_stats()
    started, today = now_iso(), today_cairo()
    all_stays = load_bmt_stays()
    scope = [s for s in all_stays if s["in_scope"]]
    stays = [s for s in scope if qualifies(s)]
    no_cat = sum(1 for s in scope if s.get("financial_category_id") is None)
    log.info(f"{len(scope)} BMT admission(s) discharged since {TRACK_FROM}: {len(stays)} are financial category "
             f"{FIN_CATEGORY_ID} (tracked), {len(scope) - len(stays)} other ({no_cat} with no category set)")
    if not stays:
        upsert_uniform(RUNS_TABLE, [{"started_at": started, "finished_at": now_iso(), "ok": True,
                                      "stays_total": 0, "stays_checked": 0, "notes": "no qualifying BMT admissions"}], "id")
        return 0

    adm_ids = [str(s["admission_id"]) for s in stays]
    track_db = {r["admission_id"]: r for r in fetch_in(TRACKING_TABLE, "*", "admission_id", adm_ids)}

    def needs_refresh(s):
        t = track_db.get(s["admission_id"])
        return not (t and (t.get("manual_status") or t.get("status") == "closed"))

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
                "decree_value": t.get(f"{p}_value"), "procedure_text": t.get(f"{p}_procedure")}

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
                      "departure_type": s.get("departure_type")}
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
    rows = [t for t in merged.values() if not t.get("_manual")]
    log.info("-" * 78)
    log.info("MONTHLY ACCRUAL ROLL-FORWARD  (n / EGP)")
    for m in rollforward(rows, today):
        log.info(f"{m['month']} | carried in {m['opening_n']}/{m['opening_value']:,.0f} + discharged {m['new_n']}/{m['new_value']:,.0f} "
                 f"- billed {m['billed_n']}/{m['billed_value']:,.0f} = carried out {m['closing_n']}/{m['closing_value']:,.0f}")
        for o, (n, v) in m["billed_by_origin"].items():
            log.info(f"        billed in {m['month']} from {'discharges of this month' if o == m['month'] else 'accrual of ' + o}: {n} / {v:,.0f}")
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
    # --- matching: only the 125k + 100k decrees of this admission; 5000 / 1000 decrees are ignored
    st = {"admission_id": 1, "check_in_date": D(2026, 7, 1), "check_out_date": D(2026, 10, 5)}
    m = match_bmt_decrees([st], rows)[1]
    assert m["first"]["decree_number"] == "202640051000" and m["second"]["decree_number"] == "202641071969", m
    #     decree dated before check-in is not taken (slack 0) ...
    late = dict(st, check_in_date=D(2026, 7, 10))
    assert match_bmt_decrees([late], rows)[1]["first"] is None
    #     ... two admissions of one patient: each gets the decrees dated inside its own window
    a1 = {"admission_id": 1, "check_in_date": D(2026, 7, 1), "check_out_date": D(2026, 8, 1)}
    a2 = {"admission_id": 2, "check_in_date": D(2026, 9, 1), "check_out_date": D(2026, 10, 5)}
    extra = rows + [{"decree_number": "X1", "decree_date": D(2026, 9, 5), "decree_type": "نفقة دولة", "decree_value": 125000.0}]
    mm = match_bmt_decrees([a2, a1], extra)
    assert mm[1]["first"]["decree_number"] == "202640051000" and mm[2]["first"]["decree_number"] == "X1", mm
    assert mm[1]["second"] is None and mm[2]["second"]["decree_number"] == "202641071969"
    #     a non-state-fund decree is ignored
    other = [dict(rows[2], decree_type="تأمين صحي")]
    assert match_bmt_decrees([st], other)[1]["first"] is None
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
    assert w[0][0] == D(2026, 6, 24) and w[-1][1] == D(2026, 12, 31) and all(a[1] == b[0] for a, b in zip(w, w[1:]))
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
