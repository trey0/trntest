"""Fetch DEM + ortho imagery from Lunaserv WMS for the ground footprint computed by `camera.build_camera`,
and prep the DEM for `sat_sim` (elevation, not raw radius; hole-filled). See docs/data-sources.md
and docs/caching.md.
"""

import dataclasses
import math
from pathlib import Path

import rasterio

from trntest import cache
from trntest.camera import Camera
from trntest.config import DEFAULT_MOON_RADIUS_M, TrntestConfig, load_config
from trntest.subprocess_utils import run_quiet


@dataclasses.dataclass(frozen=True)
class LunaservResult:
    """DEM/ortho tiles fetched for a `Camera`'s footprint, as returned by `fetch_dem_and_ortho`."""

    ortho: Path
    dem: Path
    bbox: tuple
    width: int
    height: int


def footprint_bbox_deg(footprint_lonlat):
    """Bounding box (minlon, minlat, maxlon, maxlat) of a camera's footprint corners. Longitudes are
    unwrapped onto a common branch (relative to the first corner) before taking min/max: LRO's
    near-polar orbit means a footprint can straddle the +-180 deg antimeridian, where a naive
    min/max would report a near-360 deg span instead of the true few-degree span on the other side.
    The resulting bbox may extend slightly outside [-180, 180]; Lunaserv's WMS handles that
    correctly -- confirmed empirically, an out-of-range bbox like (170, ..., 190) returns the same
    real, non-blank pixel data as the equivalent in-range request (-190, ..., -170)."""
    lons = [v[0] for v in footprint_lonlat.values() if v]
    lats = [v[1] for v in footprint_lonlat.values() if v]
    ref = lons[0]
    unwrapped_lons = [ref + (((lon - ref) + 180.0) % 360.0 - 180.0) for lon in lons]
    return min(unwrapped_lons), min(lats), max(unwrapped_lons), max(lats)


def pad_bbox(bbox, fraction):
    minx, miny, maxx, maxy = bbox
    dx, dy = (maxx - minx) * fraction, (maxy - miny) * fraction
    return (minx - dx, miny - dy, maxx + dx, maxy + dy)


def pixel_dims_for_gsd(bbox, target_gsd_m, moon_radius_m: float = DEFAULT_MOON_RADIUS_M):
    """Choose width/height (pixels) so both axes sample at ~target_gsd_m, accounting for the
    longitude/latitude physical-distance difference away from the equator (cos(lat) scaling)."""
    minx, miny, maxx, maxy = bbox
    lat_mid_rad = math.radians((miny + maxy) / 2.0)
    m_per_deg_lat = math.radians(1.0) * moon_radius_m
    m_per_deg_lon = m_per_deg_lat * math.cos(lat_mid_rad)

    width_m = (maxx - minx) * m_per_deg_lon
    height_m = (maxy - miny) * m_per_deg_lat
    width_px = max(64, round(width_m / target_gsd_m))
    height_px = max(64, round(height_m / target_gsd_m))
    return width_px, height_px


def radius_to_elevation(radius_tif_path, elevation_tif_path, moon_radius_m: float = DEFAULT_MOON_RADIUS_M):
    """Lunaserv's 'numeric_meters_absolute' DTM layer serves planetocentric radius (meters), not
    height above a datum -- subtract the reference radius so ASP sees a normal small-magnitude DEM."""
    with rasterio.open(radius_tif_path) as src:
        radius = src.read(1)
        profile = src.profile
    profile.update(count=1, dtype="float32", nodata=None)
    with rasterio.open(elevation_tif_path, "w", **profile) as dst:
        dst.write((radius - moon_radius_m).astype("float32"), 1)


def hole_fill_dem(dem_path, filled_path):
    run_quiet(
        [
            "dem_mosaic",
            str(dem_path),
            "--hole-fill-length",
            "50",
            "-o",
            str(filled_path).removesuffix("-tile-0.tif"),
        ]
    )


def fetch_dem_and_ortho(camera: Camera, config: TrntestConfig | None = None) -> LunaservResult:
    config = config or load_config()
    config.output_dir.mkdir(parents=True, exist_ok=True)

    bbox = pad_bbox(footprint_bbox_deg(camera.footprint_lonlat_deg), config.dem_padding_fraction)
    width, height = pixel_dims_for_gsd(bbox, config.dem_target_gsd_m, config.moon_radius_m)
    print(f"ROI bbox (lon/lat deg): {bbox}, size {width}x{height} px (~{config.dem_target_gsd_m} m/px)")

    ortho_path = cache.fetch_lunaserv_getmap(
        "luna_wac_global",
        bbox,
        width,
        height,
        cache_root=config.cache_root,
        srs=config.lunaserv_srs,
        base_url=config.lunaserv_base_url,
        fmt="image/tiff",
    )
    dem_radius_path = cache.fetch_lunaserv_getmap(
        "luna_wac_dtm_numeric_meters_absolute",
        bbox,
        width,
        height,
        cache_root=config.cache_root,
        srs=config.lunaserv_srs,
        base_url=config.lunaserv_base_url,
        fmt="image/tiff; mode=32bit",
    )

    dem_elevation_path = config.output_dir / "dem_elevation.tif"
    radius_to_elevation(dem_radius_path, dem_elevation_path, config.moon_radius_m)

    dem_filled_path = config.output_dir / "dem_filled-tile-0.tif"
    hole_fill_dem(dem_elevation_path, dem_filled_path)

    return LunaservResult(
        ortho=ortho_path,
        dem=dem_filled_path,
        bbox=bbox,
        width=width,
        height=height,
    )
