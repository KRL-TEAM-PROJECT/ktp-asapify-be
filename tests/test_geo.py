"""Geostationary engine: contextual anomaly, two-satellite fusion, slot choice and the 10-minute cycle, all offline."""
import math
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pytest

from asapify_engine import firms, openmeteo, satellites
from asapify_engine.canonical import Quality
from asapify_engine.contextual import RegionThermal, region_thermal
from asapify_engine.fusion import FusionConfig, fuse, power_support, solar_elevation_deg, viirs_support
from asapify_engine.geo import pick_slot, run_geo_cycle
from test_engine_ingest import REGIONS, FakeRepo

UTC = timezone.utc
NIGHT = datetime(2026, 10, 11, 18, 10, tzinfo=UTC)   # about 01:40 local solar time in Kalimantan
DAY = datetime(2026, 10, 11, 5, 0, tzinfo=UTC)       # about 12:30 local solar time
CENTRE = (REGIONS[0].lat, REGIONS[0].lon)


def view(anomaly=1.0, hot=False, cloud=0.1, lat=-1.0, lon=113.75):
    return RegionThermal(n_pixels=400, n_clear=round(400 * (1 - cloud)), cloud_fraction=cloud, n_peat=100,
                         n_peat_clear=round(100 * (1 - cloud)), background_k=2.0, background_mad_k=0.5,
                         max_anomaly_k=anomaly, max_z=anomaly / 0.5, n_hot=1 if hot else 0, n_hot_peat=1 if hot else 0,
                         hot_on_peat=True, hot_lat=lat, hot_lon=lon, mwir_k=300.0, lwir_k=297.0)


def everywhere(**kw):
    return {r.id: view(**kw, lat=r.lat, lon=r.lon) for r in REGIONS}


def observer(by_satellite, calls=None):
    def observe(name, slot, regions):
        if calls is not None:
            calls.append((name, slot))
        value = by_satellite[name]
        if isinstance(value, Exception):
            raise value
        return value
    return observe


def humid(value, calls=None):
    def fetch(points, at):
        if calls is not None:
            calls.append(at)
        return [value] * len(points)
    return fetch


def cycle(repo, views, now=NIGHT + timedelta(minutes=10), published=lambda name, slot: True, rh=25.0, **kw):
    """`now` 10 minutes after NIGHT: with every file published, the slot is NIGHT."""
    return run_geo_cycle(repo, REGIONS, now, published=published, observe=observer(views),
                         humidity=humid(rh), **kw)


# ---------- contextual anomaly ----------

def grid(seed=0):
    rng = np.random.default_rng(seed)
    lwir = np.full((20, 20), 298.0)
    mwir = lwir + 2.0 + rng.normal(0, 0.3, lwir.shape)        # a quiet surface: delta_bt about 2 K
    peat = np.zeros(lwir.shape, bool)
    peat[:, :10] = True                                        # the left half is peat
    lat, lon = np.meshgrid(np.linspace(-1.0, -1.5, 20), np.linspace(113.5, 114.0, 20), indexing="ij")
    return mwir, lwir, peat, lat, lon


def test_contextual_finds_a_small_hot_source_and_marks_peat():
    mwir, lwir, peat, lat, lon = grid()
    mwir[5, 5] = 320.0                                         # smouldering peat inside one pixel
    result = region_thermal(mwir, lwir, peat, lat, lon)
    assert result.n_hot == 1 and result.n_hot_peat == 1 and result.hot_on_peat is True
    assert result.max_anomaly_k == pytest.approx(20.0, abs=0.5) and result.max_z > 20
    assert (result.hot_lat, result.hot_lon) == (round(lat[5, 5], 4), round(lon[5, 5], 4))
    assert result.cloud_fraction == 0.0 and abs(result.background_k - 2.0) < 0.2
    mwir[5, 5], mwir[5, 15] = 300.0, 330.0                     # a fire beside the peat still counts, marked off peat
    beside = region_thermal(mwir, lwir, peat, lat, lon)
    assert beside.n_hot == 1 and beside.n_hot_peat == 0 and beside.hot_on_peat is False


def test_thin_cirrus_is_not_fire_like_and_clouds_are_counted():
    mwir, lwir, peat, lat, lon = grid()
    lwir[2:6, 2:6] -= 8.0                                      # thin cirrus: 10.4 um cools a lot, 3.9 um a little
    mwir[2:6, 2:6] -= 2.0                                      # delta_bt +6 K, but no 3.9 um rise
    lwir[14:, :] = 250.0                                       # cold cloud tops over 30 % of the cell
    result = region_thermal(mwir, lwir, peat, lat, lon)
    assert result.n_hot == 0 and result.max_anomaly_k < 3.0 and result.cloud_fraction == 0.3
    assert result.n_peat_clear == 140
    lwir[:, :] = 250.0                                         # all cloud: no clear background, no verdict
    empty = region_thermal(mwir, lwir, peat, lat, lon)
    assert empty.max_anomaly_k is None and empty.cloud_fraction == 1.0 and empty.n_hot == 0


