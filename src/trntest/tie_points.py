"""Tie points between the synthetic sat_sim render and the real WAC CDR crop.

Both images are ultimately projections of the same real WAC-VIS camera geometry (the synthetic one
at a single fixed pose; the real crop across many real poses, one per frame -- see
docs/data-sources.md). This module finds 5 real ground points that are visible in both images (a
die's "5" pattern: 4 corners + center of the shared ground area) and projects each into both
images' pixel coordinates, so the comparison figure can show explicit, geometry-derived tie points
rather than relying on the eye to judge alignment.

Procedure (split into two stages -- see `select_tie_points`/`resolve_crop_pixels`):
1. Get each image's own ground footprint (a quadrilateral in lon/lat).
2. Find each image's own "inscribed" axis-aligned lon/lat bounding box (a box entirely inside that
   quadrilateral) -- see `inscribed_bbox` for the (deliberately approximate) method.
3. Intersect the two boxes -> the ground area both images actually cover.
4. Pick 5 points in that shared box, well clear of its edges (10% margin), in the die's "5"/X
   pattern (4 corners + center).
5. Project each point into both images' pixel coordinates:
   - synthetic image: a closed-form pinhole inverse (`project_ground_to_synthetic_pixel`), from the
     exact fixed pose that rendered it -- this is exact, not an approximation of some other "real"
     camera model, so it's untouched by the change below.
   - real WAC crop: **a real ISIS `campt` ground-to-image query** (`isis_wac.ground_to_image_pixel`,
     via `isis_wac.resolve_ground_to_image_model`) against the actual, already-produced crop cube --
     not a hand-rolled SPICE approximation. `project_ground_to_crop_pixel`/`_crop_pixel_at_frame`
     (below) are the **deprecated** predecessor: a frame-index bisection over `camera.py`'s SPICE
     pose, kept for reference/comparison only. Switched because it measurably disagreed with the
     real, cube-embedded camera model -- confirmed live on this project's actual default candidate: a
     ~92-96px (out of 994 total lines, ~10%) along-track discrepancy, and 2 of the 5 SPICE-chosen die5
     points weren't even visible to the real camera at all under its own real geometry. Steps 1-4
     above (point *selection*) are unaffected and still use the SPICE-approximate footprint -- that's
     only ever used to pick plausible candidate points, not to place them; step 5 is where accuracy
     actually matters, and that's what moved to a validated tool. Because step 5's real crop_px
     requires the real WAC crop cube to already exist (a comparatively expensive ISIS pipeline run),
     point selection (`select_tie_points`) still runs early/cheaply as before, and pixel resolution
     against the real crop (`resolve_crop_pixels`) runs later, once `isis_wac.crop_for_camera`'s
     output exists -- see the notebook for the call ordering.

Axis note (see docs/data-sources.md): empirically, the WAC-VIS camera frame's **X axis is
along-track** and **Y axis is cross-track** for LRO's actual mounted/flown orientation -- opposite
to the naive "columns=X=cross-track" assumption. This only matters here and in
`camera.compute_n_frames_for_square_crop`'s width estimate (where the resulting numeric difference
was negligible, ~0.03%, given the small off-nadir angle); the synthetic image's own pinhole
projection is axis-agnostic (it just uses the real `R` matrix directly).
"""

import numpy as np
import spiceypy as spice
from matplotlib.path import Path

from trntest import isis_wac, wac
from trntest.camera import (
    Camera,
    FrameTiming,
    camera_pose_moon_me,
    frame_et,
    ray_sphere_intersect_range,
)
from trntest.config import MOON_RADIUS_KM, TrntestConfig, load_config

CORNER_NAMES = ("top_left", "top_right", "bottom_right", "bottom_left")  # polygon winding order

# Frame-index bisection interval width below which project_ground_to_crop_pixel stops refining --
# well below one frame, so it doesn't affect the returned pixel to any visible precision.
_BISECTION_MIN_INTERVAL = 1e-7


