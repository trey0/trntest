"""Live default ortho/texture source: WAC_EMP's own PDS4 archive, fetched directly rather than through
Lunaserv's WMS render. See docs/data-sources/wac-emp-pds4.md and `dem_ortho.fetch_and_shade_ortho`.
"""

import math

import rasterio
from rasterio.warp import Resampling, transform_bounds
from rasterio.warp import transform as warp_transform
from rasterio.windows import from_bounds as window_from_bounds
from rasterio.windows import transform as window_transform

from trntest import cache
from trntest.config import MOON_RADIUS_M, TrntestConfig
from trntest.geo_utils import (
    DEM_FETCH_SAFETY_MARGIN_FRACTION,
    geographic_crs,
    local_orthographic_crs,
    pad_bbox,
    reproject_raster_to_local_grid,
)
from trntest.hapke import DEFAULT_HAPKE_CALIBRATION_WAVELENGTH_NM, HAPKE_CALIBRATION_WAVELENGTHS_NM

# The WAC_EMP PDS4 archive's equirect (non-polar) tile grid covers only 0-60 deg in each hemisphere
# -- a separate polar-stereographic tile pair (`P900N`/`P900S`) covers the rest, 60-90 deg each
# hemisphere, and is also fetched by this project (see `wac_emp_tile_id_for_bbox`'s docstring and
# docs/data-sources/wac-emp-pds4.md's polar-tile bullet for the confirmed format/coverage facts).
WAC_EMP_MAX_ABS_LATITUDE_DEG = 60.0
# The archive only offers the polar tile pair at one band/resolution -- this project's own defaults
# (`DEFAULT_HAPKE_CALIBRATION_WAVELENGTH_NM = 643`, `ppd=304`) already match it, so every real call
# site hits this for free; a caller that explicitly requests a different combination for a
# polar-only footprint gets a clear `ValueError` (`wac_emp_tile_id_for_bbox`), not a wrong tile.
_WAC_EMP_POLAR_WAVELENGTH_NM = 643
_WAC_EMP_POLAR_PPD = 304
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
        `dem_ortho.fetch_and_shade_ortho`.
    :param center_lon_deg: Local Orthographic CRS tangent point longitude, degrees.
    :param center_lat_deg: Local Orthographic CRS tangent point latitude, degrees.
    :param moon_radius_m: Sphere radius, meters.
    :param wavelength_nm: One of the archive's 7 bands (matches `HAPKE_CALIBRATION_WAVELENGTHS_NM`).
    :param ppd: A resolution the archive offers for that wavelength (every band has 64 ppd; 643nm
        additionally has a 304 ppd product, this project's own default).
    :returns: The product ID, e.g. `WAC_EMP_643NM_E300N0450_304P` (equirect) or
        `WAC_EMP_643NM_P900N0000_304P` (polar).
    :raises ValueError: If `wavelength_nm` isn't one of the archive's bands, if the padded AOI
        straddles the equirect grid's own equator or `WAC_EMP_MAX_ABS_LATITUDE_DEG` boundary with the
        polar tile pair (no mosaic across either), if it straddles a 90-deg equirect longitude zone
        boundary, or if it needs the polar tile pair but `wavelength_nm`/`ppd` isn't the one
        combination the archive actually offers there.
    """
    # Product ID format, confirmed via the archive's own S3 bucket listing (see
    # docs/data-sources/wac-emp-pds4.md for the full derivation):
    # `WAC_EMP_<wavelength_nm>NM_E300<N|S><lon_center_deg*10:04d>_<ppd:03d>P` (equirect) or
    # `WAC_EMP_<wavelength_nm>NM_P900<N|S>0000_<ppd:03d>P` (polar -- always lon center code "0000",
    # there's no longitude zoning at a pole).
    #
    # Uses the same `transform_bounds`-on-the-destination-grid technique
    # `dem_gld100.astropedia_coverage_bbox_deg` uses, not an independently-padded degree-space bbox
    # (see that function's own trailing comment for why the latter causes corner nodata gaps). No
    # multi-tile mosaic in this pass, matching `astropedia_coverage_bbox_deg`'s own
    # no-automatic-fallback stance.
    if wavelength_nm not in HAPKE_CALIBRATION_WAVELENGTHS_NM:
        raise ValueError(
            f"wavelength_nm={wavelength_nm} is not one of the archive's own bands {HAPKE_CALIBRATION_WAVELENGTHS_NM}"
        )
    padded_bbox_m = pad_bbox(dst_bbox_m, DEM_FETCH_SAFETY_MARGIN_FRACTION)
    geo_crs = geographic_crs(moon_radius_m)
    ortho_crs = local_orthographic_crs(center_lon_deg, center_lat_deg, moon_radius_m)
    minlon, minlat, maxlon, maxlat = transform_bounds(ortho_crs, geo_crs, *padded_bbox_m)

    # Checked before any longitude-zone logic: near either pole, a padded AOI's own naive lon
    # min/max (from a plain bbox-corner transform) can be meaningless -- e.g. an AOI that genuinely
    # encircles the pole spans all 360 deg of longitude at once. Irrelevant here: the polar tile pair
    # has no longitude zoning at all (one tile per hemisphere, full 360 deg), so this branch never
    # needs `minlon`/`maxlon`.
    if minlat > WAC_EMP_MAX_ABS_LATITUDE_DEG or maxlat < -WAC_EMP_MAX_ABS_LATITUDE_DEG:
        hemisphere = "N" if minlat > WAC_EMP_MAX_ABS_LATITUDE_DEG else "S"
        if wavelength_nm != _WAC_EMP_POLAR_WAVELENGTH_NM or ppd != _WAC_EMP_POLAR_PPD:
            raise ValueError(
                f"Camera footprint's padded AOI (latitude range {minlat:.2f}..{maxlat:.2f} deg) needs "
                f"the polar-stereographic tile pair, which the archive only offers at "
                f"{_WAC_EMP_POLAR_WAVELENGTH_NM}nm/{_WAC_EMP_POLAR_PPD}ppd (requested "
                f"wavelength_nm={wavelength_nm}, ppd={ppd})."
            )
        return f"WAC_EMP_{_WAC_EMP_POLAR_WAVELENGTH_NM}NM_P900{hemisphere}0000_{_WAC_EMP_POLAR_PPD:03d}P"
    if maxlat > WAC_EMP_MAX_ABS_LATITUDE_DEG or minlat < -WAC_EMP_MAX_ABS_LATITUDE_DEG:
        raise ValueError(
            f"Camera footprint's padded AOI (latitude range {minlat:.2f}..{maxlat:.2f} deg) straddles "
            f"the equirect grid's +-{WAC_EMP_MAX_ABS_LATITUDE_DEG} deg boundary with the "
            "polar-stereographic tile pair -- this project doesn't mosaic across it."
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
) -> tuple:
    """Live default ortho/texture source: resolve and fetch/cache the single WAC_EMP PDS4 tile
    covering `dst_bbox_m`, mirroring `dem_gld100.fetch_dem_astropedia`'s own shape.

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
):
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
        reflectance = src.read(1, window=window)

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

    return reproject_raster_to_local_grid(
        reflectance,
        warp_src_crs,
        warp_src_transform,
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
        dst_nodata=float("nan"),
    )
