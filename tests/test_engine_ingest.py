from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from asapify_api.app import app
from asapify_api.auth import require_operator
from asapify_api.engine_routes import get_engine_store
from asapify_engine.ingest import run_tick
from asapify_engine.regions import Region, load_regions

UTC = timezone.utc
NOW = datetime(2026, 10, 13, 6, tzinfo=UTC)
FRONTIER = datetime(2026, 10, 10, 23, tzinfo=UTC)   # last hour POWER has published at NOW
REGIONS = [Region(f"R{i}.00S113.750E", -float(i), 113.75, (113.4, -i - 0.25, 114.1, -i + 0.25), "62", 100.0)
           for i in (1, 2, 3)]


def hours(start, end):
    t = datetime(start.year, start.month, start.day, tzinfo=UTC)
    while t <= datetime(end.year, end.month, end.day, 23, tzinfo=UTC):
        yield t
        t += timedelta(hours=1)


def power_fetch(recent_celsius, humidity, calls=None):
    """History years read 25 °C; the current year reads `recent_celsius`; hours after FRONTIER are unpublished."""
    def fetch(lat, lon, start, end):
        if calls is not None:
            calls.append((lat, start, end))
        ts, rh = {}, {}
        for t in hours(start, end):
            key = f"{t:%Y%m%d%H}"
            if t.year == NOW.year and t > FRONTIER:
                ts[key] = rh[key] = -999.0
            else:
                ts[key] = recent_celsius if t.year == NOW.year else 25.0
                rh[key] = humidity
        return {"geometry": {"coordinates": [lon, lat, 10]}, "header": {"fill_value": -999.0, "sources": ["TEST"]},
                "properties": {"parameter": {"TS": ts, "RH2M": rh}}}
    return fetch


def cloud_fetch(cover):
    def fetch(lat, lon, start, end):
        times = list(hours(start, end))
        return {"latitude": lat, "longitude": lon,
                "hourly": {"time": [f"{t:%Y-%m-%dT%H:%M}" for t in times], "cloud_cover": [cover] * len(times)}}
    return fetch


class FakeStore:
    def __init__(self):
        self.docs, self.baselines, self.hours, self.transitions = {}, {}, {}, {}

    def states(self):
        return {k: dict(v) for k, v in self.docs.items()}

    def baseline(self, region_id):
        return self.baselines.get(region_id)

    def save_baseline(self, region_id, doc):
        self.baselines[region_id] = doc

    def save_region(self, doc, hours, transitions):
        self.docs.setdefault(doc["id"], {}).update(doc)
        self.hours.setdefault(doc["id"], []).extend(hours)
        self.transitions.setdefault(doc["id"], []).extend(transitions)

    def region_detail(self, region_id, hours):
        doc = self.docs.get(region_id)
        return None if doc is None else {**doc, "hours": self.hours[region_id][-hours:],
                                         "transitions": self.transitions[region_id][::-1]}


def tick(store, celsius=25.5, humidity=80.0, cover=20, batch=2, calls=None, now=NOW):
    return run_tick(store, REGIONS, now, batch=batch, fetch_power=power_fetch(celsius, humidity, calls),
                    fetch_cloud=cloud_fetch(cover))


def test_first_tick_assesses_every_published_hour_and_rotates_regions():
    store = FakeStore()
    summary = tick(store)
    assert summary["regions"] == [r.id for r in REGIONS[:2]] and summary["errors"] == []
    first = store.docs[REGIONS[0].id]
    assert first["status"] == "SAFE" and first["observed_at"] == "2026-10-10T23:00:00Z"
    assert len(store.hours[REGIONS[0].id]) == 48          # 9 and 10 October; 11-13 are not published yet
    record = store.hours[REGIONS[0].id][-1]
    assert record["inputs"]["delta_t_k"] == 0.5 and record["inputs"]["baseline_samples"] >= 30
    assert record["quality"] == {"skin_temperature": "ACCEPTED", "relative_humidity": "ACCEPTED", "cloud_cover": "ACCEPTED"}
    assert "model_estimate" in record["flags"] and record["sources"]["cloud_cover"] == "open_meteo"
    assert store.transitions[REGIONS[0].id][0]["previous"] is None
    assert tick(store)["regions"][0] == REGIONS[2].id     # the never-processed region goes next


