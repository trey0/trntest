import math

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import transform as warp_transform

from trntest import dem_gld100
from trntest.config import MOON_RADIUS_M


def test_astropedia_coverage_bbox_deg_within_range():
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    bbox = dem_gld100.astropedia_coverage_bbox_deg(dst_bbox_m, 10.0, 5.0, MOON_RADIUS_M)
    assert len(bbox) == 4
    minlon, minlat, maxlon, maxlat = bbox
    assert minlon < 10.0 < maxlon
    assert minlat < 5.0 < maxlat


def test_astropedia_coverage_bbox_deg_raises_beyond_max_latitude():
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    with pytest.raises(ValueError, match="beyond Astropedia"):
        dem_gld100.astropedia_coverage_bbox_deg(dst_bbox_m, 10.0, 85.0, MOON_RADIUS_M)


def test_astropedia_coverage_bbox_deg_covers_dst_bbox_corners():
    """Regression test for the real corner-nodata bug this function's rewrite fixed (see
    docs/history.md's dated entry): the returned degree bbox, transformed back through the same
    local-Orthographic projection, must fully cover `dst_bbox_m`'s own corners, not just its
    center -- independently padding a degree-space bbox around the raw footprint (the old approach)
    used to undershoot them."""
    center_lon, center_lat = 10.0, 5.0
    dst_bbox_m = (-80000.0, -60000.0, 90000.0, 70000.0)  # deliberately asymmetric, not a plain square
    minlon, minlat, maxlon, maxlat = dem_gld100.astropedia_coverage_bbox_deg(
        dst_bbox_m, center_lon, center_lat, MOON_RADIUS_M
    )

    ortho_crs = f"+proj=ortho +lon_0={center_lon} +lat_0={center_lat} +R={MOON_RADIUS_M} +units=m +no_defs"
    geo_crs = f"+proj=longlat +R={MOON_RADIUS_M} +no_defs"
    minx, miny, maxx, maxy = dst_bbox_m
    lons, lats = warp_transform(ortho_crs, geo_crs, [minx, maxx, minx, maxx], [miny, miny, maxy, maxy])
    for lon, lat in zip(lons, lats, strict=True):
        assert minlon <= lon <= maxlon
        assert minlat <= lat <= maxlat


def _write_astropedia_style_tif(path, elevation_value, bbox_m, width, height, moon_radius_m):
    """Synthetic fixture matching Astropedia's real file: an Equidistant Cylindrical ("Equirectangular")
    projected CRS (lon_0=180, standard parallel 0 -- same as the real
    `Lunar_LRO_WAC_GLD100_DTM_79S79N_100m_v1.1.tif`), already-elevation values (not planetocentric
    radius), with real embedded georeferencing -- `reproject_astropedia_elevation_to_local_grid`
    trusts the file's own `crs`/`transform` directly, so the fixture needs a genuine one, unlike
    Lunaserv's GetMap responses which this project never trusted for that."""
    crs = f"+proj=eqc +lat_ts=0 +lon_0=180 +R={moon_radius_m} +units=m +no_defs"
    transform = transform_from_bounds(*bbox_m, width, height)
    data = np.full((height, width), elevation_value, dtype="int16")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="int16",
        crs=crs,
        transform=transform,
        nodata=-32768,
    ) as dst:
        dst.write(data, 1)


def _eqc_deg_bbox_for_meters_bbox(bbox_m, moon_radius_m):
    """Invert the same `+proj=eqc +lat_ts=0 +lon_0=180` forward formula (lon_0=180, standard
    parallel 0 makes it a simple linear relationship: x = R*radians(lon-180), y = R*radians(lat)) to
    get the lon/lat bbox that maps onto a chosen meters bbox in that CRS -- avoids needing `pyproj`
    directly in the test, and keeps the fixture's AOI request exactly aligned with its own data."""
    minx, miny, maxx, maxy = bbox_m
    minlon = 180.0 + math.degrees(minx / moon_radius_m)
    maxlon = 180.0 + math.degrees(maxx / moon_radius_m)
    minlat = math.degrees(miny / moon_radius_m)
    maxlat = math.degrees(maxy / moon_radius_m)
    return minlon, minlat, maxlon, maxlat


def test_reproject_astropedia_elevation_to_local_grid_preserves_constant_field(tmp_path):
    # Mirrors lunaserv_wms's test_reproject_dem_to_local_grid_preserves_constant_field, but for the
    # Astropedia-style source (Equirectangular meters CRS, real elevation already -- not radius) --
    # confirms the windowed-read + reproject path works correctly and doesn't need
    # `lunaserv_wms.radius_to_elevation`.
    moon_radius_m = 1_737_400.0
    elevation_value = 500.0
    native_bbox_m = (-350_000.0, 550_000.0, -250_000.0, 650_000.0)  # ~100km x 100km
    native_width, native_height = 64, 64
    native_path = tmp_path / "astropedia_native.tif"
    _write_astropedia_style_tif(native_path, elevation_value, native_bbox_m, native_width, native_height, moon_radius_m)

    # AOI well within the native file's coverage (a smaller, centered sub-region).
    minx, miny, maxx, maxy = native_bbox_m
    aoi_bbox_m = (
        minx + (maxx - minx) * 0.25,
        miny + (maxy - miny) * 0.25,
        maxx - (maxx - minx) * 0.25,
        maxy - (maxy - miny) * 0.25,
    )
    deg_bbox = _eqc_deg_bbox_for_meters_bbox(aoi_bbox_m, moon_radius_m)
    center_lon = 180.0 + math.degrees(((minx + maxx) / 2) / moon_radius_m)
    center_lat = math.degrees(((miny + maxy) / 2) / moon_radius_m)

    dst_bbox_m = (-5_000.0, -5_000.0, 5_000.0, 5_000.0)
    dst_width, dst_height = 32, 32
    output_path = tmp_path / "reprojected.tif"

    result_path = dem_gld100.reproject_astropedia_elevation_to_local_grid(
        native_path,
        deg_bbox,
        dst_bbox_m,
        dst_width,
        dst_height,
        center_lon,
        center_lat,
        moon_radius_m,
        output_path,
    )

    with rasterio.open(result_path) as src:
        result = src.read(1)
    assert result.shape == (dst_height, dst_width)
    assert not np.isnan(result).any()
    # Elevation preserved directly -- no planetocentric-radius offset subtracted, unlike the
    # deprecated Lunaserv path.
    assert result == pytest.approx(elevation_value, abs=1.0)
