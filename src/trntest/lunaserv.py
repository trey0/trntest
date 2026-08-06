"""Fetch DEM + ortho imagery from Lunaserv WMS for the ground footprint computed by `camera.build_camera`,
and prep both for `sat_sim`: the DEM as elevation (not raw radius) and hole-filled, the ortho
despeckled and blended with a real-sun-lit hillshade (`sat_sim` applies no illumination model of its
own -- see docs/data-sources.md -- so any relief in the synthetic render has to already be in this
ortho). See docs/data-sources.md and docs/caching.md.
"""

import dataclasses
import math
from pathlib import Path

import numpy as np
import rasterio
from matplotlib.colors import LightSource

from trntest import cache, illumination
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


def despeckle(data: np.ndarray, size: int = 3, n_mad: float = 6.0) -> np.ndarray:
    """Replace isolated single-pixel outliers with their local neighborhood median, leaving smooth
    terrain and large real features (e.g. a genuinely bright/saturated crater) untouched. A pixel is
    flagged only when it deviates from its `size`x`size` neighborhood median by more than `n_mad`
    scaled median-absolute-deviations *of that same neighborhood* -- this makes the threshold
    self-scaling to local contrast, and specifically means a pixel next to a real edge/large feature
    (where the neighborhood's own MAD is already high) is far less likely to be flagged than an
    isolated pixel sitting in otherwise-smooth terrain. Validated against real fetched Lunaserv WAC
    tiles (see docs/data-sources.md): ~90% of statistical outliers under this test are genuinely
    isolated single pixels (no adjacent outlier), and a known real saturated-crater blob in that data
    is untouched by design (its neighborhood MAD is not small)."""
    pad = size // 2
    padded = np.pad(data, pad, mode="edge")
    neighborhood = np.lib.stride_tricks.sliding_window_view(padded, (size, size)).reshape(*data.shape, -1)
    med = np.median(neighborhood, axis=-1)
    mad = np.median(np.abs(neighborhood - med[..., None]), axis=-1) * 1.4826  # normal-consistent scale
    is_outlier = np.abs(data.astype(np.float64) - med) > n_mad * np.maximum(mad, 1.0)
    return np.where(is_outlier, med, data).astype(data.dtype)


def shade_ortho(
    ortho: np.ndarray, dem: np.ndarray, azimuth_deg: float, elevation_deg: float, cellsize_m: float
) -> np.ndarray:
    """Blend a hillshade -- lit from the real sun direction for this camera/epoch, computed from
    `dem` -- onto `ortho`. `sat_sim` applies no illumination model of its own; it geometrically
    reprojects whatever's already in the ortho (see docs/data-sources.md), so any relief in the
    synthetic render has to come from here. A direct multiply, not `0.5 + 0.5 * hillshade` (an
    earlier version's artificial floor that halved the shading term's usable dynamic range and made
    the render look washed out relative to real WAC imagery) -- terrain facing away from the sun
    should be able to render genuinely dark, not floored at ~50% gray. This is still just local
    per-facet (Lambertian) shading, not real cast-shadow occlusion from other terrain, which remains
    out of scope (see docs/data-sources.md)."""
    light = LightSource(azdeg=azimuth_deg, altdeg=elevation_deg)
    hillshade = light.hillshade(dem.astype(np.float64), dx=cellsize_m, dy=cellsize_m)
    ortho_norm = ortho.astype(np.float64) / 255.0
    blended = ortho_norm * hillshade
    return np.clip(blended * 255.0, 0, 255).astype(np.uint8)


def despeckle_and_shade_ortho(ortho_path, dem_path, camera: Camera, output_path, config: TrntestConfig) -> None:
    """Despeckle the raw fetched ortho and blend in a real-sun hillshade computed from the (already
    hole-filled) DEM, writing the result to `output_path` -- the single ortho used by both `sat_sim`
    and every display panel (see `fetch_dem_and_ortho`)."""
    with rasterio.open(ortho_path) as src:
        ortho = src.read(1)
        profile = src.profile
    with rasterio.open(dem_path) as src:
        dem = src.read(1)

    cleaned = despeckle(ortho)

    center = camera.footprint_lonlat_deg["center"]
    assert center is not None, "camera's nadir footprint center must be a real ground point"
    center_lon, center_lat = center
    azimuth_deg, elevation_deg = illumination.sun_azimuth_elevation_deg(center_lon, center_lat, camera.et)
    shaded = shade_ortho(cleaned, dem, azimuth_deg, elevation_deg, config.dem_target_gsd_m)

    profile.update(count=1, dtype="uint8")
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(shaded, 1)


def fetch_dem_and_ortho(camera: Camera, config: TrntestConfig | None = None) -> LunaservResult:
    config = config or load_config()
    config.output_dir.mkdir(parents=True, exist_ok=True)

    bbox = pad_bbox(footprint_bbox_deg(camera.footprint_lonlat_deg), config.dem_padding_fraction)
    width, height = pixel_dims_for_gsd(bbox, config.dem_target_gsd_m, config.moon_radius_m)
    print(f"ROI bbox (lon/lat deg): {bbox}, size {width}x{height} px (~{config.dem_target_gsd_m} m/px)")

    ortho_path = cache.fetch_lunaserv_getmap(
        config.lunaserv_ortho_layer,
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

    ortho_shaded_path = config.output_dir / "ortho_shaded.tif"
    despeckle_and_shade_ortho(ortho_path, dem_filled_path, camera, ortho_shaded_path, config)

    return LunaservResult(
        ortho=ortho_shaded_path,
        dem=dem_filled_path,
        bbox=bbox,
        width=width,
        height=height,
    )
