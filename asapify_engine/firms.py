"""NASA FIRMS area API (pull-only, needs a MAP_KEY) -> hotspot rows for the verification agent and VIIRS support.

Same contract as pipeline/firms.py (`fetch_firms` + `normalize`), checked against the live API on 2026-10-13: VIIRS
rows carry bright_ti4 (K), confidence n/l/h and frp (MW); DATE + DAY_RANGE returns DATE .. DATE+DAY_RANGE-1 and
DAY_RANGE must be 1-5. The stdlib csv reader replaces pandas to keep the API image small. Errors never carry the
request URL, because the URL contains the key.
"""
from __future__ import annotations

import csv
import io
import math
from datetime import datetime, timedelta, timezone

import httpx

BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
DEDUPE_KEY = ["latitude", "longitude", "acq_date", "acq_time", "satellite"]
MAX_DAY_RANGE = 5
CONFIDENT = {"n", "nominal", "h", "high"}   # VIIRS confidence classes that count as evidence


def fetch_firms(
    map_key: str,
    source: str,
    bbox: tuple[float, float, float, float],
    day_range: int = 1,
    date: str | None = None,   # "YYYY-MM-DD" = first day of the range; None = the most recent days
    client: httpx.Client | None = None,
) -> list[dict]:
    west, south, east, north = bbox
    parts = [BASE, map_key, source, f"{west},{south},{east},{north}", str(day_range)]
    if date:
        parts.append(date)
    resp = (client or httpx).get("/".join(parts), timeout=60)
    if resp.status_code != 200:
        raise ValueError(f"FIRMS returned HTTP {resp.status_code}")
    header = resp.text.split("\n", 1)[0].strip().split(",")
    missing = [c for c in DEDUPE_KEY if c not in header]
    if missing:  # FIRMS returns errors as plain text with status 200
        raise ValueError(f"Unexpected FIRMS response (missing columns {missing}): {resp.text[:120]!r}")
    return list(csv.DictReader(io.StringIO(resp.text)))


def normalize(rows: list[dict]) -> list[dict]:
    """Dedupe (per-day results overlap between requests) + UTC timestamp + numbers as floats."""
    seen, out = set(), []
    for r in rows:
        key = tuple(r.get(c) for c in DEDUPE_KEY)
        if key in seen:
            continue
        seen.add(key)
        hhmm = str(r["acq_time"]).zfill(4)
        out.append({
            "latitude": float(r["latitude"]), "longitude": float(r["longitude"]),
            "acq_ts": datetime.strptime(f"{r['acq_date']} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc),
            "satellite": r.get("satellite"), "confidence": str(r.get("confidence", "")).lower(),
            "brightness_k": _number(r.get("bright_ti4") or r.get("brightness")),   # VIIRS I4 / MODIS band 21
            "frp_mw": _number(r.get("frp")), "daynight": r.get("daynight"),
        })
    return out


def area_hotspots(map_key: str, sources: list[str], bbox: tuple[float, float, float, float], start: datetime,
                  end: datetime, client: httpx.Client | None = None) -> list[dict]:
    """Normalised hotspots of every source inside `bbox` acquired from `start` to `end` (one request per source)."""
    first = max(start.date(), end.date() - timedelta(days=MAX_DAY_RANGE - 1))
    rows = [row for source in sources
            for row in fetch_firms(map_key, source, bbox, (end.date() - first).days + 1, first.isoformat(), client)]
    return [h for h in normalize(rows) if start <= h["acq_ts"] <= end]


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def summarize(hotspots: list[dict], centre: tuple[float, float], observed_at: datetime, limit: int = 15) -> dict:
    """What the agent sees: totals plus the strongest hotspots, trimmed so the prompt stays small."""
    lat, lon = centre
    rows = sorted(hotspots, key=lambda h: (h["frp_mw"] or 0.0, h["brightness_k"] or 0.0), reverse=True)
    brightness = [h["brightness_k"] for h in hotspots if h["brightness_k"] is not None]
    confidence: dict[str, int] = {}
    for h in hotspots:
        confidence[h["confidence"] or "unknown"] = confidence.get(h["confidence"] or "unknown", 0) + 1
    return {
        "hotspot_count": len(hotspots),
        "confident_count": sum(1 for h in hotspots if h["confidence"] in CONFIDENT),
        "highest_brightness_temp_k": max(brightness) if brightness else None,
        "confidence_counts": confidence,
        "hotspots": [{
            "lat": round(h["latitude"], 4), "lon": round(h["longitude"], 4),
            "acquired_at": h["acq_ts"].isoformat().replace("+00:00", "Z"),
            "hours_from_observation": round((h["acq_ts"] - observed_at).total_seconds() / 3600, 1),
            "distance_km": round(distance_km(lat, lon, h["latitude"], h["longitude"]), 1),
            "confidence": h["confidence"], "brightness_k": h["brightness_k"], "frp_mw": h["frp_mw"],
            "daynight": h["daynight"], "satellite": h["satellite"],
        } for h in rows[:limit]],
    }
