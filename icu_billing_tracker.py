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
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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

STAY_MATCH_TOLERANCE_DAYS = int(os.environ.get("STAY_MATCH_TOLERANCE_DAYS", "2"))
BILL_DAYS_AFTER_DISCHARGE = int(os.environ.get("BILL_DAYS_AFTER_DISCHARGE", "4"))   # 3 dept + 1 RCM
BILL_UNDER_DECREE_DAYS = int(os.environ.get("BILL_UNDER_DECREE_DAYS", "2"))
CONTINUATION_DAYS = int(os.environ.get("CONTINUATION_DAYS", "2"))
ADMIN_LETTER_DAYS = int(os.environ.get("ADMIN_LETTER_DAYS", "1"))

REQUEST_LOOKBACK_DAYS = int(os.environ.get("REQUEST_LOOKBACK_DAYS", "7"))   # list window starts this far before check-in
MAX_DETAILS_PER_RUN = int(os.environ.get("MAX_DETAILS_PER_RUN", "400"))
REQUEST_DELAY = 0.3
EPS = 0.005   # money comparison slack (float noise only; there is NO billing tolerance)

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


def assign_requests_to_stays(requests: List[dict], stays: List[dict]) -> Dict[str, Optional[int]]:
    """request_number -> admission_id (one patient's ICU requests vs that patient's stays).
    Primary rule: SMC's own ICU stay start (ICUdata.FromDate) within +-tolerance of the app check-in
    (closest wins). Fallback when SMC gave no dates: the latest stay checked in on/before the request."""
    tol = timedelta(days=STAY_MATCH_TOLERANCE_DAYS)
    result: Dict[str, Optional[int]] = {}
    for r in requests:
        best, best_diff = None, None
        sf, rd = r.get("smc_stay_from"), r.get("request_date")
        if sf:
            for s in stays:
                diff = abs((sf - s["check_in_date"]).days)
                if diff <= tol.days and (best_diff is None or diff < best_diff):
                    best, best_diff = s["admission_id"], diff
        elif rd:
            cands = [s for s in stays if s["check_in_date"] - tol <= rd]
            if cands:
                best = max(cands, key=lambda s: s["check_in_date"])["admission_id"]
        result[r["request_number"]] = best
    return result


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


def parse_request_details(html: str) -> dict:
    out = {"procid": None, "smc_stay_from": None, "smc_stay_to": None, "decree_number": None}
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
    return resp.status_code == 200 and "Home/Index" in resp.url and "" in resp.text.lower()


def smc_request(sess, method: str, url: str, **kw):
    """GET/POST with one automatic re-login if the session expired. Returns Response or None."""
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
    return None


def _mdy(d: date) -> str:
    return f"{d.month:02d}-{d.day:02d}-{d.year}"


