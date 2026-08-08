"""
supabase_client.py

Minimal PostgREST helper. Uses the service_role key (bypasses RLS — this
is the trusted daily loader, not the app's anon-key client).

Env vars required:
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
"""

import os
import logging
import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def _headers(extra=None):
    h = {
        "apikey": SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        # merge-duplicates = upsert on the unique constraint given by on_conflict
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    if extra:
        h.update(extra)
    return h


def _chunk(rows, size=500):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def upsert(table: str, rows: list, on_conflict: str):
    """
    POST rows to <table> with an upsert against the given unique
    constraint columns (comma-separated string, matching supabase_schema.sql).
    """
    if not rows:
        logging.info(f"[{table}] nothing to upsert.")
        return
    if not SUPABASE_URL or not SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set.")

    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    total = 0
    for batch in _chunk(rows):
        resp = requests.post(url, headers=_headers(), json=batch, timeout=60)
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"[{table}] upsert failed ({resp.status_code}): {resp.text[:500]}")
        total += len(batch)
    logging.info(f"[{table}] upserted {total} row(s).")


def fetch_all(table: str, select: str, filters: str = "") -> list:
    """
    GET every row of <table> matching optional PostgREST query-string
    filters (e.g. 'promoted=eq.false'), paginating with the Range header
    since a single request is capped server-side. Used for the (small)
    mapping tables and for bounded promotion queries -- not meant for
    the big fact tables (decree_issued_decrees etc.), which the app's
    own chunked fetchInChunks()/fetchAllPages() already handle.
    """
    if not SUPABASE_URL or not SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set.")

    base = f"{SUPABASE_URL}/rest/v1/{table}?select={select}"
    if filters:
        base += f"&{filters}"

    rows, offset, page = [], 0, 1000
    while True:
        resp = requests.get(
            base,
            headers=_headers({"Range": f"{offset}-{offset + page - 1}"}),
            timeout=30,
        )
        if resp.status_code not in (200, 206):
            raise RuntimeError(f"[{table}] fetch failed ({resp.status_code}): {resp.text[:300]}")
        batch = resp.json()
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def patch(table: str, filters: str, body: dict):
    """PATCH rows matching filters (PostgREST query string, e.g.
    'decree_description=eq.foo&decree_unique_name=is.null') with body.
    Used for the bounded 'promote matching pending rows' updates."""
    if not SUPABASE_URL or not SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set.")
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
    resp = requests.patch(url, headers=_headers({"Prefer": "return=minimal"}), json=body, timeout=60)
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"[{table}] patch failed ({resp.status_code}): {resp.text[:500]}")