def test_a_noisy_background_needs_a_significant_anomaly():
    mwir, lwir, peat, lat, lon = grid()
    lwir += np.random.default_rng(1).normal(0, 2.0, lwir.shape)   # delta_bt varies by about 2 K across the cell
    mwir[5, 5], lwir[5, 5] = 304.5, 298.0                      # 3.9 um rose 4.5 K, delta_bt only ~2 sigmas up
    result = region_thermal(mwir, lwir, peat, lat, lon)
    assert result.background_mad_k > 1.5 and result.n_hot == 0 and result.max_anomaly_k == 0.0


# ---------- fusion ----------

def test_both_satellites_hot_at_the_same_place_confirm_each_other():
    fused = fuse({"himawari9": view(9.0, True, lat=-1.0, lon=113.75), "gk2a": view(7.5, True, lat=-1.03, lon=113.77)},
                 night=True, slot=NIGHT, centre=CENTRE)
    assert fused.quality is Quality.ACCEPTED and "two_satellites_agree" in fused.flags
    assert fused.delta_t == 7.5 and fused.cloud_pct == 10.0          # the size both confirm; the best view's cloud


@pytest.mark.parametrize("views, night, flag", [
    ({"himawari9": view(9.0, True), "gk2a": view(8.0, True, lat=-1.5)}, True, "satellites_disagree"),   # 55 km apart
    ({"himawari9": view(9.0, True), "gk2a": view(1.0)}, True, "satellites_disagree"),
    ({"himawari9": None, "gk2a": view(9.0, True)}, True, "single_satellite:gk2a"),
    ({"himawari9": view(9.0, True), "gk2a": view(9.0, True)}, False, "daytime"),
])
def test_one_satellite_disagreement_or_daylight_degrade_the_view(views, night, flag):
    fused = fuse(views, night=night, slot=NIGHT, centre=CENTRE)
    assert fused.quality is Quality.DEGRADED and flag in fused.flags and fused.delta_t == 9.0


def test_no_clear_view_is_rejected():
    fused = fuse({"himawari9": view(9.0, True, cloud=0.8), "gk2a": None}, night=True, slot=NIGHT, centre=CENTRE)
    assert fused.quality is Quality.REJECTED and fused.delta_t is None and fused.cloud_pct == 80.0
    assert "no_clear_view" in fused.flags and "no_data:gk2a" in fused.flags


def hotspot(km_east=0.0, hours=0.0, confidence="n"):
    east = math.degrees(km_east / 6371.0) / math.cos(math.radians(1.0))
    return {"latitude": -1.0, "longitude": 113.75 + east, "confidence": confidence,
            "acq_ts": NIGHT + timedelta(hours=hours)}


def test_viirs_and_power_support_only_a_possible_hotspot():
    cfg = FusionConfig()
    assert viirs_support([hotspot(1.0, 1.0)], -1.0, 113.75, NIGHT, cfg) == 1.0
    assert viirs_support([hotspot(6.0, 1.0)], -1.0, 113.75, NIGHT, cfg) == 0.5
    assert viirs_support([hotspot(1.0, 1.0, "l"), hotspot(20.0)], -1.0, 113.75, NIGHT, cfg) is None
    recent = {"observed_at": "2026-10-09T18:00:00Z", "delta_t_k": 2.0}
    assert power_support(recent, NIGHT, cfg) == 0.5
    assert power_support({**recent, "observed_at": "2026-10-06T18:00:00Z"}, NIGHT, cfg) is None   # older than 4 days
    hot = fuse({"himawari9": view(9.0, True), "gk2a": view(8.0, True)}, night=True, slot=NIGHT, centre=CENTRE,
               hotspots=[hotspot(1.0, 1.0)], power=recent)
    assert hot.support == {"viirs_support": 1.0, "power_support": 0.5}
    quiet = fuse({"himawari9": view(1.0), "gk2a": view(1.2)}, night=True, slot=NIGHT, centre=CENTRE,
                 hotspots=[hotspot(1.0, 1.0)], power=recent)
    assert quiet.support == {} and "two_satellites_quiet" in quiet.flags


