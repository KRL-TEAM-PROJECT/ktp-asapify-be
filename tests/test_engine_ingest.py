from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from asapify_api.app import app
from asapify_api.auth import require_operator
from asapify_api.engine_routes import get_engine_repo
from asapify_engine.ingest import run_cycle
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


def power_fetch(recent_celsius, humidity, calls=None, frontier=FRONTIER):
    """History years read 25 °C; the current year reads `recent_celsius`; hours after `frontier` are unpublished."""
    def fetch(lat, lon, start, end):
        if calls is not None:
            calls.append((lat, start, end))
        ts, rh = {}, {}
        for t in hours(start, end):
            key = f"{t:%Y%m%d%H}"
            if t.year == NOW.year and t > frontier:
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


class FakeRepo:
    """In-memory Repo (ingest.py) + the agent and decision parts of FirestoreRepo."""
    def __init__(self):
        self.docs, self.baselines, self.hours, self.transitions, self.meta_doc = {}, {}, {}, {}, {}
        self.verifications, self.quota, self.alerts, self.audit = {}, {}, {}, []

    def get_meta(self):
        return dict(self.meta_doc)

    def save_meta(self, fields):
        self.meta_doc.update(fields)

    def get_states(self):
        return {k: dict(v) for k, v in self.docs.items()}

    def get_baseline(self, region_id):
        return self.baselines.get(region_id)

    def save_baseline(self, region_id, doc):
        self.baselines[region_id] = doc

    def save_region(self, doc, hours, transitions):
        self.docs.setdefault(doc["id"], {}).update(doc)
        self.hours.setdefault(doc["id"], []).extend(hours)
        self.transitions.setdefault(doc["id"], []).extend(transitions)

    def get_region(self, region_id):
        return self.docs.get(region_id)

    def get_hour(self, region_id, observed_at):
        return next((h for h in self.hours.get(region_id, []) if h["observed_at"] == observed_at), None)

    def region_detail(self, region_id, hours):
        doc = self.docs.get(region_id)
        return None if doc is None else {**doc, "hours": self.hours[region_id][-hours:],
                                         "transitions": self.transitions[region_id][::-1],
                                         "verifications": self.verifications.get(region_id, [])[::-1]}

    def agent_calls(self, day):
        return self.quota.get(day, 0)

    def reserve_agent_call(self, day, limit):
        if self.quota.get(day, 0) >= limit:
            return None
        self.quota[day] = self.quota.get(day, 0) + 1
        return self.quota[day]

    def save_verification(self, region_id, record):
        self.verifications.setdefault(region_id, []).append(record)
        self.docs.setdefault(region_id, {"id": region_id}).update(
            {"verification": {"id": record["id"], "at": record["at"], "decision": None,
                              "validation_status": record["validation_status"]}, "last_agent_at": record["at"]})

    def decide(self, region_id, verification_id, check, decision, audit, alert):
        record = next((v for v in self.verifications.get(region_id, []) if v["id"] == verification_id), None)
        if record is None:
            raise LookupError(verification_id)
        check(record)
        record["decision"] = decision
        self.audit.append({**audit, **decision})
        if alert:
            self.alerts[alert["id"]] = alert
        return record


def tick(repo, celsius=25.5, humidity=80.0, cover=20, batch=2, calls=None, now=NOW, frontier=FRONTIER, verify=None):
    return run_cycle(repo, REGIONS, now, batch=batch, fetch_power=power_fetch(celsius, humidity, calls, frontier),
                     fetch_cloud=cloud_fetch(cover), verify=verify)


def test_first_cycle_assesses_every_published_hour_and_rotates_regions():
    repo = FakeRepo()
    summary = tick(repo)
    assert summary["regions"] == [r.id for r in REGIONS[:2]] and summary["errors"] == []
    first = repo.docs[REGIONS[0].id]
    assert first["status"] == "SAFE" and first["observed_at"] == "2026-10-10T23:00:00Z"
    assert len(repo.hours[REGIONS[0].id]) == 48          # 9 and 10 October; 11-13 are not published yet
    record = repo.hours[REGIONS[0].id][-1]
    assert record["inputs"]["delta_t_k"] == 0.5 and record["inputs"]["baseline_samples"] >= 30
    assert record["quality"] == {"skin_temperature": "ACCEPTED", "relative_humidity": "ACCEPTED", "cloud_cover": "ACCEPTED"}
    assert "model_estimate" in record["flags"] and record["sources"]["cloud_cover"] == "open_meteo"
    assert repo.transitions[REGIONS[0].id][0]["previous"] is None
    assert tick(repo)["regions"][0] == REGIONS[2].id     # the never-processed region goes next