def lonlat_to_ground_km(lon_deg: float, lat_deg: float, moon_radius_km: float = MOON_RADIUS_KM) -> np.ndarray:
    return np.array(spice.latrec(moon_radius_km, np.radians(lon_deg), np.radians(lat_deg)))


def _crop_footprint_corners_spice_approx(
    frame_timing: FrameTiming, start_frame: float, n_frames: float, half_angle_rad: float
) -> dict:
    """**Deprecated** -- superseded by `crop_footprint_corners_for_camera`'s real `campt`-based
    footprint (see its docstring for why). Kept for reference/comparison only.

    The real CDR crop's own ground footprint: ray-trace +/- half-angle along the camera's
    cross-track (Y) axis at the crop's first frame (top) and last frame (bottom)."""

    def cross_track_ground(frame_index: float, sign: float) -> tuple:
        et = frame_et(frame_timing, frame_index)
        c_m, r_cam_to_me, _, _ = camera_pose_moon_me(et)
        c_km = c_m / 1000.0
        ray_cam = np.array([0.0, sign * np.sin(half_angle_rad), np.cos(half_angle_rad)])
        ray_me = r_cam_to_me @ ray_cam
        t = ray_sphere_intersect_range(c_km, ray_me)
        assert t is not None, "crop cross-track edge ray does not intersect the Moon"
        ground = c_km + t * ray_me
        _, lon, lat = spice.reclat(ground)
        return np.degrees(lon), np.degrees(lat)

    center_et = frame_et(frame_timing, start_frame + n_frames / 2.0)
    c_m, r_cam_to_me, _, _ = camera_pose_moon_me(center_et)
    boresight_me = r_cam_to_me @ np.array([0.0, 0.0, 1.0])
    c_km = c_m / 1000.0
    t = ray_sphere_intersect_range(c_km, boresight_me)
    assert t is not None, "crop center boresight does not intersect the Moon"
    center_ground = c_km + t * boresight_me
    _, lon, lat = spice.reclat(center_ground)

    return {
        "top_left": cross_track_ground(start_frame, -1.0),
        "top_right": cross_track_ground(start_frame, 1.0),
        "bottom_left": cross_track_ground(start_frame + n_frames, -1.0),
        "bottom_right": cross_track_ground(start_frame + n_frames, 1.0),
        "center": (np.degrees(lon), np.degrees(lat)),
    }


# campt's own ground-to-image solve is confirmed, live, not to round-trip reliably within this many
# pixels of a cropped WAC cube's own edge -- image-to-ground at the cube's declared first/last pixel
# succeeds, but a ground-to-image query at that *exact* resulting lon/lat fails ("not inside cube");
# insets of 1/2/5px still failed the same way, 10/20px didn't. Used by
# crop_footprint_corners_for_camera below to keep its corner queries (and therefore anything placed
# near them, e.g. select_tie_points' die5 candidates) inside campt's own numerically stable region,
# not just the cube's nominal declared extent.
_CROP_EDGE_MARGIN_PX = 20


