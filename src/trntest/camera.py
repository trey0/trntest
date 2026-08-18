"""Build a `.tsai` Pinhole camera file posed from the real LRO SPICE trajectory, at the timestamp
of a chosen LROC WAC EDR framelet -- so a small synthetic image rendered from this camera
approximates the FOV of that part of the real swath.

`config.target_frame_index` is the START of the along-track crop; the actual pose epoch is that
crop's own temporal midpoint (see `build_camera`). Its default (440) is only meaningful for this
repo's original single-demo product -- the live default path, `trntest.dataset.images_for_window`/
`generate_dataset`, sets it per-product instead, anchored at each product's own temporal midpoint
and filtered by illumination there. See docs/history.md (Phase 2) for how 440 was originally chosen.
"""

import dataclasses
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import numpy as np
import spiceypy as spice

from trntest import cache, spice_kernels
from trntest.config import DEFAULT_MOON_RADIUS_KM, TrntestConfig, load_config

PDS_NS = {
    "pds": "http://pds.nasa.gov/pds4/pds/v1",
    "lro": "http://pds.nasa.gov/pds4/mission/lro/v1",
    "img": "http://pds.nasa.gov/pds4/img/v1",
}

# Sensor-model axis convention: applying rotation_about_boresight(1) to the raw WAC-VIS camera
# frame makes the synthetic image's px (column) align with the raw frame's +Y and py (row) align
# with -X; rotation_about_boresight(3) instead aligns px with -Y and py with +X. These are the only
# two boresight rotations that put px on cross-track and py on along-track -- matching both WAC's
# and NAC's real archived-image layout (samples/columns = cross-track, lines/rows = along-track;
# see docs/data-sources.md for why this isn't actually a WAC-vs-NAC choice, both agree).
#
# Between k=1 and k=3: pick whichever makes +py increase in the same temporal sense as the real
# archived WAC image's row axis (rows increase forward in time, by construction of how
# wac.fetch_vis_mosaic stacks frames). This is genuinely pass/yaw-state-dependent, not a fixed
# hardware property -- LRO's WAC is body-fixed (no gimbal) and undergoes periodic 180-degree yaw
# flips that rotate the whole instrument frame, including which raw axis "forward in time" projects
# onto -- so `boresight_rotation_k` below measures it fresh via real SPICE trajectory data for every
# pose, instead of assuming a constant. See docs/data-sources.md ("Pass-dependent sensor axis
# convention") and docs/history.md (Phase 9) for why this isn't just a fixed hardware property.
_FORWARD_TIME_K = 1  # forward_step_me_km projects to -X_raw
_REVERSED_TIME_K = 3  # forward_step_me_km projects to +X_raw -- also Camera.reverse_crop_along_track


def boresight_rotation_k(r_cam_to_me_raw: np.ndarray, forward_step_me_km: np.ndarray) -> int:
    """Pick k in {1, 3} (see module comment above) so that +py increases in the same temporal sense
    as `forward_step_me_km` -- the real, empirically-measured "forward in time" ground-track
    direction (MOON_ME, from `ground_track_step_km`) at this pose. k=1 sends py to -X_raw, so it's
    correct when `forward_step_me_km` projects to negative X in the raw camera frame; k=3 (py=+X_raw)
    is correct when it projects positive."""
    forward_cam = r_cam_to_me_raw.T @ forward_step_me_km
    return _FORWARD_TIME_K if forward_cam[0] < 0 else _REVERSED_TIME_K


@dataclasses.dataclass(frozen=True)
class FrameTiming:
    """Per-frame acquisition timing, parsed from the EDR product's PDS4 label (start time,
    spacecraft clock, interframe delay, frame count) -- see `fetch_frame_timing`. This is the only
    role EDR data plays anywhere in this pipeline; no EDR pixel data is ever read. The actual image
    pixel data used for visual comparison against the synthetic render comes entirely from the CDR
    counterpart of the same acquisition -- see `trntest.wac.fetch_vis_mosaic`."""

    start_time: datetime
    sclk_start: str
    interframe_delay_s: float
    nframes: int


