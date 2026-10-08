"""Rule engine deterministik. Pure functions: tanpa I/O, tanpa AI, gampang di-unit-test.

Semua angka di RuleConfig adalah HIPOTESIS awal, belum tervalidasi. Kalibrasi lewat backtest."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Callable

import numpy as np

from config import WIB
from models import CellState, Status, ThermalObs

MAD_SCALE = 1.4826  # membuat MAD sebanding dengan std untuk data normal


@dataclass(frozen=True)
class RuleConfig:
    z_watch: float = 2.5
    z_trigger: float = 4.0
    night_streak_trigger: int = 3
    max_night_gap_days: int = 2       # celah antar malam valid yang masih dianggap "berurutan"
    min_baseline_samples: int = 30
    window_days: int = 7
    cooldown: timedelta = timedelta(hours=24)


def robust_z(value: float, baseline: list[float], cfg: RuleConfig) -> float | None:
    b = np.asarray([x for x in baseline if x is not None], dtype=float)
    if b.size < cfg.min_baseline_samples:
        return None                    # baseline belum cukup: jangan menilai
    med = float(np.median(b))
    mad = float(np.median(np.abs(b - med))) * MAD_SCALE
    if mad < 1e-6:
        return None                    # baseline datar: z tidak bermakna
    return (value - med) / mad


def night_key(ts: datetime) -> date:
    """Satu 'malam' (mis. 18:00-06:00 WIB) dikunci ke tanggal sore harinya."""
    return (ts.astimezone(WIB) - timedelta(hours=12)).date()


def night_streak(nightly: dict[date, float], z_watch: float, max_gap_days: int) -> int:
    """Hitung malam valid berurutan dengan skor >= z_watch, dari yang terbaru.
    Malam tanpa observasi valid (awan) TIDAK ada di `nightly`, jadi dilewati, bukan memutus."""
    streak, prev = 0, None
    for d in sorted(nightly, reverse=True):
        if prev is not None and (prev - d).days > max_gap_days:
            break
        if nightly[d] < z_watch:
            break
        streak += 1
        prev = d
    return streak


def evaluate_cell(
    cell_id: str,
    recent: list[ThermalObs],
    baseline_fn: Callable[[datetime], list[float]],   # baseline per jam-hari & bulan untuk cell ini
    prev: CellState | None,
    now: datetime,
    cfg: RuleConfig = RuleConfig(),
) -> CellState:
    scored: list[tuple[ThermalObs, float]] = []
    for o in recent:
        if o.value is None:
            continue
        z = robust_z(o.value, baseline_fn(o.ts), cfg)
        if z is not None:
            scored.append((o, z))

    carry_agent = prev.last_agent_at if prev else None
    if not scored:
        return CellState(cell_id, Status.NO_OBSERVATION, None, prev.night_streak if prev else 0,
                         now, prev.baseline_frozen if prev else False, carry_agent)

    _, latest_z = max(scored, key=lambda t: t[0].ts)
    nightly: dict[date, float] = {}
    for o, z in scored:
        if o.is_night:
            k = night_key(o.ts)
            nightly[k] = max(nightly.get(k, float("-inf")), z)
    streak = night_streak(nightly, cfg.z_watch, cfg.max_night_gap_days)

    if latest_z >= cfg.z_trigger or streak >= cfg.night_streak_trigger:
        status = Status.TRIGGERED
    elif latest_z >= cfg.z_watch:
        status = Status.WATCH
    else:
        status = Status.NORMAL

    return CellState(cell_id, status, latest_z, streak, now,
                     baseline_frozen=status is not Status.NORMAL, last_agent_at=carry_agent)


def should_trigger_agent(prev: CellState | None, new: CellState, now: datetime, cfg: RuleConfig = RuleConfig()) -> bool:
    """Trigger berdasarkan TRANSISI + cooldown, bukan kondisi. Cegah fetch citra berulang."""
    if new.status is not Status.TRIGGERED:
        return False
    if prev is not None and prev.status is Status.TRIGGERED:
        return False
    if prev is not None and prev.last_agent_at and now - prev.last_agent_at < cfg.cooldown:
        return False
    return True


def mark_agent_called(state: CellState, now: datetime) -> CellState:
    return replace(state, last_agent_at=now)