"""Generic CRS/bbox/reprojection math shared by every DEM/ortho data-source module
(`dem_gld100.py`/`ortho_wac_emp.py`/`lunaserv_wms.py`) and by `isis_wac.py`'s own DEM sampling --
none of it is specific to any one data source. Deliberately dependency-free (no other `trntest`
module beyond `config`/`product_io`), so nothing here can create an import cycle.
"""

import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import Resampling, reproject

from trntest.config import MOON_RADIUS_M
from trntest.product_io import atomic_publish

# A small pad applied before checking/fetching a data source's own coverage, accounting for a
# resampling kernel needing neighbor samples just past the destination edge -- shared by
# `dem_gld100.astropedia_coverage_bbox_deg` and `ortho_wac_emp.wac_emp_tile_id_for_bbox`, both of
# which derive a degree-space coverage bbox from the same padded local-Orthographic working grid.
DEM_FETCH_SAFETY_MARGIN_FRACTION = 0.02


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
    # without a hand-derived closed-form correction. `hapke._terrain_photometric_angles` is the one
    # caller.
    return f"+proj=geocent +R={radius_m} +units=m +no_defs"


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
    # `IAU2000:30166` local-CRS layers (see `dem_ortho.fetch_dem`). No antimeridian-unwrapping special
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


def reproject_raster_to_local_grid(
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
    # Shared warp core behind every data-source-specific reprojection function
    # (`lunaserv_wms.reproject_dem_to_local_grid`, `dem_gld100.reproject_astropedia_elevation_to_local_grid`,
    # `ortho_wac_emp.reproject_wac_emp_reflectance_to_local_grid`). Uses `rasterio.warp.reproject` so the
    # resampling method is one this project controls explicitly, not any server's opaque resampling. The
    # destination Orthographic definition matches `orthographic_xy_m`'s own forward projection math
    # exactly (same center, same sphere radius, same projection family).
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
