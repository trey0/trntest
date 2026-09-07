import math

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import transform as warp_transform

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


def test_wac_emp_tile_id_for_bbox_resolves_polar_north():
    # A footprint fully north of the equirect grid's own +-60 deg coverage resolves to the
    # polar-stereographic tile pair instead of raising (confirmed real, fetchable tile -- see
    # docs/data-sources/wac-emp-pds4.md's polar-tile bullet).
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    tile_id = ortho_wac_emp.wac_emp_tile_id_for_bbox(dst_bbox_m, 45.0, 75.0, MOON_RADIUS_M)
    assert tile_id == "WAC_EMP_643NM_P900N0000_304P"


def test_wac_emp_tile_id_for_bbox_resolves_polar_south():
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    tile_id = ortho_wac_emp.wac_emp_tile_id_for_bbox(dst_bbox_m, 45.0, -75.0, MOON_RADIUS_M)
    assert tile_id == "WAC_EMP_643NM_P900S0000_304P"


def test_wac_emp_tile_id_for_bbox_rejects_non_default_band_for_polar():
    # The polar tile pair only exists at one wavelength/ppd combination (confirmed via the archive's
    # own listing) -- a footprint that needs polar coverage but requests a different one can't be
    # silently served a wrong tile.
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    with pytest.raises(ValueError, match="archive only offers"):
        ortho_wac_emp.wac_emp_tile_id_for_bbox(dst_bbox_m, 45.0, 75.0, MOON_RADIUS_M, wavelength_nm=321, ppd=64)


def test_wac_emp_tile_id_for_bbox_raises_when_straddling_equirect_polar_boundary():
    # A footprint whose padded AOI spans from well inside the equirect grid's own +-60 deg coverage to
    # well beyond it -- no single tile (equirect or polar) covers it, and this project doesn't mosaic
    # across that boundary either, matching its existing equator/lon-zone-boundary stance.
    dst_bbox_m = (-50000.0, -200000.0, 50000.0, 200000.0)  # +-200km height, well past +-60 at center 60
    with pytest.raises(ValueError, match="doesn't mosaic across it"):
        ortho_wac_emp.wac_emp_tile_id_for_bbox(dst_bbox_m, 45.0, 60.0, MOON_RADIUS_M)


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


def _write_wac_emp_antimeridian_style_tif(path, reflectance_value, lon_min_deg, lon_max_deg, height, moon_radius_m):
    """Fixture matching the *real* WAC_EMP tile's own PROJCS convention, unlike
    `_write_wac_emp_style_tif`'s `lon_0=180`-shifted one: `central_meridian=0`/`false_easting=0` (as
    confirmed live on a real "E300*2250" tile), with the file's own transform origin placed directly
    in the unwrapped, continuous longitude domain past +-180 deg that the real 180-270/270-360 deg
    zone tiles actually use -- `lon_0=180` sidesteps the antimeridian branch-cut bug this regresses,
    since it never asks PROJ to reproject a point whose *destination*-CRS longitude representation
    disagrees in sign from the *source* file's own stored domain."""
    crs = f"+proj=eqc +lat_ts=0 +lon_0=0 +R={moon_radius_m} +units=m +no_defs"
    x_min, x_max = moon_radius_m * math.radians(lon_min_deg), moon_radius_m * math.radians(lon_max_deg)
    width = height  # square fixture is enough to exercise the bug
    y_half = (x_max - x_min) / 2
    transform_ = transform_from_bounds(x_min, -y_half, x_max, y_half, width, height)
    data = np.full((height, width), reflectance_value, dtype="float32")
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=1, dtype="float32", crs=crs, transform=transform_
    ) as dst:
        dst.write(data, 1)


