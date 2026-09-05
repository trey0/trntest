"""Hand-rolled ground-to-image (forward) projection for WAC-VIS band 1, replicating ISIS's own
camera model. Built because ISIS's own `jigsaw` (`PushFrameCameraGroundMap::SetGround`) has a
confirmed framelet-search bug that makes bundle adjustment against WAC unusable in stock ISIS -- see
`docs/pose-alignment.md` for the investigation and each formula's ISIS source citation.

Three pieces: the optics chain (`project_in_known_framelet`), the framelet search
(`find_framelet_and_project`, a 2D containment check rather than `jigsaw`'s own heuristic search),
and a 6-DOF pose-correction optimizer (`fit_pose_correction`/`PoseCorrection`) fit against control
points.

Only WAC-VIS band 1 (NAIF code -85631) is supported -- this project's own `isis_wac.py` never
requests another band, and neither `campt` nor `jigsaw` expose a way to. Only ground-to-image is
implemented here; image-to-ground still goes through `campt`/`isis_campt.ground_point_at_pixel` (see
the investigation doc for why the two directions have different risk profiles for this camera).
"""

# The optics chain (this module's projection given an already-known-correct framelet) is validated
# to exact (0.000px) agreement with `campt` output across a well-distributed pixel grid. The
# framelet search adds an arbitrary-ground-point-to-framelet lookup on top, live-validated
# end-to-end (forward-project a crop pixel's own ground point, round-trip through `campt`'s
# image-to-ground: 0.00m error across a 3x3 grid spanning the crop). The pose-correction optimizer
# has been fit against basemap-derived tie points and wired into
# `notebooks/pose_alignment_spike.py` -- see docs/pose-alignment.md's "Open item" section
# for what's still unresolved (a DEM-aware shape model, the leading suspect for why the fit only
# closes part of the gap).

import dataclasses
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from trntest import isis_campt
from trntest.camera import camera_pose_moon_me
from trntest.wac_format import SAMPLES

# A large, fixed residual used when a candidate pose correction makes a control point's ground point
# project outside the crop's coverage entirely -- keeps `scipy.optimize.least_squares`'s residual
# function well-defined without fabricating a fake pixel location. Should never trigger from
# `fit_pose_correction`'s `x0=0` starting correction, since the uncorrected model already resolves
# every control point.
_UNRESOLVED_RESIDUAL_PX = 1000.0

# WAC-VIS band 1 (bandid=3, 415nm filter, NAIF code -85631) -- ISIS's own default band for any
# WAC-VIS cube (`LroWideAngleCamera`'s constructor always calls `SetBand(1)`; neither `campt` nor
# `jigsaw` expose a way to override it). Pulled directly from the furnished IK kernel pool
# (`INS-85631_*`).
FOCAL_LENGTH_MM = 5.9983909
OD_K = (0.011386711675385401, 0.00014704581347987, 5.038005020438671e-06)  # dk1, dk2, dk3
BORESIGHT_SAMPLE = 509.5696  # raw (1024-wide) detector space
BORESIGHT_LINE = 775.8181
ITRANSS = (0.0, 0.0072884596481, 111.1111108720638)
ITRANSL = (0.0, -111.1111108720638, 0.0072884596481)
# raw_detector_sample = cube_sample + this (INS-85621_COLOR_SAMPLE_OFFSET) -- our crop cube's own
# 704-sample window is a crop of the real 1024-sample detector, per
# `CameraDetectorMap::Compute()`'s formula (see the investigation doc).
COLOR_SAMPLE_OFFSET = 160
# raw_detector_line = this (INS-85621_FILTER_OFFSET[0], band 1's own physical CCD row strip) +
# within-framelet line (1..FRAMELET_HEIGHT).
BAND_START_LINE = 703
FRAMELET_HEIGHT = 14  # isis_wac.VIS_BLOCK_HEIGHT -- sumMode=1 for WAC-VIS, no TDI summing to apply


def _distort(ux_mm: float, uy_mm: float, max_iter: int = 50, tol: float = 1e-9) -> tuple[float, float]:
    """Undistorted -> distorted focal-plane mm, via the same fixed-point iteration ISIS's own
    `LroWideAngleCameraDistortionMap::SetUndistortedFocalPlane` uses.

    :param ux_mm: Undistorted focal-plane x, mm.
    :param uy_mm: Undistorted focal-plane y, mm.
    :param max_iter: Maximum fixed-point iterations.
    :param tol: Convergence tolerance, mm.
    :returns: Distorted `(x_mm, y_mm)`.
    """
    # No closed form exists for the inverse of the radial polynomial `SetFocalPlane` applies -- see
    # docs/pose-alignment.md for the exact ISIS source this is ported from.
    xt, yt = ux_mm, uy_mm
    for _ in range(max_iter):
        rr = xt * xt + yt * yt
        dr = 1.0 + OD_K[0] * rr + OD_K[1] * rr**2 + OD_K[2] * rr**3
        xt_new, yt_new = ux_mm / dr, uy_mm / dr
        if abs(xt_new - xt) < tol and abs(yt_new - yt) < tol:
            return xt_new, yt_new
        xt, yt = xt_new, yt_new
    return xt, yt