@dataclasses.dataclass(frozen=True)
class Camera:
    """SPICE-derived pose and geometry for the synthetic camera, as returned by `build_camera`."""

    et: float
    center_frame_index: float
    camera_center_moon_me_m: list
    camera_along_track_direction_moon_me: list  # unit vector, MOON_ME frame, not a velocity -- the
    # sensor's own real along-track (py) axis, pre-twist X per the sensor-model axis convention
    # comment near this module's top (cross-track/px is the *other* pre-twist axis, cross(z, x), not
    # this). Used by `lunaserv._terrain_photometric_angles`'s `along_track_correction`; confirmed via
    # real ISIS `campt` ground truth to track real per-pixel phase far better than the spacecraft's
    # raw orbital velocity direction did (an earlier version of this field) -- see docs/history.md's
    # dated entry.
    r_cam_to_me: list
    boresight_rotation_k: int
    slant_range_km: float
    off_nadir_deg: float
    focal_length_u_px: float
    focal_length_v_px: float  # != focal_length_u_px -- see `solve_corrected_fov`'s docstring
    principal_point_u_px: float
    principal_point_v_px: float  # != image_size / 2.0 -- see `solve_corrected_fov`'s docstring
    footprint_lonlat_deg: dict[str, tuple[float, float] | None]
    render_cross_track_km: float  # the render's own actual footprint width -- != cross_track_width_km
    render_along_track_km: float  # once solve_corrected_fov shrinks the FOV; see that function's docstring
    cross_track_width_km: float
    km_per_frame: float
    n_frames_for_square_crop: int
    tsai_path: Path

    @property
    def reverse_crop_along_track(self) -> bool:
        """True when this pass's real "forward in time" ground-track direction is dominant +X in
        the raw WAC-VIS camera frame (see `boresight_rotation_k`) -- opposite of the original
        reference product's convention. `wac.fetch_vis_mosaic` must then stack CDR frames in
        reverse along-track order (and `tie_points`/`orientation` must correspondingly flip their
        row/up-direction conventions) for the crop's pixel-space chirality to keep matching the
        synthetic image's -- see docs/data-sources.md, "Pass-dependent sensor axis convention": a
        genuine, pass-dependent mirror, not just a rotation."""
        return self.boresight_rotation_k == _REVERSED_TIME_K


def fetch_frame_timing(config: TrntestConfig | None = None) -> FrameTiming:
    """Fetch and parse the chosen EDR product's PDS4 XML label for its frame timing. See
    `FrameTiming`'s docstring for why this is named for timing, not "EDR", despite reading from the
    EDR product's label."""
    config = config or load_config()
    label_path = cache.fetch_lroc_file(
        config.lroc_edr_dataset,
        config.edr_volume,
        config.edr_subdir,
        config.edr_doy,
        config.edr_product,
        "xml",
        cache_root=config.cache_root,
        base_url=config.lroc_base_url,
    )
    root = ET.parse(label_path).getroot()

    start_str = _required_text(root, ".//pds:Time_Coordinates/pds:start_date_time")
    start_time = datetime.strptime(start_str, "%Y-%m-%dT%H:%M:%S.%f%z")
    sclk_start = _required_text(root, ".//lro:spacecraft_clock_start_count")
    delay_ms = float(_required_text(root, ".//lro:interframe_delay"))
    nframes = int(_required_text(root, ".//lro:nframes"))

    return FrameTiming(start_time, sclk_start, delay_ms / 1000.0, nframes)


def _required_text(root: ET.Element, xpath: str) -> str:
    """`root.find(xpath).text`, asserting the element and its text are present -- if the EDR
    label's schema is ever missing one of these fields, fail loudly here rather than with a
    confusing `AttributeError`/`TypeError` downstream."""
    element = root.find(xpath, PDS_NS)
    assert element is not None, f"EDR label missing expected element: {xpath}"
    assert element.text is not None, f"EDR label element has no text: {xpath}"
    return element.text


def frame_et(frame_timing: FrameTiming, frame_index: float) -> float:
    """SPICE ET (seconds) of a given framelet, computed from the product's start SCLK."""
    et0 = spice.scs2e(spice_kernels.LRO_ID, frame_timing.sclk_start)
    return et0 + frame_index * frame_timing.interframe_delay_s