def test_reproject_wac_emp_reflectance_to_local_grid_handles_zone_past_antimeridian(tmp_path):
    # Regression test for a real bug (docs/proposed-tasks/open-items.md): `M1314068239CE` (physical
    # longitude ~200 deg, reported by SPICE in the signed -180..180 convention as -160.04 deg) failed
    # hillshade/report generation against the real "WAC_EMP_643NM_E300S2250_304P" tile (180-270 deg
    # zone) with `CPLE_AppDefinedError: Invalid dataset dimensions : 0 x N`. Root cause: this
    # function's `transform_bounds` call normalizes longitude into (-180, 180] before applying the
    # tile's `central_meridian=0` linear formula, but the real tile's own georeferencing is written in
    # unwrapped, continuous longitude (this zone's raster spans x in [R*pi, R*1.5pi], never negative)
    # -- landing the AOI window a full sphere circumference away from the tile's actual raster.
    moon_radius_m = 1_737_400.0
    reflectance_value = 0.08
    native_path = tmp_path / "wac_emp_native_past_antimeridian.tif"
    # A narrow native span (~30km) at 64px keeps resolution generous relative to the 10km destination
    # AOI -- see `test_reproject_wac_emp_reflectance_to_local_grid_preserves_constant_field`'s own
    # docstring for why a too-coarse native/dst ratio can spuriously clip the read window's own edges.
    _write_wac_emp_antimeridian_style_tif(native_path, reflectance_value, 199.5, 200.5, 64, moon_radius_m)

    center_lon, center_lat = -160.0, 0.0  # SPICE-style signed convention for physical longitude 200 deg
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


def _write_wac_emp_polar_style_tif(
    path, reflectance_value, center_lon_deg, center_lat_deg, half_width_m, size, moon_radius_m
):
    """Fixture matching the real WAC_EMP polar tile's own projection family -- Polar Stereographic,
    not Equirectangular (confirmed live via `gdalinfo` on the real `P900N0000` tile: EPSG method 9810,
    `lat_0=90`/`lon_0=0`). Used to prove `reproject_wac_emp_reflectance_to_local_grid`'s
    antimeridian-branch-cut fix stays correctly gated off (`is_equirect`) for this projection family --
    it has no such branch cut (longitude enters through smooth sin/cos terms, not a raw linear
    multiply), so "correcting" it the same way would corrupt otherwise-valid data instead of fixing
    anything."""
    crs = f"+proj=stere +lat_0=90 +lat_ts=90 +lon_0=0 +R={moon_radius_m} +units=m +no_defs"
    geo_crs = f"+proj=longlat +R={moon_radius_m} +no_defs"
    (cx,), (cy,) = warp_transform(geo_crs, crs, [center_lon_deg], [center_lat_deg])
    bbox_m = (cx - half_width_m, cy - half_width_m, cx + half_width_m, cy + half_width_m)
    transform_ = transform_from_bounds(*bbox_m, size, size)
    data = np.full((size, size), reflectance_value, dtype="float32")
    with rasterio.open(
        path, "w", driver="GTiff", height=size, width=size, count=1, dtype="float32", crs=crs, transform=transform_
    ) as dst:
        dst.write(data, 1)


def test_reproject_wac_emp_reflectance_to_local_grid_handles_polar_stereographic_source(tmp_path):
    # A footprint near the pole resolves to the real polar-stereographic tile family (see
    # test_wac_emp_tile_id_for_bbox_resolves_polar_north/south) -- this confirms
    # reproject_wac_emp_reflectance_to_local_grid's generic warp still produces correct, non-NaN
    # output for that different projection family, and specifically that the antimeridian-branch-cut
    # fix (which only makes sense for the equirect tiles' `central_meridian=0` formula) doesn't fire
    # for it and corrupt otherwise-good data.
    moon_radius_m = 1_737_400.0
    reflectance_value = 0.08
    center_lon, center_lat = 45.0, 80.0
    native_path = tmp_path / "wac_emp_native_polar.tif"
    _write_wac_emp_polar_style_tif(
        native_path,
        reflectance_value,
        center_lon,
        center_lat,
        half_width_m=30_000.0,
        size=120,
        moon_radius_m=moon_radius_m,
    )

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
