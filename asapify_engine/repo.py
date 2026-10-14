"""Firestore storage for the engine: the Repo of ingest.py, geo.py and verifier.py.

engine_regions/{id}                             current state, latest slot record and latest POWER support
engine_regions/{id}/slots/{YYYYMMDDHHMM}        satellite views, inputs, utilities, status and reason of that slot;
                                                `expire_at` lets a Firestore TTL policy drop it after SLOT_RETENTION
engine_regions/{id}/hours/{YYYYMMDDHH}          NASA POWER supporting data of that hour
engine_regions/{id}/transitions/{id}            status changes only
engine_regions/{id}/verifications/{id}          agent results and the operator decision on each
engine_days/{YYYY-MM-DD}                        one WIB day for the map's date picker: per slot, every region's status
                                                and delta T plus hot pixels and nearby VIIRS (geo.archive_slot, < 1 MiB)
engine_baselines/{id}                           cached POWER baseline table, rebuilt about once a week
engine_meta/ingestion                           POWER hour every region caught up with, last satellite slot, humidity
                                                of its hour and waiting agent runs
engine_meta/agent_quota_{YYYYMMDD}              agent runs started on that UTC day (hard daily quota)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

SUMMARY =("id", "validation_status", "confidence_score", "trigger", "at", "observed_at", "error")
SLOT_RETENTION = timedelta(days=30)
SLOTS_PER_HOUR = 6


def _hour_id(observed_at: str) -> str:
    return observed_at[:13].replace("-", "").replace("T", "")   # 2026-10-10T05:00:00Z -> 2026101005


def _slot_id(observed_at: str) -> str:
    return observed_at[:16].replace("-", "").replace("T", "").replace(":", "")   # 2026-10-11T18:10:00Z -> 202610111810


def verification_summary(record: dict) -> dict:
    """What the region document keeps about its latest verification."""
    return {**{k: record.get(k) for k in SUMMARY}, "decision": (record.get("decision") or {}).get("action")}


class FirestoreRepo:
    def __init__(self, db: firestore.Client):
        self.db = db
        self.regions = db.collection("engine_regions")
        self.meta = db.collection("engine_meta")
        self.archive = db.collection("engine_days")

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

    def save_region(self, doc: dict, hours: list[dict]) -> None:
        ref = self.regions.document(doc["id"])
        batch = self.db.batch()   # at most ~5 days of hours: well under 500 writes
        for record in hours:
            batch.set(ref.collection("hours").document(_hour_id(record["observed_at"])), record)
        batch.set(ref, doc, merge=True)
        batch.commit()

    def save_slots(self, entries: list[tuple[dict, dict | None, list[dict]]], day: dict | None = None) -> None:
        """One slot for every region in one batch: 65 regions x (state + slot record + a transition) + the day < 500
        writes."""
        batch = self.db.batch()
        if day is not None:
            # merge=True: the slot joins the day's `slots` map; `alert` / `warning` are only ever written as True
            batch.set(self.archive.document(day["date"]), day, merge=True)
        for doc, record, transitions in entries:
            ref = self.regions.document(doc["id"])
            if record is not None:
                expire = datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00")) + SLOT_RETENTION
                batch.set(ref.collection("slots").document(_slot_id(record["observed_at"])),
                          {**record, "expire_at": expire})
            for change in transitions:
                batch.set(ref.collection("transitions").document(_slot_id(change["observed_at"])), change)
            # merge by top-level field: `latest` is replaced whole, so no key of an older slot record survives in it
            batch.set(ref, doc, merge=list(doc))
        if entries or day is not None:
            batch.commit()

    # --- reads for operators and the agent ----------------------------------------------------------------------
    def get_region(self, region_id: str) -> dict | None:
        snap = self.regions.document(region_id).get()
        return snap.to_dict() if snap.exists else None

    def get_slot(self, region_id: str, observed_at: str) -> dict | None:
        snap = self.regions.document(region_id).collection("slots").document(_slot_id(observed_at)).get()
        return snap.to_dict() if snap.exists else None

    def region_detail(self, region_id: str, hours: int, window: tuple[str, str] | None = None) -> dict | None:
        """State plus slot records (6 an hour) and POWER support, oldest first: the last `hours`, or the slots with
        start <= observed_at < end when `window` is given (one day: at most 144 slot reads)."""
        ref = self.regions.document(region_id)
        snap = ref.get()
        if not snap.exists:
            return None
        desc = firestore.Query.DESCENDING

        def latest(name: str, limit: int, field: str = "observed_at") -> list[dict]:
            return [d.to_dict() for d in ref.collection(name).order_by(field, direction=desc).limit(limit).stream()]

        def within(name: str, limit: int, direction=firestore.Query.ASCENDING) -> list[dict]:
            q = (ref.collection(name).where(filter=FieldFilter("observed_at", ">=", window[0]))
                 .where(filter=FieldFilter("observed_at", "<", window[1]))
                 .order_by("observed_at", direction=direction).limit(limit))
            return [d.to_dict() for d in q.stream()]
        if window is None:
            slots, power = latest("slots", hours * SLOTS_PER_HOUR)[::-1], latest("hours", hours)[::-1]
            transitions = latest("transitions", 20)
        else:
            slots, power = within("slots", 24 * SLOTS_PER_HOUR), within("hours", 24)
            transitions = within("transitions", 20, desc)
        return {**snap.to_dict(), "slots": [{k: v for k, v in s.items() if k != "expire_at"} for s in slots],
                "hours": power, "transitions": transitions, "verifications": latest("verifications", 10, "at")}

    # --- day archive --------------------------------------------------------------------------------------------
    def get_day(self, day: str) -> dict | None:
        snap = self.archive.document(day).get()
        return snap.to_dict() if snap.exists else None

    def list_days(self, month: str) -> list[dict]:
        """Calendar flags of one month's archived days (only those fields are read back)."""
        q = self.archive.where(filter=FieldFilter("month", "==", month)).select(["date", "alert", "warning"])
        return [{"date": d.get("date"), "alert": bool(d.get("alert")), "warning": bool(d.get("warning"))}
                for d in (s.to_dict() for s in q.stream())]

    def day_range(self) -> tuple[str | None, str | None]:
        """First and last archived day: two one-document reads."""
        def edge(direction) -> str | None:
            docs = list(self.archive.order_by("date", direction=direction).select(["date"]).limit(1).stream())
            return docs[0].to_dict().get("date") if docs else None
        return edge(firestore.Query.ASCENDING), edge(firestore.Query.DESCENDING)

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
