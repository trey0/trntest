"""Fetch DEM + ortho imagery for the ground footprint computed by `camera.build_camera`, and prep both
for `sat_sim`: the DEM as elevation (not raw radius) and hole-filled, the ortho despeckled and blended
with a sun-lit hillshade (see `shade_ortho`'s own trailing comment for why the hillshade has to be
baked in here). Live defaults: Astropedia's GLD100 DEM (`fetch_dem_astropedia`) and WAC_EMP's PDS4
reflectance ortho (`fetch_wac_emp_reflectance`); Lunaserv WMS (`fetch_dem_native`,
`ortho_source="lunaserv_wms"`) is a deprecated fallback kept for comparison. See
docs/data-sources/astropedia-gld100.md, docs/data-sources/wac-emp-pds4.md,
docs/data-sources/lunaserv-wms.md, and docs/caching.md.
"""

import dataclasses
import math
import tempfile
import warnings
from pathlib import Path

import numpy as np
import rasterio
from matplotlib.colors import LightSource
from rasterio.errors import NotGeoreferencedWarning
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import Resampling, reproject, transform, transform_bounds
from rasterio.windows import from_bounds as window_from_bounds
from rasterio.windows import transform as window_transform

from trntest import cache, illumination
from trntest.camera import Camera
from trntest.config import MOON_RADIUS_M, TrntestConfig, load_config
from trntest.product_registry import atomic_publish, atomic_publish_prefix, writes_product
from trntest.subprocess_utils import run_quiet

# Placeholder Hapke-Henyey-Greenstein coefficients for `hapke_shade_ortho` -- illustrative values in
# each parameter's documented valid range (see ISIS's photomet.xml), not calibrated against lunar
# photometry. Kept as the `real_hapke_params=False` fallback -- see `fetch_real_hapke_params()` below
# for the calibrated alternative.
_HAPKE_PLACEHOLDER_PARAMS = {"wh": 0.52, "hg1": 0.213, "hg2": 1.0, "hh": 0.17, "b0": 0.025, "theta": 0.0}

# The ortho texture this project fetches (`config.lunaserv_ortho_layer`, WAC_EMP's own PDS4 tile) is
# itself a photometric composite, not raw albedo: every pixel is normalized to a reference geometry of
# 30 deg incidence, 0 deg emission, 30 deg phase, using an empirical photometric function (Boyd et al.
# 2012), not Hapke. `hapke_shade_ortho` relights it for a candidate's own geometry by multiplying by
# H(i,e,g)/H(these reference angles), where H is this project's own Hapke evaluation
# (`_hapke_reflectance`) -- an approximation of undoing Boyd et al.'s normalization, not a bit-exact
# inverse. The Sato et al. (2014) Hapke parameters this project samples (`fetch_real_hapke_params`)
# were themselves cross-validated against this same WAC photometric dataset, so this isn't an
# arbitrary stand-in.
REFERENCE_INCIDENCE_DEG = 30.0
REFERENCE_EMISSION_DEG = 0.0
REFERENCE_PHASE_DEG = 30.0

# ISIS's own spatially-resolved lunar Hapke calibration (Sato et al. 2014's fit, converted to ISIS's
# native parameterization) -- $ISISDATA/lro/calibration/
# WAC_global_7bands_1x1_wbhs70NS_const_each_pole.<version>.cub, a 63-band (7 wavelengths x 9 params)
# global 1deg/px cube. Already part of this project's `isis_wac.ensure_isisdata` fetch (the `lro` ISIS
# data package `lrowaccal`/`spiceinit` already need), not a new download. See
# `fetch_real_hapke_params()` below.
_HAPKE_CALIBRATION_WAVELENGTHS_NM = (321, 360, 415, 566, 604, 643, 689)
_HAPKE_CALIBRATION_PARAM_ORDER = ("wh", "hg1", "hg2", "bc0", "hc", "b0", "hh", "theta", "phi")
_HAPKE_CALIBRATION_CUBE_GLOB = "WAC_global_7bands_1x1_wbhs70NS_const_each_pole.*.cub"
# Matches `config.lunaserv_ortho_layer`'s own wavelength (`luna_wac_normalized_reflectance`, 643nm --
# see docs/data-sources/lunaserv-wms.md) -- the calibration should describe the same imagery being
# shaded.
DEFAULT_HAPKE_CALIBRATION_WAVELENGTH_NM = 643

# `fetch_dem_and_ortho`/`despeckle_and_shade_ortho`'s own `hapke`/`along_track_correction`/
# `real_hapke_params` parameter defaults -- shared with `trn_dataset.TrnTestEntry.dem_ortho_result`'s
# resumption check (via `ortho_shaded_filename` below) so the two can't disagree about which shading
# mode's cached ortho file is "the" default one to resume from. `shade_ortho`'s plain Lambertian blend
# (`hapke=False`), the uncorrected per-pixel geometry (`along_track_correction=False`), and the
# illustrative placeholder Hapke coefficients (`real_hapke_params=False`) all remain available as
# explicit fallbacks.
#
# The DEM-gradient normal-tilt correction (`_terrain_photometric_angles`) and the Hapke-ratio
# relighting correction (`hapke_shade_ortho`) are unconditional, with no parameter to opt out: both are
# judged physically correct on their own terms (independent `campt` validation for the normal-tilt fix;
# WAC_EMP's own documented i=30/e=0/g=30 normalization convention for the Hapke-ratio fix), even though
# neither is confirmed to improve -- and the Hapke-ratio fix is confirmed to worsen -- the
# brightness-matched diff against a WAC crop for the one candidate tested so far. That regression
# remains open and unexplained (see `docs/proposed-tasks/open-items.md`).
DEFAULT_HAPKE_SHADING = True
DEFAULT_ALONG_TRACK_CORRECTION = True
DEFAULT_REAL_HAPKE_PARAMS = True

# `fetch_dem_and_ortho`'s ortho-texture source. "wac_emp_pds" (live default) fetches WAC_EMP's own
# reflectance directly from its PDS4 archive (`fetch_wac_emp_reflectance`/
# `reproject_wac_emp_reflectance_to_local_grid`) -- physical reflectance, no embedded display stretch.
# "lunaserv_wms" is the deprecated fallback (the original `luna_wac_normalized_reflectance` WMS
# layer), kept reachable for comparison but carrying an uncorrected affine display stretch, not raw
# reflectance -- see docs/data-sources/lunaserv-wms.md.
DEFAULT_ORTHO_SOURCE = "wac_emp_pds"
ORTHO_SOURCES = ("wac_emp_pds", "lunaserv_wms")

# `hapke_shade_ortho`'s final, purely cosmetic reflectance->uint8 display stretch
# (`stretch_reflectance_to_uint8`) -- a fixed linear range, not a per-image adaptive/percentile
# stretch, matching this project's general preference for deterministic behavior. Chosen to keep
# typical lunar reflectance at WAC_EMP's own i=30/e=0/g=30 reference geometry (mare ~0.05-0.10,
# highlands ~0.12-0.20, fresh crater rays/ejecta up to ~0.3) inside [0, 255] without saturating most of
# a typical scene. Applied once, at the very end of the pipeline, to `relit_reflectance`
# (`hapke_shade_ortho`'s own output) -- after all physics (relighting), not baked into the input
# texture.
DISPLAY_STRETCH_REFLECTANCE_MIN = 0.0
DISPLAY_STRETCH_REFLECTANCE_MAX = 0.30


def geographic_crs(radius_m: float = MOON_RADIUS_M) -> str:
    """Plain (unprojected) geographic PROJ4 CRS string for the Moon.

    :param radius_m: Sphere radius, meters.
    :returns: A PROJ4 string treating coordinates as lon/lat degrees.
    """
    # The shared source of truth for this string -- every site in this project that used to build it
    # inline calls this instead, so they can't drift apart.
    return f"+proj=longlat +R={radius_m} +no_defs"


def local_orthographic_crs(center_lon_deg: float, center_lat_deg: float, radius_m: float = MOON_RADIUS_M) -> str:
    """Local Orthographic PROJ4 CRS string centered on a point on the Moon.

    :param center_lon_deg: Tangent point longitude, degrees.
    :param center_lat_deg: Tangent point latitude, degrees.
    :param radius_m: Sphere radius, meters.
    :returns: A PROJ4 string for a local, isotropic-meters working frame centered on that point.
    """
    # The shared source of truth for every per-AOI local working frame this project builds -- see
    # `geographic_crs`'s own trailing comment for why this is factored out rather than duplicated.
    return f"+proj=ortho +lon_0={center_lon_deg} +lat_0={center_lat_deg} +R={radius_m} +units=m +no_defs"


def moon_geocentric_crs(radius_m: float = MOON_RADIUS_M) -> str:
    """Geocentric (ECEF-style X/Y/Z Cartesian) PROJ4 CRS string for the Moon -- MOON_ME itself,
    expressed as a CRS.

    :param radius_m: Sphere radius, meters.
    :returns: A PROJ4 string usable as a `rasterio.warp` destination CRS.
    """
    # `rasterio.warp.transform` converts directly from `local_orthographic_crs`'s projected (x, y) plus
    # elevation `z` into this in one vectorized call, giving each DEM pixel its 3D MOON_ME position
    # without a hand-derived closed-form correction. `_terrain_photometric_angles` is the one caller.
    return f"+proj=geocent +R={radius_m} +units=m +no_defs"


def ortho_shaded_filename(
    hapke: bool,
    along_track_correction: bool = DEFAULT_ALONG_TRACK_CORRECTION,
    real_hapke_params: bool = DEFAULT_REAL_HAPKE_PARAMS,
    ortho_source: str = DEFAULT_ORTHO_SOURCE,
) -> str:
    """The `output_dir`-relative filename `despeckle_and_shade_ortho` writes its shaded ortho to.

    :param hapke: Whether Hapke shading (vs. plain Lambertian) was used.
    :param along_track_correction: Whether the along-track view-direction correction was applied.
        Only affects the filename when `hapke=True`.
    :param real_hapke_params: Whether calibrated (vs. placeholder) Hapke parameters were used. Only
        affects the filename when `hapke=True`.
    :param ortho_source: Which ortho/texture source was fetched (see `ORTHO_SOURCES`).
    :returns: The filename `despeckle_and_shade_ortho` writes to for this combination.
    """
    # Factored out so `trn_dataset.TrnTestEntry.dem_ortho_result`'s resumption check can ask for
    # exactly the file a matching `fetch_dem_and_ortho` call would produce, without duplicating this
    # naming logic. Each parameter that changes shading behavior gets its own suffix, deliberately:
    # this prevents a cached file written under an old default from silently being resumed as if it
    # matched a newer one. `_normaltilt` is always appended when `hapke=True`, independent of any
    # parameter -- kept as a permanent marker even though the correction it names is now unconditional,
    # since older cached files without it already exist on disk under other suffix combinations and
    # must not be resumed as if they matched. `_wacemp` is appended whenever
    # `ortho_source="wac_emp_pds"`, independent of `hapke`, since the input texture's numeric convention
    # (reflectance, not WMS DN) changes regardless of which shading mode blends it;
    # `ortho_source="lunaserv_wms"` keeps the original, suffix-less filenames.
    wacemp_suffix = "_wacemp" if ortho_source == "wac_emp_pds" else ""
    if not hapke:
        return f"ortho_shaded{wacemp_suffix}.tif"
    suffix = ("_atc" if along_track_correction else "") + ("_realparams" if real_hapke_params else "") + "_normaltilt"
    return f"ortho_shaded_hapke{suffix}{wacemp_suffix}.tif"


@dataclasses.dataclass(frozen=True)
class DemOrthoResult:
    """DEM/ortho tiles fetched for a `Camera`'s footprint, as returned by `fetch_dem_and_ortho`.

    :ivar ortho: Path to the shaded ortho GeoTIFF.
    :ivar dem: Path to the hole-filled DEM GeoTIFF.
    :ivar bbox: `(minx, miny, maxx, maxy)`, meters, in the per-camera local Orthographic CRS
        (`config.lunaserv_srs_template`) both tiles were fetched in -- not lon/lat degrees.
    :ivar width: Raster width, pixels.
    :ivar height: Raster height, pixels.
    """

    # Each `DemOrthoResult`'s tiles have their own independent local CRS, centered on that camera's
    # own footprint.

    ortho: Path
    dem: Path
    bbox: tuple
    width: int
    height: int


