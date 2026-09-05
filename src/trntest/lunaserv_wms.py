"""Deprecated fallback DEM source: Lunaserv's own WMS-served DTM layer, in its native unprojected
geographic CRS. Superseded by `dem_gld100.py` for the live default path -- kept reachable for
comparison and as the still-current source for a few one-off diagnostics. See
docs/data-sources/lunaserv-wms.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import rasterio
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import Resampling

from trntest import cache
from trntest.config import MOON_RADIUS_M, TrntestConfig
from trntest.geo_utils import footprint_bbox_deg, geographic_crs, pad_bbox, reproject_raster_to_local_grid

if TYPE_CHECKING:
    from trntest.camera import Camera


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


def fetch_dem_native(camera: Camera, config: TrntestConfig, extra_footprint_lonlat_deg: dict | None = None) -> tuple:
    """**Deprecated** -- fetch the DTM layer in Lunaserv's native, unprojected geographic CRS.

    :param camera: Camera whose footprint determines the fetch AOI.
    :param config: Project config (`lunaserv_dem_srs`, `dem_native_ppd`, `dem_padding_fraction`, ...).
    :param extra_footprint_lonlat_deg: Extra corners to union into the AOI before padding, if given.
    :returns: `(radius_tif_path, deg_bbox, width, height)` -- the fetched radius GeoTIFF path plus the
        exact degree bbox/pixel dimensions requested.
    """
    # Kept for reference/comparison, no longer called by `dem_ortho.fetch_dem_and_ortho`'s default
    # path. A second, axis-aligned crosshatch artifact is baked into Lunaserv's own native DTM tile
    # itself (present regardless of requested ppd/CRS/resampling kernel; Lunaserv exposes no resampling
    # control or backing-store metadata, so it isn't fixable client-side). The live default DEM source
    # is `dem_gld100.fetch_dem_astropedia`/`dem_gld100.reproject_astropedia_elevation_to_local_grid`.
    #
    # `config.lunaserv_dem_srs` (`IAU2000:30100`) is a fixed, unparametrized CRS the server needs no
    # reprojection to serve, unlike the per-camera local Orthographic CRS (`IAU2000:30166`)
    # `dem_ortho.fetch_and_shade_ortho` requests the ortho in. Requesting this layer any finer than
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
    # `geo_utils.reproject_raster_to_local_grid` -- entirely local, so the resampling method is one
    # this project controls and picks explicitly (`resampling`, exposed as a parameter so alternatives
    # can be compared), not Lunaserv's own opaque server-side resampling. Both CRSs are expressed as
    # generic PROJ4 strings with the Moon's own spherical radius, rather than relying on GDAL/PROJ
    # recognizing Lunaserv's `IAU2000:*` codes by name.
    #
    # This removes the original near-Nyquist server-side resampling artifact, but the resampling kernel
    # used here still matters: an ~2.4x upsample (native ~237m/px to a 100m/px working grid) through a
    # smooth reconstruction kernel can itself introduce a small periodic curvature ripple at the native
    # sample spacing, invisible in the raw elevation but visible once `hillshade`'s finite-differencing
    # amplifies it. This artifact isn't fully fixable through resampling choice alone -- part of why
    # this path is deprecated in favor of `dem_gld100.fetch_dem_astropedia`.
    with rasterio.open(native_path) as src:
        native_radius = src.read(1)

    minlon, minlat, maxlon, maxlat = native_bbox_deg
    src_crs = geographic_crs(moon_radius_m)
    src_transform = transform_from_bounds(minlon, minlat, maxlon, maxlat, native_width, native_height)

    return reproject_raster_to_local_grid(
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