def fetch_request_list(sess, nid: str, from_d: date, to_d: date) -> Optional[List[dict]]:
    """All requests of one patient in the window (pages followed). None = could not read."""
    url = f"{smc.BASE_URL}/smc/Requests/GetRequests"
    seen, rows = set(), []
    for page in range(1, 9):
        payload = {"requestId": "", "nationalId": nid, "REQUESTIMPORTANCEID": "", "TreatmentProcdId": "",
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
    import supabase_client as sb
    depts = sb.fetch_all("departments", "id,name_ar")
    icu = next((d for d in depts if (d.get("name_ar") or "").strip() == ICU_DEPARTMENT_NAME), None)
    if not icu:
        raise RuntimeError(f"ICU department '{ICU_DEPARTMENT_NAME}' not found in departments.")
    rows = sb.fetch_all(
        "patient_admissions",
        "id,patient_id,check_in_date,check_out_date,departure_type,financial_category_id,patients(name,national_id)",
        f"department_id=eq.{icu['id']}&check_out_date=gte.{TRACK_FROM}")
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
                      "departure_type": r.get("departure_type"), "financial_category_id": r.get("financial_category_id")})
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

    started = now_iso()
    today = today_cairo()
    stays = load_icu_stays()
    attach_bills(stays)
    stays = [s for s in stays if qualifies(s)]
    log.info(f"{len(stays)} qualifying ICU stay(s) discharged on/after {TRACK_FROM}")
    if not stays:
        upsert_uniform(RUNS_TABLE, [{"started_at": started, "finished_at": now_iso(), "ok": True,
                                      "stays_total": 0, "stays_checked": 0, "notes": "no qualifying stays"}], "id")
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
    if refresh:
        sess_a = smc.SMCSession(username=SMC_USERNAME_2, password=smc.PASSWORD_2)
        if not sess_a.login():
            log.error("Requests-portal login failed (SMC_USERNAME_2) - aborting run.")
            upsert_uniform(RUNS_TABLE, [{"started_at": started, "finished_at": now_iso(), "ok": False,
                                          "stays_total": len(stays), "stays_checked": 0,
                                          "notes": "requests portal login failed"}], "id")
            return 1
        sess_a.session.get(f"{smc.BASE_URL}/smc/Requests", timeout=30)
        for nid in sorted({s["patient_nid"] for s in refresh}):
            mine = [s for s in refresh if s["patient_nid"] == nid]
            from_d = min(s["check_in_date"] for s in mine) - timedelta(days=REQUEST_LOOKBACK_DAYS)
            listing = fetch_request_list(sess_a, nid, from_d, today + timedelta(days=1))
            if listing is None:
                for s in mine:
                    stay_errors[s["admission_id"]].append("request list could not be read")
                continue
            for item in listing:
                num = item["request_number"]
                row = reqs.get(num) or {"request_number": num, "patient_nid": nid, "admission_id": None,
                                        "procid": None, "is_icu": None, "smc_stay_from": None, "smc_stay_to": None,
                                        "decree_number": None, "letter_date": None, "letter_text": None,
                                        "letter_checked": False, "status_seen_on": today, "status_text": None,
                                        "status_bucket": None, "first_seen_at": now_iso()}
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
                need_details = row["is_icu"] is None or \
                    (row["is_icu"] and row["status_bucket"] == "final" and not row.get("decree_number"))
                if need_details and details_budget > 0:
                    details_budget -= 1
                    det = fetch_request_details(sess_a, num)
                    time.sleep(REQUEST_DELAY)
                    if det is None:
                        for s in mine:
                            stay_errors[s["admission_id"]].append(f"details of request {num} not readable")
                    else:
                        row["procid"] = det["procid"]
                        row["is_icu"] = (det["procid"] in ICU_PROC_IDS) if det["procid"] else None
                        row["smc_stay_from"], row["smc_stay_to"] = det["smc_stay_from"], det["smc_stay_to"]
                        if det["decree_number"]:
                            row["decree_number"] = det["decree_number"]
                        changed = True
                if row["is_icu"] and row["status_bucket"] == "admin_letter" and not row.get("letter_checked"):
                    try:
                        letters = alu.get_letters_for_request(sess_a, num)
                    except Exception as e:
                        letters = []
                        log.warning(f"letter lookup failed for {num}: {e}")
                    if letters:
                        top = letters[0]
                        row["letter_date"] = to_date(top.get("committee_date"))
                        row["letter_text"] = top.get("response_text") or None
                        row["letter_checked"] = True
                        changed = True
                row["last_checked_at"] = now_iso()
                reqs[num] = row
                if changed:
                    req_dirty.add(num)

    # assign ICU requests to stays (all tracked stays of the patient, so closed ones keep theirs)
    for nid in nids:
        mine_stays = [s for s in stays if s["patient_nid"] == nid]
        icu_reqs = [r for r in reqs.values() if r["patient_nid"] == nid and r.get("is_icu")]
        for num, adm in assign_requests_to_stays(icu_reqs, mine_stays).items():
            if reqs[num].get("admission_id") != adm:
                reqs[num]["admission_id"] = adm
                req_dirty.add(num)

    # ---------------- decree portal -------------------------------------
    refresh_ids = {s["admission_id"] for s in refresh}
    wanted = [(r["admission_id"], r["decree_number"], r["request_number"]) for r in reqs.values()
              if r.get("is_icu") and r.get("admission_id") in refresh_ids
              and r.get("status_bucket") == "final" and r.get("decree_number")]
    if wanted:
        sess_b = smc.SMCSession(username=SMC_USERNAME, password=smc.PASSWORD)
        if not sess_b.login():
            log.error("Decree-portal login failed (SMC_USERNAME).")
            for adm, dnum, _ in wanted:
                stay_errors[adm].append("decree portal login failed")
        else:
            for adm, dnum, rnum in wanted:
                d = decs.get(dnum)
                if not d or not d.get("decree_date") or d.get("decree_value") is None:
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

    # ---------------- evaluate + persist --------------------------------
    run_ts = now_iso()
    track_rows, breach_rows = [], []
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
            note = "; ".join(errs + ev["notes"])[:900] or None
            row.update({"last_checked_at": run_ts, "last_check_ok": not errs, "last_check_note": note})
        track_rows.append(row)

        manual = (track_db.get(adm) or {}).get("manual_status")
        computed = compute_breaches(ev["obligations"], today)
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
                "status_seen_on", "procid", "is_icu", "smc_stay_from", "smc_stay_to", "decree_number",
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
    upsert_uniform(RUNS_TABLE, [{"started_at": started, "finished_at": now_iso(), "ok": failed == 0,
                                  "stays_total": len(stays), "stays_checked": len(refresh),
                                  "notes": f"{failed} stay(s) had SMC read errors; {len(breach_rows)} breach row(s) written"}], "id")
    log.info(f"done - {len(stays)} stays, {len(refresh)} checked, {failed} with errors, "
             f"{len(req_dirty)} requests / {len(dec_dirty)} decrees / {len(rec_dirty)} receipts / {len(breach_rows)} breaches written")
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
    # 8) stay <-> request matching with 1-day SMC date drift and a readmission
    stays = [{"admission_id": 1, "check_in_date": D(2026, 9, 1)}, {"admission_id": 2, "check_in_date": D(2026, 10, 4)}]
    m = assign_requests_to_stays([{"request_number": "a", "smc_stay_from": D(2026, 10, 5), "request_date": D(2026, 10, 13)},
                                  {"request_number": "b", "smc_stay_from": D(2026, 8, 31), "request_date": D(2026, 9, 9)}], stays)
    assert m == {"a": 2, "b": 1}, m
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
        print("selftest OK")
        return 0
    return run()


if __name__ == "__main__":
    sys.exit(main())
