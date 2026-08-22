import math

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import transform as warp_transform

from trntest import lunaserv
from trntest.config import MOON_RADIUS_M


def test_geographic_crs_default_radius():
    assert lunaserv.geographic_crs() == f"+proj=longlat +R={MOON_RADIUS_M} +no_defs"


def test_geographic_crs_explicit_radius():
    assert lunaserv.geographic_crs(1_000_000.0) == "+proj=longlat +R=1000000.0 +no_defs"


def test_local_orthographic_crs_default_radius():
    assert lunaserv.local_orthographic_crs(10.0, -20.0) == (
        f"+proj=ortho +lon_0=10.0 +lat_0=-20.0 +R={MOON_RADIUS_M} +units=m +no_defs"
    )


def test_local_orthographic_crs_explicit_radius():
    assert lunaserv.local_orthographic_crs(10.0, -20.0, 1_000_000.0) == (
        "+proj=ortho +lon_0=10.0 +lat_0=-20.0 +R=1000000.0 +units=m +no_defs"
    )


def test_footprint_bbox_deg_no_wraparound():
    footprint = {"a": (170.0, 40.0), "b": (175.0, 42.0), "c": (172.0, 41.0), "d": (178.0, 39.0)}
    bbox = lunaserv.footprint_bbox_deg(footprint)
    assert bbox == pytest.approx((170.0, 39.0, 178.0, 42.0))


def test_footprint_bbox_deg_antimeridian_crossing():
    # Corners straddling +-180: naive min/max would give (-179, .., 178), a ~357 deg span, instead
    # of the true ~15 deg span on the far side of the seam.
    footprint = {"a": (178.0, 65.0), "b": (-179.0, 66.0), "c": (170.0, 65.5), "d": (-175.0, 66.5)}
    minx, miny, maxx, maxy = lunaserv.footprint_bbox_deg(footprint)
    assert maxx - minx == pytest.approx(15.0)
    assert miny == pytest.approx(65.0)
    assert maxy == pytest.approx(66.5)


def test_footprint_bbox_deg_skips_none_entries():
    footprint = {"a": (170.0, 40.0), "b": None, "c": (172.0, 41.0)}
    bbox = lunaserv.footprint_bbox_deg(footprint)
    assert bbox == pytest.approx((170.0, 40.0, 172.0, 41.0))


def test_orthographic_xy_m_center_is_origin():
    x, y = lunaserv.orthographic_xy_m(30.0, -12.0, center_lon_deg=30.0, center_lat_deg=-12.0)
    assert (x, y) == pytest.approx((0.0, 0.0), abs=1e-6)


def test_orthographic_xy_m_small_offsets_match_arc_length():
    # Near the tangent point, the projection is ~locally flat -- a small angular offset along one
    # axis should map to ~radius * offset_rad along the matching axis, ~0 on the other.
    radius_m = 1_737_400.0
    x_lon, y_lon = lunaserv.orthographic_xy_m(1.0, 0.0, center_lon_deg=0.0, center_lat_deg=0.0, radius_m=radius_m)
    assert x_lon == pytest.approx(radius_m * math.radians(1.0), rel=1e-4)
    assert y_lon == pytest.approx(0.0, abs=1.0)

    x_lat, y_lat = lunaserv.orthographic_xy_m(0.0, 1.0, center_lon_deg=0.0, center_lat_deg=0.0, radius_m=radius_m)
    assert y_lat == pytest.approx(radius_m * math.radians(1.0), rel=1e-4)
    assert x_lat == pytest.approx(0.0, abs=1.0)


def test_footprint_bbox_local_m_symmetric_footprint():
    radius_m = 1_737_400.0
    center_lon, center_lat = 10.0, 5.0
    footprint = {
        "center": (center_lon, center_lat),
        "top_left": (center_lon - 0.1, center_lat + 0.1),
        "top_right": (center_lon + 0.1, center_lat + 0.1),
        "bottom_left": (center_lon - 0.1, center_lat - 0.1),
        "bottom_right": (center_lon + 0.1, center_lat - 0.1),
    }
    minx, miny, maxx, maxy = lunaserv.footprint_bbox_local_m(footprint, center_lon, center_lat, radius_m)
    # Roughly symmetric around the origin (center) for a footprint symmetric in lon/lat.
    assert minx == pytest.approx(-maxx, rel=1e-3)
    assert miny == pytest.approx(-maxy, rel=1e-3)
    assert maxx > 0
    assert maxy > 0


