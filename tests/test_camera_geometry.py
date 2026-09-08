from unittest.mock import patch

import numpy as np
import pytest
import spiceypy as spice

from trntest import camera
from trntest.config import TrntestConfig


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


def test_boresight_pitch_yaw_deg_nadir_pointing_is_zero():
    c_km = np.array([0.0, 0.0, 3000.0])
    boresight_me = np.array([0.0, 0.0, -1.0])  # nadir
    forward_step_km = np.array([1.0, 0.0, 0.0])
    pitch_deg, yaw_deg = camera.boresight_pitch_yaw_deg(c_km, boresight_me, forward_step_km)
    assert pitch_deg == pytest.approx(0.0, abs=1e-9)
    assert yaw_deg == pytest.approx(0.0, abs=1e-9)


def test_boresight_pitch_yaw_deg_pure_along_track_tilt():
    c_km = np.array([0.0, 0.0, 3000.0])
    nadir = np.array([0.0, 0.0, -1.0])
    along = np.array([1.0, 0.0, 0.0])
    theta_deg = 5.0
    boresight_me = np.cos(np.radians(theta_deg)) * nadir + np.sin(np.radians(theta_deg)) * along
    pitch_deg, yaw_deg = camera.boresight_pitch_yaw_deg(c_km, boresight_me, along)
    assert pitch_deg == pytest.approx(theta_deg, abs=1e-6)
    assert yaw_deg == pytest.approx(0.0, abs=1e-6)


def test_boresight_pitch_yaw_deg_pure_cross_track_tilt():
    c_km = np.array([0.0, 0.0, 3000.0])
    nadir = np.array([0.0, 0.0, -1.0])
    along = np.array([1.0, 0.0, 0.0])
    cross = np.cross(nadir, along)
    theta_deg = 5.0
    boresight_me = np.cos(np.radians(theta_deg)) * nadir + np.sin(np.radians(theta_deg)) * cross
    pitch_deg, yaw_deg = camera.boresight_pitch_yaw_deg(c_km, boresight_me, along)
    assert pitch_deg == pytest.approx(0.0, abs=1e-6)
    assert yaw_deg == pytest.approx(theta_deg, abs=1e-6)


def test_nominal_boresight_pitch_yaw_deg_mirrors_for_forward_time_k():
    pitch_deg, yaw_deg = camera.nominal_boresight_pitch_yaw_deg(camera._REVERSED_TIME_K)
    assert pitch_deg == camera.NOMINAL_BORESIGHT_PITCH_DEG
    assert yaw_deg == camera.NOMINAL_BORESIGHT_YAW_DEG
    mirrored_pitch_deg, mirrored_yaw_deg = camera.nominal_boresight_pitch_yaw_deg(camera._FORWARD_TIME_K)
    assert mirrored_pitch_deg == -camera.NOMINAL_BORESIGHT_PITCH_DEG
    assert mirrored_yaw_deg == -camera.NOMINAL_BORESIGHT_YAW_DEG


def test_lightweight_pointing_disk_distance_deg_zero_at_the_nominal_point():
    c_km = np.array([0.0, 0.0, 3000.0])
    nadir = np.array([0.0, 0.0, -1.0])
    along = np.array([1.0, 0.0, 0.0])
    cross = np.cross(nadir, along)
    pitch_rad = np.radians(camera.NOMINAL_BORESIGHT_PITCH_DEG)
    yaw_rad = np.radians(camera.NOMINAL_BORESIGHT_YAW_DEG)
    # A tangent-plane construction: boresight_pitch_yaw_deg's own arctan2 definition recovers exactly
    # (pitch_rad, yaw_rad) from this point, for any positive scale, before normalizing.
    target_boresight_me = nadir + np.tan(pitch_rad) * along + np.tan(yaw_rad) * cross
    target_boresight_me = target_boresight_me / np.linalg.norm(target_boresight_me)
    r_corrected = camera.look_at_rotation(target_boresight_me, np.eye(3))
    # Undo the lightweight correction so the *raw* pose, once lightweight_pointing_disk_distance_deg
    # re-applies it, reproduces target_boresight_me exactly.
    r_cam_to_me_raw = r_corrected @ camera._LIGHTWEIGHT_BORESIGHT_CORRECTION.T
    forward_step_km = along
    if camera.boresight_rotation_k(r_cam_to_me_raw, forward_step_km) != camera._REVERSED_TIME_K:
        forward_step_km = -along  # the state NOMINAL_BORESIGHT_PITCH_DEG/YAW_DEG were measured in

    distance_deg = camera.lightweight_pointing_disk_distance_deg(c_km, r_cam_to_me_raw, forward_step_km)
    assert distance_deg == pytest.approx(0.0, abs=1e-6)


