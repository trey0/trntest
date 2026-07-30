"""Fetch DEM + ortho imagery from Lunaserv WMS for the ground footprint computed in Phase 2
(scripts/build_camera_from_spice.py), and prep the DEM for `sat_sim` (elevation, not raw radius;
hole-filled). See docs/data-sources.md and docs/caching.md.
"""
import math
import subprocess

import rasterio

from cache_utils import fetch_lunaserv_getmap, CACHE_ROOT
from build_camera_from_spice import build as build_camera

MOON_RADIUS_M = 1737400.0  # matches the ellipsoid radius Lunaserv's GeoTIFFs declare
SRS = "IAU2000:30100"  # confirmed empirically: plain geographic lon/lat, Moon sphere (see docs)
TARGET_GSD_M = 100.0  # matches native WAC/GLD100 resolution -- no point oversampling
PADDING_FRACTION = 0.3  # extend well beyond the camera's exact FOV, per ASP's DEM guidance


def footprint_bbox_deg(footprint_lonlat):
    lons = [v[0] for v in footprint_lonlat.values() if v]
    lats = [v[1] for v in footprint_lonlat.values() if v]
    return min(lons), min(lats), max(lons), max(lats)


def pad_bbox(bbox, fraction):
    minx, miny, maxx, maxy = bbox
    dx, dy = (maxx - minx) * fraction, (maxy - miny) * fraction
    return (minx - dx, miny - dy, maxx + dx, maxy + dy)


def pixel_dims_for_gsd(bbox, target_gsd_m):
    """Choose width/height (pixels) so both axes sample at ~target_gsd_m, accounting for the
    longitude/latitude physical-distance difference away from the equator (cos(lat) scaling)."""
    minx, miny, maxx, maxy = bbox
    lat_mid_rad = math.radians((miny + maxy) / 2.0)
    m_per_deg_lat = math.radians(1.0) * MOON_RADIUS_M
    m_per_deg_lon = m_per_deg_lat * math.cos(lat_mid_rad)

    width_m = (maxx - minx) * m_per_deg_lon
    height_m = (maxy - miny) * m_per_deg_lat
    width_px = max(64, round(width_m / target_gsd_m))
    height_px = max(64, round(height_m / target_gsd_m))
    return width_px, height_px


def radius_to_elevation(radius_tif_path, elevation_tif_path):
    """Lunaserv's 'numeric_meters_absolute' DTM layer serves planetocentric radius (meters), not
    height above a datum -- subtract the reference radius so ASP sees a normal small-magnitude DEM."""
    with rasterio.open(radius_tif_path) as src:
        radius = src.read(1)
        profile = src.profile
    profile.update(count=1, dtype="float32", nodata=None)
    with rasterio.open(elevation_tif_path, "w", **profile) as dst:
        dst.write((radius - MOON_RADIUS_M).astype("float32"), 1)


def hole_fill_dem(dem_path, filled_path):
    subprocess.run(
        [
            "dem_mosaic",
            str(dem_path),
            "--hole-fill-length", "50",
            "-o", str(filled_path).removesuffix("-tile-0.tif"),
        ],
        check=True,
    )


def fetch_dem_and_ortho(output_dir="/workspace/output"):
    info = build_camera(f"{output_dir}/camera_frame440.tsai")
    bbox = pad_bbox(footprint_bbox_deg(info["footprint_lonlat_deg"]), PADDING_FRACTION)
    width, height = pixel_dims_for_gsd(bbox, TARGET_GSD_M)
    print(f"ROI bbox (lon/lat deg): {bbox}, size {width}x{height} px (~{TARGET_GSD_M} m/px)")

    ortho_path = fetch_lunaserv_getmap("luna_wac_global", bbox, width, height, fmt="image/tiff", srs=SRS)
    dem_radius_path = fetch_lunaserv_getmap(
        "luna_wac_dtm_numeric_meters_absolute", bbox, width, height, fmt="image/tiff; mode=32bit", srs=SRS
    )

    dem_elevation_path = f"{output_dir}/dem_elevation.tif"
    radius_to_elevation(dem_radius_path, dem_elevation_path)

    dem_filled_prefix = f"{output_dir}/dem_filled"
    hole_fill_dem(dem_elevation_path, f"{dem_filled_prefix}-tile-0.tif")

    result = {
        "ortho": str(ortho_path),
        "dem": f"{dem_filled_prefix}-tile-0.tif",
        "bbox": bbox,
        "width": width,
        "height": height,
        "camera_info": info,
    }

    # run_sat_sim.sh needs the *exact* ortho/dem paths used for this footprint -- the cache dir
    # can hold tiles from multiple footprints (e.g. across frame-index changes), so it must not
    # glob for "any" cached ortho tile.
    with open(f"{output_dir}/lunaserv_result.txt", "w") as f:
        f.write(f"ORTHO={result['ortho']}\nDEM={result['dem']}\n")

    return result


if __name__ == "__main__":
    import json
    import os

    os.makedirs("/workspace/output", exist_ok=True)
    result = fetch_dem_and_ortho()
    print(json.dumps(result, indent=2, default=str))