def off_nadir_and_slant_range(c_km: np.ndarray, boresight_me: np.ndarray) -> tuple[float, float]:
    """off_nadir_deg (angle between `boresight_me` and local nadir) and slant_range_km (distance
    from `c_km` to where `boresight_me` hits the Moon) -- shared by `camera_pose_moon_me` (boresight
    = the nominal `LRO_LROCWAC_VIS` frame's raw Z axis) and `build_camera`'s re-aimed boresight (see
    its docstring) with whatever boresight direction the caller actually used."""
    nadir = -c_km / np.linalg.norm(c_km)
    off_nadir_deg = np.degrees(np.arccos(np.clip(np.dot(boresight_me, nadir), -1, 1)))
    slant_range_km = ray_sphere_intersect_range(c_km, boresight_me)
    # A camera actually looking at the Moon (as this demo's near-nadir poses always are) always
    # hits the sphere along its own boresight; None here would mean the boresight points away from
    # the Moon entirely, which would indicate a real upstream pose bug worth failing loudly on.
    assert slant_range_km is not None, "camera boresight does not intersect the Moon -- pose is not looking at it"
    return off_nadir_deg, slant_range_km


def camera_pose_moon_me(et: float):
    """Return (C_meters, R_cam_to_moon_me, slant_range_km) for the WAC VIS channel at time et."""
    state, _ = spice.spkezr("LRO", et, "MOON_ME", "NONE", "MOON")
    c_km = np.array(state[:3])
    r_cam_to_me = np.array(spice.pxform("LRO_LROCWAC_VIS", "MOON_ME", et))

    boresight_me = r_cam_to_me @ np.array([0.0, 0.0, 1.0])
    off_nadir_deg, slant_range_km = off_nadir_and_slant_range(c_km, boresight_me)
    return c_km * 1000.0, r_cam_to_me, slant_range_km, off_nadir_deg


def look_at_rotation(boresight_me: np.ndarray, reference_r_cam_to_me: np.ndarray) -> np.ndarray:
    """Builds a new R_cam_to_me whose Z axis is exactly `boresight_me` (a unit vector, MOON_ME
    frame), with X/Y axes derived from `reference_r_cam_to_me`'s own X axis via Gram-Schmidt
    orthogonalization against the new boresight. Used by `build_camera` to re-aim the synthetic
    camera at a real, ISIS-determined target ground point (see its docstring) while keeping the
    camera's roll close to its original SPICE attitude -- changes only which direction it points,
    not how it's rolled around that direction."""
    z = boresight_me / np.linalg.norm(boresight_me)
    x_ref = reference_r_cam_to_me[:, 0]
    x = x_ref - np.dot(x_ref, z) * z
    x = x / np.linalg.norm(x)
    y = np.cross(z, x)
    return np.column_stack([x, y, z])


def rotation_about_boresight(k: int) -> np.ndarray:
    """Proper (handedness-preserving) rotation by 90*k degrees about the boresight (Z) axis. Unlike
    swapping two axes while holding the third fixed (a reflection), this keeps the boresight
    direction unchanged and just relabels which of +X/+Y/-X/-Y maps to the image's px/py axes."""
    theta = np.radians(90.0 * k)
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def ray_sphere_intersect_range(
    origin_km: np.ndarray, direction_unit: np.ndarray, moon_radius_km: float = DEFAULT_MOON_RADIUS_KM
) -> float | None:
    """Distance along `direction_unit` from `origin_km` to the Moon's mean sphere; None if it misses."""
    b = 2 * np.dot(origin_km, direction_unit)
    c = np.dot(origin_km, origin_km) - moon_radius_km**2
    disc = b * b - 4 * c
    if disc < 0:
        return None
    t = (-b - np.sqrt(disc)) / 2  # near intersection
    return t


def ground_chord_km(p1_km: np.ndarray, p2_km: np.ndarray) -> float:
    """Straight-line (chord) distance between two MOON_ME points, in km -- fine at these short
    (tens of km) distances; no need for a full geodesic/haversine calculation."""
    return float(np.linalg.norm(p1_km - p2_km))


def footprint_width_height_km(footprint_lonlat_deg: dict, moon_radius_km: float) -> tuple[float, float]:
    """Real ground (cross-track width, along-track height) of a 4-corner footprint
    (`footprint_lonlat_deg`, e.g. `Camera.footprint_lonlat_deg`), each averaged over its two edges
    -- a real ground-chord measurement of the *actual* footprint, unlike `cross_track_width_km`
    (crop-window-derived, describes the real WAC crop's own extent, not necessarily the synthetic
    render's -- see `solve_corrected_fov`'s docstring for why those two now genuinely differ)."""

    def ground_km(name: str) -> np.ndarray:
        lon, lat = footprint_lonlat_deg[name]
        return np.array(spice.latrec(moon_radius_km, np.radians(lon), np.radians(lat)))

    top_left, top_right, bottom_left, bottom_right = (
        ground_km(name) for name in ("top_left", "top_right", "bottom_left", "bottom_right")
    )
    width_km = (ground_chord_km(top_left, top_right) + ground_chord_km(bottom_left, bottom_right)) / 2.0
    height_km = (ground_chord_km(top_left, bottom_left) + ground_chord_km(top_right, bottom_right)) / 2.0
    return width_km, height_km