def test_night_follows_the_sun():
    assert solar_elevation_deg(NIGHT, -1.0, 113.75) < -6 < solar_elevation_deg(DAY, -1.0, 113.75)


# ---------- slot choice ----------

def test_pick_slot_waits_for_both_satellites_then_falls_back_to_one():
    now = datetime(2026, 10, 11, 18, 45, tzinfo=UTC)

    def latency(himawari, gk2a):
        delay = {"himawari9": himawari, "gk2a": gk2a}
        return lambda name, slot: delay[name] is not None and slot + timedelta(minutes=delay[name]) <= now

    assert pick_slot(now, None, latency(12, 24)) == (datetime(2026, 10, 11, 18, 20, tzinfo=UTC), ["himawari9", "gk2a"])
    assert pick_slot(now, datetime(2026, 10, 11, 18, 20, tzinfo=UTC), latency(12, 24)) == (None, [])
    gk2a_down = latency(12, None)
    assert pick_slot(now, None, gk2a_down) == (datetime(2026, 10, 11, 17, 50, tzinfo=UTC), ["himawari9"])
    assert pick_slot(now, datetime(2026, 10, 11, 17, 50, tzinfo=UTC), gk2a_down)[0] == datetime(2026, 10, 11, 18, 0, tzinfo=UTC)
    assert pick_slot(now, None, latency(None, None)) == (None, [])


# ---------- the cycle ----------

def test_night_hotspot_both_satellites_see_enters_alert_and_requests_the_agent():
    repo = FakeRepo()
    summary = cycle(repo, {"himawari9": everywhere(anomaly=9.0, hot=True), "gk2a": everywhere(anomaly=8.5, hot=True)})
    rid = REGIONS[0].id
    assert summary["slot"] == "2026-10-11T18:10:00Z" and summary["statuses"] == {"ALERT": 3}
    assert summary["satellites"] == {"himawari9": "ok", "gk2a": "ok"}
    doc = repo.docs[rid]
    assert doc["status"] == "ALERT" and doc["observed_at"] == "2026-10-11T18:10:00Z"
    assert doc["last_agent_at"] == "2026-10-11T18:20:00Z" and repo.meta_doc["geo_slot"] == "2026-10-11T18:10:00Z"
    record = repo.slots[rid][-1]
    assert record == doc["latest"] and record["night"] is True and record["inputs"]["delta_t_k"] == 8.5
    assert record["quality"] == {"thermal": "ACCEPTED", "relative_humidity": "ACCEPTED"}
    assert record["views"]["gk2a"]["max_anomaly_k"] == 8.5 and "two_satellites_agree" in record["flags"]
    assert repo.transitions[rid][0]["current"] == "ALERT"
    assert repo.meta_doc["agent_pending"][rid] == {"observed_at": "2026-10-11T18:10:00Z",
                                                   "requested_at": "2026-10-11T18:20:00Z"}


def test_daytime_and_a_single_satellite_are_capped_at_warning():
    repo = FakeRepo()
    hot = {"himawari9": everywhere(anomaly=9.0, hot=True), "gk2a": everywhere(anomaly=9.0, hot=True)}
    cycle(repo, hot, now=DAY + timedelta(minutes=10))
    record = repo.docs[REGIONS[0].id]["latest"]
    assert record["status"] == "WARNING" and record["utilities"]["ALERT"] == 1.0
    assert "capped at WARNING (daytime)" in record["reason"] and record["quality"]["thermal"] == "DEGRADED"
    single = FakeRepo()
    cycle(single, {"himawari9": everywhere(anomaly=9.0, hot=True), "gk2a": None},
          published=lambda name, slot: name == "himawari9")
    record = single.docs[REGIONS[0].id]["latest"]
    assert record["status"] == "WARNING" and "single satellite himawari9" in record["reason"]
    assert single.meta_doc.get("agent_pending") is None


def test_cloud_makes_regions_not_observable_with_a_reason():
    repo = FakeRepo()
    cycle(repo, {"himawari9": everywhere(cloud=0.9), "gk2a": everywhere(cloud=0.95)})
    doc = repo.docs[REGIONS[0].id]
    assert doc["status"] == "NO_OBSERVABLE" and doc["utilities"] == {}
    assert doc["latest"]["reason"] == "not observable: cloud cover 90% > 60%"