def project_in_known_framelet(
    ground_me_m: np.ndarray, camera_position_me_m: np.ndarray, r_cam_to_me: np.ndarray
) -> tuple[float, float]:
    """Project a ground point through a camera pose already known to belong to the correct framelet.

    :param ground_me_m: Ground point, body-fixed ME, meters.
    :param camera_position_me_m: Camera position at that framelet's own epoch, MOON_ME meters, same
        convention as `camera.camera_pose_moon_me`.
    :param r_cam_to_me: Camera-to-MOON_ME rotation at that same epoch.
    :returns: `(cube_sample, within_framelet_line)`. `within_framelet_line` is `1..FRAMELET_HEIGHT`,
        not a full cube line -- the caller combines it with the chosen framelet index.
    """
    # Standard pinhole (BORESIGHT = (0, 0, 1), from the IK), then ISIS's own radial distortion and
    # affine focal-plane/detector maps, in the same order ISIS itself applies them. Validated to
    # exact (0.000px) agreement with `campt` output given the correct framelet -- see
    # docs/pose-alignment.md for the validation run. Does not search for the framelet
    # itself -- see `find_framelet_and_project`.
    look_me = ground_me_m - camera_position_me_m
    look_cam = r_cam_to_me.T @ look_me
    ux_mm = FOCAL_LENGTH_MM * look_cam[0] / look_cam[2]
    uy_mm = FOCAL_LENGTH_MM * look_cam[1] / look_cam[2]
    dx_mm, dy_mm = _distort(ux_mm, uy_mm)
    raw_sample = ITRANSS[0] + ITRANSS[1] * dx_mm + ITRANSS[2] * dy_mm + (BORESIGHT_SAMPLE + 1.0)
    raw_line = ITRANSL[0] + ITRANSL[1] * dx_mm + ITRANSL[2] * dy_mm + (BORESIGHT_LINE + 1.0)
    cube_sample = raw_sample - COLOR_SAMPLE_OFFSET
    within_framelet_line = raw_line - BAND_START_LINE
    return cube_sample, within_framelet_line


def calibrate_et_per_crop_line(cub_path: Path, n_lines: int) -> tuple[float, float]:
    """Fit `(et0, et_per_line)` such that framelet `f`'s acquisition ephemeris time is
    `et0 + et_per_line * center_line(f)`.

    :param cub_path: Path to the crop cube.
    :param n_lines: The crop cube's total line count (`FRAMELET_HEIGHT * n_framelets`).
    :returns: `(et0, et_per_line)`.
    """
    # center_line(f) = f * FRAMELET_HEIGHT + (FRAMELET_HEIGHT + 1) / 2 (ISIS 1-based line convention
    # -- framelet 0 spans cube lines 1..14, center 7.5). Calibrated from two `campt` `EphemerisTime`
    # queries at the center lines of the crop's first and last framelets, rather than hand-deriving
    # `crop_window_for_camera`'s row-offset/flip bookkeeping to relate a crop line back to
    # `camera.frame_et`'s own full-swath `frame_index` -- keeps the sign/offset surface area small
    # (see docs/pose-alignment.md). A pushframe sensor's per-framelet ET is exactly affine
    # in framelet index (each framelet advances by the same `interframe_delay_s`), so two points
    # fully determine it; this isn't a fit to noisy data. Any fixed, valid sample column works for
    # the ET query (`EphemerisTime` depends only on which framelet a line falls in, not the sample
    # within it) -- the crop's horizontal center is used here for no particular reason beyond being
    # unambiguously valid.
    sample = SAMPLES / 2.0
    n_framelets = n_lines / FRAMELET_HEIGHT
    line_a = (FRAMELET_HEIGHT + 1) / 2.0
    line_b = (n_framelets - 1) * FRAMELET_HEIGHT + (FRAMELET_HEIGHT + 1) / 2.0
    et_a = isis_campt.ephemeris_time_at_pixel(cub_path, sample, line_a)
    et_b = isis_campt.ephemeris_time_at_pixel(cub_path, sample, line_b)
    et_per_line = (et_b - et_a) / (line_b - line_a)
    et0 = et_a - et_per_line * line_a
    return et0, et_per_line


def _center_line(framelet_index: int) -> float:
    return framelet_index * FRAMELET_HEIGHT + (FRAMELET_HEIGHT + 1) / 2.0


