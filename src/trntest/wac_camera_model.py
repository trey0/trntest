"""Hand-rolled ground-to-image (forward) projection for WAC-VIS band 1, replicating ISIS's own
real camera model exactly -- built because a real, confirmed bug in ISIS's own `jigsaw` (its
`PushFrameCameraGroundMap::SetGround` framelet search) makes bundle adjustment against WAC
unusable in stock ISIS. See `docs/wac-jigsaw-investigation.md` for the full investigation this
module is the result of: how the bug was found and isolated (a tautological, mathematically
guaranteed-zero-error control network still produced ~350px `jigsaw` residuals), why a `jigsaw`
fix isn't available (no CLI-exposed workaround; `PushFrameCameraGroundMap` uses a spacecraft-
distance-minimizing heuristic search, not a real containment check, and that's compiled C++ we
can't patch from here), and the exact ISIS source (with citations) every formula below is pulled
from -- none of this is guessed or approximated.

**Validated to exact (0.000px) agreement with real `campt` output** across a well-distributed grid
of real pixels, for the *optics chain only* (camera-frame projection -> distortion -> focal-plane
map -> detector map), given an already-known-correct framelet.

**The framelet search is now implemented** (`find_framelet_and_project`, `calibrate_et_per_crop_line`)
-- given an arbitrary 3D ground point with no prior image coordinates, finds which framelet of a
crop actually images it. Discrete integer-framelet bisection (using `within_framelet_line` as a
monotonic search signal) to a bracketing framelet, then a real 2D containment check, deliberately
not ISIS's own distance-minimizing heuristic (the likely site of `jigsaw`'s bug) -- see
`find_framelet_and_project`'s own docstring for the full design, and confirmed-live details (real
~29% ground-coverage overlap between adjacent framelets, and why that's a legitimate multi-solution
case, not a correctness bug, resolved via a center-line tiebreak chosen for optimizer-smoothness
reasons, not "recovering the right answer"). Live-validated end-to-end against the current default
candidate: forward-project a real crop pixel's own ground point, round-trip through `campt`'s
trusted image-to-ground -- 0.00m ground error across a 3x3 grid spanning the crop's full
sample/line range.

**The optimizer is now implemented** (`fit_pose_correction`, `PoseCorrection`) -- a single, frozen
6-DOF correction (3 position + 3 rotation, not a per-framelet fit) applied identically on top of
every framelet's own real SPICE pose, fit via `scipy.optimize.least_squares` against real control
points. **Not yet done**: an actual fit against `pose_alignment`'s real basemap-derived tie points
(not just synthetic/tautological validation data), and wiring a corrected overlay into
`notebooks/pose_alignment_spike.py` (see `docs/wac-jigsaw-investigation.md`'s "Remaining work").
Only WAC-VIS's default band (band 1, NAIF code -85631) is supported -- this project's own
`isis_wac.py` never requests a different band anywhere, and neither `campt` nor `jigsaw` expose a
way to.

Only the ground-to-image direction is implemented here. Image-to-ground stays on
`campt`/`isis_wac.ground_point_at_pixel`, already proven reliable throughout this project's
history -- there was never a reason to replace it (see the investigation doc for why the two
directions have very different risk profiles for this camera)."""

import dataclasses
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from trntest import isis_wac
from trntest.camera import camera_pose_moon_me
from trntest.wac import SAMPLES

# A large, fixed (not scaled by how far off) residual used when a candidate pose correction makes a
# control point's ground point project outside the crop's real coverage entirely -- keeps
# `scipy.optimize.least_squares`'s residual function well-defined (finite, no crash) without
# fabricating a fake pixel location. Should essentially never trigger from a near-zero starting
# correction (`fit_pose_correction`'s `x0`), since the base, uncorrected model already resolves
# every real control point -- see the module docstring's own live-validation numbers.
_UNRESOLVED_RESIDUAL_PX = 1000.0

