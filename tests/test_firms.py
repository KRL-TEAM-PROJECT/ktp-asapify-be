from datetime import datetime, timezone

import httpx
import pytest

from asapify_engine import firms

UTC = timezone.utc
HEADER = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight"
CSV = "\n".join([
    HEADER,
    "-2.51,113.80,330.5,0.4,0.4,2026-10-09,0542,N20,VIIRS,n,2.0NRT,295.1,12.3,D",
    "-2.51,113.80,330.5,0.4,0.4,2026-10-09,0542,N20,VIIRS,n,2.0NRT,295.1,12.3,D",   # same detection, second request
    "-2.60,113.90,301.2,0.4,0.4,2026-10-09,1800,N21,VIIRS,l,2.0NRT,290.0,1.1,N",
]) + "\n"


def client(text, status=200, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(str(request.url))
        return httpx.Response(status, text=text)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_builds_the_area_url_and_reads_rows():
    seen = []
    rows = firms.fetch_firms("KEY", "VIIRS_NOAA20_NRT", (113.4, -2.75, 114.1, -2.25), 2, "2026-10-09",
                             client=client(CSV, seen=seen))
    assert seen == ["https://firms.modaps.eosdis.nasa.gov/api/area/csv/KEY/VIIRS_NOAA20_NRT/"
                    "113.4,-2.75,114.1,-2.25/2/2026-10-09"]
    assert len(rows) == 3 and rows[0]["bright_ti4"] == "330.5"


@pytest.mark.parametrize("text, status", [("Invalid MAP_KEY.", 200), ("server error", 500)])
def test_errors_never_carry_the_key(text, status):
    with pytest.raises(ValueError) as err:
        firms.fetch_firms("SECRET-KEY", "VIIRS_NOAA20_NRT", (1, 2, 3, 4), client=client(text, status))
    assert "SECRET-KEY" not in str(err.value)


def test_normalize_dedupes_and_summarize_trims_for_the_agent():
    hotspots = firms.normalize(firms.fetch_firms("KEY", "VIIRS_NOAA20_NRT", (1, 2, 3, 4), client=client(CSV)))
    assert len(hotspots) == 2 and hotspots[0]["acq_ts"] == datetime(2026, 10, 9, 5, 42, tzinfo=UTC)
    summary = firms.summarize(hotspots, (-2.5, 113.75), datetime(2026, 10, 9, 6, tzinfo=UTC), limit=1)
    assert summary["hotspot_count"] == 2 and summary["confident_count"] == 1
    assert summary["highest_brightness_temp_k"] == 330.5 and summary["confidence_counts"] == {"n": 1, "l": 1}
    top, = summary["hotspots"]   # trimmed to the strongest by fire radiative power
    assert top["frp_mw"] == 12.3 and top["hours_from_observation"] == -0.3 and 5 < top["distance_km"] < 7
