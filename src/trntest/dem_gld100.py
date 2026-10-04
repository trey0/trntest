"""Live default DEM source: USGS Astropedia's flat-file GLD100 DEM. See
docs/data-sources/astropedia-gld100.md and `dem_ortho.fetch_dem`.
"""

from pathlib import Path

from rasterio.warp import Resampling, transform_bounds

from trntest import cache
from trntest.config import MOON_RADIUS_M, TrntestConfig
from trntest.geo_utils import (
    DEM_FETCH_SAFETY_MARGIN_FRACTION,
    geographic_crs,
    local_orthographic_crs,
    pad_bbox,
    read_eqc_raster_to_local_grid_array,
    write_local_grid_array,
)

# Astropedia's flat-file GLD100 DEM (`config.astropedia_gld100_url`) covers +-79 deg latitude
# (`gdalinfo`'s own corner coordinates: 79d0'6.57" both ways). No silent fallback to the deprecated
# Lunaserv-native path for footprints beyond this -- see `check_astropedia_coverage`.
ASTROPEDIA_MAX_ABS_LATITUDE_DEG = 79.0


def check_astropedia_coverage(
    dst_bbox_m: tuple, center_lon_deg: float, center_lat_deg: float, moon_radius_m: float
) -> None:
    """Check that the GLD100 file covers `dst_bbox_m`, plus a small safety margin for the resampling
    kernel's own footprint.

    :param dst_bbox_m: The local-Orthographic working grid's own bbox, meters -- see
        `dem_ortho.fetch_dem`.
    :param center_lon_deg: Local Orthographic CRS tangent point longitude, degrees.
    :param center_lat_deg: Local Orthographic CRS tangent point latitude, degrees.
    :param moon_radius_m: Sphere radius, meters.
    :raises ValueError: If the padded AOI extends beyond `ASTROPEDIA_MAX_ABS_LATITUDE_DEG`.
    """
    # The `DEM_FETCH_SAFETY_MARGIN_FRACTION` pad accounts for bilinear resampling needing neighbor
    # samples just past the destination edge.
    #
    # The latitude range comes from `dst_bbox_m`'s own boundary (`rasterio.warp.transform_bounds`
    # densely samples the whole edge, not just the 4 corners), not from a degree-space bbox padded
    # independently around the footprint's own corners: two independently-padded bboxes -- one in
    # degrees, one in local-Orthographic meters -- aren't guaranteed to cover each other, since a
    # square's diagonal corners are ~41% farther from center than its edge midpoints.
    #
    # No automatic fallback to the deprecated Lunaserv path -- a caller that wants one has to ask for
    # it explicitly.
    padded_bbox_m = pad_bbox(dst_bbox_m, DEM_FETCH_SAFETY_MARGIN_FRACTION)
    geo_crs = geographic_crs(moon_radius_m)
    ortho_crs = local_orthographic_crs(center_lon_deg, center_lat_deg, moon_radius_m)
    _, minlat, _, maxlat = transform_bounds(ortho_crs, geo_crs, *padded_bbox_m)
    if minlat < -ASTROPEDIA_MAX_ABS_LATITUDE_DEG or maxlat > ASTROPEDIA_MAX_ABS_LATITUDE_DEG:
        raise ValueError(
            f"Camera footprint's padded AOI (latitude range {minlat:.2f}..{maxlat:.2f} deg) extends "
            f"beyond Astropedia's GLD100 flat file's +-{ASTROPEDIA_MAX_ABS_LATITUDE_DEG} deg "
            "coverage -- no DEM data available there from this source. The deprecated Lunaserv-native "
            "path (lunaserv_wms.fetch_dem_native/reproject_dem_to_local_grid) covers this latitude "
            "range but has its own known, unfixed artifact, and isn't used automatically here."
        )


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
        (`check_astropedia_coverage`).
    """
    # `cache.fetch_astropedia_gld100` fetches the whole ~10GB file, once, resumably; see its own
    # docstring for why this doesn't fetch a remote AOI window directly: the file isn't a
    # Cloud-Optimized GeoTIFF, so a remote windowed read pulls full-width row strips, which is slow.
    check_astropedia_coverage(dst_bbox_m, center_lon_deg, center_lat_deg, MOON_RADIUS_M)
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
    # the same file. Already elevation (Int16 meters, nodata -32768), not planetocentric radius like
    # Lunaserv's DTM layer, so no conversion.
    elevation = read_eqc_raster_to_local_grid_array(
        astropedia_path,
        dst_bbox_m,
        dst_width,
        dst_height,
        center_lon_deg,
        center_lat_deg,
        moon_radius_m,
        resampling,
        tolerance,
    )
    return write_local_grid_array(elevation, dst_bbox_m, center_lon_deg, center_lat_deg, moon_radius_m, output_path)
