"""
cairo_date.py — one place to answer "what day is it, in Cairo?"

GitHub Actions runners run in UTC. If you just use datetime.now() the
"today" the script computes can be wrong by an hour (or, right around
midnight, by a whole day) versus what a person in Cairo means by "today".
Every script in this pipeline that needs "today's date" should get it from
here instead of calling datetime.now()/date.today() directly.

Uses the stdlib zoneinfo (Python 3.9+). No extra dependency needed.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

CAIRO_TZ = ZoneInfo("Africa/Cairo")


def cairo_now() -> datetime:
    """Current datetime, correctly localized to Africa/Cairo."""
    return datetime.now(CAIRO_TZ)


def cairo_today_iso() -> str:
    """Today's date in Cairo, as YYYY-MM-DD (what the SMC APIs in this
    pipeline expect for dateFrom/dateTo/StartDate/EndDate)."""
    return cairo_now().strftime("%Y-%m-%d")


def cairo_today_slash() -> str:
    """Today's date in Cairo, as DD/MM/YYYY (used by some SMC pagination
    links / older report endpoints)."""
    return cairo_now().strftime("%d/%m/%Y")
