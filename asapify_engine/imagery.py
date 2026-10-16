"""NASA GIBS imagery of a region for the verification agent's visual check.

GIBS (Global Imagery Browse Services) serves NASA's daily satellite imagery over WMS, public and without a key. The
agent gets two views of the region plus a buffer, each with the VIIRS thermal anomalies drawn on top so it knows
where to look:
- true colour (VIIRS NOAA-20 corrected reflectance): smoke plumes;
- false colour M11-I2-I1 (shortwave infrared): burn scars and active fire, and it sees through thin smoke.
Both come from the daytime overpass (about 13:30 local solar time), so a night observation is checked against the
afternoon before it and the gap is always reported. The URLs are stored with the verification, so the operator sees
the same picture the model saw. Layer names follow the GIBS catalogue (https://nasa-gibs.github.io/gibs-api-docs/);
check them there if GIBS renames a layer. Network access goes through an injectable client, so tests stay offline.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlencode

import httpx

WMS = "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi"
OVERLAY = "VIIRS_NOAA20_Thermal_Anomalies_375m_All"
VIEWS = (("true_color", "VIIRS_NOAA20_CorrectedReflectance_TrueColor"),
         ("false_color_swir", "VIIRS_NOAA20_CorrectedReflectance_BandsM11-I2-I1"))
OVERPASS = time(13, 30)     # local solar time of the NOAA-20 daytime pass
READY_H = 3.0               # GIBS publishes a pass about 3 hours after it
BUFFER_DEG = 0.1            # about 11 km around the region cell
WIDTH = 768                 # pixels; about 100 m each across a region cell
TIMEOUT_S = 8               # per image; both are fetched at once, inside the verification's time budget


@dataclass(frozen=True)
class Picture:
    view: str
    layers: str
    url: str
    mime: str
    data: bytes


def overpass_date(observed_at: datetime, lon: float) -> tuple[date, float]:
    """The latest daytime overpass GIBS can serve at the observation, and how many hours before the observation it was.

    Uses local solar time (UTC + longitude / 15 h): Sumatra and Kalimantan cross 95-120 E, about 6-8 hours ahead.
    """
    solar = observed_at.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(hours=lon / 15)
    day = solar.date()
    if solar < datetime.combine(day, OVERPASS) + timedelta(hours=READY_H):
        day -= timedelta(days=1)
    return day, round((solar - datetime.combine(day, OVERPASS)).total_seconds() / 3600, 1)


def wms_url(layer: str, day: date, bounds: tuple[float, float, float, float]) -> str:
    """One GetMap request: the layer with the VIIRS hotspots on top, the region bounds plus a buffer."""
    west, south, east, north = (bounds[0] - BUFFER_DEG, bounds[1] - BUFFER_DEG,
                                bounds[2] + BUFFER_DEG, bounds[3] + BUFFER_DEG)
    height = max(1, round(WIDTH * (north - south) / (east - west)))
    return WMS + "?" + urlencode({
        "SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.3.0", "LAYERS": f"{layer},{OVERLAY}", "STYLES": ",",
        "CRS": "EPSG:4326", "BBOX": f"{south:.4f},{west:.4f},{north:.4f},{east:.4f}",   # WMS 1.3.0: lat, lon order
        "WIDTH": WIDTH, "HEIGHT": height, "FORMAT": "image/jpeg", "TIME": day.isoformat()})


def fetch(bounds: tuple[float, float, float, float], lon: float, observed_at: datetime,
          client: httpx.Client | None = None) -> dict:
    """Both views of the last servable overpass. A view that fails is left out and named in `errors`; nothing raises."""
    day, hours = overpass_date(observed_at, lon)
    urls = [(view, layer, wms_url(layer, day, bounds)) for view, layer in VIEWS]

    def get(item: tuple[str, str, str]) -> Picture | str:
        view, layer, url = item
        try:
            response = (client or httpx).get(url, timeout=TIMEOUT_S)
            response.raise_for_status()
            mime = response.headers.get("content-type", "").split(";")[0].strip()
            if not mime.startswith("image/"):   # GIBS answers a bad request with an XML exception, status 200
                return f"{view}: not an image ({mime or 'no content type'})"
            return Picture(view, f"{layer},{OVERLAY}", url, mime, response.content)
        except Exception as exc:   # imagery is support: the verification goes on without it
            return f"{view}: {type(exc).__name__}"

    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        got = list(pool.map(get, urls))
    return {"date": day.isoformat(), "hours_before_observation": hours,
            "pictures": [g for g in got if isinstance(g, Picture)], "errors": [g for g in got if isinstance(g, str)]}