def test_idle_cycles_cost_one_probe_and_new_hours_wake_the_next_pass():
    repo = FakeRepo()
    assert tick(repo, batch=3)["regions"] == [r.id for r in REGIONS]
    assert repo.meta_doc["caught_up"] == "2026-10-10T23:00:00Z"
    calls = []
    idle = tick(repo, batch=3, calls=calls, now=NOW + timedelta(minutes=15))
    assert idle["skipped"] and idle["hours"] == 0 and len(calls) == 1   # one probe, nothing else
    calls = []
    later = tick(repo, batch=3, calls=calls, now=NOW + timedelta(days=1),
                 frontier=FRONTIER + timedelta(days=1))
    assert later["hours"] == 3 * 24 and len(calls) == 1 + 3            # probe + one request per region
    assert repo.meta_doc["caught_up"] == "2026-10-11T23:00:00Z"


def test_hot_and_dry_region_enters_alert_and_requests_the_agent_once():
    repo = FakeRepo()
    summary = tick(repo, celsius=34.5, humidity=25.0, batch=1)
    rid = REGIONS[0].id
    assert repo.docs[rid]["status"] == "ALERT" and summary["agent_requests"] == [rid]
    assert repo.docs[rid]["latest"]["utilities"]["ALERT"] == 1.0
    assert repo.docs[rid]["last_agent_at"] == "2026-10-13T06:00:00Z"   # stored first: the cooldown survives a failure
    assert repo.transitions[rid] == [repo.transitions[rid][0]] and repo.transitions[rid][0]["current"] == "ALERT"
    assert repo.meta_doc["agent_pending"] == {rid: {"observed_at": "2026-10-09T00:00:00Z",
                                                    "requested_at": "2026-10-13T06:00:00Z"}}
    again = run_cycle(repo, REGIONS[:1], NOW + timedelta(hours=1), batch=1,
                      fetch_power=power_fetch(34.5, 25.0), fetch_cloud=cloud_fetch(20))
    assert again["agent_requests"] == []                   # already ALERT: no second request


def test_cooldown_from_an_earlier_cycle_blocks_a_new_request():
    repo = FakeRepo()
    rid = REGIONS[0].id
    tick(repo, celsius=34.5, humidity=25.0, batch=1)
    repo.docs[rid]["status"] = "SAFE"                      # e.g. it left ALERT in between
    later = run_cycle(repo, REGIONS[:1], NOW + timedelta(hours=2), batch=1,
                      fetch_power=power_fetch(34.5, 25.0, frontier=FRONTIER + timedelta(hours=1)),
                      fetch_cloud=cloud_fetch(20))
    assert later["agent_requests"] == []                   # last_agent_at was read back from the document


def test_waiting_agent_runs_are_served_on_the_next_cycle_even_when_idle():
    repo = FakeRepo()
    tick(repo, celsius=34.5, humidity=25.0, batch=3)       # no verifier configured: requests wait in meta
    assert len(repo.meta_doc["agent_pending"]) == 3
    seen = []

    def verify(region, request):
        seen.append((region.id, request["observed_at"]))
        return {"validation_status": "NEEDS_MANUAL_REVIEW"}

    idle = tick(repo, celsius=34.5, humidity=25.0, batch=3, now=NOW + timedelta(minutes=15), verify=verify)
    assert idle["skipped"] and [v["region"] for v in idle["verified"]] == [r.id for r in REGIONS]
    assert seen[0] == (REGIONS[0].id, "2026-10-09T00:00:00Z") and repo.meta_doc["agent_pending"] == {}


def test_agent_runs_wait_when_the_time_budget_is_spent():
    repo, ticks = FakeRepo(), iter(range(0, 10_000, 30))   # every clock read advances 30 s
    summary = run_cycle(repo, REGIONS[:1], NOW, batch=1, budget_s=60, clock=lambda: next(ticks),
                        fetch_power=power_fetch(34.5, 25.0), fetch_cloud=cloud_fetch(20),
                        verify=lambda region, request: pytest.fail("no time left for the agent"))
    assert summary["agent_requests"] == [REGIONS[0].id] and summary["verified"] == []
    assert REGIONS[0].id in repo.meta_doc["agent_pending"]


