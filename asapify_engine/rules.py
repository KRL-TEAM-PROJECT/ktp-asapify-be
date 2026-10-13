"""Rules engine: Fuzzy MAUT utilities per status, then a risk-weighted argmax with a safety margin.

Pure functions: no I/O, no AI, easy to unit-test. Every number in RuleConfig is an initial HYPOTHESIS from the
design document, not validated yet: calibrate it with a backtest. A utility says how well the conditions match a
status; it is not a calibrated fire probability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from math import inf

from .models import SEVERITY, Status

Trapezoid = tuple[float, float, float, float]


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


def _default_weights() -> dict[Status, dict[str, float]]:
    return {
        Status.SAFE: {"delta_t_low": 0.6, "humidity_high": 0.4},
        Status.WARNING: {"delta_t_medium": 1.0},
        Status.ALERT: {"delta_t_high": 0.6, "humidity_low": 0.4},
    }


@dataclass(frozen=True)
class RuleConfig:
    delta_t_low: Trapezoid = (-inf, -inf, 1.0, 3.0)     # K; SAFE: low temperature anomaly
    delta_t_medium: Trapezoid = (1.0, 3.0, 6.0, 8.0)    # K; WARNING: anomaly around 3-6 °C
    delta_t_high: Trapezoid = (6.0, 8.0, inf, inf)      # K; ALERT: anomaly around 8 °C or more
    humidity_high: Trapezoid = (40.0, 60.0, inf, inf)   # %; SAFE: high relative humidity
    humidity_low: Trapezoid = (-inf, -inf, 30.0, 40.0)  # %; ALERT: relative humidity below 30 %
    cloud_max: float = 60.0                              # %; above this the region is NO_OBSERVABLE
    safety_margin: float = 0.05                          # utilities this close: take the more severe status
    cooldown: timedelta = timedelta(hours=6)             # minimum gap between two automatic agent runs
    weights: dict[Status, dict[str, float]] = field(default_factory=_default_weights)
    # Supporting data (VIIRS, NASA POWER) join the ALERT utility only when they say something; see fusion.py
    support_weights: dict[str, float] = field(default_factory=lambda: {"viirs_support": 0.2, "power_support": 0.1})


@dataclass(frozen=True)
class Assessment:
    status: Status
    utilities: dict[str, float]    # "SAFE" / "WARNING" / "ALERT" -> 0..1; empty when NO_OBSERVABLE
    memberships: dict[str, float]
    reason: str


def _utility(memberships: dict[str, float], weights: dict[str, float]) -> float:
    return round(sum(w * memberships[k] for k, w in weights.items()) / sum(weights.values()), 4)


def assess(delta_t: float | None, humidity: float | None, cloud: float | None,
           cfg: RuleConfig = RuleConfig(), *, support: dict[str, float] | None = None,
           cap: str | None = None) -> Assessment:
    """Inputs must already be canonical and usable; None means the input was missing or rejected.

    Without humidity the utilities use the thermal input alone (the reason says so). `support` (VIIRS / POWER) can
    only raise the ALERT utility: no hotspot in other data is not proof of no fire. `cap` is the explicit DEGRADED
    policy of design section 3.5: daytime or a single satellite may give WARNING at most, so ALERT always rests on a
    night-time view confirmed by both satellites.
    """
    missing = [name for name, value in (("delta_t", delta_t), ("cloud_cover", cloud)) if value is None]
    if missing:
        return Assessment(Status.NO_OBSERVABLE, {}, {}, f"not observable: {', '.join(missing)} unavailable")
    if cloud > cfg.cloud_max:
        return Assessment(Status.NO_OBSERVABLE, {}, {},
                          f"not observable: cloud cover {cloud:.0f}% > {cfg.cloud_max:.0f}%")

    memberships = {
        "delta_t_low": trapezoid(delta_t, cfg.delta_t_low),
        "delta_t_medium": trapezoid(delta_t, cfg.delta_t_medium),
        "delta_t_high": trapezoid(delta_t, cfg.delta_t_high),
    }
    if humidity is not None:
        memberships["humidity_high"] = trapezoid(humidity, cfg.humidity_high)
        memberships["humidity_low"] = trapezoid(humidity, cfg.humidity_low)
    weights = {s: {k: w for k, w in ws.items() if k in memberships} for s, ws in cfg.weights.items()}
    utilities = {status.value: _utility(memberships, ws) for status, ws in weights.items()}
    extra = {k: v for k, v in (support or {}).items() if k in cfg.support_weights}
    if extra:
        memberships.update(extra)
        boosted = _utility(memberships, {**weights[Status.ALERT], **{k: cfg.support_weights[k] for k in extra}})
        utilities[Status.ALERT.value] = max(utilities[Status.ALERT.value], boosted)

    ranked = sorted(weights, key=lambda s: (utilities[s.value], SEVERITY[s]), reverse=True)
    top, runner_up = ranked[0], ranked[1]
    gap = round(utilities[top.value] - utilities[runner_up.value], 6)
    if gap <= cfg.safety_margin and SEVERITY[runner_up] > SEVERITY[top]:
        chosen = runner_up
        reason = (f"{runner_up.value} chosen over {top.value}: utilities within the "
                  f"{cfg.safety_margin} safety margin ({gap:.3f})")
    else:
        chosen = top
        reason = f"{top.value} has the highest utility ({utilities[top.value]:.3f})"
    if humidity is None:
        reason += "; humidity unavailable, thermal input only"
    if cap and chosen is Status.ALERT:
        chosen, reason = Status.WARNING, f"{reason}; capped at WARNING ({cap})"
    return Assessment(chosen, utilities, {k: round(v, 4) for k, v in memberships.items()}, reason)
