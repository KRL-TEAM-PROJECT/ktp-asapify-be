from __future__ import annotations

import json
import os
import re
from datetime import datetime
from functools import lru_cache

from google.cloud import firestore, storage
from google.cloud.firestore_v1.base_query import FieldFilter

from .core import ApiProblem, DecisionInput, check_decidable, iso, utc_now
from .notification_policy import alert_payload


class CloudRepository:
    def __init__(self):
        project, self.bucket = os.environ.get("GOOGLE_CLOUD_PROJECT"), os.environ.get("ASAPIFY_DATA_BUCKET")
        if not project or not self.bucket:
            raise ApiProblem(503, "DATA_NOT_READY", "Data storage is not configured.")
        self.db = firestore.Client(project=project, database=os.environ.get("FIRESTORE_DATABASE", "asapify"))
        self.gcs = storage.Client(project=project)

    def snapshot(self, at: datetime) -> dict:
        q = self.db.collection("api_snapshots").where(filter=FieldFilter("timestamp", "<=", iso(at)))
        docs = list(q.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(1).stream())
        if not docs:
            raise ApiProblem(503, "DATA_NOT_READY", "No observation results exist for this time yet.")
        return docs[0].to_dict()

    def json_object(self, name: str) -> dict:
        if not re.fullmatch(r"api/[A-Za-z0-9_./-]+\.json", name) or ".." in name:
            raise ApiProblem(503, "DATA_INVALID", "Invalid data reference.")
        blob = self.gcs.bucket(self.bucket).get_blob(name)
        if blob is None:
            raise ApiProblem(503, "DATA_NOT_READY", "The observation file is not available yet.")
        if blob.size > 25_000_000:
            raise ApiProblem(503, "DATA_INVALID", "The observation file exceeds the API limit.")
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
            raise ApiProblem(404, "NOT_FOUND", "The cluster is not available at this time.")
        return value

    def clusters(self, state: str, night: str | None, at: datetime | None) -> list[dict]:
        q = self.db.collection("clusters")
        if night:
            q = q.where(filter=FieldFilter("warning_nights", "array_contains", night))
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
                    "notifications": {"state": "fanout_pending", "recipients": 0, "jobs": 0} if request.action == "publish" else None}

        @firestore.transactional
        def save(transaction):
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                raise ApiProblem(404, "NOT_FOUND", "Cluster not found.")
            cluster = snap.to_dict()
            check_decidable(cluster, request)
            # Decision, audit and alert are atomic. Concurrent clicks produce exactly one reservation.
            # Recipients are written afterwards (NotificationStore.fan_out) so their number never blocks this.
            transaction.update(ref, {"decision": decision})
            transaction.create(self.db.collection("audit_log").document(), {
                "cluster_id": cluster_id, "operator_uid": actor["uid"], **decision})
            if request.action == "publish":
                alert = alert_payload(cluster, request.caption, now)
                transaction.create(self.db.collection("alerts").document(alert_id), {
                    "id": alert_id, "cluster_id": cluster_id, "province": cluster["province"],
                    "caption": request.caption, "published_by": actor["email"],
                    "published_at": now, "status": "active", "closed_at": None, "closed_by": None,
                    "telegram": None, "notifications": decision["notifications"],
                    "purpose": alert["purpose"], "expires_at": alert["expires_at"], "fanout": "pending",
                })
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
            raise ApiProblem(503, "DELIVERY_NOT_CONFIGURED", "The provincial alert group is not configured.")
        return str(value["alert_chat_id"])


@lru_cache
def get_repository() -> CloudRepository:
    return CloudRepository()
