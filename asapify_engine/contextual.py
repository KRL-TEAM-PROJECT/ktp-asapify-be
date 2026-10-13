"""Contextual thermal anomaly of one region in one geostationary slot.

The design document (section 3.4) needs the observation and its baseline to be the same measurement. A contextual
baseline gives exactly that without a multi-year archive: the reference is the median of the clear pixels of the same
region cell, in the same slot, from the same sensor. Same idea as pipeline/contextualFire.py, with the neighbourhood as
background instead of the pixel's own history; a per-pixel history can join once enough slots are stored.

delta_bt = BT(3.8-3.9 um) - BT(10.4-10.5 um): a small hot source raises the mid-infrared channel far more than the
long-wave one, while plain surface warming raises both. Thin cirrus also raises delta_bt, by cooling 10.4 um, so a
pixel only counts when the 3.9 um channel itself rose above the background too (the MODIS/VIIRS T4 test). On the
11 October 2026 18:10 UTC Himawari slot this test removed all 6 of 11 hot regions that had no VIIRS fire nearby, and
scoring the whole cell instead of the peat pixels alone found 10 of the 14 regions VIIRS saw burning instead of 4
(docs/ENGINE.md, Validation). Pure computation: arrays in, numbers out, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MAD_SCALE = 1.4826                 # makes the MAD comparable with a standard deviation for normally distributed data
BT_MIN_K, BT_MAX_K = 180.0, 400.0  # outside this range = fill value / broken pixel


@dataclass(frozen=True)
class ContextConfig:
    """Every number is an initial HYPOTHESIS: calibrate against VIIRS detections."""
    cloud_bt_k: float = 270.0      # long-wave colder than this = cloud top
    hot_anomaly_k: float = 3.0     # delta_bt this far above the background is hot (the design's WARNING start)
    hot_z: float = 3.0             # ... and at least this many robust sigmas above it (also for the 3.9 um rise)
    mwir_rise_k: float = 2.0       # the 3.9 um channel itself must be this much warmer than the background
    trim_k: float = 5.0            # background pixels this far above the median are left out: they may be fires
    min_background: int = 30       # fewer clear pixels in the cell: no verdict
    mad_floor_k: float = 0.3       # a very uniform background must not turn a tiny difference into a huge z


@dataclass(frozen=True)
class RegionThermal:
    n_pixels: int                  # satellite pixels whose centre is inside the region cell
    n_clear: int
    cloud_fraction: float          # share of the cell under cloud, 0-1
    n_peat: int                    # pixels whose centre is on KHG peat
    n_peat_clear: int
    background_k: float | None     # median delta_bt of the clear background
    background_mad_k: float | None
    max_anomaly_k: float | None    # strongest fire-like pixel (3.9 um rose too, delta_bt hot_z sigmas up): delta_bt
                                   # above the background; 0 when no pixel is fire-like; None = no verdict
    max_z: float | None
    n_hot: int                     # clear pixels counted as hot
    n_hot_peat: int                # ... of which on peat
    hot_on_peat: bool | None       # the strongest fire-like pixel lies on peat
    hot_lat: float | None          # where the strongest fire-like pixel is
    hot_lon: float | None
    mwir_k: float | None           # brightness temperatures at that pixel
    lwir_k: float | None

    def summary(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def _robust(values: np.ndarray, floor: float) -> tuple[float, float]:
    med = float(np.median(values))
    return med, max(float(np.median(np.abs(values - med))) * MAD_SCALE, floor)


def region_thermal(mwir: np.ndarray, lwir: np.ndarray, peat: np.ndarray, lat: np.ndarray, lon: np.ndarray,
                   cfg: ContextConfig = ContextConfig()) -> RegionThermal:
    """All arrays cover the pixels of one region cell; `peat` marks the pixels whose centre is on peat."""
    # 1) usable and clear pixels
    with np.errstate(invalid="ignore"):
        valid = (np.isfinite(mwir) & np.isfinite(lwir)
                 & (mwir > BT_MIN_K) & (mwir < BT_MAX_K) & (lwir > BT_MIN_K) & (lwir < BT_MAX_K))
        clear = valid & (lwir >= cfg.cloud_bt_k)
    dbt = np.where(valid, mwir - lwir, np.nan)
    n_clear = int(clear.sum())
    cloud = round(1.0 - n_clear / mwir.size, 4) if mwir.size else 1.0
    counts = dict(n_pixels=int(mwir.size), n_clear=n_clear, cloud_fraction=cloud,
                  n_peat=int(peat.sum()), n_peat_clear=int((peat & clear).sum()))
    if n_clear < cfg.min_background:
        return RegionThermal(**counts, background_k=None, background_mad_k=None, max_anomaly_k=None, max_z=None,
                             n_hot=0, n_hot_peat=0, hot_on_peat=None, hot_lat=None, hot_lon=None, mwir_k=None,
                             lwir_k=None)

    # 2) background: clear pixels, once more without the hottest ones (they may be burning themselves)
    background = clear & (dbt <= float(np.median(dbt[clear])) + cfg.trim_k)
    med, mad = _robust(dbt[background], cfg.mad_floor_k)
    med_mwir, mad_mwir = _robust(mwir[background], cfg.mad_floor_k)

    # 3) fire-like pixels: the 3.9 um channel rose and delta_bt stands out from the background; hot ones are 3 K up
    rise = mwir - med_mwir
    anomaly = dbt - med
    fire_like = clear & (rise >= cfg.mwir_rise_k) & (rise / mad_mwir >= cfg.hot_z) & (anomaly / mad >= cfg.hot_z)
    anomaly = np.where(fire_like, anomaly, -np.inf)
    hot = fire_like & (anomaly >= cfg.hot_anomaly_k)
    stats = dict(background_k=round(med, 3), background_mad_k=round(mad, 3),
                 n_hot=int(hot.sum()), n_hot_peat=int((hot & peat).sum()))
    if not fire_like.any():
        return RegionThermal(**counts, **stats, max_anomaly_k=0.0, max_z=0.0, hot_on_peat=None, hot_lat=None,
                             hot_lon=None, mwir_k=None, lwir_k=None)
    i = int(np.argmax(anomaly))
    return RegionThermal(
        **counts, **stats, max_anomaly_k=round(float(anomaly.flat[i]), 3),
        max_z=round(float(anomaly.flat[i]) / mad, 2), hot_on_peat=bool(peat.flat[i]),
        hot_lat=round(float(lat.flat[i]), 4), hot_lon=round(float(lon.flat[i]), 4),
        mwir_k=round(float(mwir.flat[i]), 2), lwir_k=round(float(lwir.flat[i]), 2))
