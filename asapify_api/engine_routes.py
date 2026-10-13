"""Risk-engine endpoints: region states for operators, and the scheduled tick for the private worker."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from asapify_engine.ingest import run_tick
from asapify_engine.regions import load_regions

from .auth import require_operator
from .core import ApiProblem, utc_now
from .notification_tasks import require_worker

RegionID = Annotated[str, Path(pattern=r"^R\d{1,2}\.\d{2}[NS]\d{2,3}\.\d{3}E$")]
SUMMARY = ("status", "since", "observed_at", "utilities", "updated_at", "agent_requested_at", "last_agent_at")
router = APIRouter()


@lru_cache
def get_engine_store():
    from google.cloud import firestore

    from asapify_engine.store import FirestoreEngineStore
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise ApiProblem(503, "DATA_NOT_READY", "Data storage is not configured.")
    db = firestore.Client(project=project, database=os.environ.get("FIRESTORE_DATABASE", "asapify"))
    return FirestoreEngineStore(db)


@router.get("/api/v1/operator/regions", dependencies=[Depends(require_operator)])
def regions(store=Depends(get_engine_store)):
    """Every observation region with its current status; status is null until the first tick reaches it."""
    states = store.states()
    out = []
    for r in load_regions():
        doc = states.get(r.id, {})
        latest = doc.get("latest") or {}
        out.append({"id": r.id, "province": r.province, "lat": r.lat, "lon": r.lon, "bounds": list(r.bounds),
                    "peat_km2": r.peat_km2, **{k: doc.get(k) for k in SUMMARY},
                    "inputs": latest.get("inputs"), "quality": latest.get("quality"), "reason": latest.get("reason")})
    return out


@router.get("/api/v1/operator/regions/{region_id}", dependencies=[Depends(require_operator)])
def region(region_id: RegionID, hours: int = Query(72, ge=1, le=240), store=Depends(get_engine_store)):
    if region_id not in {r.id for r in load_regions()}:
        raise ApiProblem(404, "NOT_FOUND", "Unknown region.")
    detail = store.region_detail(region_id, hours)
    if detail is None:
        raise ApiProblem(503, "DATA_NOT_READY", "This region has not been processed yet.")
    return detail


@router.post("/_internal/engine/tick", dependencies=[Depends(require_worker)])
def tick(batch: int = Query(10, ge=1, le=100), budget: int = Query(40, ge=5, le=280),
         store=Depends(get_engine_store)):
    """`budget` (seconds) must stay below the worker's request timeout; the default fits a 60 s timeout."""
    return run_tick(store, load_regions(), utc_now(), batch=batch, budget_s=budget)
