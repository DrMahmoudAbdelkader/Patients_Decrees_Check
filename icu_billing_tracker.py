#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
icu_billing_tracker.py -- daily ICU billing-workflow tracker
=============================================================================
Tracks every ICU stay of a State Fund patient (financial_categories.id = 4)
discharged on/after ICU_TRACK_FROM (default 2026-07-01) through this workflow
and records every deadline the RCM user missed:

  discharge ──(+4 days, Fridays skipped)──> ICU bill created in the app
                                            AND decree request filed on SMC
  request ──> SMC answer (NOT counted against the RCM user):
      * "قرار نهائى"  -> decree issued
            - bill the stay's services under the decree      (+2 days)
            - decree value < bill remaining -> file an
              "استكمال" request for the rest                  (+2 days)
      * "خطاب ادارى"  -> new request addressing the letter    (+1 day)
      * "...الغاء..." -> cancelled: always flagged until a new request exists
      * anything else -> pending on SMC
  closed when issued decree values cover the app's bill total AND the
  billing under every decree is done.

TWO SMC LOGINS (GitHub secrets, already set):
  SMC_USERNAME_2 / SMC_PASSWORD_2  requests portal  (request list, Details)
  SMC_USERNAME   / SMC_PASSWORD    decree portal    (decree page, receipts)

HOW A DECREE REQUEST IS MATCHED TO A STAY (match_requests_to_stays):
  * only requests on procedure 12 filed at the facility whose name contains ICU_FACILITY_KEYWORD
    ("جوستاف") count; every other facility is ignored.
  * only requests filed on/after the patient's OLDEST ICU discharge are looked at.
  * ONE ICU period in the admission module: every such request belongs to it - even when the
    admission date typed on SMC differs (any number of days). The difference is written as a note.
    No such request -> "not found" (that is the only case it is reported as not found).
  * SEVERAL periods: the admission dates found on SMC are grouped (+-2 days, +-1 day when two
    periods are less than 3 days apart) and matched in order: oldest<->oldest, newest<->newest,
    middle<->middle (best monotonic fit by date distance).

Everything the module shows comes from this script's evaluate_stay(); the
module only displays it and writes supervisor overrides (manual_*, excuse_*).

Usage:
    python icu_billing_tracker.py            # normal run
    python icu_billing_tracker.py --selftest # offline logic tests, no network
