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
    """DEM/ortho tiles fetched for a `Camera`'s footprint, as returned by `fetch_dem_and_ortho`.
    `bbox` is in meters, in the per-camera local Orthographic CRS (`config.lunaserv_srs_template`)
    both tiles were fetched in -- not lon/lat degrees (each `LunaservResult`'s tiles have their own
    independent local CRS, centered on that camera's own footprint)."""

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


def orthographic_xy_m(lon_deg, lat_deg, center_lon_deg, center_lat_deg, radius_m: float = DEFAULT_MOON_RADIUS_M):
    """Forward spherical Orthographic projection (meters) of `(lon_deg, lat_deg)` relative to a
    local tangent point `(center_lon_deg, center_lat_deg)` -- matches Lunaserv's `IAU2000:30166`
    layer projection exactly (same formula, same Moon radius), so a bbox computed here lines up
    with what the WMS server actually renders. Standard formula (e.g. Snyder 1987 eq. 20-3/20-4)."""
    lon, lat = math.radians(lon_deg), math.radians(lat_deg)
    lon0, lat0 = math.radians(center_lon_deg), math.radians(center_lat_deg)
    x = radius_m * math.cos(lat) * math.sin(lon - lon0)
    y = radius_m * (math.cos(lat0) * math.sin(lat) - math.sin(lat0) * math.cos(lat) * math.cos(lon - lon0))
    return x, y


def footprint_bbox_local_m(footprint_lonlat, center_lon_deg, center_lat_deg, radius_m: float = DEFAULT_MOON_RADIUS_M):
    """Bounding box (minx, miny, maxx, maxy), in meters, of a camera's footprint corners under the
    local Orthographic projection centered at `(center_lon_deg, center_lat_deg)` -- the metric
    counterpart of `footprint_bbox_deg`, used to size the WMS request against Lunaserv's
    `IAU2000:30166` local-CRS layers (see `fetch_dem_and_ortho`). No antimeridian-unwrapping
    special case is needed here (unlike `footprint_bbox_deg`): the projection's own sin/cos terms
    are already continuous across any longitude difference."""
    corners = [v for v in footprint_lonlat.values() if v is not None]
    xy = [orthographic_xy_m(lon, lat, center_lon_deg, center_lat_deg, radius_m) for lon, lat in corners]
    xs = [x for x, _ in xy]
    ys = [y for _, y in xy]
    return min(xs), min(ys), max(xs), max(ys)


def pixel_dims_for_gsd(bbox, target_gsd_m):
    """Choose width/height (pixels) so both axes sample at ~target_gsd_m. `bbox` is expected to
    already be in physical meters (e.g. `footprint_bbox_local_m`'s output) -- unlike the old
    lon/lat-degree bbox this replaced, no cos(lat) correction is needed here since the local
    Orthographic CRS's axes are already isotropic in meters."""
    minx, miny, maxx, maxy = bbox
    width_px = max(64, round((maxx - minx) / target_gsd_m))
    height_px = max(64, round((maxy - miny) / target_gsd_m))
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

    center = camera.footprint_lonlat_deg["center"]
    assert center is not None, "camera's nadir footprint center must be a real ground point"
    center_lon, center_lat = center
    # A per-camera local Orthographic CRS (Lunaserv's `IAU2000:30166`, parametrized by this
    # footprint's own center) rather than Lunaserv's native unprojected geographic grid
    # (`IAU2000:30100`) -- the geographic grid's degree-pixels are anisotropic away from the
    # equator (a degree of longitude covers less ground distance than a degree of latitude), and
    # ASP's `mapproject --ref-map` (see `render.run_mapproject`) turned out not to preserve that
    # anisotropy -- it copies the reference grid's x-resolution onto the y-axis too, silently
    # stretching any `--ref-map`'d output vertically by up to `1/cos(lat)`. A local Orthographic
    # projection has genuinely square meter pixels everywhere, so that mismatch (x-res != y-res on
    # the reference grid) can't arise in the first place. Confirmed empirically (see
    # docs/data-sources.md): `IAU2000:30166` reports the Moon's real 1,737,400 m radius (unlike the
    # generic OGC `AUTO:42003` Orthographic code, which is hardcoded to Earth's WGS84 ellipsoid).
    srs = config.lunaserv_srs_template.format(c_lon=center_lon, c_lat=center_lat)
    bbox = pad_bbox(
        footprint_bbox_local_m(camera.footprint_lonlat_deg, center_lon, center_lat, config.moon_radius_m),
        config.dem_padding_fraction,
    )
    width, height = pixel_dims_for_gsd(bbox, config.dem_target_gsd_m)
    print(f"ROI center (lon,lat deg): {center}, bbox (local m): {bbox}")
    print(f"ROI size {width}x{height} px (~{config.dem_target_gsd_m} m/px)")

    ortho_path = cache.fetch_lunaserv_getmap(
        config.lunaserv_ortho_layer,
        bbox,
        width,
        height,
        cache_root=config.cache_root,
        srs=srs,
        base_url=config.lunaserv_base_url,
        fmt="image/tiff",
    )
    dem_radius_path = cache.fetch_lunaserv_getmap(
        "luna_wac_dtm_numeric_meters_absolute",
        bbox,
        width,
        height,
        cache_root=config.cache_root,
        srs=srs,
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
