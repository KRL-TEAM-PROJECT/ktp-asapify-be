"""One cron cycle: per region fetch recent canonical inputs -> assess every new hour -> step the state machine ->
keep one audit record per hour -> (if needed) the verification agent.

Same shape as pipeline/pipeline.py run_cycle. Network access goes through injectable fetchers and storage through
the Repo protocol, so the whole flow runs in tests without the internet or Firestore.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Protocol

from . import openmeteo, power
from .canonical import Baseline, Observation, anomaly, build_baseline
from .fsm import mark_agent_called, should_trigger_agent, step
from .models import RegionState, Status, Transition
from .regions import Region
from .rules import Assessment, RuleConfig, assess

RECENT_DAYS = 4        # POWER publishes about 2-3 days behind real time
BASELINE_YEARS = 5     # the baseline uses the five years before the current one
WINDOW_DAYS = 15       # ± calendar days around the target date
AHEAD_DAYS = 10        # the cached baseline table reaches this far ahead, so it is rebuilt about once a week
AGENT_RESERVE_S = 60   # an agent run only starts while this much of the time budget is left
SKIN = {"source": power.SOURCE, "parameter": "skin_temperature",
        "measurement_type": "earth_skin_temperature", "unit": "K"}

Fetch = Callable[[float, float, date, date], dict]
Verify = Callable[[Region, dict], dict]   # (region, agent request) -> stored verification record


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Repo(Protocol):
    """Firestore implementation in repo.py; the tests use an in-memory one."""
    def get_meta(self) -> dict: ...
    def save_meta(self, fields: dict) -> None: ...                  # merged into engine_meta/ingestion
    def get_states(self) -> dict[str, dict]: ...                    # region id -> current region document
    def get_baseline(self, region_id: str) -> dict | None: ...
    def save_baseline(self, region_id: str, doc: dict) -> None: ...
    def save_region(self, doc: dict, hours: list[dict], transitions: list[dict]) -> None: ...


# --- baseline ---------------------------------------------------------------------------------------------------
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


# --- assessment -------------------------------------------------------------------------------------------------
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
    frontier = max(published)  # POWER fills hours it has not published yet; they wait for a later cycle
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


# --- state <-> documents ----------------------------------------------------------------------------------------
def published_frontier(region: Region, now: datetime, fetch_power: Fetch) -> datetime | None:
    """Latest hour POWER has published, probed with one request. POWER publishes every cell at once."""
    today = now.date()
    raw = fetch_power(region.lat, region.lon, today - timedelta(days=RECENT_DAYS), today)
    published = [o.observed_at for o in power.canonicalize(raw, now) if o.parameter == "skin_temperature" and o.usable]
    return max(published) if published else None


def _needs_work(doc: dict | None, frontier: datetime) -> bool:
    return not doc or not doc.get("observed_at") or parse(doc["observed_at"]) < frontier


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


# --- orchestration ----------------------------------------------------------------------------------------------
def process_region(repo: Repo, region: Region, prev: RegionState | None, now: datetime,
                   fetch_power: Fetch, fetch_cloud: Fetch, cfg: RuleConfig) -> dict:
    """One region, one write. Returns counts and, when the region entered ALERT, the agent request."""
    today = now.date()
    start = today - timedelta(days=RECENT_DAYS)
    # 1) baseline table, rebuilt only when the cached one no longer covers the recent window
    table = repo.get_baseline(region.id)
    if not table or not (table["first"] <= start.isoformat() and today.isoformat() <= table["last"]):
        table = refresh_baseline(region, today, fetch_power, now)
        repo.save_baseline(region.id, table)
    # 2) recent canonical inputs -> one assessment per hour POWER has published since the last pass
    recent = power.canonicalize(fetch_power(region.lat, region.lon, start, today), now)
    clouds = openmeteo.canonicalize(fetch_cloud(region.lat, region.lon, start, today), now)
    results = assess_new_hours(recent, clouds, table["entries"], table["years"],
                               prev.observed_at if prev else None, cfg)
    # 3) state machine hour by hour; an entry into ALERT anywhere in this pass requests the agent
    state, transitions, request = prev, [], None
    for r in results:
        before = state
        state, change = step(state, region.id, r.observed_at, r.assessment)
        if change:
            transitions.append(change)
        if should_trigger_agent(before, state, now, cfg):
            state = mark_agent_called(state, now)   # saved below, so the cooldown holds even if the agent fails
            request = {"observed_at": iso(r.observed_at), "requested_at": iso(now)}
    # 4) current state + hourly audit + transitions in one batch
    doc = {**_region_doc(region, now), "last_error": None}
    if state is not None:
        doc.update({"status": state.status.value, "since": iso(state.since),
                    "observed_at": iso(state.observed_at), "utilities": state.utilities})
        if state.last_agent_at is not None:
            doc["last_agent_at"] = iso(state.last_agent_at)
    if results:
        doc["latest"] = results[-1].record
    if request:
        doc["agent_requested_at"] = request["requested_at"]
    repo.save_region(doc, [{**r.record, "processed_at": iso(now)} for r in results],
                     [transition_doc(t) for t in transitions])
    return {"hours": len(results), "transitions": len(transitions), "agent_request": request}


def run_cycle(repo: Repo, regions: Iterable[Region], now: datetime | None = None, *, batch: int = 10,
              budget_s: float | None = None, clock: Callable[[], float] = time.monotonic,
              fetch_power: Fetch = power.fetch, fetch_cloud: Fetch = openmeteo.fetch,
              verify: Verify | None = None, cfg: RuleConfig = RuleConfig()) -> dict:
    """One 15-minute cycle: agent runs left from earlier cycles, then regions up to the latest hour POWER published.

    POWER publishes about once a day, so most cycles cost one Firestore read and one probe request. When new hours
    appear, the `batch` least recently updated regions that are behind are processed. With `budget_s`, no region or
    agent run starts once the budget is spent: the request ends inside the caller's timeout and the remaining work
    goes first on the next cycle. Agent requests wait in engine_meta, so a failed cycle never loses them.
    """
    now = now or datetime.now(timezone.utc)
    started = clock()
    by_id = {r.id: r for r in regions}
    summary = {"frontier": None, "skipped": None, "regions": [], "hours": 0, "transitions": 0,
               "agent_requests": [], "verified": [], "errors": []}
    if not by_id:
        return summary
    meta = repo.get_meta()
    pending = dict(meta.get("agent_pending") or {})   # region id -> agent request
    waiting = dict(pending)

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
    # 2) probe POWER once; stop if nothing new was published since the last full pass
    frontier, failure, caught_up = None, None, False
    for probe in list(by_id.values())[:3]:   # one failing cell must not block the probe
        try:
            frontier, failure = published_frontier(probe, now, fetch_power), None
            break
        except Exception as exc:
            failure = exc
    if failure is not None:   # POWER unreachable: try again on the next cycle
        summary["errors"].append({"region": None, "error": type(failure).__name__})
    elif frontier is None:
        summary["skipped"] = "power has no published hours"
    elif meta.get("caught_up") == iso(frontier):
        summary["frontier"], summary["skipped"] = iso(frontier), "no new hours since the last pass"
    else:
        # 3) regions that are behind, least recently updated first, within the batch and the time budget
        summary["frontier"] = iso(frontier)
        states = repo.get_states()
        behind = [r for r in by_id.values() if _needs_work(states.get(r.id), frontier)]
        due = sorted(behind, key=lambda r: states.get(r.id, {}).get("updated_at", ""))[:batch]
        for region in due:
            if time_left() <= 0:
                break
            try:
                result = process_region(repo, region, state_from_doc(states.get(region.id)), now,
                                        fetch_power, fetch_cloud, cfg)
            except Exception as exc:   # one failing source must not stop the other regions
                repo.save_region({**_region_doc(region, now), "last_error": type(exc).__name__}, [], [])
                summary["errors"].append({"region": region.id, "error": type(exc).__name__})
                continue
            summary["regions"].append(region.id)
            summary["hours"] += result["hours"]
            summary["transitions"] += result["transitions"]
            if result["agent_request"]:
                summary["agent_requests"].append(region.id)
                pending[region.id] = result["agent_request"]
        caught_up = len(summary["regions"]) == len(behind)
        # 4) agent runs for regions that entered ALERT in this pass, while time is left
        run_agents()
    # 5) remember the caught-up hour and the agent runs still waiting
    fields = {"caught_up": iso(frontier), "updated_at": iso(now)} if caught_up else {}
    if pending != waiting:
        fields["agent_pending"] = pending
    if fields:
        repo.save_meta(fields)
    return summary
