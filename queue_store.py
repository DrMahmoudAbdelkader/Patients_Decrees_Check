"""
queue_store.py -- reads the day's queue from the private relay (Cloudflare Worker + R2)
instead of scraping HMIS. This module NEVER talks to the upstream queue API, and it
never logs the relay URL (this repo's Actions logs are public).

Env (GitHub Actions secrets):  QUEUE_RELAY_URL, QUEUE_RELAY_SECRET
"""
import os
import logging
import requests


class QueueNotReady(RuntimeError):
    """Queue for the requested date has not been downloaded to the store yet."""


def _cfg():
    url = os.environ.get("QUEUE_RELAY_URL", "").rstrip("/")
    secret = os.environ.get("QUEUE_RELAY_SECRET", "")
    if not url or not secret:
        raise RuntimeError("QUEUE_RELAY_URL / QUEUE_RELAY_SECRET are not set.")
    return url, secret


def load_queue_records(date_iso: str) -> list:
    url, secret = _cfg()
    try:
        resp = requests.get(
            f"{url}/queue",
            params={"date": date_iso},
            headers={"Authorization": f"Bearer {secret}"},
            timeout=30,
        )
    except requests.RequestException as e:
        # type only: the exception text can contain the URL
        raise RuntimeError(f"Queue store unreachable ({type(e).__name__}).") from None

    if resp.status_code == 404:
        raise QueueNotReady(
            f"Queue data for {date_iso} is not in the store yet. "
            f"Press 'Download queue' first, then run again."
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Queue store returned HTTP {resp.status_code}.")

    payload = resp.json()
    if payload.get("date") != date_iso:   # same safety net as verify_report_date_range
        raise RuntimeError(f"Store date mismatch: asked {date_iso}, got {payload.get('date')}.")
    records = payload.get("records") or []
    logging.info(f"[queue-store] {len(records)} record(s) for {date_iso} "
                 f"(fetched_at={payload.get('fetched_at')}).")
    return records
