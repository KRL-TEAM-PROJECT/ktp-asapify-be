"""Konfigurasi dasar. Semua timestamp di seluruh codebase: UTC, timezone-aware."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta, timezone

WIB = timezone(timedelta(hours=7))


def _bbox_from_env() -> tuple[float, float, float, float]:
    # format env: "west,south,east,north"
    raw = os.environ.get("ASAPIFY_BBOX", "")
    if not raw:
        raise RuntimeError("Set ASAPIFY_BBOX='west,south,east,north' (bounding box area uji)")
    w, s, e, n = (float(x) for x in raw.split(","))
    return w, s, e, n


@dataclass(frozen=True)
class Settings:
    firms_map_key: str = os.environ.get("FIRMS_MAP_KEY", "")
    firms_source: str = os.environ.get("FIRMS_SOURCE", "VIIRS_SNPP_NRT")  # cek daftar source di docs FIRMS
    database_url: str = os.environ.get("DATABASE_URL", "")

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return _bbox_from_env()