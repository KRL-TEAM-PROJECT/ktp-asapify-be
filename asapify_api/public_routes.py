"""Read-only views for everyone: the region map, one region, and the day archive.

Same data as the operator endpoints, minus who did what: operator emails and decision notes are dropped, and a decision
keeps only its action and time. There are no actions here (no agent run, no decision, no messages).

Billing guardrail: every response is marked `public, s-maxage`, so Firebase Hosting's CDN answers repeat visits and
public traffic barely reaches Firestore. A region's detail is limited to 24 hours (about 96 slot reads); past days
never change and are cached for a day.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse

from . import engine_routes as engine
from .core import utc_now

router = APIRouter(prefix='/api/v1/public')
PRIVATE = {'requested_by', 'actor', 'by', 'published_by', 'operator_uid', 'email'}
LIVE_S, PAST_S = 120, 86400   # CDN seconds: a new slot lands every 15 minutes; a past day is final


def redact(value):
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for k, v in value.items():
        if k in PRIVATE:
            continue
        if k == 'decision' and isinstance(v, dict):
            # no caption, reason, alert id or delivery summary; `auto` (no operator, publication.py) names no one
            v = {**{x: v.get(x) for x in ('action', 'at')}, **({'auto': True} if v.get('auto') else {})}
        out[k] = redact(v)
    return out


def cached(payload, seconds: int) -> JSONResponse:
    return JSONResponse(redact(payload), headers={'Cache-Control': f'public, max-age=60, s-maxage={seconds}'})


FINAL_AFTER = timedelta(hours=1)   # the 23:40 and 23:50 WIB scans are rated by about 00:20-00:40 WIB


def past(day: str | None) -> bool:
    """A WIB day is final, and cached for a day, only once its last slots were rated: from 01:00 WIB the next day."""
    return bool(day) and day < (utc_now() + timedelta(hours=7) - FINAL_AFTER).date().isoformat()


@router.get('/regions')
def regions(repo=Depends(engine.get_engine_repo)):
    return cached(engine.regions(repo), LIVE_S)


@router.get('/regions/{region_id}')
def region(region_id: engine.RegionID, hours: int = Query(24, ge=1, le=24),
           day: str | None = Query(None, alias='date', pattern=engine.DAY), repo=Depends(engine.get_engine_repo)):
    return cached(engine.region(region_id, hours, day, repo), PAST_S if past(day) else LIVE_S)


@router.get('/days')
def days(month: str = Query(pattern=r'^\d{4}-(0[1-9]|1[0-2])$'), repo=Depends(engine.get_engine_repo)):
    return cached(engine.days(month, repo), LIVE_S)


@router.get('/days/{day}')
def archived_day(day: Annotated[str, Path(pattern=engine.DAY)], repo=Depends(engine.get_engine_repo)):
    return cached(engine.archived_day(day, repo), PAST_S if past(day) else LIVE_S)
