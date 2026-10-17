"""NASA POWER: the "normal" environment of each region, and the newest hours measured against it.

POWER is reanalysis, 0.5 x 0.625 degrees and 2-3 days behind, so it does not set the status: Himawari-9 and GK2A do
(geo.py). Its five-year climatology per region, UTC hour and calendar window is the normal that the rules shift their
curves by. Every newly published hour gets its skin-temperature anomaly and its dryness `dry_z` (how many σ the
humidity is below that hour's normal); the region's `power` summary keeps the mean `dry_z` of the last 24 published
hours and that day's humidity normal per hour, for the Open-Meteo fallback. Network access goes through injectable
fetchers and storage through the Repo protocol, so the whole flow runs in tests without the internet or Firestore.
Shared helpers for both cycles live here too.
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
BASELINE_YEARS = 2     # the two years before the current one: recent land cover; 2 x 31 days >= MIN_SAMPLES per hour.
                       # A candidate under evaluation (5 until 17 Oct 2026), not a final value
WINDOW_DAYS = 15       # ± calendar days around the target date
AHEAD_DAYS = 10        # the cached baseline table reaches this far ahead, so it is rebuilt about once a week
SKIN = {"source": power.SOURCE, "parameter": "skin_temperature",
        "measurement_type": "earth_skin_temperature", "unit": "K"}
RH = {"source": power.SOURCE, "parameter": "relative_humidity", "measurement_type": "relative_humidity_2m", "unit": "%"}
DRY_HOURS = 24             # the dryness of a region is the mean dry_z of its last 24 published hours
DRY_SIGMA_FLOOR = 3.0      # %; a very steady humidity normal must not turn a small difference into a huge z
MIN_SAMPLES = 30           # baselines thinner than this give no anomaly (as canonical.anomaly)
SUMMARY_VERSION = 2        # the `power` summary with dry_z and rh_normal; a pass with an older one is redone

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


def baseline_years(today: date) -> range:
    return range(today.year - BASELINE_YEARS, today.year)


def years_label(years: range) -> str:
    return f"{years[0]}-{years[-1]}"


def refresh_baseline(region: Region, today: date, fetch_power: Fetch, now: datetime) -> dict:
    """Skin temperature (`entries`) and humidity (`entries_rh`) normals; both come in the same POWER requests."""
    first, last = today - timedelta(days=RECENT_DAYS + 1), today + timedelta(days=AHEAD_DAYS)
    years = baseline_years(today)
    history: list[Observation] = []
    for year in years:
        start = _same_day(first, year) - timedelta(days=WINDOW_DAYS)
        end = start + (last - first) + timedelta(days=2 * WINDOW_DAYS)
        history += power.canonicalize(fetch_power(region.lat, region.lon, start, end), now)
    skin = [o for o in history if o.parameter == SKIN["parameter"]]
    humidity = [o for o in history if o.parameter == RH["parameter"]]
    return {"first": first.isoformat(), "last": last.isoformat(), "years": years_label(years),
            "window_days": WINDOW_DAYS, "entries": baseline_table(skin, first, last),
            "entries_rh": baseline_table(humidity, first, last), "built_at": iso(now)}


# --- supporting records -----------------------------------------------------------------------------------------
def _value(obs: Observation | None) -> float | None:
    return obs.value if obs is not None and obs.usable else None


def _quality(obs: Observation | None) -> str:
    return obs.quality.value if obs is not None else "MISSING"


def dry_z(humidity: Observation | None, entry: list | None) -> float | None:
    """How many σ the humidity is below its normal for that hour (positive = drier); same source as the baseline."""
    mean, std, samples = entry or (None, None, 0)
    if humidity is None or not humidity.usable or mean is None or samples < MIN_SAMPLES:
        return None
    return round((mean - humidity.value) / max(std or 0.0, DRY_SIGMA_FLOOR), 3)


def power_records(recent: list[Observation], table: dict, after: datetime | None) -> list[dict]:
    """One record per hour POWER has published since `after`: skin temperature and humidity against their normals."""
    entries, entries_rh, years = table["entries"], table.get("entries_rh") or {}, table["years"]
    ts = {o.observed_at: o for o in recent if o.parameter == "skin_temperature"}
    rh = {o.observed_at: o for o in recent if o.parameter == "relative_humidity"}
    published = [t for t, o in ts.items() if o.usable]
    if not published:
        return []
    frontier = max(published)  # POWER fills hours it has not published yet; they wait for a later cycle
    records = []
    for t in sorted(t for t in ts if t <= frontier and (after is None or t > after)):
        entry = entries.get(f"{t:%m%d%H}", [None, None, 0])
        baseline = Baseline(**SKIN, mean=entry[0], std=entry[1], samples=entry[2],
                            description=f"UTC hour {t:%H}, ±{WINDOW_DAYS} days of {t:%m-%d}, years {years}")
        humidity = rh.get(t)
        normal = entries_rh.get(f"{t:%m%d%H}")
        records.append({
            "observed_at": iso(t),
            "inputs": {"skin_temperature_k": _value(ts[t]), "baseline_k": baseline.mean,
                       "baseline_std_k": baseline.std, "baseline_samples": baseline.samples,
                       "baseline": baseline.description, "delta_t_k": anomaly(ts[t], baseline),
                       "relative_humidity_pct": _value(humidity),
                       "rh_baseline_pct": normal[0] if normal else None,
                       "rh_baseline_std_pct": normal[1] if normal else None, "dry_z": dry_z(humidity, normal)},
            "quality": {"skin_temperature": _quality(ts[t]), "relative_humidity": _quality(humidity)},
            "flags": sorted({f for o in (ts[t], humidity) if o is not None for f in o.quality_flags}),
            "sources": {"skin_temperature": power.SOURCE, "relative_humidity": power.SOURCE},
        })
    return records


def power_summary(records: list[dict], table: dict, now: datetime) -> dict:
    """The region's `power`: the latest hour, the mean dryness of the last DRY_HOURS hours, and that day's humidity
    normal per UTC hour (the Open-Meteo fallback in geo.py measures against it). `rh_normal` is two flat lists of 24,
    mean and σ: Firestore stores no array inside an array."""
    latest = records[-1]
    end = parse(latest["observed_at"])
    zs = [r["inputs"]["dry_z"] for r in records
          if r["inputs"].get("dry_z") is not None and end - parse(r["observed_at"]) < timedelta(hours=DRY_HOURS)]
    entries_rh = table.get("entries_rh") or {}
    normal = [entries_rh.get(f"{end:%m%d}{h:02d}") for h in range(24)]
    normal = [e if e and e[0] is not None and e[2] >= MIN_SAMPLES else None for e in normal]
    return {"observed_at": latest["observed_at"], **latest["inputs"],
            "dry_z": round(sum(zs) / len(zs), 3) if zs else None, "dry_z_hours": len(zs),
            "rh_normal": {"mean": [e[0] if e else None for e in normal], "std": [e[1] if e else None for e in normal]},
            "updated_at": iso(now)}


def published_frontier(region: Region, now: datetime, fetch_power: Fetch) -> datetime | None:
    """Latest hour POWER has published, probed with one request. POWER publishes every cell at once."""
    today = now.date()
    raw = fetch_power(region.lat, region.lon, today - timedelta(days=RECENT_DAYS), today)
    published = [o.observed_at for o in power.canonicalize(raw, now) if o.parameter == "skin_temperature" and o.usable]
    return max(published) if published else None


def _caught_up(doc: dict | None, frontier: datetime) -> bool:
    summary = (doc or {}).get("power") or {}
    observed = summary.get("observed_at")   # a summary without rh_normal predates the dryness: rebuild it
    return bool(observed) and parse(observed) >= frontier and isinstance(summary.get("rh_normal"), dict)


# --- orchestration ----------------------------------------------------------------------------------------------
def process_region(repo: Repo, region: Region, last: str | None, now: datetime, fetch_power: Fetch) -> int:
    """One region, one write: new hourly POWER records and the region's `power` summary (the latest of them)."""
    today = now.date()
    start = today - timedelta(days=RECENT_DAYS)
    # 1) baseline table, rebuilt only when the cached one no longer covers the recent window or other years
    table = repo.get_baseline(region.id)
    if not table or "entries_rh" not in table or table.get("years") != years_label(baseline_years(today)) or not (
            table["first"] <= start.isoformat() and today.isoformat() <= table["last"]):
        table = refresh_baseline(region, today, fetch_power, now)
        repo.save_baseline(region.id, table)
    # 2) recent canonical inputs -> one record per published hour; only the hours since the last pass are new
    recent = power.canonicalize(fetch_power(region.lat, region.lon, start, today), now)
    records = power_records(recent, table, None)
    after = parse(last) if last else None
    new = [r for r in records if after is None or parse(r["observed_at"]) > after]
    # 3) region document keeps the `power` summary; the status belongs to the satellites
    doc = {"id": region.id, "power_error": None}
    if records:   # same write either way; refreshes a summary that has no new hours but an older shape
        doc["power"] = power_summary(records, table, now)
    repo.save_region(doc, [{**r, "processed_at": iso(now)} for r in new])
    return len(new)


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
    if meta.get("caught_up") == iso(frontier) and meta.get("summary") == SUMMARY_VERSION:
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
        repo.save_meta({"caught_up": iso(frontier), "summary": SUMMARY_VERSION, "updated_at": iso(now)})
    return summary
