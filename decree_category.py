"""
decree_category.py — classifies each decree's raw description into a
broad TYPE category (medication, scan, surgery, intervention,
pathology, laboratory, icu, radiotherapy, ... -- whatever categories
you define), so the app can show/filter/sort decrees by type instead
of only by medication-regimen status.

REVISION (fixes "everything shows as مستحضر دوائي / medication")
------------------------------------------------------------------
The catalog table (decree_medication_catalog) used to hold ONLY
medication decrees, so "raw description is in the catalog" and "this
decree is a medication" were the same fact -- hence the old
`AUTO_MEDICATION_CATEGORY` shortcut below, which returned 'medication'
for every row found in the catalog, no matter what it actually was.

You've since widened that catalog to also cover scans, surgeries,
interventions, pathology, etc., and added a `service_type_Catalog`
column that records each row's REAL type. The old shortcut ignored
that column completely, so every decree whose description happened to
be in the catalog kept coming back as 'medication' even when its own
service_type_Catalog said "Scan" or "Surgery" -- that was the bug.

This version reads `service_type_Catalog` per row and uses THAT as the
category, instead of assuming 'medication'. A catalog row with that
column left blank (e.g. an older row saved before you added the
column) still defaults to 'medication', since every row in this table
used to be medication-only -- so nothing already-classified silently
changes without a real reason to.

WHY A SEPARATE STEP FROM decree_medication_catalog STILL EXISTS FOR
NON-CATALOG DECREES
------------------------------------------------------------------
    1. If a decree's raw description is in decree_medication_catalog
       -> category = that row's service_type_Catalog (normalized),
       or 'medication' if the column is blank -- automatically, no
       review needed.
    2. Otherwise -> look it up in the decree_category_map table (same
       pending/mapped/ignored shape as decree_name_map / item_name_map
       / request_name_map -- reuses the exact same NameMap class from
       decree_name_map.py) so you can manually assign a category the
       same way you already review unmapped decree descriptions today.

A raw description seen for the first time, and not in the catalog,
comes back category=None, action='pending', and is queued into
decree_category_map so it shows up for review -- never guessed at.

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

Pick whatever category strings you like when reviewing rows there.
'medication' still means "this is a medication" -- don't manually
assign it to a non-catalog row, or it'll look auto-classified when it
wasn't.

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
CATALOG_CATEGORY_COLUMN = "service_type_Catalog"  # exact case, as created in the DDL

# Every catalog row used to be a medication by construction, so a row
# whose service_type_Catalog is blank/NULL (e.g. one saved before you
# added the column) still defaults here -- this is a safety default,
# NOT a guess about rows that DO have a value set.
DEFAULT_CATALOG_CATEGORY = "medication"

# Normalizes whatever free-text you type into service_type_Catalog
# (case/spacing-insensitive) to the fixed set of category keys the
# app's badges (CATEGORY_BADGES in decree-daily-scan.js) know how to
# render nicely. Add a line here any time you introduce a new value in
# the sheet -- anything NOT listed here still comes through as its own
# lowercased/underscored key rather than being dropped; the app will
# just show it with a neutral grey badge and its literal text until
# you add both a mapping here and a badge for it in the JS module.
CATALOG_CATEGORY_ALIASES = {
    "medication": "medication",
    "meds": "medication",
    "drug": "medication",
    "scan": "scan",
    "imaging": "scan",
    "radiology": "scan",
    "surgery": "surgery",
    "operation": "surgery",
    "intervention": "intervention",
    "pathology": "pathology",
    "lab": "laboratory",
    "laboratory": "laboratory",
    "icu": "icu",
    "radiotherapy": "radiotherapy",
    "radiation": "radiotherapy",
    "chemotherapy": "medication",
}

_catalog_categories_cache: Optional[dict] = None
_category_map: Optional[NameMap] = None


def _normalize_catalog_category(raw_value) -> Optional[str]:
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if not text:
        return None
    key = text.lower().replace(" ", "_").replace("-", "_")
    return CATALOG_CATEGORY_ALIASES.get(key, key)


def _load_catalog_categories() -> dict:
    """decree_description -> normalized category, for every row in
    decree_medication_catalog, read straight from that row's own
    service_type_Catalog column (falling back to 'medication' only
    when the column is blank -- see DEFAULT_CATALOG_CATEGORY above)."""
    global _catalog_categories_cache
    if _catalog_categories_cache is not None:
        return _catalog_categories_cache
    try:
        rows = sb.fetch_all(
            MEDICATION_CATALOG_TABLE,
            f"decree_description,{CATALOG_CATEGORY_COLUMN}",
        )
    except Exception as e:
        logging.error(
            f"[category] could not load {MEDICATION_CATALOG_TABLE} ({e}) -- "
            f"no decree will auto-classify from the catalog this run."
        )
        rows = []

    mapping = {}
    blank_count = 0
    for r in rows:
        desc = r.get("decree_description")
        if not desc:
            continue
        normalized = _normalize_catalog_category(r.get(CATALOG_CATEGORY_COLUMN))
        if normalized is None:
            normalized = DEFAULT_CATALOG_CATEGORY
            blank_count += 1
        mapping[desc] = normalized

    _catalog_categories_cache = mapping
    logging.info(
        f"[category] loaded {len(mapping)} catalog description(s) with per-row service type "
        f"({blank_count} blank -> defaulted to '{DEFAULT_CATALOG_CATEGORY}')."
    )
    return mapping


def reset_category_cache():
    """Call in a long-lived process if decree_medication_catalog or
    decree_category_map changes mid-run. Not needed for the one-shot
    CLI scripts in this pipeline."""
    global _catalog_categories_cache, _category_map
    _catalog_categories_cache = None
    _category_map = None


def get_category_map() -> NameMap:
    global _category_map
    if _category_map is None:
        _category_map = NameMap("decree_category_map", "category")
    return _category_map


def categorize_decree(raw_description: Optional[str]) -> Tuple[Optional[str], str]:
    """Returns (category, action) -- action is 'mapped' | 'pending' |
    'ignored', matching decree_name_map's NameMap.lookup() convention.
    A decree whose raw description is in decree_medication_catalog
    resolves automatically from that row's OWN service_type_Catalog
    value and never touches decree_category_map at all."""
    if not raw_description:
        return None, "ignored"
    raw_description = str(raw_description).strip()
    if not raw_description:
        return None, "ignored"

    catalog_categories = _load_catalog_categories()
    if raw_description in catalog_categories:
        return catalog_categories[raw_description], "mapped"

    return get_category_map().lookup(raw_description)
