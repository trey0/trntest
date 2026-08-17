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
map -> detector map), given an already-known-correct framelet. **Not yet implemented**: the
framelet *search* itself (given an arbitrary 3D ground point with no image coordinates, which
framelet images it) -- see the module docstring's own "Remaining work" section in
`docs/wac-jigsaw-investigation.md` for the agreed design (discrete integer-framelet bisection +
explicit 2D containment check on bracketing framelets, deliberately not ISIS's own distance-
heuristic approach, which is the likely site of `jigsaw`'s bug). Only WAC-VIS's default band
(band 1, NAIF code -85631) is supported -- this project's own `isis_wac.py` never requests a
different band anywhere, and neither `campt` nor `jigsaw` expose a way to.

Only the ground-to-image direction is implemented here. Image-to-ground stays on
`campt`/`isis_wac.ground_point_at_pixel`, already proven reliable throughout this project's
history -- there was never a reason to replace it (see the investigation doc for why the two
directions have very different risk profiles for this camera)."""

import numpy as np

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
