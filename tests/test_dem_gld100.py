import math

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds as transform_from_bounds

from trntest import dem_gld100
from trntest.config import MOON_RADIUS_M


def test_check_astropedia_coverage_accepts_aoi_within_range():
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    dem_gld100.check_astropedia_coverage(dst_bbox_m, 10.0, 5.0, MOON_RADIUS_M)


def test_check_astropedia_coverage_raises_beyond_max_latitude():
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    with pytest.raises(ValueError, match="beyond Astropedia"):
        dem_gld100.check_astropedia_coverage(dst_bbox_m, 10.0, 85.0, MOON_RADIUS_M)


def test_check_astropedia_coverage_checks_the_aoi_edge_not_its_center():
    # A 100 km square reaches ~1.7 deg north of its center: past 79 deg from 78.2, not from 77.
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    dem_gld100.check_astropedia_coverage(dst_bbox_m, 10.0, 77.0, MOON_RADIUS_M)
    with pytest.raises(ValueError, match="beyond Astropedia"):
        dem_gld100.check_astropedia_coverage(dst_bbox_m, 10.0, 78.2, MOON_RADIUS_M)


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

    # AOI well within the native file's coverage, centered on it.
    minx, miny, maxx, maxy = native_bbox_m
    center_lon = 180.0 + math.degrees(((minx + maxx) / 2) / moon_radius_m)
    center_lat = math.degrees(((miny + maxy) / 2) / moon_radius_m)

    # Deliberately asymmetric, not a plain square: every corner must come back covered.
    dst_bbox_m = (-8_000.0, -6_000.0, 9_000.0, 7_000.0)
    dst_width, dst_height = 34, 26
    output_path = tmp_path / "reprojected.tif"

    result_path = dem_gld100.reproject_astropedia_elevation_to_local_grid(
        native_path,
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


def test_reproject_astropedia_elevation_to_local_grid_wraps_across_raster_edge(tmp_path):
    # Like the real file: a global raster centered on 180 deg, so its edges meet at 0 deg, a bit
    # wider than the circumference. An AOI straddling 0 deg needs both edges; the value is a smooth
    # function of longitude, so a misplaced or missing piece shows.
    moon_radius_m = 1_737_400.0
    pixel_m = 2_000.0
    width = math.ceil(2 * math.pi * moon_radius_m / pixel_m) + 1
    height = 200
    left = -width * pixel_m / 2 + 30.0
    native_bbox_m = (left, -height * pixel_m / 2, left + width * pixel_m, height * pixel_m / 2)
    xs = left + (np.arange(width) + 0.5) * pixel_m
    lon = 180.0 + np.degrees(xs / moon_radius_m)
    values = (1000.0 * np.sin(np.radians(lon))).astype("int16")
    native_path = tmp_path / "global.tif"
    _write_astropedia_style_tif(native_path, 0, native_bbox_m, width, height, moon_radius_m)
    with rasterio.open(native_path, "r+") as dst:
        dst.write(np.tile(values, (height, 1)), 1)

    dst_bbox_m = (-100_000.0, -100_000.0, 100_000.0, 100_000.0)
    output_path = tmp_path / "reprojected.tif"
    dem_gld100.reproject_astropedia_elevation_to_local_grid(
        native_path, dst_bbox_m, 50, 50, 0.0, 0.0, moon_radius_m, output_path
    )
    with rasterio.open(output_path) as src:
        result = src.read(1)
    assert not np.isnan(result).any()
    # Columns run west to east across 0 deg: sin(lon) goes from negative to positive.
    row = result[25]
    assert row[0] < -40 and row[-1] > 40
    assert np.all(np.diff(row) > 0)


def test_reproject_astropedia_elevation_to_local_grid_registers_to_subpixel(tmp_path):
    # A ramp in x, read through windows whose offsets land at arbitrary fractions of a source pixel:
    # each output pixel must match the ramp at its own position, not one shifted by that fraction.
    moon_radius_m = 1_737_400.0
    pixel_m = 1_000.0
    width, height = 400, 400
    native_bbox_m = (-200_000.0, 500_000.0, 200_000.0, 900_000.0)
    native_path = tmp_path / "ramp.tif"
    _write_astropedia_style_tif(native_path, 0, native_bbox_m, width, height, moon_radius_m)
    with rasterio.open(native_path, "r+") as dst:
        dst.write(np.tile((np.arange(width) * 10).astype("int16"), (height, 1)), 1)

    for center_x_m in (1_230.0, 7_770.0, -15_500.0):
        center_lon = 180.0 + math.degrees(center_x_m / moon_radius_m)
        center_lat = math.degrees(700_000.0 / moon_radius_m)
        output_path = tmp_path / "reprojected.tif"
        dem_gld100.reproject_astropedia_elevation_to_local_grid(
            native_path, (-500.0, -500.0, 500.0, 500.0), 1, 1, center_lon, center_lat, moon_radius_m, output_path
        )
        with rasterio.open(output_path) as src:
            value = float(src.read(1)[0, 0])
        expected = ((center_x_m - native_bbox_m[0]) / pixel_m - 0.5) * 10
        assert value == pytest.approx(expected, abs=0.5)
