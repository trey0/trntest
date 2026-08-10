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
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds
from rasterio.windows import from_bounds as window_from_bounds
from rasterio.windows import transform as window_transform

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


def union_bbox(bbox1, bbox2):
    minx1, miny1, maxx1, maxy1 = bbox1
    minx2, miny2, maxx2, maxy2 = bbox2
    return min(minx1, minx2), min(miny1, miny2), max(maxx1, maxx2), max(maxy1, maxy2)


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


def fetch_dem_native(
    camera: Camera, config: TrntestConfig, extra_footprint_lonlat_deg: dict | None = None
) -> tuple[Path, tuple, int, int]:
    """**Deprecated** -- kept for reference/comparison, no longer called by `fetch_dem_and_ortho`'s
    default path (see `docs/history.md`'s dated entry). A second, axis-aligned crosshatch artifact
    was confirmed baked into Lunaserv's own native DTM tile itself (FFT-confirmed, present regardless
    of requested ppd/CRS/resampling kernel -- Lunaserv exposes no resampling control and no
    backing-store metadata, so it isn't fixable client-side). The live default DEM source is now
    `fetch_dem_astropedia`/`reproject_astropedia_elevation_to_local_grid`.

    Fetch the DTM layer in Lunaserv's native, unprojected geographic CRS (`config.lunaserv_dem_srs`,
    `IAU2000:30100`) at its real native resolution (`config.dem_native_ppd`) -- a fixed,
    unparametrized CRS the server needs no reprojection to serve, unlike the per-camera local
    Orthographic CRS (`IAU2000:30166`) `fetch_dem_and_ortho` requests the ortho in. Confirmed
    empirically (FFT/periodicity analysis of a live resolution sweep -- see docs/history.md's dated
    entry) that requesting this layer any finer than ~`dem_native_ppd` forces the server to
    interpolate past real detail, and that interpolation is what produced a real near-Nyquist
    checkerboard artifact once reprojected into an arbitrary rotated/offset local CRS -- fetching
    native and reprojecting locally (`reproject_dem_to_local_grid`) avoids both problems at once.

    Returns the fetched radius GeoTIFF path plus the exact degree bbox/pixel dimensions requested --
    `reproject_dem_to_local_grid` needs these to build the correct source transform itself, rather
    than trusting whatever georeferencing Lunaserv's GetMap response embeds (the existing pipeline
    already doesn't rely on that for the ortho/DEM fetches -- `radius_to_elevation`/`hole_fill_dem`
    just carry `src.profile` through unchanged -- so this continues that same pattern).

    `extra_footprint_lonlat_deg`, if given, is unioned in before padding -- same rationale as
    `fetch_dem_and_ortho`'s own parameter. Combined into one dict (not two separate `footprint_bbox_deg`
    calls unioned afterward) so antimeridian-unwrapping happens against one consistent reference
    corner -- two independent unwraps could each pick a different branch near +-180 deg and produce a
    bogus near-360-deg union."""
    combined_footprint = dict(camera.footprint_lonlat_deg)
    if extra_footprint_lonlat_deg is not None:
        combined_footprint.update({f"extra_{k}": v for k, v in extra_footprint_lonlat_deg.items()})
    deg_bbox = pad_bbox(footprint_bbox_deg(combined_footprint), config.dem_padding_fraction)
    minlon, minlat, maxlon, maxlat = deg_bbox
    width = max(64, round((maxlon - minlon) * config.dem_native_ppd))
    height = max(64, round((maxlat - minlat) * config.dem_native_ppd))
    path = cache.fetch_lunaserv_getmap(
        "luna_wac_dtm_numeric_meters_absolute",
        deg_bbox,
        width,
        height,
        cache_root=config.cache_root,
        srs=config.lunaserv_dem_srs,
        base_url=config.lunaserv_base_url,
        fmt="image/tiff; mode=32bit",
    )
    return path, deg_bbox, width, height