def crop_footprint_corners_for_camera(
    frame_timing: FrameTiming, camera: Camera, config: TrntestConfig | None = None
) -> dict:
    """The real WAC crop's own real ground footprint -- queried directly via ISIS's own camera
    model (`campt` image-to-ground, `isis_wac.ground_point_at_pixel`) at the actual cropped cube's
    own pixels, `_CROP_EDGE_MARGIN_PX` in from each edge, not the deprecated
    `_crop_footprint_corners_spice_approx`'s SPICE ray-trace.

    **Queries the cropped cube (`isis_wac.crop_for_camera`'s output), not the full stitched one, and
    not the cube's exact edge pixels either** -- both confirmed live to be real problems, not
    theoretical ones. The stitched cube: `campt` extrapolates cheerfully far beyond its own declared
    extent (confirmed separately, e.g. resolving well past line 1000 on a ~3600-line cube), so a
    footprint computed from it can claim coverage the actual *cropped* cube doesn't have -- querying
    even a die5 point placed with a 10% safety margin inside the resulting shared footprint still
    hit "no surface intersection" against the real crop. Switching to the cropped cube's own exact
    first/last pixel (`sample`/`line` = 1 and `SAMPLES`/height) didn't fully fix it either: a direct
    round-trip test found `campt`'s own ground-to-image solve doesn't reliably converge within
    `_CROP_EDGE_MARGIN_PX` pixels of the cropped cube's edge (image-to-ground at the exact edge
    pixel succeeds; ground-to-image at that *same* resulting lon/lat then fails) -- an edge-region
    numerical limitation in the tool itself, not a flaw in this project's own footprint math.

    Requires both the real WAC pipeline (`isis_wac.run_pipeline`) and the actual crop
    (`isis_wac.crop_for_camera`) -- but by the time this is called (from `select_tie_points`/
    `orientation.compute_display_rotations`/`dataset.generate_dataset`, all of which run after
    `camera.build_camera()`, which already runs `run_pipeline` internally to re-aim the synthetic
    boresight -- see that function's docstring), the stitched cube already exists (fast, idempotent
    reuse); `crop_for_camera` is a genuinely new but cheap (plain ISIS `crop`, no camera-model work)
    call the first time, then idempotently reused by Phase 6's own explicit call for the same
    product.

    See docs/history.md's dated entry for the full investigation, including the original
    ~11-15%-of-extent disagreement with the SPICE-only ray-trace this replaced."""
    config = config or load_config()
    stitched = isis_wac.run_pipeline(camera.reverse_crop_along_track, frame_timing, config)
    crop = isis_wac.crop_for_camera(stitched, camera, config)
    height = camera.n_frames_for_square_crop * isis_wac.VIS_BLOCK_HEIGHT
    m = _CROP_EDGE_MARGIN_PX

    def real_ground(sample: float, line: float) -> tuple:
        return isis_wac.ground_point_at_pixel(crop.cub_path, sample, line)

    return {
        "top_left": real_ground(1 + m, 1 + m),
        "top_right": real_ground(isis_wac.SAMPLES - m, 1 + m),
        "bottom_right": real_ground(isis_wac.SAMPLES - m, height - m),
        "bottom_left": real_ground(1 + m, height - m),
        "center": real_ground(isis_wac.SAMPLES / 2.0, height / 2.0),
    }


def project_ground_to_synthetic_pixel(ground_km, c_km, r_cam_to_me, fu, fv, cu, cv) -> tuple:
    """Closed-form pinhole inverse: the synthetic image is one fixed camera, so this is exact and
    axis-agnostic (it just uses the real R directly)."""
    v_cam = r_cam_to_me.T @ (ground_km - c_km)
    px = fu * v_cam[0] / v_cam[2] + cu
    py = fv * v_cam[1] / v_cam[2] + cv
    return px, py


def _crop_pixel_at_frame(
    frame_timing: FrameTiming,
    frame_index: float,
    start_frame: float,
    n_frames: float,
    reverse: bool,
    ground_km: np.ndarray,
    half_angle_rad: float,
) -> tuple:
    """**Deprecated** -- superseded by `isis_wac.ground_to_image_pixel` for the real WAC crop (see
    this module's docstring for why). Kept for reference/comparison only, not called by
    `select_tie_points`/`resolve_crop_pixels`.

    Cross-track column (real pinhole formula, cross-track = camera Y) + row (linear frame-to-row
    mapping) for a ground point, given the along-track-matching frame index has already been found.
    `reverse` must match `wac.fetch_vis_mosaic`'s own `camera_pose.reverse_crop_along_track` for
    this same product/pose (see its docstring) -- when the mosaic's frames were stacked in reverse
    along-track order, row must be measured from the *far* end (`start_frame + n_frames`) instead,
    not just from `start_frame`."""
    et = frame_et(frame_timing, frame_index)
    c_m, r_cam_to_me, _, _ = camera_pose_moon_me(et)
    v_cam = r_cam_to_me.T @ (ground_km - c_m / 1000.0)
    fu_real = (wac.SAMPLES / 2.0) / np.tan(half_angle_rad)
    cu_real = wac.SAMPLES / 2.0
    col = fu_real * (v_cam[1] / v_cam[2]) + cu_real
    offset = frame_index - start_frame
    row = (n_frames - offset if reverse else offset) * wac.VIS_BLOCK_HEIGHT
    return col, row


