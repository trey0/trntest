import numpy as np
import pytest

from trntest import camera


def test_rotation_about_boresight_identity():
    r = camera.rotation_about_boresight(0)
    np.testing.assert_allclose(r, np.eye(3), atol=1e-12)


def test_rotation_about_boresight_90deg_preserves_z():
    r = camera.rotation_about_boresight(1)
    z = np.array([0.0, 0.0, 1.0])
    np.testing.assert_allclose(r @ z, z, atol=1e-12)
    # x -> y for a proper +90deg rotation about z
    np.testing.assert_allclose(r @ np.array([1.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]), atol=1e-12)


def test_look_at_rotation_boresight_matches_target_exactly():
    reference = np.eye(3)
    target = np.array([1.0, 2.0, 3.0])
    r = camera.look_at_rotation(target, reference)
    np.testing.assert_allclose(r[:, 2], target / np.linalg.norm(target), atol=1e-12)


def test_look_at_rotation_is_a_valid_rotation():
    reference = camera.rotation_about_boresight(1)  # some nontrivial reference frame
    target = np.array([0.2, -0.5, 0.9])
    r = camera.look_at_rotation(target, reference)
    np.testing.assert_allclose(r.T @ r, np.eye(3), atol=1e-10)
    assert np.linalg.det(r) == pytest.approx(1.0, abs=1e-10)


def test_look_at_rotation_close_to_reference_boresight_is_near_identity_change():
    # Re-aiming at (nearly) the reference's own boresight should barely change the X/Y axes.
    reference = np.eye(3)
    target = np.array([1e-6, 1e-6, 1.0])
    r = camera.look_at_rotation(target, reference)
    np.testing.assert_allclose(r[:, 0], reference[:, 0], atol=1e-4)


def test_off_nadir_and_slant_range_nadir_pointing():
    c_km = np.array([0.0, 0.0, 3000.0])
    boresight_me = np.array([0.0, 0.0, -1.0])
    off_nadir_deg, slant_range_km = camera.off_nadir_and_slant_range(c_km, boresight_me)
    assert off_nadir_deg == pytest.approx(0.0, abs=1e-9)
    assert slant_range_km == pytest.approx(3000.0 - 1737.4)


def test_ray_sphere_intersect_range_hits_sphere():
    origin = np.array([0.0, 0.0, 3000.0])
    direction = np.array([0.0, 0.0, -1.0])
    t = camera.ray_sphere_intersect_range(origin, direction, moon_radius_km=1737.4)
    assert t == pytest.approx(3000.0 - 1737.4)


def test_ray_sphere_intersect_range_misses_sphere_returns_none():
    origin = np.array([0.0, 5000.0, 3000.0])
    direction = np.array([0.0, 1.0, 0.0])
    assert camera.ray_sphere_intersect_range(origin, direction, moon_radius_km=1737.4) is None


def test_ground_chord_km():
    p1 = np.array([0.0, 0.0, 0.0])
    p2 = np.array([3.0, 4.0, 0.0])
    assert camera.ground_chord_km(p1, p2) == pytest.approx(5.0)


def test_pixel_ray_cam_center_pixel_points_along_boresight():
    ray = camera.pixel_ray_cam(128, 128, fu=200.0, fv=200.0, cu=128, cv=128)
    np.testing.assert_allclose(ray, np.array([0.0, 0.0, 1.0]), atol=1e-12)


def test_cross_track_width_km_nadir_pointing():
    c_km = np.array([0.0, 0.0, 3000.0])
    r_cam_to_me = np.diag([1.0, -1.0, -1.0])  # boresight toward -Z (nadir), see test above
    half_angle_rad = np.radians(10.0)
    width = camera.cross_track_width_km(c_km, r_cam_to_me, half_angle_rad)
    assert width > 0


def test_footprint_lonlat_center_is_nadir():
    c_km = np.array([0.0, 0.0, 3000.0])
    # Camera sits above the north pole; this rotation points the camera-Z boresight toward -Z in
    # the ME frame, i.e. straight down at nadir.
    r_cam_to_me = np.diag([1.0, -1.0, -1.0])
    size = 256
    half_angle_rad = np.radians(10.0)
    fu = fv = (size / 2.0) / np.tan(half_angle_rad)
    cu = cv = size / 2.0
    footprint = camera.footprint_lonlat(c_km, r_cam_to_me, fu, fv, cu, cv, size)
    lon, lat = footprint["center"]
    assert lon == pytest.approx(0.0, abs=1e-6)
    assert lat == pytest.approx(90.0, abs=1e-6)