def test_a_failing_agent_is_recorded_and_not_retried_forever():
    repo = FakeRepo()

    def broken(region, request):
        raise RuntimeError("quota")

    summary = tick(repo, celsius=34.5, humidity=25.0, batch=1, verify=broken)
    assert summary["errors"] == [{"region": REGIONS[0].id, "error": "RuntimeError"}]
    assert repo.meta_doc.get("agent_pending", {}) == {}


def test_cloudy_hours_are_no_observable():
    repo = FakeRepo()
    tick(repo, cover=90, batch=1)
    assert repo.docs[REGIONS[0].id]["status"] == "NO_OBSERVABLE"
    assert "cloud cover" in repo.docs[REGIONS[0].id]["latest"]["reason"]


def test_a_failing_source_is_recorded_and_does_not_stop_other_regions():
    repo = FakeRepo()
    ok = power_fetch(25.5, 80.0)

    def flaky(lat, lon, start, end):
        if lat == REGIONS[0].lat:
            raise TimeoutError("upstream timeout")
        return ok(lat, lon, start, end)

    summary = run_cycle(repo, REGIONS, NOW, batch=2, fetch_power=flaky, fetch_cloud=cloud_fetch(20))
    assert summary["errors"] == [{"region": REGIONS[0].id, "error": "TimeoutError"}]
    assert summary["regions"] == [REGIONS[1].id]
    assert repo.docs[REGIONS[0].id]["last_error"] == "TimeoutError" and "status" not in repo.docs[REGIONS[0].id]


def test_time_budget_stops_before_starting_another_region():
    repo, ticks = FakeRepo(), iter(range(0, 1000, 30))   # every clock read advances 30 s
    summary = run_cycle(repo, REGIONS, NOW, batch=3, budget_s=50, clock=lambda: next(ticks),
                        fetch_power=power_fetch(25.5, 80.0), fetch_cloud=cloud_fetch(20))
    assert summary["regions"] == [REGIONS[0].id]          # clock: 0 at start, 30 before region 1, 60 >= 50 before region 2
    assert tick(repo, batch=1)["regions"] == [REGIONS[1].id]


def test_regions_file_is_valid():
    regions = load_regions()
    assert len(regions) > 10 and len({r.id for r in regions}) == len(regions)
    assert all(r.province.isdigit() and r.peat_km2 >= 25 for r in regions)


# ---------- API ----------

@pytest.fixture
def api():
    repo = FakeRepo()
    tick(repo, batch=len(REGIONS))
    app.dependency_overrides[get_engine_repo] = lambda: repo
    yield repo
    app.dependency_overrides.clear()


def test_region_endpoints_require_an_operator(api):
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/api/v1/operator/regions").status_code == 401
    assert c.post(f"/api/v1/operator/regions/{load_regions()[0].id}/verify").status_code == 401
    assert c.post("/_internal/engine/tick").status_code in (401, 503)   # worker identity only


def test_operator_lists_regions_and_reads_one(api):
    app.dependency_overrides[require_operator] = lambda: {"uid": "test", "email": "operator@example.test"}
    c = TestClient(app, raise_server_exceptions=False)
    listing = c.get("/api/v1/operator/regions").json()
    assert len(listing) == len(load_regions())               # unprocessed regions are listed with status null
    assert all(item["status"] is None for item in listing)   # the fake repo holds test-only region ids
    known = load_regions()[0].id
    api.docs[known] = {**api.docs[REGIONS[0].id], "id": known}
    api.hours[known] = api.hours[REGIONS[0].id]
    api.transitions[known] = api.transitions[REGIONS[0].id]
    detail = c.get(f"/api/v1/operator/regions/{known}?hours=24").json()
    assert detail["status"] == "SAFE" and len(detail["hours"]) == 24 and detail["verifications"] == []
    assert c.get("/api/v1/operator/regions/R9.99S999.999E").status_code == 404
    assert c.get("/api/v1/operator/regions/not-a-region").status_code == 400
