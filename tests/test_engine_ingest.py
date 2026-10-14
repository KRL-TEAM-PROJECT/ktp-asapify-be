import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import asapify_engine
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


class FakeRepo:
    """In-memory Repo (ingest.py, geo.py) + the agent and decision parts of FirestoreRepo."""
    def __init__(self):
        self.docs, self.baselines, self.hours, self.slots, self.transitions, self.meta_doc = {}, {}, {}, {}, {}, {}
        self.verifications, self.quota, self.alerts, self.audit, self.days = {}, {}, {}, [], {}

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

    def save_region(self, doc, hours):
        self.docs.setdefault(doc["id"], {}).update(doc)
        self.hours.setdefault(doc["id"], []).extend(hours)

    def save_slots(self, entries, day=None):
        for doc, record, transitions in entries:
            self.docs.setdefault(doc["id"], {}).update(doc)   # top-level fields replaced whole, like merge=list(doc)
            if record is not None:
                self.slots.setdefault(doc["id"], []).append(record)
            self.transitions.setdefault(doc["id"], []).extend(transitions)
        if day is not None:   # like merge=True: the slot joins the `slots` map, other fields are replaced
            stored = self.days.setdefault(day["date"], {"slots": {}})
            stored.update({k: v for k, v in day.items() if k != "slots"})
            stored["slots"].update(day["slots"])

    def get_region(self, region_id):
        return self.docs.get(region_id)

    def get_slot(self, region_id, observed_at):
        return next((s for s in self.slots.get(region_id, []) if s["observed_at"] == observed_at), None)

    def region_detail(self, region_id, hours, window=None):
        doc = self.docs.get(region_id)
        if doc is None:
            return None
        slots = self.slots.get(region_id, [])
        slots = [s for s in slots if window[0] <= s["observed_at"] < window[1]] if window else slots[-hours * 6:]
        return {**doc, "slots": slots, "hours": self.hours.get(region_id, [])[-hours:],
                "transitions": self.transitions.get(region_id, [])[::-1],
                "verifications": self.verifications.get(region_id, [])[::-1]}

    def get_day(self, day):
        return self.days.get(day)

    def list_days(self, month):
        return [{"date": d["date"], "alert": bool(d.get("alert")), "warning": bool(d.get("warning"))}
                for d in self.days.values() if d["month"] == month]

    def day_range(self):
        keys = sorted(self.days)
        return (keys[0], keys[-1]) if keys else (None, None)

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


def tick(repo, celsius=25.5, humidity=80.0, batch=2, calls=None, now=NOW, frontier=FRONTIER):
    return run_cycle(repo, REGIONS, now, batch=batch, fetch_power=power_fetch(celsius, humidity, calls, frontier))


# ---------- NASA POWER as supporting data ----------

def test_power_support_records_every_published_hour_and_rotates_regions():
    repo = FakeRepo()
    summary = tick(repo)
    assert summary["regions"] == [r.id for r in REGIONS[:2]] and summary["errors"] == []
    first = repo.docs[REGIONS[0].id]
    assert "status" not in first                         # the satellites set the status, not POWER
    assert first["power"]["observed_at"] == "2026-10-10T23:00:00Z" and first["power"]["delta_t_k"] == 0.5
    assert first["power"]["relative_humidity_pct"] == 80.0 and first["power_error"] is None
    assert len(repo.hours[REGIONS[0].id]) == 48          # 9 and 10 October; 11-13 are not published yet
    record = repo.hours[REGIONS[0].id][-1]
    assert record["inputs"]["baseline_samples"] >= 30 and "status" not in record
    assert record["quality"] == {"skin_temperature": "ACCEPTED", "relative_humidity": "ACCEPTED"}
    assert "reanalysis_estimate" in record["flags"]
    assert tick(repo)["regions"][0] == REGIONS[2].id     # the never-processed region goes next