def boresight_ground_point_km(c_km: np.ndarray, r_cam_to_me: np.ndarray) -> np.ndarray:
    boresight_me = r_cam_to_me @ np.array([0.0, 0.0, 1.0])
    t = ray_sphere_intersect_range(c_km, boresight_me)
    assert t is not None, "camera boresight does not intersect the Moon -- pose is not looking at it"
    return c_km + t * boresight_me


def cross_track_width_km(c_km: np.ndarray, r_cam_to_me: np.ndarray, half_angle_rad: float) -> float:
    """Real cross-track ground width at this exact pose: ray-trace the +/- half-angle rays along
    the camera's x (cross-track) axis to the Moon's surface and return the distance between them."""
    left_me = r_cam_to_me @ np.array([-np.sin(half_angle_rad), 0.0, np.cos(half_angle_rad)])
    right_me = r_cam_to_me @ np.array([np.sin(half_angle_rad), 0.0, np.cos(half_angle_rad)])
    t_left = ray_sphere_intersect_range(c_km, left_me)
    t_right = ray_sphere_intersect_range(c_km, right_me)
    assert t_left is not None and t_right is not None, (
        "cross-track FOV edge ray does not intersect the Moon -- half_angle_rad too wide for this altitude"
    )
    left_ground = c_km + t_left * left_me
    right_ground = c_km + t_right * right_me
    return ground_chord_km(left_ground, right_ground)


def ground_track_step_km(frame_timing: FrameTiming, frame_index: float, n: int = 10) -> np.ndarray:
    """Real "forward in time" ground-track step vector (MOON_ME, km, not normalized): the
    boresight's ground point at `frame_index + n` minus at `frame_index`, smoothed over `n` frames.
    Measured fresh from real SPICE trajectory data rather than assumed from a fixed raw-camera axis,
    since that direction is genuinely pass/yaw-state-dependent (see `boresight_rotation_k`'s
    docstring and docs/data-sources.md, "Pass-dependent sensor axis convention"). Used both for
    `km_per_frame`'s magnitude and for `boresight_rotation_k`'s (and
    `orientation.compute_display_rotations`'s) direction."""
    c0_m, r0, _, _ = camera_pose_moon_me(frame_et(frame_timing, frame_index))
    c1_m, r1, _, _ = camera_pose_moon_me(frame_et(frame_timing, frame_index + n))
    ground0 = boresight_ground_point_km(c0_m / 1000.0, r0)
    ground1 = boresight_ground_point_km(c1_m / 1000.0, r1)
    return ground1 - ground0


def km_per_frame(frame_timing: FrameTiming, frame_index: int, n: int = 10) -> float:
    """Real ground advance per framelet, smoothed over `n` frames."""
    return ground_chord_km(np.zeros(3), ground_track_step_km(frame_timing, frame_index, n)) / n


def compute_n_frames_for_square_crop(
    frame_timing: FrameTiming, frame_index: int | None = None, config: TrntestConfig | None = None
) -> dict:
    """How many consecutive frames of the real WAC CDR (full 704-sample width) are needed so the
    along-track distance covered matches the real cross-track swath width -- i.e. a square crop
    of real ground, per the demo's objective (see docs/plan.md). Self-contained: furnishes SPICE
    kernels and computes the pose itself, so callers only need a FrameTiming."""
    config = config or load_config()
    if frame_index is None:
        frame_index = config.target_frame_index
    spice_kernels.fetch_and_furnish(frame_timing.start_time, config)
    et = frame_et(frame_timing, frame_index)
    c_meters, r_cam_to_me, _, _ = camera_pose_moon_me(et)
    half_angle_rad = np.radians(config.wac_vis_color_fov_deg / 2.0)
    w_cross_km = cross_track_width_km(c_meters / 1000.0, r_cam_to_me, half_angle_rad)
    per_frame_km = km_per_frame(frame_timing, frame_index)
    n_frames = max(1, round(w_cross_km / per_frame_km))
    return {
        "cross_track_width_km": w_cross_km,
        "km_per_frame": per_frame_km,
        "n_frames_for_square_crop": n_frames,
    }


