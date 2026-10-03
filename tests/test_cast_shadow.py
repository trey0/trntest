import math

import numpy as np
import pyproj
import pytest

from trntest import cast_shadow, geo_utils

# A small synthetic DEM on a local Orthographic grid: 120x120 px at 100 m/px (12 km square), tangent
# point at an arbitrary mid-latitude. Small enough that the Moon's curvature barely matters, large
# enough for a wall to cast a multi-pixel shadow.
CENTER_LON_DEG, CENTER_LAT_DEG = 20.0, 45.0
SIZE_PX, CELLSIZE_M = 120, 100.0
HALF_EXTENT_M = SIZE_PX * CELLSIZE_M / 2
BBOX = (-HALF_EXTENT_M, -HALF_EXTENT_M, HALF_EXTENT_M, HALF_EXTENT_M)


def _sun_direction(azimuth_deg, elevation_deg):
    east, north, up = geo_utils.local_enu_basis(CENTER_LON_DEG, CENTER_LAT_DEG)
    az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
    return math.cos(el) * (math.sin(az) * east + math.cos(az) * north) + math.sin(el) * up


def _east_west_wall_dem(wall_rows=slice(40, 43), height_m=500.0):
    dem = np.zeros((SIZE_PX, SIZE_PX))
    dem[wall_rows, :] = height_m
    return dem


def _sweep(dem, azimuth_deg, elevation_deg, sweep=cast_shadow.sun_sweep, **kwargs):
    return sweep(dem, BBOX, CENTER_LON_DEG, CENTER_LAT_DEG, _sun_direction(azimuth_deg, elevation_deg), **kwargs)


# Behavior every sweep must share: the original binned `sun_sweep` and its replacement `horizon_sweep`.
both_sweeps = pytest.mark.parametrize("sweep", [cast_shadow.sun_sweep, cast_shadow.horizon_sweep])


def _shadow_rows(illumination, rows, cols=slice(30, 90)):
    """Rows (from `rows`, in order) whose mean illumination over `cols` is below 0.5."""
    return [r for r in rows if illumination[r, cols].mean() < 0.5]


@both_sweeps
def test_flat_dem_is_fully_lit(sweep):
    illumination = _sweep(np.zeros((SIZE_PX, SIZE_PX)), 135.0, 30.0, sweep=sweep).illumination_fraction
    assert illumination.shape == (SIZE_PX, SIZE_PX)
    assert np.all(illumination >= 0.999)


@both_sweeps
def test_wall_shadow_length_matches_height_over_tan_elevation(sweep):
    # Sun from due north at 20 deg: a 500 m east-west wall shadows ~500/tan(20)/100 = ~13.7 rows south
    # of it (rows increase southward).
    height_m, elevation_deg = 500.0, 20.0
    illumination = _sweep(_east_west_wall_dem(height_m=height_m), 0.0, elevation_deg, sweep=sweep).illumination_fraction
    expected_rows = height_m / math.tan(math.radians(elevation_deg)) / CELLSIZE_M
    shadowed = _shadow_rows(illumination, range(43, SIZE_PX))
    assert shadowed[0] <= 44
    assert len(shadowed) == pytest.approx(expected_rows, abs=2)
    assert np.all(illumination[43 + int(expected_rows) + 3 :, 30:90] > 0.99)


@both_sweeps
def test_wall_shadow_moves_to_the_other_side_with_the_sun(sweep):
    illumination = _sweep(_east_west_wall_dem(), 180.0, 20.0, sweep=sweep).illumination_fraction
    assert len(_shadow_rows(illumination, range(43, SIZE_PX))) == 0
    north_side = _shadow_rows(illumination, range(39, -1, -1))
    assert north_side and north_side[0] >= 38
    assert len(north_side) == pytest.approx(500.0 / math.tan(math.radians(20.0)) / CELLSIZE_M, abs=2)