def test_firms_is_asked_once_and_only_when_a_hotspot_is_possible():
    quiet = {"himawari9": everywhere(), "gk2a": everywhere()}
    cycle(FakeRepo(), quiet, hotspots=lambda *a: pytest.fail("no possible hotspot: FIRMS must not be asked"))
    calls = []

    def hotspots(bbox, start, end):
        calls.append((bbox, start, end))
        return [{"latitude": r.lat, "longitude": r.lon, "confidence": "h", "acq_ts": NIGHT} for r in REGIONS]

    warm = {"himawari9": everywhere(anomaly=6.8, hot=True), "gk2a": everywhere(anomaly=6.8, hot=True)}
    alone = FakeRepo()
    cycle(alone, warm, rh=35.0, hotspots=lambda *a: [])
    assert alone.docs[REGIONS[0].id]["latest"]["utilities"] == {"SAFE": 0.0, "WARNING": 0.6, "ALERT": 0.44}
    repo = FakeRepo()
    repo.docs[REGIONS[0].id] = {"id": REGIONS[0].id, "power": {"observed_at": "2026-10-10T18:00:00Z", "delta_t_k": 3.2}}
    cycle(repo, warm, rh=35.0, hotspots=hotspots)
    assert len(calls) == 1 and calls[0][1] == datetime(2026, 10, 11, 6, 10, tzinfo=UTC)   # one request, 12 h back
    record = repo.docs[REGIONS[0].id]["latest"]
    assert record["support"] == {"viirs_support": 1.0, "power_support": 1.0}
    assert record["utilities"]["ALERT"] == 0.5692 and record["status"] == "ALERT"   # within the safety margin
    assert repo.docs[REGIONS[1].id]["latest"]["status"] == "WARNING"               # VIIRS alone lifts it less


def test_humidity_is_asked_once_an_hour_and_is_optional():
    repo, calls = FakeRepo(), []
    views = {"himawari9": everywhere(), "gk2a": everywhere()}
    for minutes in (10, 20):
        run_geo_cycle(repo, REGIONS, NIGHT + timedelta(minutes=minutes), published=lambda n, s: True,
                      observe=observer(views), humidity=humid(80.0, calls))
    assert len(calls) == 1 and repo.meta_doc["humidity"]["values"][REGIONS[0].id] == 80.0

    def down(points, at):
        raise httpx.ConnectError("offline")

    summary = run_geo_cycle(FakeRepo(), REGIONS, NIGHT + timedelta(minutes=10), published=lambda n, s: True,
                            observe=observer(views), humidity=down)
    assert {"region": None, "error": "humidity:ConnectError"} in summary["errors"]
    assert summary["statuses"] == {"SAFE": 3}


def test_a_broken_satellite_leaves_the_other_one_and_nothing_is_invented_without_both():
    repo = FakeRepo()
    summary = cycle(repo, {"himawari9": everywhere(anomaly=9.0, hot=True), "gk2a": OSError("corrupt file")})
    assert summary["satellites"] == {"himawari9": "ok", "gk2a": "error:OSError"}
    assert "single_satellite:himawari9" in repo.docs[REGIONS[0].id]["latest"]["flags"]
    blind = FakeRepo()
    summary = cycle(blind, {"himawari9": OSError("x"), "gk2a": OSError("y")})
    assert summary["skipped"] == "no satellite could be read" and blind.docs == {}
    assert blind.meta_doc["geo_slot"] == "2026-10-11T18:10:00Z"   # tried once; the next slot comes in 10 minutes


def test_a_processed_slot_is_not_read_again():
    repo, calls = FakeRepo(), []
    views = {"himawari9": everywhere(), "gk2a": everywhere()}
    for _ in range(2):
        summary = run_geo_cycle(repo, REGIONS, NIGHT + timedelta(minutes=10), published=lambda n, s: True,
                                observe=observer(views, calls), humidity=humid(80.0))
    assert summary["skipped"] == "no new slot published yet" and len(calls) == 2   # one read per satellite


def test_unreachable_buckets_skip_the_cycle_without_marking_a_slot():
    def down(name, slot):
        raise httpx.ConnectError("offline")

    repo = FakeRepo()
    summary = run_geo_cycle(repo, REGIONS, NIGHT + timedelta(minutes=10), published=down, observe=observer({}),
                            humidity=humid(80.0))
    assert summary["skipped"] == "satellite buckets unreachable" and "geo_slot" not in repo.meta_doc
    assert summary["errors"] == [{"region": None, "error": "buckets:ConnectError"}]


def test_power_support_comes_from_the_region_document():
    repo = FakeRepo()
    repo.docs[REGIONS[0].id] = {"id": REGIONS[0].id,
                                "power": {"observed_at": "2026-10-09T18:00:00Z", "delta_t_k": 3.5}}
    cycle(repo, {"himawari9": everywhere(anomaly=9.0, hot=True), "gk2a": everywhere(anomaly=9.0, hot=True)})
    doc = repo.docs[REGIONS[0].id]
    assert doc["latest"]["support"] == {"power_support": 1.0} and doc["power"]["delta_t_k"] == 3.5


