"""Fetch DEM + ortho imagery from Lunaserv WMS for the ground footprint computed by `camera.build_camera`,
and prep both for `sat_sim`: the DEM as elevation (not raw radius) and hole-filled, the ortho
despeckled and blended with a real-sun-lit hillshade (`sat_sim` applies no illumination model of its
own -- see docs/data-sources.md -- so any relief in the synthetic render has to already be in this
ortho). See docs/data-sources.md and docs/caching.md.
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
# each parameter's documented valid range (see ISIS's photomet.xml), not calibrated against real
# lunar photometry. This is a feasibility prototype for evaluating ISIS `photomet` as a hillshade
# replacement (see docs/history.md's dated entry), not a validated reflectance model. Kept as the
# `real_hapke_params=False` fallback -- see `fetch_real_hapke_params()` below for the real alternative and
# docs/history.md's dated entry for the placeholder-vs-real comparison that motivated it.
_HAPKE_PLACEHOLDER_PARAMS = {"wh": 0.52, "hg1": 0.213, "hg2": 1.0, "hh": 0.17, "b0": 0.025, "theta": 0.0}

# The ortho texture this project fetches (`config.lunaserv_ortho_layer`, `luna_wac_normalized_
# reflectance`) is itself a photometric composite, not raw albedo -- confirmed (2026-08-22,
# docs/history.md) to be ASU/LROC's WAC_EMP product (PDS4 LROLRC_2001/DATA/MDR/WAC_EMP; the exact
# match on wavelength *and* WAC_EMP's own unusual "643nm offered at 304 ppd" special case is too
# specific to be coincidence), whose own README states every pixel is "photometrically normalized to
# a standard geometry of 30 degrees incidence angle, 0 degrees emission angle, and 30 degrees phase
# angle" using "an empirically derived photometric function similar to that of Boyd et al. (2012)" --
# not Hapke. `hapke_shade_ortho` relights this texture for a real candidate's own true geometry by
# multiplying by H(i,e,g)/H(these reference angles), where H is our own Hapke evaluation
# (`_hapke_reflectance`) -- a principled approximation of undoing Boyd et al.'s empirical
# normalization (not a bit-exact inverse, since it isn't a Hapke-family function), but the Sato et
# al. (2014) Hapke parameters this project already samples (`fetch_real_hapke_params`) were
# themselves derived from/cross-validated against this same WAC photometric dataset, so it's not an
# arbitrary stand-in either. See docs/history.md's dated entry for the full investigation.
REFERENCE_INCIDENCE_DEG = 30.0
REFERENCE_EMISSION_DEG = 0.0
REFERENCE_PHASE_DEG = 30.0

# ISIS's own real, spatially-resolved lunar Hapke calibration (Sato et al. 2014's fit, converted to
# ISIS's native parameterization) -- $ISISDATA/lro/calibration/
# WAC_global_7bands_1x1_wbhs70NS_const_each_pole.<version>.cub, a 63-band (7 wavelengths x 9 params)
# global 1deg/px cube. Already part of this project's existing `isis_wac.ensure_isisdata` fetch (the
# `lro` ISIS data package `lrowaccal`/`spiceinit` already need), not a new download. See
# `fetch_real_hapke_params()` below and docs/history.md's dated entry.
_HAPKE_CALIBRATION_WAVELENGTHS_NM = (321, 360, 415, 566, 604, 643, 689)
_HAPKE_CALIBRATION_PARAM_ORDER = ("wh", "hg1", "hg2", "bc0", "hc", "b0", "hh", "theta", "phi")
_HAPKE_CALIBRATION_CUBE_GLOB = "WAC_global_7bands_1x1_wbhs70NS_const_each_pole.*.cub"
# Matches `config.lunaserv_ortho_layer`'s own real wavelength (`luna_wac_normalized_reflectance`,
# 643nm -- see docs/data-sources.md) -- the calibration should describe the same real imagery being
# shaded.
DEFAULT_HAPKE_CALIBRATION_WAVELENGTH_NM = 643

# `fetch_dem_and_ortho`/`despeckle_and_shade_ortho`'s own `hapke`/`along_track_correction`/
# `real_hapke_params` parameter defaults -- shared with `trn_dataset.TrnTestEntry.dem_ortho_result`'s
# resumption check (via `ortho_shaded_filename` below) so the two can never silently disagree about
# which shading mode's cached ortho file is "the" default one to resume from. `shade_ortho`'s plain
# Lambertian blend (`hapke=False`), the uncorrected per-pixel geometry (`along_track_correction=False`),
# and the illustrative placeholder Hapke coefficients (`real_hapke_params=False`) all remain available
# as explicit fallbacks -- see docs/history.md's dated entries for why each became the default.
#
# The DEM-gradient normal-tilt correction (Phase 70/71) and the Hapke-ratio relighting correction
# (Phase 72) are **not** toggles -- both are unconditional in `_terrain_photometric_angles`/
# `hapke_shade_ortho` respectively, with no parameter to opt out. This is a deliberate user call
# (2026-08-22, docs/history.md's Phase 72 entry): both are judged physically correct/well-justified
# on their own terms (independent real-`campt` validation for the normal-tilt fix; WAC_EMP's own
# documented i=30/e=0/g=30 empirical-normalization convention for the Hapke-ratio fix), even though
# neither is confirmed to improve, and the Hapke-ratio fix is confirmed to *worsen*, the
# brightness-matched diff against the real WAC crop for the one candidate tested so far -- that
# real-image regression remains open and unexplained, but is no longer treated as a reason to keep
# either correction optional.
DEFAULT_HAPKE_SHADING = True
DEFAULT_ALONG_TRACK_CORRECTION = True
DEFAULT_REAL_HAPKE_PARAMS = True

# `fetch_dem_and_ortho`'s ortho-texture source. "wac_emp_pds" (live default, 2026-08-23,
# docs/history.md) fetches WAC_EMP's own real reflectance directly from its PDS4 archive
# (`fetch_wac_emp_reflectance`/`reproject_wac_emp_reflectance_to_local_grid`) -- real physical
# reflectance, no embedded display stretch. "lunaserv_wms" is the deprecated fallback (the original
# `luna_wac_normalized_reflectance` WMS layer), kept reachable for comparison but confirmed to carry a
# real, uncorrected affine display stretch (not raw reflectance) -- see docs/data-sources.md.
DEFAULT_ORTHO_SOURCE = "wac_emp_pds"
ORTHO_SOURCES = ("wac_emp_pds", "lunaserv_wms")

# `hapke_shade_ortho`'s final, purely cosmetic reflectance->uint8 display stretch (`stretch_
# reflectance_to_uint8`) -- deliberately a fixed, documented linear range (not a per-image
# adaptive/percentile stretch), matching this project's general preference for deterministic behavior
# over data-dependent behavior. Chosen to keep typical real lunar reflectance at WAC_EMP's own
# i=30/e=0/g=30 reference geometry (mare ~0.05-0.10, highlands ~0.12-0.20, fresh crater
# rays/ejecta up to ~0.3) inside [0, 255] without saturating most of a typical scene -- not asserted to
# reproduce the old Lunaserv WMS stretch's specific numbers (not required, see the implementation plan
# this followed). Applied once, at the very end of the pipeline, to `relit_reflectance`
# (`hapke_shade_ortho`'s own real-units output) -- deliberately *after* all physics (relighting), not
# baked into the input texture the way Lunaserv's old WMS stretch effectively was.
DISPLAY_STRETCH_REFLECTANCE_MIN = 0.0
DISPLAY_STRETCH_REFLECTANCE_MAX = 0.30


def geographic_crs(radius_m: float = MOON_RADIUS_M) -> str:
    """Plain (unprojected) geographic PROJ4 CRS string for the Moon on a sphere of `radius_m` --
    the one shared source of truth for this string, since `rasterio.warp` needs an explicit CRS to
    treat a set of coordinates as lon/lat degrees, not just "these are degrees". Every site in this
    project that used to build this string inline (here, `craters.py`, `control_network.py`,
    `plotting.py`, and now `tie_points.py`) calls this instead, so they can't drift apart."""
    return f"+proj=longlat +R={radius_m} +no_defs"