def project_ground_to_crop_pixel(
    frame_timing: FrameTiming,
    start_frame: float,
    n_frames: float,
    reverse: bool,
    ground_km: np.ndarray,
    half_angle_rad: float,
    tol: float = 1e-6,
    max_iter: int = 60,
) -> tuple:
    """**Deprecated** -- superseded by `isis_wac.ground_to_image_pixel` for the real WAC crop (see
    this module's docstring for why). Kept for reference/comparison only, not called by
    `select_tie_points`/`resolve_crop_pixels`.

    The real crop mixes many real poses (one per frame), so finding which pixel a ground point
    falls on requires locating which frame's along-track position it matches. Bisects over frame
    index for where the along-track (camera X) component crosses zero -- monotonic over this short
    span (confirmed empirically, see docs/data-sources.md). `reverse` -- see
    `_crop_pixel_at_frame` -- only affects the row this frame index maps to, not the bisection
    itself (which is purely about locating the matching frame, independent of stacking order)."""

    def along_track_tangent(frame_index: float) -> float:
        et = frame_et(frame_timing, frame_index)
        c_m, r_cam_to_me, _, _ = camera_pose_moon_me(et)
        v_cam = r_cam_to_me.T @ (ground_km - c_m / 1000.0)
        return v_cam[0] / v_cam[2]

    lo, hi = start_frame, start_frame + n_frames
    f_lo, f_hi = along_track_tangent(lo), along_track_tangent(hi)
    if abs(f_lo) < tol:
        return _crop_pixel_at_frame(frame_timing, lo, start_frame, n_frames, reverse, ground_km, half_angle_rad)
    if abs(f_hi) < tol:
        return _crop_pixel_at_frame(frame_timing, hi, start_frame, n_frames, reverse, ground_km, half_angle_rad)
    if (f_lo > 0) == (f_hi > 0):
        raise ValueError(
            f"no sign change in along-track tangent over [{lo}, {hi}] "
            f"(f_lo={f_lo}, f_hi={f_hi}) -- point may be outside the crop's along-track range"
        )
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        f_mid = along_track_tangent(mid)
        if abs(f_mid) < tol or (hi - lo) < _BISECTION_MIN_INTERVAL:
            lo = hi = mid
            break
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi, f_hi = mid, f_mid
    frame_index_solved = (lo + hi) / 2.0
    return _crop_pixel_at_frame(
        frame_timing, frame_index_solved, start_frame, n_frames, reverse, ground_km, half_angle_rad
    )


