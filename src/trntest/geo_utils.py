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
# `dem_gld100.check_astropedia_coverage`, `dem_gld100.reproject_astropedia_elevation_to_local_grid` and
# `ortho_wac_emp.wac_emp_tile_ids_for_bbox`, all of which derive their coverage from the same padded
# local-Orthographic working grid.
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
    # `tests/test_geo_utils.py` pins `local_grid_positions_moon_me`'s closed form to this CRS.
    return f"+proj=geocent +R={radius_m} +units=m +no_defs"


def local_enu_basis(center_lon_deg: float, center_lat_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The (East, North, Up) unit vectors, in MOON_ME, of the local tangent plane at a point.

    :param center_lon_deg: Tangent point longitude, degrees.
    :param center_lat_deg: Tangent point latitude, degrees.
    :returns: `(east, north, up)`, each a MOON_ME unit vector.
    """
    lon0, lat0 = math.radians(center_lon_deg), math.radians(center_lat_deg)
    east = np.array([-math.sin(lon0), math.cos(lon0), 0.0])
    north = np.array([-math.sin(lat0) * math.cos(lon0), -math.sin(lat0) * math.sin(lon0), math.cos(lat0)])
    up = np.array([math.cos(lat0) * math.cos(lon0), math.cos(lat0) * math.sin(lon0), math.sin(lat0)])
    return east, north, up


def pixel_center_coords_m(bbox, width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    """Pixel-center coordinates of a north-up raster covering `bbox`.

    :param bbox: `(minx, miny, maxx, maxy)`, meters.
    :param width: Raster width, pixels.
    :param height: Raster height, pixels.
    :returns: `(x_centers, y_centers)`, 1D; `y_centers[0]` is the top (north) row.
    """
    minx, miny, maxx, maxy = bbox
    x_centers = minx + (np.arange(width) + 0.5) * (maxx - minx) / width
    y_centers = maxy - (np.arange(height) + 0.5) * (maxy - miny) / height
    return x_centers, y_centers


def local_grid_positions_moon_me(
    x_m: np.ndarray,
    y_m: np.ndarray,
    elevation_m: np.ndarray,
    center_lon_deg: float,
    center_lat_deg: float,
    radius_m: float = MOON_RADIUS_M,
) -> np.ndarray:
    """True 3D MOON_ME positions of points given in a `local_orthographic_crs` frame plus elevation.

    :param x_m: Local Orthographic x, meters (any shape).
    :param y_m: Local Orthographic y, meters, same shape as `x_m`.
    :param elevation_m: Elevation above the `radius_m` sphere, meters, same shape as `x_m`.
    :param center_lon_deg: Local Orthographic CRS tangent point longitude, degrees.
    :param center_lat_deg: Local Orthographic CRS tangent point latitude, degrees.
    :param radius_m: Sphere radius, meters.
    :returns: `(*x_m.shape, 3)` MOON_ME positions, meters.
    """
    # Closed form, exact for `local_orthographic_crs`'s sphere: the orthographic projection of a sphere
    # point `P` is just its components along the tangent point's east/north axes, so
    # `P = x*east + y*north + sqrt(R^2 - x^2 - y^2)*up`, and elevation scales `P` radially by
    # `(R + h) / R` -- exactly what `rasterio.warp.transform` into `moon_geocentric_crs` computes
    # (pinned to it in `tests/test_geo_utils.py`), without PROJ's per-point overhead (~20x slower on a
    # 23M-sample grid). True 3D curvature, not a flat tangent-plane approximation; validated as part of
    # `hapke._terrain_photometric_angles` against ISIS `campt` and ASP `sfs`.
    east, north, up = local_enu_basis(center_lon_deg, center_lat_deg)
    x = np.asarray(x_m, dtype=np.float64)[..., None]
    y = np.asarray(y_m, dtype=np.float64)[..., None]
    h = np.asarray(elevation_m, dtype=np.float64)[..., None]
    on_sphere = x * east + y * north + np.sqrt(radius_m**2 - x**2 - y**2) * up
    return on_sphere * ((radius_m + h) / radius_m)


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


def reproject_raster_to_local_grid_array(
    source_array: np.ndarray,
    src_crs: str,
    src_transform,
    dst_bbox_m,
    dst_width: int,
    dst_height: int,
    center_lon_deg: float,
    center_lat_deg: float,
    moon_radius_m: float,
    resampling: Resampling,
    tolerance: float,
    src_nodata: float | None = None,
    dst_nodata: float | None = None,
) -> np.ndarray:
    """The same warp core `reproject_raster_to_local_grid` uses, returning the destination array
    directly instead of writing a GeoTIFF -- the piece a multi-tile mosaic caller needs (reproject each
    source tile separately, then combine the arrays with `merge_local_grid_arrays` before writing one
    output file); `reproject_raster_to_local_grid` itself is just this plus the write step, for every
    single-source caller that doesn't need to merge anything.

    :param source_array: Single-band source raster.
    :param src_crs: Source CRS.
    :param src_transform: Source affine transform.
    :param dst_bbox_m: Destination `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param dst_width: Destination width, pixels.
    :param dst_height: Destination height, pixels.
    :param center_lon_deg: Destination CRS tangent point longitude, degrees.
    :param center_lat_deg: Destination CRS tangent point latitude, degrees.
    :param moon_radius_m: Sphere radius, meters.
    :param resampling: `rasterio.warp` resampling method.
    :param tolerance: `rasterio.warp.reproject` error tolerance.
    :param src_nodata: Source nodata value, if any.
    :param dst_nodata: Destination nodata value, if any.
    :returns: The reprojected `(dst_height, dst_width)` `float32` array.
    """
    # Uses `rasterio.warp.reproject` so the resampling method is one this project controls explicitly,
    # not any server's opaque resampling. The destination Orthographic definition matches
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
    return reprojected


def merge_local_grid_arrays(arrays: list[np.ndarray]) -> np.ndarray:
    """Combine several same-shape `reproject_raster_to_local_grid_array` outputs into one, filling each
    destination pixel from whichever array actually covers it there.

    :param arrays: One or more same-shape arrays, each `np.nan` (per that function's `dst_nodata=nan`
        convention) outside its own source tile's coverage.
    :returns: The merged array -- `arrays[0]` where it's real, else the first later array that's real
        there, else `nan` if none of them cover that pixel. Callers mosaicking genuinely adjacent,
        non-overlapping tiles (this project's only real use case, see
        `ortho_wac_emp.wac_emp_tile_ids_for_bbox`) never have more than one array real at a given pixel,
        so this "first real one wins" rule never actually has to arbitrate a disagreement.
    """
    merged = arrays[0].copy()
    for array in arrays[1:]:
        nodata_mask = np.isnan(merged)
        merged[nodata_mask] = array[nodata_mask]
    return merged


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
    """Reproject a single-band source array onto the per-camera local Orthographic working grid and
    write it out as a GeoTIFF.

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
    # `ortho_wac_emp.reproject_wac_emp_reflectance_to_local_grid`).
    reprojected = reproject_raster_to_local_grid_array(
        source_array,
        src_crs,
        src_transform,
        dst_bbox_m,
        dst_width,
        dst_height,
        center_lon_deg,
        center_lat_deg,
        moon_radius_m,
        resampling,
        tolerance,
        src_nodata=src_nodata,
        dst_nodata=dst_nodata,
    )

    return write_local_grid_array(reprojected, dst_bbox_m, center_lon_deg, center_lat_deg, moon_radius_m, output_path)


def write_local_grid_array(
    array: np.ndarray, dst_bbox_m, center_lon_deg: float, center_lat_deg: float, moon_radius_m: float, output_path
) -> Path:
    """Write a `reproject_raster_to_local_grid_array`-shaped array (or a merge of several) as a
    single-band float32 GeoTIFF on its local Orthographic grid, atomically.

    :param array: `(height, width)` array, `NaN` = no data.
    :param dst_bbox_m: The grid's `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param center_lon_deg: CRS tangent point longitude, degrees.
    :param center_lat_deg: CRS tangent point latitude, degrees.
    :param moon_radius_m: Sphere radius, meters.
    :param output_path: Where to write it.
    :returns: `output_path`, as a `Path`.
    """
    height, width = array.shape
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": local_orthographic_crs(center_lon_deg, center_lat_deg, moon_radius_m),
        "transform": transform_from_bounds(*dst_bbox_m, width, height),
        "nodata": None,
    }
    with atomic_publish(Path(output_path)) as tmp:
        with rasterio.open(tmp, "w", **profile) as dst:
            dst.write(array, 1)
    return Path(output_path)
