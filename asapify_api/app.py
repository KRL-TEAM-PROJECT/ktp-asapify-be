from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from .auth import require_operator
from .core import (ApiProblem, DecisionInput, PROVINCES, as_time, check_decidable,
                   date_key, geojson, iso, night_of, night_slots, utc_now, validate_grid)
from .notification_routes import router as notification_router
from .notification_store import get_notification_store
from .notification_tasks import enqueue_pending, get_tasks
from .repository import get_repository

app = FastAPI(title="ASAPify Operator API", version="0.1.0", docs_url=None, redoc_url=None,
              openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1000)
origins = [v.strip() for v in os.environ.get("CORS_ORIGINS", "").split(",") if v.strip()]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PUT", "DELETE"],
                       allow_headers=["Authorization", "Content-Type"], allow_credentials=False)


@app.middleware("http")
async def no_shared_cache(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(ApiProblem)
async def problem_handler(request, exc: ApiProblem):
    return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}},
                        headers={"Cache-Control": "no-store"})


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return JSONResponse(status_code=400, content={"error": {"code": "VALIDATION", "message": "Invalid parameter or request body."}})


@app.exception_handler(Exception)
async def unavailable(request, exc):
    # Credentials, external response bodies and token-bearing URLs never enter the response.
    return JSONResponse(status_code=503, content={"error": {"code": "SERVICE_UNAVAILABLE", "message": "The data service is not available yet. Try again later."}})


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "version": os.environ.get("ASAPIFY_VERSION", "0.1.0"), "time": iso(utc_now())}


@app.get("/api/v1/meta")
def meta(as_of: str | None = None, repo=Depends(get_repository)):
    at = as_time(as_of)
    snap = repo.snapshot(at)
    if snap.get("replay") and as_of is None:
        at = as_time(snap["timestamp"])
    def state(key):
        slot = snap.get(key)
        return {"last_slot": slot, "delay_min": max(0, round((at - as_time(slot)).total_seconds() / 60, 1)) if slot else None}
    return {"as_of": iso(at), "last_slot": snap["timestamp"], "is_night": 13 <= at.hour < 22,
            "replay": bool(snap.get("replay", False)), "n_sat": snap["n_sat"],
            "sats": {"himawari": state("himawari_slot"), "gk2a": state("gk2a_slot")},
            "nights": {"first": snap["archive_first"], "last": night_of(as_time(snap["timestamp"]))}}


operator = APIRouter(prefix="/api/v1/operator", dependencies=[Depends(require_operator)])


def snapshot_data(repo, at: datetime):
    snap = repo.snapshot(at)
    index = repo.json_object(snap["pixels_object"])
    grid = repo.json_object(snap["grid_object"])
    validate_grid(index, grid)
    if grid["slot"] != snap["timestamp"]:
        raise ApiProblem(503, "DATA_INVALID", "The observation manifest does not match the grid.")
    return index, grid


@operator.get("/pixels")
def pixels(request: Request, as_of: str | None = None, repo=Depends(get_repository)):
    index, _ = snapshot_data(repo, as_time(as_of))
    digest = hashlib.sha256(json.dumps(index, sort_keys=True).encode()).hexdigest()
    etag = f'"{digest}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Authorization"}
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(index, headers=headers)


@operator.get("/grid")
def grid(format: Literal["compact", "geojson"] = "geojson", as_of: str | None = None,
         province: str | None = None, repo=Depends(get_repository)):
    if province and (not province.isdigit() or int(province) not in PROVINCES):
        raise ApiProblem(400, "VALIDATION", "Invalid province code.")
    index, payload = snapshot_data(repo, as_time(as_of))
    return payload if format == "compact" else geojson(index, payload, province)