def inscribed_bbox(corners: dict, interior_point: tuple, shrink_steps: int = 40) -> tuple:
    """An approximate (not maximum-area) axis-aligned lon/lat rectangle inscribed in the polygon
    defined by `corners`: binary-searches a single isotropic shrink factor from the corners' own
    bounding box (centered at `interior_point`, which must be inside the polygon) until all 4
    shrunk-rectangle corners test as inside. There's no simple closed form for the true
    largest-inscribed-rectangle-in-a-quadrilateral; this is a deliberate, documented
    simplification, adequate for placing visualization tie points."""
    poly = Path([corners[name] for name in CORNER_NAMES])
    lons = [corners[name][0] for name in CORNER_NAMES]
    lats = [corners[name][1] for name in CORNER_NAMES]
    lon_min0, lon_max0 = min(lons), max(lons)
    lat_min0, lat_max0 = min(lats), max(lats)
    cx, cy = interior_point

    def rect_at(s: float) -> tuple:
        return (cx - s * (cx - lon_min0), cx + s * (lon_max0 - cx), cy - s * (cy - lat_min0), cy + s * (lat_max0 - cy))

    def all_corners_inside(s: float) -> bool:
        lon_min, lon_max, lat_min, lat_max = rect_at(s)
        return all(
            poly.contains_point(p)
            for p in [(lon_min, lat_min), (lon_max, lat_min), (lon_min, lat_max), (lon_max, lat_max)]
        )

    if all_corners_inside(1.0):
        return rect_at(1.0)
    lo_s, hi_s = 0.0, 1.0
    for _ in range(shrink_steps):
        mid_s = (lo_s + hi_s) / 2.0
        if all_corners_inside(mid_s):
            lo_s = mid_s
        else:
            hi_s = mid_s
    return rect_at(lo_s)


def intersect_bbox(a: tuple, b: tuple) -> tuple:
    lon_min, lon_max = max(a[0], b[0]), min(a[1], b[1])
    lat_min, lat_max = max(a[2], b[2]), min(a[3], b[3])
    assert lon_min < lon_max and lat_min < lat_max, (
        f"shared bounding box is empty: lon [{lon_min}, {lon_max}], lat [{lat_min}, {lat_max}]"
    )
    return lon_min, lon_max, lat_min, lat_max


def die5_points(bbox: tuple, center: tuple, margin_frac: float = 0.1) -> dict:
    """5 real ground points (die's-5 pattern), each placed as a `margin_frac`-shrunk offset from
    `center` towards its own corner of `bbox` (`lon_min, lon_max, lat_min, lat_max`) -- not `bbox`'s
    own naive `(lon_min+lon_max)/2` midpoint, which can drift measurably away from `center` once the
    footprint `bbox` was inscribed within is asymmetric around it. Confirmed live: a bbox-midpoint
    "center" point fell entirely outside the real WAC crop's own pushframe FOV ("no surface
    intersection") once `camera.solve_corrected_fov`'s corrected FOV made the synthetic footprint's
    own inscribed box meaningfully off-center from its true shared boresight -- the 4 corner points,
    similarly unanchored to the true center, also failed (5 of 5 tie points resolving on the demo's
    own default candidate dropped to 1 of 5). `center` must already be inside `bbox` -- true for
    `select_tie_points`'s own call (`synthetic_center`/`crop_corners["center"]` are the same real,
    shared point `inscribed_bbox` was centered on when building `bbox` in the first place), each of
    the 4 corner offsets scales that corner's own reach from `center` to `bbox`'s edge, not a single
    shared box half-width, so a footprint that's asymmetric relative to `center` (near/far corners at
    different distances, see `solve_corrected_fov`'s docstring) still keeps every point safely inside
    `bbox` on its own side."""
    lon_min, lon_max, lat_min, lat_max = bbox
    cx, cy = center
    keep = 1.0 - margin_frac
    lon_lo, lon_hi = cx - keep * (cx - lon_min), cx + keep * (lon_max - cx)
    lat_lo, lat_hi = cy - keep * (cy - lat_min), cy + keep * (lat_max - cy)
    return {
        "top_left": (lon_lo, lat_hi),
        "top_right": (lon_hi, lat_hi),
        "center": (cx, cy),
        "bottom_left": (lon_lo, lat_lo),
        "bottom_right": (lon_hi, lat_lo),
    }