# WAC-VIS band 1 (bandid=3, 415nm filter, NAIF code -85631) -- confirmed to be ISIS's own default
# band for any WAC-VIS cube (LroWideAngleCamera's constructor always calls SetBand(1) at the end
# of construction; neither `campt` nor `jigsaw` expose a way to override it -- confirmed live,
# `campt band=1` errors with "Unknown parameter [band]"). Pulled directly from the real furnished
# IK kernel pool (`INS-85631_*`), not estimated or approximated.
FOCAL_LENGTH_MM = 5.9983909
OD_K = (0.011386711675385401, 0.00014704581347987, 5.038005020438671e-06)  # dk1, dk2, dk3
BORESIGHT_SAMPLE = 509.5696  # raw (1024-wide) detector space
BORESIGHT_LINE = 775.8181
ITRANSS = (0.0, 0.0072884596481, 111.1111108720638)
ITRANSL = (0.0, -111.1111108720638, 0.0072884596481)
# raw_detector_sample = cube_sample + this (INS-85621_COLOR_SAMPLE_OFFSET) -- our crop cube's own
# 704-sample window is a crop of the real 1024-sample detector, confirmed via
# CameraDetectorMap::Compute()'s real formula (see the investigation doc).
COLOR_SAMPLE_OFFSET = 160
# raw_detector_line = this (INS-85621_FILTER_OFFSET[0], band 1's own physical CCD row strip) +
# within-framelet line (1..FRAMELET_HEIGHT).
BAND_START_LINE = 703
FRAMELET_HEIGHT = 14  # isis_wac.VIS_BLOCK_HEIGHT -- sumMode=1 for WAC-VIS, no TDI summing to apply


def _distort(ux_mm: float, uy_mm: float, max_iter: int = 50, tol: float = 1e-9) -> tuple[float, float]:
    """Undistorted -> distorted focal-plane mm, via the same fixed-point iteration ISIS's own
    `LroWideAngleCameraDistortionMap::SetUndistortedFocalPlane` uses (no closed form exists for
    the inverse of the radial polynomial `SetFocalPlane` applies) -- see the investigation doc for
    the exact source this is ported from."""
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
    """Projects a 3D ground point (body-fixed ME, meters) through a camera pose already known to
    correspond to the *correct* framelet (position/attitude at that framelet's own real epoch, in
    the same convention as `camera.camera_pose_moon_me`) into `(cube_sample,
    within_framelet_line)` -- `within_framelet_line` is 1..`FRAMELET_HEIGHT`, not a full cube line;
    the caller (the not-yet-implemented framelet search) is responsible for combining it with the
    chosen framelet index to get a real cube line, and for finding which framelet is actually
    correct in the first place -- this function does not search, it only projects.

    Standard pinhole (`BORESIGHT = (0, 0, 1)`, confirmed from the real IK) then ISIS's own real
    radial distortion and affine focal-plane/detector maps, in the same order ISIS itself applies
    them. Validated to exact (0.000px) agreement with real `campt` output given the correct
    framelet -- see the investigation doc for the validation run."""
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
    """`(et0, et_per_line)` such that a framelet `f`'s real acquisition ephemeris time is
    `et0 + et_per_line * center_line(f)`, `center_line(f) = f * FRAMELET_HEIGHT + (FRAMELET_HEIGHT +
    1) / 2` (ISIS 1-based line convention -- framelet 0 spans cube lines 1..14, center 7.5).

    Calibrated from two real `campt` `EphemerisTime` queries (`isis_wac.ephemeris_time_at_pixel`) at
    the center lines of the crop's first and last framelets, rather than hand-deriving
    `crop_window_for_camera`'s row-offset/flip bookkeeping to relate a crop line back to
    `camera.frame_et`'s own full-swath `frame_index` -- deliberately, to keep the sign/offset
    surface area small (see `docs/wac-jigsaw-investigation.md`). This is not an approximation: a
    pushframe sensor's real per-framelet ET is *exactly* affine in framelet index (each framelet
    advances by the same real `interframe_delay_s`), so two real points fully determine it -- the
    two-point fit isn't fit to noisy data, it's just avoiding re-deriving a known-linear
    relationship's offset/slope by hand.

    `n_lines` is the crop cube's real total line count (`FRAMELET_HEIGHT * n_framelets`); any fixed,
    valid sample column works for the ET query (`campt`'s `EphemerisTime` depends only on which
    framelet a line falls in, not the sample within it) -- the crop's own horizontal center is used
    here for no particular reason beyond being unambiguously valid."""
    sample = SAMPLES / 2.0
    n_framelets = n_lines / FRAMELET_HEIGHT
    line_a = (FRAMELET_HEIGHT + 1) / 2.0
    line_b = (n_framelets - 1) * FRAMELET_HEIGHT + (FRAMELET_HEIGHT + 1) / 2.0
    et_a = isis_wac.ephemeris_time_at_pixel(cub_path, sample, line_a)
    et_b = isis_wac.ephemeris_time_at_pixel(cub_path, sample, line_b)
    et_per_line = (et_b - et_a) / (line_b - line_a)
    et0 = et_a - et_per_line * line_a
    return et0, et_per_line


