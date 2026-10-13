"""Risk-engine endpoints: region states for operators, and the scheduled cycle for the private worker."""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from asapify_engine.config import Settings
from asapify_engine.ingest import run_cycle
from asapify_engine.regions import load_regions

from .auth import require_operator
from .core import ApiProblem, utc_now
from .notification_tasks import require_worker

RegionID = Annotated[str, Path(pattern=r"^R\d{1,2}\.\d{2}[NS]\d{2,3}\.\d{3}E$")]
SUMMARY = ("status", "since", "observed_at", "utilities", "updated_at", "agent_requested_at", "last_agent_at")
router = APIRouter()


@lru_cache
def get_engine_repo():
    from google.cloud import firestore

    from asapify_engine.repo import FirestoreRepo
    settings = Settings()
    if not settings.project:
        raise ApiProblem(503, "DATA_NOT_READY", "Data storage is not configured.")
    return FirestoreRepo(firestore.Client(project=settings.project, database=settings.database))


@router.get("/api/v1/operator/regions", dependencies=[Depends(require_operator)])
def regions(repo=Depends(get_engine_repo)):
    """Every observation region with its current status; status is null until the first cycle reaches it."""
    states = repo.get_states()
    out = []
    for r in load_regions():
        doc = states.get(r.id, {})
        latest = doc.get("latest") or {}
        out.append({"id": r.id, "province": r.province, "lat": r.lat, "lon": r.lon, "bounds": list(r.bounds),
                    "peat_km2": r.peat_km2, **{k: doc.get(k) for k in SUMMARY},
                    "inputs": latest.get("inputs"), "quality": latest.get("quality"), "reason": latest.get("reason")})
    return out


@router.get("/api/v1/operator/regions/{region_id}", dependencies=[Depends(require_operator)])
def region(region_id: RegionID, hours: int = Query(72, ge=1, le=240), repo=Depends(get_engine_repo)):
    if region_id not in {r.id for r in load_regions()}:
        raise ApiProblem(404, "NOT_FOUND", "Unknown region.")
    detail = repo.region_detail(region_id, hours)
    if detail is None:
        raise ApiProblem(503, "DATA_NOT_READY", "This region has not been processed yet.")
    return detail


@router.post("/_internal/engine/tick", dependencies=[Depends(require_worker)])
def tick(batch: int = Query(10, ge=1, le=100), budget: int = Query(40, ge=5, le=280),
         repo=Depends(get_engine_repo)):
    """`budget` (seconds) must stay below the worker's request timeout (300 s in production)."""
    return run_cycle(repo, load_regions(), utc_now(), batch=batch, budget_s=budget)
