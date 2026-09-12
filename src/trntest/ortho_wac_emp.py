"""Live default ortho/texture source: WAC_EMP's own PDS4 archive, fetched directly rather than through
Lunaserv's WMS render. See docs/data-sources/wac-emp-pds4.md and `dem_ortho.fetch_and_shade_ortho`.
"""

import math
from pathlib import Path

import rasterio
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import Resampling, transform_bounds
from rasterio.warp import transform as warp_transform
from rasterio.windows import from_bounds as window_from_bounds
from rasterio.windows import transform as window_transform

from trntest import cache, wac_emp_edge_correction
from trntest.config import MOON_RADIUS_M, TrntestConfig
from trntest.geo_utils import (
    DEM_FETCH_SAFETY_MARGIN_FRACTION,
    geographic_crs,
    local_orthographic_crs,
    merge_local_grid_arrays,
    pad_bbox,
    reproject_raster_to_local_grid_array,
)
from trntest.hapke import DEFAULT_HAPKE_CALIBRATION_WAVELENGTH_NM, HAPKE_CALIBRATION_WAVELENGTHS_NM
from trntest.product_io import atomic_publish

# The WAC_EMP PDS4 archive's equirect (non-polar) tile grid covers only 0-60 deg in each hemisphere
# -- a separate polar-stereographic tile pair (`P900N`/`P900S`) covers the rest, 60-90 deg each
# hemisphere, and is also fetched by this project (see `wac_emp_tile_ids_for_bbox`'s docstring and
# docs/data-sources/wac-emp-pds4.md's polar-tile bullet for the confirmed format/coverage facts).
WAC_EMP_MAX_ABS_LATITUDE_DEG = 60.0
# The archive only offers the polar tile pair at one band/resolution -- this project's own defaults
# (`DEFAULT_HAPKE_CALIBRATION_WAVELENGTH_NM = 643`, `ppd=304`) already match it, so every real call
# site hits this for free; a caller that explicitly requests a different combination for a
# polar-only footprint gets a clear `ValueError` (`wac_emp_tile_ids_for_bbox`), not a wrong tile.
_WAC_EMP_POLAR_WAVELENGTH_NM = 643
_WAC_EMP_POLAR_PPD = 304
# The equirect grid's own tiling scheme, confirmed via the archive's directory listing: exactly one
# 60-deg-tall latitude band per hemisphere (0-60, center magnitude 30.0 -- hence the tile ID's fixed
# "E300" segment below), and 4 lon zones 90 deg wide each, centered at 45/135/225/315 (0-90, 90-180,
# 180-270, 270-360, Positive-East).
_WAC_EMP_LON_ZONE_WIDTH_DEG = 90.0
_WAC_EMP_N_LON_ZONES = 4
_WAC_EMP_LAT_BAND_CENTER_CODE = 300  # fixed: (0+60)/2 * 10 -- the tile ID's literal "E300" segment
# The 3 latitude breakpoints where one WAC_EMP tile's coverage ends and the next begins: the equator
# (equirect grid's own N/S tile split) and +-WAC_EMP_MAX_ABS_LATITUDE_DEG (equirect/polar split).
# `_lat_bands_touched` walks the 4 resulting bands (south-polar, south-equirect, north-equirect,
# north-polar) to find which ones a padded AOI actually overlaps.
_WAC_EMP_LAT_BREAKPOINTS_DEG = (-90.0, -WAC_EMP_MAX_ABS_LATITUDE_DEG, 0.0, WAC_EMP_MAX_ABS_LATITUDE_DEG, 90.0)


def _lat_bands_touched(minlat: float, maxlat: float) -> list[tuple[float, float]]:
    """Which of the 4 fixed WAC_EMP latitude bands (south-polar/south-equirect/north-equirect/
    north-polar, split at `_WAC_EMP_LAT_BREAKPOINTS_DEG`) a padded AOI's `[minlat, maxlat]` overlaps --
    2 bands for an AOI straddling one boundary (e.g. the equator, or the equirect/polar split), a real
    case for this project's own manifest (rows near +-60 deg latitude are common).

    :returns: `(lo, hi)` pairs, ordered south to north.
    """
    breakpoints = _WAC_EMP_LAT_BREAKPOINTS_DEG
    return [(lo, hi) for lo, hi in zip(breakpoints[:-1], breakpoints[1:], strict=True) if maxlat > lo and minlat < hi]


