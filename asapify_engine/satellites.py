"""Himawari-9 AHI and GK2A AMI adapters: one 10-minute slot -> one RegionThermal per peat region and satellite.

Both come from NOAA Open Data on AWS (public, no key) and are decoded with satpy (readers `ahi_hsd` and `ami_l1b`):
- Himawari-9 at 140.7 E: bands B07 (3.9 um) and B13 (10.4 um, as in pipeline/himawari.py), only segments 5-6 of
  10, which cover the equatorial band with every region. Published about 12 minutes after the scan starts.
- GK2A at 128.2 E: channels SW038 (3.8 um) and IR105 (10.5 um), full-disk files. Published about 22-24 minutes
  after the scan starts, now and then later (measured on the bucket on 13 October 2026).
For each region the pixels whose centre lies in its NASA POWER cell are scored against each other (contextual.py), and
the ones on KHG peat are marked. Pixel windows are computed from the satellite grid on first use and cached per process.
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import httpx

from .contextual import ContextConfig, RegionThermal, region_thermal
from .regions import Region

SLOT = timedelta(minutes=10)
DASK_CHUNK = "4MiB"


@dataclass(frozen=True)
class Satellite:
    name: str
    reader: str            # satpy reader
    mwir: str              # 3.8-3.9 um dataset name
    lwir: str              # 10.4-10.5 um dataset name
    base: str              # bucket URL
    reader_kwargs: tuple = ()

    def urls(self, slot: datetime) -> list[str]:
        s = slot.astimezone(timezone.utc)
        if self.name == "himawari9":
            folder = f"{self.base}/AHI-L1b-FLDK/{s:%Y/%m/%d/%H%M}"
            return [f"{folder}/HS_H09_{s:%Y%m%d_%H%M}_{band}_FLDK_R20_S{seg:02d}10.DAT.bz2"
                    for band in (self.mwir, self.lwir) for seg in (5, 6)]
        folder = f"{self.base}/AMI/L1B/FD/{s:%Y%m/%d/%H}"
        return [f"{folder}/gk2a_ami_le1b_{band.lower()}_fd020ge_{s:%Y%m%d%H%M}.nc" for band in (self.mwir, self.lwir)]


SATELLITES = {
    "himawari9": Satellite("himawari9", "ahi_hsd", "B07", "B13", "https://noaa-himawari9.s3.amazonaws.com"),
    # KMA's own radiance-to-brightness-temperature coefficients from the file, not a plain Planck inversion
    "gk2a": Satellite("gk2a", "ami_l1b", "SW038", "IR105", "https://noaa-gk2a-pds.s3.amazonaws.com",
                      (("calib_mode", "FILE"),)),
}


def slot_floor(at: datetime) -> datetime:
    at = at.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return at - timedelta(minutes=at.minute % 10)


def published(name: str, slot: datetime, client: httpx.Client | None = None) -> bool:
    """Every file of the slot is on the bucket (HEAD requests only, stops at the first missing one)."""
    http = client or httpx
    return all(http.head(url, timeout=20).status_code == 200 for url in SATELLITES[name].urls(slot))


def download(sat: Satellite, slot: datetime, folder: Path, client: httpx.Client | None = None) -> list[Path]:
    http = client or httpx
    paths = []
    for url in sat.urls(slot):
        path = folder / url.rsplit("/", 1)[1]
        with http.stream("GET", url, timeout=120) as resp:
            if resp.status_code != 200:
                raise FileNotFoundError(f"{sat.name} {slot:%Y-%m-%d %H:%M} not on the bucket yet")
            with path.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
        paths.append(path)
    return paths


@lru_cache
def _peat_shapes() -> dict:
    """KHG peat inside each region (tools/build_regions.py), as prepared shapely geometries."""
    import shapely
    from shapely.geometry import shape
    data = json.loads(Path(__file__).with_name("regions_peat.geojson").read_text(encoding="utf-8"))
    shapes = {}
    for f in data["features"]:
        geom = shape(f["geometry"])
        shapely.prepare(geom)
        shapes[f["properties"]["id"]] = geom
    return shapes


_windows: dict[tuple, dict] = {}


def _region_windows(area, regions: tuple[Region, ...]) -> dict[str, tuple]:
    """Per region: array slice of its cell, and the inside-cell / on-peat masks. Cached per satellite grid."""
    import numpy as np
    import shapely

    key = (str(area.crs), area.shape, tuple(round(v, 1) for v in area.area_extent))
    if key in _windows:
        return _windows[key]
    peat = _peat_shapes()
    out = {}
    for r in regions:
        w, s, e, n = r.bounds
        edge_lon = np.concatenate([np.linspace(w, e, 15), np.linspace(w, e, 15), np.full(15, w), np.full(15, e)])
        edge_lat = np.concatenate([np.full(15, s), np.full(15, n), np.linspace(s, n, 15), np.linspace(s, n, 15)])
        cols, rows = area.get_array_indices_from_lonlat(edge_lon, edge_lat)
        cols, rows = np.ma.compressed(cols), np.ma.compressed(rows)
        if cols.size == 0:
            continue   # the cell is off this satellite's disk
        r0, r1 = max(int(rows.min()) - 1, 0), min(int(rows.max()) + 2, area.shape[0])
        c0, c1 = max(int(cols.min()) - 1, 0), min(int(cols.max()) + 2, area.shape[1])
        lon, lat = area.get_lonlats(data_slice=(slice(r0, r1), slice(c0, c1)))
        inside = np.isfinite(lon) & (lon >= w) & (lon < e) & (lat >= s) & (lat < n)
        on_peat = inside & shapely.contains_xy(peat[r.id], lon, lat) if r.id in peat else np.zeros_like(inside)
        out[r.id] = (slice(r0, r1), slice(c0, c1), inside, on_peat, lat, lon)
    _windows[key] = out
    return out


def read_slot(sat: Satellite, paths: list[Path], regions: tuple[Region, ...],
              cfg: ContextConfig = ContextConfig()) -> dict[str, RegionThermal]:
    """Decode one slot and score every region the satellite can see."""
    import dask
    import numpy as np
    # small chunks, set before satpy's readers are imported (they read it once): only the chunks under the regions
    # are decoded. On one vCPU (11 October slot) both satellites took ~50 s and 740 MB with dask's 128 MiB default,
    # ~20 s and ~200 MB with 4 MiB.
    dask.config.set({"array.chunk-size": DASK_CHUNK})
    from satpy import Scene

    scn = Scene(reader=sat.reader, filenames=[str(p) for p in paths], reader_kwargs=dict(sat.reader_kwargs))
    scn.load([sat.mwir, sat.lwir], calibration="brightness_temperature")
    windows = _region_windows(scn[sat.mwir].attrs["area"], regions)
    if not windows:
        return {}
    # one read per band of the block that holds every region: slicing per region would decode the files 130 times
    r0, r1 = min(w[0].start for w in windows.values()), max(w[0].stop for w in windows.values())
    c0, c1 = min(w[1].start for w in windows.values()), max(w[1].stop for w in windows.values())
    mwir = np.asarray(scn[sat.mwir][r0:r1, c0:c1].values, dtype="float64")
    lwir = np.asarray(scn[sat.lwir][r0:r1, c0:c1].values, dtype="float64")
    out = {}
    for region_id, (rows, cols, inside, on_peat, lat, lon) in windows.items():
        block = (slice(rows.start - r0, rows.stop - r0), slice(cols.start - c0, cols.stop - c0))
        out[region_id] = region_thermal(mwir[block][inside], lwir[block][inside], on_peat[inside], lat[inside],
                                        lon[inside], cfg)
    return out


def observe(name: str, slot: datetime, regions: tuple[Region, ...], cfg: ContextConfig = ContextConfig(),
            client: httpx.Client | None = None) -> dict[str, RegionThermal]:
    """One satellite, one slot: download to a temporary folder, decode, score. FileNotFoundError = not published."""
    sat = SATELLITES[name]
    with tempfile.TemporaryDirectory(prefix=f"{name}-") as folder:
        return read_slot(sat, download(sat, slot, Path(folder), client), regions, cfg)
