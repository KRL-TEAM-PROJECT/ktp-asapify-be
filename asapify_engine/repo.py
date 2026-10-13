"""Firestore storage for the engine: the Repo of ingest.py and verifier.py (pipeline/pipeline.py assumed Postgres).

engine_regions/{id}                             current state + latest record (merge writes keep agent fields)
engine_regions/{id}/hours/{YYYYMMDDHH}          inputs, quality flags, utilities, status and reason for that hour
engine_regions/{id}/transitions/{YYYYMMDDHH}    status changes only
engine_regions/{id}/verifications/{id}          agent results and the operator decision on each
engine_baselines/{id}                           cached baseline table, rebuilt about once a week
engine_meta/ingestion                           latest POWER hour every region caught up with + waiting agent runs
engine_meta/agent_quota_{YYYYMMDD}              agent runs started on that UTC day (hard daily quota)
"""
from __future__ import annotations

from datetime import date
from typing import Callable

from google.cloud import firestore

SUMMARY = ("id", "validation_status", "confidence_score", "trigger", "at", "observed_at", "error")


def _hour_id(observed_at: str) -> str:
    return observed_at[:13].replace("-", "").replace("T", "")   # 2026-10-10T05:00:00Z -> 2026101005


def verification_summary(record: dict) -> dict:
    """What the region document keeps about its latest verification."""
    return {**{k: record.get(k) for k in SUMMARY}, "decision": (record.get("decision") or {}).get("action")}


class FirestoreRepo:
    def __init__(self, db: firestore.Client):
        self.db = db
        self.regions = db.collection("engine_regions")
        self.meta = db.collection("engine_meta")

    # --- ingestion ----------------------------------------------------------------------------------------------
    def get_meta(self) -> dict:
        doc = self.meta.document("ingestion").get()
        return doc.to_dict() if doc.exists else {}

    def save_meta(self, fields: dict) -> None:
        # merge by top-level field: a smaller agent_pending map replaces the old one instead of merging into it
        self.meta.document("ingestion").set(fields, merge=list(fields))

    def get_states(self) -> dict[str, dict]:
        return {doc.id: doc.to_dict() for doc in self.regions.stream()}

    def get_baseline(self, region_id: str) -> dict | None:
        doc = self.db.collection("engine_baselines").document(region_id).get()
        return doc.to_dict() if doc.exists else None

    def save_baseline(self, region_id: str, doc: dict) -> None:
        self.db.collection("engine_baselines").document(region_id).set(doc)

    def save_region(self, doc: dict, hours: list[dict], transitions: list[dict]) -> None:
        ref = self.regions.document(doc["id"])
        batch = self.db.batch()   # at most ~5 days of hours + their transitions: well under 500 writes
        for record in hours:
            batch.set(ref.collection("hours").document(_hour_id(record["observed_at"])), record)
        for record in transitions:
            batch.set(ref.collection("transitions").document(_hour_id(record["observed_at"])), record)
        batch.set(ref, doc, merge=True)
        batch.commit()

    # --- reads for operators and the agent ----------------------------------------------------------------------
    def get_region(self, region_id: str) -> dict | None:
        snap = self.regions.document(region_id).get()
        return snap.to_dict() if snap.exists else None

    def get_hour(self, region_id: str, observed_at: str) -> dict | None:
        snap = self.regions.document(region_id).collection("hours").document(_hour_id(observed_at)).get()
        return snap.to_dict() if snap.exists else None

    def region_detail(self, region_id: str, hours: int) -> dict | None:
        ref = self.regions.document(region_id)
        snap = ref.get()
        if not snap.exists:
            return None
        desc = firestore.Query.DESCENDING
        records = [d.to_dict() for d in ref.collection("hours").order_by("observed_at", direction=desc).limit(hours).stream()]
        changes = [d.to_dict() for d in ref.collection("transitions").order_by("observed_at", direction=desc).limit(20).stream()]
        checks = [d.to_dict() for d in ref.collection("verifications").order_by("at", direction=desc).limit(10).stream()]
        return {**snap.to_dict(), "hours": records[::-1], "transitions": changes, "verifications": checks}

    # --- agent --------------------------------------------------------------------------------------------------
    def agent_calls(self, day: date) -> int:
        snap = self.meta.document(f"agent_quota_{day:%Y%m%d}").get()
        return (snap.to_dict() or {}).get("count", 0) if snap.exists else 0

    def reserve_agent_call(self, day: date, limit: int) -> int | None:
        """Hard daily quota, taken in a transaction BEFORE any model call. None = quota used up."""
        ref = self.meta.document(f"agent_quota_{day:%Y%m%d}")

        @firestore.transactional
        def reserve(tx):
            snap = ref.get(transaction=tx)
            used = (snap.to_dict() or {}).get("count", 0) if snap.exists else 0
            if used >= limit:
                return None
            tx.set(ref, {"day": day.isoformat(), "count": used + 1, "limit": limit})
            return used + 1
        return reserve(self.db.transaction())

    def save_verification(self, region_id: str, record: dict) -> None:
        ref = self.regions.document(region_id)
        batch = self.db.batch()
        batch.set(ref.collection("verifications").document(record["id"]), record)
        batch.set(ref, {"verification": verification_summary(record), "last_agent_at": record["at"]}, merge=True)
        batch.commit()

    def decide(self, region_id: str, verification_id: str, check: Callable[[dict], None], decision: dict,
               audit: dict, alert: dict | None) -> dict:
        """Decision, audit entry and alert are atomic; a second decision on the same verification fails `check`."""
        region_ref = self.regions.document(region_id)
        ref = region_ref.collection("verifications").document(verification_id)

        @firestore.transactional
        def save(tx):
            snap = ref.get(transaction=tx)            # every read comes before the first write
            region = region_ref.get(transaction=tx).to_dict() or {}
            if not snap.exists:
                raise LookupError(verification_id)
            verification = snap.to_dict()
            check(verification)
            tx.update(ref, {"decision": decision})
            if (region.get("verification") or {}).get("id") == verification_id:
                tx.set(region_ref, {"verification": {"decision": decision["action"]}}, merge=True)
            tx.create(self.db.collection("audit_log").document(), {**audit, **decision})
            if alert:
                tx.create(self.db.collection("alerts").document(alert["id"]), alert)
            return verification
        return save(self.db.transaction())
