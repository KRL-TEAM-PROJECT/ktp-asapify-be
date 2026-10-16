"""Rules engine: fuzzy memberships of the thermal anomaly, shifted by the environment, then a risk-weighted argmax with
a safety margin.

The satellites' thermal anomaly decides. The environment does not add weight of its own; it moves the curves:

    ΔT_eff = ΔT + d_rh − d_sun
    d_rh   = rh_shift_k · tanh(dry_z / 2)               dry_z: how much drier the air is than its normal, in σ of the
                                                        NASA POWER climatology for that place, hour and season
    d_sun  = sun_shift_k · max(0, sin(sun elevation))   reflected sunlight raises the 3.9 µm channel by day

SAFE, WARNING and ALERT are the low, medium and high memberships of ΔT_eff. With the default trapezoids and safety
margin the status boundaries are ΔT_eff = 1.95 K (SAFE/WARNING) and 6.95 K (WARNING/ALERT), so a drier than normal
night lowers both and the midday sun raises both, smoothly. Supporting data (VIIRS, NASA POWER) can only raise ALERT,
and only when ΔT_eff already has a high membership.

Pure functions: no I/O, no AI, easy to unit-test. Every number in RuleConfig is an initial HYPOTHESIS, not validated
yet: calibrate it with a backtest (tools/backtest_rules.py). A utility says how well the conditions match a status; it
is not a calibrated fire probability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from math import inf, isfinite, radians, sin, tanh

from .models import SEVERITY, Status

Trapezoid = tuple[float, float, float, float]
CURVES = {"delta_t_low": Status.SAFE, "delta_t_medium": Status.WARNING, "delta_t_high": Status.ALERT}


def trapezoid(x: float, shape: Trapezoid) -> float:
    """Membership 0..1: rises a→b, flat b→c, falls c→d. Infinite shoulders stay at 1."""
    a, b, c, d = shape
    if x < a or x > d:
        return 0.0
    if x < b:
        return (x - a) / (b - a)
    if x <= c:
        return 1.0
    return (d - x) / (d - c)


@dataclass(frozen=True)
class RuleConfig:
    delta_t_low: Trapezoid = (-inf, -inf, 1.0, 3.0)     # K; SAFE: low temperature anomaly
    delta_t_medium: Trapezoid = (1.0, 3.0, 6.0, 8.0)    # K; WARNING: anomaly around 3-6 °C
    delta_t_high: Trapezoid = (6.0, 8.0, inf, inf)      # K; ALERT: anomaly around 8 °C or more
    rh_shift_k: float = 1.0                              # K; the most a drier (wetter) than normal air moves the curves
    sun_shift_k: float = 1.0                             # K; the most the sun raises the thresholds (sun overhead)
    cloud_max: float = 60.0                              # %; above this the region is NO_OBSERVABLE
    safety_margin: float = 0.05                          # utilities this close: take the more severe status
    cooldown: timedelta = timedelta(hours=6)             # minimum gap between two automatic agent runs
    # Supporting data (VIIRS, NASA POWER) join the ALERT utility only when they say something; see fusion.py
    support_weights: dict[str, float] = field(default_factory=lambda: {"viirs_support": 0.2, "power_support": 0.1})


@dataclass(frozen=True)
class Assessment:
    status: Status
    utilities: dict[str, float]    # "SAFE" / "WARNING" / "ALERT" -> 0..1; empty when NO_OBSERVABLE
    memberships: dict[str, float]
    reason: str
    adjust: dict[str, float | None] = field(default_factory=dict)   # ΔT, the shifts and ΔT_eff behind the memberships


def valid_input(value: float | None, low: float = -inf, high: float = inf) -> float | None:
    """None for a missing, non-finite or out-of-range input: it then counts as unavailable, never as a value."""
    return value if value is not None and isfinite(value) and low <= value <= high else None


def shifts(dry_z: float | None, sun_elevation_deg: float | None, cfg: RuleConfig = RuleConfig()) -> tuple[float, float]:
    """(d_rh, d_sun) in K. A missing input moves nothing."""
    d_rh = cfg.rh_shift_k * tanh(dry_z / 2) if dry_z is not None else 0.0
    d_sun = cfg.sun_shift_k * max(0.0, sin(radians(sun_elevation_deg))) if sun_elevation_deg is not None else 0.0
    return d_rh, d_sun


def _why(delta_t: float, eff: float, dry_z: float | None, d_rh: float, d_sun: float) -> str:
    air = ("no humidity anomaly" if dry_z is None else
           f"air {abs(dry_z):.1f}σ {'drier' if dry_z >= 0 else 'wetter'} than normal {d_rh:+.2f} K")
    sun = f", sun {-d_sun:+.2f} K" if d_sun else ""
    return f"ΔT {delta_t:.2f} K counted as {eff:.2f} K ({air}{sun})"


def assess(delta_t: float | None, cloud: float | None, cfg: RuleConfig = RuleConfig(), *,
           dry_z: float | None = None, sun_elevation_deg: float | None = None,
           support: dict[str, float] | None = None) -> Assessment:
    """Inputs must already be canonical and usable; None means the input was missing or rejected.

    `dry_z` is the air's dryness anomaly against its normal (positive = drier); without it the curves stay put and the
    reason says so. `support` (VIIRS / POWER) can only raise the ALERT utility: no hotspot in other data is not proof
    of no fire. No view caps the status: day, night, one satellite or two all go through the same curves.
    """
    delta_t, cloud = valid_input(delta_t), valid_input(cloud, 0.0, 100.0)
    dry_z, sun_elevation_deg = valid_input(dry_z), valid_input(sun_elevation_deg, -90.0, 90.0)
    missing = [name for name, value in (("delta_t", delta_t), ("cloud_cover", cloud)) if value is None]
    if missing:
        return Assessment(Status.NO_OBSERVABLE, {}, {}, f"not observable: {', '.join(missing)} unavailable")
    if cloud > cfg.cloud_max:
        return Assessment(Status.NO_OBSERVABLE, {}, {},
                          f"not observable: cloud cover {cloud:.0f}% > {cfg.cloud_max:.0f}%")

    d_rh, d_sun = shifts(dry_z, sun_elevation_deg, cfg)
    eff = delta_t + d_rh - d_sun
    memberships = {name: trapezoid(eff, getattr(cfg, name)) for name in CURVES}
    utilities = {status.value: round(memberships[name], 4) for name, status in CURVES.items()}
    extra = {k: min(max(v, 0.0), 1.0) for k, v in (support or {}).items()
             if k in cfg.support_weights and valid_input(v) is not None}
    if extra:
        memberships.update(extra)
    high = memberships["delta_t_high"]
    if extra and high > 0:
        boosted = (high + sum(cfg.support_weights[k] * v for k, v in extra.items())) / (
            1 + sum(cfg.support_weights[k] for k in extra))
        utilities[Status.ALERT.value] = round(max(high, boosted), 4)

    ranked = sorted(CURVES.values(), key=lambda s: (utilities[s.value], SEVERITY[s]), reverse=True)
    top, runner_up = ranked[0], ranked[1]
    gap = round(utilities[top.value] - utilities[runner_up.value], 6)
    if gap <= cfg.safety_margin and SEVERITY[runner_up] > SEVERITY[top]:
        chosen = runner_up
        reason = (f"{runner_up.value} chosen over {top.value}: utilities within the "
                  f"{cfg.safety_margin} safety margin ({gap:.3f})")
    else:
        chosen = top
        reason = f"{top.value} has the highest utility ({utilities[top.value]:.3f})"
    reason += "; " + _why(delta_t, eff, dry_z, d_rh, d_sun)
    adjust = {"delta_t_k": round(delta_t, 3), "dry_z": None if dry_z is None else round(dry_z, 2),
              "rh_shift_k": round(d_rh, 3), "sun_shift_k": round(d_sun, 3), "delta_t_eff_k": round(eff, 3)}
    return Assessment(chosen, utilities, {k: round(v, 4) for k, v in memberships.items()}, reason, adjust)
