"""Open-Meteo adapter (public API, no key): hourly total cloud cover -> canonical observations.

Values come from weather models, not from a satellite cloud mask, so every observation carries the
`model_estimate` flag. Used because NASA POWER cloud amount is published months late.
Attribution: weather data by Open-Meteo.com (CC BY 4.0).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import httpx

from .canonical import Observation, Quality

URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
SOURCE = "open_meteo"
RESOLUTION = "best-match model grid"


def fetch(lat: float, lon: float, start: date, end: date, client: httpx.Client | None = None) -> dict:
    params = {"latitude": lat, "longitude": lon, "start_date": start.isoformat(), "end_date": end.isoformat(),
              "hourly": "cloud_cover", "timezone": "UTC"}
    response = (client or httpx).get(URL, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def canonicalize(raw: dict, ingested_at: datetime) -> list[Observation]:
    lat, lon = raw["latitude"], raw["longitude"]
    hourly = raw["hourly"]
    out: list[Observation] = []
    for stamp, value in zip(hourly["time"], hourly["cloud_cover"]):
        flags = ["model_estimate"]
        if value is None:
            normalised, quality = None, Quality.REJECTED
            flags.append("missing_value")
        elif 0 <= value <= 100:
            normalised, quality = float(value), Quality.ACCEPTED
        else:
            normalised, quality = None, Quality.REJECTED
            flags.append("out_of_range")
        out.append(Observation(
            source=SOURCE, parameter="cloud_cover", value=normalised, unit="%", measurement_type="total_cloud_cover",
            observed_at=datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc), ingested_at=ingested_at,
            spatial_reference=f"EPSG:4326 point {lat},{lon}", spatial_resolution=RESOLUTION,
            quality=quality, quality_flags=tuple(flags), source_metadata={"open_meteo_variable": "cloud_cover"}))
    return out