def test_footprint_bbox_local_m_skips_none_entries():
    footprint = {"a": (10.0, 5.0), "b": None, "c": (10.2, 5.2)}
    bbox = lunaserv.footprint_bbox_local_m(footprint, center_lon_deg=10.0, center_lat_deg=5.0)
    assert len(bbox) == 4


def test_pixel_dims_for_gsd_isotropic_for_square_bbox():
    # Unlike the old lon/lat-degree version, a square meter bbox should give equal width/height --
    # no cos(lat) correction needed since the local Orthographic CRS is already isotropic.
    bbox = (-10_000.0, -10_000.0, 10_000.0, 10_000.0)
    width_px, height_px = lunaserv.pixel_dims_for_gsd(bbox, target_gsd_m=100.0)
    assert width_px == height_px == 200


def test_camera_local_enu_m_directly_overhead_tangent_point():
    radius_m = 1_737_400.0
    altitude_m = 100_000.0
    lon0_deg, lat0_deg = 30.0, 0.0
    lon0, lat0 = math.radians(lon0_deg), math.radians(lat0_deg)
    camera_moon_me_m = [
        (radius_m + altitude_m) * math.cos(lat0) * math.cos(lon0),
        (radius_m + altitude_m) * math.cos(lat0) * math.sin(lon0),
        (radius_m + altitude_m) * math.sin(lat0),
    ]
    east, north, up = lunaserv._camera_local_enu_m(camera_moon_me_m, lon0_deg, lat0_deg, radius_m)
    assert (east, north) == pytest.approx((0.0, 0.0), abs=1e-6)
    assert up == pytest.approx(altitude_m, rel=1e-9)


def test_camera_local_enu_m_matches_orthographic_xy_for_on_sphere_point():
    # A point *on* the sphere (zero altitude) at a nearby (lon, lat) should land at the same
    # (east, north) `orthographic_xy_m` already computes for it (both are the same tangent-plane
    # projection, just derived two different ways -- see `_camera_local_enu_m`'s docstring), with
    # up ~0 (small, curvature-only 2nd-order error for a nearby point).
    radius_m = 1_737_400.0
    center_lon_deg, center_lat_deg = 10.0, 5.0
    point_lon_deg, point_lat_deg = 10.2, 5.1
    point_moon_me_m = [
        radius_m * math.cos(math.radians(point_lat_deg)) * math.cos(math.radians(point_lon_deg)),
        radius_m * math.cos(math.radians(point_lat_deg)) * math.sin(math.radians(point_lon_deg)),
        radius_m * math.sin(math.radians(point_lat_deg)),
    ]
    east, north, up = lunaserv._camera_local_enu_m(point_moon_me_m, center_lon_deg, center_lat_deg, radius_m)
    expected_x, expected_y = lunaserv.orthographic_xy_m(
        point_lon_deg, point_lat_deg, center_lon_deg, center_lat_deg, radius_m
    )
    assert (east, north) == pytest.approx((expected_x, expected_y), rel=1e-4)
    # Curvature (sagitta) term, ~radius * angle_rad**2 / 2 for a ~0.22 deg separation -- ~13m here,
    # not exactly 0 (the point is *on* the sphere, but the tangent *plane* dips below it away from
    # the tangent point itself).
    assert up == pytest.approx(0.0, abs=20.0)


