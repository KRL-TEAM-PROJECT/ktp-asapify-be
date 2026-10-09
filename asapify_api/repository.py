from __future__ import annotations

import json
import os
import re
from datetime import datetime
from functools import lru_cache

from google.cloud import firestore, storage
from google.cloud.firestore_v1.base_query import FieldFilter

from .core import ApiProblem, DecisionInput, check_decidable, iso, utc_now
from .notification_store import NotificationStore


class CloudRepository:
    def __init__(self):
        project, self.bucket = os.environ.get("GOOGLE_CLOUD_PROJECT"), os.environ.get("ASAPIFY_DATA_BUCKET")
        if not project or not self.bucket:
            raise ApiProblem(503, "DATA_NOT_READY", "Penyimpanan data belum dikonfigurasi.")
        self.db = firestore.Client(project=project, database=os.environ.get("FIRESTORE_DATABASE", "asapify"))
        self.gcs = storage.Client(project=project)

    def snapshot(self, at: datetime) -> dict:
        q = self.db.collection("api_snapshots").where(filter=FieldFilter("timestamp", "<=", iso(at)))
        docs = list(q.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(1).stream())
        if not docs:
            raise ApiProblem(503, "DATA_NOT_READY", "Belum ada hasil observasi untuk waktu ini.")
        return docs[0].to_dict()

    def json_object(self, name: str) -> dict:
        if not re.fullmatch(r"api/[A-Za-z0-9_./-]+\.json", name) or ".." in name:
            raise ApiProblem(503, "DATA_INVALID", "Referensi data tidak sah.")
        blob = self.gcs.bucket(self.bucket).get_blob(name)
        if blob is None:
            raise ApiProblem(503, "DATA_NOT_READY", "Berkas observasi belum tersedia.")
        if blob.size > 25_000_000:
            raise ApiProblem(503, "DATA_INVALID", "Berkas observasi melebihi batas API.")
        return json.loads(blob.download_as_bytes())

    def night(self, night: str) -> dict | None:
        doc = self.db.collection("api_nights").document(night).get()
        return doc.to_dict() if doc.exists else None

    def nights(self, month: str) -> list[dict]:
        q = self.db.collection("api_nights").where(filter=FieldFilter("month", "==", month))
        return sorted([d.to_dict() for d in q.stream()], key=lambda v: v["night"])

    def cluster(self, cluster_id: str, at: datetime | None = None) -> dict:
        ref = self.db.collection("clusters").document(cluster_id)
        if at is None:
            doc = ref.get()
            value = doc.to_dict() if doc.exists else None
        else:
            q = ref.collection("history").where(filter=FieldFilter("timestamp", "<=", iso(at)))
            docs = list(q.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(1).stream())
            value = docs[0].to_dict()["cluster"] if docs else None
        if value is None:
            raise ApiProblem(404, "NOT_FOUND", "Kelompok tidak tersedia pada waktu ini.")
        return value

    def clusters(self, state: str, night: str | None, at: datetime | None) -> list[dict]:
        q = self.db.collection("clusters")
        if night:
            q = q.where(filter=FieldFilter("awas_nights", "array_contains", night))
        elif at is None:
            q = q.where(filter=FieldFilter("state", "==", state))
        items = []
        for doc in q.stream():
            try:
                c = self.cluster(doc.id, at) if at else doc.to_dict()
            except ApiProblem as exc:
                if exc.code == "NOT_FOUND":
                    continue
                raise
            if night or c["state"] == state:
                items.append(c)
        return items

    def decide(self, cluster_id: str, request: DecisionInput, actor: dict) -> tuple[dict, dict]:
        ref = self.db.collection("clusters").document(cluster_id)
        alert_id = "A-" + cluster_id.removeprefix("C-")
        now = iso(utc_now())
        decision = {"action": request.action, "by": actor["email"], "at": now,
                    "reason": request.reason.strip() if request.action == "reject" else None,
                    "alert_id": alert_id if request.action == "publish" else None,
                    "telegram": None,
                    "notifications": {"state": "outbox_saved", "recipients": 0, "jobs": 0} if request.action == "publish" else None}

        @firestore.transactional
        def save(transaction):
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                raise ApiProblem(404, "NOT_FOUND", "Kelompok tidak ditemukan.")
            cluster = snap.to_dict()
            check_decidable(cluster, request)
            notifier = NotificationStore(self.db)
            inboxes, jobs = notifier.prepare_publication(transaction, cluster, request.caption_id, now) if request.action == "publish" else ([], [])
            if request.action == "publish":
                decision["notifications"] = {"state": "outbox_saved" if inboxes else "no_recipients",
                                             "recipients": len(inboxes), "jobs": len(jobs)}
            # Decision, outbox and audit are atomic. Concurrent clicks produce exactly one reservation.
            transaction.update(ref, {"decision": decision})
            transaction.create(self.db.collection("audit_log").document(), {
                "cluster_id": cluster_id, "operator_uid": actor["uid"], **decision})
            if request.action == "publish":
                transaction.create(self.db.collection("alerts").document(alert_id), {
                    "id": alert_id, "cluster_id": cluster_id, "province": cluster["province"],
                    "caption_id": request.caption_id, "published_by": actor["email"],
                    "published_at": now, "status": "active", "closed_at": None, "closed_by": None,
                    "telegram": None, "notifications": decision["notifications"],
                })
            notifier.write_publication(transaction, inboxes, jobs)
            return cluster

        cluster = save(self.db.transaction())
        return decision, cluster

    def finish_delivery(self, cluster_id: str, alert_id: str, outcome: dict) -> None:
        batch = self.db.batch()
        batch.update(self.db.collection("clusters").document(cluster_id), {"decision.telegram": outcome})
        batch.update(self.db.collection("alerts").document(alert_id), {"telegram": outcome})
        batch.commit()

    def telegram_chat(self, province: str) -> str:
        doc = self.db.collection("tg_chats").document(province).get()
        value = doc.to_dict() if doc.exists else {}
        if not value.get("alert_chat_id"):
            raise ApiProblem(503, "DELIVERY_NOT_CONFIGURED", "Grup peringatan provinsi belum dikonfigurasi.")
        return str(value["alert_chat_id"])


@lru_cache
def get_repository() -> CloudRepository:
    return CloudRepository()
