"""
admin_letter_lookup.py — "what did the admin letter actually say?"

Ported from Extract_Admin_Letters_Unified_Script.py's Method B (Details
page -> letter_ids -> _PrintLetters popup) and its tag-aware
extract_response_text() fix, cut down to exactly what request_status_sync.py
needs: given a request_number that SendRequestStatusJson already told us
is at status "خطاب ادارى" / "خطاب إداري" (an administrative letter --
usually means the decree request was declined or redirected rather than
granted), fetch that letter's own نص الخطاب (response text) and committee
date, so the app can show WHY, not just THAT.

Deliberately reuses this repo's own smc_session.SMCSession (same account
request_status_sync.py already logs in with -- SMC_USERNAME_2) instead of
the unified script's separate session class, so there's exactly one login
per run.

A single request can (rarely) have more than one letter attached to it
over its lifetime (e.g. a follow-up correspondence round) -- get_letters_
for_request() returns all of them, most-recent-committee-date first; the
caller decides whether it wants just the latest or the full history.
"""

import re
import logging
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

import smc_session as smc

BASE_URL = smc.BASE_URL

# The site spells this status inconsistently across pages: the plain
# request-list JSON (SendRequestStatusJson, used by request_status_sync.py)
# comes back "خطاب ادارى" (no hamza), while the letter popups themselves
# say "خطاب إداري" (with hamza). Match both everywhere so a spelling
# difference never silently hides a real admin letter.
ADMIN_LETTER_STATUS_VARIANTS = {"خطاب ادارى", "خطاب إداري"}


def is_admin_letter_status(status: Optional[str]) -> bool:
    return bool(status) and status.strip() in ADMIN_LETTER_STATUS_VARIANTS


# =====================================================================
# Text extraction (ported verbatim in spirit from the unified script's
# "THE FIX" -- tag-aware regex first, pipe-split fallback second, so a
# multi-line/multi-paragraph letter response is never truncated)
# =====================================================================
def _pipe_field(pipe_text: str, label: str) -> str:
    parts = [p.strip() for p in pipe_text.split("|")]
    for i, part in enumerate(parts):
        if label in part and i + 1 < len(parts):
            return parts[i + 1].strip()
    return ""


def _ar2en(text: str) -> str:
    t = {"٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
         "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9"}
    return "".join(t.get(c, c) for c in (text or ""))


def extract_response_text(html: str, pipe_text: str) -> str:
    """Walk the real HTML from '<b>نص الخطاب</b><br/>' up to the next
    <b>/cell/row boundary, then strip inner tags -- survives multi-line
    responses. Falls back to a naive pipe-split only if that tag-aware
    pattern isn't found at all."""
    m = re.search(
        r"نص الخطاب\s*</b>\s*<br\s*/?>\s*(.*?)(?:<b>|</td>|</tr>|$)",
        html, re.DOTALL,
    )
    if m:
        candidate = BeautifulSoup(m.group(1), "html.parser").get_text(" ", strip=True)
        if candidate:
            return candidate
    return _pipe_field(pipe_text, "نص الخطاب")


def _extract_committee_date(html: str, pipe_text: str) -> str:
    m = re.search(r"تاريخ اللجنة\s*<br\s*/?>\s*([\d٠-٩]{2}-[\d٠-٩]{2}-[\d٠-٩]{4})", html)
    if m:
        return _ar2en(m.group(1)).strip()
    raw = _pipe_field(pipe_text, "تاريخ اللجنة")
    return _ar2en(raw).strip() if raw else ""


# =====================================================================
# STEP A — Details page -> letter_ids attached to this request
# =====================================================================
def get_letter_ids_for_request(session: smc.SMCSession, request_number: str) -> List[str]:
    """Same DecreesReadyToSend-style regex the unified script uses, just
    for the _PrintLetters?RecommIDs= links instead of the decree ones."""
    url = f"{BASE_URL}/smc/Requests/Details/{request_number}"
    try:
        resp = session.session.get(url, timeout=30)
        resp.encoding = "utf-8"
    except Exception as e:
        logging.error(f"[admin_letter_lookup] Details fetch failed for {request_number}: {e}")
        return []
    if resp.status_code != 200:
        logging.warning(f"[admin_letter_lookup] Details for {request_number} returned HTTP {resp.status_code}.")
        return []

    letter_ids = set()
    for m in re.finditer(r"_PrintLetters\?RecommIDs='\s*\+\s*(\d+)", resp.text):
        letter_ids.add(m.group(1))
    # Site has been seen ordering these newest-last; caller wants
    # most-recent-first, and lacking a real timestamp per id, the
    # highest id is the newest as they're clearly sequential.
    return sorted(letter_ids, key=int, reverse=True)


# =====================================================================
# STEP B — _PrintLetters popup -> committee date + response text
# =====================================================================
def get_letter_response(session: smc.SMCSession, letter_id: str) -> Dict:
    url = f"{BASE_URL}/smc/Requests/_PrintLetters"
    result = {"letter_id": letter_id, "committee_date": "", "response_text": ""}
    try:
        resp = session.session.get(url, params={"RecommIDs": f"{letter_id},"}, timeout=30)
        resp.encoding = "utf-8"
    except Exception as e:
        logging.error(f"[admin_letter_lookup] _PrintLetters fetch failed for {letter_id}: {e}")
        return result
    if resp.status_code != 200:
        logging.warning(f"[admin_letter_lookup] _PrintLetters for {letter_id} returned HTTP {resp.status_code}.")
        return result

    soup = BeautifulSoup(resp.text, "html.parser")
    pipe_text = soup.get_text(separator="|", strip=True)
    html = str(soup)
    result["committee_date"] = _extract_committee_date(html, pipe_text)
    result["response_text"] = extract_response_text(html, pipe_text)
    return result


# =====================================================================
# Combined: everything request_status_sync.py needs for one request_number
# =====================================================================
def get_letters_for_request(session: smc.SMCSession, request_number: str, delay: float = 0.3) -> List[Dict]:
    """Returns every admin-letter response attached to this request,
    most-recent first (see get_letter_ids_for_request's ordering note).
    Empty list if the request has no letter_ids at all (shouldn't
    normally happen for a request whose status IS an admin-letter
    status, but the site's own data can be messy -- never raises)."""
    import time
    letter_ids = get_letter_ids_for_request(session, request_number)
    if not letter_ids:
        logging.warning(f"[admin_letter_lookup] {request_number}: status is an admin-letter status "
                         f"but no letter_ids found on its Details page.")
        return []
    out = []
    for lid in letter_ids:
        out.append(get_letter_response(session, lid))
        time.sleep(delay)
    return out
