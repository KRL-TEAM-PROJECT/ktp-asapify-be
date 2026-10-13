"""NASA POWER adapter (public API, no key): hourly skin temperature and humidity -> canonical observations.

POWER serves MERRA-2 history and GEOS-IT for recent days (about 2-3 days behind real time) at
0.5° x 0.625°; one point request returns the grid cell that contains the point. POWER's cloud amount
(CERES) is published months late, so cloud cover comes from the Open-Meteo adapter instead.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import httpx

from .canonical import Observation, Quality

URL = "https://power.larc.nasa.gov/api/temporal/hourly/point"
SOURCE = "nasa_power"
RESOLUTION = "0.5deg x 0.625deg"
# POWER parameter -> (canonical parameter, canonical unit, measurement type)
PARAMETERS = {
    "TS": ("skin_temperature", "K", "earth_skin_temperature"),
    "RH2M": ("relative_humidity", "%", "relative_humidity_2m"),
}


def fetch(lat: float, lon: float, start: date, end: date, client: httpx.Client | None = None) -> dict:
    params = {"parameters": ",".join(PARAMETERS), "community": "AG", "latitude": lat, "longitude": lon,
              "start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
              "format": "JSON", "time-standard": "UTC"}
    response = (client or httpx).get(URL, params=params, timeout=120)
    response.raise_for_status()
    return response.json()


def canonicalize(raw: dict, ingested_at: datetime) -> list[Observation]:
    lon, lat = raw["geometry"]["coordinates"][:2]
    header = raw.get("header", {})
    fill = header.get("fill_value", -999.0)
    out: list[Observation] = []
    for code, series in raw["properties"]["parameter"].items():
        if code not in PARAMETERS:
            continue
        name, unit, kind = PARAMETERS[code]
        for stamp, value in series.items():
            flags = ["reanalysis_estimate"]  # modelled values, not a direct measurement
            if value is None or value <= fill:
                normalised, quality = None, Quality.REJECTED
                flags.append("missing_value")  # also POWER's placeholder for hours not published yet
            elif code == "TS":
                normalised, quality = round(value + 273.15, 2), Quality.ACCEPTED  # °C -> K
            elif 0 <= value <= 100:
                normalised, quality = float(value), Quality.ACCEPTED
            else:
                normalised, quality = None, Quality.REJECTED
                flags.append("out_of_range")
            out.append(Observation(
                source=SOURCE, parameter=name, value=normalised, unit=unit, measurement_type=kind,
                observed_at=datetime.strptime(stamp, "%Y%m%d%H").replace(tzinfo=timezone.utc),
                ingested_at=ingested_at, spatial_reference=f"EPSG:4326 point {lat},{lon}",
                spatial_resolution=RESOLUTION, quality=quality, quality_flags=tuple(flags),
                source_metadata={"power_parameter": code, "power_sources": header.get("sources", [])}))
    return out