def _reproject_raster_to_local_grid(
    source_array: np.ndarray,
    src_crs: str,
    src_transform,
    dst_bbox_m,
    dst_width: int,
    dst_height: int,
    center_lon_deg: float,
    center_lat_deg: float,
    moon_radius_m: float,
    output_path,
    resampling: Resampling,
    tolerance: float,
    src_nodata: float | None = None,
    dst_nodata: float | None = None,
) -> Path:
    """Shared warp core behind both `reproject_dem_to_local_grid` (deprecated, Lunaserv-native
    source) and `reproject_astropedia_elevation_to_local_grid` (live default, Astropedia source) --
    reprojects any single-band source array/CRS/transform onto the per-camera local Orthographic
    working grid the ortho fetch already uses (`dst_bbox_m`/`dst_width`/`dst_height`, computed the
    same way for both -- see `fetch_dem_and_ortho`), via `rasterio.warp.reproject`, so the resampling
    method is one this project controls and picks explicitly, not any server's own opaque resampling.
    The destination Orthographic definition matches `orthographic_xy_m`'s own hand-verified forward
    projection math exactly (same center, same sphere radius, same projection family)."""
    dst_crs = f"+proj=ortho +lon_0={center_lon_deg} +lat_0={center_lat_deg} +R={moon_radius_m} +units=m +no_defs"
    dst_transform = transform_from_bounds(*dst_bbox_m, dst_width, dst_height)

    reprojected = np.full((dst_height, dst_width), np.nan, dtype="float32")
    reproject(
        source=source_array,
        destination=reprojected,
        src_transform=src_transform,
        src_crs=src_crs,
        src_nodata=src_nodata,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        dst_nodata=dst_nodata,
        resampling=resampling,
        tolerance=tolerance,
    )

    profile = {
        "driver": "GTiff",
        "height": dst_height,
        "width": dst_width,
        "count": 1,
        "dtype": "float32",
        "crs": dst_crs,
        "transform": dst_transform,
        "nodata": None,
    }
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(reprojected, 1)
    return Path(output_path)


def reproject_dem_to_local_grid(
    native_path,
    native_bbox_deg,
    native_width: int,
    native_height: int,
    dst_bbox_m,
    dst_width: int,
    dst_height: int,
    center_lon_deg: float,
    center_lat_deg: float,
    moon_radius_m: float,
    output_path,
    resampling: Resampling = Resampling.cubic,
    tolerance: float = 0.125,
) -> Path:
    """**Deprecated** -- kept for reference/comparison alongside `fetch_dem_native` (see that
    function's own docstring for why, and `docs/history.md`'s dated entry). Behavior unchanged from
    before this function's warp core was factored out into `_reproject_raster_to_local_grid`.

    Reproject a native-CRS DTM array (`fetch_dem_native`'s output) onto the same per-camera local
    Orthographic working grid the ortho fetch already uses -- entirely locally, so the resampling
    method is one this project controls and picks explicitly (`resampling`, exposed as a parameter
    specifically so alternatives can be compared -- see docs/history.md's dated entry), not
    Lunaserv's own opaque server-side resampling.

    Both CRSs are expressed as generic PROJ4 strings with the Moon's own spherical radius, rather than
    relying on GDAL/PROJ recognizing Lunaserv's `IAU2000:*` codes by name (untested/unnecessary --
    Orthographic and plain geographic are both standard PROJ operations once parametrized this way).
    This removed the original near-Nyquist server-side resampling artifact, but the resampling kernel
    used *here* still matters -- an ~2.4x upsample (native ~237m/px -> a 100m/px working grid) through
    a smooth reconstruction kernel can itself introduce a small periodic curvature ripple at the
    native sample spacing, invisible in the raw elevation but visible once `hillshade`'s
    finite-differencing (which is sensitive to slope, i.e. the reconstruction's derivative) amplifies
    it -- see docs/history.md's dated entry for the empirical comparison that picked `resampling`'s
    current default, and for why this artifact turned out not to be fully fixable this way at all."""
    with rasterio.open(native_path) as src:
        native_radius = src.read(1)

    minlon, minlat, maxlon, maxlat = native_bbox_deg
    src_crs = f"+proj=longlat +R={moon_radius_m} +no_defs"
    src_transform = transform_from_bounds(minlon, minlat, maxlon, maxlat, native_width, native_height)

    return _reproject_raster_to_local_grid(
        native_radius,
        src_crs,
        src_transform,
        dst_bbox_m,
        dst_width,
        dst_height,
        center_lon_deg,
        center_lat_deg,
        moon_radius_m,
        output_path,
        resampling=resampling,
        tolerance=tolerance,
    )


