import math

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds as transform_from_bounds

from trntest import hapke


# The 5 tests below exercise `_terrain_photometric_angles` directly with synthetic, non-real-Moon-
# location geometry (a flat/offset-camera setup, not tied to any actual candidate) -- since Phase 77
# (docs/history.md's dated entry) that function is fully MOON_ME-native and needs a real tangent point
# just to build the local Orthographic CRS it inverts DEM points through, even for a synthetic test.
# These fix an arbitrary tangent point, `(lon, lat) = (0.0, 0.0)`, where MOON_ME's own axes happen to
# be a simple permutation of the old local (East, North, Up) frame these tests used to work in
# directly (`_local_enu_basis(0.0, 0.0)`: up=(1,0,0), east=(0,1,0), north=(0,0,1)) -- so each test's
# own inputs translate mechanically via the two helpers below, and every test's own *expected-value*
# closed-form math (the actual thing each test checks) is unchanged from before this refactor.
def _moon_me_position_at_tangent_point_00(east_m=0.0, north_m=0.0, up_m=0.0, radius_m=1_737_400.0):
    return np.array([radius_m + up_m, east_m, north_m])


def _moon_me_direction_at_tangent_point_00(east=0.0, north=0.0, up=0.0):
    return np.array([up, east, north])


def _sun_direction_moon_me_at_tangent_point_00(azimuth_deg, elevation_deg):
    # Same local-ENU sun-vector formula `_terrain_photometric_angles` used to build internally before
    # Phase 77 (now built once by `real_geometry_photometric_angles` via
    # `_moon_me_direction_from_local_enu` -- see its own test below), reused here so these direct
    # unit tests can keep expressing the sun in the same readable azimuth/elevation terms.
    az_rad, el_rad = math.radians(90.0 - azimuth_deg), math.radians(elevation_deg)
    east = math.cos(az_rad) * math.cos(el_rad)
    north = math.sin(az_rad) * math.cos(el_rad)
    up = math.sin(el_rad)
    return _moon_me_direction_at_tangent_point_00(east, north, up)


def test_moon_me_direction_from_local_enu_pure_up_returns_the_tangent_points_own_up_axis():
    # The inverse rotation of the old (deleted) `_local_enu_direction`: a local-frame vector with only
    # an "Up" component, rotated into MOON_ME, should land exactly on the tangent point's own real
    # radial direction (`_local_enu_basis`'s own `up`) -- a direct check of the rotation alone (no
    # tangent-point *position* subtraction involved, since this is a direction, not a position).
    lon0_deg, lat0_deg = 12.0, -34.0
    magnitude = 1.6  # km/s-scale, but this function is unit-agnostic
    moon_me = hapke._moon_me_direction_from_local_enu([0.0, 0.0, magnitude], lon0_deg, lat0_deg)
    _, _, up = hapke._local_enu_basis(lon0_deg, lat0_deg)
    assert moon_me == pytest.approx(magnitude * up, rel=1e-9)


def test_terrain_photometric_angles_flat_dem_directly_below_camera():
    # Flat terrain, camera directly overhead the grid's own center (odd grid size so a pixel center
    # lands exactly at x=y=0): at that exact tangent point the normal-tilt effect (unconditional since
    # Phase 72, part of the exact MOON_ME embedding since Phase 77 -- see
    # `_terrain_photometric_angles`'s own docstring) contributes exactly zero by symmetry -- so
    # incidence there is still exactly (90 - elevation_deg), the view direction is exactly straight up
    # too, so emission should be ~0 and phase should coincide with incidence. Only checked at the
    # center pixel, not "everywhere" -- away from the tangent point, incidence now genuinely varies
    # with position (real physics, not something to isolate away); see
    # test_terrain_photometric_angles_normal_tilt_correction_matches_closed_form_true_tilt for that
    # effect's own dedicated validation.
    width = height = 11
    bbox = (-100.0, -100.0, 100.0, 100.0)
    dem = np.zeros((height, width))
    altitude_m = 1_000.0
    center_lon_deg, center_lat_deg = 0.0, 0.0
    camera_center_moon_me_m = _moon_me_position_at_tangent_point_00(up_m=altitude_m)
    azimuth_deg, elevation_deg = 90.0, 30.0
    sun_direction_moon_me = _sun_direction_moon_me_at_tangent_point_00(azimuth_deg, elevation_deg)

    incidence_deg, emission_deg, phase_deg = hapke._terrain_photometric_angles(
        dem,
        bbox,
        center_lon_deg,
        center_lat_deg,
        camera_center_moon_me_m,
        sun_direction_moon_me,
        cellsize_m=200.0 / width,
        radius_m=1_737_400.0,
    )

    center = height // 2, width // 2
    assert incidence_deg[center] == pytest.approx(90.0 - elevation_deg, abs=1e-6)
    assert emission_deg[center] == pytest.approx(0.0, abs=1e-6)
    assert phase_deg[center] == pytest.approx(90.0 - elevation_deg, abs=1e-6)