def local_orthographic_crs(center_lon_deg: float, center_lat_deg: float, radius_m: float = MOON_RADIUS_M) -> str:
    """Local Orthographic PROJ4 CRS string centered on `(center_lon_deg, center_lat_deg)`, on a
    sphere of `radius_m` -- the shared source of truth for every per-AOI local isotropic-meters
    working frame this project builds (see `geographic_crs`'s docstring for why this is factored out
    rather than duplicated)."""
    return f"+proj=ortho +lon_0={center_lon_deg} +lat_0={center_lat_deg} +R={radius_m} +units=m +no_defs"


def moon_geocentric_crs(radius_m: float = MOON_RADIUS_M) -> str:
    """Geocentric (ECEF-style X/Y/Z Cartesian) PROJ4 CRS string for the Moon on a sphere of
    `radius_m` -- MOON_ME itself, expressed as a real CRS. `rasterio.warp.transform` converts
    directly from `local_orthographic_crs`'s own projected (x, y) *plus* a real elevation `z` into
    this in one vectorized call, giving each DEM pixel its true 3D MOON_ME position via a real,
    already-imported library tool instead of a hand-derived closed-form correction --
    `_terrain_photometric_angles` is the one caller (2026-08-23, docs/history.md's dated entry: this
    replaced three successive hand-derived terrain-embedding corrections, verified live to reproduce
    the same MOON_ME X/Y/Z as both the two-step ortho->geographic->geocentric path and the original
    closed form, to full float64 precision)."""
    return f"+proj=geocent +R={radius_m} +units=m +no_defs"


def ortho_shaded_filename(
    hapke: bool,
    along_track_correction: bool = DEFAULT_ALONG_TRACK_CORRECTION,
    real_hapke_params: bool = DEFAULT_REAL_HAPKE_PARAMS,
    ortho_source: str = DEFAULT_ORTHO_SOURCE,
) -> str:
    """The `output_dir`-relative filename `despeckle_and_shade_ortho` writes its shaded ortho to for
    a given `hapke`/`along_track_correction`/`real_hapke_params`/`ortho_source` combination -- factored
    out so `trn_dataset.TrnTestEntry.dem_ortho_result`'s own resumption check can ask for exactly the
    file `fetch_dem_and_ortho(..., hapke=DEFAULT_HAPKE_SHADING,
    along_track_correction=DEFAULT_ALONG_TRACK_CORRECTION,
    real_hapke_params=DEFAULT_REAL_HAPKE_PARAMS, ortho_source=DEFAULT_ORTHO_SOURCE)` would produce,
    without duplicating this naming logic (and risking it drifting out of sync). `along_track_correction`/
    `real_hapke_params` only change the filename when `hapke=True` (both are a no-op on the Lambertian
    fallback, see `hapke_shade_ortho`'s docstring) -- giving each a real, distinct suffix rather than
    reusing `hapke=True`'s own plain filename is deliberate: `DEFAULT_ALONG_TRACK_CORRECTION` flipping to
    `True` after `DEFAULT_HAPKE_SHADING` already had real cached `ortho_shaded_hapke.tif` files on
    disk (from before this default existed) is exactly the kind of stale-cache-under-a-new-default's-
    name risk that bit `DEFAULT_HAPKE_SHADING` itself once already -- see docs/history.md's dated
    entries. `real_hapke_params` follows the identical pattern for the same reason.

    The `_normaltilt` suffix is **permanent, not conditional on a parameter** (Phase 72,
    docs/history.md): the normal-tilt correction it names became unconditional, but the suffix itself
    stays -- real `ortho_shaded_hapke_atc_realparams.tif` files (no `_normaltilt`) already exist on
    disk from before that correction existed at all, so dropping the suffix now would resume that
    stale, pre-correction content under today's default's own name, exactly the risk this function's
    whole naming scheme exists to prevent. `_normaltilt` is simply always appended when `hapke=True`
    now, the same way `hapke=True` itself always gets the `_hapke` base name.

    `_wacemp` is appended whenever `ortho_source="wac_emp_pds"` (the live default, 2026-08-23,
    docs/history.md's dated entry) -- unconditionally on `hapke`, since the input texture's own real
    numeric convention changed (real reflectance, no embedded display stretch -- see
    `hapke_shade_ortho`'s docstring) regardless of which shading mode blends it. `ortho_source=
    "lunaserv_wms"` (the deprecated fallback) keeps the original, suffix-less filenames exactly as
    before this migration -- real pre-migration cached files on disk stay valid, resumable content
    under their own existing names, not silently reinterpreted under a new source's semantics."""
    wacemp_suffix = "_wacemp" if ortho_source == "wac_emp_pds" else ""
    if not hapke:
        return f"ortho_shaded{wacemp_suffix}.tif"
    suffix = ("_atc" if along_track_correction else "") + ("_realparams" if real_hapke_params else "") + "_normaltilt"
    return f"ortho_shaded_hapke{suffix}{wacemp_suffix}.tif"


