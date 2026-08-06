"""
supabase_client.py

Minimal PostgREST upsert helper. Uses the service_role key (bypasses RLS —
this is the trusted daily loader, not the app's anon-key client).

Env vars required:
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
"""

import os
import logging
import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def _headers(on_conflict: str):
    return {
        "apikey": SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        # merge-duplicates = upsert on the unique constraint given by on_conflict
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }


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
        resp = requests.post(url, headers=_headers(on_conflict), json=batch, timeout=60)
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"[{table}] upsert failed ({resp.status_code}): {resp.text[:500]}")
        total += len(batch)
    logging.info(f"[{table}] upserted {total} row(s).")
