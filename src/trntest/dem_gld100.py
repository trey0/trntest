"""Live default DEM source: USGS Astropedia's flat-file GLD100 DEM. See
docs/data-sources/astropedia-gld100.md and `dem_ortho.fetch_dem`.
"""

import numpy as np
import rasterio
from rasterio.warp import Resampling, transform_bounds
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
) -> tuple:
    """Live default DEM source: ensure Astropedia's flat-file GLD100 DEM is downloaded/cached locally.

    :param dst_bbox_m: `dem_ortho.fetch_dem`'s own already-padded (and, if applicable, already unioned
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
):
    """Read just the AOI from the local cached Astropedia file and reproject it onto the per-camera
    local Orthographic working grid `lunaserv_wms.reproject_dem_to_local_grid` uses.

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
    # Lunaserv's DTM layer -- `lunaserv_wms.radius_to_elevation` is skipped entirely for this path.
    with rasterio.open(astropedia_path) as src:
        src_crs = src.crs
        src_nodata = src.nodata
        minlon, minlat, maxlon, maxlat = deg_bbox
        geo_crs = geographic_crs(moon_radius_m)
        left, bottom, right, top = transform_bounds(geo_crs, src_crs, minlon, minlat, maxlon, maxlat)
        window = window_from_bounds(left, bottom, right, top, transform=src.transform)
        src_transform = window_transform(window, src.transform)
        elevation = src.read(1, window=window)

    return reproject_raster_to_local_grid(
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
