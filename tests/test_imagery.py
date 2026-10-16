"""The verification agent's visual inputs: NASA GIBS imagery and the weather at the hot pixel, all offline."""
import time
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from asapify_engine import imagery, openmeteo, verifier
from test_verifier import RECORD, REGION, SETTINGS, TOOL, YAML_OK

UTC = timezone.utc
REAL_FETCH, REAL_WEATHER = imagery.fetch, openmeteo.fetch_weather   # conftest swaps them for offline fakes
JPEG = b"\xff\xd8\xff\xe0" + bytes(imagery.MIN_BYTES)   # as large as a picture with something in it


class Answer:
    def __init__(self, content=b"", mime="image/jpeg", status=200, payload=None):
        self.content, self.headers, self.status_code, self.payload = content, {"content-type": mime}, status, payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)

    def json(self):
        return self.payload


class Client:
    def __init__(self, answer):
        self.answer, self.urls = answer, []

    def get(self, url, **kw):
        self.urls.append((url, kw))
        return self.answer(url) if callable(self.answer) else self.answer


# ---------- imagery ----------

@pytest.mark.parametrize("at, day, hours", [
    (datetime(2026, 10, 11, 18, 10, tzinfo=UTC), date(2026, 10, 11), 12.2),   # 01:45 solar: the afternoon before
    (datetime(2026, 10, 11, 6, 0, tzinfo=UTC), date(2026, 10, 10), 24.1),     # 13:35 solar: today's pass not served yet
    (datetime(2026, 10, 11, 10, 0, tzinfo=UTC), date(2026, 10, 11), 4.1),     # 17:35 solar: today's pass
])
def test_the_image_is_the_last_daytime_pass_gibs_can_serve(at, day, hours):
    assert imagery.overpass_date(at, 113.75) == (day, hours)


def test_the_request_draws_the_hotspots_on_the_region_and_a_buffer():
    query = parse_qs(urlsplit(imagery.wms_url("TrueColor", date(2026, 10, 11), REGION.bounds)).query)
    assert query["LAYERS"] == [f"TrueColor,{imagery.OVERLAY}"] and query["TIME"] == ["2026-10-11"]
    assert query["BBOX"] == ["-2.8500,113.3375,-2.1500,114.1625"]   # WMS 1.3.0 in EPSG:4326: latitude first
    assert query["WIDTH"] == ["768"] and query["HEIGHT"] == ["652"] and query["FORMAT"] == ["image/jpeg"]


def test_fetch_keeps_the_images_and_names_what_failed():
    def answer(url):   # GIBS answers a bad layer with an XML exception and status 200
        return Answer(JPEG) if "TrueColor" in url else Answer(b"<ServiceException/>", mime="text/xml")
    client = Client(answer)
    got = REAL_FETCH(REGION.bounds, REGION.lon, datetime(2026, 10, 11, 18, 10, tzinfo=UTC), client=client)
    assert got["date"] == "2026-10-11" and got["hours_before_observation"] == 12.2
    assert [p.view for p in got["pictures"]] == ["true_color"] and got["pictures"][0].data == JPEG
    assert got["errors"] == ["false_color_swir: not an image (text/xml)"]
    assert all(kw["timeout"] == imagery.TIMEOUT_S for _, kw in client.urls)


def test_a_blank_picture_is_left_out_and_the_type_is_read_in_any_case():
    def answer(url):   # no data yet renders one flat colour: a tiny JPEG that would read as "clear, nothing there"
        return Answer(JPEG, mime="Image/JPEG; charset=binary") if "TrueColor" in url else Answer(b"\xff\xd8 black")
    got = REAL_FETCH(REGION.bounds, REGION.lon, datetime(2026, 10, 11, 18, 10, tzinfo=UTC), client=Client(answer))
    assert [p.view for p in got["pictures"]] == ["true_color"] and got["pictures"][0].mime == "image/jpeg"
    assert got["errors"] == ["false_color_swir: blank image (8 bytes)"]


def test_fetch_never_raises():
    got = REAL_FETCH(REGION.bounds, REGION.lon, datetime(2026, 10, 11, 18, 10, tzinfo=UTC),
                     client=Client(Answer(status=503)))
    assert got["pictures"] == [] and len(got["errors"]) == 2


