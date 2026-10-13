"""Engine settings from the environment. Every timestamp in the engine is UTC and timezone-aware."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    """Read when constructed, not at import, so tests and scripts can set the environment first."""
    project: str = field(default_factory=lambda: _env("GOOGLE_CLOUD_PROJECT"))
    database: str = field(default_factory=lambda: _env("FIRESTORE_DATABASE", "asapify"))
    firms_map_key: str = field(default_factory=lambda: _env("FIRMS_MAP_KEY"))   # Secret Manager: firms-map-key
    # comma-separated FIRMS sources; NOAA-20 and NOAA-21 fly the same orbit about 50 minutes apart
    firms_source: str = field(default_factory=lambda: _env("FIRMS_SOURCE", "VIIRS_NOAA20_NRT,VIIRS_NOAA21_NRT"))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-3.5-flash"))
    agent_daily_quota: int = field(default_factory=lambda: int(_env("AGENT_DAILY_QUOTA", "20")))

    @property
    def agent_ready(self) -> bool:
        return bool(self.project and self.firms_map_key)