@both_sweeps
def test_output_contract_nan_cell_stays_local(sweep):
    dem = np.zeros((SIZE_PX, SIZE_PX))
    dem[60, 70] = np.nan
    illumination = _sweep(dem, 135.0, 30.0, sweep=sweep).illumination_fraction
    assert illumination.dtype == np.float32
    assert np.isnan(illumination[60, 70])
    assert np.isnan(illumination).sum() == 1
    finite = illumination[np.isfinite(illumination)]
    assert finite.min() >= 0.0 and finite.max() <= 1.0


def test_chunked_result_is_bit_identical_to_single_chunk():
    dem = _east_west_wall_dem()
    dem[70:80, 20:30] = 300.0
    whole = _sweep(dem, 30.0, 15.0, chunk_rows=SIZE_PX)
    chunked = _sweep(dem, 30.0, 15.0, chunk_rows=7)
    np.testing.assert_array_equal(whole.illumination_fraction, chunked.illumination_fraction)
    np.testing.assert_array_equal(whole.bin_counts, chunked.bin_counts)


def test_bin_occupancy_is_aggregated_not_one_sample_per_bin():
    # The default bin size aggregates several samples per bin; see `BIN_SIZE_SAFETY_FACTOR`.
    counts = _sweep(np.zeros((SIZE_PX, SIZE_PX)), 45.0, 30.0).bin_counts
    occupied = counts[counts > 0]
    assert occupied.mean() > 3.0
    assert (occupied == 1).mean() < 0.1


def test_sweep_illuminated_gap_never_occludes():
    # Axis 0 increases toward the Sun, so the sweep runs from the last index down.
    z_max = np.array([[1.0], [np.nan], [5.0], [np.nan], [2.0]])
    lit = cast_shadow.sweep_illuminated(z_max)
    np.testing.assert_array_equal(lit[:, 0], [0.0, np.nan, 1.0, np.nan, 1.0])


def test_sweep_illuminated_equal_height_is_lit():
    lit = cast_shadow.sweep_illuminated(np.array([[3.0], [3.0], [3.0]]))
    np.testing.assert_array_equal(lit[:, 0], [1.0, 1.0, 1.0])


def test_sun_aligned_basis_is_orthonormal_right_handed_and_z_near_up():
    _, _, up = geo_utils.local_enu_basis(CENTER_LON_DEG, CENTER_LAT_DEG)
    x_hat, y_hat, z_hat = cast_shadow.sun_aligned_basis(_sun_direction(224.0, 13.0), up)
    for v in (x_hat, y_hat, z_hat):
        assert np.linalg.norm(v) == pytest.approx(1.0)
    assert np.dot(x_hat, y_hat) == pytest.approx(0.0, abs=1e-12)
    assert np.dot(x_hat, z_hat) == pytest.approx(0.0, abs=1e-12)
    assert np.dot(y_hat, z_hat) == pytest.approx(0.0, abs=1e-12)
    np.testing.assert_allclose(np.cross(x_hat, y_hat), z_hat, atol=1e-12)
    # z_hat leans away from up by exactly the sun elevation.
    assert math.degrees(math.acos(np.dot(z_hat, up))) == pytest.approx(13.0)


def test_sun_aligned_basis_overhead_sun_returns_none_and_sweep_is_all_lit():
    _, _, up = geo_utils.local_enu_basis(CENTER_LON_DEG, CENTER_LAT_DEG)
    assert cast_shadow.sun_aligned_basis(up, up) is None
    dem = _east_west_wall_dem()
    illumination = cast_shadow.illumination_fraction(dem, BBOX, CENTER_LON_DEG, CENTER_LAT_DEG, up)
    assert np.all(illumination == 1.0)