# ---------- weather at the hot pixel ----------

def test_weather_gives_wind_humidity_and_the_last_day_of_rain():
    times = [f"2026-10-11T{h:02d}:00" for h in range(24)] + [f"2026-10-12T{h:02d}:00" for h in range(24)]
    payload = {"hourly": {"time": times, "relative_humidity_2m": [70] * 48, "wind_speed_10m": [3.2] * 48,
                          "wind_direction_10m": [135] * 48, "precipitation": [0.5] * 10 + [0.0] * 38}}
    client = Client(Answer(payload=payload))
    got = REAL_WEATHER(-2.51, 113.8, datetime(2026, 10, 11, 18, 10, tzinfo=UTC), client=client)
    assert got == {"source": "open_meteo", "at": "2026-10-11T18:00Z", "relative_humidity_pct": 70,
                   "wind_speed_ms": 3.2, "wind_from_deg": 135, "precipitation_24h_mm": 5.0}
    assert client.urls[0][1]["params"]["latitude"] == -2.51   # the hot pixel, not the region centre


# ---------- the visual check in a verification ----------

# a night slot (01:45 solar) and the afternoon pass before it, as imagery.overpass_date gives them
NIGHT_RECORD = {**RECORD, "observed_at": "2026-10-10T18:10:00Z"}
DAY, HOURS = imagery.overpass_date(datetime(2026, 10, 10, 18, 10, tzinfo=UTC), REGION.lon)   # 2026-10-10, 12.2 h
PICTURE = imagery.Picture("true_color", f"T,{imagery.OVERLAY}", "https://gibs.example/true.jpg", "image/jpeg", JPEG)
SWIR = imagery.Picture("false_color_swir", f"S,{imagery.OVERLAY}", "https://gibs.example/swir.jpg", "image/jpeg", JPEG)
SHOWN = {"date": DAY.isoformat(), "hours_before_observation": HOURS, "pictures": [PICTURE, SWIR], "errors": []}
WEATHER = {"source": "open_meteo", "at": "2026-10-10T18:00Z", "wind_from_deg": 135, "precipitation_24h_mm": 0.0}
# the same FIRMS answer, its confident hotspot before the afternoon pass (already burning) or after it (started later)
EARLY = {**TOOL, "hotspots": [{**TOOL["hotspots"][0], "hours_from_observation": -13.0}]}
LATE = {**TOOL, "hotspots": [{**TOOL["hotspots"][0], "hours_from_observation": -0.3}]}


def visual(smoke="visible", scar="not_visible", day=DAY.isoformat(), status="CONFIRMED", notes=" A grey plume."):
    return YAML_OK.replace("validation_status: CONFIRMED", f"validation_status: {status}").replace(
        "draft_operator_alert:", f"""visual_evidence:
  smoke: {smoke}
  burn_scar: {scar}
  image_date: {day}
  notes:{notes}
draft_operator_alert:""")


def run_with(text, seen, results):
    def run(payload, state, settings, pictures=()):
        seen.update(payload=payload, pictures=list(pictures))
        return text, results, {"model_calls": 2}
    return run


def verify(text, shown=SHOWN, seen=None, results=(EARLY,)):
    seen = {} if seen is None else seen
    return verifier.analyze_region(REGION, NIGHT_RECORD, datetime(2026, 10, 13, 6, tzinfo=UTC), trigger="auto",
                                   settings=SETTINGS, run=run_with(text, seen, list(results)),
                                   look=lambda *a: shown, weather=lambda *a: WEATHER)


def test_the_model_gets_the_images_and_the_weather_and_the_record_keeps_the_urls():
    seen = {}
    record = verify(visual(), seen=seen)
    assert record["validation_status"] == "CONFIRMED" and record["checks"] == []
    assert seen["pictures"] == [PICTURE, SWIR]
    assert "images_attached_in_order" in seen["payload"] and "wind_from_deg: 135" in seen["payload"]
    assert record["imagery"] == {"date": "2026-10-10", "hours_before_observation": 12.2,
                                 "images": [{"view": "true_color", "url": "https://gibs.example/true.jpg"},
                                            {"view": "false_color_swir", "url": "https://gibs.example/swir.jpg"}],
                                 "errors": []}
    assert record["visual_evidence"]["smoke"] == "visible" and record["weather"] == WEATHER