def _lon_zones_touched(minlon_norm: float, maxlon_norm: float) -> list[int]:
    """Which of the 4 fixed 90-deg WAC_EMP longitude zones (0: 0-90, 1: 90-180, 2: 180-270, 3: 270-360)
    a padded AOI's `[minlon_norm, maxlon_norm]` (both already normalized into `[0, 360)`) overlaps.

    Walks zone indices upward from `minlon_norm`'s own zone, wrapping mod `_WAC_EMP_N_LON_ZONES`, until
    reaching `maxlon_norm`'s zone -- this also transparently covers an AOI that straddles the 0/360 deg
    cut itself (`minlon_norm > maxlon_norm` after normalizing, e.g. an AOI centered near true longitude
    0), since that's just zone 3 followed by zone 0 in this same walk, not a fundamentally different
    case. Correct as long as the AOI's own true angular width is well under half the Moon's
    circumference (true for any real camera footprint, padded or not) -- otherwise which "direction"
    is the short way around is ambiguous, guarded against below rather than assumed.

    :returns: Zone indices, in walk order (not necessarily sorted -- e.g. `[3, 0]` for a 0/360 straddle).
    """
    zone_min = int(minlon_norm // _WAC_EMP_LON_ZONE_WIDTH_DEG)
    zone_max = int(maxlon_norm // _WAC_EMP_LON_ZONE_WIDTH_DEG)
    zones = [zone_min]
    zone = zone_min
    while zone != zone_max:
        zone = (zone + 1) % _WAC_EMP_N_LON_ZONES
        zones.append(zone)
        if len(zones) > _WAC_EMP_N_LON_ZONES:
            raise ValueError(
                f"Camera footprint's padded AOI (longitude range {minlon_norm:.2f}..{maxlon_norm:.2f} "
                "deg, normalized) spans more than a full turn of longitude -- not a real camera "
                "footprint; refusing to guess a wrap direction."
            )
    return zones


def _equirect_tile_id(hemisphere: str, lon_zone: int, wavelength_nm: int, ppd: int) -> str:
    lon_center_code = round(lon_zone * _WAC_EMP_LON_ZONE_WIDTH_DEG + _WAC_EMP_LON_ZONE_WIDTH_DEG / 2) * 10
    return f"WAC_EMP_{wavelength_nm}NM_E{_WAC_EMP_LAT_BAND_CENTER_CODE}{hemisphere}{lon_center_code:04d}_{ppd:03d}P"


def _polar_tile_id(hemisphere: str) -> str:
    return f"WAC_EMP_{_WAC_EMP_POLAR_WAVELENGTH_NM}NM_P900{hemisphere}0000_{_WAC_EMP_POLAR_PPD:03d}P"


def wac_emp_tile_ids_for_bbox(
    dst_bbox_m: tuple,
    center_lon_deg: float,
    center_lat_deg: float,
    moon_radius_m: float,
    wavelength_nm: int = DEFAULT_HAPKE_CALIBRATION_WAVELENGTH_NM,
    ppd: int = 304,
) -> list[str]:
    """Resolve every WAC_EMP PDS4 tile (product ID, no extension) needed to fully cover `dst_bbox_m` --
    more than one if the padded AOI straddles a tile boundary (the equator, the equirect grid's own
    90-deg longitude zones, or its +-`WAC_EMP_MAX_ABS_LATITUDE_DEG` split with the polar tile pair).
    Callers mosaic the returned tiles (`fetch_wac_emp_reflectance`/
    `reproject_wac_emp_reflectance_to_local_grid`) rather than treating any one of them as sufficient
    alone.

    :param dst_bbox_m: The local-Orthographic working grid's own already-padded bbox, meters -- see
        `dem_ortho.fetch_and_shade_ortho`.
    :param center_lon_deg: Local Orthographic CRS tangent point longitude, degrees.
    :param center_lat_deg: Local Orthographic CRS tangent point latitude, degrees.
    :param moon_radius_m: Sphere radius, meters.
    :param wavelength_nm: One of the archive's 7 bands (matches `HAPKE_CALIBRATION_WAVELENGTHS_NM`).
    :param ppd: A resolution the archive offers for that wavelength (every band has 64 ppd; 643nm
        additionally has a 304 ppd product, this project's own default).
    :returns: One or more product IDs, e.g. `["WAC_EMP_643NM_E300N0450_304P"]` (equirect only) or
        `["WAC_EMP_643NM_E300N0450_304P", "WAC_EMP_643NM_P900N0000_304P"]` (straddling the polar
        boundary) -- order is the walk order `_lat_bands_touched`/`_lon_zones_touched` produce, not
        meaningful to callers (they mosaic, not index into this list).
    :raises ValueError: If `wavelength_nm` isn't one of the archive's bands, if the AOI needs the polar
        tile pair but `wavelength_nm`/`ppd` isn't the one combination the archive actually offers
        there, or if the AOI is implausibly wide (`_lon_zones_touched`'s own guard).
    """
    # Product ID format, confirmed via the archive's own S3 bucket listing (see
    # docs/data-sources/wac-emp-pds4.md for the full derivation):
    # `WAC_EMP_<wavelength_nm>NM_E300<N|S><lon_center_deg*10:04d>_<ppd:03d>P` (equirect) or
    # `WAC_EMP_<wavelength_nm>NM_P900<N|S>0000_<ppd:03d>P` (polar -- always lon center code "0000",
    # there's no longitude zoning at a pole).
    #
    # Uses the same `transform_bounds`-on-the-destination-grid technique
    # `dem_gld100.astropedia_coverage_bbox_deg` uses, not an independently-padded degree-space bbox
    # (see that function's own trailing comment for why the latter causes corner nodata gaps).
    if wavelength_nm not in HAPKE_CALIBRATION_WAVELENGTHS_NM:
        raise ValueError(
            f"wavelength_nm={wavelength_nm} is not one of the archive's own bands {HAPKE_CALIBRATION_WAVELENGTHS_NM}"
        )
    padded_bbox_m = pad_bbox(dst_bbox_m, DEM_FETCH_SAFETY_MARGIN_FRACTION)
    geo_crs = geographic_crs(moon_radius_m)
    ortho_crs = local_orthographic_crs(center_lon_deg, center_lat_deg, moon_radius_m)
    minlon, minlat, maxlon, maxlat = transform_bounds(ortho_crs, geo_crs, *padded_bbox_m)
    minlon_norm, maxlon_norm = minlon % 360.0, maxlon % 360.0

    tile_ids: list[str] = []
    needs_polar = False
    for lo, hi in _lat_bands_touched(minlat, maxlat):
        if hi <= -WAC_EMP_MAX_ABS_LATITUDE_DEG or lo >= WAC_EMP_MAX_ABS_LATITUDE_DEG:
            # Polar band: no longitude zoning at all (one tile per hemisphere covers the whole cap),
            # so `minlon`/`maxlon` -- which can be meaningless near a pole an AOI genuinely encircles
            # -- are never consulted here.
            needs_polar = True
            tile_ids.append(_polar_tile_id("N" if lo >= WAC_EMP_MAX_ABS_LATITUDE_DEG else "S"))
        else:
            hemisphere = "N" if lo >= 0.0 else "S"
            for zone in _lon_zones_touched(minlon_norm, maxlon_norm):
                tile_ids.append(_equirect_tile_id(hemisphere, zone, wavelength_nm, ppd))

    if needs_polar and (wavelength_nm != _WAC_EMP_POLAR_WAVELENGTH_NM or ppd != _WAC_EMP_POLAR_PPD):
        raise ValueError(
            f"Camera footprint's padded AOI (latitude range {minlat:.2f}..{maxlat:.2f} deg) needs "
            f"the polar-stereographic tile pair, which the archive only offers at "
            f"{_WAC_EMP_POLAR_WAVELENGTH_NM}nm/{_WAC_EMP_POLAR_PPD}ppd (requested "
            f"wavelength_nm={wavelength_nm}, ppd={ppd})."
        )

    # Dedupe while preserving first-seen order -- a degenerate padded AOI could otherwise repeat a
    # zone (e.g. `_lat_bands_touched` never actually double-counts a band, but keeping this cheap
    # safety net costs nothing and matches `merge_local_grid_arrays`'s own "no real caller relies on
    # duplicates" assumption).
    seen: set[str] = set()
    deduped = []
    for tile_id in tile_ids:
        if tile_id not in seen:
            seen.add(tile_id)
            deduped.append(tile_id)
    return deduped


def fetch_wac_emp_reflectance(
    dst_bbox_m: tuple,
    center_lon_deg: float,
    center_lat_deg: float,
    config: TrntestConfig,
    wavelength_nm: int = DEFAULT_HAPKE_CALIBRATION_WAVELENGTH_NM,
    ppd: int = 304,
) -> list[tuple]:
    """Live default ortho/texture source: resolve and fetch/cache every WAC_EMP PDS4 tile covering
    `dst_bbox_m` (usually one, more if the AOI straddles a tile boundary -- see
    `wac_emp_tile_ids_for_bbox`), mirroring `dem_gld100.fetch_dem_astropedia`'s own shape per tile.

    :param dst_bbox_m: The local-Orthographic working grid's own already-padded bbox, meters.
    :param center_lon_deg: Local Orthographic CRS tangent point longitude, degrees.
    :param center_lat_deg: Local Orthographic CRS tangent point latitude, degrees.
    :param config: Project config (`cache_root`, `wac_emp_base_url`).
    :param wavelength_nm: Passed through to `wac_emp_tile_ids_for_bbox`.
    :param ppd: Passed through to `wac_emp_tile_ids_for_bbox`.
    :returns: `[(local_cached_path, product_id), ...]`, one entry per tile --
        `reproject_wac_emp_reflectance_to_local_grid` needs only the paths (it reads each AOI window
        directly from the file's own embedded georeferencing); the product IDs are returned for
        logging/cache-busting/debugging.
    :raises ValueError: If the footprint needs a tile this project doesn't fetch
        (`wac_emp_tile_ids_for_bbox`).
    """
    product_ids = wac_emp_tile_ids_for_bbox(
        dst_bbox_m, center_lon_deg, center_lat_deg, MOON_RADIUS_M, wavelength_nm=wavelength_nm, ppd=ppd
    )
    return [
        (cache.fetch_wac_emp_tile(product_id, config.cache_root, config.wac_emp_base_url), product_id)
        for product_id in product_ids
    ]


def _reproject_one_wac_emp_tile_to_array(
    wac_emp_path,
    dst_bbox_m,
    dst_width: int,
    dst_height: int,
    center_lon_deg: float,
    center_lat_deg: float,
    moon_radius_m: float,
    resampling: Resampling,
    tolerance: float,
    apply_edge_correction: bool = True,
):
    """Read just the AOI from one local cached WAC_EMP tile and reproject it onto the per-camera local
    Orthographic working grid the DEM fetch uses -- the single-tile core
    `reproject_wac_emp_reflectance_to_local_grid` calls once per tile and mosaics.

    :param wac_emp_path: One of `fetch_wac_emp_reflectance`'s cached file paths.
    :param dst_bbox_m: Destination `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param dst_width: Destination width, pixels.
    :param dst_height: Destination height, pixels.
    :param center_lon_deg: Destination CRS tangent point longitude, degrees.
    :param center_lat_deg: Destination CRS tangent point latitude, degrees.
    :param moon_radius_m: Sphere radius, meters.
    :param resampling: `rasterio.warp` resampling method.
    :param tolerance: `rasterio.warp.reproject` error tolerance.
    :param apply_edge_correction: Whether to apply `wac_emp_edge_correction`'s masking/model-fit
        correction for the archive's own real ±60 deg edge-brightening defect (both hemispheres) --
        normally sourced from `TrntestConfig.wac_emp_edge_correction_enabled`, exposed as a parameter
        here so this single-tile core stays a pure function of its own arguments. Set false to get
        this tile's raw, uncorrected archive data.
    :returns: The reprojected `(dst_height, dst_width)` array -- `np.nan` (per
        `reproject_raster_to_local_grid_array`'s `dst_nodata=nan` convention) wherever this tile alone
        doesn't cover the destination grid (always true for at least part of it when the AOI straddles
        a tile boundary and this is only one of several tiles covering it). Values are physical
        reflectance (IEEE754 float32, no embedded display stretch), not Lunaserv WMS-served DN.
    """
    # Mirrors `dem_gld100.reproject_astropedia_elevation_to_local_grid`'s window-read-then-warp shape,
    # except the AOI window comes directly from `dst_bbox_m` transformed into the file's own embedded
    # CRS. No separate degree-space bbox intermediate is needed here, unlike Astropedia's path: this
    # file's PDS3 label carries a trustworthy projected CRS/transform GDAL's PDS3 driver reads
    # natively, not a hand-rolled equirect PROJ4 string or manual byte offsets.
    #
    # Because this is reflectance, not DN, no `/255.0` un-scaling assumption applies to this output;
    # `hapke.hapke_shade_ortho`/`hapke.shade_ortho` treat it as reflectance directly (see their own
    # docstrings for the resulting numeric-pipeline change).
    with rasterio.open(wac_emp_path) as src:
        src_nodata = src.nodata
        left, bottom, right, top = transform_bounds(
            local_orthographic_crs(center_lon_deg, center_lat_deg, moon_radius_m), src.crs, *dst_bbox_m
        )
        # The equirect tiles' own branch-cut bug (see below) is specific to their linear
        # `x = R * lon_rad` formula -- the polar tiles' real Polar Stereographic projection has no
        # such branch cut (longitude enters through smooth sin/cos terms, not a raw multiply that
        # needs unwrapping), so none of this correction applies there. Gating on the actual PROJ4
        # `proj` tag (not e.g. a filename check) means this stays correct even if a future WAC_EMP
        # tile family turns out to need the same treatment, or an existing one doesn't.
        is_equirect = src.crs.to_dict().get("proj") == "eqc"
        if is_equirect:
            # `transform_bounds` normalizes longitude into (-180, 180] before applying `src.crs`'s
            # `central_meridian=0` Equirectangular formula (`x = R * lon_rad`), but each WAC_EMP tile's
            # own PDS4 georeferencing is written in unwrapped, continuous longitude -- the "E300*2250"
            # (180-270 deg) tile's raster spans x in [R*pi, R*1.5pi], entirely positive, never negated
            # back into (-180, 180]'s range. For any AOI whose true longitude falls past +-180 deg in
            # that continuous domain (confirmed via a real `M1314068239CE` repro, longitude -160.04 deg
            # / 199.96 deg unwrapped), the normalized-vs-unwrapped mismatch lands `left`/`right` a full
            # sphere circumference away from the tile's actual raster -- silently producing a `Window`
            # with a wildly wrong offset that later reads as zero-width (`CPLE_AppDefinedError: Invalid
            # dataset dimensions`). Latitude has no such branch cut, so `bottom`/`top` need no
            # correction. Shifting by the sphere's own circumference (this CRS's one linear
            # degree-of-freedom) puts the window back where the tile's own longitude convention
            # actually stores it.
            circumference_m = 2 * math.pi * moon_radius_m
            if right < src.bounds.left:
                left, right = left + circumference_m, right + circumference_m
            elif left > src.bounds.right:
                left, right = left - circumference_m, right - circumference_m
        window = window_from_bounds(left, bottom, right, top, transform=src.transform)
        src_transform = window_transform(window, src.transform)
        # `boundless=True` (not this project's usual plain windowed read): unlike the old single-tile
        # lookup this replaced, a tile returned by `wac_emp_tile_ids_for_bbox` when the AOI straddles a
        # boundary only covers *part* of `dst_bbox_m` by construction -- the window computed above
        # legitimately extends past this tile's own raster on the side the other tile(s) cover instead.
        # A plain (non-boundless) read would silently clip to the tile's real extent, returning an
        # array smaller than `window` while `src_transform` above still describes the *unclipped*
        # window's own origin -- misregistering every pixel by the clipped amount. `boundless=True`
        # always returns exactly `window`'s own shape, filled with `fill_value` beyond the tile's real
        # data, which `reproject`'s `src_nodata` below (and `merge_local_grid_arrays` after it) then
        # correctly treats as "not covered by this tile" rather than misplaced real data.
        if src_nodata is None:
            src_nodata = float("nan")
        reflectance = src.read(1, window=window, boundless=True, fill_value=src_nodata)

        # Correct the archive's own real ±60 deg edge-brightening defect (`wac_emp_edge_correction` --
        # kept in its own module, toggleable, since it's a fix for a specific data defect, not a
        # structural part of this reprojection) before any reprojection/resampling touches this window
        # -- native pixel space is where it was measured and where "N pixels from the boundary" is
        # unambiguous, unlike the destination local-Orthographic grid `reproject` produces below. Each
        # function checks both hemispheres' own boundary internally and no-ops for whichever (usually
        # both) this window doesn't reach, regardless of the flag.
        if apply_edge_correction:
            if is_equirect:
                wac_emp_edge_correction.mask_equirect_edge_row(
                    reflectance, src_transform, src.crs, moon_radius_m, src_nodata
                )
            else:
                wac_emp_edge_correction.mask_and_correct_polar_edge(
                    reflectance, src_transform, src.crs, moon_radius_m, src_nodata
                )

        if not is_equirect:
            warp_src_crs, warp_src_transform = src.crs, src_transform
        else:
            # The same branch cut bites `rasterio.warp.reproject` below, not just the window read
            # above: it runs its own `src.crs` <-> destination-CRS coordinate transform per pixel,
            # which normalizes longitude into (-180, 180] exactly like `transform_bounds` did, and
            # (for a `central_meridian=0` tile whose own raster lives in unwrapped, past-180-deg
            # coordinates) finds nothing there -- silently producing an all-nodata output instead of
            # raising. Re-expressing the read window in an equivalent Equirectangular CRS whose
            # `central_meridian` is this tile's own PROJ-normalized center (rather than the tile
            # file's literal one, whatever that happens to be) sidesteps this: every point within one
            # 90-deg-wide WAC_EMP zone sits within 45 deg of its own center, so no destination
            # longitude near it can cross +-180 deg from that center either. `src.crs`'s inverse
            # projection (`warp_transform`, i.e. real PROJ, not a hand-rolled `x = R * lon_rad`
            # assumption that would only hold for `central_meridian=0` specifically) gives this
            # tile's own center point's true, already-normalized longitude; a pure `central_meridian`
            # change is just an additive shift in this linear projection, so translating
            # `src_transform`'s origin by that same center's raw-frame X keeps every pixel at its
            # real physical location.
            src_center_x = (src.bounds.left + src.bounds.right) / 2
            src_center_y = (src.bounds.bottom + src.bounds.top) / 2
            (warp_center_lon_deg,), _ = warp_transform(
                src.crs, geographic_crs(moon_radius_m), [src_center_x], [src_center_y]
            )
            warp_src_crs = f"+proj=eqc +lat_ts=0 +lon_0={warp_center_lon_deg} +R={moon_radius_m} +units=m +no_defs"
            warp_src_transform = rasterio.Affine(
                src_transform.a,
                src_transform.b,
                src_transform.c - src_center_x,
                src_transform.d,
                src_transform.e,
                src_transform.f,
            )

    return reproject_raster_to_local_grid_array(
        reflectance,
        warp_src_crs,
        warp_src_transform,
        dst_bbox_m,
        dst_width,
        dst_height,
        center_lon_deg,
        center_lat_deg,
        moon_radius_m,
        resampling,
        tolerance,
        src_nodata=src_nodata,
        dst_nodata=float("nan"),
    )


def reproject_wac_emp_reflectance_to_local_grid(
    wac_emp_paths: list,
    dst_bbox_m,
    dst_width: int,
    dst_height: int,
    center_lon_deg: float,
    center_lat_deg: float,
    moon_radius_m: float,
    output_path,
    resampling: Resampling = Resampling.bilinear,
    tolerance: float = 0.125,
    apply_edge_correction: bool = True,
):
    """Reproject one or more local cached WAC_EMP tiles onto the per-camera local Orthographic working
    grid the DEM fetch uses, mosaicking them if there's more than one (a straddling AOI, per
    `wac_emp_tile_ids_for_bbox`) rather than requiring a single tile to cover the whole grid.

    :param wac_emp_paths: `fetch_wac_emp_reflectance`'s cached file paths -- one per tile; the usual
        case is a single-element list.
    :param dst_bbox_m: Destination `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param dst_width: Destination width, pixels.
    :param dst_height: Destination height, pixels.
    :param center_lon_deg: Destination CRS tangent point longitude, degrees.
    :param center_lat_deg: Destination CRS tangent point latitude, degrees.
    :param moon_radius_m: Sphere radius, meters.
    :param output_path: Where to write the reprojected reflectance GeoTIFF.
    :param resampling: `rasterio.warp` resampling method.
    :param tolerance: `rasterio.warp.reproject` error tolerance.
    :param apply_edge_correction: Whether to apply `wac_emp_edge_correction`'s fix for the archive's
        own real ±60 deg edge-brightening defect, both hemispheres (masking each source tile's own
        worst-affected native pixels, subtracting a fitted model from the rest, then closing the small
        coverage gap the masking opens) -- normally sourced from `TrntestConfig
        .wac_emp_edge_correction_enabled`. Set false to get the raw, uncorrected archive data
        mosaicked with no gap-filling either (nothing to fill without the masking).
    :returns: `output_path`, as a `Path`. Values are physical reflectance (IEEE754 float32, no
        embedded display stretch), not Lunaserv WMS-served DN.
    """
    # Each tile is read/warped independently (`_reproject_one_wac_emp_tile_to_array`, which already
    # applies the equirect antimeridian-branch-cut fix per its own source CRS), then merged with
    # `merge_local_grid_arrays` -- correct regardless of which combination of tile projection families
    # (equirect, polar, or one of each) is involved, since the merge step only ever sees the shared
    # destination grid, never the source tiles' own differing CRSs.
    arrays = [
        _reproject_one_wac_emp_tile_to_array(
            wac_emp_path,
            dst_bbox_m,
            dst_width,
            dst_height,
            center_lon_deg,
            center_lat_deg,
            moon_radius_m,
            resampling,
            tolerance,
            apply_edge_correction,
        )
        for wac_emp_path in wac_emp_paths
    ]
    merged = merge_local_grid_arrays(arrays)
    if apply_edge_correction:
        # Closes the small, artifact-scale coverage gap the edge correction above can leave (see
        # `wac_emp_edge_correction.GAP_FILL_MAX_RADIUS_PX`'s own comment) -- a no-op whenever `merged`
        # has no `NaN` at all, the overwhelmingly common case for any footprint that doesn't touch
        # either ±60 deg boundary. Skipped entirely when the correction itself is off: there's no
        # masking-induced gap to close, and unconditionally filling could paper over a genuine
        # no-coverage region instead.
        merged = wac_emp_edge_correction.fill_nearby_gaps(merged, wac_emp_edge_correction.GAP_FILL_MAX_RADIUS_PX)

    dst_crs = local_orthographic_crs(center_lon_deg, center_lat_deg, moon_radius_m)
    dst_transform = transform_from_bounds(*dst_bbox_m, dst_width, dst_height)
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
            dst.write(merged, 1)
    return Path(output_path)
