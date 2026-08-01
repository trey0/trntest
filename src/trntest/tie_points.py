"""SPICE-derived tie points between the synthetic sat_sim render and the real WAC CDR crop.

Both images are ultimately projections of the same real WAC-VIS camera geometry (the synthetic one
at a single fixed pose; the real crop across many real poses, one per frame -- see
docs/data-sources.md). This module finds 5 real ground points that are visible in both images (a
die's "5" pattern: 4 corners + center of the shared ground area) and projects each into both
images' pixel coordinates, so the comparison figure can show explicit, geometry-derived tie points
rather than relying on the eye to judge alignment.

Procedure:
1. Get each image's own ground footprint (a quadrilateral in lon/lat).
2. Find each image's own "inscribed" axis-aligned lon/lat bounding box (a box entirely inside that
   quadrilateral) -- see `inscribed_bbox` for the (deliberately approximate) method.
3. Intersect the two boxes -> the ground area both images actually cover.
4. Pick 5 points in that shared box, well clear of its edges (10% margin), in the die's "5"/X
   pattern (4 corners + center).
5. Project each point into both images' pixel coordinates using their real camera models: a
   closed-form pinhole inverse for the synthetic image (single fixed pose); a small root-find over
   frame index for the real crop (it mixes many real poses, one per frame -- see
   `project_ground_to_crop_pixel`).

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

from trntest import wac
from trntest.camera import (
    Camera,
    FrameTiming,
    camera_pose_moon_me,
    frame_et,
    ray_sphere_intersect_range,
)
from trntest.config import DEFAULT_MOON_RADIUS_KM, TrntestConfig, load_config

CORNER_NAMES = ("top_left", "top_right", "bottom_right", "bottom_left")  # polygon winding order

# Frame-index bisection interval width below which project_ground_to_crop_pixel stops refining --
# well below one frame, so it doesn't affect the returned pixel to any visible precision.
_BISECTION_MIN_INTERVAL = 1e-7


def lonlat_to_ground_km(lon_deg: float, lat_deg: float, moon_radius_km: float = DEFAULT_MOON_RADIUS_KM) -> np.ndarray:
    return np.array(spice.latrec(moon_radius_km, np.radians(lon_deg), np.radians(lat_deg)))


def crop_footprint_corners(
    frame_timing: FrameTiming, start_frame: float, n_frames: float, half_angle_rad: float
) -> dict:
    """The real CDR crop's own ground footprint: ray-trace +/- half-angle along the camera's
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
    """Cross-track column (real pinhole formula, cross-track = camera Y) + row (linear frame-to-row
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
    """The real crop mixes many real poses (one per frame), so finding which pixel a ground point
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


def die5_points(bbox: tuple, margin_frac: float = 0.1) -> dict:
    lon_min, lon_max, lat_min, lat_max = bbox
    w, h = lon_max - lon_min, lat_max - lat_min
    lon_lo, lon_hi = lon_min + margin_frac * w, lon_max - margin_frac * w
    lat_lo, lat_hi = lat_min + margin_frac * h, lat_max - margin_frac * h
    return {
        "top_left": (lon_lo, lat_hi),
        "top_right": (lon_hi, lat_hi),
        "center": ((lon_min + lon_max) / 2.0, (lat_min + lat_max) / 2.0),
        "bottom_left": (lon_lo, lat_lo),
        "bottom_right": (lon_hi, lat_lo),
    }


def compute_tie_points(frame_timing: FrameTiming, camera: Camera, config: TrntestConfig | None = None) -> dict:
    """Returns {name: {"lonlat": (lon, lat), "synthetic_px": (px, py), "crop_px": (col, row)}}."""
    config = config or load_config()
    half_angle_rad = np.radians(config.wac_vis_color_fov_deg / 2.0)
    n_frames = camera.n_frames_for_square_crop

    # Use the exact (already boresight-rotated, per the fixed sensor-model convention) C/R that
    # build_camera() wrote into the .tsai -- not a fresh, unrotated camera_pose_moon_me() call.
    c_km = np.array(camera.camera_center_moon_me_m) / 1000.0
    r_cam_to_me = np.array(camera.r_cam_to_me)
    fu = fv = camera.focal_length_px
    cu = cv = config.image_size / 2.0

    synthetic_corners = camera.footprint_lonlat_deg
    crop_corners = crop_footprint_corners(frame_timing, config.target_frame_index, n_frames, half_angle_rad)

    synthetic_center = synthetic_corners["center"]
    assert synthetic_center is not None, "synthetic camera's own boresight does not intersect the Moon"
    inscribed_synthetic = inscribed_bbox(synthetic_corners, synthetic_center)
    inscribed_crop = inscribed_bbox(crop_corners, crop_corners["center"])
    shared_bbox = intersect_bbox(inscribed_synthetic, inscribed_crop)

    points = die5_points(shared_bbox)

    results = {}
    for name, (lon, lat) in points.items():
        ground_km = lonlat_to_ground_km(lon, lat, config.moon_radius_km)
        px, py = project_ground_to_synthetic_pixel(ground_km, c_km, r_cam_to_me, fu, fv, cu, cv)
        col, row = project_ground_to_crop_pixel(
            frame_timing,
            config.target_frame_index,
            n_frames,
            camera.reverse_crop_along_track,
            ground_km,
            half_angle_rad,
        )

        image_size = config.image_size
        assert 0 <= px < image_size and 0 <= py < image_size, f"tie point {name} outside synthetic image: ({px}, {py})"
        assert 0 <= col < wac.SAMPLES and 0 <= row < n_frames * wac.VIS_BLOCK_HEIGHT, (
            f"tie point {name} outside CDR crop: ({col}, {row})"
        )

        results[name] = {"lonlat": (lon, lat), "synthetic_px": (px, py), "crop_px": (col, row)}

    return results