# Confirmed via `gdalinfo`'s own corner coordinates on the real file (79d0'6.57" both ways): the
# real coverage of Astropedia's flat-file GLD100 DEM (`config.astropedia_gld100_url`). No silent
# fallback to the deprecated Lunaserv-native path for footprints beyond this -- see
# `astropedia_coverage_bbox_deg`.
ASTROPEDIA_MAX_ABS_LATITUDE_DEG = 79.0


DEM_FETCH_SAFETY_MARGIN_FRACTION = 0.02


def astropedia_coverage_bbox_deg(
    dst_bbox_m: tuple, center_lon_deg: float, center_lat_deg: float, moon_radius_m: float
) -> tuple:
    """The real lon/lat degree bbox needed to fully cover `dst_bbox_m` (the local-Orthographic
    working grid's own bbox, meters -- see `fetch_dem_and_ortho`) once reprojected, plus a small
    extra `DEM_FETCH_SAFETY_MARGIN_FRACTION` pad for the resampling kernel's own footprint (bilinear
    needs real neighbor samples just past the exact destination edge, not just up to it) -- and the
    coverage guard: raises `ValueError` if the result extends beyond `ASTROPEDIA_MAX_ABS_LATITUDE_DEG`.

    **Derived directly from `dst_bbox_m`'s own boundary** (`rasterio.warp.transform_bounds` densely
    samples the whole edge, not just the 4 corners), *not* by independently padding a degree-space
    bbox around the footprint's own corners the way this function used to. Confirmed live (see
    docs/history.md's dated entry) that two independently-padded bboxes -- one in degrees, one in
    local-Orthographic meters, as this function and `fetch_dem_and_ortho`'s own `dst_bbox_m` used to
    compute separately -- aren't guaranteed to cover each other: a square's diagonal corners are
    ~41% farther from center than its edge midpoints, so an independent degree-space padding, even a
    generous one, can undershoot the destination grid's own corners -- leaving small but real nodata
    triangles exactly there, regardless of how large `dem_padding_fraction` is, since that padding
    was never the thing being undershot. Deriving the degree bbox from `dst_bbox_m` directly instead
    makes that mismatch structurally impossible: there's only one padded bbox now, not two.

    No automatic fallback to the deprecated Lunaserv path -- a caller that wants one has to ask for
    it explicitly."""
    padded_bbox_m = pad_bbox(dst_bbox_m, DEM_FETCH_SAFETY_MARGIN_FRACTION)
    geo_crs = f"+proj=longlat +R={moon_radius_m} +no_defs"
    ortho_crs = f"+proj=ortho +lon_0={center_lon_deg} +lat_0={center_lat_deg} +R={moon_radius_m} +units=m +no_defs"
    minlon, minlat, maxlon, maxlat = transform_bounds(ortho_crs, geo_crs, *padded_bbox_m)
    if minlat < -ASTROPEDIA_MAX_ABS_LATITUDE_DEG or maxlat > ASTROPEDIA_MAX_ABS_LATITUDE_DEG:
        raise ValueError(
            f"Camera footprint's padded AOI (latitude range {minlat:.2f}..{maxlat:.2f} deg) extends "
            f"beyond Astropedia's GLD100 flat file's real +-{ASTROPEDIA_MAX_ABS_LATITUDE_DEG} deg "
            "coverage -- no DEM data available there from this source. The deprecated Lunaserv-native "
            "path (lunaserv.fetch_dem_native/reproject_dem_to_local_grid) covers this latitude range "
            "but has its own known, unfixed artifact -- see docs/history.md's dated entry -- and isn't "
            "used automatically here."
        )
    return minlon, minlat, maxlon, maxlat


