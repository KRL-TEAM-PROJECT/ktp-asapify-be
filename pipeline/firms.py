"""FIRMS area API (pull-only, butuh MAP_KEY). Endpoint dan nama kolom di bawah ditulis dari
ingatan format FIRMS: cek dulu di docs (https://firms.modaps.eosdis.nasa.gov/api/) sebelum dipakai."""
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
    date: str | None = None,   # "YYYY-MM-DD" untuk data arsip; None = terbaru
) -> pd.DataFrame:
    west, south, east, north = bbox
    parts = [BASE, map_key, source, f"{west},{south},{east},{north}", str(day_range)]
    if date:
        parts.append(date)
    resp = httpx.get("/".join(parts), timeout=60)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    missing = [c for c in DEDUPE_KEY if c not in df.columns]
    if missing:  # FIRMS bisa mengembalikan pesan error sebagai teks biasa
        raise ValueError(f"Respons FIRMS tidak sesuai ekspektasi (kolom hilang {missing}): {resp.text[:200]!r}")
    return df


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Dedupe (hasil per-hari tumpang tindih antar request) + timestamp UTC."""
    out = df.drop_duplicates(subset=DEDUPE_KEY).copy()
    hhmm = out["acq_time"].astype(str).str.zfill(4)
    out["acq_ts"] = pd.to_datetime(out["acq_date"].astype(str) + " " + hhmm, format="%Y-%m-%d %H%M", utc=True)
    return out