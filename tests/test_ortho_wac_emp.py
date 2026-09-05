import math

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds as transform_from_bounds

from trntest import ortho_wac_emp
from trntest.config import MOON_RADIUS_M


def test_wac_emp_tile_id_for_bbox_resolves_known_northern_tile():
    # Real, confirmed tile (docs/data-sources.md): 90-180E, 0-60N -- center (135, 30).
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    tile_id = ortho_wac_emp.wac_emp_tile_id_for_bbox(dst_bbox_m, 135.0, 30.0, MOON_RADIUS_M)
    assert tile_id == "WAC_EMP_643NM_E300N1350_304P"


def test_wac_emp_tile_id_for_bbox_resolves_southern_hemisphere():
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    tile_id = ortho_wac_emp.wac_emp_tile_id_for_bbox(dst_bbox_m, 135.0, -30.0, MOON_RADIUS_M)
    assert tile_id == "WAC_EMP_643NM_E300S1350_304P"


def test_wac_emp_tile_id_for_bbox_honors_wavelength_and_ppd():
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    tile_id = ortho_wac_emp.wac_emp_tile_id_for_bbox(dst_bbox_m, 45.0, 30.0, MOON_RADIUS_M, wavelength_nm=321, ppd=64)
    assert tile_id == "WAC_EMP_321NM_E300N0450_064P"


def test_wac_emp_tile_id_for_bbox_rejects_unknown_wavelength():
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    with pytest.raises(ValueError, match="wavelength_nm"):
        ortho_wac_emp.wac_emp_tile_id_for_bbox(dst_bbox_m, 135.0, 30.0, MOON_RADIUS_M, wavelength_nm=500)


def test_wac_emp_tile_id_for_bbox_raises_beyond_max_latitude():
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    with pytest.raises(ValueError, match="beyond WAC_EMP"):
        ortho_wac_emp.wac_emp_tile_id_for_bbox(dst_bbox_m, 135.0, 85.0, MOON_RADIUS_M)


def test_wac_emp_tile_id_for_bbox_raises_when_straddling_equator():
    # A footprint centered right at the equator, tall enough that its padded AOI spans both
    # hemispheres -- no single equirect tile covers it.
    dst_bbox_m = (-50000.0, -300000.0, 50000.0, 300000.0)
    with pytest.raises(ValueError, match="straddles the equator"):
        ortho_wac_emp.wac_emp_tile_id_for_bbox(dst_bbox_m, 135.0, 0.0, MOON_RADIUS_M)


def test_wac_emp_tile_id_for_bbox_raises_when_straddling_lon_zone_boundary():
    # A footprint centered right at a 90-deg lon zone boundary, wide enough that its padded AOI spans
    # two lon zones -- no single equirect tile covers it.
    dst_bbox_m = (-300000.0, -50000.0, 300000.0, 50000.0)
    with pytest.raises(ValueError, match="straddles a WAC_EMP tile"):
        ortho_wac_emp.wac_emp_tile_id_for_bbox(dst_bbox_m, 90.0, 30.0, MOON_RADIUS_M)


def _write_wac_emp_style_tif(path, reflectance_value, bbox_m, width, height, moon_radius_m):
    """Synthetic fixture matching WAC_EMP's real file: an Equidistant Cylindrical ("Equirectangular")
    projected CRS with real embedded georeferencing (like Astropedia's own fixture -- see
    `test_dem_gld100._write_astropedia_style_tif`), but float32 reflectance values (no int16
    planetocentric-radius convention) and no `nodata` set (WAC_EMP's own real missing-data sentinel is
    a set of specific non-finite float32 bit patterns this project doesn't need to special-case for a
    simple, fully-valid synthetic AOI)."""
    crs = f"+proj=eqc +lat_ts=0 +lon_0=180 +R={moon_radius_m} +units=m +no_defs"
    transform_ = transform_from_bounds(*bbox_m, width, height)
    data = np.full((height, width), reflectance_value, dtype="float32")
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=1, dtype="float32", crs=crs, transform=transform_
    ) as dst:
        dst.write(data, 1)


def test_reproject_wac_emp_reflectance_to_local_grid_preserves_constant_field(tmp_path):
    # Mirrors test_dem_gld100's test_reproject_astropedia_elevation_to_local_grid_preserves_constant_field,
    # but for a WAC_EMP-style source (real reflectance values, no radius-to-elevation conversion
    # applicable). Native fixture is deliberately much larger than the 10km destination AOI (unlike a
    # real WAC_EMP tile, which spans a whole 60x90-deg quadrant against a typical few-hundred-km
    # footprint AOI, this test's own native/dst size ratio must still be generous enough that the read
    # window's own fractional-source-pixel rounding at its edge -- `window_from_bounds`'s float window
    # vs. `src.read`'s integer-rounded actual read shape, the same mechanism
    # `reproject_astropedia_elevation_to_local_grid` relies on -- doesn't itself clip into the
    # destination AOI's real coverage.
    moon_radius_m = 1_737_400.0
    reflectance_value = 0.08
    native_bbox_m = (-500_000.0, 400_000.0, 500_000.0, 1_400_000.0)  # ~1000km x 1000km
    native_width, native_height = 64, 64
    native_path = tmp_path / "wac_emp_native.tif"
    _write_wac_emp_style_tif(native_path, reflectance_value, native_bbox_m, native_width, native_height, moon_radius_m)

    minx, miny, maxx, maxy = native_bbox_m
    center_lon = 180.0 + math.degrees(((minx + maxx) / 2) / moon_radius_m)
    center_lat = math.degrees(((miny + maxy) / 2) / moon_radius_m)

    dst_bbox_m = (-5_000.0, -5_000.0, 5_000.0, 5_000.0)
    dst_width, dst_height = 32, 32
    output_path = tmp_path / "reprojected.tif"

    result_path = ortho_wac_emp.reproject_wac_emp_reflectance_to_local_grid(
        native_path, dst_bbox_m, dst_width, dst_height, center_lon, center_lat, moon_radius_m, output_path
    )

    with rasterio.open(result_path) as src:
        result = src.read(1)
    assert result.shape == (dst_height, dst_width)
    assert not np.isnan(result).any()
    assert result == pytest.approx(reflectance_value, abs=1e-4)