def fetch_dem_astropedia(
    dst_bbox_m: tuple, center_lon_deg: float, center_lat_deg: float, config: TrntestConfig
) -> tuple[Path, tuple]:
    """Live default DEM source: ensure Astropedia's flat-file GLD100 DEM is downloaded/cached locally
    (`cache.fetch_astropedia_gld100` -- the whole ~10GB file, once, resumably; see its own docstring
    for why this doesn't fetch a remote AOI window directly: the file isn't a Cloud-Optimized
    GeoTIFF, so a remote windowed read pulls full-width row strips, confirmed slow in testing).

    `dst_bbox_m` is `fetch_dem_and_ortho`'s own already-padded (and, if applicable, already unioned
    with `extra_footprint_lonlat_deg`) local-Orthographic working-grid bbox -- passed in directly
    (not re-derived from the raw camera footprint) so there's exactly one padded AOI decision, not
    two independent ones (see `astropedia_coverage_bbox_deg`'s docstring for why that used to cause
    real corner nodata gaps).

    Returns the local cached file path plus the degree bbox that covers it
    (`astropedia_coverage_bbox_deg`, which also raises if the footprint needs data outside the
    file's real coverage) -- `reproject_astropedia_elevation_to_local_grid` needs the bbox to know
    which AOI window to read from the (large, local) file."""
    deg_bbox = astropedia_coverage_bbox_deg(dst_bbox_m, center_lon_deg, center_lat_deg, config.moon_radius_m)
    path = cache.fetch_astropedia_gld100(config.cache_root, config.astropedia_gld100_url)
    return path, deg_bbox


def reproject_astropedia_elevation_to_local_grid(
    astropedia_path,
    deg_bbox,
    dst_bbox_m,
    dst_width: int,
    dst_height: int,
    center_lon_deg: float,
    center_lat_deg: float,
    moon_radius_m: float,
    output_path,
    resampling: Resampling = Resampling.bilinear,
    tolerance: float = 0.125,
) -> Path:
    """Read just the AOI (`deg_bbox`, from `fetch_dem_astropedia`) from the local cached Astropedia
    file and reproject it onto the same per-camera local Orthographic working grid
    `reproject_dem_to_local_grid` uses -- fast now (no network, no row-strip-over-HTTP penalty),
    unlike a remote `/vsicurl/` windowed read of the same file. Uses the file's own embedded
    `crs`/`transform` directly rather than hardcoding Astropedia's Equidistant Cylindrical PROJ4
    parameters by hand -- more robust, and this file (unlike Lunaserv's GetMap responses) actually
    has trustworthy embedded georeferencing.

    This data is already real elevation (Int16 meters, confirmed via `gdalinfo`: nodata -32768),
    *not* planetocentric radius like Lunaserv's DTM layer -- `radius_to_elevation` is skipped
    entirely for this path; the reprojected output here is elevation directly."""
    with rasterio.open(astropedia_path) as src:
        src_crs = src.crs
        src_nodata = src.nodata
        minlon, minlat, maxlon, maxlat = deg_bbox
        geo_crs = f"+proj=longlat +R={moon_radius_m} +no_defs"
        left, bottom, right, top = transform_bounds(geo_crs, src_crs, minlon, minlat, maxlon, maxlat)
        window = window_from_bounds(left, bottom, right, top, transform=src.transform)
        src_transform = window_transform(window, src.transform)
        elevation = src.read(1, window=window)

    return _reproject_raster_to_local_grid(
        elevation,
        src_crs,
        src_transform,
        dst_bbox_m,
        dst_width,
        dst_height,
        center_lon_deg,
        center_lat_deg,
        moon_radius_m,
        output_path,
        resampling=resampling,
        tolerance=tolerance,
        src_nodata=src_nodata,
        dst_nodata=np.nan,
    )


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