def test_fine_heights_are_registered_to_fine_pixel_centers():
    # A linear ramp in columns: away from the mirrored edges (whose influence on a cubic spline decays
    # geometrically), each fine sample must equal the ramp at its own fine pixel center,
    # `(j + 0.5) / f - 0.5` native pixels. A corner-aligned mapping (`ndimage.zoom`'s default) would
    # be off by ~0.12 native px (~1.2 m here) this far in.
    f, width, margin_px = 2, 40, 10
    dem = np.tile(np.arange(width, dtype=np.float64) * 10.0, (8, 1))
    coeffs, nan_mask = cast_shadow._spline_coefficients(dem)
    z = cast_shadow._fine_heights(coeffs, nan_mask, 2, 6, f)
    expected = ((np.arange(width * f) + 0.5) / f - 0.5) * 10.0
    interior = slice(margin_px * f, -margin_px * f)
    np.testing.assert_allclose(z[0, interior], expected[interior], atol=1e-3)


def test_dem_grid_positions_center_at_zero_elevation_is_radius_times_up():
    radius_m = 1_737_400.0
    position = geo_utils.local_grid_positions_moon_me(
        np.array(0.0), np.array(0.0), np.array(0.0), CENTER_LON_DEG, CENTER_LAT_DEG, radius_m
    )
    _, _, up = geo_utils.local_enu_basis(CENTER_LON_DEG, CENTER_LAT_DEG)
    np.testing.assert_allclose(position, radius_m * up, atol=1e-6)


@both_sweeps
def test_high_sun_sun_facing_slope_is_not_shadowed_by_itself(sweep):
    # Sun from the east at 84 deg over a 20 deg slope descending eastward (toward the Sun). Along the sun
    # ray, the slope folds back on itself (`x` decreases eastward whenever the slope exceeds
    # 90 - elevation = 6 deg), so ordering the sweep by `x` would let the slope's top shadow its own
    # sun-facing face. Nothing here can cast a shadow.
    cols = np.arange(SIZE_PX)
    ramp = -np.clip(cols - 30, 0, 60) * CELLSIZE_M * math.tan(math.radians(20.0))
    dem = np.tile(ramp, (SIZE_PX, 1))
    illumination = _sweep(dem, 90.0, 84.0, sweep=sweep).illumination_fraction
    assert np.all(illumination > 0.99)


@both_sweeps
def test_high_sun_tall_block_does_not_shadow_its_sun_side(sweep):
    # Sun from the east at 84 deg; a 2 km north-south block at columns 40-42. Its shadow falls west
    # (~2000 / tan(84) = ~210 m, ~2 px; the spline rounds the block's edges, so only the pixel next to
    # it is fully dark), never on the floor east of it, even though the block is higher than that floor.
    dem = np.zeros((SIZE_PX, SIZE_PX))
    dem[:, 40:43] = 2000.0
    illumination = _sweep(dem, 90.0, 84.0, sweep=sweep).illumination_fraction
    assert np.all(illumination[:, 43:] > 0.99)
    assert np.all(illumination[30:90, 39] < 0.5)
    assert np.all(illumination[:, :36] > 0.99)


def _realistic_frame():
    return cast_shadow.SunFrame.create(CENTER_LON_DEG, CENTER_LAT_DEG, _sun_direction(240.0, 15.5))


def _full_size_points(seed):
    rng = np.random.default_rng(seed)
    return rng.uniform(-150_000, 150_000, 500), rng.uniform(-150_000, 150_000, 500), rng.uniform(-5_000, 5_000, 500)


def test_sun_frame_closed_forms_match_proj_pipeline_both_directions():
    frame = _realistic_frame()
    transformer = pyproj.Transformer.from_pipeline(frame.proj_pipeline())
    x, y, h = _full_size_points(0)
    ours = frame.to_sun(x, y, h)
    np.testing.assert_allclose(ours, np.stack(transformer.transform(x, y, h), axis=-1), atol=1e-3)
    back = transformer.transform(ours[:, 0], ours[:, 1], ours[:, 2], direction="INVERSE")
    np.testing.assert_allclose(np.stack(frame.from_sun(ours), axis=-1), np.stack(back, axis=-1), atol=1e-3)


