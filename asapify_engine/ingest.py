"""NASA POWER supporting data: per region, the skin-temperature anomaly and humidity of every newly published hour.

POWER is reanalysis, 0.5 x 0.625 degrees and 2-3 days behind, so it no longer sets the status: Himawari-9 and GK2A do
(geo.py). It supports them instead: fusion.py reads the latest `power` summary of a region whenever a satellite sees
a possible hotspot there. Network access goes through injectable fetchers and storage through the Repo protocol, so the
whole flow runs in tests without the internet or Firestore. Shared helpers for both cycles live here too.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Protocol

from . import power
from .canonical import Baseline, Observation, anomaly, build_baseline
from .models import RegionState, Status, Transition
from .regions import Region

RECENT_DAYS = 4        # POWER publishes about 2-3 days behind real time
BASELINE_YEARS = 5     # the baseline uses the five years before the current one
WINDOW_DAYS = 15       # ± calendar days around the target date
AHEAD_DAYS = 10        # the cached baseline table reaches this far ahead, so it is rebuilt about once a week
SKIN = {"source": power.SOURCE, "parameter": "skin_temperature",
        "measurement_type": "earth_skin_temperature", "unit": "K"}

Fetch = Callable[[float, float, date, date], dict]


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
    def save_region(self, doc: dict, hours: list[dict]) -> None: ...                 # POWER support
    def save_slots(self, entries: list[tuple[dict, dict | None, list[dict]]],   # (state, slot, transitions)
                   day: dict | None = None) -> None: ...                       # + the slot's day archive entry


# --- state <-> documents (used by the geostationary cycle) ------------------------------------------------------
def state_from_doc(doc: dict | None) -> RegionState | None:
    if not doc or not doc.get("status"):
        return None
    return RegionState(doc["id"], Status(doc["status"]), parse(doc["since"]), parse(doc["observed_at"]),
                       doc.get("utilities") or {}, parse(doc["last_agent_at"]) if doc.get("last_agent_at") else None)


def transition_doc(t: Transition) -> dict:
    return {"region_id": t.region_id, "observed_at": iso(t.observed_at),
            "previous": t.previous.value if t.previous else None, "current": t.current.value,
            "utilities": t.utilities, "reason": t.reason}


def region_doc(region: Region, now: datetime) -> dict:
    return {"id": region.id, "province": region.province, "lat": region.lat, "lon": region.lon,
            "bounds": list(region.bounds), "peat_km2": region.peat_km2, "updated_at": iso(now)}


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


# --- supporting records -----------------------------------------------------------------------------------------
def _value(obs: Observation | None) -> float | None:
    return obs.value if obs is not None and obs.usable else None


def _quality(obs: Observation | None) -> str:
    return obs.quality.value if obs is not None else "MISSING"


def power_records(recent: list[Observation], table: dict, years: str, after: datetime | None) -> list[dict]:
    """One record per hour POWER has published since `after`: skin temperature against its baseline, and humidity."""
    ts = {o.observed_at: o for o in recent if o.parameter == "skin_temperature"}
    rh = {o.observed_at: o for o in recent if o.parameter == "relative_humidity"}
    published = [t for t, o in ts.items() if o.usable]
    if not published:
        return []
    frontier = max(published)  # POWER fills hours it has not published yet; they wait for a later cycle
    records = []
    for t in sorted(t for t in ts if t <= frontier and (after is None or t > after)):
        entry = table.get(f"{t:%m%d%H}", [None, None, 0])
        baseline = Baseline(**SKIN, mean=entry[0], std=entry[1], samples=entry[2],
                            description=f"UTC hour {t:%H}, ±{WINDOW_DAYS} days of {t:%m-%d}, years {years}")
        humidity = rh.get(t)
        records.append({
            "observed_at": iso(t),
            "inputs": {"skin_temperature_k": _value(ts[t]), "baseline_k": baseline.mean,
                       "baseline_std_k": baseline.std, "baseline_samples": baseline.samples,
                       "baseline": baseline.description, "delta_t_k": anomaly(ts[t], baseline),
                       "relative_humidity_pct": _value(humidity)},
            "quality": {"skin_temperature": _quality(ts[t]), "relative_humidity": _quality(humidity)},
            "flags": sorted({f for o in (ts[t], humidity) if o is not None for f in o.quality_flags}),
            "sources": {"skin_temperature": power.SOURCE, "relative_humidity": power.SOURCE},
        })
    return records


def published_frontier(region: Region, now: datetime, fetch_power: Fetch) -> datetime | None:
    """Latest hour POWER has published, probed with one request. POWER publishes every cell at once."""
    today = now.date()
    raw = fetch_power(region.lat, region.lon, today - timedelta(days=RECENT_DAYS), today)
    published = [o.observed_at for o in power.canonicalize(raw, now) if o.parameter == "skin_temperature" and o.usable]
    return max(published) if published else None


def _caught_up(doc: dict | None, frontier: datetime) -> bool:
    observed = ((doc or {}).get("power") or {}).get("observed_at")
    return bool(observed) and parse(observed) >= frontier


# --- orchestration ----------------------------------------------------------------------------------------------
def process_region(repo: Repo, region: Region, last: str | None, now: datetime, fetch_power: Fetch) -> int:
    """One region, one write: new hourly POWER records and the region's `power` summary (the latest of them)."""
    today = now.date()
    start = today - timedelta(days=RECENT_DAYS)
    # 1) baseline table, rebuilt only when the cached one no longer covers the recent window
    table = repo.get_baseline(region.id)
    if not table or not (table["first"] <= start.isoformat() and today.isoformat() <= table["last"]):
        table = refresh_baseline(region, today, fetch_power, now)
        repo.save_baseline(region.id, table)
    # 2) recent canonical inputs -> one record per hour POWER has published since the last pass
    recent = power.canonicalize(fetch_power(region.lat, region.lon, start, today), now)
    records = power_records(recent, table["entries"], table["years"], parse(last) if last else None)
    # 3) region document keeps only the latest hour as `power`; the status belongs to the satellites
    doc = {"id": region.id, "power_error": None}
    if records:
        latest = records[-1]
        doc["power"] = {"observed_at": latest["observed_at"], **latest["inputs"], "updated_at": iso(now)}
    repo.save_region(doc, [{**r, "processed_at": iso(now)} for r in records])
    return len(records)


