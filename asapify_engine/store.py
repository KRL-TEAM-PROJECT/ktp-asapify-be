"""Firestore storage for the engine: current state per region, an hourly audit record and every transition.

engine_regions/{id}                         current state + latest record (merge writes keep agent fields)
engine_regions/{id}/hours/{YYYYMMDDHH}      inputs, quality flags, utilities, status and reason for that hour
engine_regions/{id}/transitions/{YYYYMMDDHH}  status changes only
engine_baselines/{id}                       cached baseline table, rebuilt about once a week
"""
from __future__ import annotations

from google.cloud import firestore


def _hour_id(observed_at: str) -> str:
    return observed_at[:13].replace("-", "").replace("T", "")   # 2026-10-10T05:00:00Z -> 2026101005


class FirestoreEngineStore:
    def __init__(self, db: firestore.Client):
        self.db = db
        self.regions = db.collection("engine_regions")

    def states(self) -> dict[str, dict]:
        return {doc.id: doc.to_dict() for doc in self.regions.stream()}

    def baseline(self, region_id: str) -> dict | None:
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

    def region_detail(self, region_id: str, hours: int) -> dict | None:
        ref = self.regions.document(region_id)
        snap = ref.get()
        if not snap.exists:
            return None
        desc = firestore.Query.DESCENDING
        records = [d.to_dict() for d in ref.collection("hours").order_by("observed_at", direction=desc).limit(hours).stream()]
        changes = [d.to_dict() for d in ref.collection("transitions").order_by("observed_at", direction=desc).limit(20).stream()]
        return {**snap.to_dict(), "hours": records[::-1], "transitions": changes}
