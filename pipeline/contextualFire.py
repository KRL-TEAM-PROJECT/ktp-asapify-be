"""Contextual thermal anomaly scoring for smouldering peat.

Mechanism (see the team discussion for the physics):
  1. delta_bt        : BT band7 (~3.9um, sensitive to small hot sources) minus BT band13
                        (~10.4um, close to the linear average). A large delta = a small
                        hot source in the pixel, not uniform surface heating.
  2. robust_z         : that delta is scored against the pixel's own history
                        (median/MAD per hour & month), not an absolute threshold. This is
                        what makes the threshold adapt to season and day/night.
  3. neighbor_support  : fraction of neighbouring pixels anomalous at the same time.
                        Separates a cluster (more convincing) from a single lit pixel
                        on its own (more likely sensor noise).
  4. night_streak      : night anomalies that persist for several consecutive days,
                        because smouldering spreads slowly and lasts long.

Every number in ContextualFireConfig is an initial HYPOTHESIS, not validated -> calibrate
through backtests. This class is pure computation (no I/O): the caller (Repo) supplies the
baseline and neighbour data, so it is easy to unit-test and not tied to one Postgres/S3.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

import numpy as np

from config import UTC_PLUS_7

MAD_SCALE = 1.4826  # makes MAD comparable to the std for normally distributed data

Status = Literal["no_observation", "normal", "watch", "triggered"]


@dataclass(frozen=True)
class BandObs:
    """One BT band7 & band13 timestep for one cell. value None = cloudy pixel /
    no valid observation (NOT 0, so it is never mistaken for 'cold')."""
    ts: datetime          # UTC, tz-aware
    bt_b07: float | None
    bt_b13: float | None
    is_night: bool

    @property
    def delta(self) -> float | None:
        if self.bt_b07 is None or self.bt_b13 is None:
            return None
        return self.bt_b07 - self.bt_b13


@dataclass(frozen=True)
class ContextualScore:
    cell_id: str
    ts: datetime
    delta_z: float | None            # how extreme the delta is versus this cell's baseline
    neighbor_support: float | None   # fraction of neighbours also anomalous; None = not checked
    night_streak: int
    status: Status


@dataclass(frozen=True)
class ContextualFireConfig:
    z_watch: float = 2.5
    z_trigger: float = 4.0
    min_baseline_samples: int = 30
    night_streak_trigger: int = 3
    max_night_gap_days: int = 2          # gap between valid nights still counted as consecutive
    neighbor_support_min: float = 0.0    # 0 = spatial coherence not required to trigger
    neighbor_z_threshold: float = 2.0    # a neighbour counts as "also anomalous" if z >= this


class ContextualFireScorer:
    """Pure computation. Never touches DB/S3."""

    def __init__(self, cfg: ContextualFireConfig = ContextualFireConfig()) -> None:
        self.cfg = cfg

    # --- building blocks -----------------------------------------------------------------
    @staticmethod
    def delta_bt(obs: BandObs) -> float | None:
        return obs.delta

    @staticmethod
    def robust_z(value: float, baseline: list[float], min_samples: int) -> float | None:
        b = np.asarray([x for x in baseline if x is not None], dtype=float)
        if b.size < min_samples:
            return None                     # baseline too short: do not score
        med = float(np.median(b))
        mad = float(np.median(np.abs(b - med))) * MAD_SCALE
        if mad < 1e-6:
            return None                     # flat baseline: z is meaningless
        return (value - med) / mad

    def neighbor_support(
        self, neighbor_deltas: list[float], neighbor_baseline: list[float]
    ) -> float | None:
        """Fraction of neighbouring pixels whose delta is also >= neighbor_z_threshold at the same time."""
        if not neighbor_deltas:
            return None
        zs = [
            self.robust_z(d, neighbor_baseline, self.cfg.min_baseline_samples)
            for d in neighbor_deltas if d is not None
        ]
        zs = [z for z in zs if z is not None]
        if not zs:
            return None
        hits = sum(1 for z in zs if z >= self.cfg.neighbor_z_threshold)
        return hits / len(zs)

    @staticmethod
    def _night_key(ts: datetime) -> date:
        """One 'night' (18:00-06:00 UTC+7) is keyed to the date of its evening.
        TODO: switch to solar elevation for production."""
        return (ts.astimezone(UTC_PLUS_7) - timedelta(hours=12)).date()

    def night_streak(self, nightly_z: dict[date, float]) -> int:
        """Consecutive valid nights with z >= z_watch, newest first. Nights without a
        valid observation (cloud) are absent from `nightly_z`, so they are skipped, not a break."""
        streak, prev = 0, None
        for d in sorted(nightly_z, reverse=True):
            if prev is not None and (prev - d).days > self.cfg.max_night_gap_days:
                break
            if nightly_z[d] < self.cfg.z_watch:
                break
            streak += 1
            prev = d
        return streak

    # --- orchestration ---------------------------------------------------------------------
    def score(
        self,
        cell_id: str,
        recent: list[BandObs],
        baseline_delta: list[float],
        now: datetime,
        neighbor_deltas_at: dict[datetime, list[float]] | None = None,
        neighbor_baseline: list[float] | None = None,
    ) -> ContextualScore:
        scored: list[tuple[BandObs, float]] = []
        for o in recent:
            d = self.delta_bt(o)
            if d is None:
                continue
            z = self.robust_z(d, baseline_delta, self.cfg.min_baseline_samples)
            if z is not None:
                scored.append((o, z))

        if not scored:
            return ContextualScore(cell_id, now, None, None, 0, "no_observation")

        latest_obs, latest_z = max(scored, key=lambda t: t[0].ts)

        nightly_z: dict[date, float] = {}
        for o, z in scored:
            if o.is_night:
                k = self._night_key(o.ts)
                nightly_z[k] = max(nightly_z.get(k, float("-inf")), z)
        streak = self.night_streak(nightly_z)

        support = None
        if neighbor_deltas_at is not None and neighbor_baseline is not None:
            support = self.neighbor_support(neighbor_deltas_at.get(latest_obs.ts, []), neighbor_baseline)
        meets_neighbor = support is None or support >= self.cfg.neighbor_support_min

        if meets_neighbor and (latest_z >= self.cfg.z_trigger or streak >= self.cfg.night_streak_trigger):
            status: Status = "triggered"
        elif latest_z >= self.cfg.z_watch:
            status = "watch"
        else:
            status = "normal"

        return ContextualScore(cell_id, now, latest_z, support, streak, status)