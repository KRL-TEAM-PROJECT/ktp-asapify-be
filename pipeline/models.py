from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal


class Status(str, Enum):
    NO_OBSERVATION = "no_observation"  # cloud / data gap. NOT "safe"
    NORMAL = "normal"
    WATCH = "watch"          # low threshold: shown in the UI only, no imagery requested
    TRIGGERED = "triggered"  # high / persistent threshold: call the agent


@dataclass(frozen=True)
class ThermalObs:
    """One thermal observation per cell. `value` is deliberately generic so the rule engine
    is not locked into the LST vs brightness temperature decision (open item)."""
    cell_id: str
    ts: datetime                 # UTC, tz-aware
    value: float | None          # None = no valid observation
    kind: Literal["bt", "lst_minus_sat"]
    is_night: bool


@dataclass(frozen=True)
class CellState:
    cell_id: str
    status: Status
    score: float | None          # latest robust z-score
    night_streak: int
    updated_at: datetime
    baseline_frozen: bool = False    # True while the cell is flagged: keep it out of the baseline
    last_agent_at: datetime | None = None