@dataclasses.dataclass(frozen=True)
class PoseCorrection:
    """A single, frozen 6-DOF correction applied identically on top of every framelet's own SPICE
    pose (not a per-framelet fit)."""

    delta_position_m: np.ndarray  # added to the camera position (camera_pose_moon_me's C_meters)
    # Composed on the camera side (R_corrected = R_original @ this), modeling a fixed mounting/
    # boresight-style correction to the camera's own internal frame, rather than a bias in the
    # inertial/ME frame (which would instead suggest a trajectory error, not a camera-model one) --
    # matches WAC-VIS's boresight offset being frame-constant, not time-varying (see
    # docs/data-sources/spice-kernels-naif.md's WAC frame chain note).
    delta_rotation: np.ndarray

    @staticmethod
    def identity() -> "PoseCorrection":
        return PoseCorrection(delta_position_m=np.zeros(3), delta_rotation=np.eye(3))


def _project_at_framelet(
    ground_me_m: np.ndarray,
    framelet_index: int,
    et0: float,
    et_per_line: float,
    correction: PoseCorrection | None = None,
):
    """`project_in_known_framelet` at a given framelet's own SPICE pose, optionally adjusted by a
    `PoseCorrection`.

    :param ground_me_m: Ground point, body-fixed ME, meters.
    :param framelet_index: Which framelet's pose to project through.
    :param et0: From `calibrate_et_per_crop_line`.
    :param et_per_line: From `calibrate_et_per_crop_line`.
    :param correction: Applied on top of the framelet's own pose, if given.
    :returns: `(cube_sample, within_framelet_line)`, see `project_in_known_framelet`.
    """
    et = et0 + et_per_line * _center_line(framelet_index)
    c_m, r_cam_to_me, _, _ = camera_pose_moon_me(et)
    if correction is not None:
        c_m = c_m + correction.delta_position_m
        r_cam_to_me = r_cam_to_me @ correction.delta_rotation
    return project_in_known_framelet(ground_me_m, c_m, r_cam_to_me)


def find_framelet_and_project(
    ground_me_m: np.ndarray,
    n_framelets: int,
    et0: float,
    et_per_line: float,
    correction: PoseCorrection | None = None,
) -> tuple[float, float] | None:
    """Find which framelet of a crop images a ground point with no prior image coordinates, and
    project it.

    :param ground_me_m: Ground point, body-fixed ME, meters.
    :param n_framelets: Framelet count of the crop cube being searched.
    :param et0: From `calibrate_et_per_crop_line`.
    :param et_per_line: From `calibrate_et_per_crop_line`.
    :param correction: Applied to every framelet's pose identically before projecting, if given (used
        by `fit_pose_correction` to evaluate a candidate correction's residuals).
    :returns: `(sample, line)` in the crop cube's 1-based convention, or `None` if no framelet images
        the point (it's outside the crop's coverage).
    """
    # Two-stage search, deliberately not `jigsaw`'s own spacecraft-distance-minimizing heuristic (the
    # confirmed site of its bug -- see the module docstring and docs/pose-alignment.md):
    #
    # 1. Discrete integer-framelet bisection, using `project_in_known_framelet`'s own
    #    `within_framelet_line` as the monotonic search signal (a fixed ground point's image line
    #    moves monotonically across the sensor as the spacecraft advances along-track, over a crop's
    #    short timespan) -- narrows to a single bracketing framelet (or an adjacent pair, if the true
    #    answer sits on a boundary) in O(log n_framelets) pose evaluations. Whether
    #    `within_framelet_line` increases or decreases with framelet index is measured live from the
    #    two range endpoints, not assumed -- it flips with a pass's yaw state (same underlying cause
    #    as `camera.reverse_crop_along_track`), so a hardcoded direction would silently converge to
    #    the wrong framelet for the opposite-yaw case.
    # 2. A 2D containment check (`1 <= sample <= SAMPLES`, `1 <= within_framelet_line <=
    #    FRAMELET_HEIGHT`) on the bracketing framelet(s) from step 1, since the monotonic signal alone
    #    doesn't guarantee the sample axis also lands in range. Adjacent framelets overlap by ~4 of
    #    their 14 lines (~29%, confirmed via live Docker validation, matching
    #    docs/external-tools.md's independent note from the `usgscsm` bug investigation that adjacent
    #    Pushframe exposures have ground-coverage overlap) -- a ground point can validly land in two
    #    different framelets, and either is an equally correct answer; there's no "right" one to
    #    recover. When more than one framelet validly contains the point, this picks whichever puts
    #    the point closer to that framelet's own center line, not whichever the bisection happened to
    #    converge on first (an artifact of the search path, not a geometrically meaningful signal):
    #    for `fit_pose_correction`'s gradient-based optimizer, the choice needs to stay smooth as pose
    #    parameters are perturbed, not just correct at one exact point, and picking the framelet where
    #    the point sits deepest inside its valid range maximizes the neighborhood of nearby
    #    ground/pose perturbations that stay on the same framelet before the choice flips.
    lo, hi = 0, n_framelets - 1
    _, within_line_lo = _project_at_framelet(ground_me_m, lo, et0, et_per_line, correction)
    _, within_line_hi = _project_at_framelet(ground_me_m, hi, et0, et_per_line, correction)
    increasing = within_line_hi > within_line_lo
    while lo < hi:
        mid = (lo + hi) // 2
        _, within_line = _project_at_framelet(ground_me_m, mid, et0, et_per_line, correction)
        if 1.0 <= within_line <= FRAMELET_HEIGHT:
            lo = hi = mid
        elif (within_line < 1.0) == increasing:
            lo = mid + 1
        else:
            hi = mid

    candidates = [f for f in (lo - 1, lo, lo + 1) if 0 <= f < n_framelets]
    valid = []
    for f in candidates:
        sample, within_line = _project_at_framelet(ground_me_m, f, et0, et_per_line, correction)
        if 1.0 <= sample <= SAMPLES and 1.0 <= within_line <= FRAMELET_HEIGHT:
            valid.append((f, sample, within_line))
    if not valid:
        return None

    f, sample, within_line = min(valid, key=lambda v: abs(v[2] - (FRAMELET_HEIGHT + 1) / 2.0))
    line = f * FRAMELET_HEIGHT + within_line
    return sample, line