def test_idle_cycles_cost_one_probe_and_new_hours_wake_the_next_pass():
    repo = FakeRepo()
    assert tick(repo, batch=3)["regions"] == [r.id for r in REGIONS]
    assert repo.meta_doc["caught_up"] == "2026-10-10T23:00:00Z"
    calls = []
    idle = tick(repo, batch=3, calls=calls, now=NOW + timedelta(minutes=15))
    assert idle["skipped"] and idle["hours"] == 0 and len(calls) == 1   # one probe, nothing else
    calls = []
    later = tick(repo, batch=3, calls=calls, now=NOW + timedelta(days=1), frontier=FRONTIER + timedelta(days=1))
    assert later["hours"] == 3 * 24 and len(calls) == 1 + 3            # probe + one request per region
    assert repo.meta_doc["caught_up"] == "2026-10-11T23:00:00Z"
    assert repo.docs[REGIONS[0].id]["power"]["observed_at"] == "2026-10-11T23:00:00Z"


def test_a_failing_source_is_recorded_and_does_not_stop_other_regions():
    repo = FakeRepo()
    ok = power_fetch(25.5, 80.0)

    def flaky(lat, lon, start, end):
        if lat == REGIONS[0].lat:
            raise TimeoutError("upstream timeout")
        return ok(lat, lon, start, end)

    summary = run_cycle(repo, REGIONS, NOW, batch=2, fetch_power=flaky)
    assert summary["errors"] == [{"region": REGIONS[0].id, "error": "TimeoutError"}]
    assert summary["regions"] == [REGIONS[1].id]
    assert repo.docs[REGIONS[0].id]["power_error"] == "TimeoutError" and "power" not in repo.docs[REGIONS[0].id]


def test_time_budget_stops_before_starting_another_region():
    repo, ticks = FakeRepo(), iter(range(0, 1000, 30))   # every clock read advances 30 s
    summary = run_cycle(repo, REGIONS, NOW, batch=3, budget_s=50, clock=lambda: next(ticks),
                        fetch_power=power_fetch(25.5, 80.0))
    assert summary["regions"] == [REGIONS[0].id]          # clock: 0 at start, 30 before region 1, 60 >= 50 before region 2
    assert tick(repo, batch=1)["regions"] == [REGIONS[1].id]


def test_regions_file_and_peat_shapes_are_valid():
    regions = load_regions()
    assert len(regions) > 10 and len({r.id for r in regions}) == len(regions)
    assert all(r.province.isdigit() and r.peat_km2 >= 25 for r in regions)
    peat = json.loads(Path(asapify_engine.__file__).with_name("regions_peat.geojson").read_text(encoding="utf-8"))
    assert {f["properties"]["id"] for f in peat["features"]} == {r.id for r in regions}


# ---------- API ----------

@pytest.fixture
def api():
    repo = FakeRepo()
    app.dependency_overrides[get_engine_repo] = lambda: repo
    yield repo
    app.dependency_overrides.clear()


def test_region_endpoints_require_an_operator(api):
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/api/v1/operator/regions").status_code == 401
    assert c.post(f"/api/v1/operator/regions/{load_regions()[0].id}/verify").status_code == 401
    assert c.post("/_internal/engine/tick").status_code in (401, 503)       # worker identity only
    assert c.post("/_internal/engine/geo-tick").status_code in (401, 503)