def run_cycle(repo: Repo, regions: Iterable[Region], now: datetime | None = None, *, batch: int = 10,
              budget_s: float | None = None, clock: Callable[[], float] = time.monotonic,
              fetch_power: Fetch = power.fetch) -> dict:
    """Bring every region's POWER support up to the latest published hour.

    POWER publishes about once a day, so most cycles cost one Firestore read and one probe request. When new hours
    appear, the `batch` least recently updated regions that are behind are processed. With `budget_s`, no region
    starts once the budget is spent: the request ends inside the caller's timeout and the rest go first next time.
    """
    now = now or datetime.now(timezone.utc)
    started = clock()
    regions = list(regions)
    summary = {"frontier": None, "skipped": None, "regions": [], "hours": 0, "errors": []}
    if not regions:
        return summary
    meta = repo.get_meta()
    # 1) probe POWER once; stop if nothing new was published since the last full pass
    frontier, failure = None, None
    for probe in regions[:3]:   # one failing cell must not block the probe
        try:
            frontier, failure = published_frontier(probe, now, fetch_power), None
            break
        except Exception as exc:
            failure = exc
    if failure is not None:   # POWER unreachable: try again on the next cycle
        summary["errors"].append({"region": None, "error": type(failure).__name__})
        return summary
    if frontier is None:
        summary["skipped"] = "power has no published hours"
        return summary
    summary["frontier"] = iso(frontier)
    if meta.get("caught_up") == iso(frontier):
        summary["skipped"] = "no new hours since the last pass"
        return summary
    # 2) regions that are behind, least recently updated first, within the batch and the time budget
    states = repo.get_states()
    behind = [r for r in regions if not _caught_up(states.get(r.id), frontier)]
    due = sorted(behind, key=lambda r: ((states.get(r.id) or {}).get("power") or {}).get("updated_at", ""))[:batch]
    for region in due:
        if budget_s is not None and clock() - started >= budget_s:
            break
        try:
            last = ((states.get(region.id) or {}).get("power") or {}).get("observed_at")
            summary["hours"] += process_region(repo, region, last, now, fetch_power)
        except Exception as exc:   # one failing source must not stop the other regions
            repo.save_region({"id": region.id, "power_error": type(exc).__name__}, [])
            summary["errors"].append({"region": region.id, "error": type(exc).__name__})
            continue
        summary["regions"].append(region.id)
    # 3) remember the hour every region has caught up with
    if len(summary["regions"]) == len(behind):
        repo.save_meta({"caught_up": iso(frontier), "updated_at": iso(now)})
    return summary