def test_terrain_photometric_angles_emission_grows_with_offset_from_nadir():
    # Off-center pixel, still flat terrain: emission from a finite-altitude camera directly
    # overhead the grid's own center should match the exact flat-ground formula
    # atan(horizontal_distance / altitude) -- confirms real parallax (not just a nadir
    # approximation) drives emission here.
    width = height = 11
    minx, miny, maxx, maxy = bbox = (-100.0, -100.0, 100.0, 100.0)
    dem = np.zeros((height, width))
    altitude_m = 1_000.0
    center_lon_deg, center_lat_deg = 0.0, 0.0
    camera_center_moon_me_m = _moon_me_position_at_tangent_point_00(up_m=altitude_m)
    sun_direction_moon_me = _sun_direction_moon_me_at_tangent_point_00(azimuth_deg=0.0, elevation_deg=45.0)

    _, emission_deg, _ = hapke._terrain_photometric_angles(
        dem,
        bbox,
        center_lon_deg,
        center_lat_deg,
        camera_center_moon_me_m,
        sun_direction_moon_me,
        cellsize_m=200.0 / width,
        radius_m=1_737_400.0,
    )

    row = height // 2
    col = width - 1
    x = minx + (col + 0.5) * (maxx - minx) / width
    expected_emission_deg = math.degrees(math.atan(abs(x) / altitude_m))
    # abs, not a tight rel: at the real Moon's radius, this grid's ~91m offset has two small, real
    # deviations from the exact flat-ground formula -- the ground-position sagitta effect
    # (~1.2e-5 deg) and, since Phase 72, the always-on normal-tilt effect's own contribution at
    # this same small offset -- see test_terrain_photometric_angles_curvature_correction_...` below
    # for the ground-position effect validated at a scale where it actually matters, and
    # test_terrain_photometric_angles_normal_tilt_correction_matches_closed_form_true_tilt for the
    # normal-tilt effect's own dedicated large-offset validation. `abs=5e-3` covers both real,
    # tiny effects at this offset without masking a real bug (observed live: ~2.7e-3 deg combined).
    assert emission_deg[row, col] == pytest.approx(expected_emission_deg, abs=5e-3)


