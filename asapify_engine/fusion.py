"""Two geostationary views of the same peat -> one input for the rules engine.

Himawari-9 (140.7 E) and GK2A (128.2 E) look at Sumatra and Kalimantan from different angles, so a hotspot both see
at about the same place is confirmed, and one that only one satellite sees is not. VIIRS (NASA FIRMS) and NASA POWER are
supporting data: the latest value near the hotspot can raise the utilities, never lower them, because no hotspot is not
proof of no fire (design document section 8, rule 4). Pure computation, no I/O.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from .canonical import Quality
from .contextual import RegionThermal
from .firms import distance_km

SATELLITES = ("himawari9", "gk2a")


@dataclass(frozen=True)
class FusionConfig:
    """Every number is an initial HYPOTHESIS, like RuleConfig."""
    agree_km: float = 8.0                   # hot pixels of both satellites this close = the same hotspot (pixels 2-4 km)
    min_clear: float = 0.4                  # a satellite observes a region when 40 % of its cell is clear
    night_sun_elevation_deg: float = -6.0   # civil twilight: below it the 3.9 um channel has no sunlight
    viirs_km: tuple[float, float] = (2.0, 10.0)      # VIIRS support is full within 2 km and gone beyond 10 km
    viirs_hours: tuple[float, float] = (3.0, 12.0)   # ... and full within 3 h, gone beyond 12 h
    power_max_age_hours: float = 96.0       # POWER only supports with data at most 4 days old
    power_delta_t_k: tuple[float, float] = (1.0, 3.0)  # POWER skin-temperature anomaly: no support at 1 K, full at 3 K


@dataclass(frozen=True)
class Fused:
    delta_t: float | None          # brightness-temperature anomaly the rules use (K); None = not observable
    cloud_pct: float               # share of the cell under cloud in the clearest view
    quality: Quality               # ACCEPTED: night and both satellites agree; DEGRADED: daytime or one satellite
    flags: tuple[str, ...]
    support: dict[str, float]      # viirs_support / power_support, only at a possible hotspot and when they say something
    hot_lat: float | None
    hot_lon: float | None


def solar_elevation_deg(ts: datetime, lat: float, lon: float) -> float:
    """NOAA approximation (about 0.5 degrees), enough to tell day from night; same formula as pipeline/himawari.py."""
    ts = ts.astimezone(timezone.utc)
    hour = ts.hour + ts.minute / 60 + ts.second / 3600
    g = 2 * math.pi / 365 * (ts.timetuple().tm_yday - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    ha = math.radians((hour * 60 + eqtime + 4 * lon) / 4 - 180)   # hour angle from the true solar time
    lat_r = math.radians(lat)
    sin_el = math.sin(lat_r) * math.sin(decl) + math.cos(lat_r) * math.cos(decl) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_el))))


def _ramp(x: float, full: float, none: float) -> float:
    """1 up to `full`, falling linearly to 0 at `none`."""
    return 1.0 if x <= full else 0.0 if x >= none else (none - x) / (none - full)


def viirs_support(hotspots: list[dict], lat: float, lon: float, slot: datetime, cfg: FusionConfig) -> float | None:
    """Strongest nearby VIIRS evidence for a point (nominal or high confidence only); None when there is none."""
    best = 0.0
    for h in hotspots:
        if h["confidence"] not in ("n", "nominal", "h", "high"):
            continue
        near = _ramp(distance_km(lat, lon, h["latitude"], h["longitude"]), *cfg.viirs_km)
        recent = _ramp(abs((h["acq_ts"] - slot).total_seconds()) / 3600, *cfg.viirs_hours)
        best = max(best, near * recent)
    return round(best, 3) if best > 0 else None


def power_support(power: dict | None, slot: datetime, cfg: FusionConfig) -> float | None:
    """Latest NASA POWER skin-temperature anomaly of the region as supporting evidence; None when old or missing."""
    if not power or power.get("delta_t_k") is None or not power.get("observed_at"):
        return None
    age = (slot - datetime.fromisoformat(power["observed_at"].replace("Z", "+00:00"))).total_seconds() / 3600
    if age > cfg.power_max_age_hours:
        return None
    lo, hi = cfg.power_delta_t_k
    value = min(1.0, max(0.0, (power["delta_t_k"] - lo) / (hi - lo)))
    return round(value, 3) if value > 0 else None


def fuse(views: dict[str, RegionThermal | None], *, night: bool, slot: datetime, centre: tuple[float, float],
         hotspots: list[dict] = (), power: dict | None = None, cfg: FusionConfig = FusionConfig()) -> Fused:
    """`views` maps himawari9 / gk2a to that satellite's view of the region (None = no data for this slot)."""
    # 1) which satellites saw enough of the peat
    seen = {name: v for name, v in views.items()
            if v is not None and v.max_anomaly_k is not None and 1 - v.cloud_fraction >= cfg.min_clear}
    clouds = [v.cloud_fraction for v in views.values() if v is not None]
    cloud_pct = round(100 * (min(clouds) if clouds else 1.0), 1)
    flags = ([] if night else ["daytime"]) + [f"no_data:{name}" for name, v in views.items() if v is None]
    if not seen:
        return Fused(None, cloud_pct, Quality.REJECTED, tuple(flags + ["no_clear_view"]), {}, None, None)

    # 2) mutual confirmation: both hot at about the same place, or both quiet
    hot = {name: v for name, v in seen.items() if v.n_hot > 0}
    if len(seen) == 2:
        h, g = seen["himawari9"], seen["gk2a"]
        if len(hot) == 2 and distance_km(h.hot_lat, h.hot_lon, g.hot_lat, g.hot_lon) <= cfg.agree_km:
            delta_t, agree = min(h.max_anomaly_k, g.max_anomaly_k), True   # the size both satellites confirm
            flags.append("two_satellites_agree")
        elif len(hot) == 0:
            delta_t, agree = min(h.max_anomaly_k, g.max_anomaly_k), True   # likewise: what both see
            flags.append("two_satellites_quiet")
        else:
            delta_t, agree = max(h.max_anomaly_k, g.max_anomaly_k), False
            flags.append("satellites_disagree")
    else:
        (name, only), = seen.items()
        delta_t, agree = only.max_anomaly_k, False
        flags.append(f"single_satellite:{name}")
    quality = Quality.ACCEPTED if night and agree else Quality.DEGRADED

    # 3) where the strongest anomaly is; supporting data only count at a possible hotspot, never on a quiet region
    lead = max(hot.values() or seen.values(), key=lambda v: v.max_anomaly_k)
    lat, lon = (lead.hot_lat, lead.hot_lon) if lead.hot_lat is not None else centre
    support = {}
    if hot:
        support = {"viirs_support": viirs_support(list(hotspots), lat, lon, slot, cfg),
                   "power_support": power_support(power, slot, cfg)}
    return Fused(round(delta_t, 3), cloud_pct, quality, tuple(flags),
                 {k: v for k, v in support.items() if v is not None}, lat, lon)
