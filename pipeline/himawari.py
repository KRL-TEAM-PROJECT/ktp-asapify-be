from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Mapping, Protocol

import numpy as np
import xarray as xr
from pycontrails.datalib.himawari import Himawari
from pyproj import Transformer

from models import ThermalObs

CellCoords = Mapping[str, tuple[float, float]]  # cell_id -> (lon, lat) of the cell centre

BT_MIN_K, BT_MAX_K = 180.0, 350.0  # outside this range = fill value / bad pixel


class ThermalSource(Protocol):
    def sample_cells(self, ts: datetime, cell_ids: list[str]) -> list[ThermalObs]: ...


def _as_utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


def _solar_elevation_deg(ts_utc: datetime, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """NOAA approximation (~0.5 degree accuracy), enough to decide day/night."""
    hour = ts_utc.hour + ts_utc.minute / 60 + ts_utc.second / 3600
    g = 2 * math.pi / 365 * (ts_utc.timetuple().tm_yday - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    tst = hour * 60 + eqtime + 4 * lon               # true solar time (menit)
    ha = np.radians(tst / 4 - 180)                   # hour angle
    lat_r = np.radians(lat)
    sin_el = np.sin(lat_r) * math.sin(decl) + np.cos(lat_r) * math.cos(decl) * np.cos(ha)
    return np.degrees(np.arcsin(np.clip(sin_el, -1, 1)))


class HimawariSource:
    """value = ΔBT = BT(B07) - BT(B13) per cell, satu timestep."""

    def __init__(self, cells: CellCoords, *, cloud_bt_k: float = 285.0,
                 night_sun_elev_deg: float = -6.0) -> None:
        self._cells = cells
        self._cloud_bt_k = cloud_bt_k          # placeholder, must be tuned from data
        self._night_elev = night_sun_elev_deg  # civil twilight: B07 bebas pantulan matahari
        self._hima = Himawari(region="F", bands=("B07", "B13"))
        self._to_geos: Transformer | None = None
        self._h: float | None = None

    def sample_cells(self, ts: datetime, cell_ids: list[str]) -> list[ThermalObs]:
        if not cell_ids:
            return []
        ts_utc = _as_utc(ts)
        lon, lat = self._lonlat(cell_ids)

        # 1) scene full disk (lazy/dask); 2) lon/lat -> sudut pandang satelit
        da = self._hima.get(ts_utc.strftime("%Y-%m-%dT%H:%M:%S"))
        x, y = self._project(da, lon, lat)
        on_disk = np.isfinite(x) & np.isfinite(y)

        # 3) nearest pixel per cell (no averaging, per "cell = pixel")
        b07 = np.full(len(cell_ids), np.nan)
        b13 = np.full(len(cell_ids), np.nan)
        if on_disk.any():
            pts = da.sel(band_id=[7, 13]).sel(
                x=xr.DataArray(x[on_disk], dims="cell"),
                y=xr.DataArray(y[on_disk], dims="cell"),
                method="nearest",
            ).compute()
            b07[on_disk] = pts.sel(band_id=7).values
            b13[on_disk] = pts.sel(band_id=13).values

        # 4) validity: finite, physical range, and not cloudy (cold B13 = cloud)
        with np.errstate(invalid="ignore"):
            valid = (
                np.isfinite(b07) & np.isfinite(b13)
                & (b07 > BT_MIN_K) & (b07 < BT_MAX_K)
                & (b13 > BT_MIN_K) & (b13 < BT_MAX_K)
                & (b13 >= self._cloud_bt_k)
            )
        delta = np.where(valid, b07 - b13, np.nan)

        # 5) night/day from solar elevation (computed for ALL cells, including cloudy ones)
        night = _solar_elevation_deg(ts_utc, lat, lon) < self._night_elev

        return [
            ThermalObs(cell_id=cid, ts=ts, value=None if np.isnan(d) else float(d), is_night=bool(n), kind='bt')
            for cid, d, n in zip(cell_ids, delta, night)
        ]

    def _lonlat(self, cell_ids: list[str]) -> tuple[np.ndarray, np.ndarray]:
        missing = [c for c in cell_ids if c not in self._cells]
        if missing:
            raise KeyError(f"cells without coordinates: {missing[:5]}")
        arr = np.array([self._cells[c] for c in cell_ids], dtype=float)
        return arr[:, 0], arr[:, 1]

    def _project(self, da: xr.DataArray, lon: np.ndarray, lat: np.ndarray):
        if self._to_geos is None:
            crs = da.attrs["crs"]  # geostationary proj string from metadata
            self._to_geos = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
            m = re.search(r"\+h=([\d.]+)", crs)
            if m is None:
                raise ValueError(f"Himawari CRS has no +h parameter: {crs}")
            self._h = float(m.group(1))
        with np.errstate(all="ignore"):
            xm, ym = self._to_geos.transform(lon, lat)
        # x,y coordinates in the DataArray are radians (scan angle) = metres / satellite height
        return np.asarray(xm, float) / self._h, np.asarray(ym, float) / self._h


if __name__ == "__main__":
    # Manual peat-cell check: run `python himawari.py` from pipeline/. Never runs on import.
    import geopandas as gpd
    import matplotlib.pyplot as plt

    from peat_mask import build_cells, peat_fraction

    # x/y coordinates are static, so one band is enough (lighter to download)
    scene = Himawari(region="F", bands=("B13",)).get("2026-09-28T17:00:00")

    cells = build_cells(scene, bbox=(108.0, -4.5, 119.5, 7.5))
    peat = gpd.read_file("assets/khg_peat_hydrological_units.shp")   # adjust the path if needed
    peat_cells = peat_fraction(cells, peat, min_fraction=0.5)

    print(len(cells), "pixels in bbox ->", len(peat_cells), "peat cells")
    print(peat_cells["peat_fraction"].describe())

    fig, ax = plt.subplots(figsize=(8, 8))
    peat.to_crs(4326).plot(ax=ax, color="lightgrey")
    peat_cells.sample(min(500, len(peat_cells))).plot(ax=ax, color="red")
    fig.savefig("cek_peat_cells.png", dpi=150)