def test_terrain_photometric_angles_curvature_correction_reduces_emission_at_large_offset():
    # At DEM-pixel scale the sagitta effect is negligible (see the abs-tolerance note on
    # `test_terrain_photometric_angles_emission_grows_with_offset_from_nadir` above) -- but real
    # candidate footprints are tens of km wide (`docs/reproject-fov-investigation.md`: a real,
    # live-validated 143.1x142.6km footprint), where it isn't. Flat terrain, offset purely along x
    # (y=0), so the true local vertical also tilts purely in the x-z plane (same symmetry argument as
    # `_terrain_photometric_angles`'s own docstring) -- the closed form below accounts for both the
    # ground-position sagitta effect *and* the always-on (since Phase 72) normal-tilt effect, not
    # just the former, since neither is optional any more.
    radius_m = 1_737_400.0
    altitude_m = 68_500.0  # a real value used elsewhere in this project (docs/history.md)
    width = height = 21
    half_extent_m = 100_000.0
    bbox = (-half_extent_m, -half_extent_m, half_extent_m, half_extent_m)
    dem = np.zeros((height, width))
    center_lon_deg, center_lat_deg = 0.0, 0.0
    camera_local_enu_m = (0.0, 0.0, altitude_m)  # local-ENU, only used below for the independent closed form
    camera_center_moon_me_m = _moon_me_position_at_tangent_point_00(up_m=altitude_m, radius_m=radius_m)
    sun_direction_moon_me = _sun_direction_moon_me_at_tangent_point_00(azimuth_deg=0.0, elevation_deg=45.0)

    _, emission_deg, _ = hapke._terrain_photometric_angles(
        dem,
        bbox,
        center_lon_deg,
        center_lat_deg,
        camera_center_moon_me_m,
        sun_direction_moon_me,
        cellsize_m=2 * half_extent_m / width,
        radius_m=radius_m,
    )

    row = height // 2
    col = width - 1
    x = -half_extent_m + (col + 0.5) * (2 * half_extent_m) / width

    naive_flat_ground_emission_deg = math.degrees(math.atan(abs(x) / altitude_m))

    # Independently-derived closed form (not a call into hapke, and in local-ENU terms -- an
    # arbitrary, equally-valid choice of Cartesian frame for this self-contained computation, unrelated
    # to how the function under test now does its own): true angular offset theta (sin theta = x /
    # radius_m), the true local vertical tilted by theta toward the offset ([sin theta, 0, cos theta]
    # in local ENU, see `_terrain_photometric_angles`'s own docstring), and the ground point's exact
    # position (sagitta term, unchanged from before).
    theta = math.asin(x / radius_m)
    true_normal = (math.sin(theta), 0.0, math.cos(theta))
    sag = math.sqrt(radius_m**2 - x**2) - radius_m
    ground = (x, 0.0, sag)
    view_vec = tuple(c - g for c, g in zip(camera_local_enu_m, ground, strict=True))
    view_norm = math.sqrt(sum(v**2 for v in view_vec))
    view_dir = tuple(v / view_norm for v in view_vec)
    true_emission_deg = math.degrees(math.acos(sum(n * v for n, v in zip(true_normal, view_dir, strict=True))))

    # The correction is real and non-negligible at this scale (a real candidate's own edge)...
    assert abs(naive_flat_ground_emission_deg - true_emission_deg) > 0.1
    # ...and the function's actual output matches the curvature-and-tilt-aware value, not the old
    # flat one. `abs`, not `rel`: `np.gradient`'s own discretization error is real and larger here
    # than the dedicated normal-tilt closed-form test's own ~0.016 deg residual, since this test's
    # grid is deliberately much coarser (~9.5km/px here vs. ~1km/px there -- this test's original
    # purpose, the ground-position sagitta effect, doesn't depend on grid resolution at all, only
    # the now-also-present normal-tilt effect does). Observed live: ~0.157 deg -- consistent with
    # (if smaller than) a naive quadratic truncation-error scaling from the finer test's own residual,
    # not a sign of a derivation bug. See the dedicated test above for a tight, resolution-controlled
    # check of the normal-tilt effect specifically.
    assert emission_deg[row, col] == pytest.approx(true_emission_deg, abs=0.2)


def test_terrain_photometric_angles_normal_tilt_correction_matches_closed_form_true_tilt():
    # Phase 70/72's normal-tilt fix: even with perfectly flat terrain, the *normal* tilts by the
    # sphere's own curvature away from the tangent point, not just `ground`'s position (which the
    # test above already covers). This isolates that normal-tilt effect specifically, and checks it
    # against a closed-form true-tilt angle (not a second call into `hapke`), independent of the
    # docstring's own already-cited synthetic-sphere validation number.
    #
    # Test point due north of the tangent point (x=0, y>0), sun also due north (`azimuth_deg=0.0`):
    # this keeps the true surface normal, the sun direction, and the tangent point's own "Up" axis all
    # coplanar, so the effect reduces to simple angle subtraction instead of a general 3D dot product.
    # At true angular offset theta from the tangent point, the true local vertical is known (see
    # `_terrain_photometric_angles`'s own docstring) to tilt by exactly theta toward the point, i.e.
    # toward the sun here -- so incidence should drop from the flat-DEM value of `90 - elevation_deg`
    # to `90 - elevation_deg - theta_deg`.
    radius_m = 1_737_400.0
    altitude_m = 68_500.0
    elevation_deg = 45.0
    width = height = 201
    half_extent_m = 100_000.0
    bbox = (-half_extent_m, -half_extent_m, half_extent_m, half_extent_m)
    dem = np.zeros((height, width))
    center_lon_deg, center_lat_deg = 0.0, 0.0
    camera_center_moon_me_m = _moon_me_position_at_tangent_point_00(up_m=altitude_m, radius_m=radius_m)
    sun_direction_moon_me = _sun_direction_moon_me_at_tangent_point_00(azimuth_deg=0.0, elevation_deg=elevation_deg)

    incidence_deg, _, _ = hapke._terrain_photometric_angles(
        dem,
        bbox,
        center_lon_deg,
        center_lat_deg,
        camera_center_moon_me_m,
        sun_direction_moon_me,
        cellsize_m=2 * half_extent_m / width,
        radius_m=radius_m,
    )

    row = 0  # north edge (`y_centers` is built north-to-top, see the function's own construction)
    col = width // 2  # x == 0 exactly for odd `width`, keeping the geometry coplanar
    y = half_extent_m - (0 + 0.5) * (2 * half_extent_m) / height

    theta_deg = math.degrees(math.asin(y / radius_m))
    expected_incidence_deg = (90.0 - elevation_deg) - theta_deg

    # The tilt is real and non-negligible at this offset...
    assert theta_deg > 1.0
    # ...and the function's actual output matches the closed-form tilted-normal value, not the old
    # flat-normal `90 - elevation_deg`. `abs`, not `rel`: `np.gradient`'s central-difference
    # approximation carries a real, small discretization error at this grid's ~1km/px spacing (the
    # docstring's own cited ~0.0017 deg residual is at a finer, ~100m/px production resolution) --
    # observed residual here is ~0.016 deg, so 0.02 gives a small margin without masking a real bug.
    assert incidence_deg[row, col] == pytest.approx(expected_incidence_deg, abs=0.02)