@dataclasses.dataclass(frozen=True)
class PoseCorrectionFit:
    """The result of `fit_pose_correction`."""

    correction: PoseCorrection
    residuals_px: np.ndarray  # (N, 2) -- final (sample, line) residuals per control point, at the fit
    success: bool
    cost: float  # scipy's own 0.5 * sum(residuals**2), for comparing fits


def fit_pose_correction(
    ground_points_me_m: np.ndarray,
    observed_pixels: np.ndarray,
    n_framelets: int,
    et0: float,
    et_per_line: float,
) -> PoseCorrectionFit:
    """Fit a single, frozen 6-DOF `PoseCorrection` against control points, via
    `scipy.optimize.least_squares`.

    :param ground_points_me_m: `(N, 3)` control-point ground points, body-fixed ME meters -- e.g.
        `control_network.resolve_control_points`'s output (`ground_lonlat` converted via
        `tie_points.lonlat_to_ground_km`).
    :param observed_pixels: `(N, 2)` `(sample, line)` observed pixels, same length and point order as
        `ground_points_me_m`, crop's 1-based convention.
    :param n_framelets: Framelet count of the crop cube.
    :param et0: From `calibrate_et_per_crop_line`.
    :param et_per_line: From `calibrate_et_per_crop_line`.
    :returns: The fitted correction, per-point residuals, and solver status.
    """
    # The rotation correction is parameterized as a 3-vector rotation vector
    # (`Rotation.from_rotvec`, the standard small-angle/exponential-map parameterization for a pose
    # refinement close to identity) rather than 3 separate Euler angles, avoiding gimbal-lock and
    # non-uniqueness. Residual = predicted-minus-observed pixel for every control point, computed via
    # `find_framelet_and_project` under the candidate correction; a point whose ground point falls
    # outside the crop's coverage under some candidate correction gets a fixed
    # `_UNRESOLVED_RESIDUAL_PX` penalty rather than crashing the solve (should never trigger starting
    # from `x0=0`, since the uncorrected model already resolves every control point). Starts from
    # `x0=0` (no correction), since it's already known to be close to correct.
    n_points = len(ground_points_me_m)

    def residuals(x: np.ndarray) -> np.ndarray:
        correction = PoseCorrection(delta_position_m=x[:3], delta_rotation=Rotation.from_rotvec(x[3:]).as_matrix())
        out = np.full(2 * n_points, _UNRESOLVED_RESIDUAL_PX)
        for i in range(n_points):
            result = find_framelet_and_project(
                ground_points_me_m[i], n_framelets, et0, et_per_line, correction=correction
            )
            if result is not None:
                pred_sample, pred_line = result
                out[2 * i] = pred_sample - observed_pixels[i, 0]
                out[2 * i + 1] = pred_line - observed_pixels[i, 1]
        return out

    fit = least_squares(residuals, np.zeros(6))
    correction = PoseCorrection(delta_position_m=fit.x[:3], delta_rotation=Rotation.from_rotvec(fit.x[3:]).as_matrix())
    return PoseCorrectionFit(
        correction=correction, residuals_px=fit.fun.reshape(n_points, 2), success=fit.success, cost=fit.cost
    )