def test_along_track_extent_km_matches_direct_ground_point_decomposition():
    moon_radius_km = 1737.4
    boresight_ground_km = np.array(spice.latrec(moon_radius_km, 0.0, 0.0))
    along_track_axis_me = np.array([0.0, 0.0, 1.0])  # local "north" at the equator, tangent to nadir
    near_ground_km = np.array(spice.latrec(moon_radius_km, 0.0, -np.radians(0.5)))
    far_ground_km = np.array(spice.latrec(moon_radius_km, 0.0, np.radians(0.5)))
    expected_near_km = -float(np.dot(near_ground_km - boresight_ground_km, along_track_axis_me))
    expected_far_km = float(np.dot(far_ground_km - boresight_ground_km, along_track_axis_me))

    def lonlat_deg(ground_km):
        _, lon, lat = spice.reclat(ground_km)
        return np.degrees(lon), np.degrees(lat)

    footprint = {
        "top_left": lonlat_deg(near_ground_km),
        "top_right": lonlat_deg(near_ground_km),
        "bottom_left": lonlat_deg(far_ground_km),
        "bottom_right": lonlat_deg(far_ground_km),
    }
    near_km, far_km = camera.along_track_extent_km(footprint, boresight_ground_km, along_track_axis_me)
    assert near_km == pytest.approx(expected_near_km)
    assert far_km == pytest.approx(expected_far_km)


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


def test_lightweight_footprint_lonlat_deg_composes_boresight_correction_and_k_twist_then_delegates():
    # Only the SPICE-touching helpers below are mocked -- no `isis_wac`/`isis_campt` call is ever
    # reached (there's no real ISIS binary or furnished kernel in this test environment; a real
    # attempt would raise/hang, not silently succeed), confirming this path is genuinely ISIS-free.
    frame_timing = camera.FrameTiming(start_time=None, sclk_start="fake", interframe_delay_s=1.0, nframes=500)
    config = TrntestConfig(image_size=1316)
    c_meters_raw = np.array([1.0, 2.0, 3.0])
    r_cam_to_me_raw = np.diag([1.0, -1.0, -1.0])
    forward_step_km = np.array([0.0, 0.0, 1.0])

    with patch.object(
        camera, "compute_n_frames_for_square_crop", return_value={"n_frames_for_square_crop": 70}
    ) as mock_n_frames:
        with patch.object(camera, "frame_et", return_value=123.0) as mock_frame_et:
            with patch.object(camera, "camera_pose_moon_me", return_value=(c_meters_raw, r_cam_to_me_raw, 0.0, 0.0)):
                with patch.object(camera, "ground_track_step_km", return_value=forward_step_km):
                    with patch.object(camera, "boresight_rotation_k", return_value=1) as mock_k:
                        with patch.object(camera, "footprint_lonlat", return_value={"center": (1.0, 2.0)}) as mock_fp:
                            result = camera.lightweight_footprint_lonlat_deg(
                                frame_timing, target_frame_index=100, config=config
                            )

    assert result == {"center": (1.0, 2.0)}
    mock_n_frames.assert_called_once_with(frame_timing, 100, config)
    # center_frame_index = target_frame_index + n_frames_for_square_crop / 2.0
    mock_frame_et.assert_called_once_with(frame_timing, 100 + 35.0)
    mock_k.assert_called_once()

    args, kwargs = mock_fp.call_args
    c_km_arg, r_cam_to_me_arg, fu_arg, fv_arg, cu_arg, cv_arg, size_arg = args
    np.testing.assert_allclose(c_km_arg, c_meters_raw / 1000.0)
    expected_r = r_cam_to_me_raw @ camera._LIGHTWEIGHT_BORESIGHT_CORRECTION @ camera.rotation_about_boresight(1)
    np.testing.assert_allclose(r_cam_to_me_arg, expected_r)
    assert fu_arg == fv_arg == camera.FIXED_FOCAL_LENGTH_PX
    assert cu_arg == config.image_size / 2.0
    assert cv_arg == config.image_size / 2.0
    assert size_arg == config.image_size