def _center_line(framelet_index: int) -> float:
    return framelet_index * FRAMELET_HEIGHT + (FRAMELET_HEIGHT + 1) / 2.0


@dataclasses.dataclass(frozen=True)
class PoseCorrection:
    """A single, frozen (not time-varying) 6-DOF correction applied identically on top of every
    framelet's own real per-frame SPICE pose -- not a per-framelet fit; see
    `docs/wac-jigsaw-investigation.md`'s "the plan, in one paragraph" for why a single degree-0
    correction is the deliberate scope for this first pass. `delta_position_m` is added directly to
    the real camera position (`camera.camera_pose_moon_me`'s `C_meters`, MOON_ME frame).
    `delta_rotation` (3x3) is composed on the *camera* side (`R_corrected = R_original @
    delta_rotation`), modeling a fixed mounting/boresight-style correction to the camera's own
    internal frame -- matches this project's own precedent that WAC-VIS's real boresight offset is
    frame-constant, not time-varying (`docs/data-sources.md`), rather than a bias applied in the
    inertial/ME frame (which would instead suggest a trajectory error, not a camera-model one)."""

    delta_position_m: np.ndarray
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
    """Real per-framelet SPICE pose (`camera.camera_pose_moon_me` at that framelet's own real
    center-line ET -- the *actual* WAC-VIS instrument pointing at that instant, not this project's
    synthetic re-aimed camera), optionally adjusted by a `PoseCorrection`, then
    `project_in_known_framelet` through it."""
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
    """Given an arbitrary 3D ground point (body-fixed ME, meters) with no prior image coordinates,
    finds which framelet of a `n_framelets`-framelet crop actually images it and returns its real
    `(sample, line)` in the crop cube's own 1-based convention -- or `None` if no framelet does
    (point genuinely outside the crop's real coverage). `correction`, when given, is applied to
    every framelet's real SPICE pose identically before projecting (see `PoseCorrection`) -- used by
    `fit_pose_correction` to evaluate a candidate correction's residuals during optimization.

    Two-stage search, deliberately *not* `jigsaw`'s own spacecraft-distance-minimizing heuristic
    (the confirmed site of its bug, see the module docstring and `docs/wac-jigsaw-investigation.md`):

    1. **Discrete integer-framelet bisection**, using `project_in_known_framelet`'s own
       `within_framelet_line` as the monotonic search signal (a fixed ground point's image line
       moves monotonically across the sensor as the spacecraft advances along-track, over this
       crop's short real timespan) -- narrowing to a single bracketing framelet (or an adjacent
       pair, if the true answer sits right on a boundary) in O(log n_framelets) real pose
       evaluations. Whether `within_framelet_line` *increases* or *decreases* with framelet index is
       measured live from the two range endpoints, not assumed -- it flips with this pass's real yaw
       state (same underlying cause as `camera.reverse_crop_along_track`), so a hardcoded direction
       would silently converge to the wrong framelet for a pass with the opposite sign (caught live
       via a synthetic test candidate, not yet observed on a real one).
    2. **A real 2D containment check** (`1 <= sample <= SAMPLES`, `1 <= within_framelet_line <=
       FRAMELET_HEIGHT`) on the bracketing framelet(s) from step 1 -- not just trusting the
       bisection's own endpoint, since the monotonic signal alone doesn't guarantee the sample axis
       also lands in range. **Real overlap confirmed to actually occur for this product** (live
       Docker validation: adjacent framelets' `within_framelet_line` advances by ~9.9 lines per
       framelet step, not the full `FRAMELET_HEIGHT`=14 -- a ~4-line/~29% real overlap, matching
       `docs/data-sources.md`'s independent note, from the `usgscsm` bug investigation, that
       adjacent Pushframe exposures have real ground-coverage overlap). When more than one framelet
       validly contains the point, picks whichever puts the point closer to that framelet's own
       center line (the user's own proposed tiebreak) -- deliberately *not* keyed to the bisection's
       own convergence index, which is just an artifact of the search path (which valid framelet the
       binary search happens to land on first), not a geometrically meaningful signal.

       A ground point genuinely landing in two different, both-valid framelets (confirmed live: real
       overlap in this crop's coverage, `ground_err` 0.00m against `campt`'s trusted inverse either
       way) isn't a correctness problem to resolve -- ground-to-image legitimately has more than one
       valid solution there, and any of them is an equally correct answer; there's no need to recover
       whichever specific pixel a prior observation happened to be measured at. What the center-line
       tiebreak is actually for: for a downstream optimizer, the choice needs to stay *smooth* as
       pose parameters are perturbed during the fit, not just correct at one exact point -- picking
       the framelet where the point sits deepest inside its valid range (farthest from either
       containment boundary) maximizes the neighborhood of nearby ground/pose perturbations that
       stay on the same framelet's smooth reprojection chart, before a small step would flip the
       choice to a different framelet entirely (a discontinuity gradient-based optimization can't
       handle well).

       `within_framelet_line`'s sign of change per framelet step is **not assumed fixed** -- it
       depends on this pass's real yaw state (same underlying cause as `camera.
       reverse_crop_along_track`/`boresight_rotation_k`: LRO's WAC undergoes periodic 180-degree yaw
       flips), so the direction is measured live from the two range endpoints before bisecting,
       rather than hardcoded to whichever direction one validated real candidate happened to have
       (caught live: a synthetic test candidate with the opposite sign converged to a completely
       wrong framelet under the hardcoded-direction version of this function)."""
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
    """Fits a single, frozen 6-DOF `PoseCorrection` (3 position + 3 rotation, degree-0 -- see that
    class's own docstring for why) against real control points, via
    `scipy.optimize.least_squares`. `ground_points_me_m` (N, 3) and `observed_pixels` (N, 2,
    `(sample, line)` in the crop's own 1-based convention) are same-length paired arrays -- e.g.
    `control_network.resolve_control_points`'s output (`ground_lonlat` converted to MOON_ME meters
    via `tie_points.lonlat_to_ground_km`) and `observed_pixels` directly.

    The rotation correction is parameterized as a 3-vector rotation vector (`scipy.spatial.transform.
    Rotation.from_rotvec`, the standard small-angle/exponential-map parameterization for a pose
    refinement close to identity -- avoids the gimbal-lock and non-uniqueness issues of an Euler-angle
    parameterization) rather than 3 separate angles.

    Residual = predicted-minus-observed pixel, `(sample, line)`, for every control point, computed
    via `find_framelet_and_project` under the candidate correction; a control point whose ground
    point falls outside the crop's coverage under some candidate correction gets a fixed
    `_UNRESOLVED_RESIDUAL_PX` penalty rather than crashing the solve (see that constant's own
    docstring -- should essentially never trigger starting from `x0=0`, since the uncorrected model
    already resolves every real control point).

    Starts from `x0 = 0` (no correction) -- the right starting point here, since it's already known
    to be close to correct (the overlay's own visible misalignment is small, "not huge" per direct
    user observation), not an arbitrary default."""
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