def footprint_bbox_deg(footprint_lonlat):
    """Bounding box of a camera's footprint corners.

    :param footprint_lonlat: Mapping of corner name to `(lon_deg, lat_deg)` (or `None`).
    :returns: `(minlon, minlat, maxlon, maxlat)`, degrees. May extend slightly outside [-180, 180].
    """
    # Longitudes are unwrapped onto a common branch (relative to the first corner) before taking
    # min/max: LRO's near-polar orbit means a footprint can straddle the +-180 deg antimeridian, where
    # a naive min/max would report a near-360 deg span instead of the true few-degree span on the other
    # side. Lunaserv's WMS handles an out-of-range bbox correctly -- e.g. (170, ..., 190) returns the
    # same pixel data as the equivalent in-range request (-190, ..., -170).
    lons = [v[0] for v in footprint_lonlat.values() if v]
    lats = [v[1] for v in footprint_lonlat.values() if v]
    ref = lons[0]
    unwrapped_lons = [ref + (((lon - ref) + 180.0) % 360.0 - 180.0) for lon in lons]
    return min(unwrapped_lons), min(lats), max(unwrapped_lons), max(lats)


def pad_bbox(bbox, fraction):
    """Pad a bbox outward by `fraction` of its own width/height on each side.

    :param bbox: `(minx, miny, maxx, maxy)`.
    :param fraction: Fraction of width/height to pad by, per side.
    :returns: The padded bbox, same units as `bbox`.
    """
    minx, miny, maxx, maxy = bbox
    dx, dy = (maxx - minx) * fraction, (maxy - miny) * fraction
    return (minx - dx, miny - dy, maxx + dx, maxy + dy)


def union_bbox(bbox1, bbox2):
    """The smallest bbox containing both `bbox1` and `bbox2`.

    :param bbox1: `(minx, miny, maxx, maxy)`.
    :param bbox2: `(minx, miny, maxx, maxy)`, same units as `bbox1`.
    :returns: The union bbox.
    """
    minx1, miny1, maxx1, maxy1 = bbox1
    minx2, miny2, maxx2, maxy2 = bbox2
    return min(minx1, minx2), min(miny1, miny2), max(maxx1, maxx2), max(maxy1, maxy2)


def orthographic_xy_m(lon_deg, lat_deg, center_lon_deg, center_lat_deg, radius_m: float = MOON_RADIUS_M):
    """Forward spherical Orthographic projection of a point relative to a local tangent point.

    :param lon_deg: Point longitude, degrees.
    :param lat_deg: Point latitude, degrees.
    :param center_lon_deg: Tangent point longitude, degrees.
    :param center_lat_deg: Tangent point latitude, degrees.
    :param radius_m: Sphere radius, meters.
    :returns: `(x, y)`, meters.
    """
    # Standard formula (e.g. Snyder 1987 eq. 20-3/20-4). Matches Lunaserv's `IAU2000:30166` layer
    # projection exactly (same formula, same Moon radius), so a bbox computed here lines up with what
    # the WMS server actually renders.
    lon, lat = math.radians(lon_deg), math.radians(lat_deg)
    lon0, lat0 = math.radians(center_lon_deg), math.radians(center_lat_deg)
    x = radius_m * math.cos(lat) * math.sin(lon - lon0)
    y = radius_m * (math.cos(lat0) * math.sin(lat) - math.sin(lat0) * math.cos(lat) * math.cos(lon - lon0))
    return x, y


def footprint_bbox_local_m(footprint_lonlat, center_lon_deg, center_lat_deg, radius_m: float = MOON_RADIUS_M):
    """Bounding box of a camera's footprint corners under the local Orthographic projection.

    :param footprint_lonlat: Mapping of corner name to `(lon_deg, lat_deg)` (or `None`).
    :param center_lon_deg: Projection tangent point longitude, degrees.
    :param center_lat_deg: Projection tangent point latitude, degrees.
    :param radius_m: Sphere radius, meters.
    :returns: `(minx, miny, maxx, maxy)`, meters.
    """
    # The metric counterpart of `footprint_bbox_deg`, used to size the WMS request against Lunaserv's
    # `IAU2000:30166` local-CRS layers (see `fetch_dem_and_ortho`). No antimeridian-unwrapping special
    # case is needed here (unlike `footprint_bbox_deg`): the projection's own sin/cos terms are already
    # continuous across any longitude difference.
    corners = [v for v in footprint_lonlat.values() if v is not None]
    xy = [orthographic_xy_m(lon, lat, center_lon_deg, center_lat_deg, radius_m) for lon, lat in corners]
    xs = [x for x, _ in xy]
    ys = [y for _, y in xy]
    return min(xs), min(ys), max(xs), max(ys)


def pixel_dims_for_gsd(bbox, target_gsd_m):
    """Choose width/height, in pixels, so both axes sample at ~`target_gsd_m`.

    :param bbox: `(minx, miny, maxx, maxy)`, meters (e.g. `footprint_bbox_local_m`'s output).
    :param target_gsd_m: Target ground sample distance, meters/pixel.
    :returns: `(width_px, height_px)`.
    """
    # Unlike the old lon/lat-degree bbox this replaced, no cos(lat) correction is needed here since the
    # local Orthographic CRS's axes are already isotropic in meters.
    minx, miny, maxx, maxy = bbox
    width_px = max(64, round((maxx - minx) / target_gsd_m))
    height_px = max(64, round((maxy - miny) / target_gsd_m))
    return width_px, height_px


def radius_to_elevation(radius_tif_path, elevation_tif_path, moon_radius_m: float = MOON_RADIUS_M):
    """Convert Lunaserv's planetocentric-radius DTM layer to elevation above `moon_radius_m`.

    :param radius_tif_path: Input GeoTIFF, planetocentric radius in meters.
    :param elevation_tif_path: Output GeoTIFF path; written elevation in meters.
    :param moon_radius_m: Reference radius subtracted from each pixel.
    """
    with rasterio.open(radius_tif_path) as src:
        radius = src.read(1)
        profile = src.profile
    profile.update(count=1, dtype="float32", nodata=None)
    with rasterio.open(elevation_tif_path, "w", **profile) as dst:
        dst.write((radius - moon_radius_m).astype("float32"), 1)


def fetch_dem_native(
    camera: Camera, config: TrntestConfig, extra_footprint_lonlat_deg: dict | None = None
) -> tuple[Path, tuple, int, int]:
    """**Deprecated** -- fetch the DTM layer in Lunaserv's native, unprojected geographic CRS.

    :param camera: Camera whose footprint determines the fetch AOI.
    :param config: Project config (`lunaserv_dem_srs`, `dem_native_ppd`, `dem_padding_fraction`, ...).
    :param extra_footprint_lonlat_deg: Extra corners to union into the AOI before padding, if given.
    :returns: `(radius_tif_path, deg_bbox, width, height)` -- the fetched radius GeoTIFF path plus the
        exact degree bbox/pixel dimensions requested.
    """
    # Kept for reference/comparison, no longer called by `fetch_dem_and_ortho`'s default path. A
    # second, axis-aligned crosshatch artifact is baked into Lunaserv's own native DTM tile itself
    # (present regardless of requested ppd/CRS/resampling kernel; Lunaserv exposes no resampling
    # control or backing-store metadata, so it isn't fixable client-side). The live default DEM source
    # is `fetch_dem_astropedia`/`reproject_astropedia_elevation_to_local_grid`.
    #
    # `config.lunaserv_dem_srs` (`IAU2000:30100`) is a fixed, unparametrized CRS the server needs no
    # reprojection to serve, unlike the per-camera local Orthographic CRS (`IAU2000:30166`)
    # `fetch_dem_and_ortho` requests the ortho in. Requesting this layer any finer than
    # ~`config.dem_native_ppd` forces the server to interpolate past its own detail, which produces a
    # near-Nyquist checkerboard artifact once reprojected into an arbitrary rotated/offset local CRS;
    # fetching native and reprojecting locally (`reproject_dem_to_local_grid`) avoids both problems.
    #
    # The returned bbox/dimensions let `reproject_dem_to_local_grid` build the source transform itself,
    # rather than trusting whatever georeferencing Lunaserv's GetMap response embeds.
    #
    # `extra_footprint_lonlat_deg`, if given, is combined into one dict with the camera's own footprint
    # before computing the bbox (not two separate `footprint_bbox_deg` calls unioned afterward), so
    # antimeridian-unwrapping happens against one consistent reference corner -- two independent
    # unwraps could each pick a different branch near +-180 deg and produce a bogus near-360-deg union.
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
    """Reproject a single-band source array onto the per-camera local Orthographic working grid.

    :param source_array: Single-band source raster.
    :param src_crs: Source CRS.
    :param src_transform: Source affine transform.
    :param dst_bbox_m: Destination `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param dst_width: Destination width, pixels.
    :param dst_height: Destination height, pixels.
    :param center_lon_deg: Destination CRS tangent point longitude, degrees.
    :param center_lat_deg: Destination CRS tangent point latitude, degrees.
    :param moon_radius_m: Sphere radius, meters.
    :param output_path: Where to write the reprojected single-band GeoTIFF.
    :param resampling: `rasterio.warp` resampling method.
    :param tolerance: `rasterio.warp.reproject` error tolerance.
    :param src_nodata: Source nodata value, if any.
    :param dst_nodata: Destination nodata value, if any.
    :returns: `output_path`, as a `Path`.
    """
    # Shared warp core behind both `reproject_dem_to_local_grid` (deprecated, Lunaserv-native source)
    # and `reproject_astropedia_elevation_to_local_grid` (live default, Astropedia source). Uses
    # `rasterio.warp.reproject` so the resampling method is one this project controls explicitly, not
    # any server's opaque resampling. The destination Orthographic definition matches
    # `orthographic_xy_m`'s own forward projection math exactly (same center, same sphere radius, same
    # projection family).
    dst_crs = local_orthographic_crs(center_lon_deg, center_lat_deg, moon_radius_m)
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
    with atomic_publish(Path(output_path)) as tmp:
        with rasterio.open(tmp, "w", **profile) as dst:
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
    """**Deprecated** -- reproject a native-CRS DTM array (`fetch_dem_native`'s output) onto the
    per-camera local Orthographic working grid the ortho fetch uses.

    :param native_path: `fetch_dem_native`'s output GeoTIFF path.
    :param native_bbox_deg: Source `(minlon, minlat, maxlon, maxlat)`, degrees.
    :param native_width: Source width, pixels.
    :param native_height: Source height, pixels.
    :param dst_bbox_m: Destination `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param dst_width: Destination width, pixels.
    :param dst_height: Destination height, pixels.
    :param center_lon_deg: Destination CRS tangent point longitude, degrees.
    :param center_lat_deg: Destination CRS tangent point latitude, degrees.
    :param moon_radius_m: Sphere radius, meters.
    :param output_path: Where to write the reprojected single-band GeoTIFF.
    :param resampling: `rasterio.warp` resampling method.
    :param tolerance: `rasterio.warp.reproject` error tolerance.
    :returns: `output_path`, as a `Path`.
    """
    # Kept for reference/comparison alongside `fetch_dem_native` (see that function's own trailing
    # comment for why). Behavior unchanged from before this function's warp core was factored out into
    # `_reproject_raster_to_local_grid` -- entirely local, so the resampling method is one this project
    # controls and picks explicitly (`resampling`, exposed as a parameter so alternatives can be
    # compared), not Lunaserv's own opaque server-side resampling. Both CRSs are expressed as generic
    # PROJ4 strings with the Moon's own spherical radius, rather than relying on GDAL/PROJ recognizing
    # Lunaserv's `IAU2000:*` codes by name.
    #
    # This removes the original near-Nyquist server-side resampling artifact, but the resampling kernel
    # used here still matters: an ~2.4x upsample (native ~237m/px to a 100m/px working grid) through a
    # smooth reconstruction kernel can itself introduce a small periodic curvature ripple at the native
    # sample spacing, invisible in the raw elevation but visible once `hillshade`'s finite-differencing
    # amplifies it. This artifact isn't fully fixable through resampling choice alone -- part of why
    # this path is deprecated in favor of `fetch_dem_astropedia`.
    with rasterio.open(native_path) as src:
        native_radius = src.read(1)

    minlon, minlat, maxlon, maxlat = native_bbox_deg
    src_crs = geographic_crs(moon_radius_m)
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


