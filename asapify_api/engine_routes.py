"""Risk-engine endpoints: region states, agent verification and decisions for operators; the cycles for the worker."""
from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Path, Query
from pydantic import Field

from asapify_engine import firms, satellites
from asapify_engine.config import Settings
from asapify_engine.geo import run_geo_cycle
from asapify_engine.ingest import run_cycle
from asapify_engine.regions import Region, load_regions
from asapify_engine.verifier import MAX_TOOL_CALLS, AgentNotConfigured, QuotaExhausted, verify_region

from .auth import require_operator
from .core import ApiProblem, DecisionInput, as_time, iso, utc_now
from .notification_store import get_notification_store
from .notification_tasks import enqueue_pending, get_tasks, require_worker

RegionID = Annotated[str, Path(pattern=r"^R\d{1,2}\.\d{2}[NS]\d{2,3}\.\d{3}E$")]
SUMMARY = ("status", "since", "observed_at", "utilities", "updated_at", "agent_requested_at", "last_agent_at",
           "verification")
MANUAL_GAP = timedelta(minutes=5)   # one manual verification per region per 5 minutes: double clicks, quota
router = APIRouter()


@lru_cache
def get_engine_repo():
    from google.cloud import firestore

    from asapify_engine.repo import FirestoreRepo
    settings = Settings()
    if not settings.project:
        raise ApiProblem(503, "DATA_NOT_READY", "Data storage is not configured.")
    return FirestoreRepo(firestore.Client(project=settings.project, database=settings.database))


def _region(region_id: str) -> Region:
    region = next((r for r in load_regions() if r.id == region_id), None)
    if region is None:
        raise ApiProblem(404, "NOT_FOUND", "Unknown region.")
    return region


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
                    **{k: latest.get(k) for k in ("inputs", "quality", "reason", "flags", "night")}})
    return out


@router.get("/api/v1/operator/regions/{region_id}", dependencies=[Depends(require_operator)])
def region(region_id: RegionID, hours: int = Query(24, ge=1, le=72), repo=Depends(get_engine_repo)):
    """State, the last `hours` of 10-minute slots (6 reads an hour, so at most 432) and POWER support."""
    _region(region_id)
    detail = repo.region_detail(region_id, hours)
    if detail is None:
        raise ApiProblem(503, "DATA_NOT_READY", "This region has not been processed yet.")
    return detail


@router.get("/api/v1/operator/agent", dependencies=[Depends(require_operator)])
def agent(repo=Depends(get_engine_repo)):
    """Agent configuration and today's quota, so the Re-verify button can say why it is disabled."""
    settings = Settings()
    used = repo.agent_calls(utc_now().date())
    return {"configured": settings.agent_ready, "model": settings.gemini_model,
            "daily_quota": settings.agent_daily_quota, "used_today": used,
            "remaining": max(0, settings.agent_daily_quota - used), "max_tool_calls": MAX_TOOL_CALLS}


@router.post("/api/v1/operator/regions/{region_id}/verify")
def verify(region_id: RegionID, actor=Depends(require_operator), repo=Depends(get_engine_repo)):
    """Re-verify button: one agent run now, for the latest assessed hour of the region."""
    region = _region(region_id)
    doc = repo.get_region(region_id)
    if not doc or not doc.get("latest"):
        raise ApiProblem(409, "DATA_NOT_READY", "This region has no assessed hour to verify yet.")
    now = utc_now()
    last = (doc.get("verification") or {}).get("at")
    if last and now - as_time(last) < MANUAL_GAP:
        raise ApiProblem(409, "VERIFIED_RECENTLY", "This region was verified less than 5 minutes ago.")
    try:
        return verify_region(repo, region, now, trigger="manual", actor=actor["email"])
    except AgentNotConfigured:
        raise ApiProblem(503, "INTEGRATION_NOT_READY", "The verification agent is not configured yet.")
    except QuotaExhausted:
        raise ApiProblem(429, "AGENT_QUOTA_EXHAUSTED", "Today's agent quota is used up. It resets at 00:00 UTC.")


