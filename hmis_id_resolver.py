"""
hmis_id_resolver.py — resolves internal HMIS "Medical No." values (the
his_mr your queue report now gives you, since outpat_clnc_lst_det_j
has no national-ID column of its own) to each patient's 14-digit
national ID, needed everywhere downstream that keys off national_id
(SMC lookups, decree_queue_data, ...).

Cache-first: checks economy_patient_registry (Supabase) for a mapping
already seen before doing a live HMIS lookup, so a patient queued
again on a later day doesn't trigger a fresh HMIS scrape every single
run. Every live lookup's result is written back into that same table
-- the same table the hmis-patient-lookup edge function's own MR
lookups already populate (see that function's upsertEconomyPatientRegistry),
so both stay one shared, always-warming source of truth instead of two
independent caches.

A Medical No. that can't be resolved this run (blank, HMIS lookup
failure, genuinely not found) is simply left out of the returned dict
-- never guessed at. Callers should drop that row rather than write a
national_id of None/"" into a table where it's the join key.

Usage:
    resolver = HmisIdResolver()
    national_id_by_mr = resolver.resolve(["5570", "6745", ...])
    # -> {"5570": "29001011234567", ...}  (missing keys = unresolved)
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import hmis_session as hmis
import supabase_client as sb

REGISTRY_TABLE = "economy_patient_registry"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ddmmyyyy_to_iso(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


class HmisIdResolver:
    """One instance = at most one HMIS login for however many
    resolve() calls you make with it -- share one instance across a
    run instead of creating a new one per batch."""

    def __init__(self):
        self._session: Optional[hmis.HMISSession] = None

    def _get_session(self) -> hmis.HMISSession:
        if self._session is None:
            session = hmis.HMISSession()
            if not session.login():
                raise RuntimeError("HMIS login failed — cannot resolve Medical No. -> national ID.")
            self._session = session
        return self._session

    def _load_cached(self, mrs: List[str]) -> Dict[str, str]:
        if not mrs:
            return {}
        in_list = ",".join(f'"{m}"' for m in mrs)
        rows = sb.fetch_all(
            REGISTRY_TABLE,
            "his_mr,national_id",
            filters=f"his_mr=in.({in_list})",
        )
        return {r["his_mr"]: r["national_id"] for r in rows if r.get("national_id")}

    def resolve(self, medical_numbers: List[str]) -> Dict[str, str]:
        """Returns {medical_no: national_id} for every Medical No. that
        could be resolved (cached or freshly looked up this call). A
        medical_no missing from the result could not be resolved this
        run — see the warnings logged for why."""
        mrs = sorted({str(m).strip() for m in medical_numbers if m and str(m).strip()})
        if not mrs:
            return {}

        try:
            cached = self._load_cached(mrs)
        except Exception as e:
            logging.warning(
                f"[hmis-id] could not load {REGISTRY_TABLE} cache ({e}) — "
                f"will look up everything live this run."
            )
            cached = {}

        result = dict(cached)
        missing = [m for m in mrs if m not in cached]
        logging.info(
            f"[hmis-id] {len(cached)}/{len(mrs)} Medical No.(s) already cached; "
            f"{len(missing)} need a live HMIS lookup."
        )

        to_upsert = []
        failed = []
        for i, mr in enumerate(missing, 1):
            try:
                session = self._get_session()
                patient = session.get_patient_by_mr(mr)
            except Exception as e:
                logging.error(f"[hmis-id] HMIS lookup failed for Medical No. {mr}: {e}")
                failed.append(mr)
                continue

            if not patient or not patient.get("national_id"):
                logging.warning(f"[hmis-id] Medical No. {mr} resolved to no national_id — skipping.")
                failed.append(mr)
                continue

            result[mr] = patient["national_id"]
            to_upsert.append({
                "his_mr": mr,
                "national_id": patient["national_id"],
                "arabic_name": patient.get("arabic_name") or None,
                "english_name": patient.get("english_name") or None,
                "phone": patient.get("phone") or None,
                "birth_date": _ddmmyyyy_to_iso(patient.get("birth_date")),
                "raw_his": patient,
                "last_synced_at": _now_iso(),
            })

            if i % 25 == 0:
                logging.info(f"[hmis-id] looked up {i}/{len(missing)}...")

        if to_upsert:
            try:
                sb.upsert(REGISTRY_TABLE, to_upsert, on_conflict="his_mr")
            except Exception as e:
                logging.warning(
                    f"[hmis-id] could not write {len(to_upsert)} fresh mapping(s) back to "
                    f"{REGISTRY_TABLE} ({e}) — they resolved fine for this run but will be "
                    f"looked up live again next run."
                )

        if failed:
            logging.warning(
                f"[hmis-id] {len(failed)} Medical No.(s) could not be resolved this run: "
                f"{failed[:10]}" + (" ..." if len(failed) > 10 else "")
            )

        return result
