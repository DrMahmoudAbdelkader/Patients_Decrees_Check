"""
decree_category.py — classifies each decree's raw description into a
broad TYPE category (medication, scan, surgery, intervention,
pathology, radiotherapy, ... -- whatever categories you define), so
the app can show/filter/sort decrees by type instead of only by
medication-regimen status.

WHY A SEPARATE STEP FROM decree_medication_catalog
------------------------------------------------------
decree_medication_catalog already tells you, implicitly, "this is a
medication decree" -- every raw description in that table IS one, by
construction (that's the table your regimen classification in
patient_decree_value.py is keyed on). So:

    1. If a decree's raw description is in decree_medication_catalog
       -> category = "medication", automatically, no review needed.
    2. Otherwise -> look it up in the NEW decree_category_map table
       (same pending/mapped/ignored shape as decree_name_map /
       item_name_map / request_name_map -- reuses the exact same
       NameMap class from decree_name_map.py) so you can manually
       assign scan / surgery / intervention / pathology / radiotherapy
       / whatever categories you want, the same way you already
       review unmapped decree descriptions today.

A raw description seen for the first time comes back category=None,
action='pending', and is queued into decree_category_map so it shows
up for review -- never guessed at.

SETUP (run once against Supabase, matching decree_name_map's existing
table shape -- adjust types/defaults if your other three map tables
differ):

    create table decree_category_map (
        raw_text    text primary key,
        category    text,                 -- null until reviewed
        status      text not null default 'pending',  -- pending|mapped|ignored
        times_seen  integer not null default 1,
        last_seen   timestamptz,
        created_at  timestamptz not null default now()
    );

Pick whatever category strings you like when reviewing rows (e.g.
'medication' is reserved/automatic -- don't manually assign it to a
non-catalog row, or it'll look auto-classified when it wasn't).

Usage:
    category, action = categorize_decree(raw_description)
    # action: 'mapped' (category is usable) | 'pending' (needs review,
    # category is None) | 'ignored' (category is None, permanently)
"""

import logging
from typing import Optional, Tuple

import supabase_client as sb
from decree_name_map import NameMap

MEDICATION_CATALOG_TABLE = "decree_medication_catalog"
AUTO_MEDICATION_CATEGORY = "medication"

_medication_descriptions_cache: Optional[set] = None
_category_map: Optional[NameMap] = None


def _load_medication_descriptions() -> set:
    global _medication_descriptions_cache
    if _medication_descriptions_cache is not None:
        return _medication_descriptions_cache
    try:
        rows = sb.fetch_all(MEDICATION_CATALOG_TABLE, "decree_description")
    except Exception as e:
        logging.error(
            f"[category] could not load {MEDICATION_CATALOG_TABLE} ({e}) -- "
            f"no decree will auto-classify as '{AUTO_MEDICATION_CATEGORY}' this run."
        )
        rows = []
    _medication_descriptions_cache = {
        r["decree_description"] for r in rows if r.get("decree_description")
    }
    logging.info(f"[category] loaded {len(_medication_descriptions_cache)} medication description(s).")
    return _medication_descriptions_cache


def reset_category_cache():
    """Call in a long-lived process if decree_medication_catalog or
    decree_category_map changes mid-run. Not needed for the one-shot
    CLI scripts in this pipeline."""
    global _medication_descriptions_cache, _category_map
    _medication_descriptions_cache = None
    _category_map = None


def get_category_map() -> NameMap:
    global _category_map
    if _category_map is None:
        _category_map = NameMap("decree_category_map", "category")
    return _category_map


def categorize_decree(raw_description: Optional[str]) -> Tuple[Optional[str], str]:
    """Returns (category, action) -- action is 'mapped' | 'pending' |
    'ignored', matching decree_name_map's NameMap.lookup() convention.
    Medication decrees resolve automatically from decree_medication_catalog
    and never touch decree_category_map at all."""
    if not raw_description:
        return None, "ignored"
    raw_description = str(raw_description).strip()
    if not raw_description:
        return None, "ignored"

    if raw_description in _load_medication_descriptions():
        return AUTO_MEDICATION_CATEGORY, "mapped"

    return get_category_map().lookup(raw_description)