def fetch_dem_and_ortho(
    camera: Camera, config: TrntestConfig | None = None, extra_footprint_lonlat_deg: dict | None = None
) -> LunaservResult:
    """`extra_footprint_lonlat_deg`, if given, is unioned into the fetch AOI alongside `camera`'s
    own footprint before padding -- e.g. `tie_points.crop_footprint_corners_for_camera`'s real WAC
    crop footprint, which isn't always the same size/shape as the synthetic camera's own FOV. Keeps
    the DEM/ortho fetch big enough to cover both Phase 5 and Phase 6's real ground needs in one
    request, rather than risking a real-WAC display later running past the edge of what was
    actually fetched here.

    `extra_footprint_lonlat_deg` is a ray-traced estimate (`crop_footprint_corners`'s idealized
    ±half-angle rays at exactly 2 along-track positions), not the real mapprojected WAC crop's own
    actual extent (only knowable after `isis_wac.run_mapproject`, which itself needs this fetch to
    already exist -- a genuine chicken-and-egg constraint the real crop can drift slightly past on
    one edge). Tried doubling `dem_padding_fraction`'s effect specifically on this side to close
    that gap -- confirmed empirically it doesn't actually help (the margin on the tight edge stayed
    ~0 regardless of 261km vs. 410km total fetch width, since the drift is directional/asymmetric,
    not just "not enough padding") while meaningfully increasing fetch time (a real WMS timeout hit
    during testing) -- reverted. The remaining worst-case gap measured ~120m, close to a single
    ~100m/px DEM pixel -- not the comfortable margin `pad_bbox` gives everywhere else, but not a
    meaningfully visible nodata gap in practice either."""
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
    unpadded_bbox = footprint_bbox_local_m(camera.footprint_lonlat_deg, center_lon, center_lat, config.moon_radius_m)
    if extra_footprint_lonlat_deg is not None:
        unpadded_bbox = union_bbox(
            unpadded_bbox,
            footprint_bbox_local_m(extra_footprint_lonlat_deg, center_lon, center_lat, config.moon_radius_m),
        )
    bbox = pad_bbox(unpadded_bbox, config.dem_padding_fraction)
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
    # The DEM itself is *not* fetched from Lunaserv at all -- live default source is USGS Astropedia's
    # flat-file GLD100 (see `docs/data-sources.md`'s "Astropedia GLD100 flat file" section and
    # `docs/history.md`'s dated entry): Lunaserv's DTM layer was confirmed to have a real, unfixable
    # (client-side) crosshatch artifact baked into its own native tile, regardless of requested
    # ppd/CRS/resampling kernel -- Astropedia's flat file has no such artifact. `fetch_dem_astropedia`
    # ensures the whole ~10GB file is downloaded/cached locally once (raises if this camera's
    # footprint needs data outside the file's real +-79 deg latitude coverage -- no silent fallback to
    # the deprecated Lunaserv-native path below), then `reproject_astropedia_elevation_to_local_grid`
    # reads just this AOI from the local file and reprojects it onto this same local-CRS grid --
    # already real elevation (not planetocentric radius), so `radius_to_elevation` is skipped.
    astropedia_path, astropedia_deg_bbox = fetch_dem_astropedia(bbox, center_lon, center_lat, config)
    dem_elevation_path = config.output_dir / "dem_elevation.tif"
    reproject_astropedia_elevation_to_local_grid(
        astropedia_path,
        astropedia_deg_bbox,
        bbox,
        width,
        height,
        center_lon,
        center_lat,
        config.moon_radius_m,
        dem_elevation_path,
    )

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