"""

import os
import re
import sys
import json
import time
import logging
import argparse
import itertools
import zlib
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import smc_session as smc    # BASE_URL, SMCSession, USERNAME(_2) / PASSWORD(_2) from the SMC_* env secrets

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("icu_billing_tracker")

CAIRO = ZoneInfo("Africa/Cairo")

# ---------------------------------------------------------------------
# Configuration (all overridable from the workflow's env)
# ---------------------------------------------------------------------
TRACK_FROM = os.environ.get("ICU_TRACK_FROM", "2026-07-01")
ICU_DEPARTMENT_NAME = os.environ.get("ICU_DEPARTMENT_NAME", "العناية المركزة")
FIN_CATEGORY_ID = int(os.environ.get("ICU_FIN_CATEGORY_ID", "4"))
ICU_PROC_IDS = {p.strip() for p in os.environ.get("ICU_PROC_IDS", "12").split(",") if p.strip()}
DECREE_YEAR_PREFIXES = tuple(p.strip() for p in os.environ.get("DECREE_YEAR_PREFIXES", "2026,2027,2028").split(",") if p.strip())
REJECTED_RECEIPT_PREFIXES = tuple(p.strip().upper() for p in os.environ.get("REJECTED_RECEIPT_PREFIXES", "R").split(",") if p.strip())

STAY_MATCH_TOLERANCE_DAYS = int(os.environ.get("STAY_MATCH_TOLERANCE_DAYS", "2"))     # icu_bills <-> stay only
ICU_FACILITY_KEYWORD = os.environ.get("ICU_FACILITY_KEYWORD", "جوستاف")               # only requests of this facility count
SMC_DATE_TOLERANCE_DAYS = int(os.environ.get("SMC_DATE_TOLERANCE_DAYS", "2"))          # grouping of SMC admission dates
SMC_DATE_TOLERANCE_TIGHT_DAYS = int(os.environ.get("SMC_DATE_TOLERANCE_TIGHT_DAYS", "1"))
TIGHT_GAP_DAYS = int(os.environ.get("TIGHT_GAP_DAYS", "3"))                           # periods closer than this -> tight tolerance
ICU_CONTEXT_DAYS = int(os.environ.get("ICU_CONTEXT_DAYS", "30"))                      # earlier ICU periods still counted as periods
# parallel runners: runner i of N handles only the patients whose national id hashes to i (all stays of a patient stay together)
SHARD_TOTAL = max(1, int(os.environ.get("ICU_SHARD_TOTAL", "1") or "1"))
SHARD_INDEX = int(os.environ.get("ICU_SHARD_INDEX", "0") or "0")
ICU_SERVER_PROC_FILTER = os.environ.get("ICU_SERVER_PROC_FILTER", "1") != "0"         # ask SMC for procedure 12 only (self-verified)
BILL_DAYS_AFTER_DISCHARGE = int(os.environ.get("BILL_DAYS_AFTER_DISCHARGE", "4"))   # 3 dept + 1 RCM
BILL_UNDER_DECREE_DAYS = int(os.environ.get("BILL_UNDER_DECREE_DAYS", "2"))
CONTINUATION_DAYS = int(os.environ.get("CONTINUATION_DAYS", "2"))
ADMIN_LETTER_DAYS = int(os.environ.get("ADMIN_LETTER_DAYS", "1"))

REQUEST_LOOKBACK_DAYS = int(os.environ.get("REQUEST_LOOKBACK_DAYS", "7"))   # list window starts this far before check-in
MAX_DETAILS_PER_RUN = int(os.environ.get("MAX_DETAILS_PER_RUN", "400"))
REQUEST_DELAY = 0.3
EPS = 0.005   # money comparison slack (float noise only; there is NO billing tolerance)

# ---- (b) run statistics -------------------------------------------------
# Counters for the end-of-run report (what was read, what failed). Reset at the start of run().
STATS: Dict[str, object] = {}


def reset_stats():
    STATS.clear()
    STATS.update({"calls": {}, "fails": {}, "fail_samples": [], "details_fetched": 0, "details_unreadable": 0,
                  "details_budget_exhausted": False, "decree_pages": 0, "receipt_lists": 0, "letters_read": 0,
                  "phase_seconds": {}, "login": {}})


reset_stats()

TRACKING_TABLE = "icu_stay_tracking"
REQUESTS_TABLE = "icu_stay_requests"
DECREES_TABLE = "icu_stay_decrees"
RECEIPTS_TABLE = "icu_stay_receipts"
BREACHES_TABLE = "icu_stay_breaches"
RUNS_TABLE = "icu_tracker_runs"


# =====================================================================
# Small helpers
# =====================================================================
def today_cairo() -> date:
    return datetime.now(CAIRO).date()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ar2en(text) -> str:
    table = {"٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4", "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9"}
    return "".join(table.get(c, c) for c in str(text or ""))


def to_date(value) -> Optional[date]:
    """'2026-10-04' / '2026-10-04T10:00:00+00:00' / date -> date, else None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(value).strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def cairo_date_of_timestamp(value) -> Optional[date]:
    """timestamptz string -> Cairo calendar date."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CAIRO).date()
    except ValueError:
        return to_date(value)


def to_float(value) -> Optional[float]:
    if value is None:
        return None
    s = re.sub(r"[^\d.\-]", "", ar2en(value))
    if s in ("", "-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def digits(value) -> str:
    return re.sub(r"\D", "", ar2en(value))


def shard_map(nids, total: int = 0) -> Dict[str, int]:
    """Even split: patients sorted by national id, dealt round-robin (shard sizes differ by at most 1).
    Every runner loads the same stays, so every runner computes the same map."""
    total = total or SHARD_TOTAL
    return {n: i % total for i, n in enumerate(sorted(set(nids)))}


def iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


# =====================================================================
# Deadline arithmetic -- full dates only, Friday is the only day off
# =====================================================================
def add_work_days(start: date, n: int) -> date:
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() != 4:        # Monday=0 ... Friday=4
            n -= 1
    return d


def work_days_between(start_excl: date, end_incl: date) -> int:
    """Non-Friday days in (start, end]."""
    if end_incl <= start_excl:
        return 0
    n, cur = 0, start_excl
    while cur < end_incl:
        cur += timedelta(days=1)
        if cur.weekday() != 4:
            n += 1
    return n


# =====================================================================
# SMC status buckets
# =====================================================================
_NORM = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ـ": None})


def norm_ar(text) -> str:
    return re.sub(r"\s+", " ", str(text or "").translate(_NORM)).strip()


def status_bucket(status_text: Optional[str]) -> str:
    """pending | final | admin_letter | cancelled  (user's rules)."""
    n = norm_ar(status_text)
    if not n:
        return "pending"
    if "الغاء" in n:
        return "cancelled"
    if "خطاب" in n and "ادار" in n:
        return "admin_letter"
    if "قرار" in n and "نهائي" in n:
        return "final"
    return "pending"


# =====================================================================
# PURE EVALUATION (no I/O) -- the single source of workflow logic
# =====================================================================
def _ob(key_stage: str, ref: Optional[str], due: Optional[date], done: Optional[date], note: str = "") -> dict:
    return {"key": key_stage if not ref else f"{key_stage}:{ref}", "stage": key_stage, "ref": ref,
            "due": due, "done": done, "note": note}


def _billing_progress(receipts: List[dict], needed: float) -> Tuple[Optional[date], float]:
    """Date the cumulative counted receipts first reached `needed` (None if never) and the total so far."""
    cum = 0.0
    for rc in sorted([r for r in receipts if r.get("counted", True)],
                     key=lambda r: (r.get("registered_on") or date.max, str(r.get("receipt_id")))):
        cum += float(rc.get("value") or 0)
        if cum >= needed - EPS:
            return rc.get("registered_on"), cum
    return None, cum


def evaluate_stay(stay: dict, requests: List[dict], decrees: Dict[str, dict],
                  receipts_by_decree: Dict[str, List[dict]], today: date) -> dict:
    """
    stay: check_out_date(date), bill_total(float|None), bill_created_on(date|None)
    requests: this stay's ICU requests: request_number, request_date, status_bucket,
              status_seen_on, letter_date
    decrees: decree_number -> {request_number, decree_date, decree_value}
    receipts_by_decree: decree_number -> [{receipt_id, registered_on, value, counted}]
    """
    obligations: List[dict] = []
    notes: List[str] = []
    out_date: date = stay["check_out_date"]
    bill_total = stay.get("bill_total")
    bill_total = float(bill_total) if bill_total else 0.0

    chain = sorted(requests, key=lambda r: (r.get("request_date") or date.min,
                                            int(digits(r.get("request_number")) or 0)))
    due_first = add_work_days(out_date, BILL_DAYS_AFTER_DISCHARGE)

    obligations.append(_ob("bill_creation", None, due_first, stay.get("bill_created_on")))
    obligations.append(_ob("first_request", None, due_first, chain[0]["request_date"] if chain else None))

    next_of = {r["request_number"]: (chain[i + 1] if i + 1 < len(chain) else None) for i, r in enumerate(chain)}

    # ---- admin letters and cancellations -------------------------------
    for r in chain:
        nxt = next_of[r["request_number"]]
        nxt_date = nxt["request_date"] if nxt else None
        if r["status_bucket"] == "admin_letter":
            letter_date = r.get("letter_date") or r.get("status_seen_on") or r.get("request_date")
            note = "" if r.get("letter_date") else "letter date unknown - using first day the status was seen"
            obligations.append(_ob("admin_letter", r["request_number"],
                                   add_work_days(letter_date, ADMIN_LETTER_DAYS), nxt_date, note))
        elif r["status_bucket"] == "cancelled":
            seen = r.get("status_seen_on") or r.get("request_date")
            obligations.append(_ob("cancelled_request", r["request_number"], seen, nxt_date,
                                   "request cancelled on SMC - a new request is required"))

    # ---- decrees: allocation, billing, continuation ----------------------
    data_missing = False
    dec_rows = []
    for r in chain:
        if r["status_bucket"] != "final":
            continue
        dnum = r.get("decree_number")
        d = decrees.get(dnum) if dnum else None
        if not dnum:
            data_missing = True
            notes.append(f"request {r['request_number']} is final but no decree number found yet")
            continue
        if not d or not d.get("decree_date") or d.get("decree_value") is None:
            data_missing = True
            notes.append(f"decree {dnum} details not read yet")
            continue
        dec_rows.append((d, r))
    dec_rows.sort(key=lambda x: (x[0]["decree_date"], str(x[0].get("decree_number") or "")))

    covered = 0.0
    billed_total = 0.0
    billing_obs: List[dict] = []
    for d, r in dec_rows:
        dnum = d["decree_number"]
        value = float(d["decree_value"] or 0)
        alloc = min(value, max(0.0, bill_total - covered)) if bill_total else 0.0
        covered += alloc
        recs = receipts_by_decree.get(dnum, [])
        billed_total += sum(float(x.get("value") or 0) for x in recs if x.get("counted", True))
        if alloc > EPS:
            done_on, billed_sofar = _billing_progress(recs, alloc)
            note = ""
            if done_on is None:
                note = f"billed {billed_sofar:g} of {alloc:g}" if billed_sofar > EPS else "nothing billed yet"
            ob = _ob("bill_under_decree", dnum, add_work_days(d["decree_date"], BILL_UNDER_DECREE_DAYS), done_on, note)
            obligations.append(ob)
            billing_obs.append(ob)
        if bill_total and covered < bill_total - EPS:
            nxt = next_of[r["request_number"]]
            obligations.append(_ob("continuation_request", dnum,
                                   add_work_days(d["decree_date"], CONTINUATION_DAYS),
                                   nxt["request_date"] if nxt else None,
                                   f"decree {value:g} covers only part of the bill ({bill_total - covered:g} left)"))

    remaining = max(0.0, bill_total - covered) if bill_total else None
    all_billed = all(o["done"] for o in billing_obs)
    closed = bool(bill_total) and covered >= bill_total - EPS and all_billed and not data_missing \
        and bool(stay.get("bill_created_on"))

    unfinished = [o for o in obligations if not o["done"] and o["due"]]
    if closed:
        stage, status = "closed", "closed"
        next_stage, next_due = None, None
        closed_on = max([o["done"] for o in billing_obs] + [stay["bill_created_on"]])
    else:
        status, closed_on = "open", None
        if unfinished:
            nxt_ob = min(unfinished, key=lambda o: o["due"])
            stage, next_stage, next_due = nxt_ob["stage"], nxt_ob["stage"], nxt_ob["due"]
        else:
            stage = "data_pending" if data_missing else "awaiting_smc"
            next_stage, next_due = None, None

    if not chain:
        summary = "no_request"
    else:
        last = chain[-1]["status_bucket"]
        summary = {"final": "issued", "admin_letter": "admin_letter", "cancelled": "cancelled"}.get(last, "pending")

    return {
        "stage": stage, "status": status, "closed_on": closed_on,
        "next_due_stage": next_stage, "next_due_date": next_due,
        "covered_value": round(covered, 2) if bill_total else None,
        "billed_value": round(billed_total, 2),
        "remaining_to_cover": round(remaining, 2) if remaining is not None else None,
        "requests_count": len(chain), "smc_summary": summary,
        "obligations": obligations, "notes": notes,
    }


def compute_breaches(obligations: List[dict], today: date) -> List[dict]:
    """Late obligations only: either done late (resolved) or overdue right now (open)."""
    out = []
    for o in obligations:
        due, done = o.get("due"), o.get("done")
        if not due:
            continue
        if done:
            if done > due:
                out.append({"key": o["key"], "stage": o["stage"], "ref": o["ref"], "due": due, "done": done,
                            "delay_days": work_days_between(due, done), "status": "resolved", "note": o["note"]})
        elif today > due or o["stage"] == "cancelled_request":
            out.append({"key": o["key"], "stage": o["stage"], "ref": o["ref"], "due": due, "done": None,
                        "delay_days": work_days_between(due, today), "status": "open", "note": o["note"]})
    return out


def match_requests_to_stays(requests: List[dict], stays: List[dict]) -> Tuple[Dict[str, Optional[int]], Dict[int, List[str]]]:
    """One patient's ICU decree requests (procedure 12, right facility) vs that patient's ICU periods.
    -> ({request_number: admission_id or None}, {admission_id: [notes]}).  See the module docstring."""
    result: Dict[str, Optional[int]] = {r["request_number"]: None for r in requests}
    notes: Dict[int, List[str]] = {}
    if not stays:
        return result, notes
    ss = sorted(stays, key=lambda s: (s["check_in_date"], s["check_out_date"]))
    oldest_out = ss[0]["check_out_date"]
    cands = [r for r in requests if not r.get("request_date") or r["request_date"] >= oldest_out]

    def add_note(adm, text):
        if text not in notes.setdefault(adm, []):
            notes[adm].append(text)

    def mismatch_notes(stay, reqs_):
        by_date: Dict[date, List[str]] = {}
        for r in reqs_:
            sf = r.get("smc_stay_from")
            if sf and sf != stay["check_in_date"]:
                by_date.setdefault(sf, []).append(r["request_number"])
        for sf, nums in sorted(by_date.items()):
            add_note(stay["admission_id"],
                     f"decree request {', '.join(nums)} was found carrying admission date {sf} while the admission "
                     f"module carries {stay['check_in_date']} ({abs((sf - stay['check_in_date']).days)} day(s) apart)")

    if len(ss) == 1:                                   # one period: every request after discharge is its request
        for r in cands:
            result[r["request_number"]] = ss[0]["admission_id"]
        mismatch_notes(ss[0], cands)
        return result, notes

    # ---- several periods ------------------------------------------------
    tol = SMC_DATE_TOLERANCE_DAYS
    for a_, b_ in zip(ss, ss[1:]):
        if (b_["check_in_date"] - a_["check_out_date"]).days < TIGHT_GAP_DAYS:
            tol = SMC_DATE_TOLERANCE_TIGHT_DAYS
            break
    dated = sorted([r for r in cands if r.get("smc_stay_from")],
                   key=lambda r: (r["smc_stay_from"], r.get("request_date") or date.min))
    clusters: List[dict] = []
    for r in dated:
        if clusters and (r["smc_stay_from"] - clusters[-1]["anchor"]).days <= tol:
            clusters[-1]["reqs"].append(r)
        else:
            clusters.append({"anchor": r["smc_stay_from"], "reqs": [r]})

    def cost(c, st):
        d = abs((c["anchor"] - st["check_in_date"]).days)
        first_req = min((x.get("request_date") or date.max) for x in c["reqs"])
        if first_req < st["check_out_date"]:            # filed before that period even ended: unlikely pairing
            d += 1000
        return d

    n, m = len(clusters), len(ss)
    chosen: List[int] = []
    if n:
        if n <= m:                                       # oldest<->oldest ... newest<->newest, order preserved
            best = min(itertools.combinations(range(m), n),
                       key=lambda combo: sum(cost(clusters[i], ss[j]) for i, j in enumerate(combo)))
            chosen = list(best)
        else:                                            # more SMC admission dates than periods: nearest period each
            chosen = [min(range(m), key=lambda j: (cost(c, ss[j]), -j)) for c in clusters]
            add_note(ss[-1]["admission_id"],
                     f"SMC holds {n} different admission dates for ICU requests but the admission module holds only {m} period(s)")
    for c, j in zip(clusters, chosen):
        for r in c["reqs"]:
            result[r["request_number"]] = ss[j]["admission_id"]
        mismatch_notes(ss[j], c["reqs"])
    for r in cands:                                      # requests without an SMC admission date: by filing date
        if r.get("smc_stay_from"):
            continue
        before = [st for st in ss if r.get("request_date") and st["check_out_date"] <= r["request_date"]]
        result[r["request_number"]] = (before[-1] if before else ss[0])["admission_id"]
    return result, notes


def assign_requests_to_stays(requests: List[dict], stays: List[dict]) -> Dict[str, Optional[int]]:
    return match_requests_to_stays(requests, stays)[0]


# =====================================================================
# SMC parsing (verified against the two HAR captures)
# =====================================================================
def parse_request_list(html: str) -> List[dict]:
    soup = BeautifulSoup(html or "", "html.parser")
    table = soup.find("table", {"id": "requestTable"})
    rows = []
    if not table:
        return rows
    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue
        num = digits(tds[0].get_text(strip=True))
        if not num:
            continue
        rows.append({"request_number": num,
                     "request_date": to_date(tds[2].get_text(strip=True)),
                     "status_text": re.sub(r"\s+", " ", tds[6].get_text(" ", strip=True))})
    return rows


def extract_decree_numbers(html: str) -> List[str]:
    """Decrees listed in the 'القرارات' column of the request page (ignores PrevDec() in <script>)."""
    soup = BeautifulSoup(html, "html.parser")
    found: List[str] = []
    for th in soup.find_all("th"):
        if th.get_text(strip=True) != "القرارات":
            continue
        table = th.find_parent("table")
        if table is None:
            continue
        for a in table.find_all("a"):
            txt = a.get_text(strip=True)
            if re.fullmatch(r"\d{10,}", txt):
                found.append(txt)
                continue
            m = re.search(r"DecreesReadyToSend=(\d+)", a.get("onclick", ""))
            if m:
                found.append(m.group(1))
    return list(dict.fromkeys(found))


def facility_ok(text: Optional[str]) -> bool:
    return bool(text) and norm_ar(ICU_FACILITY_KEYWORD) in norm_ar(text)


def parse_request_details(html: str) -> dict:
    out = {"procid": None, "smc_stay_from": None, "smc_stay_to": None, "decree_number": None, "facility": None}
    try:
        soup = BeautifulSoup(html, "html.parser")
        names = []
        for fid in ("RecommHospitalName", "sendingSite"):          # treating facility / sending facility
            for inp in soup.find_all("input", id=fid):
                v = re.sub(r"\s+", " ", inp.get("value") or "").strip()
                if v and v not in names:
                    names.append(v)
        out["facility"] = " | ".join(names) or None
    except Exception as e:                                        # never let a parse problem hide a request
        log.warning(f"facility parse failed: {e}")
    m = re.search(r"TREATMENTPROCEDUREID\s*=\s*'?(\d+)'?", html)
    if m:
        out["procid"] = m.group(1)
    m = re.search(r"var\s+ICUdata\s*=\s*(\{.*?\})\s*;", html, re.S)
    if m:
        try:
            icu = json.loads(m.group(1))
            out["smc_stay_from"] = to_date(icu.get("FromDate"))
            out["smc_stay_to"] = to_date(icu.get("ToDate"))
        except ValueError:
            pass
    wanted = [d for d in extract_decree_numbers(html) if d.startswith(DECREE_YEAR_PREFIXES)]
    if wanted:
        out["decree_number"] = wanted[-1]       # most recently issued
        if len(wanted) > 1:
            log.warning(f"several decree numbers on one request page {wanted} - using the last")
    return out


def parse_decree_page(html: str) -> Optional[dict]:
    soup = BeautifulSoup(html or "", "html.parser")
    table = soup.find("table", {"id": "decreeTable"})
    if not table:
        return None
    txt = ar2en(re.sub(r"\s+", " ", table.get_text(" ", strip=True)))
    out = {"decree_date": None, "decree_value": None, "procedure_text": None}
    m = re.search(r"تاريخ القرار:\s*(\d{1,2})-(\d{1,2})-(\d{4})", txt)
    if m:
        try:
            out["decree_date"] = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    m = re.search(r"قيمة القرار:\s*([\d,]+(?:\.\d+)?)", txt)
    if m:
        out["decree_value"] = to_float(m.group(1))
    m = re.search(r"الإجراء:\s*(.*?)(?:\s+هذه الحالة|$)", txt)
    if m:
        out["procedure_text"] = m.group(1).strip() or None
    return out


def parse_receipts(html: str, decree_number: str) -> List[dict]:
    soup = BeautifulSoup(html or "", "html.parser")
    table = soup.find("table", {"id": "hospDecreeReceiptTblId"})
    rows = []
    if not table:
        return rows
    for tr in table.find_all("tr", class_="item"):
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue
        link = tds[0].find("a")
        rid = digits(link.get_text(strip=True) if link else tds[0].get_text(strip=True))
        if not rid:
            continue
        reg_raw = re.sub(r"\s+", " ", ar2en(tds[2].get_text(" ", strip=True)))
        reg_date = None
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", reg_raw)        # M/D/YYYY h:mm:ss AM
        if m:
            try:
                reg_date = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError:
                pass
        st_in = tr.find("input", class_="STATUSID")
        code = (st_in.get("value") if st_in else "") or ""
        rows.append({
            "receipt_id": rid, "decree_number": decree_number,
            "registered_on": reg_date, "registered_at_local": reg_raw,
            "value": to_float(tds[6].get_text(strip=True)) or 0.0,
            "status_code": code, "status_text": re.sub(r"\s+", " ", tds[3].get_text(" ", strip=True)),
            "counted": not code.upper().startswith(REJECTED_RECEIPT_PREFIXES),
        })
    return rows


# =====================================================================
# SMC I/O
# =====================================================================
def _expired(resp) -> bool:
    return resp.status_code == 200 and "Home/Index" in resp.url and "username" in resp.text.lower()


def smc_request(sess, method: str, url: str, **kw):
    """GET/POST with one automatic re-login if the session expired. Returns Response or None."""
    # ---- (a) count SMC calls / failures per portal --------------------------
    portal = getattr(sess, "_portal", "?")
    STATS["calls"][portal] = STATS["calls"].get(portal, 0) + 1
    for attempt in (1, 2, 3):
        try:
            resp = sess.session.request(method, url, timeout=30, **kw)
            if _expired(resp):
                log.warning("SMC session expired - logging in again")
                if not sess.login():
                    return None
                continue
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp
            log.warning(f"{method} {url} -> HTTP {resp.status_code} (attempt {attempt})")
        except Exception as e:
            log.warning(f"{method} {url} failed: {e} (attempt {attempt})")
        time.sleep(REQUEST_DELAY * attempt * 3)
    STATS["fails"][portal] = STATS["fails"].get(portal, 0) + 1
    if len(STATS["fail_samples"]) < 10:
        STATS["fail_samples"].append(f"{portal}: {method} {url.replace(smc.BASE_URL, '')}")
    return None


def _mdy(d: date) -> str:
    return f"{d.month:02d}-{d.day:02d}-{d.year}"


def fetch_request_list(sess, nid: str, from_d: date, to_d: date, proc_id: str = "") -> Optional[List[dict]]:
    """All requests of one patient in the window (pages followed). None = could not read."""
    url = f"{smc.BASE_URL}/smc/Requests/GetRequests"
    seen, rows = set(), []
    for page in range(1, 9):
        payload = {"requestId": "", "nationalId": nid, "REQUESTIMPORTANCEID": "", "TreatmentProcdId": proc_id,
                   "patientName": "", "fromDate": _mdy(from_d), "toDate": _mdy(to_d), "recommendHosId": "",
                   "statusId": "", "cancerCase": "false", "source": "", "page": str(page)}
        resp = smc_request(sess, "POST", url, data=payload,
                           headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
        if resp is None:
            return None if page == 1 else rows
        got = parse_request_list(resp.text)
        fresh = [g for g in got if g["request_number"] not in seen]
        if not fresh:
            break
        for g in fresh:
            seen.add(g["request_number"])
            rows.append(g)
        time.sleep(REQUEST_DELAY)
    return rows


def fetch_icu_request_list(sess, nid: str, from_d: date, to_d: date, probe: dict):
    """-> (listing, ids returned by the procedure-filtered query or None).
    The server-side procedure filter is only trusted after it was cross-checked against Details pages
    (probe state True); until then the full list is read, so nothing can be hidden by a misbehaving filter."""
    if probe["state"] is False:
        return fetch_request_list(sess, nid, from_d, to_d), None
    filt: Dict[str, dict] = {}
    for pid in sorted(ICU_PROC_IDS):
        got = fetch_request_list(sess, nid, from_d, to_d, proc_id=pid)
        if got is None:
            return (fetch_request_list(sess, nid, from_d, to_d), None) if probe["state"] is None else (None, None)
        for g in got:
            filt[g["request_number"]] = g
    if probe["state"] is True:
        return list(filt.values()), set(filt)
    full = fetch_request_list(sess, nid, from_d, to_d)
    return full, (set(filt) if full is not None else None)


def fetch_request_details(sess, request_number: str) -> Optional[dict]:
    resp = smc_request(sess, "GET", f"{smc.BASE_URL}/smc/Requests/Details/{request_number}")
    if resp is None or 'id="requestID"' not in resp.text:
        return None
    return parse_request_details(resp.text)


def fetch_decree(sess, decree_number: str) -> Optional[dict]:
    resp = smc_request(sess, "GET", f"{smc.BASE_URL}/smc/DecreeTreatmentProcedure/Create/{decree_number}")
    return parse_decree_page(resp.text) if resp is not None else None


def fetch_receipts(sess, decree_number: str) -> Optional[List[dict]]:
    resp = smc_request(sess, "POST", f"{smc.BASE_URL}/smc//HospDecreeReceipts/GetHospitalDecreeReceipts",
                       data={"decreeId": decree_number},
                       headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    if resp is None:
        return None
    if 'id="hospDecreeReceiptTblId"' not in resp.text:
        return []       # a decree with no bills yet renders no table
    return parse_receipts(resp.text, decree_number)


# =====================================================================
# Supabase I/O
# =====================================================================
def _ser(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        out[k] = v.isoformat() if isinstance(v, (date, datetime)) else v
    return out


def upsert_uniform(table: str, rows: List[dict], on_conflict: str):
    """PostgREST bulk upsert needs identical keys per batch -> group rows by key set.
    Columns absent from a row (manual_*, excuse_*) are left untouched by merge-duplicates."""
    import supabase_client as sb
    groups: Dict[tuple, List[dict]] = {}
    for r in rows:
        r = _ser(r)
        groups.setdefault(tuple(sorted(r.keys())), []).append(r)
    for batch in groups.values():
        sb.upsert(table, batch, on_conflict)


def fetch_in(table: str, select: str, col: str, values: List[str], extra: str = "", chunk: int = 40) -> List[dict]:
    import supabase_client as sb
    rows: List[dict] = []
    vals = [v for v in values if v]
    for i in range(0, len(vals), chunk):
        flt = f"{col}=in.({','.join(vals[i:i + chunk])})" + (f"&{extra}" if extra else "")
        rows.extend(sb.fetch_all(table, select, flt))
    return rows


def load_icu_stays() -> List[dict]:
    """Every ICU period discharged since TRACK_FROM - ICU_CONTEXT_DAYS. Only periods discharged on/after
    TRACK_FROM are 'in_scope' (tracked); the earlier ones just count as extra periods of the same patient."""
    import supabase_client as sb
    context_from = (to_date(TRACK_FROM) or date(2026, 7, 1)) - timedelta(days=ICU_CONTEXT_DAYS)
    depts = sb.fetch_all("departments", "id,name_ar")
    icu = next((d for d in depts if (d.get("name_ar") or "").strip() == ICU_DEPARTMENT_NAME), None)
    if not icu:
        raise RuntimeError(f"ICU department '{ICU_DEPARTMENT_NAME}' not found in departments.")
    rows = sb.fetch_all(
        "patient_admissions",
        "id,patient_id,check_in_date,check_out_date,departure_type,financial_category_id,patients(name,national_id)",
        f"department_id=eq.{icu['id']}&check_out_date=gte.{context_from.isoformat()}")
    stays = []
    for r in rows:
        p = r.get("patients") or {}
        if isinstance(p, list):
            p = p[0] if p else {}
        ci, co = to_date(r.get("check_in_date")), to_date(r.get("check_out_date"))
        nid = digits(p.get("national_id"))
        if not (ci and co and nid):
            continue
        stays.append({"admission_id": r["id"], "patient_id": r.get("patient_id"), "patient_nid": nid,
                      "patient_name": (p.get("name") or "").strip(), "check_in_date": ci, "check_out_date": co,
                      "departure_type": r.get("departure_type"), "financial_category_id": r.get("financial_category_id"),
                      "in_scope": co >= (to_date(TRACK_FROM) or date.min)})
    return stays


def attach_bills(stays: List[dict]):
    """Link icu_bills to stays (patient_nid + stay overlap), set bill_* on each stay and the bill's fin. category."""
    import supabase_client as sb
    bills = sb.fetch_all(
        "icu_bills", "id,bill_number,patient_nid,stay_from,stay_to,grand_total,financial_category_id,created_at,updated_at",
        f"created_at=gte.{TRACK_FROM}")
    by_nid: Dict[str, List[dict]] = {}
    for s in stays:
        by_nid.setdefault(s["patient_nid"], []).append(s)
        s.update({"icu_bill_id": None, "icu_bill_number": None, "bill_total": None, "bill_created_on": None, "_bill_cat": None})
    tol = timedelta(days=STAY_MATCH_TOLERANCE_DAYS)
    assigned: Dict[int, List[dict]] = {}
    for b in bills:
        cands = by_nid.get(digits(b.get("patient_nid")), [])
        bf, bt, created = to_date(b.get("stay_from")), to_date(b.get("stay_to")), cairo_date_of_timestamp(b.get("created_at"))
        best, best_score = None, None
        for s in cands:
            if (bf and bf > s["check_out_date"] + tol) or (bt and bt < s["check_in_date"] - tol):
                continue
            if not bf and not bt and created and created < s["check_out_date"]:
                continue
            score = abs(((bt or created or s["check_out_date"]) - s["check_out_date"]).days)
            if best_score is None or score < best_score:
                best, best_score = s, score
        if best:
            assigned.setdefault(best["admission_id"], []).append(b)
    for s in stays:
        mine = assigned.get(s["admission_id"], [])
        if not mine:
            continue
        latest = max(mine, key=lambda b: str(b.get("updated_at") or b.get("created_at") or ""))
        earliest = min(cairo_date_of_timestamp(b.get("created_at")) or date.max for b in mine)
        s.update({"icu_bill_id": str(latest["id"]), "icu_bill_number": latest.get("bill_number"),
                  "bill_total": float(latest.get("grand_total") or 0) or None,
                  "bill_created_on": earliest if earliest != date.max else None,
                  "_bill_cat": latest.get("financial_category_id")})


def qualifies(stay: dict) -> bool:
    cat = stay.get("financial_category_id")
    if cat is None:
        cat = stay.get("_bill_cat")
    return cat is not None and int(cat) == FIN_CATEGORY_ID


# =====================================================================
# Main run
# =====================================================================
def run():
    import supabase_client as sb
    import admin_letter_lookup as alu

    reset_stats()
    started = now_iso()
    today = today_cairo()
    all_stays = load_icu_stays()
    scope = [s for s in all_stays if s["in_scope"]]
    attach_bills(scope)
    stays = [s for s in scope if qualifies(s)]
    if SHARD_TOTAL > 1:
        total_q = len(stays)
        mine_nids = {n for n, k in shard_map(s["patient_nid"] for s in stays).items() if k == SHARD_INDEX}
        stays = [s for s in stays if s["patient_nid"] in mine_nids]
        log.info(f"shard {SHARD_INDEX + 1}/{SHARD_TOTAL}: {len(stays)} of {total_q} qualifying stay(s) belong to this runner")
    # every ICU period of the tracked patients (earlier ones included): decides single- vs multi-period matching
    tracked_nids = {s["patient_nid"] for s in stays}
    nid_stays: Dict[str, List[dict]] = {}
    for s in all_stays:
        if s["patient_nid"] in tracked_nids:
            nid_stays.setdefault(s["patient_nid"], []).append(s)
    # requests are only looked at from the patient's OLDEST period discharge onward
    gate_by_nid = {n: sorted(v, key=lambda x: (x["check_in_date"], x["check_out_date"]))[0]["check_out_date"]
                   for n, v in nid_stays.items()}
    log.info(f"{len(stays)} qualifying ICU stay(s) discharged on/after {TRACK_FROM}")
    if not stays:
        upsert_uniform(RUNS_TABLE, [{"started_at": started, "finished_at": now_iso(), "ok": True,
                                      "stays_total": 0, "stays_checked": 0,
                                      "notes": "no qualifying stays" + (f" (shard {SHARD_INDEX + 1}/{SHARD_TOTAL})" if SHARD_TOTAL > 1 else "")}], "id")
        return 0

    adm_ids = [str(s["admission_id"]) for s in stays]
    track_db = {r["admission_id"]: r for r in fetch_in(TRACKING_TABLE, "admission_id,status,manual_status", "admission_id", adm_ids)}
    nids = sorted({s["patient_nid"] for s in stays})
    req_db = {r["request_number"]: r for r in fetch_in(REQUESTS_TABLE, "*", "patient_nid", nids)}
    dec_db = {r["decree_number"]: r for r in fetch_in(DECREES_TABLE, "*", "admission_id", adm_ids)}
    rec_db = {r["receipt_id"]: r for r in fetch_in(RECEIPTS_TABLE, "*", "admission_id", adm_ids)}
    br_db = {(r["admission_id"], r["obligation_key"]): r for r in fetch_in(BREACHES_TABLE, "*", "admission_id", adm_ids)}

    def needs_refresh(s):
        t = track_db.get(s["admission_id"])
        return not (t and (t.get("manual_status") or t.get("status") == "closed"))

    refresh = [s for s in stays if needs_refresh(s)]
    stay_errors: Dict[int, List[str]] = {s["admission_id"]: [] for s in stays}
    log.info(f"{len(refresh)} open stay(s) will be re-checked on SMC")

    # the facility column is new: run icu_stay_requests_facility.sql once; until then it is re-read each run
    try:
        sb.fetch_all(REQUESTS_TABLE, "facility", "limit=1")
        facility_col = True
    except Exception:
        facility_col = False
        log.warning("icu_stay_requests.facility column missing - run icu_stay_requests_facility.sql "
                    "(facility is re-read from SMC every run until then)")

    # in-memory fact tables (db rows normalised to python types)
    def norm_req(r):
        r = dict(r)
        for k in ("request_date", "status_seen_on", "smc_stay_from", "smc_stay_to", "letter_date"):
            r[k] = to_date(r.get(k))
        return r
    reqs: Dict[str, dict] = {k: norm_req(v) for k, v in req_db.items()}
    req_dirty: set = set()
    decs: Dict[str, dict] = {}
    for k, v in dec_db.items():
        v = dict(v); v["decree_date"] = to_date(v.get("decree_date")); decs[k] = v
    dec_dirty: set = set()
    recs: Dict[str, dict] = {}
    for k, v in rec_db.items():
        v = dict(v); v["registered_on"] = to_date(v.get("registered_on")); recs[k] = v
    rec_dirty: set = set()

    # ---------------- requests portal ----------------------------------
    details_budget = MAX_DETAILS_PER_RUN
    list_sizes: Dict[str, Optional[int]] = {}
    if refresh:
        sess_a = smc.SMCSession(username=smc.USERNAME_2, password=smc.PASSWORD_2)
        sess_a._portal = "requests"
        t_phase = time.time()
        if not sess_a.login():
            STATS["login"]["requests"] = False
            log.error("Requests-portal login failed (SMC_USERNAME_2) - aborting run.")
            upsert_uniform(RUNS_TABLE, [{"started_at": started, "finished_at": now_iso(), "ok": False,
                                          "stays_total": len(stays), "stays_checked": 0,
                                          "notes": "requests portal login failed"}], "id")
            return 1
        STATS["login"]["requests"] = True
        log.info("[requests portal] login OK (SMC_USERNAME_2) - reading request lists + Details pages")
        sess_a.session.get(f"{smc.BASE_URL}/smc/Requests", timeout=30)
        nid_list = sorted({s["patient_nid"] for s in refresh})
        probe = {"state": None if ICU_SERVER_PROC_FILTER else False, "verified": 0, "tried": 0}
        unread_by_nid: Dict[str, int] = {}
        for n_i, nid in enumerate(nid_list, 1):
            if n_i == 1 or n_i % 10 == 0 or n_i == len(nid_list):
                log.info(f"[requests portal] patient {n_i}/{len(nid_list)} | {int(time.time() - t_phase)}s elapsed | "
                         f"{STATS['details_fetched']} Details pages read | {STATS['calls'].get('requests', 0)} calls")
            mine = [s for s in refresh if s["patient_nid"] == nid]
            ctx = nid_stays.get(nid) or mine
            from_d = min(s["check_in_date"] for s in ctx) - timedelta(days=REQUEST_LOOKBACK_DAYS)
            gate = gate_by_nid.get(nid)
            listing, filt_ids = fetch_icu_request_list(sess_a, nid, from_d, today + timedelta(days=1), probe)
            list_sizes[nid] = None if listing is None else len(listing)
            if listing is None:
                for s in mine:
                    stay_errors[s["admission_id"]].append("request list could not be read")
                continue
            seen_nums = []
            for item in listing:
                num = item["request_number"]
                if gate and item["request_date"] and item["request_date"] < gate:
                    continue            # filed before the patient's oldest ICU discharge: cannot be the decree request
                seen_nums.append(num)
                row = reqs.get(num) or {"request_number": num, "patient_nid": nid, "admission_id": None,
                                        "procid": None, "is_icu": None, "facility": None, "smc_stay_from": None,
                                        "smc_stay_to": None, "decree_number": None, "letter_date": None,
                                        "letter_text": None, "letter_checked": False, "status_seen_on": today,
                                        "status_text": None, "status_bucket": None, "first_seen_at": now_iso()}
                changed = num not in reqs
                if row.get("status_text") != item["status_text"]:
                    if row.get("status_text") is not None:
                        row["status_seen_on"] = today
                        row["letter_checked"] = False if status_bucket(item["status_text"]) == "admin_letter" else row["letter_checked"]
                    row["status_text"] = item["status_text"]
                    row["status_bucket"] = status_bucket(item["status_text"])
                    changed = True
                if row.get("request_date") != item["request_date"]:
                    row["request_date"] = item["request_date"]; changed = True
                need_details = (row["is_icu"] is None
                                or (row.get("procid") in ICU_PROC_IDS and not row.get("facility"))
                                or (row["is_icu"] and row["status_bucket"] == "final" and not row.get("decree_number")))
                if need_details and details_budget > 0:
                    details_budget -= 1
                    STATS["details_fetched"] += 1
                    det = fetch_request_details(sess_a, num)
                    time.sleep(REQUEST_DELAY)
                    if det is None:
                        STATS["details_unreadable"] += 1
                        for s in mine:
                            stay_errors[s["admission_id"]].append(f"details of request {num} not readable")
                    else:
                        row["procid"] = det["procid"]
                        row["facility"] = det.get("facility")
                        proc_ok = (det["procid"] in ICU_PROC_IDS) if det["procid"] else None
                        if proc_ok is None:
                            row["is_icu"] = None
                        elif not proc_ok:
                            row["is_icu"] = False
                        elif not row["facility"]:
                            row["is_icu"] = None
                            for s in mine:
                                stay_errors[s["admission_id"]].append(f"facility of request {num} not readable")
                        else:
                            row["is_icu"] = facility_ok(row["facility"])    # procedure 12 AND the right facility
                        row["smc_stay_from"], row["smc_stay_to"] = det["smc_stay_from"], det["smc_stay_to"]
                        if det["decree_number"]:
                            row["decree_number"] = det["decree_number"]
                        changed = True
                elif need_details:
                    unread_by_nid[nid] = unread_by_nid.get(nid, 0) + 1
                if row["is_icu"] and row["status_bucket"] == "admin_letter" and not row.get("letter_checked"):
                    try:
                        letters = alu.get_letters_for_request(sess_a, num)
                    except Exception as e:
                        letters = []
                        log.warning(f"letter lookup failed for {num}: {e}")
                    if letters:
                        STATS["letters_read"] += 1
                        top = letters[0]
                        row["letter_date"] = to_date(top.get("committee_date"))
                        row["letter_text"] = top.get("response_text") or None
                        row["letter_checked"] = True
                        changed = True
                row["last_checked_at"] = now_iso()
                reqs[num] = row
                if changed:
                    req_dirty.add(num)
            if unread_by_nid.get(nid):
                for s in mine:
                    stay_errors[s["admission_id"]].append(
                        f"{unread_by_nid[nid]} request(s) after discharge not read yet (Details budget) - "
                        "ICU request lookup incomplete, nothing is reported as 'not found' until it is read")
            # while probing: the SMC procedure filter is trusted only if it agrees with the Details pages
            if filt_ids is not None and probe["state"] is None:
                probe["tried"] += 1
                known = [n_ for n_ in seen_nums if reqs[n_].get("procid")]
                if len(known) == len(seen_nums) and len(filt_ids) < len(listing):
                    icu_by_details = {n_ for n_ in seen_nums if reqs[n_]["procid"] in ICU_PROC_IDS}
                    after_gate = {n_ for n_ in filt_ids if n_ in set(seen_nums)}
                    if icu_by_details == after_gate:
                        probe["verified"] += 1
                    else:
                        probe["state"] = False
                        log.warning("SMC procedure filter disagrees with the Details pages - filter disabled, "
                                    "reading Details after the discharge date instead")
                if probe["state"] is None and probe["verified"] >= 3:
                    probe["state"] = True
                    log.info("SMC procedure filter verified on 3 patients - ICU request lists are now filtered server-side")
                elif probe["state"] is None and probe["tried"] >= 10:
                    probe["state"] = False
                    log.info("SMC procedure filter could not be verified - using the full request list")
        STATS["proc_filter"] = probe["state"]
        STATS["details_unread"] = sum(unread_by_nid.values())
        if details_budget <= 0:
            STATS["details_budget_exhausted"] = True
        STATS["phase_seconds"]["requests_portal"] = int(time.time() - t_phase) if refresh else 0

    # assign ICU requests to stays (see match_requests_to_stays); notes carry any admission-date difference
    scope_ids = {s["admission_id"] for s in stays}
    match_notes: Dict[int, List[str]] = {}
    for nid in nids:
        icu_reqs = [r for r in reqs.values() if r["patient_nid"] == nid and r.get("is_icu")]
        mapping, notes = match_requests_to_stays(icu_reqs, nid_stays.get(nid, []))
        for num, adm in mapping.items():
            adm = adm if adm in scope_ids else None        # earlier (out-of-scope) periods are not tracked rows
            if reqs[num].get("admission_id") != adm:
                reqs[num]["admission_id"] = adm
                req_dirty.add(num)
        for adm, texts in notes.items():
            if adm in scope_ids:
                match_notes.setdefault(adm, []).extend(texts)
    # which requests are relevant (filed on/after the patient's oldest ICU discharge) - for the end-of-run report
    for r in reqs.values():
        g = gate_by_nid.get(r["patient_nid"])
        r["_relevant"] = bool(g is None or not r.get("request_date") or r["request_date"] >= g)

    # ---------------- decree portal -------------------------------------
    refresh_ids = {s["admission_id"] for s in refresh}
    wanted = [(r["admission_id"], r["decree_number"], r["request_number"]) for r in reqs.values()
              if r.get("is_icu") and r.get("admission_id") in refresh_ids
              and r.get("status_bucket") == "final" and r.get("decree_number")]
    if wanted:
        sess_b = smc.SMCSession(username=smc.USERNAME, password=smc.PASSWORD)
        sess_b._portal = "decree"
        t_b = time.time()
        if not sess_b.login():
            STATS["login"]["decree"] = False
            log.error("Decree-portal login failed (SMC_USERNAME).")
            for adm, dnum, _ in wanted:
                stay_errors[adm].append("decree portal login failed")
        else:
            STATS["login"]["decree"] = True
            log.info(f"[decree portal] login OK (SMC_USERNAME) - reading {len(wanted)} issued decree(s) + their receipts")
            for adm, dnum, rnum in wanted:
                d = decs.get(dnum)
                if not d or not d.get("decree_date") or d.get("decree_value") is None:
                    STATS["decree_pages"] += 1
                    info = fetch_decree(sess_b, dnum)
                    time.sleep(REQUEST_DELAY)
                    if not info or not info["decree_date"]:
                        stay_errors[adm].append(f"decree {dnum} page not readable")
                        continue
                    d = {"decree_number": dnum, "admission_id": adm, "request_number": rnum,
                         "decree_date": info["decree_date"], "decree_value": info["decree_value"],
                         "procedure_text": info["procedure_text"], "details_checked_at": now_iso(),
                         "receipts_total": 0, "receipts_count": 0, "receipts_checked_at": None}
                    decs[dnum] = d; dec_dirty.add(dnum)
                STATS["receipt_lists"] += 1
                got = fetch_receipts(sess_b, dnum)
                time.sleep(REQUEST_DELAY)
                if got is None:
                    stay_errors[adm].append(f"receipts of decree {dnum} not readable")
                    continue
                for rc in got:
                    old = recs.get(rc["receipt_id"])
                    rc["admission_id"] = adm
                    if not old or any(old.get(k) != rc.get(k) for k in
                                      ("registered_on", "value", "status_code", "status_text", "counted", "decree_number")):
                        recs[rc["receipt_id"]] = rc; rec_dirty.add(rc["receipt_id"])
                counted = [x for x in recs.values() if x["decree_number"] == dnum and x.get("counted", True)]
                tot, cnt = round(sum(float(x.get("value") or 0) for x in counted), 2), len(counted)
                if d.get("receipts_total") != tot or d.get("receipts_count") != cnt or d.get("request_number") != rnum:
                    d.update({"receipts_total": tot, "receipts_count": cnt, "request_number": rnum,
                              "receipts_checked_at": now_iso()})
                    dec_dirty.add(dnum)
        if wanted:
            STATS["phase_seconds"]["decree_portal"] = int(time.time() - t_b)

    # ---------------- evaluate + persist --------------------------------
    run_ts = now_iso()
    track_rows, breach_rows = [], []
    summaries: List[dict] = []
    for s in stays:
        adm = s["admission_id"]
        my_reqs = [r for r in reqs.values() if r.get("admission_id") == adm and r.get("is_icu")]
        my_decs = {k: v for k, v in decs.items() if v.get("admission_id") == adm}
        my_recs: Dict[str, List[dict]] = {}
        for rc in recs.values():
            if rc.get("admission_id") == adm:
                my_recs.setdefault(rc["decree_number"], []).append(rc)
        ev = evaluate_stay(s, my_reqs, my_decs, my_recs, today)
        errs = stay_errors[adm]
        checked_now = adm in refresh_ids
        row = {"admission_id": adm, "patient_id": s["patient_id"], "patient_nid": s["patient_nid"],
               "patient_name": s["patient_name"], "check_in_date": s["check_in_date"],
               "check_out_date": s["check_out_date"], "departure_type": s["departure_type"],
               "financial_category_id": FIN_CATEGORY_ID,
               "icu_bill_id": s["icu_bill_id"], "icu_bill_number": s["icu_bill_number"],
               "bill_total": s["bill_total"], "bill_created_on": s["bill_created_on"],
               "stage": ev["stage"], "status": ev["status"], "closed_on": ev["closed_on"],
               "next_due_stage": ev["next_due_stage"], "next_due_date": ev["next_due_date"],
               "covered_value": ev["covered_value"], "billed_value": ev["billed_value"],
               "remaining_to_cover": ev["remaining_to_cover"], "requests_count": ev["requests_count"],
               "smc_summary": ev["smc_summary"], "updated_at": run_ts}
        if checked_now:
            note = "; ".join(errs + ev["notes"] + match_notes.get(adm, []))[:900] or None
            row.update({"last_checked_at": run_ts, "last_check_ok": not errs, "last_check_note": note})
        track_rows.append(row)

        manual = (track_db.get(adm) or {}).get("manual_status")
        computed = compute_breaches(ev["obligations"], today)
        summaries.append({"stay": s, "ev": ev, "reqs": my_reqs, "decs": my_decs, "recs": my_recs, "errs": errs,
                          "breaches": computed, "manual": manual, "checked": checked_now})
        keys_now = set()
        for b in computed:
            keys_now.add(b["key"])
            if manual:
                continue           # waived / manually closed: nothing new, existing delays stay frozen
            smc_side = b["stage"] != "bill_creation"
            if errs and smc_side:
                continue           # an unreadable SMC check must never invent or grow a delay
            old = br_db.get((adm, b["key"]))
            if old and old["status"] == "resolved" and b["status"] == "resolved" and \
                    old.get("delay_days") == b["delay_days"] and to_date(old.get("done_on")) == b["done"]:
                continue
            row_b = {"admission_id": adm, "obligation_key": b["key"], "stage": b["stage"], "ref": b["ref"],
                     "due_date": b["due"], "done_on": b["done"], "delay_days": b["delay_days"],
                     "status": b["status"], "note": b["note"] or None}
            if b["status"] == "resolved" and (not old or old["status"] != "resolved"):
                row_b["resolved_at"] = run_ts
            breach_rows.append(row_b)
        # existing open delays whose obligation is now done on time / no longer required, or stay closed manually
        for (a, key), old in br_db.items():
            if a != adm or old["status"] != "open" or key in keys_now:
                continue
            breach_rows.append({"admission_id": adm, "obligation_key": key, "stage": old["stage"], "ref": old["ref"],
                                "due_date": to_date(old["due_date"]), "done_on": None, "delay_days": old["delay_days"],
                                "status": "resolved", "resolved_at": run_ts,
                                "note": "closed manually" if manual else "no longer required"})
        if manual:
            for (a, key), old in br_db.items():
                if a == adm and old["status"] == "open" and key in keys_now:
                    breach_rows.append({"admission_id": adm, "obligation_key": key, "stage": old["stage"],
                                        "ref": old["ref"], "due_date": to_date(old["due_date"]), "done_on": None,
                                        "delay_days": old["delay_days"], "status": "resolved", "resolved_at": run_ts,
                                        "note": "closed manually"})

    # parents first (FKs): tracking -> requests -> decrees -> receipts -> breaches
    upsert_uniform(TRACKING_TABLE, track_rows, "admission_id")
    req_cols = ["request_number", "patient_nid", "admission_id", "request_date", "status_text", "status_bucket",
                "status_seen_on", "procid", "is_icu"] + (["facility"] if facility_col else []) + [ "smc_stay_from", "smc_stay_to", "decree_number",
                "letter_date", "letter_text", "letter_checked", "last_checked_at"]
    upsert_uniform(REQUESTS_TABLE, [{k: reqs[n].get(k) for k in req_cols} for n in req_dirty], "request_number")
    dec_cols = ["decree_number", "admission_id", "request_number", "decree_date", "decree_value", "procedure_text",
                "details_checked_at", "receipts_total", "receipts_count", "receipts_checked_at"]
    upsert_uniform(DECREES_TABLE, [{k: decs[n].get(k) for k in dec_cols} for n in dec_dirty], "decree_number")
    rec_cols = ["receipt_id", "decree_number", "admission_id", "registered_on", "registered_at_local", "value",
                "status_code", "status_text", "counted"]
    upsert_uniform(RECEIPTS_TABLE, [{k: recs[n].get(k) for k in rec_cols} for n in rec_dirty], "receipt_id")
    upsert_uniform(BREACHES_TABLE, breach_rows, "admission_id,obligation_key")

    failed = sum(1 for a in refresh_ids if stay_errors[a])
    rc = report_run(stays, summaries, list_sizes, reqs, today, started)
    upsert_uniform(RUNS_TABLE, [{"started_at": started, "finished_at": now_iso(), "ok": failed == 0,
                                  "stays_total": len(stays), "stays_checked": len(refresh),
                                  "notes": (f"[shard {SHARD_INDEX + 1}/{SHARD_TOTAL}] " if SHARD_TOTAL > 1 else "")
                                           + f"{failed} stay(s) had SMC read errors; {len(breach_rows)} breach row(s) written"}], "id")
    log.info(f"done - {len(stays)} stays, {len(refresh)} checked, {failed} with errors, "
             f"{len(req_dirty)} requests / {len(dec_dirty)} decrees / {len(rec_dirty)} receipts / {len(breach_rows)} breaches written")
    return rc


# =====================================================================
# End-of-run report: per-stay lines, funnel, problems, GitHub run summary
# =====================================================================
def _mask(nid: str) -> str:
    return "***" + (nid[-4:] if nid else "????")      # logs may be visible to others: never print a full national id


def report_run(stays, summaries, list_sizes, reqs, today, started) -> int:
    log.info("=" * 78)
    log.info("PER-STAY RESULT  (#admission | patient | discharge | SMC requests | decrees | billed | state)")
    for x in sorted(summaries, key=lambda y: y["stay"]["check_out_date"]):
        s, ev = x["stay"], x["ev"]
        kinds: Dict[str, int] = {}
        for r in x["reqs"]:
            kinds[r["status_bucket"]] = kinds.get(r["status_bucket"], 0) + 1
        req_txt = ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) or "NO ICU REQUEST FOUND"
        dec_val = sum(float(d.get("decree_value") or 0) for d in x["decs"].values())
        late = [f"{b['stage']}{(':' + b['ref']) if b['ref'] else ''} {b['delay_days']}d {b['status']}" for b in x["breaches"]]
        state = ev["stage"] if not x["manual"] else f"manual-{x['manual']}"
        due = f" next due {ev['next_due_date']}" if ev.get("next_due_date") and ev["status"] == "open" else ""
        err = f" | READ ERRORS: {'; '.join(x['errs'])}" if x["errs"] else ""
        log.info(f"#{s['admission_id']} {_mask(s['patient_nid'])} out {s['check_out_date']} | req: {req_txt} | "
                 f"decrees {len(x['decs'])} ({dec_val:g}) | bill {s.get('bill_total') or '-'} billed {ev['billed_value']:g} | "
                 f"{state}{due}{(' | late: ' + '; '.join(late)) if late else ''}{err}")

    n = len(summaries)
    with_icu = sum(1 for x in summaries if x["reqs"])
    with_final = sum(1 for x in summaries if any(r["status_bucket"] == "final" for r in x["reqs"]))
    with_dec = sum(1 for x in summaries if x["decs"])
    with_rec = sum(1 for x in summaries if any(x["recs"].values()))
    closed = sum(1 for x in summaries if x["ev"]["status"] == "closed")
    open_delays = [b for x in summaries for b in x["breaches"] if b["status"] == "open"]
    resolved_late = sum(1 for x in summaries for b in x["breaches"] if b["status"] == "resolved")
    no_req_overdue = sum(1 for x in summaries if not x["reqs"] and any(b["stage"] == "first_request" for b in x["breaches"]))
    errored = [x for x in summaries if x["errs"]]
    zero_list = [x for x in summaries if x["checked"] and list_sizes.get(x["stay"]["patient_nid"]) == 0]
    icu_all = [r for r in reqs.values() if r.get("is_icu")]
    orphans = [r for r in icu_all if not r.get("admission_id")]
    unknown_proc = [r for r in reqs.values() if r.get("is_icu") is None and r.get("_relevant", True)]
    final_no_dec = [r for r in icu_all if r.get("status_bucket") == "final" and not r.get("decree_number")]
    letters_missing = [r for r in icu_all if r.get("status_bucket") == "admin_letter" and not r.get("letter_checked")]
    calls, fails = STATS["calls"], STATS["fails"]

    problems: List[str] = []
    if STATS["login"].get("requests") is False:
        problems.append("Requests-portal login FAILED (SMC_USERNAME_2 / SMC_PASSWORD_2)")
    if STATS["login"].get("decree") is False:
        problems.append("Decree-portal login FAILED (SMC_USERNAME / SMC_PASSWORD)")
    if errored:
        problems.append(f"{len(errored)} stay(s) had SMC read errors (their SMC-side delays were NOT touched this run)")
    if fails:
        problems.append(f"unreadable SMC pages: {fails}  e.g. {STATS['fail_samples'][:3]}")
    if zero_list:
        problems.append(f"{len(zero_list)} patient(s) returned 0 requests from SMC - check the national id: "
                        + ", ".join(f"#{x['stay']['admission_id']}({_mask(x['stay']['patient_nid'])})" for x in zero_list[:10]))
    if orphans:
        log.info(f"  {len(orphans)} ICU request(s) of the right facility belong to a patient period that is not tracked "
                 f"(earlier than {TRACK_FROM} or another financial category) - ignored: "
                 + ", ".join(r["request_number"] for r in orphans[:8]))
    if unknown_proc:
        problems.append(f"{len(unknown_proc)} request(s) filed after a discharge whose Details page was not read yet (retried next run; "
                        "the affected stays are NOT reported as 'not found' meanwhile)")
    if STATS["details_budget_exhausted"]:
        problems.append(f"Details budget ({MAX_DETAILS_PER_RUN}) exhausted - remaining requests are read on the next run")
    if final_no_dec:
        problems.append(f"{len(final_no_dec)} final request(s) without a decree number on the page: "
                        + ", ".join(r["request_number"] for r in final_no_dec[:8]))
    if letters_missing:
        problems.append(f"{len(letters_missing)} admin-letter request(s) whose letter text/date could not be read "
                        "(letter date falls back to the day the status was first seen)")

    log.info("=" * 78)
    log.info("RUN REPORT")
    log.info(f"  discharged ICU stays tracked (cat {FIN_CATEGORY_ID}, >= {TRACK_FROM}) : {n}  (re-checked on SMC this run: {sum(1 for x in summaries if x['checked'])})")
    log.info(f"  ... with at least 1 ICU request found on SMC          : {with_icu}")
    log.info(f"  ... with a final decree request                       : {with_final}")
    log.info(f"  ... with the issued decree's details read             : {with_dec}")
    log.info(f"  ... with at least 1 receipt (bill) under a decree     : {with_rec}")
    log.info(f"  ... closed (fully covered + billed)                   : {closed}   open: {n - closed}")
    log.info(f"  ... no ICU request filed and already overdue          : {no_req_overdue}")
    log.info(f"  delays: {len(open_delays)} still open, {resolved_late} resolved late (permanent record)")
    log.info(f"  SMC calls  requests portal: {calls.get('requests', 0)} (failed {fails.get('requests', 0)}) | "
             f"decree portal: {calls.get('decree', 0)} (failed {fails.get('decree', 0)})")
    log.info(f"  read this run: {STATS['details_fetched']} request Details pages ({STATS['details_unreadable']} unreadable), "
             f"{STATS['letters_read']} admin letters, {STATS['decree_pages']} decree pages, {STATS['receipt_lists']} receipt lists")
    log.info(f"  SMC procedure filter: {STATS.get('proc_filter')} | details left unread: {STATS.get('details_unread', 0)}")
    log.info(f"  phase time (s): {STATS['phase_seconds']}  | request cache: {len(reqs)} requests ({len(icu_all)} ICU)")
    if problems:
        for pr in problems:
            log.warning("PROBLEM: " + pr)
            print(f"::warning title=ICU tracker::{pr}")
    else:
        log.info("NO PROBLEMS DETECTED - both portals logged in and every page was read.")
    log.info("=" * 78)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        top = sorted(open_delays, key=lambda b: -b["delay_days"])[:15]
        owner = {id(b): x["stay"] for x in summaries for b in x["breaches"]}
        md = [f"## ICU billing tracker - {today.isoformat()}" + (f" - shard {SHARD_INDEX + 1}/{SHARD_TOTAL}" if SHARD_TOTAL > 1 else ""),
              "| Check | Result |", "|---|---|",
              f"| Requests portal login (SMC_USERNAME_2) | {'OK' if STATS['login'].get('requests') else ('FAILED' if STATS['login'].get('requests') is False else 'not needed')} |",
              f"| Decree portal login (SMC_USERNAME) | {'OK' if STATS['login'].get('decree') else ('FAILED' if STATS['login'].get('decree') is False else 'not needed (no issued decree to read)')} |",
              f"| Tracked ICU stays (category {FIN_CATEGORY_ID}, since {TRACK_FROM}) | {n} |",
              f"| With an ICU request found on SMC | {with_icu} |",
              f"| With a final decree request | {with_final} |",
              f"| Decree details read | {with_dec} |",
              f"| With receipts (bills) under a decree | {with_rec} |",
              f"| Closed / open | {closed} / {n - closed} |",
              f"| No request filed and overdue | {no_req_overdue} |",
              f"| Open delays / resolved late | {len(open_delays)} / {resolved_late} |",
              f"| SMC calls (requests / decree portal) | {calls.get('requests', 0)} / {calls.get('decree', 0)} |",
              f"| Failed SMC calls | {sum(fails.values()) if fails else 0} |",
              f"| Details pages read this run | {STATS['details_fetched']} |", ""]
        md.append("### Problems" if problems else "### No problems detected")
        md += [f"- {pr}" for pr in problems]
        if top:
            md += ["", "### Longest open delays", "| Admission | Patient | Stage | Reference | Days late |", "|---|---|---|---|---|"]
            for b in top:
                st = owner[id(b)]
                md.append(f"| #{st['admission_id']} | {_mask(st['patient_nid'])} | {b['stage']} | {b['ref'] or '-'} | {b['delay_days']} |")
        try:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(md) + "\n")
        except OSError as e:
            log.warning(f"could not write the run summary: {e}")

    hard = STATS["login"].get("requests") is False or STATS["login"].get("decree") is False
    ratio = (len(errored) / max(1, sum(1 for x in summaries if x["checked"])))
    if hard or ratio >= 0.25:
        print(f"::error title=ICU tracker::run marked FAILED ({'login failure' if hard else f'{ratio:.0%} of stays had read errors'})")
        return 1
    return 0


# =====================================================================
# Offline self-test of the workflow logic
# =====================================================================
def _selftest_full():
    D = date
    out = D(2026, 10, 5)                                    # Monday discharge
    base = {"check_out_date": out, "bill_total": 3000.0, "bill_created_on": D(2026, 10, 8)}
    req = lambda n, rd, b, **kw: {"request_number": n, "request_date": rd, "status_bucket": b,
                                  "status_seen_on": kw.get("seen"), "letter_date": kw.get("letter"),
                                  "decree_number": kw.get("dec")}
    # 1) perfect run -> closed, no breaches
    ev = evaluate_stay(base, [req("1", D(2026, 10, 9), "final", dec="2026A")],
                       {"2026A": {"decree_number": "2026A", "request_number": "1", "decree_date": D(2026, 10, 12), "decree_value": 3000.0}},
                       {"2026A": [{"receipt_id": "9", "registered_on": D(2026, 10, 13), "value": 3000.0, "counted": True}]},
                       D(2026, 10, 20))
    assert ev["status"] == "closed", ev
    assert compute_breaches(ev["obligations"], D(2026, 10, 20)) == [], compute_breaches(ev["obligations"], D(2026, 10, 20))
    # 2) bill created late and no request yet
    late = dict(base, bill_created_on=None)
    ev = evaluate_stay(late, [], {}, {}, D(2026, 10, 14))
    br = {b["key"]: b for b in compute_breaches(ev["obligations"], D(2026, 10, 14))}
    assert br["bill_creation"]["status"] == "open" and br["first_request"]["status"] == "open"
    assert br["bill_creation"]["delay_days"] == work_days_between(D(2026, 10, 10), D(2026, 10, 14)) == 4
    assert ev["stage"] in ("bill_creation", "first_request")
    # 3) admin letter, new request filed 2 days late
    reqs = [req("1", D(2026, 10, 9), "admin_letter", letter=D(2026, 10, 11)), req("2", D(2026, 10, 15), "pending")]
    ev = evaluate_stay(base, reqs, {}, {}, D(2026, 10, 16))
    br = {b["key"]: b for b in compute_breaches(ev["obligations"], D(2026, 10, 16))}
    assert br["admin_letter:1"]["status"] == "resolved" and br["admin_letter:1"]["delay_days"] == 3, br   # due Mon 12 -> done Thu 15
    assert ev["stage"] == "awaiting_smc"
    # 4) partial decree (1000 of 3000): billing + continuation both overdue
    reqs = [req("1", D(2026, 10, 9), "final", dec="2026A")]
    decs = {"2026A": {"decree_number": "2026A", "request_number": "1", "decree_date": D(2026, 10, 12), "decree_value": 1000.0}}
    ev = evaluate_stay(base, reqs, decs, {"2026A": [{"receipt_id": "9", "registered_on": D(2026, 10, 12), "value": 400.0, "counted": True}]}, D(2026, 10, 20))
    keys = {b["key"]: b for b in compute_breaches(ev["obligations"], D(2026, 10, 20))}
    assert "bill_under_decree:2026A" in keys and "continuation_request:2026A" in keys, keys
    assert "billed 400 of 1000" in keys["bill_under_decree:2026A"]["note"]
    assert ev["status"] == "open" and ev["remaining_to_cover"] == 2000.0
    # 5) full loop: continuation decree covers the rest, everything billed -> closed
    reqs = [req("1", D(2026, 10, 9), "final", dec="2026A"), req("2", D(2026, 10, 13), "final", dec="2026B")]
    decs["2026B"] = {"decree_number": "2026B", "request_number": "2", "decree_date": D(2026, 10, 19), "decree_value": 2500.0}
    ev = evaluate_stay(base, reqs, decs,
                       {"2026A": [{"receipt_id": "9", "registered_on": D(2026, 10, 13), "value": 1000.0, "counted": True}],
                        "2026B": [{"receipt_id": "10", "registered_on": D(2026, 10, 20), "value": 2000.0, "counted": True}]},
                       D(2026, 10, 25))
    assert ev["status"] == "closed" and ev["covered_value"] == 3000.0, ev
    # 6) rejected receipt does not count
    ev = evaluate_stay(base, [req("1", D(2026, 10, 9), "final", dec="2026A")],
                       {"2026A": {"decree_number": "2026A", "request_number": "1", "decree_date": D(2026, 10, 12), "decree_value": 3000.0}},
                       {"2026A": [{"receipt_id": "9", "registered_on": D(2026, 10, 13), "value": 3000.0, "counted": False}]}, D(2026, 10, 20))
    assert ev["status"] == "open"
    # 7) cancelled request is always flagged
    ev = evaluate_stay(base, [req("1", D(2026, 10, 9), "cancelled", seen=D(2026, 10, 10))], {}, {}, D(2026, 10, 10))
    assert any(b["stage"] == "cancelled_request" for b in compute_breaches(ev["obligations"], D(2026, 10, 10)))
    # 8) stay <-> request matching ------------------------------------------------
    R = lambda n, rd, sf: {"request_number": n, "request_date": rd, "smc_stay_from": sf}
    S = lambda i, ci, co: {"admission_id": i, "check_in_date": ci, "check_out_date": co}
    #  8a) ONE period, SMC admission date 2 days off (the old "not found" bug) -> caught, with a note
    one = [S(1, D(2026, 10, 4), D(2026, 10, 9))]
    m, nt = match_requests_to_stays([R("a", D(2026, 10, 13), D(2026, 10, 2))], one)
    assert m == {"a": 1} and "carrying admission date 2026-10-02" in nt[1][0] and "2026-10-04" in nt[1][0], (m, nt)
    #  8b) ONE period, SMC date 20 days off -> still the one we seek
    m, _ = match_requests_to_stays([R("a", D(2026, 10, 13), D(2026, 9, 14))], one)
    assert m == {"a": 1}, m
    #  8c) ONE period, same date -> no note;  request filed BEFORE discharge -> ignored
    m, nt = match_requests_to_stays([R("a", D(2026, 10, 13), D(2026, 10, 4)), R("b", D(2026, 10, 6), D(2026, 10, 4))], one)
    assert m == {"a": 1, "b": None} and not nt, (m, nt)
    #  8d) no request after discharge -> nothing assigned (the only genuine "not found")
    m, _ = match_requests_to_stays([R("b", D(2026, 10, 6), D(2026, 10, 4))], one)
    assert m == {"b": None}
    #  8e) TWO periods, both SMC dates drift by 2 days
    two = [S(1, D(2026, 9, 1), D(2026, 9, 5)), S(2, D(2026, 10, 4), D(2026, 10, 9))]
    m, _ = match_requests_to_stays([R("x", D(2026, 10, 13), D(2026, 10, 6)), R("y", D(2026, 9, 9), D(2026, 8, 30))], two)
    assert m == {"x": 2, "y": 1}, m
    #  8f) THREE periods 2 days apart (tight tolerance 1): oldest/middle/newest matched in order
    three = [S(1, D(2026, 9, 1), D(2026, 9, 5)), S(2, D(2026, 9, 7), D(2026, 9, 12)), S(3, D(2026, 9, 14), D(2026, 9, 20))]
    reqs3 = [R("a", D(2026, 9, 8), D(2026, 9, 1)), R("b", D(2026, 9, 15), D(2026, 9, 8)), R("c", D(2026, 9, 25), D(2026, 9, 13))]
    m, nt = match_requests_to_stays(reqs3, three)
    assert m == {"a": 1, "b": 2, "c": 3}, m
    assert any("2026-09-08" in t and "2026-09-07" in t for t in nt[2]), nt
    #  8g) THREE periods but SMC holds only two admission dates -> oldest and newest; middle has none
    m, _ = match_requests_to_stays([R("a", D(2026, 9, 8), D(2026, 9, 2)), R("c", D(2026, 9, 25), D(2026, 9, 15))], three)
    assert m == {"a": 1, "c": 3}, m
    #  8h) several requests (admin letter chain) of the same period all follow it
    m, _ = match_requests_to_stays([R("a", D(2026, 9, 8), D(2026, 9, 3)), R("b", D(2026, 9, 12), D(2026, 9, 3)),
                                    R("c", D(2026, 9, 25), D(2026, 9, 13))], three)
    assert m == {"a": 1, "b": 1, "c": 3}, m
    #  8i) FOUR periods are handled the same way
    four = three + [S(4, D(2026, 9, 25), D(2026, 9, 30))]
    m, _ = match_requests_to_stays([R("a", D(2026, 9, 8), D(2026, 9, 1)), R("d", D(2026, 10, 3), D(2026, 9, 26))], four)
    assert m == {"a": 1, "d": 4}, m
    #  8j) facility rule
    assert facility_ok(" جوستاف روسي انترناشيونال - مصر (EPE) *") and not facility_ok("مستشفى آخر")
    # 9) status buckets
    assert status_bucket("قرار نهائى") == "final" and status_bucket("قرار نهائي") == "final"
    assert status_bucket("خطاب ادارى") == "admin_letter" and status_bucket("خطاب إداري") == "admin_letter"
    assert status_bucket("إلغاء الطلب") == "cancelled" and status_bucket("لجنة طبية") == "pending"
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true", help="run offline logic tests and exit")
    args = ap.parse_args()
    if args.selftest:
        assert add_work_days(date(2026, 10, 5), 4) == date(2026, 10, 10)
        assert add_work_days(date(2026, 10, 8), 1) == date(2026, 10, 10)
        _selftest_full()
        ids = [f"2670226220{n:04d}" for n in range(74)]            # sharding: each patient on exactly one runner, evenly
        for tot in (1, 7, 20):
            sizes = [0] * tot
            for k in shard_map(ids, tot).values():
                sizes[k] += 1
            assert sum(sizes) == len(ids) and max(sizes) - min(sizes) <= 1, sizes
        print("selftest OK")
        return 0
    return run()


if __name__ == "__main__":
    sys.exit(main())
