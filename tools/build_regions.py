"""Build the observation regions: NASA POWER grid cells (0.5° x 0.625°) that hold peat in the monitored provinces.

One region = one POWER cell, so every region gets exactly one value per parameter and hour. A POWER point
request at the region centre returns that cell. Output:
- asapify_engine/regions.json: id, centre, bounds, province, peat area (read by the engine at runtime)
- a GeoJSON of the peat inside each region, simplified, for the frontend map

Tool-only dependencies: pip install shapely pyshp
Usage: python tools/build_regions.py KHG_SHAPEFILE_BASE PROVINCES_GEOJSON OUT_REGIONS_JSON OUT_REGIONS_GEOJSON
"""
from __future__ import annotations

import json
import math
import sys

import shapefile
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

LAT_STEP, LON_STEP = 0.5, 0.625   # MERRA-2 grid used by NASA POWER for skin temperature and humidity
MIN_PEAT_KM2 = 25.0               # skip slivers that would decide a whole cell's status on a few pixels
KM_PER_DEG = 111.32


def region_id(lat: float, lon: float) -> str:
    return f"R{abs(lat):.2f}{'S' if lat < 0 else 'N'}{lon:.3f}E"


def area_km2(geom, lat: float) -> float:
    return geom.area * KM_PER_DEG * KM_PER_DEG * math.cos(math.radians(lat))


def main(khg_base: str, provinces_path: str, out_json: str, out_geojson: str) -> None:
    peat = unary_union([shape(s.__geo_interface__) for s in shapefile.Reader(khg_base).shapes()]).buffer(0)
    provinces = [(f["properties"]["code"], shape(f["geometry"]).buffer(0))
                 for f in json.load(open(provinces_path, encoding="utf-8"))["features"]]
    monitored = peat.intersection(unary_union([g for _, g in provinces]))
    west, south, east, north = monitored.bounds

    regions, features = [], []
    for i in range(math.floor(south / LAT_STEP), math.ceil(north / LAT_STEP) + 1):
        lat = round(i * LAT_STEP, 3)
        for j in range(math.floor(west / LON_STEP), math.ceil(east / LON_STEP) + 1):
            lon = round(j * LON_STEP, 4)
            cell = box(lon - LON_STEP / 2, lat - LAT_STEP / 2, lon + LON_STEP / 2, lat + LAT_STEP / 2)
            piece = monitored.intersection(cell)
            if piece.is_empty or area_km2(piece, lat) < MIN_PEAT_KM2:
                continue
            province = max(provinces, key=lambda p: piece.intersection(p[1]).area)[0]
            rid = region_id(lat, lon)
            regions.append({"id": rid, "lat": lat, "lon": lon,
                            "bounds": [round(v, 4) for v in cell.bounds],
                            "province": province, "peat_km2": round(area_km2(piece, lat), 1)})
            features.append({"type": "Feature", "properties": {"id": rid, "province": province},
                             "geometry": mapping(piece.simplify(0.005, preserve_topology=True))})

    regions.sort(key=lambda r: r["id"])
    with open(out_json, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"grid": "NASA POWER MERRA-2 0.5deg x 0.625deg", "source": "KHG indicative peat map (KLHK)",
                   "min_peat_km2": MIN_PEAT_KM2, "regions": regions}, fh, indent=1)
        fh.write("\n")
    with open(out_geojson, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"type": "FeatureCollection", "features": features}, fh, separators=(",", ":"))
    by_province: dict[str, int] = {}
    for r in regions:
        by_province[r["province"]] = by_province.get(r["province"], 0) + 1
    print(f"{len(regions)} regions, {sum(r['peat_km2'] for r in regions):.0f} km² of peat; per province: {by_province}")


if __name__ == "__main__":
    main(*sys.argv[1:5])
