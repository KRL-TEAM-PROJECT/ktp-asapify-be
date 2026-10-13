"""Shared engine types. Every timestamp in the engine is UTC and timezone-aware.

Same role as pipeline/models.py: the status levels and the per-region state live here, so the rules engine, the
state machine and storage all talk about the same objects.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Status(str, Enum):
    SAFE = "SAFE"                    # low temperature anomaly and humid air
    WARNING = "WARNING"              # medium anomaly: closer monitoring, no agent call
    ALERT = "ALERT"                  # high anomaly and dry air: the verification agent runs
    NO_OBSERVABLE = "NO_OBSERVABLE"  # cloud / data gap. NOT "safe"


SEVERITY = {Status.SAFE: 0, Status.WARNING: 1, Status.ALERT: 2}   # NO_OBSERVABLE has no rank


@dataclass(frozen=True)
class RegionState:
    region_id: str
    status: Status
    since: datetime                  # when the region entered this status
    observed_at: datetime            # observation time of the latest assessment
    utilities: dict[str, float]
    last_agent_at: datetime | None = None


@dataclass(frozen=True)
class Transition:
    """One status change, kept for audit (design document section 4.4)."""
    region_id: str
    observed_at: datetime
    previous: Status | None
    current: Status
    utilities: dict[str, float]
    reason: str