def pixel_ray_cam(px: float, py: float, fu: float, fv: float, cu: float, cv: float) -> np.ndarray:
    v = np.array([(px - cu) / fu, (py - cv) / fv, 1.0])
    return v / np.linalg.norm(v)


def footprint_lonlat(
    c_km: np.ndarray, r_cam_to_me: np.ndarray, fu, fv, cu, cv, size: int
) -> dict[str, tuple[float, float] | None]:
    """Ground lon/lat (deg) of the image's 4 corners + center, via sphere intersection. "center" is
    the boresight ray `(cu, cv)`, not necessarily the geometric image-center pixel `(size/2, size/2)`
    -- the two coincide whenever `cu=cv=size/2` (true everywhere in this codebase before
    `solve_corrected_fov`), but diverge once the principal point is offset from center; every
    consumer of `footprint_lonlat_deg["center"]` (AOI centering, sun-angle lookups, display
    rotation) wants the real pose target, not literal image-center pixel, so this must track `(cu,
    cv)`, not a hardcoded `(size/2, size/2)`."""
    pts = {
        "center": (cu, cv),
        "top_left": (0, 0),
        "top_right": (size, 0),
        "bottom_left": (0, size),
        "bottom_right": (size, size),
    }
    out: dict[str, tuple[float, float] | None] = {}
    for name, (px, py) in pts.items():
        ray_cam = pixel_ray_cam(px, py, fu, fv, cu, cv)
        ray_me = r_cam_to_me @ ray_cam
        t = ray_sphere_intersect_range(c_km, ray_me)
        if t is None:
            out[name] = None
            continue
        ground = c_km + t * ray_me
        radius, lon, lat = spice.reclat(ground)
        out[name] = (np.degrees(lon), np.degrees(lat))
    return out


# Empirically tuned shrink factors for `solve_corrected_fov` below -- tuned on one real image
# (M1327210646CE), then cross-validated unchanged against 3 more spanning 38.5N to -67.5S, all
# reaching ~100% valid-pixel coverage. Deliberately conservative past an exact geometric fit, per
# the project owner's own call: real terrain (vs. this ray-trace's idealized sphere) varies actual
# coverage some, so a little arbitrary margin traded for reliability is fine, not something to solve
# away with a more exact model. See docs/reproject-fov-investigation.md for the full derivation.
FOV_CROSS_TRACK_SCALE = 0.93  # shrinks the cross-track half-angle -- closes a real corner-coupling overshoot
FOV_ALONG_TRACK_MARGIN = 0.93  # extra shrink on the along-track solve targets -- terrain-variation safety margin


def _corner_ground_km(
    c_km: np.ndarray, r_cam_to_me: np.ndarray, half_angle_u: float, half_angle_v: float, sign_u: float, sign_v: float
) -> np.ndarray:
    """Real ground point of an image corner ray -- cross-track and along-track angular offsets
    applied together (matching `pixel_ray_cam`'s own ray formula), not solved independently per
    axis -- see `solve_corrected_fov`'s docstring for why that distinction is the actual root cause
    this function's caller works around."""
    direction_cam = np.array([sign_u * np.tan(half_angle_u), sign_v * np.tan(half_angle_v), 1.0])
    direction_cam = direction_cam / np.linalg.norm(direction_cam)
    direction_me = r_cam_to_me @ direction_cam
    t = ray_sphere_intersect_range(c_km, direction_me)
    assert t is not None, "corner FOV ray does not intersect the Moon"
    return c_km + t * direction_me


