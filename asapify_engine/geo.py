"""Geostationary cycle, every 10 minutes: Himawari-9 and GK2A -> one fused thermal input per peat region -> rules
(VIIRS and NASA POWER as support, capped at WARNING by day or with one satellite) -> state machine -> agent if needed.

This replaces NASA POWER as the source of the status: POWER is 2-3 days late and 50 km coarse, so it only supports
now (ingest.py). Satellites, FIRMS and Open-Meteo go through injectable callables and storage through the Repo
protocol of ingest.py, so the whole flow runs in tests without the internet or Firestore.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from . import openmeteo, satellites
from .canonical import Quality
from .contextual import RegionThermal
from .fsm import mark_agent_called, should_trigger_agent, step
from .fusion import SATELLITES, FusionConfig, Fused, fuse, solar_elevation_deg
from .ingest import Repo, iso, parse, region_doc, state_from_doc, transition_doc
from .models import Status
from .regions import Region
from .rules import Assessment, RuleConfig, assess

FIRST = timedelta(minutes=10)   # nothing is on the bucket sooner: Himawari ~12 min, GK2A ~22-24 min after the scan
WAIT = timedelta(minutes=40)    # a slot only one satellite published is processed once it is this old
AGENT_RESERVE_S = 60            # an agent run only starts while this much of the time budget is left
THERMAL = "Himawari-9 AHI B07-B13 and GK2A AMI SW038-IR105 brightness temperatures (NOAA open data on AWS)"

Published = Callable[[str, datetime], bool]
Observe = Callable[[str, datetime, tuple[Region, ...]], dict[str, RegionThermal]]   # FileNotFoundError: not there
Hotspots = Callable[[tuple[float, float, float, float], datetime, datetime], list[dict]]   # bbox, start, end
Humidity = Callable[[list[tuple[float, float]], datetime], list[float | None]]
Verify = Callable[[Region, dict], dict]   # (region, agent request) -> stored verification record


def pick_slot(now: datetime, done: datetime | None, published: Published) -> tuple[datetime | None, list[str]]:
    """Newest slot both satellites published; else the oldest slot past WAIT that one of them did; else None."""
    newest = satellites.slot_floor(now - FIRST)
    candidates = [s for s in (newest - i * satellites.SLOT for i in range(WAIT // satellites.SLOT + 1))
                  if done is None or s > done]
    found: dict[datetime, list[str]] = {}
    for slot in candidates:
        found[slot] = [name for name in SATELLITES if published(name, slot)]
        if len(found[slot]) == len(SATELLITES):
            return slot, found[slot]
    for slot in reversed(candidates):
        if now - slot >= WAIT and found[slot]:
            return slot, found[slot]
    return None, []


def _no_view_reason(views: dict[str, RegionThermal | None], fused: Fused, cfg: RuleConfig) -> str:
    if all(v is None for v in views.values()):
        return "not observable: no satellite view of this region in this slot"
    if fused.cloud_pct > cfg.cloud_max:
        return f"not observable: cloud cover {fused.cloud_pct:.0f}% > {cfg.cloud_max:.0f}%"
    return "not observable: too few clear pixels in the region"


def _cap(fused: Fused, night: bool) -> str | None:
    """Why a DEGRADED view may give WARNING at most (design document section 3.5)."""
    if fused.quality is not Quality.DEGRADED:
        return None
    reasons = [] if night else ["daytime"]
    reasons += [f.replace("_", " ").replace(":", " ") for f in fused.flags
                if f.startswith(("single_satellite", "satellites_disagree"))]
    return ", ".join(reasons)


def assess_slot(region: Region, views: dict[str, RegionThermal | None], slot: datetime, *,
                humidity: float | None, hotspots: Callable[[], list[dict]], power: dict | None,
                cfg: RuleConfig = RuleConfig(), fusion: FusionConfig = FusionConfig()) -> tuple[Assessment, dict]:
    """One region, one slot -> the assessment and its audit record."""
    elevation = solar_elevation_deg(slot, region.lat, region.lon)
    night = elevation < fusion.night_sun_elevation_deg
    hot = any(v is not None and v.n_hot > 0 for v in views.values())   # FIRMS is only asked when a hotspot may be
    fused = fuse(views, night=night, slot=slot, centre=(region.lat, region.lon),
                 hotspots=hotspots() if hot else (), power=power, cfg=fusion)
    if fused.delta_t is None:
        result = Assessment(Status.NO_OBSERVABLE, {}, {}, _no_view_reason(views, fused, cfg))
    else:
        result = assess(fused.delta_t, humidity, fused.cloud_pct, cfg, support=fused.support, cap=_cap(fused, night))
    record = {
        "observed_at": iso(slot), "night": night, "sun_elevation_deg": round(elevation, 1),
        "views": {name: v.summary() if v is not None else None for name, v in views.items()},
        "inputs": {"delta_t_k": fused.delta_t, "cloud_cover_pct": fused.cloud_pct, "relative_humidity_pct": humidity,
                   "hot_lat": fused.hot_lat, "hot_lon": fused.hot_lon},
        "support": fused.support,
        "quality": {"thermal": fused.quality.value, "relative_humidity": "ACCEPTED" if humidity is not None else "MISSING"},
        "flags": list(fused.flags) + (["model_estimate"] if humidity is not None else []),
        "sources": {"thermal": THERMAL, "relative_humidity": openmeteo.SOURCE, "viirs_support": "nasa_firms",
                    "power_support": "nasa_power"},
        "status": result.status.value, "utilities": result.utilities, "memberships": result.memberships,
        "reason": result.reason,
    }
    return result, record


def process_slot(repo: Repo, regions: tuple[Region, ...], slot: datetime, views: dict, now: datetime, *,
                 humidity: dict[str, float | None], hotspots: Callable[[], list[dict]],
                 cfg: RuleConfig = RuleConfig(), fusion: FusionConfig = FusionConfig()) -> dict:
    """Every region through the rules and the state machine; slot records, states and transitions in one batch."""
    states = repo.get_states()
    out = {"statuses": {}, "transitions": 0, "agent_requests": {}, "errors": []}
    entries = []
    for region in regions:
        doc = states.get(region.id) or {}
        prev = state_from_doc(doc)
        if prev is not None and slot <= prev.observed_at:
            continue   # an older or repeated slot never moves the state backwards
        try:
            result, record = assess_slot(region, {name: None if v is None else v.get(region.id)
                                                  for name, v in views.items()},
                                         slot, humidity=humidity.get(region.id), hotspots=hotspots,
                                         power=doc.get("power"), cfg=cfg, fusion=fusion)
        except Exception as exc:   # one broken region must not stop the others
            entries.append(({**region_doc(region, now), "last_error": type(exc).__name__}, None, []))
            out["errors"].append({"region": region.id, "error": type(exc).__name__})
            continue
        state, change = step(prev, region.id, slot, result)
        new = {**region_doc(region, now), "last_error": None, "latest": record}
        if should_trigger_agent(prev, state, now, cfg):
            state = mark_agent_called(state, now)   # saved below, so the cooldown holds even if the agent fails
            out["agent_requests"][region.id] = {"observed_at": iso(slot), "requested_at": iso(now)}
            new["agent_requested_at"] = iso(now)
        new.update({"status": state.status.value, "since": iso(state.since), "observed_at": iso(state.observed_at),
                    "utilities": state.utilities})
        if state.last_agent_at is not None:
            new["last_agent_at"] = iso(state.last_agent_at)
        entries.append((new, record, [transition_doc(change)] if change else []))
        out["statuses"][state.status.value] = out["statuses"].get(state.status.value, 0) + 1
        out["transitions"] += 1 if change else 0
    repo.save_slots(entries)
    return out


def _humidity(meta: dict, regions: tuple[Region, ...], slot: datetime, fetch: Humidity) -> tuple[dict, dict | None]:
    """Humidity of the slot's hour per region; Open-Meteo is asked once per hour and the answer cached in meta."""
    hour = iso(slot.replace(minute=0))
    cached = meta.get("humidity") or {}
    if cached.get("hour") == hour:
        return cached.get("values") or {}, None
    values = fetch([(r.lat, r.lon) for r in regions], slot)
    fresh = {"hour": hour, "values": {r.id: v for r, v in zip(regions, values)}}
    return fresh["values"], fresh