def test_waiting_agent_runs_go_first_and_failures_are_not_retried():
    repo = FakeRepo()
    hot = {"himawari9": everywhere(anomaly=9.0, hot=True), "gk2a": everywhere(anomaly=9.0, hot=True)}
    cycle(repo, hot)                                              # no verifier configured: requests wait in meta
    assert len(repo.meta_doc["agent_pending"]) == 3
    seen = []

    def verify(region, request):
        seen.append(region.id)
        if region.id == REGIONS[1].id:
            raise RuntimeError("quota")
        return {"validation_status": "NEEDS_MANUAL_REVIEW"}

    later = cycle(repo, hot, now=NIGHT + timedelta(minutes=30), verify=verify)   # the 18:20 slot
    assert seen == [r.id for r in REGIONS] and repo.meta_doc["agent_pending"] == {}
    assert {"region": REGIONS[1].id, "error": "RuntimeError"} in later["errors"]
    assert later["agent_requests"] == []                          # still ALERT: no second request


def test_agent_runs_wait_when_the_time_budget_is_spent():
    repo, ticks = FakeRepo(), iter(range(0, 10_000, 30))           # every clock read advances 30 s
    hot = {"himawari9": everywhere(anomaly=9.0, hot=True), "gk2a": everywhere(anomaly=9.0, hot=True)}
    summary = cycle(repo, hot, budget_s=60, clock=lambda: next(ticks),
                    verify=lambda region, request: pytest.fail("no time left for the agent"))
    assert len(summary["agent_requests"]) == 3 and summary["verified"] == []
    assert len(repo.meta_doc["agent_pending"]) == 3


# ---------- adapters, offline ----------

def test_bucket_urls_and_slots():
    slot = satellites.slot_floor(datetime(2026, 10, 11, 18, 17, 42, tzinfo=UTC))
    assert slot == NIGHT
    hima = satellites.SATELLITES["himawari9"].urls(slot)
    assert hima[0] == ("https://noaa-himawari9.s3.amazonaws.com/AHI-L1b-FLDK/2026/10/11/1810/"
                       "HS_H09_20261011_1810_B07_FLDK_R20_S0510.DAT.bz2") and len(hima) == 4
    assert satellites.SATELLITES["gk2a"].urls(slot)[1] == (
        "https://noaa-gk2a-pds.s3.amazonaws.com/AMI/L1B/FD/202610/11/18/gk2a_ami_le1b_ir105_fd020ge_202610111810.nc")
    seen = []

    def handler(request):
        seen.append(request.method)
        return httpx.Response(200 if "B07" in request.url.path else 404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert satellites.published("himawari9", slot, client) is False and seen == ["HEAD"] * 3   # stops at B13


def test_open_meteo_humidity_for_many_points_in_one_request():
    def handler(request):
        assert request.url.params["latitude"] == "-1.0,-2.0"
        hourly = lambda rh: {"time": ["2026-10-11T17:00", "2026-10-11T18:00"], "relative_humidity_2m": rh}
        return httpx.Response(200, json=[{"hourly": hourly([70, 65])}, {"hourly": hourly([60, 120])}])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert openmeteo.fetch_humidity([(-1.0, 113.0), (-2.0, 113.0)], NIGHT, client) == [65.0, None]


def test_firms_area_hotspots_merge_sources_and_keep_the_window(monkeypatch):
    rows = {"A": [{"latitude": "-1", "longitude": "113", "acq_date": "2026-10-11", "acq_time": "1815",
                   "satellite": "N20", "confidence": "n", "bright_ti4": "330", "frp": "5", "daynight": "N"}],
            "B": [{"latitude": "-1", "longitude": "113", "acq_date": "2026-10-10", "acq_time": "0100",
                   "satellite": "N21", "confidence": "h", "bright_ti4": "340", "frp": "9", "daynight": "N"}]}
    asked = []

    def fetch(key, source, bbox, day_range, date, client=None):
        asked.append((source, day_range, date))
        return rows[source]

    monkeypatch.setattr(firms, "fetch_firms", fetch)
    found = firms.area_hotspots("KEY", ["A", "B"], (100, -5, 120, 5), NIGHT - timedelta(hours=12), NIGHT + timedelta(hours=1))
    assert asked == [("A", 1, "2026-10-11"), ("B", 1, "2026-10-11")] and [h["satellite"] for h in found] == ["N20"]