def solve_corrected_fov(
    c_km: np.ndarray,
    r_cam_to_me: np.ndarray,
    along_track_axis_me: np.ndarray,
    cross_track_axis_me: np.ndarray,
    crop_footprint_lonlat: dict,
    config: TrntestConfig,
) -> tuple[float, float, float, float]:
    """Solve a corrected `(fu, fv, cu, cv)` whose rendered FOV stays inside the real WAC crop's own
    footprint (`crop_footprint_lonlat`, `tie_points.crop_footprint_corners_for_camera` -- real ISIS
    `campt` ground truth, not a SPICE approximation), fixing a real bug where the naive symmetric
    `fu=fv` FOV overshoots the real crop -- confirmed two coupled causes: (1) the along-track FOV
    was calibrated to a flat, non-perspective target (`n_frames_for_square_crop * km_per_frame`) but
    rendered through a real perspective (ray-sphere-intersection) projection; (2) even after fixing
    that alone, the far corners stayed elongated *cross-track* too, because a corner ray combines
    both angular offsets at once, and the more oblique that combined angle is, the farther out
    *both* ground components land -- a coupling a standard 4-parameter pinhole can't fully separate
    (`fu` can't depend on `py`). So `fu` shrinks a bit too (`FOV_CROSS_TRACK_SCALE`), not just
    `fv`/`cv`, and both are solved by ray-tracing the actual corner (`_corner_ground_km`), not a
    per-axis approximation. `cu` is left untouched (`image_size / 2.0`) -- the coverage gap this
    fixes is along-track/cross-track-coupled, not asymmetric left-right, on every real image tested
    so far. See docs/reproject-fov-investigation.md for the full derivation and cross-validation."""
    cu = config.image_size / 2.0
    boresight_ground_km = boresight_ground_point_km(c_km, r_cam_to_me)

    def decompose_km(ground_km: np.ndarray) -> tuple[float, float]:
        rel = ground_km - boresight_ground_km
        return float(np.dot(rel, cross_track_axis_me)), float(np.dot(rel, along_track_axis_me))

    along_track_km = {}
    for name, lonlat in crop_footprint_lonlat.items():
        if name == "center" or lonlat is None:
            continue
        lon, lat = lonlat
        ground_km = np.array(spice.latrec(config.moon_radius_km, np.radians(lon), np.radians(lat)))
        _, along_track_km[name] = decompose_km(ground_km)
    target_near_km = -np.mean([v for v in along_track_km.values() if v < 0])
    target_far_km = np.mean([v for v in along_track_km.values() if v >= 0])

    def solve_half_angle_v(half_angle_u: float, target_km: float, sign_v: float, hi: float = np.radians(45.0)) -> float:
        """Bisect for the along-track half-angle (radians) whose corner ray (at the given, fixed
        cross-track half-angle) lands its along-track component at `target_km` (a positive
        magnitude; `sign_v` picks near/far) -- monotonic over this range."""
        lo = 1e-4
        for _ in range(60):
            mid = (lo + hi) / 2.0
            _, at = decompose_km(_corner_ground_km(c_km, r_cam_to_me, half_angle_u, mid, 1.0, sign_v))
            magnitude = sign_v * at
            lo, hi = (mid, hi) if magnitude < target_km else (lo, mid)
        return (lo + hi) / 2.0

    original_half_angle_rad = np.radians(config.wac_vis_color_fov_deg / 2.0)
    half_angle_u = original_half_angle_rad * FOV_CROSS_TRACK_SCALE
    fu = cu / np.tan(half_angle_u)

    near_half_angle_rad = solve_half_angle_v(half_angle_u, target_near_km * FOV_ALONG_TRACK_MARGIN, sign_v=-1.0)
    far_half_angle_rad = solve_half_angle_v(half_angle_u, target_far_km * FOV_ALONG_TRACK_MARGIN, sign_v=1.0)
    fv = config.image_size / (np.tan(near_half_angle_rad) + np.tan(far_half_angle_rad))
    cv = fv * np.tan(near_half_angle_rad)
    return fu, fv, cu, cv