def test_terrain_photometric_angles_along_track_correction_removes_along_track_component():
    # Flat terrain again: a camera offset with both an along-track and cross-track component should,
    # once corrected, behave exactly as if only the cross-track component existed -- the along-track
    # correction's whole point. Checked at the grid's own center pixel (x=y=0, the tangent point),
    # where the normal-tilt effect contributes exactly zero by symmetry (see
    # test_terrain_photometric_angles_flat_dem_directly_below_camera's own comment) -- a clean
    # isolation without needing to account for it here.
    width = height = 11
    bbox = (-100.0, -100.0, 100.0, 100.0)
    dem = np.zeros((height, width))
    center_lon_deg, center_lat_deg = 0.0, 0.0
    # Camera 100m east, 200m north, 1000m up from the pixel directly below (grid center).
    camera_center_moon_me_m = _moon_me_position_at_tangent_point_00(east_m=100.0, north_m=200.0, up_m=1000.0)
    # Due north -- an arbitrary nonzero magnitude is fine too.
    along_track_direction_moon_me = _moon_me_direction_at_tangent_point_00(north=1.0)
    sun_direction_moon_me = _sun_direction_moon_me_at_tangent_point_00(azimuth_deg=0.0, elevation_deg=45.0)

    _, emission_deg, _ = hapke._terrain_photometric_angles(
        dem,
        bbox,
        center_lon_deg,
        center_lat_deg,
        camera_center_moon_me_m,
        sun_direction_moon_me,
        cellsize_m=200.0 / width,
        radius_m=1_737_400.0,
        along_track_direction_moon_me=along_track_direction_moon_me,
    )

    center = height // 2, width // 2
    # North offset is exactly along the along-track direction, so it's fully removed -- what's left is exactly as if
    # the camera had been at (100, 0, 1000) instead (pure cross-track + altitude). abs, not rel=1e-6, for the same
    # real-Moon-radius sagitta reason as `test_terrain_photometric_angles_emission_grows_with_offset_from_nadir`.
    expected_emission_deg = math.degrees(math.atan(100.0 / 1000.0))
    assert emission_deg[center] == pytest.approx(expected_emission_deg, abs=1e-4)


def test_stretch_reflectance_to_uint8_maps_range_endpoints():
    reflectance = np.array([[hapke.DISPLAY_STRETCH_REFLECTANCE_MIN, hapke.DISPLAY_STRETCH_REFLECTANCE_MAX]])
    result = hapke.stretch_reflectance_to_uint8(reflectance)
    assert result[0, 0] == 0
    assert result[0, 1] == 255


def test_stretch_reflectance_to_uint8_clips_outside_range():
    reflectance = np.array([[-1.0, 1000.0]])
    result = hapke.stretch_reflectance_to_uint8(reflectance)
    assert result[0, 0] == 0
    assert result[0, 1] == 255


def test_despeckle_replaces_isolated_spike():
    data = np.full((20, 20), 100, dtype=np.uint8)
    data[10, 10] = 250
    cleaned = hapke.despeckle(data)
    assert cleaned[10, 10] == 100


def test_despeckle_leaves_smooth_constant_region_untouched():
    data = np.full((20, 20), 100, dtype=np.uint8)
    data[10, 10] = 250
    cleaned = hapke.despeckle(data)
    # everywhere but the spike's own 3x3 neighborhood is unaffected
    mask = np.ones_like(data, dtype=bool)
    mask[9:12, 9:12] = False
    assert np.array_equal(cleaned[mask], data[mask])


def test_despeckle_leaves_smooth_gradient_untouched():
    # a real gradient has no isolated single-pixel deviations -- shouldn't false-positive anywhere
    data = np.linspace(0, 255, 20 * 20, dtype=np.uint8).reshape(20, 20)
    cleaned = hapke.despeckle(data)
    assert np.array_equal(cleaned, data)