CLEAR = "clear imagery within 24 h, after confident hotspots, shows neither smoke nor a burn scar"


@pytest.mark.parametrize("text, problem", [
    (YAML_OK, "visual evidence missing although images were shown"),
    (visual(day="2026-10-08"), "image_date matches no image shown"),
    (visual(smoke="not_visible"), CLEAR),
    (visual(smoke="no_image"), "no_image reported although the image was shown"),
    (visual(scar="no_image"), "no_image reported although the image was shown"),
])
def test_a_visual_report_that_does_not_fit_the_images_goes_to_manual_review(text, problem):
    record = verify(text)
    assert record["validation_status"] == "NEEDS_MANUAL_REVIEW" and problem in record["checks"]
    assert record["agent_validation_status"] == "CONFIRMED"


def test_cloud_over_the_place_says_nothing_and_does_not_block_confirmed():
    assert verify(visual(smoke="obscured", scar="obscured"))["validation_status"] == "CONFIRMED"


def test_an_image_from_before_the_hotspots_does_not_contradict_them():
    """The afternoon image cannot show a fire first detected that evening."""
    record = verify(visual(smoke="not_visible"), results=(LATE,))
    assert record["validation_status"] == "CONFIRMED" and record["checks"] == []


def test_one_view_alone_does_not_contradict_confirmed():
    """Smoke barely shows in the SWIR view and scars are harder in true colour: both must have been looked at."""
    swir_only = {**SHOWN, "pictures": [SWIR]}
    assert verify(visual(smoke="no_image", scar="not_visible"), shown=swir_only)["validation_status"] == "CONFIRMED"
    assert verify(visual(smoke="not_visible"), shown={**SHOWN, "pictures": [PICTURE]})["checks"] == []


def test_a_stale_image_says_nothing():
    old = {**SHOWN, "hours_before_observation": 24.1}
    assert verify(visual(smoke="not_visible"), shown=old)["validation_status"] == "CONFIRMED"


@pytest.mark.parametrize("text", [YAML_OK.replace("CONFIRMED", "REJECTED"), visual(day="2026-10-08", status="REJECTED"),
                                  visual(smoke="not_visible", status="REJECTED")])
def test_the_visual_report_never_turns_a_rejection_into_a_manual_review(text):
    """Only CONFIRMED is held back by the images; a REJECTED stays rejected, so the operator cannot publish it."""
    record = verify(text)
    assert record["validation_status"] == "REJECTED" and record["checks"] == []


@pytest.mark.parametrize("notes, day", [("", DAY.isoformat()), (" null", DAY.isoformat()),
                                        (" A plume.", f"{DAY.isoformat()}T05:30:00Z")])
def test_a_blank_note_or_a_timestamp_still_parses(notes, day):
    verdict = verifier.parse_verdict(visual(notes=notes, day=day))
    assert verdict.visual_evidence.image_date == DAY.isoformat() and isinstance(verdict.visual_evidence.notes, str)


def test_slow_imagery_or_weather_cannot_eat_the_model_time(monkeypatch):
    monkeypatch.setattr(verifier, "GATHER_S", 0.2)

    def slow(*args):
        time.sleep(2)
        return SHOWN
    started = time.monotonic()
    shown, weather = verifier.gather(REGION, NIGHT_RECORD, slow, lambda *a: WEATHER)
    assert time.monotonic() - started < 1.0
    assert shown == {"error": "Timeout"} and weather == WEATHER


def test_without_images_the_model_must_not_claim_to_see_anything():
    blind = {"date": "2026-10-09", "hours_before_observation": 16.0, "pictures": [], "errors": ["true_color: 503"]}
    assert verify(YAML_OK, shown=blind)["validation_status"] == "CONFIRMED"
    claimed = verify(visual(), shown=blind)
    assert "visual evidence reported without an image" in claimed["checks"]
    assert verify(visual("no_image", "no_image", "2026-10-09"), shown=blind)["validation_status"] == "CONFIRMED"
