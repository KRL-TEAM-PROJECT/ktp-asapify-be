"""Scheduled ingestion: per region, fetch recent canonical inputs, assess every new hour, step the state machine
and keep one audit record per hour. Network access goes through injectable fetchers, so the whole flow runs in
tests without the internet.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Protocol

from . import openmeteo, power
from .canonical import Baseline, Observation, anomaly, build_baseline
from .fsm import RegionState, Transition, should_trigger_agent, step
from .regions import Region
from .rules import Assessment, RuleConfig, Status, assess

RECENT_DAYS = 4       # POWER publishes about 2-3 days behind real time
BASELINE_YEARS = 5    # the baseline uses the five years before the current one
WINDOW_DAYS = 15      # ± calendar days around the target date
AHEAD_DAYS = 10       # the cached baseline table reaches this far ahead, so it is rebuilt about once a week
SKIN = {"source": power.SOURCE, "parameter": "skin_temperature",
        "measurement_type": "earth_skin_temperature", "unit": "K"}

Fetch = Callable[[float, float, date, date], dict]


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _same_day(day: date, year: int) -> date:
    try:
        return day.replace(year=year)
    except ValueError:  # 29 February in a non-leap year
        return day.replace(year=year, day=28)


def baseline_table(history: list[Observation], first: date, last: date) -> dict[str, list]:
    """Baseline per calendar day and UTC hour from `first` to `last`: key MMDDHH -> [mean, std, samples]."""
    by_hour: dict[int, list[Observation]] = defaultdict(list)
    for o in history:
        by_hour[o.observed_at.hour].append(o)
    template = next(iter(history), None)
    entries: dict[str, list] = {}
    day = first
    while day <= last:
        for hour in range(24):
            at = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
            if template is None:
                entries[f"{at:%m%d%H}"] = [None, None, 0]
                continue
            b = build_baseline(replace(template, observed_at=at), by_hour[hour], window_days=WINDOW_DAYS)
            entries[f"{at:%m%d%H}"] = [b.mean, b.std, b.samples]
        day += timedelta(days=1)
    return entries


def refresh_baseline(region: Region, today: date, fetch_power: Fetch, now: datetime) -> dict:
    first, last = today - timedelta(days=RECENT_DAYS + 1), today + timedelta(days=AHEAD_DAYS)
    years = range(today.year - BASELINE_YEARS, today.year)
    history: list[Observation] = []
    for year in years:
        start = _same_day(first, year) - timedelta(days=WINDOW_DAYS)
        end = start + (last - first) + timedelta(days=2 * WINDOW_DAYS)
        history += [o for o in power.canonicalize(fetch_power(region.lat, region.lon, start, end), now)
                    if o.parameter == SKIN["parameter"]]
    return {"first": first.isoformat(), "last": last.isoformat(), "years": f"{years[0]}-{years[-1]}",
            "window_days": WINDOW_DAYS, "entries": baseline_table(history, first, last), "built_at": iso(now)}


@dataclass(frozen=True)
class HourResult:
    observed_at: datetime
    assessment: Assessment
    record: dict


def _value(obs: Observation | None) -> float | None:
    return obs.value if obs is not None and obs.usable else None


def _quality(obs: Observation | None) -> str:
    return obs.quality.value if obs is not None else "MISSING"


def assess_new_hours(recent: list[Observation], clouds: list[Observation], table: dict, years: str,
                     after: datetime | None, cfg: RuleConfig = RuleConfig()) -> list[HourResult]:
    ts = {o.observed_at: o for o in recent if o.parameter == "skin_temperature"}
    rh = {o.observed_at: o for o in recent if o.parameter == "relative_humidity"}
    cc = {o.observed_at: o for o in clouds}
    published = [t for t, o in ts.items() if o.usable]
    if not published:
        return []
    frontier = max(published)  # POWER fills hours it has not published yet; they wait for a later tick
    results = []
    for t in sorted(t for t in ts if t <= frontier and (after is None or t > after)):
        entry = table.get(f"{t:%m%d%H}", [None, None, 0])
        baseline = Baseline(**SKIN, mean=entry[0], std=entry[1], samples=entry[2],
                            description=f"UTC hour {t:%H}, ±{WINDOW_DAYS} days of {t:%m-%d}, years {years}")
        delta_t = anomaly(ts[t], baseline)
        humidity, cloud = rh.get(t), cc.get(t)
        result = assess(delta_t, _value(humidity), _value(cloud), cfg)
        record = {
            "observed_at": iso(t),
            "inputs": {"skin_temperature_k": _value(ts[t]), "baseline_k": baseline.mean,
                       "baseline_std_k": baseline.std, "baseline_samples": baseline.samples,
                       "baseline": baseline.description, "delta_t_k": delta_t,
                       "relative_humidity_pct": _value(humidity), "cloud_cover_pct": _value(cloud)},
            "quality": {"skin_temperature": _quality(ts[t]), "relative_humidity": _quality(humidity),
                        "cloud_cover": _quality(cloud)},
            "flags": sorted({f for o in (ts[t], humidity, cloud) if o is not None for f in o.quality_flags}),
            "sources": {"skin_temperature": power.SOURCE, "relative_humidity": power.SOURCE,
                        "cloud_cover": openmeteo.SOURCE},
            "status": result.status.value, "utilities": result.utilities, "memberships": result.memberships,
            "reason": result.reason,
        }
        results.append(HourResult(t, result, record))
    return results


class Store(Protocol):
    def states(self) -> dict[str, dict]: ...
    def baseline(self, region_id: str) -> dict | None: ...
    def save_baseline(self, region_id: str, doc: dict) -> None: ...
    def save_region(self, doc: dict, hours: list[dict], transitions: list[dict]) -> None: ...


def state_from_doc(doc: dict | None) -> RegionState | None:
    if not doc or not doc.get("status"):
        return None
    return RegionState(doc["id"], Status(doc["status"]), parse(doc["since"]), parse(doc["observed_at"]),
                       doc.get("utilities") or {}, parse(doc["last_agent_at"]) if doc.get("last_agent_at") else None)


def transition_doc(t: Transition) -> dict:
    return {"region_id": t.region_id, "observed_at": iso(t.observed_at),
            "previous": t.previous.value if t.previous else None, "current": t.current.value,
            "utilities": t.utilities, "reason": t.reason}


def _region_doc(region: Region, now: datetime) -> dict:
    return {"id": region.id, "province": region.province, "lat": region.lat, "lon": region.lon,
            "bounds": list(region.bounds), "peat_km2": region.peat_km2, "updated_at": iso(now)}


def process_region(store: Store, region: Region, previous: RegionState | None, now: datetime,
                   fetch_power: Fetch, fetch_cloud: Fetch, cfg: RuleConfig) -> dict:
    today = now.date()
    start = today - timedelta(days=RECENT_DAYS)
    table = store.baseline(region.id)
    if not table or not (table["first"] <= start.isoformat() and today.isoformat() <= table["last"]):
        table = refresh_baseline(region, today, fetch_power, now)
        store.save_baseline(region.id, table)
    recent = power.canonicalize(fetch_power(region.lat, region.lon, start, today), now)
    clouds = openmeteo.canonicalize(fetch_cloud(region.lat, region.lon, start, today), now)
    results = assess_new_hours(recent, clouds, table["entries"], table["years"],
                               previous.observed_at if previous else None, cfg)
    state, transitions = previous, []
    for r in results:
        state, change = step(state, region.id, r.observed_at, r.assessment)
        if change:
            transitions.append(change)
    agent_requested = state is not None and should_trigger_agent(previous, state, now)
    doc = _region_doc(region, now)
    doc["last_error"] = None
    if state is not None:
        doc.update({"status": state.status.value, "since": iso(state.since),
                    "observed_at": iso(state.observed_at), "utilities": state.utilities})
    if results:
        doc["latest"] = results[-1].record
    if agent_requested:
        doc["agent_requested_at"] = iso(now)
    store.save_region(doc, [{**r.record, "processed_at": iso(now)} for r in results],
                      [transition_doc(t) for t in transitions])
    return {"hours": len(results), "transitions": len(transitions), "agent_requested": agent_requested}


def run_tick(store: Store, regions: Iterable[Region], now: datetime, *, batch: int = 10,
             budget_s: float | None = None, clock: Callable[[], float] = time.monotonic,
             fetch_power: Fetch = power.fetch, fetch_cloud: Fetch = openmeteo.fetch,
             cfg: RuleConfig = RuleConfig()) -> dict:
    """Process the `batch` least recently updated regions, so every region is refreshed every few ticks.

    With `budget_s`, no new region starts once that many seconds have passed: the request finishes inside
    the caller's timeout and the remaining regions go first on the next tick.
    """
    started = clock()
    states = store.states()
    due = sorted(regions, key=lambda r: states.get(r.id, {}).get("updated_at", ""))[:batch]
    summary = {"regions": [], "hours": 0, "transitions": 0, "agent_requests": [], "errors": []}
    for region in due:
        if budget_s is not None and clock() - started >= budget_s:
            break
        try:
            result = process_region(store, region, state_from_doc(states.get(region.id)), now,
                                    fetch_power, fetch_cloud, cfg)
        except Exception as exc:  # one failing source must not stop the other regions
            store.save_region({**_region_doc(region, now), "last_error": type(exc).__name__}, [], [])
            summary["errors"].append({"region": region.id, "error": type(exc).__name__})
            continue
        summary["regions"].append(region.id)
        summary["hours"] += result["hours"]
        summary["transitions"] += result["transitions"]
        if result["agent_requested"]:
            summary["agent_requests"].append(region.id)
    return summary