class RegionDecision(DecisionInput):
    verification_id: str = Field(pattern=r"^\d{8}T\d{6}Z$")


@router.post("/api/v1/operator/regions/{region_id}/decision", status_code=201)
def decision(region_id: RegionID, body: RegionDecision, actor=Depends(require_operator),
             repo=Depends(get_engine_repo)):
    """Publish or reject one verification: once, audited, and publish needs a caption (same rules as clusters)."""
    region = _region(region_id)
    body.validate_action()
    publish, now = body.action == "publish", utc_now()
    alert_id = f"A-{region_id}-{body.verification_id}"
    result = {"action": body.action, "by": actor["email"], "at": iso(now),
              "reason": None if publish else body.reason.strip(), "alert_id": alert_id if publish else None,
              "notifications": {"state": "fanout_pending", "recipients": 0, "jobs": 0} if publish else None}
    alert = {"id": alert_id, "region_id": region_id, "verification_id": body.verification_id, "cluster_id": None,
             "source_path": f"engine_regions/{region_id}/verifications/{body.verification_id}",
             "province": region.province, "caption": body.caption, "published_by": actor["email"],
             "published_at": iso(now), "status": "active", "closed_at": None, "closed_by": None, "telegram": None,
             "notifications": result["notifications"], "purpose": "emergency",
             "expires_at": iso(now + timedelta(hours=1)), "fanout": "pending"} if publish else None

    def check(verification: dict) -> None:
        if verification.get("decision"):
            raise ApiProblem(409, "ALREADY_DECIDED", "An operator has already decided on this verification.")
        if publish and verification.get("validation_status") == "REJECTED":
            raise ApiProblem(409, "VERIFICATION_REJECTED", "The agent rejected this indication. Verify again or reject it.")

    audit = {"region_id": region_id, "verification_id": body.verification_id, "operator_uid": actor["uid"]}
    try:
        repo.decide(region_id, body.verification_id, check, dict(result), audit, alert)
    except LookupError:
        raise ApiProblem(404, "NOT_FOUND", "Verification not found.")
    if publish:
        # Fan-out and Cloud Tasks run after the decision commits; the Scheduler sweep resumes both.
        try:
            store = get_notification_store()
            result["notifications"] = store.fan_out(alert_id)
            result["queue"] = {"state": "accepted", "count": enqueue_pending(store, get_tasks())}
        except Exception:
            result["queue"] = {"state": "awaiting_recovery", "count": 0}
    return result


@router.post("/_internal/engine/tick", dependencies=[Depends(require_worker)])
def tick(batch: int = Query(10, ge=1, le=100), budget: int = Query(40, ge=5, le=280),
         repo=Depends(get_engine_repo)):
    """NASA POWER support. `budget` (seconds) must stay below the worker's request timeout (300 s in production)."""
    return run_cycle(repo, load_regions(), utc_now(), batch=batch, budget_s=budget)


@router.post("/_internal/engine/geo-tick", dependencies=[Depends(require_worker)])
def geo_tick(budget: int = Query(240, ge=30, le=280), repo=Depends(get_engine_repo)):
    """One Himawari-9 + GK2A slot for every region, every 10 minutes. `budget` (seconds) stays below the 300 s timeout."""
    settings = Settings()
    verify = hotspots = None
    if settings.agent_ready:
        def verify(region: Region, request: dict) -> dict:
            return verify_region(repo, region, utc_now(), trigger="auto", observed_at=request["observed_at"],
                                 settings=settings)
    if settings.firms_map_key:
        sources = [s.strip() for s in settings.firms_source.split(",") if s.strip()]

        def hotspots(bbox: tuple, start, end) -> list[dict]:
            return firms.area_hotspots(settings.firms_map_key, sources, bbox, start, end)
    with httpx.Client() as client:   # one connection pool for the HEAD requests and downloads on both buckets
        return run_geo_cycle(repo, load_regions(), utc_now(), budget_s=budget, verify=verify, hotspots=hotspots,
                             published=lambda name, slot: satellites.published(name, slot, client),
                             observe=lambda name, slot, regions: satellites.observe(name, slot, regions, client=client))