# Astropedia's flat-file GLD100 DEM (`config.astropedia_gld100_url`) covers +-79 deg latitude
# (`gdalinfo`'s own corner coordinates: 79d0'6.57" both ways). No silent fallback to the deprecated
# Lunaserv-native path for footprints beyond this -- see `astropedia_coverage_bbox_deg`.
ASTROPEDIA_MAX_ABS_LATITUDE_DEG = 79.0


DEM_FETCH_SAFETY_MARGIN_FRACTION = 0.02


def astropedia_coverage_bbox_deg(
    dst_bbox_m: tuple, center_lon_deg: float, center_lat_deg: float, moon_radius_m: float
) -> tuple:
    """The lon/lat degree bbox needed to fully cover `dst_bbox_m` once reprojected, plus a small
    safety margin for the resampling kernel's own footprint.

    :param dst_bbox_m: The local-Orthographic working grid's own bbox, meters -- see
        `fetch_dem_and_ortho`.
    :param center_lon_deg: Local Orthographic CRS tangent point longitude, degrees.
    :param center_lat_deg: Local Orthographic CRS tangent point latitude, degrees.
    :param moon_radius_m: Sphere radius, meters.
    :returns: `(minlon, minlat, maxlon, maxlat)`, degrees.
    :raises ValueError: If the result extends beyond `ASTROPEDIA_MAX_ABS_LATITUDE_DEG`.
    """
    # The `DEM_FETCH_SAFETY_MARGIN_FRACTION` pad accounts for bilinear resampling needing neighbor
    # samples just past the destination edge.
    #
    # Derived directly from `dst_bbox_m`'s own boundary (`rasterio.warp.transform_bounds` densely
    # samples the whole edge, not just the 4 corners), not by independently padding a degree-space bbox
    # around the footprint's own corners: two independently-padded bboxes -- one in degrees, one in
    # local-Orthographic meters -- aren't guaranteed to cover each other, since a square's diagonal
    # corners are ~41% farther from center than its edge midpoints. Deriving the degree bbox from
    # `dst_bbox_m` directly makes that mismatch structurally impossible.
    #
    # No automatic fallback to the deprecated Lunaserv path -- a caller that wants one has to ask for
    # it explicitly.
    padded_bbox_m = pad_bbox(dst_bbox_m, DEM_FETCH_SAFETY_MARGIN_FRACTION)
    geo_crs = geographic_crs(moon_radius_m)
    ortho_crs = local_orthographic_crs(center_lon_deg, center_lat_deg, moon_radius_m)
    minlon, minlat, maxlon, maxlat = transform_bounds(ortho_crs, geo_crs, *padded_bbox_m)
    if minlat < -ASTROPEDIA_MAX_ABS_LATITUDE_DEG or maxlat > ASTROPEDIA_MAX_ABS_LATITUDE_DEG:
        raise ValueError(
            f"Camera footprint's padded AOI (latitude range {minlat:.2f}..{maxlat:.2f} deg) extends "
            f"beyond Astropedia's GLD100 flat file's +-{ASTROPEDIA_MAX_ABS_LATITUDE_DEG} deg "
            "coverage -- no DEM data available there from this source. The deprecated Lunaserv-native "
            "path (lunaserv.fetch_dem_native/reproject_dem_to_local_grid) covers this latitude range "
            "but has its own known, unfixed artifact, and isn't used automatically here."
        )
    return minlon, minlat, maxlon, maxlat


def fetch_dem_astropedia(
    dst_bbox_m: tuple, center_lon_deg: float, center_lat_deg: float, config: TrntestConfig
) -> tuple[Path, tuple]:
    """Live default DEM source: ensure Astropedia's flat-file GLD100 DEM is downloaded/cached locally.

    :param dst_bbox_m: `fetch_dem_and_ortho`'s own already-padded (and, if applicable, already unioned
        with `extra_footprint_lonlat_deg`) local-Orthographic working-grid bbox.
    :param center_lon_deg: Local Orthographic CRS tangent point longitude, degrees.
    :param center_lat_deg: Local Orthographic CRS tangent point latitude, degrees.
    :param config: Project config (`cache_root`, `astropedia_gld100_url`).
    :returns: `(local_cached_path, deg_bbox)` -- `reproject_astropedia_elevation_to_local_grid` needs
        the bbox to know which AOI window to read from the file.
    :raises ValueError: If the footprint needs data outside the file's coverage
        (`astropedia_coverage_bbox_deg`).
    """
    # `cache.fetch_astropedia_gld100` fetches the whole ~10GB file, once, resumably; see its own
    # docstring for why this doesn't fetch a remote AOI window directly: the file isn't a
    # Cloud-Optimized GeoTIFF, so a remote windowed read pulls full-width row strips, which is slow.
    #
    # `dst_bbox_m` is passed in directly, not re-derived from the raw camera footprint, so there's
    # exactly one padded AOI decision, not two independent ones (see
    # `astropedia_coverage_bbox_deg`'s own trailing comment for why that used to cause corner nodata
    # gaps).
    deg_bbox = astropedia_coverage_bbox_deg(dst_bbox_m, center_lon_deg, center_lat_deg, MOON_RADIUS_M)
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
    """Read just the AOI from the local cached Astropedia file and reproject it onto the per-camera
    local Orthographic working grid `reproject_dem_to_local_grid` uses.

    :param astropedia_path: `fetch_dem_astropedia`'s cached file path.
    :param deg_bbox: `(minlon, minlat, maxlon, maxlat)`, degrees, from `fetch_dem_astropedia` -- the
        AOI window to read.
    :param dst_bbox_m: Destination `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param dst_width: Destination width, pixels.
    :param dst_height: Destination height, pixels.
    :param center_lon_deg: Destination CRS tangent point longitude, degrees.
    :param center_lat_deg: Destination CRS tangent point latitude, degrees.
    :param moon_radius_m: Sphere radius, meters.
    :param output_path: Where to write the reprojected elevation GeoTIFF.
    :param resampling: `rasterio.warp` resampling method.
    :param tolerance: `rasterio.warp.reproject` error tolerance.
    :returns: `output_path`, as a `Path`. Values are elevation, meters, not planetocentric radius.
    """
    # Fast (no network, no row-strip-over-HTTP penalty), unlike a remote `/vsicurl/` windowed read of
    # the same file. Uses the file's own embedded `crs`/`transform` directly rather than hardcoding
    # Astropedia's Equidistant Cylindrical PROJ4 parameters by hand, since this file (unlike Lunaserv's
    # GetMap responses) has trustworthy embedded georeferencing.
    #
    # This data is already elevation (Int16 meters, nodata -32768), not planetocentric radius like
    # Lunaserv's DTM layer -- `radius_to_elevation` is skipped entirely for this path.
    with rasterio.open(astropedia_path) as src:
        src_crs = src.crs
        src_nodata = src.nodata
        minlon, minlat, maxlon, maxlat = deg_bbox
        geo_crs = geographic_crs(moon_radius_m)
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


# The WAC_EMP PDS4 archive's equirect (non-polar) tile grid covers only 0-60 deg in each hemisphere
# (confirmed via the archive's own S3 bucket listing) -- a separate polar-stereographic tile pair
# (`P900N`/`P900S` in the product-ID scheme) covers 60-90 deg, in a format this project doesn't fetch
# (see `wac_emp_tile_id_for_bbox`'s docstring).
WAC_EMP_MAX_ABS_LATITUDE_DEG = 60.0
# The equirect grid's own tiling scheme, confirmed via the archive's directory listing: exactly one
# 60-deg-tall latitude band per hemisphere (0-60, center magnitude 30.0 -- hence the tile ID's fixed
# "E300" segment below), and 4 lon zones 90 deg wide each, centered at 45/135/225/315 (0-90, 90-180,
# 180-270, 270-360, Positive-East).
_WAC_EMP_LON_ZONE_WIDTH_DEG = 90.0
_WAC_EMP_LAT_BAND_CENTER_CODE = 300  # fixed: (0+60)/2 * 10 -- the tile ID's literal "E300" segment


