"""Skoring anomali termal kontekstual untuk smoldering gambut.

Mekanisme (lihat diskusi tim untuk detail fisiknya):
  1. delta_bt        : BT band7 (~3.9um, sensitif sumber kecil-panas) minus BT band13
                        (~10.4um, mendekati rata-rata linear). Delta besar = ada sumber
                        panas kecil di piksel, bukan pemanasan permukaan yang merata.
  2. robust_z         : delta itu dinilai relatif ke riwayat piksel itu sendiri
                        (median/MAD per jam & bulan), bukan threshold absolut. Ini yang
                        membuat threshold otomatis menyesuaikan musim & siang/malam.
  3. neighbor_support  : fraksi piksel tetangga yang juga anomali di waktu sama.
                        Membedakan cluster (lebih meyakinkan) dari satu piksel nyala
                        sendirian (lebih mencurigakan sebagai noise sensor).
  4. night_streak      : anomali malam yang bertahan beberapa hari berturut-turut,
                        karena smoldering merambat lambat dan bertahan lama.

Semua angka di ContextualFireConfig adalah HIPOTESIS awal, belum tervalidasi -> kalibrasi
lewat backtest. Kelas ini murni komputasi (tanpa I/O): baseline dan data tetangga dipasok
oleh caller (Repo), supaya gampang di-unit-test dan tidak terikat ke Postgres/S3 tertentu.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

import numpy as np

from config import WIB

MAD_SCALE = 1.4826  # membuat MAD sebanding dengan std untuk data terdistribusi normal

Status = Literal["no_observation", "normal", "watch", "triggered"]


@dataclass(frozen=True)
class BandObs:
    """Satu timestep BT band7 & band13 untuk satu cell. value None = piksel berawan /
    tidak ada observasi valid (BUKAN 0, supaya tidak disalahartikan sebagai 'dingin')."""
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
    delta_z: float | None            # seberapa ekstrem delta dibanding baseline cell ini
    neighbor_support: float | None   # fraksi tetangga yang ikut anomali; None = tidak dicek
    night_streak: int
    status: Status


@dataclass(frozen=True)
class ContextualFireConfig:
    z_watch: float = 2.5
    z_trigger: float = 4.0
    min_baseline_samples: int = 30
    night_streak_trigger: int = 3
    max_night_gap_days: int = 2          # celah antar malam valid yg masih dianggap berurutan
    neighbor_support_min: float = 0.0    # 0 = koherensi spasial tidak wajib untuk trigger
    neighbor_z_threshold: float = 2.0    # tetangga dianggap "ikut anomali" jika z >= ini


class ContextualFireScorer:
    """Pure computation. Tidak menyentuh DB/S3 sama sekali."""

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
            return None                     # baseline belum cukup: jangan menilai
        med = float(np.median(b))
        mad = float(np.median(np.abs(b - med))) * MAD_SCALE
        if mad < 1e-6:
            return None                     # baseline datar: z tidak bermakna
        return (value - med) / mad

    def neighbor_support(
        self, neighbor_deltas: list[float], neighbor_baseline: list[float]
    ) -> float | None:
        """Fraksi piksel tetangga yang deltanya juga >= neighbor_z_threshold pada waktu sama."""
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
        """Satu 'malam' (18:00-06:00 WIB) dikunci ke tanggal sore harinya.
        TODO: ganti ke sudut matahari untuk produksi."""
        return (ts.astimezone(WIB) - timedelta(hours=12)).date()

    def night_streak(self, nightly_z: dict[date, float]) -> int:
        """Malam valid berurutan dengan z >= z_watch, dari yang terbaru. Malam tanpa
        observasi valid (awan) tidak ada di `nightly_z`, jadi dilewati, bukan memutus."""
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