def test_despeckle_leaves_large_blob_interior_untouched():
    # simulates a real saturated-crater feature: a large uniform region, not an isolated pixel
    data = np.full((20, 20), 50, dtype=np.uint8)
    data[5:15, 5:15] = 255
    cleaned = hapke.despeckle(data)
    interior = cleaned[8:12, 8:12]
    assert np.all(interior == 255)


def _write_fake_hapke_calibration_cube(path, wavelengths_nm, bbox_deg, width, height):
    """A tiny synthetic multi-band GeoTIFF in the real calibration cube's own CRS/band-layout
    convention (Equirectangular, 9 params per wavelength band, `_HAPKE_CALIBRATION_PARAM_ORDER`
    order) -- each band's constant value encodes `wavelength_index * 100 + param_index`, so reading
    the wrong band/pixel is easy to catch."""
    minlon, minlat, maxlon, maxlat = bbox_deg
    crs = "+proj=eqc +lat_ts=0 +lat_0=0 +lon_0=0 +x_0=0 +y_0=0 +R=1737400 +units=m +no_defs"
    r = 1_737_400.0
    minx, maxx = math.radians(minlon) * r, math.radians(maxlon) * r
    miny, maxy = math.radians(minlat) * r, math.radians(maxlat) * r
    transform_ = transform_from_bounds(minx, miny, maxx, maxy, width, height)
    n_params = len(hapke._HAPKE_CALIBRATION_PARAM_ORDER)
    n_bands = len(wavelengths_nm) * n_params
    profile = dict(driver="GTiff", height=height, width=width, count=n_bands, dtype="float32", crs=crs)
    with rasterio.open(path, "w", transform=transform_, **profile) as dst:
        for band in range(1, n_bands + 1):
            wavelength_index, param_index = divmod(band - 1, n_params)
            dst.write(np.full((height, width), wavelength_index * 100 + param_index, dtype="float32"), band)


def test_sample_hapke_calibration_reads_the_right_band_and_pixel(tmp_path):
    # Must cover all 7 real wavelengths -- `_sample_hapke_calibration`'s band offset is computed
    # against the real, full `HAPKE_CALIBRATION_WAVELENGTHS_NM` layout, not whatever's in the file.
    path = tmp_path / "fake_hapke_calibration.tif"
    _write_fake_hapke_calibration_cube(
        path, hapke.HAPKE_CALIBRATION_WAVELENGTHS_NM, bbox_deg=(9.0, 4.0, 11.0, 6.0), width=4, height=4
    )

    params = hapke._sample_hapke_calibration(path, center_lon_deg=10.1, center_lat_deg=5.1, wavelength_nm=643)

    # 643nm's wavelength_index in the real layout -> encoded value = wavelength_index * 100 + param_index
    wavelength_index = hapke.HAPKE_CALIBRATION_WAVELENGTHS_NM.index(643)
    expected = {name: wavelength_index * 100.0 + i for i, name in enumerate(hapke._HAPKE_CALIBRATION_PARAM_ORDER)}
    assert params == pytest.approx(expected)


def test_sample_hapke_calibration_different_wavelength_reads_a_different_band_block(tmp_path):
    path = tmp_path / "fake_hapke_calibration.tif"
    _write_fake_hapke_calibration_cube(
        path, hapke.HAPKE_CALIBRATION_WAVELENGTHS_NM, bbox_deg=(9.0, 4.0, 11.0, 6.0), width=4, height=4
    )

    params_321 = hapke._sample_hapke_calibration(path, center_lon_deg=10.1, center_lat_deg=5.1, wavelength_nm=321)

    wavelength_index = hapke.HAPKE_CALIBRATION_WAVELENGTHS_NM.index(321)
    expected = {name: wavelength_index * 100.0 + i for i, name in enumerate(hapke._HAPKE_CALIBRATION_PARAM_ORDER)}
    assert params_321 == pytest.approx(expected)


def test_sample_hapke_calibration_rejects_a_wavelength_not_in_the_cube(tmp_path):
    path = tmp_path / "fake_hapke_calibration.tif"
    _write_fake_hapke_calibration_cube(path, wavelengths_nm=(643,), bbox_deg=(9.0, 4.0, 11.0, 6.0), width=4, height=4)

    with pytest.raises(ValueError, match="wavelength_nm"):
        hapke._sample_hapke_calibration(path, center_lon_deg=10.0, center_lat_deg=5.0, wavelength_nm=999)
