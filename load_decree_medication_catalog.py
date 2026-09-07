"""
load_decree_medication_catalog.py — (re-)loads decree_medication_catalog
from your two source Excel files. Run this any time you update either
file (e.g. after filling in more Average_Dose_Value cells, or adding a
brand-new medication decree description).

Merge key: the EXACT raw Decree_Description text, matched between the
two files. If a description exists in the map file but not the cutoff
file, it's still loaded (average_dose_value = NULL, needs_cutoff_value
= True, unless it's a Supportive/Pain Management row, which never
needs one). If a description exists ONLY in the cutoff file (a handful
of near-duplicate/typo rows were found last time), it's loaded too --
with treatment_plan_name = NULL, so query it directly to notice and
fix the underlying description in the map file.

Duplicate raw descriptions in the map file (exact repeated rows) are
silently collapsed to one, keeping the first occurrence.

Usage:
    python load_decree_medication_catalog.py \\
        --map-file Decrees_Description_Map.xlsx \\
        --cutoff-file Decreees_Cutt_Off_Values.xlsx
    python load_decree_medication_catalog.py ... --dry-run
"""

import os
import sys
import logging
import argparse

sys.path.insert(0, os.path.dirname(__file__))

import openpyxl
import supabase_client as sb

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

TABLE = "decree_medication_catalog"
SUPPORTIVE_NAMES = {"Supportive", "Pain Management"}


def _read_sheet(path, columns):
    """Returns a list of dicts using the header row's column names,
    for whichever of `columns` are present (missing ones come back as
    None per row rather than raising)."""
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    header = [c.value for c in ws[1]]
    idx = {name: header.index(name) for name in columns if name in header}
    missing = [c for c in columns if c not in idx]
    if missing:
        logging.warning(f"[{path}] missing expected column(s): {missing}")

    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        row = {name: (r[i] if i < len(r) else None) for name, i in idx.items()}
        rows.append(row)
    return rows


def load_map_file(path):
    """Returns {decree_description: {treatment_plan_name, is_cycles, procedure_code}},
    first-occurrence-wins on duplicate descriptions."""
    rows = _read_sheet(path, [
        "Decree_Description", "Modified_Description", "Is_Cycles",
        "Treatment_Plan_Procedure_Code_At_SMC_Website",
    ])
    out = {}
    dup_count = 0
    for row in rows:
        desc = (row.get("Decree_Description") or "").strip()
        if not desc:
            continue
        if desc in out:
            dup_count += 1
            continue
        out[desc] = {
            "treatment_plan_name": (row.get("Modified_Description") or "").strip() or None,
            "is_cycles": str(row.get("Is_Cycles") or "").strip().lower() == "yes",
            "procedure_code": row.get("Treatment_Plan_Procedure_Code_At_SMC_Website"),
        }
    if dup_count:
        logging.info(f"[{path}] {dup_count} duplicate description row(s) collapsed to first occurrence.")
    return out


def load_cutoff_file(path):
    """Returns {decree_description: {average_dose_value, is_cycles_decree}}."""
    rows = _read_sheet(path, ["Decree_Description", "Average_Dose_Value", "Is_Cycles_Decree"])
    out = {}
    for row in rows:
        desc = (row.get("Decree_Description") or "").strip()
        if not desc:
            continue
        out[desc] = {
            "average_dose_value": row.get("Average_Dose_Value"),
            "is_cycles_decree": str(row.get("Is_Cycles_Decree") or "").strip().lower() == "yes",
        }
    return out


def build_catalog_rows(map_data: dict, cutoff_data: dict) -> list:
    all_descriptions = set(map_data) | set(cutoff_data)
    rows = []
    mismatches = []

    for desc in sorted(all_descriptions):
        m = map_data.get(desc, {})
        c = cutoff_data.get(desc, {})

        treatment_plan_name = m.get("treatment_plan_name")
        is_cycles = m.get("is_cycles", c.get("is_cycles_decree", False))

        if "is_cycles" in m and "is_cycles_decree" in c and m["is_cycles"] != c["is_cycles_decree"]:
            mismatches.append(desc)

        is_supportive = treatment_plan_name in SUPPORTIVE_NAMES
        average_dose_value = c.get("average_dose_value")
        needs_cutoff_value = (average_dose_value is None) and not is_supportive

        rows.append({
            "decree_description": desc,
            "treatment_plan_name": treatment_plan_name,
            "is_cycles": bool(is_cycles),
            "average_dose_value": average_dose_value,
            "procedure_code": m.get("procedure_code"),
            "is_supportive": is_supportive,
            "needs_cutoff_value": needs_cutoff_value,
        })

    if mismatches:
        logging.warning(
            f"{len(mismatches)} description(s) have DIFFERENT Is_Cycles between the two files -- "
            f"loaded using the map file's value, but please double-check: {mismatches[:10]}"
            + (" ..." if len(mismatches) > 10 else "")
        )

    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--map-file", required=True, help="Path to Decrees_Description_Map.xlsx")
    parser.add_argument("--cutoff-file", required=True, help="Path to Decreees_Cutt_Off_Values.xlsx")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    map_data = load_map_file(args.map_file)
    cutoff_data = load_cutoff_file(args.cutoff_file)
    logging.info(f"Map file: {len(map_data)} unique description(s). Cutoff file: {len(cutoff_data)} description(s).")

    rows = build_catalog_rows(map_data, cutoff_data)
    needs_cutoff = [r for r in rows if r["needs_cutoff_value"]]
    logging.info(f"Built {len(rows)} catalog row(s); {len(needs_cutoff)} still need an average_dose_value.")

    if args.dry_run:
        import csv
        out_path = "./decree_medication_catalog_preview.csv"
        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        logging.info(f"(dry-run) wrote preview -> {out_path}")
        return

    sb.upsert(TABLE, rows, on_conflict="decree_description")
    logging.info(f"Loaded {len(rows)} row(s) into '{TABLE}'.")


if __name__ == "__main__":
    main()
