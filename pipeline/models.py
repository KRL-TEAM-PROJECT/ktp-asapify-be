from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal


class Status(str, Enum):
    NO_OBSERVATION = "no_observation"  # awan / gap data. BUKAN "aman"
    NORMAL = "normal"
    WATCH = "watch"          # threshold rendah: tampil di UI saja, citra belum dipanggil
    TRIGGERED = "triggered"  # threshold tinggi / persisten: panggil agent


@dataclass(frozen=True)
class ThermalObs:
    """Satu observasi termal per cell. `value` sengaja generik supaya rule engine
    tidak terkunci ke keputusan LST vs brightness temperature (open item)."""
    cell_id: str
    ts: datetime                 # UTC, tz-aware
    value: float | None          # None = tidak ada observasi valid
    kind: Literal["bt", "lst_minus_sat"]
    is_night: bool


@dataclass(frozen=True)
class CellState:
    cell_id: str
    status: Status
    score: float | None          # robust z-score terbaru
    night_streak: int
    updated_at: datetime
    baseline_frozen: bool = False    # True kalau cell sedang ter-flag: jangan masuk baseline
    last_agent_at: datetime | None = None