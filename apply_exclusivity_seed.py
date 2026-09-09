"""
apply_exclusivity_seed.py -- one-shot loader that PATCHes the new
columns (exclusivity_group, financial_review_scope, depot_value,
depot_interval_months, partner_value) onto EXISTING rows of
decree_medication_catalog, matched by exact decree_description.

This does NOT create new catalog rows -- it only annotates rows that
load_decree_medication_catalog.py already loaded from your map/cutoff
files. A description in the CSV with no matching catalog row is
skipped and reported so you can add it to the map file first.

Run this AFTER you've reviewed/edited exclusivity_seed_review.csv by
hand -- it is a first-pass draft, not a ground truth (see that file's
own generating script's docstring for exactly what it did and didn't
try to classify).

Usage:
    python apply_exclusivity_seed.py --csv exclusivity_seed_review.csv --dry-run
    python apply_exclusivity_seed.py --csv exclusivity_seed_review.csv
"""

import os
import sys
import csv
import logging
import argparse

sys.path.insert(0, os.path.dirname(__file__))

import supabase_client as sb

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

TABLE = "decree_medication_catalog"


def _clean(value):
    if value is None or value == "":
        return None
    return value


def load_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            desc = (r.get("decree_description") or "").strip()
            if not desc:
                continue
            rows.append({
                "decree_description": desc,
                "exclusivity_group": _clean((r.get("exclusivity_group") or "").strip()),
                "financial_review_scope": _clean((r.get("financial_review_scope") or "").strip()) or "in_scope",
                "depot_value": _clean((r.get("depot_value") or "").strip()),
                "depot_interval_months": _clean((r.get("depot_interval_months") or "").strip()),
                "partner_value": _clean((r.get("partner_value") or "").strip()),
            })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", required=True, help="Path to the reviewed exclusivity_seed_review.csv")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be patched, don't touch Supabase")
    args = parser.parse_args()

    seed_rows = load_csv(args.csv)
    logging.info(f"{len(seed_rows)} row(s) loaded from {args.csv}")

    existing = sb.fetch_all(TABLE, "decree_description")
    existing_descs = {r["decree_description"] for r in existing if r.get("decree_description")}

    matched, unmatched, skipped = [], [], 0
    for row in seed_rows:
        desc = row["decree_description"]
        if desc not in existing_descs:
            unmatched.append(desc)
            continue
        body = {k: v for k, v in row.items() if k != "decree_description"}
        # A row with literally nothing to set (every new field blank) --
        # don't bother sending a no-op PATCH.
        if not any(body.get(k) not in (None, "in_scope") for k in body) and body.get("financial_review_scope") == "in_scope":
            skipped += 1
            continue
        matched.append((desc, body))

    logging.info(f"{len(matched)} row(s) will be patched, {len(unmatched)} description(s) not found in the catalog, "
                 f"{skipped} row(s) had nothing to change.")

    if unmatched:
        logging.warning("Not found in decree_medication_catalog (add to your map file first if these should exist):")
        for u in unmatched[:20]:
            logging.warning(f"  - {u}")
        if len(unmatched) > 20:
            logging.warning(f"  ... and {len(unmatched) - 20} more")

    if args.dry_run:
        for desc, body in matched[:10]:
            logging.info(f"(dry-run) would patch '{desc[:60]}...' -> {body}")
        logging.info(f"(dry-run) {len(matched)} total patch(es) prepared -- nothing sent.")
        return

    done = 0
    for desc, body in matched:
        escaped = desc.replace(",", "%2C")
        sb.patch(TABLE, f"decree_description=eq.{escaped}", body)
        done += 1
        if done % 25 == 0:
            logging.info(f"Patched {done}/{len(matched)}...")

    logging.info(f"Done -- {done} catalog row(s) patched.")


if __name__ == "__main__":
    main()