@operator.get("/grid/night")
def night_grid(night: str, as_of: str | None = None, repo=Depends(get_repository)):
    night = date_key(night)
    at = as_time(as_of)
    snap = repo.snapshot(at)
    if not snap["archive_first"] <= night <= night_of(as_time(snap["timestamp"])):
        raise ApiProblem(400, "VALIDATION", "The night is outside the available archive.")
    manifest = repo.night(night)
    if manifest is None or not manifest["has_data"]:
        return {"pixels_version": repo.json_object(snap["pixels_object"])["version"], "night": night,
                "slots": night_slots(night), "status": [None] * 54, "n_sat": [None] * 54, "clusters": {}}
    result = copy.deepcopy(repo.json_object(manifest["object"]))
    if result["slots"] != night_slots(night) or len(result["status"]) != 54 or len(result["n_sat"]) != 54:
        raise ApiProblem(503, "DATA_INVALID", "The night archive is inconsistent.")
    for i, slot in enumerate(result["slots"]):
        if as_time(slot) > at:
            result["status"][i], result["n_sat"][i] = None, None
    # Do not reveal a future cluster in historical playback.
    result["clusters"] = {cid: cells for cid, cells in result["clusters"].items()
                          if any(s and any(0 <= i < len(s) and s[i] == "A" for i in cells)
                                 for s in result["status"])}
    return result


@operator.get("/nights")
def nights(month: str, repo=Depends(get_repository)):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ApiProblem(400, "VALIDATION", "Month must be a valid YYYY-MM.")
    return [{k: n[k] for k in ("night", "has_data", "warning_clusters")} for n in repo.nights(month)]


@operator.get("/basemaps")
def basemaps(as_of: str | None = None):
    as_time(as_of)
    # The existing FE already provides street/plain maps; no fabricated satellite layer.
    return []


SUMMARY_KEYS = ("id", "state", "province", "rep_pixel", "centroid", "pixels", "trigger_slot",
                "utility_score", "neighbour_support", "n_sat", "verification", "decision")


@operator.get("/clusters")
def clusters(state: Literal["active", "closed"] = "active", night: str | None = None,
             as_of: str | None = None, repo=Depends(get_repository)):
    if night:
        date_key(night)
    result = []
    for c in repo.clusters(state, night, as_time(as_of) if as_of else None):
        item = {k: c.get(k) for k in SUMMARY_KEYS}
        if item["verification"]:
            item["verification"] = {k: item["verification"][k] for k in ("result", "viirs_within_2km_48h", "tool_calls", "at")}
        result.append(item)
    return result


@operator.get("/clusters/{cluster_id}")
def cluster(cluster_id: str, as_of: str | None = None, repo=Depends(get_repository)):
    return repo.cluster(cluster_id, as_time(as_of) if as_of else None)


@operator.post("/clusters/{cluster_id}/decision", status_code=201)
def decision(cluster_id: str, body: DecisionInput, actor=Depends(require_operator), repo=Depends(get_repository)):
    current = repo.cluster(cluster_id)
    check_decidable(current, body)
    result, _ = repo.decide(cluster_id, body, actor)
    if body.action == "publish":
        # Fan-out and Cloud Tasks run after the decision commits. The Scheduler sweep resumes both.
        try:
            store = get_notification_store()
            result["notifications"] = store.fan_out(result["alert_id"])
            count = enqueue_pending(store, get_tasks())
            result["queue"] = {"state": "accepted", "count": count}
        except Exception:
            result["queue"] = {"state": "awaiting_recovery", "count": 0}
    return result


@operator.post("/clusters/{cluster_id}/verify")
def verify(cluster_id: str):
    raise ApiProblem(503, "INTEGRATION_NOT_READY", "The verification agent is not connected yet.")


@operator.get("/alerts")
def alerts():
    raise ApiProblem(503, "INTEGRATION_NOT_READY", "Alert management is not available at this stage.")


@operator.post("/alerts/{alert_id}/close")
def close_alert(alert_id: str):
    raise ApiProblem(503, "INTEGRATION_NOT_READY", "Closing alerts is not connected yet.")


app.include_router(operator)

app.include_router(notification_router)
