from unittest.mock import patch

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


def test_calibrate_et_per_crop_line_fits_an_exact_affine_line_through_two_real_points():
    # Two real campt EphemerisTime values at the crop's first/last framelet centers should
    # reproduce exactly at those same two lines (an affine fit through exactly 2 points is exact by
    # construction) -- the real check is that et0/et_per_line are derived correctly from them, not
    # that the fit is approximate.
    n_lines = 980  # 70 framelets * FRAMELET_HEIGHT (14)
    center_a = (wac_camera_model.FRAMELET_HEIGHT + 1) / 2.0  # framelet 0's center line, 7.5
    center_b = 69 * wac_camera_model.FRAMELET_HEIGHT + center_a  # framelet 69's center line

    def fake_et(cub_path, sample, line):
        assert line in (center_a, center_b)
        return 1000.0 if line == center_a else 1000.0 - 69 * 1.40625  # matches a real interframe_delay

    with patch("trntest.isis_wac.ephemeris_time_at_pixel", side_effect=fake_et):
        et0, et_per_line = wac_camera_model.calibrate_et_per_crop_line("fake.cub", n_lines)

    assert et0 + et_per_line * center_a == pytest.approx(1000.0)
    assert et0 + et_per_line * center_b == pytest.approx(1000.0 - 69 * 1.40625)
    assert et_per_line * wac_camera_model.FRAMELET_HEIGHT == pytest.approx(-1.40625)


def test_find_framelet_and_project_returns_none_when_no_framelet_contains_the_point():
    # A point that projects out of [1, SAMPLES] range on every framelet in the crop.
    with patch.object(wac_camera_model, "_project_at_framelet", return_value=(-50.0, 7.5)):
        result = wac_camera_model.find_framelet_and_project(np.zeros(3), n_framelets=70, et0=0.0, et_per_line=0.0)
    assert result is None


def test_find_framelet_and_project_finds_the_single_containing_framelet():
    # Synthetic, non-overlapping framelet geometry (within_line steps by exactly FRAMELET_HEIGHT per
    # framelet, matching FRAMELET_HEIGHT's own scale -- no real overlap band) -- framelet 5 is the
    # unique correct answer, at within_line=3.0 (not centered, so a wrong tiebreak would be obvious).
    true_f = 5

    def fake_project(ground_me_m, f, et0, et_per_line):
        within_line = 3.0 - wac_camera_model.FRAMELET_HEIGHT * (f - true_f)
        return 400.0, within_line

    with patch.object(wac_camera_model, "_project_at_framelet", side_effect=fake_project):
        result = wac_camera_model.find_framelet_and_project(np.zeros(3), n_framelets=70, et0=0.0, et_per_line=0.0)

    assert result == (400.0, true_f * wac_camera_model.FRAMELET_HEIGHT + 3.0)


def test_find_framelet_and_project_picks_the_overlap_candidate_closest_to_its_own_center_line():
    # Real WAC framelets overlap ~29% (within_line steps by ~9.9, not the full 14, per framelet --
    # confirmed live, see find_framelet_and_project's own docstring) -- reproduces that scenario
    # synthetically: framelets 4 and 5 are both valid for this ground point (within_line 11.9 and
    # 2.0 respectively), and the center-line tiebreak should prefer framelet 4 (|11.9-7.5|=4.4)
    # over framelet 5 (|2.0-7.5|=5.5), even though framelet 5 is the "exact" original-pixel answer --
    # this is deliberate (see the module docstring: any geometrically valid solution is correct,
    # the tiebreak is about optimizer smoothness, not recovering one specific answer).
    step = 9.9

    def fake_project(ground_me_m, f, et0, et_per_line):
        within_line = 2.0 + step * (5 - f)
        return 674.0, within_line

    with patch.object(wac_camera_model, "_project_at_framelet", side_effect=fake_project):
        result = wac_camera_model.find_framelet_and_project(np.zeros(3), n_framelets=70, et0=0.0, et_per_line=0.0)

    expected_within_line = 2.0 + step * (5 - 4)
    assert result == pytest.approx((674.0, 4 * wac_camera_model.FRAMELET_HEIGHT + expected_within_line))


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