@dataclasses.dataclass(frozen=True)
class DemOrthoResult:
    """DEM/ortho tiles fetched for a `Camera`'s footprint, as returned by `fetch_dem_and_ortho`.
    `bbox` is in meters, in the per-camera local Orthographic CRS (`config.lunaserv_srs_template`)
    both tiles were fetched in -- not lon/lat degrees (each `DemOrthoResult`'s tiles have their own
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


def orthographic_xy_m(lon_deg, lat_deg, center_lon_deg, center_lat_deg, radius_m: float = MOON_RADIUS_M):
    """Forward spherical Orthographic projection (meters) of `(lon_deg, lat_deg)` relative to a
    local tangent point `(center_lon_deg, center_lat_deg)` -- matches Lunaserv's `IAU2000:30166`
    layer projection exactly (same formula, same Moon radius), so a bbox computed here lines up
    with what the WMS server actually renders. Standard formula (e.g. Snyder 1987 eq. 20-3/20-4)."""
    lon, lat = math.radians(lon_deg), math.radians(lat_deg)
    lon0, lat0 = math.radians(center_lon_deg), math.radians(center_lat_deg)
    x = radius_m * math.cos(lat) * math.sin(lon - lon0)
    y = radius_m * (math.cos(lat0) * math.sin(lat) - math.sin(lat0) * math.cos(lat) * math.cos(lon - lon0))
    return x, y


def footprint_bbox_local_m(footprint_lonlat, center_lon_deg, center_lat_deg, radius_m: float = MOON_RADIUS_M):
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


def radius_to_elevation(radius_tif_path, elevation_tif_path, moon_radius_m: float = MOON_RADIUS_M):
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
    geo_crs = geographic_crs(moon_radius_m)
    ortho_crs = local_orthographic_crs(center_lon_deg, center_lat_deg, moon_radius_m)
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


# Confirmed live via the WAC_EMP PDS4 archive's own S3 bucket listing (2026-08-23, docs/data-sources.md):
# the equirect (non-polar) tile grid covers only 0-60 deg in each hemisphere -- a genuinely separate
# polar-stereographic tile pair (`P900N`/`P900S` in the real product-ID scheme) covers 60-90 deg, in
# an unverified format this project doesn't fetch (see `wac_emp_tile_id_for_bbox`'s docstring).
WAC_EMP_MAX_ABS_LATITUDE_DEG = 60.0
# The equirect grid's own tiling scheme, confirmed live (not guessed from one example filename) via
# the archive's real directory listing: exactly one 60-deg-tall latitude band per hemisphere (0-60,
# center magnitude 30.0 -- hence the tile ID's fixed "E300" segment below), and 4 lon zones 90 deg
# wide each, centered at 45/135/225/315 (0-90, 90-180, 180-270, 270-360, Positive-East).
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
    """Resolve the single WAC_EMP PDS4 tile (product ID, no extension) that fully covers `dst_bbox_m`
    (the local-Orthographic working grid's own already-padded bbox, meters -- see
    `fetch_dem_and_ortho`), for one real `wavelength_nm`/`ppd` combination.

    Real product ID format, confirmed live via the archive's own S3 bucket listing (not guessed from
    one example filename -- see docs/data-sources.md's "WAC_EMP PDS4 archive" section for the full
    listing/derivation): `WAC_EMP_<wavelength_nm>NM_E300<N|S><lon_center_deg*10:04d>_<ppd:03d>P`.
    `wavelength_nm` must be one of the real archive's 7 bands (matches
    `_HAPKE_CALIBRATION_WAVELENGTHS_NM`, the same real bands ISIS's own calibration cube offers,
    confirmed to be the identical wavelength set) -- the default (643) matches
    `DEFAULT_HAPKE_CALIBRATION_WAVELENGTH_NM`, the same real imagery wavelength already used elsewhere
    in this file. `ppd` must be a real resolution the archive actually offers for that wavelength
    (every band has 64 ppd; 643nm additionally has a real 304 ppd product, this project's own default).

    Uses the same `transform_bounds`-on-the-destination-grid technique `astropedia_coverage_bbox_deg`
    already established (not an independently-padded degree-space bbox -- see that function's own
    docstring for why the latter caused real corner nodata gaps before this project's DEM fetch).

    Raises `ValueError` (mirroring `astropedia_coverage_bbox_deg`'s own message style) if the padded
    AOI extends beyond `WAC_EMP_MAX_ABS_LATITUDE_DEG`, straddles the equator (the tile grid's own
    hemisphere boundary), or straddles a 90-deg longitude zone boundary -- no silent multi-tile mosaic
    in this pass, matching `astropedia_coverage_bbox_deg`'s own "no automatic fallback/mosaic" stance."""
    if wavelength_nm not in _HAPKE_CALIBRATION_WAVELENGTHS_NM:
        raise ValueError(
            f"wavelength_nm={wavelength_nm} is not one of the real archive's own bands "
            f"{_HAPKE_CALIBRATION_WAVELENGTHS_NM}"
        )
    padded_bbox_m = pad_bbox(dst_bbox_m, DEM_FETCH_SAFETY_MARGIN_FRACTION)
    geo_crs = geographic_crs(moon_radius_m)
    ortho_crs = local_orthographic_crs(center_lon_deg, center_lat_deg, moon_radius_m)
    minlon, minlat, maxlon, maxlat = transform_bounds(ortho_crs, geo_crs, *padded_bbox_m)

    if minlat < -WAC_EMP_MAX_ABS_LATITUDE_DEG or maxlat > WAC_EMP_MAX_ABS_LATITUDE_DEG:
        raise ValueError(
            f"Camera footprint's padded AOI (latitude range {minlat:.2f}..{maxlat:.2f} deg) extends "
            f"beyond WAC_EMP's real equirect tile grid's +-{WAC_EMP_MAX_ABS_LATITUDE_DEG} deg coverage "
            "-- the polar-stereographic tile set beyond this isn't fetched by this project (unverified "
            "format, see wac_emp_tile_id_for_bbox's own docstring); the deprecated Lunaserv-WMS ortho "
            "path (fetch_dem_and_ortho(..., ortho_source='lunaserv_wms')) has no such limit but carries "
            "a confirmed, uncorrected affine display stretch -- see docs/data-sources.md."
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
    """Live default ortho/texture source: resolve and fetch/cache the single real WAC_EMP PDS4 tile
    covering `dst_bbox_m` (`wac_emp_tile_id_for_bbox`, which also raises if the footprint needs a tile
    this project doesn't fetch), mirroring `fetch_dem_astropedia`'s own shape. Returns the local cached
    file path plus the resolved tile's product ID -- `reproject_wac_emp_reflectance_to_local_grid`
    needs only the path (it reads the AOI window directly from the file's own embedded
    georeferencing); the product ID is returned for logging/cache-busting/debugging."""
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
    """Read just the AOI from the local cached WAC_EMP tile and reproject it onto the same per-camera
    local Orthographic working grid the DEM fetch uses -- mirrors
    `reproject_astropedia_elevation_to_local_grid`'s exact window-read-then-warp shape, except the AOI
    window comes directly from `dst_bbox_m` transformed into the file's own embedded CRS (no separate
    degree-space bbox intermediate needed here, unlike Astropedia's path -- this file's own PDS3 label
    already carries a real, trustworthy projected CRS/transform GDAL's PDS3 driver reads natively, not
    a hand-rolled equirect PROJ4 string or manual byte offsets, confirmed live against the real label).

    This data is real physical reflectance (IEEE754 float32, no embedded display stretch, confirmed
    live -- see docs/data-sources.md) -- unlike Lunaserv's WMS-served DN, no `/255.0` un-scaling
    assumption applies to this output at all; `hapke_shade_ortho`/`shade_ortho` treat it as reflectance
    directly (see their own docstrings for the resulting numeric-pipeline change)."""
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
    """`dem_mosaic`'s own `-o <prefix>` convention appends a fixed `-tile-0.tif` to whatever prefix
    it's given -- `filled_path` is always named to match (ending in exactly `-tile-0.tif`, the
    caller's own convention). `atomic_publish_prefix` builds a temp prefix the same way, so this is
    atomic despite the prefix-based (not exact-path) tool convention that `atomic_publish_path`'s own
    contract doesn't directly fit -- see that helper's own docstring."""
    filled_path = Path(filled_path)
    with atomic_publish_prefix(filled_path, "-tile-0.tif") as tmp_prefix:
        run_quiet(["dem_mosaic", str(dem_path), "--hole-fill-length", "50", "-o", str(tmp_prefix)])


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


def _local_enu_basis(center_lon_deg: float, center_lat_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The (East, North, Up) unit vectors, in MOON_ME, of the local tangent plane at
    `(center_lon_deg, center_lat_deg)` -- used by `_moon_me_direction_from_local_enu` to rotate a
    single local-frame *direction* (the sun's azimuth/elevation) into MOON_ME. `_terrain_photometric_
    angles` itself no longer works in this local frame at all (2026-08-23, docs/history.md's dated
    entry) -- real positions (DEM points, camera) are genuine MOON_ME throughout now, since a
    tangent-plane position embedding is exactly the hand-derived-approximation pattern that kept
    needing new correction terms (Phases 70/72/76); a free *direction* has no such embedding step and
    was never part of that bug class, so rotating one between orthonormal frames stays exact and
    is kept here, at the one boundary that still needs it."""
    lon0, lat0 = math.radians(center_lon_deg), math.radians(center_lat_deg)
    east = np.array([-math.sin(lon0), math.cos(lon0), 0.0])
    north = np.array([-math.sin(lat0) * math.cos(lon0), -math.sin(lat0) * math.sin(lon0), math.cos(lat0)])
    up = np.array([math.cos(lat0) * math.cos(lon0), math.cos(lat0) * math.sin(lon0), math.sin(lat0)])
    return east, north, up


def _moon_me_direction_from_local_enu(local_enu_vector, center_lon_deg: float, center_lat_deg: float) -> np.ndarray:
    """Rotate a local (East, North, Up) *direction* (e.g. the sun's azimuth/elevation, converted to a
    local ENU unit vector by `real_geometry_photometric_angles`) into MOON_ME, via `_local_enu_basis`'s
    same orthonormal (East, North, Up) triad -- the linear combination `east*e + north*n + up*u`. The
    exact inverse rotation of the old (now-deleted) `_local_enu_direction`; lossless either direction,
    since (east, north, up) are orthonormal."""
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
    """Per-pixel incidence/emission/phase angles (degrees, as raw geometry -- not
    `LightSource.hillshade`'s own scene-relative contrast-stretched intensity, see its
    `shade_normals` -- which ISIS `photomet` needs real angles for) for `dem`'s own orthographic
    ortho, using the **real, finite camera position** (`camera_center_moon_me_m`) rather than an
    idealized infinitely-distant nadir viewer -- so emission and phase genuinely vary per pixel from
    actual parallax (each pixel's own real vector to the spacecraft), not just from local terrain
    slope. Earlier version of this function assumed a fixed straight-down view direction (fine for
    describing an already-existing flat WMS mosaic, but not a real camera's own perspective geometry)
    -- see docs/history.md's dated entry.

    **Fully MOON_ME-native (2026-08-23, docs/history.md's dated entry) -- no local-ENU frame or
    hand-derived closed-form terrain correction anywhere in this function any more.** Three
    successive such corrections were found and fixed here across Phases 70/72/76 (a sagitta term, a
    normal-tilt correction, then a relief-displacement fix) -- each discovered by noticing the
    previous one was still only an approximation, which is exactly the failure mode of re-deriving
    tangent-plane trig by hand instead of using real 3D geometry. All three are now subsumed by one
    call: `ground`, each DEM pixel's true 3D position, comes directly from `rasterio.warp.transform`
    (already imported in this file) converting `dem`'s local orthographic `(x, y)` *plus* its own
    elevation into `moon_geocentric_crs`'s real MOON_ME X/Y/Z -- confirmed live to reproduce the same
    numbers as both the old two-step (ortho -> geographic -> geocentric) path and the original closed
    form, to full float64 precision (this is an equivalence, not a further correctness fix -- the old
    formula was already algebraically exact, just harder to trust by inspection). `camera_center_
    moon_me_m`/`along_track_direction_moon_me` (`Camera.camera_center_moon_me_m`/`camera_along_track_
    direction_moon_me`, both already real MOON_ME) are used directly, with no rotation into any local
    frame at all -- `_camera_local_enu_m`/`_local_enu_direction`, which used to do that rotation, are
    deleted. `sun_direction_moon_me` is the one remaining input converted from a local frame (the
    sun's azimuth/elevation, by `real_geometry_photometric_angles`'s caller, via
    `_moon_me_direction_from_local_enu`) -- deliberately kept, since rotating a free *direction*
    between orthonormal frames is exact and lossless (no elevation-embedding step to get subtly
    wrong, unlike a *position*), so it was never part of the bug class the rest of this refactor
    fixes; see that function's own docstring for the full reasoning.

    The surface normal at each pixel comes from the identical `np.gradient`-based convention
    `LightSource.hillshade` itself uses internally (now over real MOON_ME coordinates instead of a
    locally-embedded approximation), so this stays geometrically consistent with `shade_ortho`'s
    existing Lambertian shading (which only ever needed incidence, unaffected by any of this). The
    Sun, unlike the camera, stays treated as an effectively parallel-ray, scene-wide direction (real
    lunar distance makes this negligible -- see `illumination.sun_azimuth_elevation_deg`'s
    docstring), so `sun_direction_moon_me` is a single vector rather than a per-pixel one.

    `along_track_direction_moon_me`, if given, projects the raw per-pixel view direction onto the
    plane perpendicular to it before computing emission/phase -- a "poor man's" correction for this
    project's single-frozen-camera-pose approximation of a real, multi-second pushframe scan:
    `camera_center_moon_me_m` is one fixed position (matched to the crop's own center-frame time), but
    the real spacecraft was measurably elsewhere (confirmed via real ISIS `campt` SpacecraftPosition
    at the crop's edges: ~150km of real motion across a real ~97s scan for one such crop) at the time
    any other line was actually captured, so the raw along-track component of `view_dir` reflects the
    *wrong* real position away from center. A real scanning pushframe/pushbroom sensor observes each
    line close to nadir *in its own along-track direction* at the instant it's captured (that's the
    basic operating assumption of "scan by flying forward"), so discarding the along-track component
    of the raw (wrong-position) view direction and keeping only the cross-track component approximates
    that real per-line near-nadir-along-track geometry without needing this project's per-line real
    timing machinery that already exists elsewhere (`isis_wac`'s per-line time reconstruction).
    `camera_along_track_direction_moon_me` -- the sensor's own real along-track axis (derived from the
    camera's own re-aimed attitude, not the spacecraft's raw orbital velocity direction) -- was
    validated directly against real `campt` phase at 5 sample points and found substantially more
    accurate than the spacecraft's raw orbital velocity direction (mean absolute phase error 1.3 deg
    vs. 4.8 deg) or the camera's cross-track axis (16.9 deg) -- see docs/history.md's dated entries
    for the full numbers.

    **Independently validated against real ISIS `campt` ground truth** (`tests/test_lunaserv_campt_
    validation.py`, `heavy`-marked): for the ellipsoid limit (`dem` all zero), this function's
    incidence/emission/phase agree with real `campt` output (queried against a real candidate's own
    real CSM camera + real sun position, `csminit`-attached) to within the residual expected from
    known, already-understood approximations -- observed live: max |diff| ~0.018 deg across 15 angle
    comparisons (5 sample points x phase/incidence/emission). **Independently validated against ASP
    `sfs`'s own ray-DEM intersection** (`tests/test_sfs_validation_lambertian_incidence.py`,
    `heavy`-marked), for the full DEM-aware case, across a real candidate's entire real-coverage
    region (not a sparse sample): mean|diff| 0.0005 deg / max|diff| 0.0005 deg -- essentially
    floating-point/interpolation noise, not real geometric disagreement (this was 0.0237/0.5138 deg
    before the Phase 76 relief-displacement fix this refactor now subsumes). Both residual budgets are
    expected to carry over unchanged by this refactor (proven algebraically equivalent to what was
    measured), not improve -- a real change in either would indicate a bug in this refactor, not
    progress. ISIS's own `phocube` was also investigated as a DEM-aware validation tool and shelved
    (its `localincidence=true` mode returned degenerate, implausible values on a real scene -- real
    ISIS issue DOI-USGS/ISIS3#3645 suggests its DEM-aware path is generally less mature than its
    ellipsoid path); `campt`'s own plain angle output was separately confirmed to stay
    ellipsoid-normal-based even with a real DEM shape model attached, so it has no DEM-aware mode of
    its own to fall back on either. See docs/history.md's dated entries for the full investigation.

    A real, still-unexplained regression remains open and is *not* addressed by this refactor: wiring
    in the normal-tilt correction (Phase 71) made the brightness-matched diff against the real WAC
    crop measurably *worse* (6.89 -> 7.58 mean|diff|), despite the geometry itself being independently
    confirmed correct above -- leading hypothesis, not verified: the base ortho texture is itself
    already photometrically normalized by someone else's own processing that this project's own
    from-scratch re-shading was never validated against. Flagged for a future pass."""
    height, width = dem.shape
    minx, miny, maxx, maxy = bbox
    x_centers = minx + (np.arange(width) + 0.5) * (maxx - minx) / width
    y_centers = maxy - (np.arange(height) + 0.5) * (maxy - miny) / height  # row 0 = north/top, matches `dy` below
    x_grid, y_grid = np.meshgrid(x_centers, y_centers)

    dy = -cellsize_m
    dem64 = dem.astype(np.float64)
    # Each DEM pixel's own true 3D MOON_ME position, via one vectorized `rasterio.warp.transform`
    # call from `dem`'s own local orthographic (x, y) plus its real elevation into `moon_geocentric_
    # crs`'s real MOON_ME X/Y/Z -- see this function's own docstring for why this replaced three
    # successive hand-derived closed-form corrections.
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

    # `normal`: the true surface normal, via each of `ground`'s own 3 MOON_ME coordinate channels'
    # partial derivatives (row/col index space -> physical space, `dy`/`cellsize_m`) and their cross
    # product -- the general "parametric surface normal" construction, unchanged from before this
    # refactor except that `ground` now comes directly from a real geocentric transform rather than a
    # locally-embedded approximation.
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
    """Write `values` as a plain (non-georeferenced) single-band ISIS3 cube -- `photomet`'s
    `ANGLESOURCE=BACKPLANE` mode only matches backplane files to the input cube by sample/line
    dimensions, so no real CRS/transform is needed here (and translating this project's local
    Orthographic CRS into an ISIS `Mapping` group is real, unnecessary risk -- ISIS only recognizes a
    specific set of projections). GDAL's own `ISIS3` driver (`rw+v`, confirmed via `gdalinfo
    --formats`) writes/reads real `.cub` files directly, so no `gdal2isis`/`isis2std` round-trip
    through a separate conversion tool is needed either. Deliberately non-georeferenced (see above),
    so `rasterio`'s own `NotGeoreferencedWarning` is expected and suppressed here rather than left to
    print into a notebook cell's real output."""
    path.unlink(missing_ok=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with rasterio.open(
            path, "w", driver="ISIS3", height=values.shape[0], width=values.shape[1], count=1, dtype="float32"
        ) as dst:
            dst.write(values.astype("float32"), 1)


def _hapke_calibration_cube_path(config: TrntestConfig) -> Path:
    """Resolve the real ISIS lunar Hapke calibration cube's path, fetching the `lro` ISIS data
    package (via a local import of `isis_wac.ensure_isisdata` -- `isis_wac.py` itself imports
    `DemOrthoResult` from this module, so a module-level import here would be circular) if not
    already present. Globs rather than hardcoding a specific version number (`.0001.cub` today) and
    picks the highest-numbered match, the same "don't assume a specific version" discipline this
    project already applies to other ISIS data area files."""
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
    """Pure sampling logic behind `fetch_real_hapke_params` -- reads all 9 parameters for one wavelength
    band at one real ground point from `cube_path` (any raster GDAL can open in the calibration
    cube's own Equirectangular CRS/band layout, real or a test fixture). Split out from
    `fetch_real_hapke_params` so it's unit-testable against a small synthetic fixture, without needing a
    real `$ISISDATA` or network access -- the same reasoning `_terrain_photometric_angles` being
    plain-Python (no ISIS subprocess) already followed."""
    if wavelength_nm not in _HAPKE_CALIBRATION_WAVELENGTHS_NM:
        raise ValueError(
            f"wavelength_nm={wavelength_nm} is not one of the cube's own bands {_HAPKE_CALIBRATION_WAVELENGTHS_NM}"
        )
    band_offset = _HAPKE_CALIBRATION_WAVELENGTHS_NM.index(wavelength_nm) * len(_HAPKE_CALIBRATION_PARAM_ORDER)
    lon_0_360 = center_lon_deg % 360.0  # cube's own convention, confirmed via its embedded georeferencing

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
    """Real, spatially-resolved Hapke parameters (Sato et al. 2014's own fit, converted to ISIS's
    native `Wh`/`Hg1`/`Hg2`/`Bc0`/`hc`/`B0`/`Hh`/`Theta`/`phi` parameterization) sampled at one real
    ground point from ISIS's own calibration cube (`_hapke_calibration_cube_path`) -- the
    `real_hapke_params=True` alternative to `_HAPKE_PLACEHOLDER_PARAMS`'s illustrative constants (see
    `hapke_shade_ortho`). Returns all 9 real parameters, keyed lowercase to match
    `_HAPKE_PLACEHOLDER_PARAMS`'s own keys (`wh`, `hg1`, ...) -- `hapke_shade_ortho` uses only the 6
    the simpler shadow-hiding-only `HAPKEHEN` model this project already calls accepts (`wh`, `hg1`,
    `hg2`, `hh`, `b0`, `theta`); `bc0`/`hc`/`phi` describe the fuller Hapke model's separate
    coherent-backscatter term, confirmed (via this same cube, globally) always `0`/`1`/`0` for this
    real WAC-derived product -- i.e. genuinely unused by it, not a modeling choice made here.

    A single value per image (this function's own real footprint center), not per-pixel: within one
    real ~143km candidate footprint, `wh`/`b0`/`hg1` vary only modestly (checked directly against
    this cube -- a few percent of each parameter's own full-Moon range); real, but secondary next to
    the placeholder-vs-real gap this exists to fix (e.g. `b0`: 0.025 placeholder vs. ~1.5-2.2 real, a
    ~60x difference). `hg2`/`hh` vary somewhat more within one footprint, but still a smaller effect
    than the placeholder gap. Per-pixel sampling (reprojecting the calibration cube onto the same
    working grid `reproject_astropedia_elevation_to_local_grid` builds the DEM/ortho on) would be a
    real further refinement, not implemented here -- see docs/plan.md's open items.

    `wavelength_nm` must be one of the cube's own 7 bands (321/360/415/566/604/643/689) -- the
    default matches `config.lunaserv_ortho_layer`'s own real wavelength (643nm, see
    docs/data-sources.md), since the calibration should describe the same real imagery being shaded."""
    path = _hapke_calibration_cube_path(config)
    return _sample_hapke_calibration(path, center_lon_deg, center_lat_deg, wavelength_nm)


def _hapke_reflectance(
    phase_deg: np.ndarray,
    incidence_deg: np.ndarray,
    emission_deg: np.ndarray,
    hapkehen_params: dict,
) -> np.ndarray:
    """Runs ISIS `photomet` (`PHTNAME=HAPKEHEN`, `ANGLESOURCE=BACKPLANE`, `NORMNAME=SHADE`) as a pure
    Hapke-model evaluator: given phase/incidence/emission angle rasters (any shape -- the real
    per-pixel candidate geometry, or a small constant-valued array for a single reference geometry),
    returns H(angles), the raw Hapke reflectance factor, with no dependence on any actual image
    content -- the `from` cube fed to `photomet` is always zeroed, and `NORMNAME=SHADE` overwrites it
    with the model's own output regardless. Factored out of `hapke_shade_ortho` so it can call this
    twice (real geometry, and the fixed reference geometry `luna_wac_normalized_reflectance` is
    itself normalized to -- see `REFERENCE_INCIDENCE_DEG`'s own module-level comment) without
    duplicating the `photomet` subprocess-orchestration logic. `photomet` also requires a `FROM` cube
    purely as a size/dtype template (a `BandBin` label group is enough to open the file at all --
    confirmed empirically, not documented in `photomet.xml` -- added via `editlab` since GDAL's
    `ISIS3` writer doesn't create one from scratch).

    **Uses a call-scoped `tempfile.TemporaryDirectory()` for its own scratch cubes**, not
    `config.output_dir` -- confirmed live (`docs/intermediate-product-plan.md`'s Phase 2 always
    intended this, but it wasn't actually implemented until a real race surfaced it): two workers
    computing the same entry's `hillshade` and `reproject` concurrently both reach this function
    (via `despeckle_and_shade_ortho`/`hapke_shade_ortho`) and raced on these fixed-name cubes,
    corrupting each other's writes (`**I/O ERROR** Failed to write blob`). Nothing here persists or
    resumes across calls -- pure single-call scratch, so a call-scoped temp dir (auto-deleted on
    return, success or exception) is the right fix, not a unique-but-persistent path, which would
    just trade a collision bug for an accumulation one."""
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
    """The default ortho-shading mode (`DEFAULT_HAPKE_SHADING`) -- a real Hapke bidirectional
    reflectance function (ISIS `photomet`, `PHTNAME=HAPKEHEN`) instead of `shade_ortho`'s fallback
    plain Lambertian `LightSource.hillshade`, via
    `ANGLESOURCE=BACKPLANE` fed the angle rasters `_terrain_photometric_angles` computes ourselves
    from `camera`'s own real position (sidesteps the fact that this ortho has no ISIS camera model
    for `photomet` to derive angles from automatically -- see docs/history.md's dated entry for the
    full evaluation). `_HAPKE_PLACEHOLDER_PARAMS` is illustrative, not lunar-calibrated -- a
    feasibility prototype, not a validated photometric model; `real_hapke_params=True` (the default,
    `DEFAULT_REAL_HAPKE_PARAMS`) uses `fetch_real_hapke_params()`'s real, ISIS-calibration-cube-sourced
    values for `camera`'s own footprint center instead (see that function's docstring for what it does
    and doesn't capture) -- `real_hapke_params=False` falls back to the placeholder, kept for
    comparison in `notebooks/real_hapke_params.ipynb`. See docs/history.md's dated entry for the
    placeholder-vs-real comparison that motivated making this the default.

    `photomet`'s own subprocess-orchestration mechanics (a zeroed `FROM` cube as a pure size/dtype
    template, `NORMNAME=SHADE` overwriting it with the model's own output, the `editlab`-added
    `BandBin` group needed just to open the file at all) live in the shared `_hapke_reflectance`
    helper -- see its own docstring.

    **Relights `ortho` by the ratio H(i,e,g)/H(reference), not a bare rescaled H(i,e,g)** (fixed
    2026-08-22, see docs/history.md's dated entry): `ortho` isn't raw albedo -- it's itself already a
    photometric composite, normalized to a fixed reference geometry (`REFERENCE_INCIDENCE_DEG`'s own
    module-level comment explains the reference geometry and why it's only an approximate, not exact,
    correction). Multiplying it by a *bare*, arbitrarily-rescaled H(i,e,g) (this function's own
    earlier approach: `reflectance / percentile99(reflectance)`) double-counts the photometric
    function with no relationship to the geometry the texture was actually produced at -- it isn't a
    relighting operation at all, just an unprincipled per-pixel contrast rescale. Dividing by
    H(reference) instead makes the multiplier a true, physically meaningful relighting ratio (~1 for
    geometries close to the reference, deviating for real geometries far from it), so no separate
    display-range rescale is needed beyond simple `[0, 255]` clipping -- an unusually bright
    opposition-surge patch legitimately saturating toward white, rather than the whole frame being
    dimmed to keep it in range, was reasoned (Phase 72) to be the physically correct behavior here,
    not an artifact to avoid. **On review (2026-08-23, Phase 78): that reasoning was never actually
    validated or deliberately signed off on as a real decision -- it's improvised and was allowed to
    stand unchallenged, not a settled design stance. Whether/how often this saturation actually
    happens on real candidates, and whether it's actually acceptable, is an open question -- see
    `docs/plan.md`'s open items.** Like `_terrain_photometric_angles`'s own normal-tilt correction,
    this has no opt-out parameter -- a deliberate user call (2026-08-22, Phase 72, docs/history.md),
    made despite this specific
    correction being confirmed to *worsen* the brightness-matched diff against the real WAC crop for
    the one candidate tested so far. See `REFERENCE_INCIDENCE_DEG`'s own module-level comment and
    docs/history.md's Phase 72 entry for the full rationale and that empirical result.

    `along_track_correction` (`DEFAULT_ALONG_TRACK_CORRECTION`) applies `_terrain_photometric_angles`'s
    along-track correction (see its own docstring) using `camera`'s own real along-track attitude
    axis (`Camera.camera_along_track_direction_moon_me`) -- on by default, validated against real
    `campt` output to substantially improve on the base per-pixel-camera-position approach alone
    (this function's own earlier default); `along_track_correction=False` falls back to that base
    approach. See docs/history.md's dated entries for the full evaluation, including an earlier,
    less-accurate version of this correction that used the spacecraft's raw orbital velocity instead.

    `_terrain_photometric_angles`'s own real-3D-geometry terrain embedding (see its docstring) is
    unconditional, with no parameter here to control it any more.

    **Returns real relit reflectance directly (float64, physical units), not a display-ready `uint8`
    image** (2026-08-23, docs/history.md's dated entry -- the WAC_EMP-PDS migration). `ortho` is now
    assumed to already be real physical reflectance (WAC_EMP's own PDS4 archive tile, no embedded
    display stretch -- see `reproject_wac_emp_reflectance_to_local_grid`'s docstring), not Lunaserv's
    old WMS-served `uint8` DN this function used to assume implicitly via its own `/255.0`
    un-scale-then-`*255.0`-rescale round trip. `relit_reflectance = ortho * ratio` directly on that real
    basis -- no unknown display stretch riding along any more. `despeckle_and_shade_ortho` applies the
    separate, explicit `stretch_reflectance_to_uint8` cosmetic step afterward, not this function --
    keeping the physics (relighting, here) and the display cosmetics (the stretch) in that deliberate
    order, rather than fused into one `uint8`-in-`uint8`-out call the way this function used to be."""
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
    """The one, explicit place real relit reflectance (`hapke_shade_ortho`'s own output) becomes a
    display-ready `uint8` image -- a fixed linear stretch (`DISPLAY_STRETCH_REFLECTANCE_MIN`/`_MAX`,
    see their own module-level comment for why fixed, not adaptive), applied once at the very end of
    the pipeline rather than baked into the input texture the way Lunaserv's old, uncorrected WMS
    display stretch effectively was (see docs/data-sources.md). Purely cosmetic -- has no bearing on
    any of this module's actual photometric physics, all of which is already complete by the time this
    runs.

    `DISPLAY_STRETCH_REFLECTANCE_MAX = 0.30` was confirmed empirically non-saturating for exactly one
    real candidate (2026-08-23, Phase 78) -- not swept across others. Whether/how often real input
    actually exceeds it (clipping to 255, with the downstream effects that has -- e.g. biasing
    `sfs_validation.true_albedo_map`'s recovered albedo at those pixels) is an open question, not a
    validated non-issue -- see `docs/plan.md`'s open items."""
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
    """`(incidence_deg, emission_deg, phase_deg)` at `camera`'s own real per-pixel geometry --
    `_terrain_photometric_angles` given `camera`'s own real MOON_ME position
    (`camera.camera_center_moon_me_m`, used directly -- no local-frame conversion, see
    `_terrain_photometric_angles`'s own docstring) and, when `along_track_correction` is on (the
    default), its own real along-track attitude axis (`camera.camera_along_track_direction_moon_me`,
    also used directly). Factored out of `real_geometry_hapke_reflectance` (Phase 74 follow-up) for
    callers that only need the angles themselves, not a Hapke evaluation -- e.g. `sfs_validation.py`'s
    Lambertian-mode incidence cross-check, which compares this function's own `incidence_deg` directly
    against `sfs`'s independently ray-traced one (confirmed live to agree to ~0.0005 deg mean/max,
    after Phase 76's relief-displacement fix -- see docs/history.md's dated entries).

    `azimuth_deg`/`elevation_deg` describe the sun relative to `center`'s own local horizon -- the one
    place this function still does a local-frame-to-MOON_ME conversion
    (`_moon_me_direction_from_local_enu`), since it's the existing, human-readable convention every
    caller/test/notebook uses and `shade_ortho`'s Lambertian fallback genuinely needs az/el regardless
    for matplotlib's `LightSource` API. This is exact and lossless (a free direction rotated between
    orthonormal frames, not a position embedding), so it was never part of the hand-derived-
    approximation bug class Phases 70/72/76/this-refactor otherwise eliminated -- see
    `_terrain_photometric_angles`'s own docstring for the full reasoning."""
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
    """H(i,e,g) at `camera`'s own real per-pixel geometry (`real_geometry_photometric_angles`) -- the
    same computation `hapke_shade_ortho` itself makes to build its H(real)/H(reference) relighting
    ratio, factored out (Phase 74 follow-up) so `sfs_validation.true_albedo_map` can divide this
    exact factor back out of an already-shaded ortho to recover the geometry-independent albedo
    `hapke_shade_ortho` started from, without duplicating this setup or risking it drifting out of
    sync with `hapke_shade_ortho`'s own computation -- an earlier version of
    `sfs_validation.true_albedo_map` divided by `reference_hapke_reflectance` instead, a real
    double-counting bug (see docs/history.md's dated entry). Returns `(reflectance, hapkehen_params)`
    -- the params dict too, since a caller building an albedo map also needs it for
    `sfs_validation.hapke_params_to_asp_model_coeffs`."""
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
    """Slices a params dict (`_HAPKE_PLACEHOLDER_PARAMS`, or `fetch_real_hapke_params`'s wider 9-key
    return) down to exactly the 6 keys the simpler shadow-hiding-only `HAPKEHEN` model this project
    calls actually accepts (`wh`, `hg1`, `hg2`, `hh`, `b0`, `theta`) -- shared by `hapke_shade_ortho`
    and `sfs_validation.py`'s own use of the same real calibration source, so both stay in sync
    about which 6 of `fetch_real_hapke_params`'s 9 keys are the ones that matter here."""
    return {name: hapkehen_source[name] for name in _HAPKE_PLACEHOLDER_PARAMS}


def reference_hapke_reflectance(hapkehen_params: dict) -> float:
    """H(reference), the Hapke reflectance factor at the fixed reference geometry `ortho` is itself
    normalized to (`REFERENCE_INCIDENCE_DEG`'s own module-level comment) -- the shared denominator
    both `hapke_shade_ortho`'s relighting ratio and `sfs_validation.true_albedo_map`'s "true albedo"
    proxy (`ortho / H(reference)`, undoing the same normalization from the other direction) divide
    by. A small constant-valued array (no per-pixel variation possible, same params/angles
    everywhere), not a full-size raster pass -- `_hapke_reflectance`'s own `photomet` call still
    needs a real array shape to run against, hence the `(3, 3)` filler."""
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
    """Despeckle the raw fetched ortho and blend in a real-sun hillshade computed from the (already
    hole-filled) DEM, writing the result to `output_path` -- the single ortho used by both `sat_sim`
    and every display panel (see `fetch_dem_and_ortho`). Defaults to `hapke_shade_ortho`'s
    ISIS-`photomet`-backed Hapke shading (which needs `bbox`, unlike the fallback `shade_ortho`, to
    place the DEM's own pixels in the same local frame as `camera`'s real position -- see
    `_terrain_photometric_angles`); `hapke=False` falls back to the plain Lambertian
    `shade_ortho` blend -- see `hapke_shade_ortho`'s docstring. `along_track_correction`/
    `real_hapke_params` are passed straight through to `hapke_shade_ortho` (both a no-op when
    `hapke=False`, `shade_ortho` has no camera-position or Hapke-model dependence at all).

    `hapke=True`'s branch applies `stretch_reflectance_to_uint8` explicitly, right here, to
    `hapke_shade_ortho`'s real-relit-reflectance output -- the one place that cosmetic display step
    happens (2026-08-23, docs/history.md's dated entry).

    **`hapke=False`'s branch also needs `stretch_reflectance_to_uint8` first when `ortho_source=
    "wac_emp_pds"`** -- a real bug caught live (not just reasoned about) the first time this migration
    regenerated `notebooks/hapke_hillshade.ipynb`: `shade_ortho` is deliberately unchanged, still
    assuming its `ortho` input is already `[0, 255]` DN (see its own docstring, and the implementation
    plan this followed, for why it's kept that way rather than generalized), but `cleaned` is real
    reflectance (~0.05-0.3) when the source is WAC_EMP -- `shade_ortho`'s own internal `/255.0` then
    `*255.0` round-trip algebraically cancels for values this small, so the whole result truncated to
    an all-zero, fully black image under `.astype(np.uint8)` (confirmed live: 100% zero pixels).
    Applying the same display stretch *before* handing the array to `shade_ortho` -- turning it back
    into a DN-like `[0, 255]` array first -- fixes this without touching `shade_ortho` itself at all.
    `ortho_source="lunaserv_wms"` skips this (its `cleaned` is already real DN, `shade_ortho`'s own
    native convention)."""
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
    fetching -- so `trn_dataset.TrnTestEntry.dem_ortho_result` can resume from a prior `generate()`
    run's output instead of re-fetching from Lunaserv/Astropedia. `bbox`/`width`/`height` are read
    back from `ortho_path`'s own embedded georeferencing rather than recomputed or stored separately
    -- `_reproject_raster_to_local_grid` (via `despeckle_and_shade_ortho`, which carries the fetched
    ortho's own `profile` through unchanged) writes `dst_transform`/`dst_crs` from exactly this same
    `bbox`/`width`/`height` at fetch time, so reading them back from the file is an exact
    round-trip, not an approximation -- the same "raster's own georeferencing is authoritative"
    pattern `isis_wac._orthographic_map_pvl` already relies on elsewhere."""
    with rasterio.open(ortho_path) as src:
        width, height = src.width, src.height
        bbox = (src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top)
    return DemOrthoResult(ortho=Path(ortho_path), dem=Path(dem_path), bbox=bbox, width=width, height=height)


@dataclasses.dataclass(frozen=True)
class DemFetchResult:
    """The entry's one DEM, as returned by `fetch_dem` -- `bbox`/`width`/`height` describe the padded
    local-CRS working grid it was fetched onto, the same grid `fetch_and_shade_ortho` must reuse
    exactly (never re-derive) for its own ortho fetch, so the two can't disagree about the AOI."""

    dem: Path
    bbox: tuple
    width: int
    height: int


@writes_product("dem_filled")
def fetch_dem(
    camera: Camera, config: TrntestConfig | None = None, extra_footprint_lonlat_deg: dict | None = None
) -> DemFetchResult:
    """The entry's one DEM fetch -- split out of the old combined `fetch_dem_and_ortho`
    (`docs/intermediate-product-plan.md`'s Phase 4) so `product_registry` has exactly one legible,
    checkable writer for the `"dem_filled"` label (principle 2), decoupled from the ortho-shading
    concern (`fetch_and_shade_ortho`, a genuine intentional-variant family -- multiple valid shaded
    orthos by design, principle 1) that used to be fused into the same function.

    **Still takes `extra_footprint_lonlat_deg` as a caller-suppliable parameter -- principle 1's "no
    caller-supplied parameter should be able to change identity" isn't fully closed by this split.**
    `dem_filled_path`'s own filename still doesn't encode this parameter (unlike `ortho_shaded_filename`'s
    careful suffix discipline for its own parameters), so two calls against the same output directory
    with genuinely different footprints can still silently disagree about "the" DEM -- see
    `docs/plan.md`'s own open item for the real historical bug this class of gap already caused once
    (docs/history.md's Phase 78 entry) and what a full fix would need. Not solved here: this phase
    only makes the *current* single writer legible/auditable (`writes_product`) and its actual file
    write atomic (`atomic_publish`, in `reproject_astropedia_elevation_to_local_grid`), not the
    filename-collision gap itself -- flagged rather than silently assumed fixed."""
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
    # docs/data-sources.md's "Astropedia GLD100 flat file" section and docs/history.md's dated entry.
    # `fetch_dem_astropedia` ensures the whole ~10GB file is downloaded/cached locally once (raises if
    # this camera's footprint needs data outside the file's real +-79 deg latitude coverage -- no
    # silent fallback to the deprecated Lunaserv-native path), then
    # `reproject_astropedia_elevation_to_local_grid` reads just this AOI from the local file and
    # reprojects it onto this same local-CRS grid -- already real elevation (not planetocentric
    # radius), so `radius_to_elevation` is skipped.
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
    """The ortho-shading half of the old combined `fetch_dem_and_ortho` (Phase 4, split out alongside
    `fetch_dem` -- see that function's own docstring for why). **Takes `dem` (`fetch_dem`'s own
    output) as an input, and always reuses its `bbox`/`width`/`height` exactly -- never re-derives the
    footprint/AOI itself.** This is what actually closes the entanglement `fetch_dem`'s docstring
    describes for the DEM/ortho *pairing* specifically (the two can no longer fetch against two
    different bboxes, unlike the old single function where a caller could -- in principle, though
    never observed live -- have driven them apart via inconsistent internal state); the DEM's own
    filename-collision gap against a *different* `fetch_dem` call is still open, as noted there.

    `ortho_source` (`ORTHO_SOURCES`) picks where the raw ortho/texture comes from before shading:
    `"wac_emp_pds"` (live default) fetches WAC_EMP's own real reflectance directly from its PDS4
    archive (`fetch_wac_emp_reflectance`/`reproject_wac_emp_reflectance_to_local_grid`) -- real
    physical reflectance, no embedded display stretch, unlike `"lunaserv_wms"` (the deprecated
    fallback, the original Lunaserv WMS `luna_wac_normalized_reflectance` layer, confirmed to carry a
    real, uncorrected affine display stretch -- see docs/data-sources.md). Raises `ValueError` if
    `ortho_source` isn't one of `ORTHO_SOURCES`. `"wac_emp_pds"` additionally raises if the camera's
    footprint needs latitude beyond WAC_EMP's own real equirect-tile coverage or straddles a tile
    boundary (`wac_emp_tile_id_for_bbox`) -- no silent fallback to `"lunaserv_wms"` in that case; a
    caller that wants the fallback has to ask for it explicitly.

    **`ortho_source="lunaserv_wms"` is only numerically coherent with `hapke=False`** after this
    migration -- `hapke_shade_ortho` now assumes its `ortho` input is already real reflectance (see
    its own docstring), which `"lunaserv_wms"`'s raw WMS DN is not (it's DN under an unknown, confirmed
    non-trivial affine stretch). `shade_ortho`'s plain-Lambertian fallback is the one that still speaks
    `"lunaserv_wms"`'s own DN convention unchanged. No code-level guard against this combination --
    just don't request it.

    Shades the fetched ortho with ISIS `photomet`'s Hapke model by default
    (`despeckle_and_shade_ortho`'s `hapke` passthrough, see `hapke_shade_ortho`'s docstring), with
    `along_track_correction`'s own real-along-track-axis refinement on by default too, and
    `_terrain_photometric_angles`'s own curvature-aware surface normal unconditionally applied
    (no longer a parameter here at all, see that function's docstring) -- `hapke=False` falls back to
    the plain Lambertian `shade_ortho` blend instead (`along_track_correction`/`real_hapke_params` are
    then both a no-op). `real_hapke_params=True` (the default) uses `fetch_real_hapke_params()`'s
    real, ISIS-calibration-cube-sourced Hapke coefficients instead of `_HAPKE_PLACEHOLDER_PARAMS`'s
    illustrative ones, see `hapke_shade_ortho`'s docstring. Each `hapke`/`along_track_correction`/
    `real_hapke_params` combination writes to its own filename (`ortho_shaded_filename`) rather than a
    single shared one, so any combination can be fetched for the same camera and compared directly,
    e.g. `notebooks/hapke_hillshade.ipynb`/`notebooks/along_track_correction.ipynb`/
    `notebooks/real_hapke_params.ipynb`, and so `trn_dataset.TrnTestEntry.dem_ortho_result`'s
    resumption check can never mistake one mode's cached file for another's -- including,
    deliberately, never resuming a file cached under an older default before `along_track_correction`/
    `real_hapke_params` existed or before either became `True` by default; see docs/history.md's
    dated entries.

    `bbox`/`width`/`height` (the fetch AOI, already unioned with whatever footprint `fetch_dem` was
    given -- e.g. `tie_points.crop_footprint_corners_for_camera`'s real WAC crop footprint, which
    isn't always the same size/shape as the synthetic camera's own FOV) all come from `dem`, not
    recomputed here -- see `fetch_dem`'s own docstring for that computation and its own remaining
    caveats (the ray-traced-estimate-vs-real-crop margin, the still-open filename-collision gap)."""
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

    if ortho_source == "wac_emp_pds":
        # Live default: WAC_EMP's own real reflectance, fetched directly from its PDS4 archive rather
        # than through Lunaserv's WMS render -- confirmed (docs/history.md's dated entry) that the WMS
        # layer's DN carries a real, uncorrected affine display stretch, not raw reflectance.
        # `fetch_wac_emp_reflectance` raises if this footprint needs a tile beyond the real archive's
        # own equirect coverage (see its own docstring) -- no silent fallback to the deprecated
        # Lunaserv path below.
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
    """Composes `fetch_dem` + `fetch_and_shade_ortho` -- unchanged signature/behavior from before
    that Phase 4 split (`docs/intermediate-product-plan.md`); see those two functions' own
    docstrings for what's now individually `product_registry`-decorated, and for the DEM
    filename-collision gap that split doesn't itself close."""
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