def test_operator_lists_regions_and_reads_one_with_its_slots(api):
    app.dependency_overrides[require_operator] = lambda: {"uid": "test", "email": "operator@example.test"}
    c = TestClient(app, raise_server_exceptions=False)
    known = load_regions()[0].id
    start = datetime(2026, 10, 11, 12, tzinfo=UTC)
    slots = [{"observed_at": f"{start + timedelta(minutes=10 * i):%Y-%m-%dT%H:%M:%SZ}", "status": "SAFE",
              "inputs": {"delta_t_k": 0.4}, "quality": {"thermal": "ACCEPTED"}, "flags": ["two_satellites_quiet"],
              "night": True, "reason": "SAFE has the highest utility (1.000)"} for i in range(30)]
    api.docs[known] = {"id": known, "status": "SAFE", "since": slots[0]["observed_at"],
                       "observed_at": slots[-1]["observed_at"], "utilities": {"SAFE": 1.0}, "latest": slots[-1],
                       "power": {"observed_at": "2026-10-10T23:00:00Z", "delta_t_k": 0.5}}
    api.slots[known] = slots
    api.hours[known] = [{"observed_at": f"2026-10-10T{h:02d}:00:00Z"} for h in range(24)]
    listing = c.get("/api/v1/operator/regions").json()
    assert len(listing) == len(load_regions())               # unprocessed regions are listed with status null
    item = next(i for i in listing if i["id"] == known)
    assert item["status"] == "SAFE" and item["flags"] == ["two_satellites_quiet"] and item["night"] is True
    assert sum(1 for i in listing if i["status"] is None) == len(listing) - 1
    detail = c.get(f"/api/v1/operator/regions/{known}?hours=2").json()
    assert len(detail["slots"]) == 12 and detail["slots"][-1] == slots[-1] and len(detail["hours"]) == 2
    assert detail["power"]["delta_t_k"] == 0.5 and detail["verifications"] == []
    assert len(c.get(f"/api/v1/operator/regions/{known}").json()["slots"]) == 30   # default: the last 24 hours
    assert c.get(f"/api/v1/operator/regions/{known}?hours=500").status_code >= 400
    assert c.get("/api/v1/operator/regions/R9.99S999.999E").status_code == 404
    assert c.get("/api/v1/operator/regions/not-a-region").status_code == 400


def test_operator_reads_the_day_archive_and_a_region_by_day(api):
    app.dependency_overrides[require_operator] = lambda: {"uid": "test", "email": "operator@example.test"}
    c = TestClient(app, raise_server_exceptions=False)
    ids = [r.id for r in load_regions()]
    slot = {"at": "2026-10-11T18:10:00Z", "satellites": ["himawari9", "gk2a"], "status": "S" * len(ids),
            "delta_t": [0.4] * len(ids), "hot": {}}
    older = {**slot, "at": "2026-10-11T18:00:00Z", "status": "S" * (len(ids) - 1)}   # written with another regions file
    api.days["2026-10-12"] = {"date": "2026-10-12", "month": "2026-10", "regions": ids, "alert": True,
                              "slots": {"0110": json.dumps(slot), "0100": json.dumps(older)}}
    assert c.get("/api/v1/operator/days?month=2026-10").json() == {
        "first": "2026-10-12", "last": "2026-10-12", "days": [{"date": "2026-10-12", "alert": True, "warning": False}]}
    assert c.get("/api/v1/operator/days?month=2026-11").json()["days"] == []
    assert c.get("/api/v1/operator/days?month=2026-13").status_code == 400
    day = c.get("/api/v1/operator/days/2026-10-12").json()
    assert day["regions"] == ids and day["slots"] == [{"key": "0110", **slot}]   # the misaligned slot is left out
    assert c.get("/api/v1/operator/days/2026-10-13").json()["error"]["code"] == "NOT_FOUND"
    assert c.get("/api/v1/operator/days/2026-02-30").status_code == 400
    known = ids[0]
    api.docs[known] = {"id": known, "status": "SAFE"}
    api.slots[known] = [{"observed_at": t} for t in ("2026-10-11T16:50:00Z", "2026-10-11T17:00:00Z",
                                                     "2026-10-12T16:50:00Z", "2026-10-12T17:00:00Z")]
    detail = c.get(f"/api/v1/operator/regions/{known}?date=2026-10-12").json()   # 00:00-23:50 WIB on 12 October
    assert [s["observed_at"] for s in detail["slots"]] == ["2026-10-11T17:00:00Z", "2026-10-12T16:50:00Z"]
    assert c.get(f"/api/v1/operator/regions/{known}?date=12-10-2026").status_code == 400