def test_terrain_photometric_angles_flat_dem_directly_below_camera():
    # Flat terrain, camera directly overhead the grid's own center (odd grid size so a pixel center
    # lands exactly at x=y=0): at that exact tangent point the normal-tilt correction (unconditional
    # since Phase 72, see `_terrain_photometric_angles`'s own docstring) contributes exactly zero --
    # `sphere_sag` is an even function of (x, y), so its gradient at the origin is exactly 0 both
    # analytically and under `np.gradient`'s central-difference scheme -- so incidence there is still
    # exactly (90 - elevation_deg), the view direction is exactly straight up too, so emission should
    # be ~0 and phase should coincide with incidence. Only checked at the center pixel, not
    # "everywhere" -- away from the tangent point, incidence now genuinely varies with position (real
    # physics, not something to isolate away); see
    # test_terrain_photometric_angles_normal_tilt_correction_matches_closed_form_true_tilt for that
    # effect's own dedicated validation.
    width = height = 11
    bbox = (-100.0, -100.0, 100.0, 100.0)
    dem = np.zeros((height, width))
    altitude_m = 1_000.0
    camera_local_enu_m = np.array([0.0, 0.0, altitude_m])
    azimuth_deg, elevation_deg = 90.0, 30.0

    incidence_deg, emission_deg, phase_deg = lunaserv._terrain_photometric_angles(
        dem,
        bbox,
        camera_local_enu_m,
        azimuth_deg,
        elevation_deg,
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
    camera_local_enu_m = np.array([0.0, 0.0, altitude_m])

    _, emission_deg, _ = lunaserv._terrain_photometric_angles(
        dem,
        bbox,
        camera_local_enu_m,
        azimuth_deg=0.0,
        elevation_deg=45.0,
        cellsize_m=200.0 / width,
        radius_m=1_737_400.0,
    )

    row = height // 2
    col = width - 1
    x = minx + (col + 0.5) * (maxx - minx) / width
    expected_emission_deg = math.degrees(math.atan(abs(x) / altitude_m))
    # abs, not a tight rel: at the real Moon's radius, this grid's ~91m offset has two small, real
    # deviations from the exact flat-ground formula -- the ground-position sagitta correction
    # (~1.2e-5 deg) and, since Phase 72, the always-on normal-tilt correction's own contribution at
    # this same small offset -- see test_terrain_photometric_angles_curvature_correction_...` below
    # for the ground-position correction validated at a scale where it actually matters, and
    # test_terrain_photometric_angles_normal_tilt_correction_matches_closed_form_true_tilt for the
    # normal-tilt correction's own dedicated large-offset validation. `abs=5e-3` covers both real,
    # tiny effects at this offset without masking a real bug (observed live: ~2.7e-3 deg combined).
    assert emission_deg[row, col] == pytest.approx(expected_emission_deg, abs=5e-3)


def test_terrain_photometric_angles_curvature_correction_reduces_emission_at_large_offset():
    # At DEM-pixel scale the sagitta correction is negligible (see the abs-tolerance note on
    # `test_terrain_photometric_angles_emission_grows_with_offset_from_nadir` above) -- but real
    # candidate footprints are tens of km wide (`docs/reproject-fov-investigation.md`: a real,
    # live-validated 143.1x142.6km footprint), where it isn't. Flat terrain, offset purely along x
    # (y=0), so the true local vertical also tilts purely in the x-z plane (same symmetry argument as
    # `_terrain_photometric_angles`'s own docstring) -- the closed form below accounts for both the
    # ground-position sagitta correction *and* the always-on (since Phase 72) normal-tilt correction,
    # not just the former, since neither is optional any more.
    radius_m = 1_737_400.0
    altitude_m = 68_500.0  # a real value used elsewhere in this project (docs/history.md)
    width = height = 21
    half_extent_m = 100_000.0
    bbox = (-half_extent_m, -half_extent_m, half_extent_m, half_extent_m)
    dem = np.zeros((height, width))
    camera_local_enu_m = np.array([0.0, 0.0, altitude_m])

    _, emission_deg, _ = lunaserv._terrain_photometric_angles(
        dem,
        bbox,
        camera_local_enu_m,
        azimuth_deg=0.0,
        elevation_deg=45.0,
        cellsize_m=2 * half_extent_m / width,
        radius_m=radius_m,
    )

    row = height // 2
    col = width - 1
    x = -half_extent_m + (col + 0.5) * (2 * half_extent_m) / width

    naive_flat_ground_emission_deg = math.degrees(math.atan(abs(x) / altitude_m))

    # Independently-derived closed form (not a call into lunaserv): true angular offset theta (sin
    # theta = x / radius_m), the true local vertical tilted by theta toward the offset ([sin theta, 0,
    # cos theta] in local ENU, see `_terrain_photometric_angles`'s own docstring), and the ground
    # point's exact position (sagitta term, unchanged from before).
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
    # purpose, the ground-position sagitta correction, doesn't depend on grid resolution at all, only
    # the now-also-present normal-tilt correction does). Observed live: ~0.157 deg -- consistent with
    # (if smaller than) a naive quadratic truncation-error scaling from the finer test's own residual,
    # not a sign of a derivation bug. See the dedicated test above for a tight, resolution-controlled
    # check of the normal-tilt correction specifically.
    assert emission_deg[row, col] == pytest.approx(true_emission_deg, abs=0.2)


def test_terrain_photometric_angles_normal_tilt_correction_matches_closed_form_true_tilt():
    # Phase 70/72's normal-tilt fix (unconditional, no opt-out parameter): `normal`'s gradient input
    # uses `dem + sphere_sag`, not raw `dem` -- so even with perfectly flat terrain, the *normal* tilts by
    # the sphere's own curvature away from the tangent point, not just `ground`'s position (which the
    # test above already covers). This isolates that normal-tilt effect specifically, and checks it
    # against a closed-form true-tilt angle (not a second call into `lunaserv`), independent of the
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
    camera_local_enu_m = np.array([0.0, 0.0, altitude_m])

    incidence_deg, _, _ = lunaserv._terrain_photometric_angles(
        dem,
        bbox,
        camera_local_enu_m,
        azimuth_deg=0.0,
        elevation_deg=elevation_deg,
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


def test_local_enu_direction_pure_radial_vector_is_pure_up():
    # A vector pointing exactly along the tangent point's own radial direction (no East/North
    # component at all) should land entirely on the "Up" axis in the local frame, regardless of its
    # magnitude -- a direct check of `_local_enu_direction`'s basis vectors alone (no tangent-point
    # subtraction involved, unlike `_camera_local_enu_m`).
    lon0_deg, lat0_deg = 12.0, -34.0
    lon0, lat0 = math.radians(lon0_deg), math.radians(lat0_deg)
    magnitude = 1.6  # km/s-scale, but this function is unit-agnostic
    radial = magnitude * np.array([math.cos(lat0) * math.cos(lon0), math.cos(lat0) * math.sin(lon0), math.sin(lat0)])
    east, north, up = lunaserv._local_enu_direction(radial, lon0_deg, lat0_deg)
    assert (east, north) == pytest.approx((0.0, 0.0), abs=1e-9)
    assert up == pytest.approx(magnitude, rel=1e-9)


def test_terrain_photometric_angles_along_track_correction_removes_along_track_component():
    # Flat terrain again: a camera offset with both an along-track and cross-track component should,
    # once corrected, behave exactly as if only the cross-track component existed -- the along-track
    # correction's whole point. Checked at the grid's own center pixel (x=y=0, the tangent point),
    # where the always-on normal-tilt correction contributes exactly zero by symmetry (see
    # test_terrain_photometric_angles_flat_dem_directly_below_camera's own comment) -- a clean
    # isolation without needing to account for it here.
    width = height = 11
    bbox = (-100.0, -100.0, 100.0, 100.0)
    dem = np.zeros((height, width))
    # Camera 100m east, 200m north, 1000m up from the pixel directly below (grid center).
    camera_local_enu_m = np.array([100.0, 200.0, 1000.0])
    along_track_local_enu = np.array([0.0, 1.0, 0.0])  # due north -- an arbitrary nonzero magnitude is fine too

    _, emission_deg, _ = lunaserv._terrain_photometric_angles(
        dem,
        bbox,
        camera_local_enu_m,
        azimuth_deg=0.0,
        elevation_deg=45.0,
        cellsize_m=200.0 / width,
        radius_m=1_737_400.0,
        along_track_local_enu=along_track_local_enu,
    )

    center = height // 2, width // 2
    # North offset is exactly along the along-track direction, so it's fully removed -- what's left is exactly as if
    # the camera had been at (100, 0, 1000) instead (pure cross-track + altitude). abs, not rel=1e-6, for the same
    # real-Moon-radius sagitta reason as `test_terrain_photometric_angles_emission_grows_with_offset_from_nadir`.
    expected_emission_deg = math.degrees(math.atan(100.0 / 1000.0))
    assert emission_deg[center] == pytest.approx(expected_emission_deg, abs=1e-4)


def _write_native_radius_tif(path, radius_value, native_bbox_deg, width, height):
    minlon, minlat, maxlon, maxlat = native_bbox_deg
    transform = transform_from_bounds(minlon, minlat, maxlon, maxlat, width, height)
    data = np.full((height, width), radius_value, dtype="float32")
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=1, dtype="float32", transform=transform
    ) as dst:
        dst.write(data, 1)


def test_reproject_dem_to_local_grid_shape_matches_destination(tmp_path):
    moon_radius_m = 1_737_400.0
    center_lon, center_lat = 10.0, 5.0
    native_width, native_height = 64, 64
    native_bbox_deg = (center_lon - 1.0, center_lat - 1.0, center_lon + 1.0, center_lat + 1.0)
    native_path = tmp_path / "native.tif"
    _write_native_radius_tif(native_path, moon_radius_m, native_bbox_deg, native_width, native_height)

    dst_bbox_m = (-5_000.0, -5_000.0, 5_000.0, 5_000.0)
    dst_width, dst_height = 32, 32
    output_path = tmp_path / "reprojected.tif"

    result_path = lunaserv.reproject_dem_to_local_grid(
        native_path,
        native_bbox_deg,
        native_width,
        native_height,
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


def test_reproject_dem_to_local_grid_preserves_constant_field(tmp_path):
    # A uniform native "radius" input (no real terrain variation) should stay ~uniform after
    # reprojection -- the projection math itself shouldn't introduce an artificial gradient/artifact
    # for trivial input, and the destination grid (a small AOI well within the native bbox's
    # coverage) should be fully populated, no nodata gaps.
    moon_radius_m = 1_737_400.0
    center_lon, center_lat = 10.0, 5.0
    native_width, native_height = 64, 64
    native_bbox_deg = (center_lon - 1.0, center_lat - 1.0, center_lon + 1.0, center_lat + 1.0)
    native_path = tmp_path / "native.tif"
    _write_native_radius_tif(native_path, moon_radius_m, native_bbox_deg, native_width, native_height)

    dst_bbox_m = (-5_000.0, -5_000.0, 5_000.0, 5_000.0)
    dst_width, dst_height = 32, 32
    output_path = tmp_path / "reprojected.tif"

    result_path = lunaserv.reproject_dem_to_local_grid(
        native_path,
        native_bbox_deg,
        native_width,
        native_height,
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
    assert not np.isnan(result).any()
    assert result == pytest.approx(moon_radius_m, rel=1e-4)


def test_astropedia_coverage_bbox_deg_within_range():
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    bbox = lunaserv.astropedia_coverage_bbox_deg(dst_bbox_m, 10.0, 5.0, MOON_RADIUS_M)
    assert len(bbox) == 4
    minlon, minlat, maxlon, maxlat = bbox
    assert minlon < 10.0 < maxlon
    assert minlat < 5.0 < maxlat


def test_astropedia_coverage_bbox_deg_raises_beyond_max_latitude():
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    with pytest.raises(ValueError, match="beyond Astropedia"):
        lunaserv.astropedia_coverage_bbox_deg(dst_bbox_m, 10.0, 85.0, MOON_RADIUS_M)


def test_astropedia_coverage_bbox_deg_covers_dst_bbox_corners():
    """Regression test for the real corner-nodata bug this function's rewrite fixed (see
    docs/history.md's dated entry): the returned degree bbox, transformed back through the same
    local-Orthographic projection, must fully cover `dst_bbox_m`'s own corners, not just its
    center -- independently padding a degree-space bbox around the raw footprint (the old approach)
    used to undershoot them."""
    center_lon, center_lat = 10.0, 5.0
    dst_bbox_m = (-80000.0, -60000.0, 90000.0, 70000.0)  # deliberately asymmetric, not a plain square
    minlon, minlat, maxlon, maxlat = lunaserv.astropedia_coverage_bbox_deg(
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
    # Mirrors test_reproject_dem_to_local_grid_preserves_constant_field, but for the Astropedia-style
    # source (Equirectangular meters CRS, real elevation already -- not radius) -- confirms the
    # windowed-read + reproject path works correctly and doesn't need `radius_to_elevation`.
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

    result_path = lunaserv.reproject_astropedia_elevation_to_local_grid(
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


def test_despeckle_replaces_isolated_spike():
    data = np.full((20, 20), 100, dtype=np.uint8)
    data[10, 10] = 250
    cleaned = lunaserv.despeckle(data)
    assert cleaned[10, 10] == 100


def test_despeckle_leaves_smooth_constant_region_untouched():
    data = np.full((20, 20), 100, dtype=np.uint8)
    data[10, 10] = 250
    cleaned = lunaserv.despeckle(data)
    # everywhere but the spike's own 3x3 neighborhood is unaffected
    mask = np.ones_like(data, dtype=bool)
    mask[9:12, 9:12] = False
    assert np.array_equal(cleaned[mask], data[mask])


def test_despeckle_leaves_smooth_gradient_untouched():
    # a real gradient has no isolated single-pixel deviations -- shouldn't false-positive anywhere
    data = np.linspace(0, 255, 20 * 20, dtype=np.uint8).reshape(20, 20)
    cleaned = lunaserv.despeckle(data)
    assert np.array_equal(cleaned, data)


def test_despeckle_leaves_large_blob_interior_untouched():
    # simulates a real saturated-crater feature: a large uniform region, not an isolated pixel
    data = np.full((20, 20), 50, dtype=np.uint8)
    data[5:15, 5:15] = 255
    cleaned = lunaserv.despeckle(data)
    interior = cleaned[8:12, 8:12]
    assert np.all(interior == 255)


def test_ortho_shaded_filename_no_hapke_ignores_other_flags():
    assert lunaserv.ortho_shaded_filename(False) == "ortho_shaded.tif"
    no_hapke = lunaserv.ortho_shaded_filename(False, along_track_correction=True, real_hapke_params=True)
    assert no_hapke == "ortho_shaded.tif"


def test_ortho_shaded_filename_matches_todays_defaults():
    # All-defaults call must resolve to exactly the file `fetch_dem_and_ortho`'s own defaults would
    # produce. `_normaltilt` is a permanent, unconditional part of this filename since Phase 72 (no
    # parameter controls it any more -- see `ortho_shaded_filename`'s own docstring for why it's kept
    # anyway), so this is deliberately not the pre-Phase-70 filename (see
    # `test_ortho_shaded_filename_real_params_false_matches_pre_phase_69` below for that backward-
    # compat guarantee, which still applies to `real_hapke_params` specifically).
    assert lunaserv.ortho_shaded_filename(True) == "ortho_shaded_hapke_atc_realparams_normaltilt.tif"


def test_ortho_shaded_filename_real_params_false_matches_pre_phase_69():
    # Backward-compat check: existing cached files from before `real_hapke_params` existed (when
    # `hapke`/`along_track_correction` were the only toggles) must still resolve to the same name
    # under an explicit `real_hapke_params=False` -- plus the now-permanent `_normaltilt` suffix
    # (Phase 72), which every `hapke=True` filename gets regardless of any parameter now.
    assert lunaserv.ortho_shaded_filename(True, real_hapke_params=False) == "ortho_shaded_hapke_atc_normaltilt.tif"
    assert lunaserv.ortho_shaded_filename(True, along_track_correction=False, real_hapke_params=False) == (
        "ortho_shaded_hapke_normaltilt.tif"
    )


def test_ortho_shaded_filename_real_params_suffix():
    assert lunaserv.ortho_shaded_filename(True, along_track_correction=True, real_hapke_params=True) == (
        "ortho_shaded_hapke_atc_realparams_normaltilt.tif"
    )
    assert lunaserv.ortho_shaded_filename(True, along_track_correction=False, real_hapke_params=True) == (
        "ortho_shaded_hapke_realparams_normaltilt.tif"
    )


def test_ortho_shaded_filename_normaltilt_suffix_always_present_when_hapke_true():
    # No parameter controls this any more (Phase 72, see `ortho_shaded_filename`'s own docstring) --
    # `_normaltilt` is simply always appended whenever `hapke=True`, even with every other flag off.
    assert lunaserv.ortho_shaded_filename(True, along_track_correction=False, real_hapke_params=False) == (
        "ortho_shaded_hapke_normaltilt.tif"
    )


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
    n_params = len(lunaserv._HAPKE_CALIBRATION_PARAM_ORDER)
    n_bands = len(wavelengths_nm) * n_params
    profile = dict(driver="GTiff", height=height, width=width, count=n_bands, dtype="float32", crs=crs)
    with rasterio.open(path, "w", transform=transform_, **profile) as dst:
        for band in range(1, n_bands + 1):
            wavelength_index, param_index = divmod(band - 1, n_params)
            dst.write(np.full((height, width), wavelength_index * 100 + param_index, dtype="float32"), band)


def test_sample_hapke_calibration_reads_the_right_band_and_pixel(tmp_path):
    # Must cover all 7 real wavelengths -- `_sample_hapke_calibration`'s band offset is computed
    # against the real, full `_HAPKE_CALIBRATION_WAVELENGTHS_NM` layout, not whatever's in the file.
    path = tmp_path / "fake_hapke_calibration.tif"
    _write_fake_hapke_calibration_cube(
        path, lunaserv._HAPKE_CALIBRATION_WAVELENGTHS_NM, bbox_deg=(9.0, 4.0, 11.0, 6.0), width=4, height=4
    )

    params = lunaserv._sample_hapke_calibration(path, center_lon_deg=10.1, center_lat_deg=5.1, wavelength_nm=643)

    # 643nm's wavelength_index in the real layout -> encoded value = wavelength_index * 100 + param_index
    wavelength_index = lunaserv._HAPKE_CALIBRATION_WAVELENGTHS_NM.index(643)
    expected = {name: wavelength_index * 100.0 + i for i, name in enumerate(lunaserv._HAPKE_CALIBRATION_PARAM_ORDER)}
    assert params == pytest.approx(expected)


def test_sample_hapke_calibration_different_wavelength_reads_a_different_band_block(tmp_path):
    path = tmp_path / "fake_hapke_calibration.tif"
    _write_fake_hapke_calibration_cube(
        path, lunaserv._HAPKE_CALIBRATION_WAVELENGTHS_NM, bbox_deg=(9.0, 4.0, 11.0, 6.0), width=4, height=4
    )

    params_321 = lunaserv._sample_hapke_calibration(path, center_lon_deg=10.1, center_lat_deg=5.1, wavelength_nm=321)

    wavelength_index = lunaserv._HAPKE_CALIBRATION_WAVELENGTHS_NM.index(321)
    expected = {name: wavelength_index * 100.0 + i for i, name in enumerate(lunaserv._HAPKE_CALIBRATION_PARAM_ORDER)}
    assert params_321 == pytest.approx(expected)


def test_sample_hapke_calibration_rejects_a_wavelength_not_in_the_cube(tmp_path):
    path = tmp_path / "fake_hapke_calibration.tif"
    _write_fake_hapke_calibration_cube(path, wavelengths_nm=(643,), bbox_deg=(9.0, 4.0, 11.0, 6.0), width=4, height=4)

    with pytest.raises(ValueError, match="wavelength_nm"):
        lunaserv._sample_hapke_calibration(path, center_lon_deg=10.0, center_lat_deg=5.0, wavelength_nm=999)
