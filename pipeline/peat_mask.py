import re
import geopandas as gpd
import numpy as np
import shapely
import xarray as xr
from pyproj import Transformer


def _edges(c: np.ndarray) -> np.ndarray:
    """Pixel centres (1D, monotonic) -> pixel edges (length n+1)."""
    mid = (c[:-1] + c[1:]) / 2
    return np.concatenate([[c[0] - (mid[0] - c[0])], mid, [c[-1] + (c[-1] - mid[-1])]])


def build_cells(scene: xr.DataArray, bbox: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    """Cell = Himawari pixel inside the bbox. `scene` can come from one Himawari.get() (static coordinates)."""
    crs = scene.attrs["crs"]
    m = re.search(r"\+h=([\d.]+)", crs)
    if m is None:
        raise ValueError(f"Himawari CRS has no +h parameter: {crs}")
    h = float(m.group(1))
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    xs, ys = scene.x.values, scene.y.values

    # 1) bbox -> window index (sample the bbox edges so the x/y extremes are captured)
    w, s, e, n = bbox
    t = np.linspace(0, 1, 25)
    elon = np.concatenate([w + t * (e - w)] * 2 + [np.full(25, w), np.full(25, e)])
    elat = np.concatenate([np.full(25, s), np.full(25, n)] + [s + t * (n - s)] * 2)
    ex, ey = fwd.transform(elon, elat)
    ex, ey = np.asarray(ex) / h, np.asarray(ey) / h
    cols = np.flatnonzero((xs >= ex.min()) & (xs <= ex.max()))
    rows = np.flatnonzero((ys >= ey.min()) & (ys <= ey.max()))
    r0, r1, c0, c1 = rows[0], rows[-1], cols[0], cols[-1]

    # 2) pixel corners -> lon/lat (footprint polygon), pixel centres -> lon/lat
    xe, ye = _edges(xs)[c0:c1 + 2], _edges(ys)[r0:r1 + 2]
    XE, YE = np.meshgrid(xe, ye)
    LON, LAT = (np.asarray(a) for a in inv.transform(XE * h, YE * h))
    ring = np.stack([np.stack([LON[:-1, :-1], LAT[:-1, :-1]], -1), np.stack([LON[:-1, 1:], LAT[:-1, 1:]], -1),
                     np.stack([LON[1:, 1:], LAT[1:, 1:]], -1), np.stack([LON[1:, :-1], LAT[1:, :-1]], -1),
                     np.stack([LON[:-1, :-1], LAT[:-1, :-1]], -1)], axis=2).reshape(-1, 5, 2)
    XC, YC = np.meshgrid(xs[c0:c1 + 1], ys[r0:r1 + 1])
    clon, clat = (np.asarray(a).ravel() for a in inv.transform(XC * h, YC * h))
    rr, cc = np.meshgrid(np.arange(r0, r1 + 1), np.arange(c0, c1 + 1), indexing="ij")

    ok = np.isfinite(ring).all(axis=(1, 2)) & np.isfinite(clon) & np.isfinite(clat)
    out = gpd.GeoDataFrame(
        {"cell_id": [f"h{r}_{c}" for r, c in zip(rr.ravel()[ok], cc.ravel()[ok])],
         "row": rr.ravel()[ok], "col": cc.ravel()[ok], "lon": clon[ok], "lat": clat[ok]},
        geometry=shapely.polygons(ring[ok]), crs="EPSG:4326")
    return out


def peat_fraction(cells: gpd.GeoDataFrame, peat: gpd.GeoDataFrame, min_fraction: float = 0.5) -> gpd.GeoDataFrame:
    if cells.crs is None or peat.crs is None:
        raise ValueError("cells and peat must both have a CRS")
    minx, miny, maxx, maxy = cells.total_bounds
    peat = peat.to_crs(cells.crs).clip((minx, miny, maxx, maxy), keep_geom_type=True)  # drop peat outside the window first
    peat_union = peat.geometry.union_all() if hasattr(peat.geometry, "union_all") else peat.geometry.unary_union
    shapely.prepare(peat_union)

    geoms = cells.geometry.to_numpy()
    hit = shapely.intersects(geoms, peat_union)          # filter cepat (prepared geometry)
    fracs = np.zeros(len(cells))
    fracs[hit] = shapely.area(shapely.intersection(geoms[hit], peat_union)) / shapely.area(geoms[hit])

    out = cells.copy()
    out["peat_fraction"] = fracs
    return out[out["peat_fraction"] >= min_fraction].reset_index(drop=True)