def run_geo_cycle(repo: Repo, regions: Iterable[Region], now: datetime | None = None, *,
                  budget_s: float | None = None, clock: Callable[[], float] = time.monotonic,
                  published: Published = satellites.published, observe: Observe = satellites.observe,
                  hotspots: Hotspots | None = None, humidity: Humidity = openmeteo.fetch_humidity,
                  verify: Verify | None = None, cfg: RuleConfig = RuleConfig(),
                  fusion: FusionConfig = FusionConfig()) -> dict:
    """Waiting agent runs, then one slot for every region, then agent runs for regions that entered ALERT.

    With `budget_s`, no agent run starts once the budget is spent: the request ends inside the caller's timeout and
    the remaining runs wait in engine_meta for the next cycle, so a failed cycle never loses them.
    """
    now = now or datetime.now(timezone.utc)
    started = clock()
    regions = tuple(regions)
    by_id = {r.id: r for r in regions}
    summary = {"slot": None, "skipped": None, "satellites": {}, "statuses": {}, "transitions": 0,
               "agent_requests": [], "verified": [], "errors": []}
    if not regions:
        return summary
    meta = repo.get_meta()
    pending = dict(meta.get("agent_pending") or {})   # region id -> agent request
    waiting = dict(pending)
    fields: dict = {}

    def time_left() -> float:
        return float("inf") if budget_s is None else budget_s - (clock() - started)

    def run_agents() -> None:
        for region_id, request in list(pending.items()):
            if verify is None or time_left() < AGENT_RESERVE_S:
                return
            pending.pop(region_id)   # attempted once: the outcome (or the failure) is recorded, not retried
            try:
                record = verify(by_id[region_id], request)
                summary["verified"].append({"region": region_id, "validation_status": record.get("validation_status")})
            except Exception as exc:   # e.g. the daily quota is used up
                summary["errors"].append({"region": region_id, "error": type(exc).__name__})

    # 1) agent runs left over from earlier cycles go first: they are already late
    run_agents()
    # 2) the slot: the newest one both satellites published, or an old one only one of them did
    try:
        slot, names = pick_slot(now, parse(meta["geo_slot"]) if meta.get("geo_slot") else None, published)
        summary["skipped"] = None if slot else "no new slot published yet"
    except Exception as exc:   # the buckets are unreachable: try again on the next cycle
        slot, names = None, []
        summary["skipped"] = "satellite buckets unreachable"
        summary["errors"].append({"region": None, "error": f"buckets:{type(exc).__name__}"})
    if slot is not None:
        summary["slot"] = fields["geo_slot"] = iso(slot)   # attempted once, like agent runs
        views: dict[str, dict[str, RegionThermal] | None] = {}
        for name in SATELLITES:
            views[name], summary["satellites"][name] = None, "not_published"
            if name in names:
                try:
                    views[name], summary["satellites"][name] = observe(name, slot, regions), "ok"
                except Exception as exc:   # a broken file or decoder must not stop the other satellite
                    summary["satellites"][name] = f"error:{type(exc).__name__}"
        if all(v is None for v in views.values()):
            summary["skipped"] = "no satellite could be read"   # the regions keep their state; nothing is invented
        else:
            # supporting inputs: humidity of the hour (cached), FIRMS only when some region has a possible hotspot
            try:
                rh, fresh = _humidity(meta, regions, slot, humidity)
                if fresh:
                    fields["humidity"] = fresh
            except Exception as exc:   # optional: the rules then use the thermal input alone
                rh = {}
                summary["errors"].append({"region": None, "error": f"humidity:{type(exc).__name__}"})
            bbox = (min(r.bounds[0] for r in regions) - 0.1, min(r.bounds[1] for r in regions) - 0.1,
                    max(r.bounds[2] for r in regions) + 0.1, max(r.bounds[3] for r in regions) + 0.1)
            fetched: list[list[dict]] = []

            def nearby() -> list[dict]:
                if not fetched:
                    rows = []
                    if hotspots is not None:
                        try:
                            rows = hotspots(bbox, slot - timedelta(hours=fusion.viirs_hours[1]), now)
                        except Exception as exc:   # VIIRS only supports: the satellites still decide without it
                            summary["errors"].append({"region": None, "error": f"firms:{type(exc).__name__}"})
                    fetched.append(rows)
                return fetched[0]

            result = process_slot(repo, regions, slot, views, now, humidity=rh, hotspots=nearby, cfg=cfg, fusion=fusion)
            summary["statuses"], summary["transitions"] = result["statuses"], result["transitions"]
            summary["agent_requests"] = list(result["agent_requests"])
            summary["errors"] += result["errors"]
            pending.update(result["agent_requests"])
    # 3) agent runs for regions that entered ALERT in this slot, while time is left
    run_agents()
    # 4) remember the slot, the humidity of its hour and the agent runs still waiting
    if pending != waiting:
        fields["agent_pending"] = pending
    if fields:
        repo.save_meta(fields)
    return summary
