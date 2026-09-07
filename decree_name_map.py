"""
decree_name_map.py

Mapping tables now live in Supabase (decree_name_map, item_name_map,
request_name_map) instead of the two committed Excel files. Loaded fresh
at the start of every run, so a mapping you add in-app this morning is
picked up by tonight's cron run without touching this repo.

Each table has a `status` column: 'pending' | 'mapped' | 'ignored'.
  - mapped   -> raw_text has a confirmed unique value; used for lookups.
  - ignored  -> raw_text is confirmed irrelevant (non-medication service,
                junk request text, etc.) -- EXCLUDE this row outright,
                same as the old Excel #N/A behavior, and never re-touch
                its times_seen/last_seen.
  - pending  -> seen at least once, no decision yet. Per your "map it ->
                included right away" requirement, rows for a pending raw
                value are NOT dropped anymore -- daily_sync.py keeps them
                with decree_unique_name / unique_item_name = NULL. The
                app's "map it" action (needs-review.js) promotes those
                NULL rows to the chosen unique_name the moment you submit
                it -- see the WHERE-bounded UPDATE in that file. This
                module's job is only to keep the map tables themselves in
                sync: insert new pending rows, bump times_seen/last_seen
                on ones already pending. It never writes 'mapped' or
                'ignored' -- only the app does that.

normalize_decree_name() / normalize_item_name() / normalize_request_text()
all return (value_or_None, action) where action is one of:
    'mapped'   -> value is the unique name/description, include normally
    'pending'  -> value is None, include the row with a NULL unique name
    'ignored'  -> exclude the row entirely, do not touch the map row
"""

import os
import logging
from datetime import datetime, timezone

import supabase_client as sb

_ORIGINAL_HINTS = ("original", "raw", "description", "name")  # kept for reference; no longer used for Excel


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class NameMap:
    """One loaded map table (decree_name_map / item_name_map / request_name_map)."""

    def __init__(self, table: str, unique_col: str):
        self.table = table
        self.unique_col = unique_col
        self.mapped = {}      # raw_text -> unique value
        self.ignored = set()  # raw_text
        self.pending = set()  # raw_text already sitting as 'pending' in the table
        self._load()

    def _load(self):
        try:
            rows = sb.fetch_all(self.table, f"raw_text,{self.unique_col},status")
        except Exception as e:
            logging.error(
                f"[{self.table}] could not load from Supabase ({e}) -- treating as "
                f"empty for this run. EVERYTHING will come back 'pending' until this "
                f"is fixed -- rows will still be kept (not dropped), just unmatched."
            )
            rows = []
        for r in rows:
            raw = r.get("raw_text")
            if not raw:
                continue
            status = r.get("status")
            if status == "mapped" and r.get(self.unique_col):
                self.mapped[raw] = r[self.unique_col]
            elif status == "ignored":
                self.ignored.add(raw)
            else:
                self.pending.add(raw)
        logging.info(
            f"[{self.table}] loaded {len(self.mapped)} mapped, "
            f"{len(self.ignored)} ignored, {len(self.pending)} pending."
        )

    def lookup(self, raw_text: str):
        if not raw_text:
            return None, "ignored"  # nothing to map -- treat like excluded, not a pending row
        raw_text = str(raw_text).strip()
        if not raw_text:
            return None, "ignored"
        if raw_text in self.mapped:
            return self.mapped[raw_text], "mapped"
        if raw_text in self.ignored:
            return None, "ignored"
        return None, "pending"

    def push_pending(self, seen_raw_texts: set):
        """Upsert every raw_text seen this run that came back 'pending'.
        Only sends {raw_text, last_seen} -- the DB trigger (see
        supabase_schema_additions.sql) handles times_seen and refuses to
        downgrade a row that became 'mapped'/'ignored' since load time."""
        if not seen_raw_texts:
            return
        now = _now_iso()
        rows = [{"raw_text": t, "last_seen": now} for t in sorted(seen_raw_texts)]
        sb.upsert(self.table, rows, on_conflict="raw_text")
        logging.info(f"[{self.table}] upserted {len(rows)} pending raw value(s).")


_decree_map = None
_item_map = None
_request_map = None


def get_decree_map() -> NameMap:
    global _decree_map
    if _decree_map is None:
        _decree_map = NameMap("decree_name_map", "unique_name")
    return _decree_map


def get_item_map() -> NameMap:
    global _item_map
    if _item_map is None:
        _item_map = NameMap("item_name_map", "unique_name")
    return _item_map


def get_request_map() -> NameMap:
    global _request_map
    if _request_map is None:
        _request_map = NameMap("request_name_map", "decree_unique_name")
    return _request_map


def normalize_decree_name(raw_description: str):
    return get_decree_map().lookup(raw_description)


def normalize_item_name(raw_item_name: str):
    return get_item_map().lookup(raw_item_name)


def normalize_request_text(raw_text: str):
    return get_request_map().lookup(raw_text)