def select_tie_points(frame_timing: FrameTiming, camera: Camera, config: TrntestConfig | None = None) -> dict:
    """Pick 5 real ground points visible in both images (die's-5 pattern -- see this module's
    docstring) and project each into the synthetic image's exact pixel coordinates. Requires the
    real WAC crop cube to exist (via `crop_footprint_corners_for_camera`'s real `campt`-based
    footprint, used to pick plausible candidate points, not yet to place them) -- true by the time
    this is called in practice, since `camera.build_camera()` already ran the real WAC pipeline
    internally (see its docstring), so this doesn't add new expensive ISIS work. Call
    `resolve_crop_pixels` once the real crop is cropped (`isis_wac.crop_for_camera`'s output) to
    fill in each point's real `crop_px`.

    Returns {name: {"lonlat": (lon, lat), "synthetic_px": (px, py)}} -- no "crop_px" yet."""
    config = config or load_config()

    # Use the exact (already boresight-rotated, per the fixed sensor-model convention) C/R that
    # build_camera() wrote into the .tsai -- not a fresh, unrotated camera_pose_moon_me() call.
    c_km = np.array(camera.camera_center_moon_me_m) / 1000.0
    r_cam_to_me = np.array(camera.r_cam_to_me)
    fu, fv = camera.focal_length_u_px, camera.focal_length_v_px
    cu, cv = camera.principal_point_u_px, camera.principal_point_v_px

    synthetic_corners = camera.footprint_lonlat_deg
    crop_corners = crop_footprint_corners_for_camera(frame_timing, camera, config)

    synthetic_center = synthetic_corners["center"]
    assert synthetic_center is not None, "synthetic camera's own boresight does not intersect the Moon"
    inscribed_synthetic = inscribed_bbox(synthetic_corners, synthetic_center)
    inscribed_crop = inscribed_bbox(crop_corners, crop_corners["center"])
    shared_bbox = intersect_bbox(inscribed_synthetic, inscribed_crop)

    points = die5_points(shared_bbox, synthetic_center)

    results = {}
    for name, (lon, lat) in points.items():
        ground_km = lonlat_to_ground_km(lon, lat)
        px, py = project_ground_to_synthetic_pixel(ground_km, c_km, r_cam_to_me, fu, fv, cu, cv)

        image_size = config.image_size
        assert 0 <= px < image_size and 0 <= py < image_size, f"tie point {name} outside synthetic image: ({px}, {py})"

        results[name] = {"lonlat": (lon, lat), "synthetic_px": (px, py)}

    return results


def resolve_crop_pixels(tie_points: dict, model: "isis_wac.GroundToImageModel") -> dict:
    """Fill in each selected tie point's real `crop_px`, via a genuine ISIS `campt` ground-to-image
    query against the actual WAC crop (`model`, from `isis_wac.resolve_ground_to_image_model`) -- see
    this module's docstring for why this replaced the deprecated SPICE-only projection below.
    Converts ISIS's 1-based, pixel-center `Sample`/`Line` convention to this project's existing
    0-based, pixel-corner convention (`- 0.5`, matching `project_ground_to_synthetic_pixel`'s
    `cu = image_size / 2.0`-style pinhole formulas) so both images' tie points plot consistently.

    Points the real camera doesn't actually see are dropped (with a printed warning), not raised --
    confirmed live this happens for real, plausible die5 points (`select_tie_points`'s footprint
    estimate is only ever approximate, see the module docstring); only raises if *none* of the points
    resolve, since that would mean something is fundamentally wrong, not just an edge case in the
    approximate footprint."""
    resolved = {}
    dropped = []
    for name, info in tie_points.items():
        lon, lat = info["lonlat"]
        pixel = isis_wac.ground_to_image_pixel(model, lon, lat)
        if pixel is None:
            dropped.append(name)
            continue
        sample, line = pixel
        resolved[name] = {**info, "crop_px": (sample - 0.5, line - 0.5)}

    if not resolved:
        raise RuntimeError(
            "none of the selected tie points project into the real WAC crop -- "
            "select_tie_points's approximate footprint may be badly wrong for this candidate"
        )
    if dropped:
        print(
            f"tie_points.resolve_crop_pixels: {len(dropped)} of {len(tie_points)} tie point(s) don't "
            f"project into the real WAC crop under its actual camera model, dropped: {dropped}"
        )
    return resolved
