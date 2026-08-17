import numpy as np
import pytest

from trntest import wac_camera_model


def test_distort_is_a_small_near_identity_perturbation_for_small_inputs():
    # Near the boresight (small ux/uy), distortion should be a small correction, not a wild jump --
    # a basic sanity check on the iteration's stability, not a substitute for the real validation
    # against campt (see docs/wac-jigsaw-investigation.md for that).
    dx, dy = wac_camera_model._distort(0.01, -0.02)
    assert dx == pytest.approx(0.01, abs=1e-4)
    assert dy == pytest.approx(-0.02, abs=1e-4)


def test_distort_inverts_the_real_undistort_formula():
    # _distort (undistorted -> distorted, iterative) should genuinely invert
    # LroWideAngleCameraDistortionMap::SetFocalPlane's real closed-form formula (distorted ->
    # undistorted), not just be numerically stable. Apply the real formula directly here (not
    # exposed by the module, since ground-to-image only needs the iterative direction) to check
    # the round trip.
    ux, uy = 2.0, -1.5
    dx, dy = wac_camera_model._distort(ux, uy)
    rr = dx * dx + dy * dy
    dk = wac_camera_model.OD_K
    dr = 1.0 + dk[0] * rr + dk[1] * rr**2 + dk[2] * rr**3
    recovered_ux, recovered_uy = dx * dr, dy * dr
    assert recovered_ux == pytest.approx(ux, abs=1e-6)
    assert recovered_uy == pytest.approx(uy, abs=1e-6)


def test_project_in_known_framelet_returns_finite_reasonable_pixel():
    # Coarse sanity check only (right order of magnitude, not a specific tight range) -- the real,
    # decisive validation is exact (0.000px) agreement against real campt output, see
    # docs/wac-jigsaw-investigation.md.
    r_cam_to_me = np.eye(3)
    camera_position = np.array([0.0, 0.0, -1737400.0 - 100000.0])  # 100km above the south pole
    ground = np.array([0.0, 0.0, -1737400.0])  # boresight = +Z, straight down

    sample, within_line = wac_camera_model.project_in_known_framelet(ground, camera_position, r_cam_to_me)

    assert np.isfinite(sample) and np.isfinite(within_line)
    assert 0 < sample < 704