def test_sun_frame_from_sun_inverts_to_sun():
    frame = _realistic_frame()
    x, y, h = _full_size_points(1)
    np.testing.assert_allclose(
        np.stack(frame.from_sun(frame.to_sun(x, y, h)), axis=-1), np.stack([x, y, h], -1), atol=1e-6
    )


def test_sun_frame_is_none_for_overhead_sun_and_horizon_sweep_is_all_lit():
    _, _, up = geo_utils.local_enu_basis(CENTER_LON_DEG, CENTER_LAT_DEG)
    assert cast_shadow.SunFrame.create(CENTER_LON_DEG, CENTER_LAT_DEG, up) is None
    result = cast_shadow.horizon_sweep(_east_west_wall_dem(), BBOX, CENTER_LON_DEG, CENTER_LAT_DEG, up)
    assert np.all(result.illumination_fraction == 1.0)


def test_horizon_sweep_chunked_result_is_bit_identical_to_single_chunk():
    dem = _east_west_wall_dem()
    dem[70:80, 20:30] = 300.0
    whole = _sweep(dem, 30.0, 15.0, sweep=cast_shadow.horizon_sweep, chunk_rows=SIZE_PX)
    chunked = _sweep(dem, 30.0, 15.0, sweep=cast_shadow.horizon_sweep, chunk_rows=7)
    np.testing.assert_array_equal(whole.illumination_fraction, chunked.illumination_fraction)


def _grazing_cross_sloped_plane(azimuth_deg, along_sun_slope, cross_sun_slope):
    """A plane rising toward the Sun's azimuth (facing away from it) and tilted across it."""
    x, y = geo_utils.pixel_center_coords_m(BBOX, SIZE_PX, SIZE_PX)
    xg, yg = np.meshgrid(x, y)
    az = math.radians(azimuth_deg)
    toward_sun = xg * math.sin(az) + yg * math.cos(az)
    across_sun = xg * math.cos(az) - yg * math.sin(az)
    return along_sun_slope * toward_sun + cross_sun_slope * across_sun


def test_grazing_cross_sloped_plane_is_lit_without_screen_door():
    # Rising toward a 15.5 deg Sun at 0.2 (< tan 15.5 = 0.277) is lit everywhere: nothing on the plane
    # rises above any ray. The cross-sun tilt is what made the binned sweep's per-bin max overstate
    # occluders by ~0.1 x bin width, more than the ~0.07 x spacing real margin -- the "screen door".
    #
    # Checked away from the outer 2 px, where the spline's `mode="mirror"` folds heights back instead of
    # continuing the plane (true of both sweeps; real DEMs are fetched padded past the footprint).
    azimuth_deg, elevation_deg = 240.0, 15.5
    dem = _grazing_cross_sloped_plane(azimuth_deg, 0.2, 0.1)
    interior = (slice(2, -2), slice(2, -2))
    horizon = _sweep(dem, azimuth_deg, elevation_deg, sweep=cast_shadow.horizon_sweep).illumination_fraction
    assert np.all(horizon[interior] > 0.99)
    binned = _sweep(dem, azimuth_deg, elevation_deg).illumination_fraction
    assert (binned[interior] < 0.99).sum() > 0  # the case actually exercises the binned sweep's failure mode


def test_terrain_solve_converges_across_a_wide_curved_dem():
    # 240 km wide (curvature tilts local vertical ~4 deg at the edges) with ~5 km of relief: the
    # zero-elevation guess lands hundreds of meters off, and the solve must still converge.
    size_px, half_extent_m = 120, 120_000.0
    bbox = (-half_extent_m, -half_extent_m, half_extent_m, half_extent_m)
    rows, cols = np.mgrid[0:size_px, 0:size_px]
    dem = 2500.0 * np.sin(rows / 9.0) * np.cos(cols / 7.0)
    result = cast_shadow.horizon_sweep(dem, bbox, CENTER_LON_DEG, CENTER_LAT_DEG, _sun_direction(240.0, 15.5))
    assert result.max_terrain_residual_m < cast_shadow.TERRAIN_SOLVE_TOLERANCE_M
