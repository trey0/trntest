"""Live default DEM source: USGS Astropedia's flat-file GLD100 DEM. See
docs/data-sources/astropedia-gld100.md and `dem_ortho.fetch_dem`.
"""

import math
from pathlib import Path

import numpy as np
import rasterio
from affine import Affine
from rasterio.warp import Resampling, transform_bounds
from rasterio.windows import Window
from rasterio.windows import from_bounds as window_from_bounds
from rasterio.windows import transform as window_transform

from trntest import cache
from trntest.config import MOON_RADIUS_M, TrntestConfig
from trntest.geo_utils import (
    DEM_FETCH_SAFETY_MARGIN_FRACTION,
    geographic_crs,
    local_orthographic_crs,
    merge_local_grid_arrays,
    pad_bbox,
    reproject_raster_to_local_grid_array,
    write_local_grid_array,
)

# Astropedia's flat-file GLD100 DEM (`config.astropedia_gld100_url`) covers +-79 deg latitude
# (`gdalinfo`'s own corner coordinates: 79d0'6.57" both ways). No silent fallback to the deprecated
# Lunaserv-native path for footprints beyond this -- see `astropedia_coverage_bbox_deg`.
ASTROPEDIA_MAX_ABS_LATITUDE_DEG = 79.0


def astropedia_coverage_bbox_deg(
    dst_bbox_m: tuple, center_lon_deg: float, center_lat_deg: float, moon_radius_m: float
) -> tuple:
    """The lon/lat degree bbox needed to fully cover `dst_bbox_m` once reprojected, plus a small
    safety margin for the resampling kernel's own footprint.

    :param dst_bbox_m: The local-Orthographic working grid's own bbox, meters -- see
        `dem_ortho.fetch_dem`.
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
            "path (lunaserv_wms.fetch_dem_native/reproject_dem_to_local_grid) covers this latitude "
            "range but has its own known, unfixed artifact, and isn't used automatically here."
        )
    return minlon, minlat, maxlon, maxlat


def fetch_dem_astropedia(
    dst_bbox_m: tuple, center_lon_deg: float, center_lat_deg: float, config: TrntestConfig
) -> Path:
    """Live default DEM source: ensure Astropedia's flat-file GLD100 DEM is downloaded/cached locally.

    :param dst_bbox_m: `dem_ortho.fetch_dem`'s own already-padded (and, if applicable, already unioned
        with `extra_footprint_lonlat_deg`) local-Orthographic working-grid bbox.
    :param center_lon_deg: Local Orthographic CRS tangent point longitude, degrees.
    :param center_lat_deg: Local Orthographic CRS tangent point latitude, degrees.
    :param config: Project config (`cache_root`, `astropedia_gld100_url`).
    :returns: The local cached file path.
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
    astropedia_coverage_bbox_deg(dst_bbox_m, center_lon_deg, center_lat_deg, MOON_RADIUS_M)
    return cache.fetch_astropedia_gld100(config.cache_root, config.astropedia_gld100_url)


def reproject_astropedia_elevation_to_local_grid(
    astropedia_path,
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
    """Read just the AOI from the local cached Astropedia file and reproject it onto the per-camera
    local Orthographic working grid `lunaserv_wms.reproject_dem_to_local_grid` uses.

    :param astropedia_path: `fetch_dem_astropedia`'s cached file path.
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
    :raises ValueError: If the file isn't Equidistant Cylindrical, as GLD100 is.
    """
    # Fast (no network, no row-strip-over-HTTP penalty), unlike a remote `/vsicurl/` windowed read of
    # the same file. Uses the file's own embedded georeferencing rather than hardcoding Astropedia's
    # Equidistant Cylindrical PROJ4 parameters by hand, since this file (unlike Lunaserv's GetMap
    # responses) has trustworthy embedded georeferencing.
    #
    # This data is already elevation (Int16 meters, nodata -32768), not planetocentric radius like
    # Lunaserv's DTM layer -- `lunaserv_wms.radius_to_elevation` is skipped entirely for this path.
    #
    # The file is centered on 180 deg, so its raster edges meet at 0 deg. Transforming an AOI that
    # straddles 0 deg into the file's own CRS gives a whole-width min/max whose edges are wherever
    # `transform_bounds`' densification happened to sample nearest the cut: up to ~0.5 deg just west
    # of 0 deg came back NaN (the `dem_gld100` seam probes at 45N/60N 0E), the same failure
    # `ortho_wac_emp` had at 180 deg. So the window is computed in an Equirectangular CRS centered on
    # the AOI, where it never crosses a cut, and read as up to two pieces, one per raster edge, each
    # warped separately. Concatenating the pieces into one array would misregister one of them: the
    # raster is 109165 px x 100 m, ~94 m more than the circumference, so its last column overlaps
    # its first.
    with rasterio.open(astropedia_path) as src:
        src_nodata = src.nodata
        warp_crs = f"+proj=eqc +lat_ts=0 +lon_0={center_lon_deg} +R={moon_radius_m} +units=m +no_defs"
        left, bottom, right, top = transform_bounds(
            local_orthographic_crs(center_lon_deg, center_lat_deg, moon_radius_m),
            warp_crs,
            *pad_bbox(dst_bbox_m, DEM_FETCH_SAFETY_MARGIN_FRACTION),
        )
        # x in `src.crs` = x in `warp_crs` + `shift` (a `lon_0` change is a pure x shift in this
        # linear projection), modulo the circumference.
        if src.crs.to_dict().get("proj") != "eqc":
            raise ValueError(f"{astropedia_path} isn't Equidistant Cylindrical like GLD100: {src.crs}")
        src_lon0_deg = src.crs.to_dict().get("lon_0", 0.0)
        circumference_m = 2 * np.pi * moon_radius_m
        shift_m = moon_radius_m * np.radians((center_lon_deg - src_lon0_deg + 180.0) % 360.0 - 180.0)
        pieces = []
        for wrap_m in (-circumference_m, 0.0, circumference_m):
            piece_left = max(left + shift_m + wrap_m, src.bounds.left)
            piece_right = min(right + shift_m + wrap_m, src.bounds.right)
            if piece_left >= piece_right:
                continue
            # An integer window, flooring the near edges and ceiling the far ones so it contains the
            # requested extent. GDAL reads a fractional window as the nearest whole pixels while
            # `window_transform` keeps the fraction, which put every DEM up to half a pixel off.
            window = window_from_bounds(piece_left, bottom, piece_right, top, transform=src.transform)
            col_off, row_off = math.floor(window.col_off), math.floor(window.row_off)
            col_stop = math.ceil(window.col_off + window.width)
            row_stop = math.ceil(window.row_off + window.height)
            window = Window(col_off, row_off, col_stop - col_off, row_stop - row_off).intersection(
                Window(0, 0, src.width, src.height)
            )
            src_transform = window_transform(window, src.transform)
            warp_transform = Affine.translation(-shift_m - wrap_m, 0) * src_transform
            pieces.append((src.read(1, window=window), warp_transform))

    arrays = [
        reproject_raster_to_local_grid_array(
            elevation,
            warp_crs,
            warp_transform,
            dst_bbox_m,
            dst_width,
            dst_height,
            center_lon_deg,
            center_lat_deg,
            moon_radius_m,
            resampling,
            tolerance,
            src_nodata=src_nodata,
            dst_nodata=np.nan,
        )
        for elevation, warp_transform in pieces
    ]
    return write_local_grid_array(
        merge_local_grid_arrays(arrays), dst_bbox_m, center_lon_deg, center_lat_deg, moon_radius_m, output_path
    )
