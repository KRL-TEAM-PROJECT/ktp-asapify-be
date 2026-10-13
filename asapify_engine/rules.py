"""Rules engine: Fuzzy MAUT utilities per status, then a risk-weighted argmax with a safety margin.

Pure functions, no I/O. Every breakpoint is an initial prototype parameter from the design document and
must be calibrated on historical data. A utility says how well the conditions match a status; it is not a
calibrated fire probability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from math import inf


class Status(str, Enum):
    SAFE = "SAFE"
    WARNING = "WARNING"
    ALERT = "ALERT"
    NO_OBSERVABLE = "NO_OBSERVABLE"


SEVERITY = {Status.SAFE: 0, Status.WARNING: 1, Status.ALERT: 2}

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
    safety_margin: float = 0.05
    weights: dict[Status, dict[str, float]] = field(default_factory=_default_weights)


@dataclass(frozen=True)
class Assessment:
    status: Status
    utilities: dict[str, float]    # "SAFE" / "WARNING" / "ALERT" -> 0..1; empty when NO_OBSERVABLE
    memberships: dict[str, float]
    reason: str


def assess(delta_t: float | None, humidity: float | None, cloud: float | None,
           cfg: RuleConfig = RuleConfig()) -> Assessment:
    """Inputs must already be canonical and usable; None means the input was missing or rejected."""
    missing = [name for name, value in (("delta_t", delta_t), ("humidity", humidity), ("cloud_cover", cloud))
               if value is None]
    if missing:
        return Assessment(Status.NO_OBSERVABLE, {}, {}, f"not observable: {', '.join(missing)} unavailable")
    if cloud > cfg.cloud_max:
        return Assessment(Status.NO_OBSERVABLE, {}, {},
                          f"not observable: cloud cover {cloud:.0f}% > {cfg.cloud_max:.0f}%")

    memberships = {
        "delta_t_low": trapezoid(delta_t, cfg.delta_t_low),
        "delta_t_medium": trapezoid(delta_t, cfg.delta_t_medium),
        "delta_t_high": trapezoid(delta_t, cfg.delta_t_high),
        "humidity_high": trapezoid(humidity, cfg.humidity_high),
        "humidity_low": trapezoid(humidity, cfg.humidity_low),
    }
    utilities = {status.value: round(sum(w * memberships[k] for k, w in weights.items()) / sum(weights.values()), 4)
                 for status, weights in cfg.weights.items()}

    ranked = sorted(cfg.weights, key=lambda s: (utilities[s.value], SEVERITY[s]), reverse=True)
    top, runner_up = ranked[0], ranked[1]
    gap = round(utilities[top.value] - utilities[runner_up.value], 6)
    if gap <= cfg.safety_margin and SEVERITY[runner_up] > SEVERITY[top]:
        chosen = runner_up
        reason = (f"{runner_up.value} chosen over {top.value}: utilities within the "
                  f"{cfg.safety_margin} safety margin ({gap:.3f})")
    else:
        chosen = top
        reason = f"{top.value} has the highest utility ({utilities[top.value]:.3f})"
    return Assessment(chosen, utilities, {k: round(v, 4) for k, v in memberships.items()}, reason)