def write_tsai(path, c_meters, r_cam_to_me, fu, fv, cu, cv):
    lines = [
        "VERSION_4",
        "PINHOLE",
        f"fu = {fu}",
        f"fv = {fv}",
        f"cu = {cu}",
        f"cv = {cv}",
        "u_direction = 1  0  0",
        "v_direction = 0  1  0",
        "w_direction = 0  0  1",
        "C = " + " ".join(f"{x:.6f}" for x in c_meters),
        "R = " + " ".join(f"{x:.9f}" for x in r_cam_to_me.flatten()),
        "pitch = 1",
        "NULL",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def build_camera(config: TrntestConfig | None = None, output_tsai_path: str | Path | None = None) -> Camera:
    """Fetch the target frame's timing, pose the camera from real SPICE trajectory data, and write
    the resulting `.tsai` Pinhole camera file.

    **Boresight re-aiming**: the raw SPICE pose (`camera_pose_moon_me`, boresight = the nominal
    `LRO_LROCWAC_VIS` frame's Z axis) is *not* used directly as the synthetic camera's final
    attitude -- confirmed live (see docs/history.md's dated entry) that `[0, 0, 1]` in that frame is
    measurably not WAC-VIS's real optical boresight (a roughly constant ~5-6 degree real angular
    offset, persisting across a wide line range and two very different candidates -- i.e. not a
    timing/line-selection error, and not fixable by a constant rotation correction either: a
    Wahba-fit cross-check confirmed the *attitude* -- the full rotation matrix -- is already exactly
    correct, so no rotation exists that's both consistent with that and changes where `[0,0,1]`
    points without being a no-op). So instead: run the real WAC pipeline (`isis_wac.run_pipeline`,
    idempotent -- shares its output with Phase 6's own explicit call for the same product, no
    duplicated ISIS work) to get a real, camera-model-aware stitched cube, query ISIS's own real
    camera model (`isis_wac.ground_point_at_pixel`) for the real ground point at the crop's own true
    center pixel (`center_frame_index * VIS_BLOCK_HEIGHT` -- exactly `isis_wac.crop_window_for_camera`'s
    own window-center line, so this matches wherever the eventual displayed crop actually centers,
    regardless of `flip`/`camera.reverse_crop_along_track` -- window *boundaries* are computed the
    same translation-based way regardless of flip; flip only reorders *content* within them), and
    re-aims the boresight at that real target via `look_at_rotation`. A real, meaningful cost
    (~10-20s, the `lrowaccal`+`framestitch` steps `spice_kernels.fetch_and_furnish`'s default kernel
    resolution doesn't already pay for) traded for actual accuracy -- see the dated entry for the
    full investigation and why a hand-tuned constant-tilt "fix" doesn't work.

    **FOV correction**: `solve_corrected_fov` (see its own docstring) then shrinks the naive
    symmetric `fu=fv` FOV so the render stays inside the real WAC crop's own footprint -- reuses the
    same stitched cube's real crop (`tie_points.crop_footprint_corners_for_camera`, one more cheap,
    idempotent ISIS `crop` call) as its ground truth. Applied here, not only for the not-yet-built
    `reproject` product type, so `hillshade` and a future `reproject` share byte-identical
    `(fu, fv, cu, cv)` -- deliberate, for pixel-grid-identical SSIM/diff-style comparison between
    them later; `crop` (the real image) is unaffected, naturally larger, and doesn't need FOV
    parity with the other two. See docs/reproject-fov-investigation.md."""
    config = config or load_config()
    frame_timing = fetch_frame_timing(config)
    spice_kernels.fetch_and_furnish(frame_timing.start_time, config)

    # The real CDR comparison crop (Phase 5) spans n_frames frames *starting* at
    # target_frame_index, not centered on it -- so the synthetic camera's pose epoch must be the
    # crop's temporal midpoint, not its start, for the two images' centers to actually match.
    # The n_frames estimate itself barely changes over this short span, so using the start frame's
    # geometry for that estimate (not yet knowing the midpoint) isn't a meaningful circularity.
    crop_info = compute_n_frames_for_square_crop(frame_timing, config.target_frame_index, config)
    center_frame_index = config.target_frame_index + crop_info["n_frames_for_square_crop"] / 2.0
    et = frame_et(frame_timing, center_frame_index)

    c_meters, r_cam_to_me_raw, _, _ = camera_pose_moon_me(et)
    # Apply the sensor-model axis convention (see `boresight_rotation_k`'s docstring above) -- this
    # only relabels px/py against the (unchanged) boresight, it doesn't move the camera. k is
    # measured fresh from this pose's real ground-track direction, not a fixed constant. Uses the
    # raw (not yet re-aimed) pose -- k is a discrete choice between 2 axis conventions, not
    # sensitive to the ~5-6 degree re-aiming correction below.
    forward_step_km = ground_track_step_km(frame_timing, center_frame_index)
    k = boresight_rotation_k(r_cam_to_me_raw, forward_step_km)

    from trntest import isis_wac  # noqa: PLC0415 -- circular otherwise (isis_wac imports Camera/FrameTiming)

    stitched = isis_wac.run_pipeline(k == _REVERSED_TIME_K, frame_timing, config)
    center_line = center_frame_index * isis_wac.VIS_BLOCK_HEIGHT
    target_lon, target_lat = isis_wac.ground_point_at_pixel(stitched.cub_path, isis_wac.SAMPLES / 2.0, center_line)
    target_ground_km = np.array(spice.latrec(config.moon_radius_km, np.radians(target_lon), np.radians(target_lat)))
    boresight_me = target_ground_km - c_meters / 1000.0
    boresight_me = boresight_me / np.linalg.norm(boresight_me)
    off_nadir_deg, slant_range_km = off_nadir_and_slant_range(c_meters / 1000.0, boresight_me)

    r_cam_to_me_pretwist = look_at_rotation(boresight_me, r_cam_to_me_raw)
    # Pre-twist X is along-track (py, up to sign) regardless of k, per the sensor-model axis
    # convention comment near this module's top -- py maps to +-X_pretwist for either valid k, only
    # its sign differs; cross(z, x) (would-be pre-twist Y) is cross-track instead, the *other* one.
    # A unit vector, not a velocity -- see `Camera.camera_along_track_direction_moon_me`'s own note.
    along_track_direction_me = r_cam_to_me_pretwist[:, 0]
    r_cam_to_me = r_cam_to_me_pretwist @ rotation_about_boresight(k)

    if output_tsai_path is None:
        output_tsai_path = config.output_dir / f"camera_frame{config.target_frame_index}.tsai"
    output_tsai_path = Path(output_tsai_path)
    output_tsai_path.parent.mkdir(parents=True, exist_ok=True)

    # Provisional camera, naive symmetric FOV -- only its crop-window fields
    # (reverse_crop_along_track/center_frame_index/n_frames_for_square_crop) are used below, all
    # already known at this point; its own fu/fv/cu/cv are provisional, replaced by
    # solve_corrected_fov's result right after -- see that function's docstring for why the naive
    # symmetric FOV overshoots the real crop.
    half_angle_rad = np.radians(config.wac_vis_color_fov_deg / 2.0)
    provisional_fu = (config.image_size / 2.0) / np.tan(half_angle_rad)
    provisional_cu = config.image_size / 2.0
    provisional_camera = Camera(
        et=et,
        center_frame_index=center_frame_index,
        camera_center_moon_me_m=c_meters.tolist(),
        camera_along_track_direction_moon_me=along_track_direction_me.tolist(),
        r_cam_to_me=r_cam_to_me.tolist(),
        boresight_rotation_k=k,
        slant_range_km=slant_range_km,
        off_nadir_deg=off_nadir_deg,
        focal_length_u_px=provisional_fu,
        focal_length_v_px=provisional_fu,
        principal_point_u_px=provisional_cu,
        principal_point_v_px=provisional_cu,
        footprint_lonlat_deg={},
        render_cross_track_km=crop_info["cross_track_width_km"],  # provisional, replaced below
        render_along_track_km=crop_info["cross_track_width_km"],
        cross_track_width_km=crop_info["cross_track_width_km"],
        km_per_frame=crop_info["km_per_frame"],
        n_frames_for_square_crop=crop_info["n_frames_for_square_crop"],
        tsai_path=output_tsai_path,
    )

    from trntest import tie_points  # noqa: PLC0415 -- circular otherwise (tie_points imports Camera)

    crop_footprint = tie_points.crop_footprint_corners_for_camera(frame_timing, provisional_camera, config)
    cross_track_axis_me = r_cam_to_me[:, 0]
    fu, fv, cu, cv = solve_corrected_fov(
        c_meters / 1000.0, r_cam_to_me, along_track_direction_me, cross_track_axis_me, crop_footprint, config
    )

    write_tsai(output_tsai_path, c_meters, r_cam_to_me, fu, fv, cu, cv)
    footprint = footprint_lonlat(c_meters / 1000.0, r_cam_to_me, fu, fv, cu, cv, config.image_size)
    render_cross_track_km, render_along_track_km = footprint_width_height_km(footprint, config.moon_radius_km)

    return dataclasses.replace(
        provisional_camera,
        focal_length_u_px=fu,
        focal_length_v_px=fv,
        principal_point_u_px=cu,
        principal_point_v_px=cv,
        footprint_lonlat_deg=footprint,
        render_cross_track_km=render_cross_track_km,
        render_along_track_km=render_along_track_km,
    )