def test_later_ticks_only_add_new_hours_and_reuse_the_baseline():
    store = FakeStore()
    tick(store, batch=3)
    calls = []
    summary = tick(store, batch=3, calls=calls, now=NOW + timedelta(minutes=15))
    assert summary["hours"] == 0 and summary["transitions"] == 0
    assert len(calls) == 3                                 # one recent request per region, no baseline rebuild


def test_hot_and_dry_region_enters_alert_and_requests_the_agent_once():
    store = FakeStore()
    summary = tick(store, celsius=34.5, humidity=25.0, batch=1)
    rid = REGIONS[0].id
    assert store.docs[rid]["status"] == "ALERT" and summary["agent_requests"] == [rid]
    assert store.docs[rid]["latest"]["utilities"]["ALERT"] == 1.0
    assert store.transitions[rid] == [store.transitions[rid][0]] and store.transitions[rid][0]["current"] == "ALERT"
    again = run_tick(store, REGIONS[:1], NOW + timedelta(hours=1), batch=1,
                     fetch_power=power_fetch(34.5, 25.0), fetch_cloud=cloud_fetch(20))
    assert again["agent_requests"] == []                   # already ALERT: no second request


def test_cloudy_hours_are_no_observable():
    store = FakeStore()
    tick(store, cover=90, batch=1)
    assert store.docs[REGIONS[0].id]["status"] == "NO_OBSERVABLE"
    assert "cloud cover" in store.docs[REGIONS[0].id]["latest"]["reason"]


def test_a_failing_source_is_recorded_and_does_not_stop_other_regions():
    store = FakeStore()
    ok = power_fetch(25.5, 80.0)

    def flaky(lat, lon, start, end):
        if lat == REGIONS[0].lat:
            raise TimeoutError("upstream timeout")
        return ok(lat, lon, start, end)

    summary = run_tick(store, REGIONS, NOW, batch=2, fetch_power=flaky, fetch_cloud=cloud_fetch(20))
    assert summary["errors"] == [{"region": REGIONS[0].id, "error": "TimeoutError"}]
    assert summary["regions"] == [REGIONS[1].id]
    assert store.docs[REGIONS[0].id]["last_error"] == "TimeoutError" and "status" not in store.docs[REGIONS[0].id]


def test_time_budget_stops_before_starting_another_region():
    store, ticks = FakeStore(), iter(range(0, 1000, 30))   # every clock read advances 30 s
    summary = run_tick(store, REGIONS, NOW, batch=3, budget_s=50, clock=lambda: next(ticks),
                       fetch_power=power_fetch(25.5, 80.0), fetch_cloud=cloud_fetch(20))
    assert summary["regions"] == [REGIONS[0].id]          # clock: 0 at start, 30 before region 1, 60 >= 50 before region 2
    assert tick(store, batch=1)["regions"] == [REGIONS[1].id]


def test_regions_file_is_valid():
    regions = load_regions()
    assert len(regions) > 10 and len({r.id for r in regions}) == len(regions)
    assert all(r.province.isdigit() and r.peat_km2 >= 25 for r in regions)


# ---------- API ----------

@pytest.fixture
def api():
    store = FakeStore()
    tick(store, batch=len(REGIONS))
    app.dependency_overrides[get_engine_store] = lambda: store
    yield store
    app.dependency_overrides.clear()


def test_region_endpoints_require_an_operator(api):
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/api/v1/operator/regions").status_code == 401
    assert c.post("/_internal/engine/tick").status_code in (401, 503)   # worker identity only


def test_operator_lists_regions_and_reads_one(api):
    app.dependency_overrides[require_operator] = lambda: {"uid": "test", "email": "operator@example.test"}
    c = TestClient(app, raise_server_exceptions=False)
    listing = c.get("/api/v1/operator/regions").json()
    assert len(listing) == len(load_regions())               # unprocessed regions are listed with status null
    assert all(item["status"] is None for item in listing)   # the fake store holds test-only region ids
    known = load_regions()[0].id
    api.docs[known] = {**api.docs[REGIONS[0].id], "id": known}
    api.hours[known] = api.hours[REGIONS[0].id]
    api.transitions[known] = api.transitions[REGIONS[0].id]
    detail = c.get(f"/api/v1/operator/regions/{known}?hours=24").json()
    assert detail["status"] == "SAFE" and len(detail["hours"]) == 24
    assert c.get("/api/v1/operator/regions/R9.99S999.999E").status_code == 404
    assert c.get("/api/v1/operator/regions/not-a-region").status_code == 400
