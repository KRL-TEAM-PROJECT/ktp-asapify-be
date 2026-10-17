"""Canonical data model: every source adapter turns raw records into Observations.

The rules engine only reads canonical observations. Canonicalisation aligns representation and checks
compatibility; it never makes physically different measurements equivalent (skin temperature, air
temperature and brightness temperature stay separate parameters).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from statistics import fmean, pstdev
from typing import Iterable


class Quality(str, Enum):
    ACCEPTED = "ACCEPTED"   # meets the quality and compatibility requirements
    DEGRADED = "DEGRADED"   # usable only where the rules engine has an explicit policy for it
    REJECTED = "REJECTED"   # never reaches the risk calculation


@dataclass(frozen=True)
class Observation:
    source: str                # adapter id: "nasa_power", "open_meteo"
    parameter: str             # canonical name: "skin_temperature", "relative_humidity", "cloud_cover"
    value: float | None        # normalised value; None when rejected
    unit: str                  # canonical unit: "K" or "%"
    measurement_type: str      # physical meaning, e.g. "earth_skin_temperature"
    observed_at: datetime      # actual observation time, UTC
    ingested_at: datetime      # when this system received the record
    spatial_reference: str     # e.g. "EPSG:4326 point -2.4,113.9"
    spatial_resolution: str    # e.g. "0.5deg x 0.625deg"
    quality: Quality
    quality_flags: tuple[str, ...] = ()
    source_metadata: dict = field(default_factory=dict, hash=False, compare=False)

    @property
    def usable(self) -> bool:
        return self.quality is Quality.ACCEPTED and self.value is not None


@dataclass(frozen=True)
class Baseline:
    """Reference value for one parameter, place and UTC hour, built from earlier years of the same source."""
    source: str
    parameter: str
    measurement_type: str
    unit: str
    mean: float | None
    std: float | None
    samples: int
    description: str


def _calendar_distance(a: datetime, b: datetime) -> int:
    """Days between two calendar dates, ignoring the year (wraps around the new year)."""
    gap = abs(a.timetuple().tm_yday - b.timetuple().tm_yday)
    return min(gap, 365 - gap)


# An earlier year's value is at least half a year older than the target. Inside a calendar window of a few weeks that
# keeps the current season out and the previous ones in, also across the new year: for 3 January the window of a
# year before runs from mid-December to mid-January. Comparing calendar years instead counted this December as its
# own normal and left out last January (until 18 October 2026).
EARLIER = timedelta(days=183)


def build_baseline(target: Observation, history: Iterable[Observation], *, window_days: int = 15) -> Baseline:
    """Mean of usable earlier-year values with the same definition, UTC hour and calendar window as `target`."""
    pool = [o for o in history
            if o.usable and o.observed_at <= target.observed_at - EARLIER
            and (o.source, o.parameter, o.measurement_type, o.unit)
            == (target.source, target.parameter, target.measurement_type, target.unit)
            and o.observed_at.hour == target.observed_at.hour
            and _calendar_distance(o.observed_at, target.observed_at) <= window_days]
    values = [o.value for o in pool]
    years = sorted({o.observed_at.year for o in pool})
    span = f"{years[0]}-{years[-1]}" if years else "none"
    return Baseline(
        source=target.source, parameter=target.parameter, measurement_type=target.measurement_type, unit=target.unit,
        mean=round(fmean(values), 3) if values else None, std=round(pstdev(values), 3) if len(values) > 1 else None,
        samples=len(values),
        description=f"UTC hour {target.observed_at.hour:02d}, ±{window_days} days of "
                    f"{target.observed_at:%m-%d}, years {span}")


def anomaly(target: Observation, baseline: Baseline, *, min_samples: int = 30) -> float | None:
    """ΔT = observation − baseline, only when both are usable and describe the same measurement."""
    if (target.source, target.parameter, target.measurement_type, target.unit) != (
            baseline.source, baseline.parameter, baseline.measurement_type, baseline.unit):
        raise ValueError(f"incompatible measurements: {target.parameter} ({target.source}) "
                         f"vs baseline {baseline.parameter} ({baseline.source})")
    if not target.usable or baseline.mean is None or baseline.samples < min_samples:
        return None
    return round(target.value - baseline.mean, 3)
