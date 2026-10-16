"""Open-Meteo adapter (public API, no key): relative humidity of the current hour for every region, in one request.

Values come from weather models, so the slot records flag them as `model_estimate`. Used because NASA POWER humidity is
2-3 days late; cloud now comes from the satellites' own long-wave channel (contextual.py).
Attribution: weather data by Open-Meteo.com (CC BY 4.0).
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

URL = "https://api.open-meteo.com/v1/forecast"
SOURCE = "open_meteo"


def fetch_humidity(points: list[tuple[float, float]], at: datetime, client: httpx.Client | None = None
                   ) -> list[float | None]:
    """Relative humidity at 2 m for the UTC hour of `at`, one value per (lat, lon) point, in request order.

    Open-Meteo counts every location as one call (free tier: 10,000 a day), so the caller asks once per hour.
    """
    params = {"latitude": ",".join(str(lat) for lat, _ in points),
              "longitude": ",".join(str(lon) for _, lon in points),
              "hourly": "relative_humidity_2m", "timezone": "UTC", "past_days": 1, "forecast_days": 1}
    response = (client or httpx).get(URL, params=params, timeout=60)
    response.raise_for_status()
    data = response.json()
    stamp = f"{at.astimezone(timezone.utc):%Y-%m-%dT%H}:00"
    values = []
    for item in data if isinstance(data, list) else [data]:   # a list when more than one location is asked
        value = dict(zip(item["hourly"]["time"], item["hourly"]["relative_humidity_2m"])).get(stamp)
        values.append(float(value) if value is not None and 0 <= value <= 100 else None)
    return values


def fetch_weather(lat: float, lon: float, at: datetime, client: httpx.Client | None = None, timeout: float = 8
                  ) -> dict:
    """Weather at one point (the hot pixel) for the verification agent: humidity, wind and the last 24 hours of rain.

    Model values for the model cell around the point (about 9-25 km), not a station: context for the visual check
    (which way smoke would drift, whether it just rained), never an input of the rules.
    """
    params = {"latitude": lat, "longitude": lon, "timezone": "UTC", "past_days": 2, "forecast_days": 1,
              "hourly": "relative_humidity_2m,wind_speed_10m,wind_direction_10m,precipitation",
              "wind_speed_unit": "ms"}
    response = (client or httpx).get(URL, params=params, timeout=timeout)
    response.raise_for_status()
    hourly = response.json()["hourly"]
    stamp = f"{at.astimezone(timezone.utc):%Y-%m-%dT%H}:00"
    if stamp not in hourly["time"]:
        return {"source": SOURCE, "at": None}
    i = hourly["time"].index(stamp)
    rain = [v for v in hourly["precipitation"][max(0, i - 23):i + 1] if v is not None]
    return {"source": SOURCE, "at": stamp + "Z", "relative_humidity_pct": hourly["relative_humidity_2m"][i],
            "wind_speed_ms": hourly["wind_speed_10m"][i], "wind_from_deg": hourly["wind_direction_10m"][i],
            "precipitation_24h_mm": round(sum(rain), 1) if rain else None}