def wac_emp_tile_id_for_bbox(
    dst_bbox_m: tuple,
    center_lon_deg: float,
    center_lat_deg: float,
    moon_radius_m: float,
    wavelength_nm: int = DEFAULT_HAPKE_CALIBRATION_WAVELENGTH_NM,
    ppd: int = 304,
) -> str:
    """Resolve the single WAC_EMP PDS4 tile (product ID, no extension) that fully covers `dst_bbox_m`.

    :param dst_bbox_m: The local-Orthographic working grid's own already-padded bbox, meters -- see
        `fetch_dem_and_ortho`.
    :param center_lon_deg: Local Orthographic CRS tangent point longitude, degrees.
    :param center_lat_deg: Local Orthographic CRS tangent point latitude, degrees.
    :param moon_radius_m: Sphere radius, meters.
    :param wavelength_nm: One of the archive's 7 bands (matches `_HAPKE_CALIBRATION_WAVELENGTHS_NM`).
    :param ppd: A resolution the archive offers for that wavelength (every band has 64 ppd; 643nm
        additionally has a 304 ppd product, this project's own default).
    :returns: The product ID, e.g. `WAC_EMP_643NM_E300N0450_304P`.
    :raises ValueError: If `wavelength_nm` isn't one of the archive's bands, or if the padded AOI
        extends beyond `WAC_EMP_MAX_ABS_LATITUDE_DEG`, straddles the equator (the tile grid's own
        hemisphere boundary), or straddles a 90-deg longitude zone boundary.
    """
    # Product ID format, confirmed via the archive's own S3 bucket listing (see
    # docs/data-sources/wac-emp-pds4.md for the full derivation):
    # `WAC_EMP_<wavelength_nm>NM_E300<N|S><lon_center_deg*10:04d>_<ppd:03d>P`.
    #
    # Uses the same `transform_bounds`-on-the-destination-grid technique `astropedia_coverage_bbox_deg`
    # uses, not an independently-padded degree-space bbox (see that function's own trailing comment for
    # why the latter causes corner nodata gaps). No multi-tile mosaic in this pass, matching
    # `astropedia_coverage_bbox_deg`'s own no-automatic-fallback stance.
    if wavelength_nm not in _HAPKE_CALIBRATION_WAVELENGTHS_NM:
        raise ValueError(
            f"wavelength_nm={wavelength_nm} is not one of the archive's own bands {_HAPKE_CALIBRATION_WAVELENGTHS_NM}"
        )
    padded_bbox_m = pad_bbox(dst_bbox_m, DEM_FETCH_SAFETY_MARGIN_FRACTION)
    geo_crs = geographic_crs(moon_radius_m)
    ortho_crs = local_orthographic_crs(center_lon_deg, center_lat_deg, moon_radius_m)
    minlon, minlat, maxlon, maxlat = transform_bounds(ortho_crs, geo_crs, *padded_bbox_m)

    if minlat < -WAC_EMP_MAX_ABS_LATITUDE_DEG or maxlat > WAC_EMP_MAX_ABS_LATITUDE_DEG:
        raise ValueError(
            f"Camera footprint's padded AOI (latitude range {minlat:.2f}..{maxlat:.2f} deg) extends "
            f"beyond WAC_EMP's equirect tile grid's +-{WAC_EMP_MAX_ABS_LATITUDE_DEG} deg coverage -- "
            "the polar-stereographic tile set beyond this isn't fetched by this project (unverified "
            "format, see wac_emp_tile_id_for_bbox's own docstring); the deprecated Lunaserv-WMS ortho "
            "path (fetch_dem_and_ortho(..., ortho_source='lunaserv_wms')) has no such limit but carries "
            "an uncorrected affine display stretch -- see docs/data-sources/lunaserv-wms.md."
        )
    if minlat < 0.0 < maxlat:
        raise ValueError(
            f"Camera footprint's padded AOI (latitude range {minlat:.2f}..{maxlat:.2f} deg) straddles "
            "the equator -- WAC_EMP's equirect tile grid has a separate tile per hemisphere and this "
            "project doesn't mosaic across the boundary."
        )
    hemisphere = "N" if maxlat >= 0.0 else "S"

    minlon_norm, maxlon_norm = minlon % 360.0, maxlon % 360.0
    if minlon_norm > maxlon_norm:
        raise ValueError(
            f"Camera footprint's padded AOI (longitude range {minlon:.2f}..{maxlon:.2f} deg) appears "
            "to straddle the 0/360 deg longitude boundary -- not handled by this tile lookup."
        )
    zone_min = int(minlon_norm // _WAC_EMP_LON_ZONE_WIDTH_DEG)
    zone_max = int(maxlon_norm // _WAC_EMP_LON_ZONE_WIDTH_DEG)
    if zone_min != zone_max:
        raise ValueError(
            f"Camera footprint's padded AOI (longitude range {minlon:.2f}..{maxlon:.2f} deg) straddles "
            f"a WAC_EMP tile's {_WAC_EMP_LON_ZONE_WIDTH_DEG:.0f}-deg longitude zone boundary -- this "
            "project doesn't mosaic across the boundary."
        )
    lon_center_code = round(zone_min * _WAC_EMP_LON_ZONE_WIDTH_DEG + _WAC_EMP_LON_ZONE_WIDTH_DEG / 2) * 10

    return f"WAC_EMP_{wavelength_nm}NM_E{_WAC_EMP_LAT_BAND_CENTER_CODE}{hemisphere}{lon_center_code:04d}_{ppd:03d}P"


def fetch_wac_emp_reflectance(
    dst_bbox_m: tuple,
    center_lon_deg: float,
    center_lat_deg: float,
    config: TrntestConfig,
    wavelength_nm: int = DEFAULT_HAPKE_CALIBRATION_WAVELENGTH_NM,
    ppd: int = 304,
) -> tuple[Path, str]:
    """Live default ortho/texture source: resolve and fetch/cache the single WAC_EMP PDS4 tile
    covering `dst_bbox_m`, mirroring `fetch_dem_astropedia`'s own shape.

    :param dst_bbox_m: The local-Orthographic working grid's own already-padded bbox, meters.
    :param center_lon_deg: Local Orthographic CRS tangent point longitude, degrees.
    :param center_lat_deg: Local Orthographic CRS tangent point latitude, degrees.
    :param config: Project config (`cache_root`, `wac_emp_base_url`).
    :param wavelength_nm: Passed through to `wac_emp_tile_id_for_bbox`.
    :param ppd: Passed through to `wac_emp_tile_id_for_bbox`.
    :returns: `(local_cached_path, product_id)` -- `reproject_wac_emp_reflectance_to_local_grid` needs
        only the path (it reads the AOI window directly from the file's own embedded georeferencing);
        the product ID is returned for logging/cache-busting/debugging.
    :raises ValueError: If the footprint needs a tile this project doesn't fetch
        (`wac_emp_tile_id_for_bbox`).
    """
    product_id = wac_emp_tile_id_for_bbox(
        dst_bbox_m, center_lon_deg, center_lat_deg, MOON_RADIUS_M, wavelength_nm=wavelength_nm, ppd=ppd
    )
    path = cache.fetch_wac_emp_tile(product_id, config.cache_root, config.wac_emp_base_url)
    return path, product_id


def reproject_wac_emp_reflectance_to_local_grid(
    wac_emp_path,
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
    """Read just the AOI from the local cached WAC_EMP tile and reproject it onto the per-camera local
    Orthographic working grid the DEM fetch uses.

    :param wac_emp_path: `fetch_wac_emp_reflectance`'s cached file path.
    :param dst_bbox_m: Destination `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param dst_width: Destination width, pixels.
    :param dst_height: Destination height, pixels.
    :param center_lon_deg: Destination CRS tangent point longitude, degrees.
    :param center_lat_deg: Destination CRS tangent point latitude, degrees.
    :param moon_radius_m: Sphere radius, meters.
    :param output_path: Where to write the reprojected reflectance GeoTIFF.
    :param resampling: `rasterio.warp` resampling method.
    :param tolerance: `rasterio.warp.reproject` error tolerance.
    :returns: `output_path`, as a `Path`. Values are physical reflectance (IEEE754 float32, no
        embedded display stretch), not Lunaserv WMS-served DN.
    """
    # Mirrors `reproject_astropedia_elevation_to_local_grid`'s window-read-then-warp shape, except the
    # AOI window comes directly from `dst_bbox_m` transformed into the file's own embedded CRS. No
    # separate degree-space bbox intermediate is needed here, unlike Astropedia's path: this file's
    # PDS3 label carries a trustworthy projected CRS/transform GDAL's PDS3 driver reads natively, not a
    # hand-rolled equirect PROJ4 string or manual byte offsets.
    #
    # Because this is reflectance, not DN, no `/255.0` un-scaling assumption applies to this output;
    # `hapke_shade_ortho`/`shade_ortho` treat it as reflectance directly (see their own docstrings for
    # the resulting numeric-pipeline change).
    with rasterio.open(wac_emp_path) as src:
        src_crs = src.crs
        src_nodata = src.nodata
        left, bottom, right, top = transform_bounds(
            local_orthographic_crs(center_lon_deg, center_lat_deg, moon_radius_m), src_crs, *dst_bbox_m
        )
        window = window_from_bounds(left, bottom, right, top, transform=src.transform)
        src_transform = window_transform(window, src.transform)
        reflectance = src.read(1, window=window)

    return _reproject_raster_to_local_grid(
        reflectance,
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
    """Hole-fill `dem_path` via ASP's `dem_mosaic`, writing to `filled_path`.

    :param dem_path: Input DEM GeoTIFF.
    :param filled_path: Output path; must end in exactly `-tile-0.tif` (`dem_mosaic`'s own `-o
        <prefix>` convention appends that suffix to whatever prefix it's given).
    """
    # `atomic_publish_prefix` builds a temp prefix the same way, so this is atomic despite the
    # prefix-based (not exact-path) tool convention that `atomic_publish_path`'s own contract doesn't
    # directly fit -- see that helper's own docstring.
    filled_path = Path(filled_path)
    with atomic_publish_prefix(filled_path, "-tile-0.tif") as tmp_prefix:
        run_quiet(["dem_mosaic", str(dem_path), "--hole-fill-length", "50", "-o", str(tmp_prefix)])


def despeckle(data: np.ndarray, size: int = 3, n_mad: float = 6.0) -> np.ndarray:
    """Replace isolated single-pixel outliers with their local neighborhood median, leaving smooth
    terrain and large bright/saturated features (e.g. a crater) untouched.

    :param data: Input raster.
    :param size: Neighborhood window size, pixels.
    :param n_mad: Outlier threshold, in scaled median-absolute-deviations of the local neighborhood.
    :returns: `data` with outlier pixels replaced by their neighborhood median.
    """
    # A pixel is flagged only when it deviates from its `size`x`size` neighborhood median by more than
    # `n_mad` scaled MADs of that same neighborhood -- this makes the threshold self-scaling to local
    # contrast, so a pixel next to an edge or large feature (where the neighborhood's own MAD is
    # already high) is far less likely to be flagged than an isolated pixel in otherwise-smooth terrain.
    # Validated against fetched Lunaserv WAC tiles (see docs/data-sources/lunaserv-wms.md): ~90% of
    # statistical outliers under this test are isolated single pixels with no adjacent outlier, and a
    # known saturated-crater blob in that data is untouched by design (its neighborhood MAD is not
    # small).
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
    """Blend a Lambertian hillshade, lit from `(azimuth_deg, elevation_deg)` and computed from `dem`,
    onto `ortho`.

    :param ortho: `[0, 255]`-range ortho image.
    :param dem: Elevation, meters.
    :param azimuth_deg: Sun azimuth, degrees.
    :param elevation_deg: Sun elevation, degrees.
    :param cellsize_m: DEM pixel size, meters.
    :returns: The shaded `uint8` image.
    """
    # `sat_sim` applies no illumination model of its own; it geometrically reprojects whatever's
    # already in the ortho (see docs/external-tools.md's ASP `sat_sim` section), so any relief in the
    # synthetic render has to come from here. A direct multiply, not `0.5 + 0.5 * hillshade` (an
    # earlier version's artificial floor that halved the shading term's usable dynamic range and
    # washed out the render relative to WAC imagery): terrain facing away from the sun should render
    # dark, not floored at ~50% gray. This is still just local per-facet shading, not cast-shadow
    # occlusion from other terrain, which remains out of scope (same section).
    light = LightSource(azdeg=azimuth_deg, altdeg=elevation_deg)
    hillshade = light.hillshade(dem.astype(np.float64), dx=cellsize_m, dy=cellsize_m)
    ortho_norm = ortho.astype(np.float64) / 255.0
    blended = ortho_norm * hillshade
    return np.clip(blended * 255.0, 0, 255).astype(np.uint8)


def _local_enu_basis(center_lon_deg: float, center_lat_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The (East, North, Up) unit vectors, in MOON_ME, of the local tangent plane at a point.

    :param center_lon_deg: Tangent point longitude, degrees.
    :param center_lat_deg: Tangent point latitude, degrees.
    :returns: `(east, north, up)`, each a MOON_ME unit vector.
    """
    # Used by `_moon_me_direction_from_local_enu` to rotate a single local-frame direction (the sun's
    # azimuth/elevation) into MOON_ME. `_terrain_photometric_angles` itself works entirely in MOON_ME
    # (positions -- DEM points, camera -- are never embedded into a local tangent plane, which is a
    # lossy approximation); a free direction has no such embedding step, so rotating one between
    # orthonormal frames stays exact, and this is the one remaining boundary that still needs it.
    lon0, lat0 = math.radians(center_lon_deg), math.radians(center_lat_deg)
    east = np.array([-math.sin(lon0), math.cos(lon0), 0.0])
    north = np.array([-math.sin(lat0) * math.cos(lon0), -math.sin(lat0) * math.sin(lon0), math.cos(lat0)])
    up = np.array([math.cos(lat0) * math.cos(lon0), math.cos(lat0) * math.sin(lon0), math.sin(lat0)])
    return east, north, up


def _moon_me_direction_from_local_enu(local_enu_vector, center_lon_deg: float, center_lat_deg: float) -> np.ndarray:
    """Rotate a local (East, North, Up) direction into MOON_ME.

    :param local_enu_vector: `(e, n, u)` components (e.g. the sun's azimuth/elevation, converted to a
        local ENU unit vector by `real_geometry_photometric_angles`).
    :param center_lon_deg: Tangent point longitude, degrees.
    :param center_lat_deg: Tangent point latitude, degrees.
    :returns: The direction as a MOON_ME vector.
    """
    # Via `_local_enu_basis`'s orthonormal (East, North, Up) triad -- the linear combination
    # `east*e + north*n + up*u`. Lossless either direction, since (east, north, up) are orthonormal.
    east, north, up = _local_enu_basis(center_lon_deg, center_lat_deg)
    e, n, u = np.asarray(local_enu_vector, dtype=np.float64)
    return e * east + n * north + u * up


def _terrain_photometric_angles(
    dem: np.ndarray,
    bbox: tuple,
    center_lon_deg: float,
    center_lat_deg: float,
    camera_center_moon_me_m,
    sun_direction_moon_me,
    cellsize_m: float,
    radius_m: float,
    along_track_direction_moon_me=None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-pixel incidence/emission/phase angles for `dem`'s own orthographic ortho, using the finite
    camera position rather than an idealized infinitely-distant nadir viewer.

    :param dem: Elevation, meters, on the local orthographic grid described by `bbox`.
    :param bbox: `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param center_lon_deg: Local Orthographic CRS tangent point longitude, degrees.
    :param center_lat_deg: Local Orthographic CRS tangent point latitude, degrees.
    :param camera_center_moon_me_m: Camera position, MOON_ME meters.
    :param sun_direction_moon_me: Sun direction, MOON_ME unit vector.
    :param cellsize_m: DEM pixel size, meters.
    :param radius_m: Sphere radius, meters.
    :param along_track_direction_moon_me: The camera's along-track attitude axis, MOON_ME unit vector,
        if the along-track correction should be applied; `None` to skip it.
    :returns: `(incidence_deg, emission_deg, phase_deg)`, each the same shape as `dem` -- raw geometry,
        not `LightSource.hillshade`'s own scene-relative contrast-stretched intensity (see its
        `shade_normals`), which is what ISIS `photomet` needs.
    """
    # Uses the finite camera position (`camera_center_moon_me_m`) rather than an idealized
    # infinitely-distant nadir viewer, so emission and phase vary per pixel from parallax (each pixel's
    # own vector to the spacecraft), not just from local terrain slope.
    #
    # Fully MOON_ME-native: no local tangent-plane position embedding anywhere in this function.
    # `ground`, each DEM pixel's true 3D position, comes from `rasterio.warp.transform` converting
    # `dem`'s local orthographic `(x, y)` plus its own elevation directly into `moon_geocentric_crs`'s
    # MOON_ME X/Y/Z. `camera_center_moon_me_m`/`along_track_direction_moon_me` are used directly, with
    # no rotation into any local frame. `sun_direction_moon_me` is the one input converted from a local
    # frame (the sun's azimuth/elevation, by `real_geometry_photometric_angles`'s caller, via
    # `_moon_me_direction_from_local_enu`) -- rotating a free direction between orthonormal frames is
    # exact and lossless, unlike embedding a position into a tangent plane, so this conversion carries
    # no approximation error; see that function's own docstring.
    #
    # The surface normal at each pixel comes from the same `np.gradient`-based convention
    # `LightSource.hillshade` uses internally, over MOON_ME coordinates, so this stays geometrically
    # consistent with `shade_ortho`'s Lambertian shading (which only needs incidence). The Sun, unlike
    # the camera, is treated as an effectively parallel-ray, scene-wide direction (lunar distance makes
    # this negligible -- see `illumination.sun_azimuth_elevation_deg`'s docstring), so
    # `sun_direction_moon_me` is a single vector rather than a per-pixel one.
    #
    # `along_track_direction_moon_me`, if given, projects the raw per-pixel view direction onto the
    # plane perpendicular to it before computing emission/phase -- a correction for this project's
    # single-frozen-camera-pose approximation of a multi-second pushframe scan: `camera_center_moon_me_m`
    # is one fixed position (matched to the crop's own center-frame time), but the spacecraft was
    # measurably elsewhere by the time any other line was actually captured, so the raw along-track
    # component of `view_dir` reflects the wrong position away from center. A scanning
    # pushframe/pushbroom sensor observes each line close to nadir in its own along-track direction at
    # the instant it's captured, so discarding the along-track component of the raw view direction and
    # keeping only the cross-track component approximates that per-line near-nadir geometry without
    # needing this project's per-line timing machinery that exists elsewhere (`isis_wac`'s per-line time
    # reconstruction). `camera_along_track_direction_moon_me` -- the sensor's own along-track axis,
    # derived from the camera's re-aimed attitude, not the spacecraft's raw orbital velocity direction --
    # is substantially more accurate against `campt` ground truth than either the spacecraft's raw
    # orbital velocity direction or the camera's cross-track axis.
    #
    # Independently validated against ISIS `campt` ground truth
    # (`tests/test_lunaserv_campt_validation.py`, `heavy`-marked) for the ellipsoid limit (`dem` all
    # zero), and against ASP `sfs`'s own ray-DEM intersection
    # (`tests/test_sfs_validation_lambertian_incidence.py`, `heavy`-marked) for the full DEM-aware case
    # across a candidate's entire coverage region -- both agree to within floating-point/interpolation
    # noise. ISIS's own `phocube` was investigated as a DEM-aware validation tool and shelved: its
    # `localincidence=true` mode returns degenerate values on a real scene (ISIS issue
    # DOI-USGS/ISIS3#3645), and `campt`'s own plain angle output stays ellipsoid-normal-based even with
    # a DEM shape model attached, so it has no DEM-aware mode of its own to fall back on either.
    #
    # A still-unexplained regression remains open, not addressed here: the normal-tilt correction made
    # the brightness-matched diff against a WAC crop measurably worse, despite the geometry itself being
    # independently confirmed correct above. Leading hypothesis, not verified: the base ortho texture is
    # already photometrically normalized in a way this project's own re-shading was never validated
    # against. See `docs/proposed-tasks/open-items.md`.
    height, width = dem.shape
    minx, miny, maxx, maxy = bbox
    x_centers = minx + (np.arange(width) + 0.5) * (maxx - minx) / width
    y_centers = maxy - (np.arange(height) + 0.5) * (maxy - miny) / height  # row 0 = north/top, matches `dy` below
    x_grid, y_grid = np.meshgrid(x_centers, y_centers)

    dy = -cellsize_m
    dem64 = dem.astype(np.float64)
    # Each DEM pixel's 3D MOON_ME position, via one vectorized `rasterio.warp.transform` call from
    # `dem`'s local orthographic (x, y) plus its elevation into `moon_geocentric_crs`'s MOON_ME X/Y/Z.
    ground_x, ground_y, ground_z = transform(
        local_orthographic_crs(center_lon_deg, center_lat_deg, radius_m),
        moon_geocentric_crs(radius_m),
        x_grid.ravel(),
        y_grid.ravel(),
        dem64.ravel(),
    )
    ground_shape = (height, width)
    ground = np.stack(
        [np.reshape(ground_x, ground_shape), np.reshape(ground_y, ground_shape), np.reshape(ground_z, ground_shape)],
        axis=-1,
    )

    # `normal`: the surface normal, via each of `ground`'s 3 MOON_ME coordinate channels' partial
    # derivatives (row/col index space -> physical space, `dy`/`cellsize_m`) and their cross product --
    # the general parametric-surface-normal construction.
    ground_d_row, ground_d_col = np.gradient(ground, dy, cellsize_m, axis=(0, 1))
    normal = np.cross(ground_d_col, ground_d_row)
    normal /= np.linalg.norm(normal, axis=-1, keepdims=True)

    camera_center_moon_me_m = np.asarray(camera_center_moon_me_m, dtype=np.float64)
    view_vec = camera_center_moon_me_m - ground
    view_dir = view_vec / np.linalg.norm(view_vec, axis=-1, keepdims=True)

    if along_track_direction_moon_me is not None:
        v_hat = np.asarray(along_track_direction_moon_me, dtype=np.float64)
        v_hat = v_hat / np.linalg.norm(v_hat)
        view_dir = view_dir - np.sum(view_dir * v_hat, axis=-1, keepdims=True) * v_hat
        view_dir /= np.linalg.norm(view_dir, axis=-1, keepdims=True)

    sun_dir = np.asarray(sun_direction_moon_me, dtype=np.float64)
    sun_dir = sun_dir / np.linalg.norm(sun_dir)

    incidence_deg = np.degrees(np.arccos(np.clip(normal @ sun_dir, -1.0, 1.0)))
    emission_deg = np.degrees(np.arccos(np.clip(np.sum(normal * view_dir, axis=-1), -1.0, 1.0)))
    phase_deg = np.degrees(np.arccos(np.clip(view_dir @ sun_dir, -1.0, 1.0)))
    return incidence_deg, emission_deg, phase_deg


def _write_backplane_cube(path: Path, values: np.ndarray) -> None:
    """Write `values` as a plain (non-georeferenced) single-band ISIS3 cube.

    :param path: Output `.cub` path.
    :param values: Single-band raster to write.
    """
    # `photomet`'s `ANGLESOURCE=BACKPLANE` mode only matches backplane files to the input cube by
    # sample/line dimensions, so no CRS/transform is needed here (and translating this project's local
    # Orthographic CRS into an ISIS `Mapping` group is unnecessary risk -- ISIS only recognizes a
    # specific set of projections). GDAL's own `ISIS3` driver (`rw+v`, per `gdalinfo --formats`)
    # writes/reads `.cub` files directly, so no `gdal2isis`/`isis2std` round-trip through a separate
    # conversion tool is needed either. Deliberately non-georeferenced, so `rasterio`'s own
    # `NotGeoreferencedWarning` is expected and suppressed here rather than left to print into a
    # notebook cell's output.
    path.unlink(missing_ok=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with rasterio.open(
            path, "w", driver="ISIS3", height=values.shape[0], width=values.shape[1], count=1, dtype="float32"
        ) as dst:
            dst.write(values.astype("float32"), 1)


def _hapke_calibration_cube_path(config: TrntestConfig) -> Path:
    """Resolve the ISIS lunar Hapke calibration cube's path, fetching the `lro` ISIS data package if
    not already present.

    :param config: Project config (`cache_root`).
    :returns: Path to the highest-numbered matching calibration cube.
    :raises FileNotFoundError: If no matching cube is found after fetching.
    """
    # Globs rather than hardcoding a specific version number (`.0001.cub` today), the same "don't
    # assume a specific version" discipline this project applies to other ISIS data area files.
    from trntest import isis_wac  # noqa: PLC0415 -- circular otherwise (isis_wac imports DemOrthoResult)

    isis_wac.ensure_isisdata(config)
    isisdata = config.cache_root / "isisdata"
    candidates = sorted((isisdata / "lro" / "calibration").glob(_HAPKE_CALIBRATION_CUBE_GLOB))
    if not candidates:
        raise FileNotFoundError(
            f"No {_HAPKE_CALIBRATION_CUBE_GLOB} found under {isisdata / 'lro' / 'calibration'} -- "
            "ensure_isisdata should have fetched it as part of the 'lro' ISIS data package."
        )
    return candidates[-1]


def _sample_hapke_calibration(
    cube_path: Path, center_lon_deg: float, center_lat_deg: float, wavelength_nm: int
) -> dict[str, float]:
    """Read all 9 Hapke parameters for one wavelength band at one ground point from `cube_path`.

    :param cube_path: Any raster GDAL can open in the calibration cube's own Equirectangular CRS/band
        layout (the real cube, or a test fixture).
    :param center_lon_deg: Ground point longitude, degrees.
    :param center_lat_deg: Ground point latitude, degrees.
    :param wavelength_nm: One of the cube's 7 bands (`_HAPKE_CALIBRATION_WAVELENGTHS_NM`).
    :returns: Dict of all 9 parameters, keyed by `_HAPKE_CALIBRATION_PARAM_ORDER`.
    :raises ValueError: If `wavelength_nm` isn't one of the cube's bands.
    """
    # Pure sampling logic, split out of `fetch_real_hapke_params` so it's unit-testable against a small
    # synthetic fixture, without needing `$ISISDATA` or network access -- the same reasoning
    # `_terrain_photometric_angles` being plain-Python (no ISIS subprocess) already follows.
    if wavelength_nm not in _HAPKE_CALIBRATION_WAVELENGTHS_NM:
        raise ValueError(
            f"wavelength_nm={wavelength_nm} is not one of the cube's own bands {_HAPKE_CALIBRATION_WAVELENGTHS_NM}"
        )
    band_offset = _HAPKE_CALIBRATION_WAVELENGTHS_NM.index(wavelength_nm) * len(_HAPKE_CALIBRATION_PARAM_ORDER)
    lon_0_360 = center_lon_deg % 360.0  # cube's own convention, per its embedded georeferencing

    with rasterio.open(cube_path) as src:
        (x,), (y,) = transform(geographic_crs(MOON_RADIUS_M), src.crs, [lon_0_360], [center_lat_deg])
        row, col = src.index(x, y)
        return {
            name: float(src.read(band_offset + i + 1, window=((row, row + 1), (col, col + 1)))[0, 0])
            for i, name in enumerate(_HAPKE_CALIBRATION_PARAM_ORDER)
        }


def fetch_real_hapke_params(
    center_lon_deg: float,
    center_lat_deg: float,
    config: TrntestConfig,
    wavelength_nm: int = DEFAULT_HAPKE_CALIBRATION_WAVELENGTH_NM,
) -> dict[str, float]:
    """Spatially-resolved Hapke parameters (Sato et al. 2014's fit) sampled at one ground point from
    ISIS's own calibration cube -- the `real_hapke_params=True` alternative to
    `_HAPKE_PLACEHOLDER_PARAMS`'s illustrative constants (see `hapke_shade_ortho`).

    :param center_lon_deg: Ground point longitude, degrees.
    :param center_lat_deg: Ground point latitude, degrees.
    :param config: Project config, passed to `_hapke_calibration_cube_path`.
    :param wavelength_nm: One of the cube's 7 bands (321/360/415/566/604/643/689); the default matches
        `config.lunaserv_ortho_layer`'s own wavelength (643nm, see docs/data-sources/lunaserv-wms.md).
    :returns: All 9 parameters (ISIS's native `Wh`/`Hg1`/`Hg2`/`Bc0`/`hc`/`B0`/`Hh`/`Theta`/`phi`
        parameterization), keyed lowercase to match `_HAPKE_PLACEHOLDER_PARAMS`'s own keys.
    """
    # `hapke_shade_ortho` uses only the 6 the simpler shadow-hiding-only `HAPKEHEN` model this project
    # calls accepts (`wh`, `hg1`, `hg2`, `hh`, `b0`, `theta`); `bc0`/`hc`/`phi` describe the fuller
    # Hapke model's separate coherent-backscatter term, which this cube sets to `0`/`1`/`0` globally
    # for this WAC-derived product -- unused by it, not a modeling choice made here.
    #
    # A single value per image (this function's own footprint center), not per-pixel: within one
    # ~143km candidate footprint, `wh`/`b0`/`hg1` vary only a few percent of each parameter's full-Moon
    # range -- secondary next to the placeholder-vs-calibrated gap this exists to fix (e.g. `b0`: 0.025
    # placeholder vs. ~1.5-2.2 calibrated, a ~60x difference). `hg2`/`hh` vary somewhat more within one
    # footprint, but still a smaller effect. Per-pixel sampling (reprojecting the calibration cube onto
    # the same working grid `reproject_astropedia_elevation_to_local_grid` builds the DEM/ortho on)
    # would be a further refinement, not implemented here -- see `docs/proposed-tasks/open-items.md`.
    path = _hapke_calibration_cube_path(config)
    return _sample_hapke_calibration(path, center_lon_deg, center_lat_deg, wavelength_nm)


def _hapke_reflectance(
    phase_deg: np.ndarray,
    incidence_deg: np.ndarray,
    emission_deg: np.ndarray,
    hapkehen_params: dict,
) -> np.ndarray:
    """Run ISIS `photomet` (`PHTNAME=HAPKEHEN`) as a pure Hapke-model evaluator.

    :param phase_deg: Phase angle, degrees -- any shape.
    :param incidence_deg: Incidence angle, degrees -- same shape as `phase_deg`.
    :param emission_deg: Emission angle, degrees -- same shape as `phase_deg`.
    :param hapkehen_params: `HAPKEHEN` model parameters (`wh`, `hg1`, `hg2`, `hh`, `b0`, `theta`).
    :returns: H(angles), the Hapke reflectance factor, same shape as `phase_deg`.
    """
    # `ANGLESOURCE=BACKPLANE`/`NORMNAME=SHADE` give H(angles) with no dependence on any actual image
    # content -- the `from` cube fed to `photomet` is always zeroed, and `NORMNAME=SHADE` overwrites it
    # with the model's own output regardless. Factored out of `hapke_shade_ortho` so it can call this
    # twice (candidate geometry, and the fixed reference geometry `luna_wac_normalized_reflectance` is
    # itself normalized to -- see `REFERENCE_INCIDENCE_DEG`'s own module-level comment) without
    # duplicating the `photomet` subprocess-orchestration logic. `photomet` also requires a `FROM` cube
    # purely as a size/dtype template (a `BandBin` label group is enough to open the file at all -- not
    # documented in `photomet.xml` -- added via `editlab` since GDAL's `ISIS3` writer doesn't create
    # one from scratch).
    #
    # Uses a call-scoped `tempfile.TemporaryDirectory()` for its own scratch cubes, not
    # `config.output_dir` (see `docs/intermediate-product-discipline.md`'s access-mode discipline):
    # fixed-name scratch cubes under a shared directory let two workers computing different entries'
    # shading concurrently race on the same filenames and corrupt each other's writes. Nothing here
    # persists or resumes across calls -- pure single-call scratch, so a call-scoped temp dir
    # (auto-deleted on return, success or exception) is the right fix, not a unique-but-persistent path,
    # which would just trade a collision bug for an accumulation one.
    with tempfile.TemporaryDirectory() as tmp_dir:
        work_dir = Path(tmp_dir)
        from_cub, phase_cub = work_dir / "hapke_from.cub", work_dir / "hapke_phase.cub"
        incidence_cub, emission_cub = work_dir / "hapke_incidence.cub", work_dir / "hapke_emission.cub"
        out_cub = work_dir / "hapke_out.cub"

        _write_backplane_cube(from_cub, np.zeros_like(incidence_deg, dtype=np.float32))
        _write_backplane_cube(phase_cub, phase_deg)
        _write_backplane_cube(incidence_cub, incidence_deg)
        _write_backplane_cube(emission_cub, emission_deg)

        run_quiet(["editlab", f"from={from_cub}", "options=addg", "grpname=BandBin"])
        run_quiet(["editlab", f"from={from_cub}", "options=addkey", "grpname=BandBin", "keyword=Center", "value=1.0"])

        run_quiet(
            [
                "photomet",
                f"from={from_cub}",
                f"to={out_cub}",
                "anglesource=backplane",
                f"phase_angle_file={phase_cub}",
                f"incidence_angle_file={incidence_cub}",
                f"emission_angle_file={emission_cub}",
                "phtname=hapkehen",
                *(f"{name}={value}" for name, value in hapkehen_params.items()),
                "zerob0standard=true",
                "normname=shade",
                "albedo=1.0",
                "incref=0.0",
            ]
        )

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            with rasterio.open(out_cub) as src:
                return src.read(1).astype(np.float64)


def hapke_shade_ortho(
    ortho: np.ndarray,
    dem: np.ndarray,
    bbox: tuple,
    camera: Camera,
    azimuth_deg: float,
    elevation_deg: float,
    cellsize_m: float,
    config: TrntestConfig,
    along_track_correction: bool = DEFAULT_ALONG_TRACK_CORRECTION,
    real_hapke_params: bool = DEFAULT_REAL_HAPKE_PARAMS,
) -> np.ndarray:
    """The default ortho-shading mode (`DEFAULT_HAPKE_SHADING`): relight `ortho` using a Hapke
    bidirectional reflectance function (ISIS `photomet`, `PHTNAME=HAPKEHEN`) instead of `shade_ortho`'s
    fallback plain Lambertian blend.

    :param ortho: Physical reflectance (WAC_EMP's own PDS4 archive tile, no embedded display stretch --
        see `reproject_wac_emp_reflectance_to_local_grid`'s docstring), not Lunaserv's old WMS-served
        `uint8` DN.
    :param dem: Elevation, meters, on the local orthographic grid described by `bbox`.
    :param bbox: `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param camera: The camera this ortho/DEM pair was fetched for.
    :param azimuth_deg: Sun azimuth, degrees.
    :param elevation_deg: Sun elevation, degrees.
    :param cellsize_m: DEM pixel size, meters.
    :param config: Project config, passed to `fetch_real_hapke_params` when `real_hapke_params=True`.
    :param along_track_correction: Apply `_terrain_photometric_angles`'s along-track correction using
        `camera`'s own along-track attitude axis. On by default.
    :param real_hapke_params: Use `fetch_real_hapke_params()`'s ISIS-calibration-cube-sourced Hapke
        coefficients (the default) rather than `_HAPKE_PLACEHOLDER_PARAMS`'s illustrative constants
        (kept for comparison in `notebooks/real_hapke_params.ipynb`).
    :returns: Relit reflectance directly (float64, physical units), not a display-ready `uint8` image
        -- `despeckle_and_shade_ortho` applies the separate `stretch_reflectance_to_uint8` cosmetic
        step afterward.
    """
    # The angle rasters come from `_terrain_photometric_angles`, computed from `camera`'s own position
    # (this ortho has no ISIS camera model for `photomet` to derive angles from automatically).
    # `along_track_correction`, on by default, is substantially more accurate against `campt` ground
    # truth than the base per-pixel-camera-position approach alone.
    # `photomet`'s own subprocess-orchestration mechanics live in the shared `_hapke_reflectance`
    # helper -- see its own docstring. `_HAPKE_PLACEHOLDER_PARAMS` is illustrative, not
    # lunar-calibrated -- a feasibility prototype, not a validated photometric model; see
    # `fetch_real_hapke_params`'s own docstring for what its calibrated alternative does and doesn't
    # capture.
    #
    # Relights `ortho` by the ratio H(i,e,g)/H(reference), not a bare rescaled H(i,e,g): `ortho` isn't
    # raw albedo -- it's already a photometric composite, normalized to a fixed reference geometry
    # (`REFERENCE_INCIDENCE_DEG`'s own module-level comment explains the reference geometry and why
    # it's only an approximate, not exact, correction). Multiplying by a bare, arbitrarily-rescaled
    # H(i,e,g) double-counts the photometric function with no relationship to the geometry the texture
    # was actually produced at -- not a relighting operation, just an unprincipled per-pixel contrast
    # rescale. Dividing by H(reference) instead makes the multiplier a physically meaningful relighting
    # ratio (~1 for geometries close to the reference, deviating for geometries far from it), so no
    # separate display-range rescale is needed beyond simple `[0, 255]` clipping. An unusually bright
    # opposition-surge patch legitimately saturating toward white, rather than the whole frame being
    # dimmed to keep it in range, is treated as the physically correct behavior here, not an artifact
    # to avoid -- but that stance was never validated against real candidates and isn't a settled
    # design decision; whether/how often this saturation actually happens, and whether it's acceptable,
    # is an open question, see `docs/proposed-tasks/open-items.md`. Like `_terrain_photometric_angles`'s own
    # normal-tilt correction, this has no opt-out parameter, despite being confirmed to worsen the
    # brightness-matched diff against a WAC crop for the one candidate tested so far.
    #
    # `_terrain_photometric_angles`'s own 3D-geometry terrain embedding is unconditional, with no
    # parameter here to control it.
    reflectance, hapkehen_params = real_geometry_hapke_reflectance(
        dem, bbox, camera, azimuth_deg, elevation_deg, cellsize_m, config, along_track_correction, real_hapke_params
    )
    reference_reflectance = reference_hapke_reflectance(hapkehen_params)

    if reference_reflectance > 0 and np.isfinite(reference_reflectance):
        ratio = reflectance / reference_reflectance
    else:
        ratio = np.zeros_like(reflectance)

    return ortho.astype(np.float64) * ratio


def stretch_reflectance_to_uint8(
    reflectance: np.ndarray,
    lo: float = DISPLAY_STRETCH_REFLECTANCE_MIN,
    hi: float = DISPLAY_STRETCH_REFLECTANCE_MAX,
) -> np.ndarray:
    """Apply a fixed linear stretch to convert reflectance to a display-ready `uint8` image.

    :param reflectance: Physical reflectance (e.g. `hapke_shade_ortho`'s own output).
    :param lo: Reflectance mapped to 0.
    :param hi: Reflectance mapped to 255.
    :returns: The stretched `uint8` image, clipped to `[0, 255]`.
    """
    # The one, explicit place relit reflectance becomes a display-ready image -- a fixed range, not a
    # per-image adaptive/percentile stretch (see `DISPLAY_STRETCH_REFLECTANCE_MIN`/`_MAX`'s own
    # module-level comment for why), applied once at the very end of the pipeline rather than baked
    # into the input texture the way Lunaserv's old, uncorrected WMS display stretch effectively was
    # (see docs/data-sources/lunaserv-wms.md). Purely cosmetic -- has no bearing on this module's
    # photometric physics, all of which is already complete by the time this runs.
    #
    # `DISPLAY_STRETCH_REFLECTANCE_MAX = 0.30` was confirmed non-saturating for exactly one candidate,
    # not swept across others. Whether/how often input actually exceeds it (clipping to 255, with the
    # downstream effects that has -- e.g. biasing `sfs_validation.true_albedo_map`'s recovered albedo
    # at those pixels) is an open question, not a validated non-issue -- see `docs/proposed-tasks/open-items.md`.
    normalized = (reflectance.astype(np.float64) - lo) / (hi - lo)
    return np.clip(normalized * 255.0, 0, 255).astype(np.uint8)


def real_geometry_photometric_angles(
    dem: np.ndarray,
    bbox: tuple,
    camera: Camera,
    azimuth_deg: float,
    elevation_deg: float,
    cellsize_m: float,
    along_track_correction: bool = DEFAULT_ALONG_TRACK_CORRECTION,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """`(incidence_deg, emission_deg, phase_deg)` at `camera`'s own per-pixel geometry.

    :param dem: Elevation, meters, on the local orthographic grid described by `bbox`.
    :param bbox: `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param camera: The camera this ortho/DEM pair was fetched for.
    :param azimuth_deg: Sun azimuth relative to `camera`'s footprint center's local horizon, degrees.
    :param elevation_deg: Sun elevation relative to that same local horizon, degrees.
    :param cellsize_m: DEM pixel size, meters.
    :param along_track_correction: Apply the along-track correction using `camera`'s own along-track
        attitude axis (`camera.camera_along_track_direction_moon_me`). On by default.
    :returns: `(incidence_deg, emission_deg, phase_deg)`, each the same shape as `dem`.
    """
    # `_terrain_photometric_angles` given `camera`'s MOON_ME position (`camera.camera_center_moon_me_m`,
    # used directly -- no local-frame conversion, see `_terrain_photometric_angles`'s own docstring).
    # Factored out of `real_geometry_hapke_reflectance` for callers that only need the angles
    # themselves, not a Hapke evaluation -- e.g. `sfs_validation.py`'s Lambertian-mode incidence
    # cross-check, which compares this function's own `incidence_deg` directly against `sfs`'s
    # independently ray-traced one.
    #
    # `azimuth_deg`/`elevation_deg` are the one place this function still does a local-frame-to-MOON_ME
    # conversion (`_moon_me_direction_from_local_enu`), since it's the human-readable convention every
    # caller/test/notebook uses and `shade_ortho`'s Lambertian fallback needs az/el regardless for
    # matplotlib's `LightSource` API. This conversion is exact and lossless (a free direction rotated
    # between orthonormal frames, not a position embedding) -- see `_terrain_photometric_angles`'s own
    # docstring.
    center = camera.footprint_lonlat_deg["center"]
    assert center is not None, "camera's nadir footprint center must be a real ground point"
    center_lon_deg, center_lat_deg = center
    az_rad, el_rad = math.radians(90.0 - azimuth_deg), math.radians(elevation_deg)
    sun_local_enu = np.array(
        [math.cos(az_rad) * math.cos(el_rad), math.sin(az_rad) * math.cos(el_rad), math.sin(el_rad)]
    )
    sun_direction_moon_me = _moon_me_direction_from_local_enu(sun_local_enu, center_lon_deg, center_lat_deg)
    along_track_direction_moon_me = camera.camera_along_track_direction_moon_me if along_track_correction else None
    return _terrain_photometric_angles(
        dem,
        bbox,
        center_lon_deg,
        center_lat_deg,
        camera.camera_center_moon_me_m,
        sun_direction_moon_me,
        cellsize_m,
        MOON_RADIUS_M,
        along_track_direction_moon_me,
    )


def real_geometry_hapke_reflectance(
    dem: np.ndarray,
    bbox: tuple,
    camera: Camera,
    azimuth_deg: float,
    elevation_deg: float,
    cellsize_m: float,
    config: TrntestConfig,
    along_track_correction: bool = DEFAULT_ALONG_TRACK_CORRECTION,
    real_hapke_params: bool = DEFAULT_REAL_HAPKE_PARAMS,
) -> tuple[np.ndarray, dict]:
    """H(i,e,g) at `camera`'s own per-pixel geometry.

    :param dem: Elevation, meters, on the local orthographic grid described by `bbox`.
    :param bbox: `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param camera: The camera this ortho/DEM pair was fetched for.
    :param azimuth_deg: Sun azimuth, degrees.
    :param elevation_deg: Sun elevation, degrees.
    :param cellsize_m: DEM pixel size, meters.
    :param config: Project config, passed to `fetch_real_hapke_params` when `real_hapke_params=True`.
    :param along_track_correction: Passed through to `real_geometry_photometric_angles`.
    :param real_hapke_params: Use calibrated (vs. placeholder) Hapke parameters.
    :returns: `(reflectance, hapkehen_params)` -- the params dict too, since a caller building an
        albedo map also needs it for `sfs_validation.hapke_params_to_asp_model_coeffs`.
    """
    # The same computation `hapke_shade_ortho` itself makes to build its H(candidate)/H(reference)
    # relighting ratio, factored out so `sfs_validation.true_albedo_map` can divide this exact factor
    # back out of an already-shaded ortho to recover the geometry-independent albedo
    # `hapke_shade_ortho` started from, without duplicating this setup or risking it drifting out of
    # sync with `hapke_shade_ortho`'s own computation.
    incidence_deg, emission_deg, phase_deg = real_geometry_photometric_angles(
        dem, bbox, camera, azimuth_deg, elevation_deg, cellsize_m, along_track_correction
    )

    center = camera.footprint_lonlat_deg["center"]
    assert center is not None, "camera's nadir footprint center must be a real ground point"
    if real_hapke_params:
        hapkehen_source = fetch_real_hapke_params(*center, config)
    else:
        hapkehen_source = _HAPKE_PLACEHOLDER_PARAMS
    hapkehen_params = hapkehen_params_from_source(hapkehen_source)

    reflectance = _hapke_reflectance(phase_deg, incidence_deg, emission_deg, hapkehen_params)
    return reflectance, hapkehen_params


def hapkehen_params_from_source(hapkehen_source: dict) -> dict:
    """Slice a params dict down to the 6 keys the `HAPKEHEN` model this project calls actually accepts.

    :param hapkehen_source: `_HAPKE_PLACEHOLDER_PARAMS`, or `fetch_real_hapke_params`'s wider 9-key
        return.
    :returns: Dict with exactly `wh`, `hg1`, `hg2`, `hh`, `b0`, `theta`.
    """
    # Shared by `hapke_shade_ortho` and `sfs_validation.py`'s own use of the same calibration source, so
    # both stay in sync about which 6 of `fetch_real_hapke_params`'s 9 keys are the ones that matter
    # here.
    return {name: hapkehen_source[name] for name in _HAPKE_PLACEHOLDER_PARAMS}


def reference_hapke_reflectance(hapkehen_params: dict) -> float:
    """H(reference), the Hapke reflectance factor at the fixed reference geometry `ortho` is itself
    normalized to.

    :param hapkehen_params: `HAPKEHEN` model parameters (see `hapkehen_params_from_source`).
    :returns: The scalar H(reference) value.
    """
    # The shared denominator both `hapke_shade_ortho`'s relighting ratio and
    # `sfs_validation.true_albedo_map`'s "true albedo" proxy (`ortho / H(reference)`, undoing the same
    # normalization from the other direction) divide by. See `REFERENCE_INCIDENCE_DEG`'s own
    # module-level comment for what the reference geometry is. A small constant-valued array (no
    # per-pixel variation possible, same params/angles everywhere), not a full-size raster pass --
    # `_hapke_reflectance`'s own `photomet` call still needs an array shape to run against, hence the
    # `(3, 3)` filler.
    reference_shape = (3, 3)
    return _hapke_reflectance(
        np.full(reference_shape, REFERENCE_PHASE_DEG),
        np.full(reference_shape, REFERENCE_INCIDENCE_DEG),
        np.full(reference_shape, REFERENCE_EMISSION_DEG),
        hapkehen_params,
    )[0, 0]


def despeckle_and_shade_ortho(
    ortho_path,
    dem_path,
    camera: Camera,
    output_path,
    config: TrntestConfig,
    bbox: tuple,
    hapke: bool = DEFAULT_HAPKE_SHADING,
    along_track_correction: bool = DEFAULT_ALONG_TRACK_CORRECTION,
    real_hapke_params: bool = DEFAULT_REAL_HAPKE_PARAMS,
    ortho_source: str = DEFAULT_ORTHO_SOURCE,
) -> None:
    """Despeckle the fetched ortho and blend in a sun-lit hillshade computed from the (already
    hole-filled) DEM, writing the result to `output_path`.

    :param ortho_path: Fetched raw ortho GeoTIFF path.
    :param dem_path: Hole-filled DEM GeoTIFF path.
    :param camera: The camera this ortho/DEM pair was fetched for.
    :param output_path: Where to write the shaded `uint8` ortho -- the single ortho used by both
        `sat_sim` and every display panel (see `fetch_dem_and_ortho`).
    :param config: Project config, passed through to `hapke_shade_ortho`.
    :param bbox: `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param hapke: Use `hapke_shade_ortho`'s ISIS-`photomet`-backed Hapke shading (the default);
        `hapke=False` falls back to the plain Lambertian `shade_ortho` blend.
    :param along_track_correction: Passed straight through to `hapke_shade_ortho` (a no-op when
        `hapke=False`).
    :param real_hapke_params: Passed straight through to `hapke_shade_ortho` (a no-op when
        `hapke=False`).
    :param ortho_source: Which ortho/texture source `ortho_path` came from (`ORTHO_SOURCES`) --
        affects how the Lambertian fallback (`hapke=False`) normalizes the input.
    """
    # `hapke=True`'s branch applies `stretch_reflectance_to_uint8` explicitly, right here, to
    # `hapke_shade_ortho`'s relit-reflectance output -- the one place that cosmetic display step
    # happens.
    #
    # `hapke=False`'s branch also needs `stretch_reflectance_to_uint8` applied before the ortho reaches
    # `shade_ortho` when `ortho_source="wac_emp_pds"`: `shade_ortho` assumes its `ortho` input is
    # already `[0, 255]` DN (see its own docstring for why it's kept that way rather than generalized),
    # but `cleaned` is physical reflectance (~0.05-0.3) when the source is WAC_EMP -- `shade_ortho`'s
    # own internal `/255.0` then `*255.0` round-trip cancels for values this small, truncating the
    # whole result to an all-zero, fully black image under `.astype(np.uint8)`. Applying the display
    # stretch first, turning the array back into DN-like `[0, 255]` before handing it to `shade_ortho`,
    # fixes this without touching `shade_ortho` itself. `ortho_source="lunaserv_wms"` skips this (its
    # `cleaned` is already DN, `shade_ortho`'s own native convention).
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
    if hapke:
        relit_reflectance = hapke_shade_ortho(
            cleaned,
            dem,
            bbox,
            camera,
            azimuth_deg,
            elevation_deg,
            config.dem_target_gsd_m,
            config,
            along_track_correction=along_track_correction,
            real_hapke_params=real_hapke_params,
        )
        shaded = stretch_reflectance_to_uint8(relit_reflectance)
    else:
        lambertian_input = stretch_reflectance_to_uint8(cleaned) if ortho_source == "wac_emp_pds" else cleaned
        shaded = shade_ortho(lambertian_input, dem, azimuth_deg, elevation_deg, config.dem_target_gsd_m)

    profile.update(count=1, dtype="uint8")
    with atomic_publish(Path(output_path)) as tmp:
        with rasterio.open(tmp, "w", **profile) as dst:
            dst.write(shaded, 1)


def result_from_files(ortho_path: Path, dem_path: Path) -> DemOrthoResult:
    """Reconstruct a `DemOrthoResult` from an already-generated ortho/DEM pair on disk -- pure IO, no
    fetching.

    :param ortho_path: Shaded ortho GeoTIFF path.
    :param dem_path: Hole-filled DEM GeoTIFF path.
    :returns: A `DemOrthoResult` with `bbox`/`width`/`height` read back from `ortho_path`'s own
        embedded georeferencing.
    """
    # So `trn_dataset.TrnTestEntry.dem_ortho_result` can resume from a prior `generate()` run's output
    # instead of re-fetching from Lunaserv/Astropedia. `bbox`/`width`/`height` are read back rather than
    # recomputed or stored separately: `_reproject_raster_to_local_grid` (via
    # `despeckle_and_shade_ortho`, which carries the fetched ortho's own `profile` through unchanged)
    # writes `dst_transform`/`dst_crs` from exactly this same `bbox`/`width`/`height` at fetch time, so
    # reading them back from the file is an exact round-trip -- the same "raster's own georeferencing
    # is authoritative" pattern `isis_wac._orthographic_map_pvl` relies on elsewhere.
    with rasterio.open(ortho_path) as src:
        width, height = src.width, src.height
        bbox = (src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top)
    return DemOrthoResult(ortho=Path(ortho_path), dem=Path(dem_path), bbox=bbox, width=width, height=height)


@dataclasses.dataclass(frozen=True)
class DemFetchResult:
    """The entry's one DEM, as returned by `fetch_dem`.

    :ivar dem: Path to the hole-filled DEM GeoTIFF.
    :ivar bbox: `(minx, miny, maxx, maxy)`, meters, the padded local-CRS working grid it was fetched
        onto -- `fetch_and_shade_ortho` must reuse this exactly (never re-derive) for its own ortho
        fetch, so the two can't disagree about the AOI.
    :ivar width: Raster width, pixels.
    :ivar height: Raster height, pixels.
    """

    dem: Path
    bbox: tuple
    width: int
    height: int


@writes_product("dem_filled")
def fetch_dem(
    camera: Camera, config: TrntestConfig | None = None, extra_footprint_lonlat_deg: dict | None = None
) -> DemFetchResult:
    """The entry's one DEM fetch.

    :param camera: Camera whose footprint determines the fetch AOI.
    :param config: Project config; `load_config()` if not given.
    :param extra_footprint_lonlat_deg: Extra corners to union into the AOI before padding, if given.
    :returns: A `DemFetchResult` for the fetched, hole-filled DEM.
    """
    # Split out of the old combined `fetch_dem_and_ortho` so `product_registry` has exactly one
    # legible, checkable writer for the `"dem_filled"` label (principle 2), decoupled from the
    # ortho-shading concern (`fetch_and_shade_ortho`, an intentional variant family -- multiple valid
    # shaded orthos by design, principle 1) that used to be fused into the same function.
    #
    # Still takes `extra_footprint_lonlat_deg` as a caller-suppliable parameter -- principle 1's "no
    # caller-supplied parameter should be able to change identity" isn't fully closed by this split.
    # `dem_filled_path`'s own filename still doesn't encode this parameter (unlike
    # `ortho_shaded_filename`'s suffix discipline for its own parameters), so two calls against the
    # same output directory with different footprints can still silently disagree about "the" DEM --
    # see `docs/proposed-tasks/open-items.md` for what a full fix would need. Not solved here: this phase only
    # makes the current single writer legible/auditable (`writes_product`) and its file write atomic
    # (`atomic_publish`, in `reproject_astropedia_elevation_to_local_grid`), not the filename-collision
    # gap itself -- flagged rather than silently assumed fixed.
    config = config or load_config()
    config.output_dir.mkdir(parents=True, exist_ok=True)

    center = camera.footprint_lonlat_deg["center"]
    assert center is not None, "camera's nadir footprint center must be a real ground point"
    center_lon, center_lat = center
    unpadded_bbox = footprint_bbox_local_m(camera.footprint_lonlat_deg, center_lon, center_lat, MOON_RADIUS_M)
    if extra_footprint_lonlat_deg is not None:
        unpadded_bbox = union_bbox(
            unpadded_bbox,
            footprint_bbox_local_m(extra_footprint_lonlat_deg, center_lon, center_lat, MOON_RADIUS_M),
        )
    bbox = pad_bbox(unpadded_bbox, config.dem_padding_fraction)
    width, height = pixel_dims_for_gsd(bbox, config.dem_target_gsd_m)
    print(f"ROI center (lon,lat deg): {center}, bbox (local m): {bbox}")
    print(f"ROI size {width}x{height} px (~{config.dem_target_gsd_m} m/px)")

    # Live default DEM source: USGS Astropedia's flat-file GLD100, not Lunaserv's WMS -- see
    # docs/data-sources/astropedia-gld100.md. `fetch_dem_astropedia` ensures the whole ~10GB file is
    # downloaded/cached locally once (raises if this camera's footprint needs data outside the file's
    # +-79 deg latitude coverage -- no silent fallback to the deprecated Lunaserv-native path), then
    # `reproject_astropedia_elevation_to_local_grid` reads just this AOI from the local file and
    # reprojects it onto this same local-CRS grid -- already elevation (not planetocentric radius), so
    # `radius_to_elevation` is skipped.
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
        MOON_RADIUS_M,
        dem_elevation_path,
    )

    dem_filled_path = config.output_dir / "dem_filled-tile-0.tif"
    hole_fill_dem(dem_elevation_path, dem_filled_path)
    return DemFetchResult(dem=dem_filled_path, bbox=bbox, width=width, height=height)


@writes_product("ortho_shaded")
def fetch_and_shade_ortho(
    camera: Camera,
    dem: DemFetchResult,
    config: TrntestConfig | None = None,
    hapke: bool = DEFAULT_HAPKE_SHADING,
    along_track_correction: bool = DEFAULT_ALONG_TRACK_CORRECTION,
    real_hapke_params: bool = DEFAULT_REAL_HAPKE_PARAMS,
    ortho_source: str = DEFAULT_ORTHO_SOURCE,
) -> DemOrthoResult:
    """The ortho-shading half of the old combined `fetch_dem_and_ortho`, split out alongside
    `fetch_dem` -- see that function's own docstring for why.

    :param camera: Camera whose footprint determines the fetch AOI.
    :param dem: `fetch_dem`'s output; its `bbox`/`width`/`height` are reused exactly, never re-derived.
    :param config: Project config; `load_config()` if not given.
    :param hapke: Use ISIS `photomet`'s Hapke model (the default, via `despeckle_and_shade_ortho`'s
        `hapke` passthrough); `hapke=False` falls back to the plain Lambertian `shade_ortho` blend.
    :param along_track_correction: Passed through to `hapke_shade_ortho`. On by default.
    :param real_hapke_params: Passed through to `hapke_shade_ortho`. On by default.
    :param ortho_source: Which ortho/texture source to fetch before shading (`ORTHO_SOURCES`):
        `"wac_emp_pds"` (live default) fetches WAC_EMP's own reflectance directly from its PDS4
        archive -- physical reflectance, no embedded display stretch. `"lunaserv_wms"` is the
        deprecated fallback (the original Lunaserv WMS layer), which carries an uncorrected affine
        display stretch -- see docs/data-sources/lunaserv-wms.md.
    :returns: A `DemOrthoResult` for the fetched, shaded ortho (paired with `dem`).
    :raises ValueError: If `ortho_source` isn't one of `ORTHO_SOURCES`, or (for `"wac_emp_pds"`) if the
        camera's footprint needs latitude beyond WAC_EMP's own equirect-tile coverage or straddles a
        tile boundary (`wac_emp_tile_id_for_bbox`) -- no silent fallback to `"lunaserv_wms"` in that
        case; a caller that wants the fallback has to ask for it explicitly.
    """
    # Taking `dem` (`fetch_dem`'s output) as an input and always reusing its `bbox`/`width`/`height`
    # exactly closes the entanglement `fetch_dem`'s docstring describes for the DEM/ortho pairing
    # specifically: the two can no longer fetch against two different bboxes. The DEM's own
    # filename-collision gap against a different `fetch_dem` call is still open, as noted there.
    #
    # `ortho_source="lunaserv_wms"` is only numerically coherent with `hapke=False`:
    # `hapke_shade_ortho` assumes its `ortho` input is already reflectance (see its own docstring),
    # which `"lunaserv_wms"`'s raw WMS DN is not (DN under an unknown, non-trivial affine stretch).
    # `shade_ortho`'s plain-Lambertian fallback is the one that still speaks `"lunaserv_wms"`'s own DN
    # convention unchanged. No code-level guard against this combination -- just don't request it.
    #
    # `_terrain_photometric_angles`'s own curvature-aware surface normal is unconditionally applied
    # (not a parameter here at all, see that function's docstring). Each
    # `hapke`/`along_track_correction`/`real_hapke_params` combination writes to its own filename
    # (`ortho_shaded_filename`) rather than a single shared one, so any combination can be fetched for
    # the same camera and compared directly (e.g. `notebooks/hapke_hillshade.ipynb`/
    # `notebooks/along_track_correction.ipynb`/`notebooks/real_hapke_params.ipynb`), and so
    # `trn_dataset.TrnTestEntry.dem_ortho_result`'s resumption check can never mistake one mode's
    # cached file for another's.
    #
    # `bbox`/`width`/`height` (the fetch AOI, already unioned with whatever footprint `fetch_dem` was
    # given -- e.g. `tie_points.crop_footprint_corners_for_camera`'s WAC crop footprint, which isn't
    # always the same size/shape as the synthetic camera's own FOV) all come from `dem`, not
    # recomputed here -- see `fetch_dem`'s own docstring for that computation and its remaining
    # caveats (the ray-traced-estimate-vs-crop margin, the still-open filename-collision gap).
    if ortho_source not in ORTHO_SOURCES:
        raise ValueError(f"ortho_source={ortho_source!r} is not one of {ORTHO_SOURCES!r}")
    config = config or load_config()
    config.output_dir.mkdir(parents=True, exist_ok=True)
    bbox, width, height = dem.bbox, dem.width, dem.height

    center = camera.footprint_lonlat_deg["center"]
    assert center is not None, "camera's nadir footprint center must be a real ground point"
    center_lon, center_lat = center
    # A per-camera local Orthographic CRS (Lunaserv's `IAU2000:30166`, parametrized by this
    # footprint's own center) rather than Lunaserv's native unprojected geographic grid
    # (`IAU2000:30100`) -- the geographic grid's degree-pixels are anisotropic away from the equator
    # (a degree of longitude covers less ground distance than a degree of latitude), and ASP's
    # `mapproject --ref-map` (see `render.run_mapproject`) doesn't preserve that anisotropy: it copies
    # the reference grid's x-resolution onto the y-axis too, silently stretching any `--ref-map`'d
    # output vertically by up to `1/cos(lat)`. A local Orthographic projection has square meter pixels
    # everywhere, so that mismatch can't arise in the first place. `IAU2000:30166` reports the Moon's
    # 1,737,400 m radius (unlike the generic OGC `AUTO:42003` Orthographic code, which is hardcoded to
    # Earth's WGS84 ellipsoid) -- see docs/data-sources/lunaserv-wms.md.
    srs = config.lunaserv_srs_template.format(c_lon=center_lon, c_lat=center_lat)

    if ortho_source == "wac_emp_pds":
        # Live default: WAC_EMP's own reflectance, fetched directly from its PDS4 archive rather than
        # through Lunaserv's WMS render -- the WMS layer's DN carries an uncorrected affine display
        # stretch, not raw reflectance. `fetch_wac_emp_reflectance` raises if this footprint needs a
        # tile beyond the archive's own equirect coverage (see its own docstring) -- no silent
        # fallback to the deprecated Lunaserv path below.
        wac_emp_path, wac_emp_product_id = fetch_wac_emp_reflectance(bbox, center_lon, center_lat, config)
        print(f"WAC_EMP tile: {wac_emp_product_id}")
        ortho_path = config.output_dir / "ortho_wac_emp.tif"
        reproject_wac_emp_reflectance_to_local_grid(
            wac_emp_path, bbox, width, height, center_lon, center_lat, MOON_RADIUS_M, ortho_path
        )
    else:
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
    ortho_shaded_path = config.output_dir / ortho_shaded_filename(
        hapke, along_track_correction, real_hapke_params, ortho_source
    )
    despeckle_and_shade_ortho(
        ortho_path,
        dem.dem,
        camera,
        ortho_shaded_path,
        config,
        bbox,
        hapke=hapke,
        along_track_correction=along_track_correction,
        real_hapke_params=real_hapke_params,
        ortho_source=ortho_source,
    )

    return DemOrthoResult(
        ortho=ortho_shaded_path,
        dem=dem.dem,
        bbox=bbox,
        width=width,
        height=height,
    )


def fetch_dem_and_ortho(
    camera: Camera,
    config: TrntestConfig | None = None,
    extra_footprint_lonlat_deg: dict | None = None,
    hapke: bool = DEFAULT_HAPKE_SHADING,
    along_track_correction: bool = DEFAULT_ALONG_TRACK_CORRECTION,
    real_hapke_params: bool = DEFAULT_REAL_HAPKE_PARAMS,
    ortho_source: str = DEFAULT_ORTHO_SOURCE,
) -> DemOrthoResult:
    """Compose `fetch_dem` + `fetch_and_shade_ortho`.

    :param camera: Camera whose footprint determines the fetch AOI.
    :param config: Project config; `load_config()` if not given.
    :param extra_footprint_lonlat_deg: Extra corners to union into the AOI before padding, if given.
    :param hapke: Passed through to `fetch_and_shade_ortho`.
    :param along_track_correction: Passed through to `fetch_and_shade_ortho`.
    :param real_hapke_params: Passed through to `fetch_and_shade_ortho`.
    :param ortho_source: Passed through to `fetch_and_shade_ortho`.
    :returns: A `DemOrthoResult` for the fetched DEM/ortho pair.
    """
    # See `fetch_dem`/`fetch_and_shade_ortho`'s own docstrings for what's now individually
    # `product_registry`-decorated, and for the DEM filename-collision gap that split doesn't itself
    # close.
    dem = fetch_dem(camera, config, extra_footprint_lonlat_deg)
    return fetch_and_shade_ortho(
        camera,
        dem,
        config,
        hapke=hapke,
        along_track_correction=along_track_correction,
        real_hapke_params=real_hapke_params,
        ortho_source=ortho_source,
    )
