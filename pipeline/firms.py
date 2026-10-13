"""FIRMS area API (pull-only, needs a MAP_KEY). The endpoint and column names below were written from
memory of the FIRMS format: check the docs (https://firms.modaps.eosdis.nasa.gov/api/) before use."""
from __future__ import annotations

import io

import httpx
import pandas as pd

BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
DEDUPE_KEY = ["latitude", "longitude", "acq_date", "acq_time", "satellite"]


def fetch_firms(
    map_key: str,
    source: str,
    bbox: tuple[float, float, float, float],
    day_range: int = 1,
    date: str | None = None,   # "YYYY-MM-DD" for archive data; None = latest
) -> pd.DataFrame:
    west, south, east, north = bbox
    parts = [BASE, map_key, source, f"{west},{south},{east},{north}", str(day_range)]
    if date:
        parts.append(date)
    resp = httpx.get("/".join(parts), timeout=60)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    missing = [c for c in DEDUPE_KEY if c not in df.columns]
    if missing:  # FIRMS can return an error message as plain text
        raise ValueError(f"Unexpected FIRMS response (missing columns {missing}): {resp.text[:200]!r}")
    return df


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Dedupe (daily results overlap between requests) + UTC timestamps."""
    out = df.drop_duplicates(subset=DEDUPE_KEY).copy()
    hhmm = out["acq_time"].astype(str).str.zfill(4)
    out["acq_ts"] = pd.to_datetime(out["acq_date"].astype(str) + " " + hhmm, format="%Y-%m-%d %H%M", utc=True)
    return out