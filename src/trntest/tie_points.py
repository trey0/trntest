"""Tie points between the synthetic sat_sim render and the real WAC CDR crop.

Both images project the same WAC-VIS camera geometry -- the synthetic one at a single fixed pose,
the real crop across many poses, one per frame. Finds 5 ground points shared by both (a die's-5
pattern: 4 corners + center) and projects each into both images' pixel coordinates, so a comparison
figure can show explicit, geometry-derived tie points instead of relying on the eye to judge
alignment.

Split into two stages: `select_tie_points` (cheap, runs early -- see its own docstring) picks the
points and projects them into the synthetic image; `resolve_crop_pixels` (needs the real crop cube
to exist) projects them into the real one.
"""
# Point selection (select_tie_points) does its box-inscribing/intersection/placement geometry
# (inscribed_bbox/intersect_bbox/die5_points) in a shared local Orthographic frame (meters, see
# _footprint_to_local_m), not raw lon/lat degrees: a raw-degree axis-aligned box is badly distorted
# near the poles, where a degree of longitude covers a shrinking distance, and this measurably
# dropped die5 points there. inscribed_bbox/intersect_bbox/die5_points are themselves pure
# planar-geometry functions with no lon/lat-specific logic.
#
# Axis note: the WAC-VIS camera frame's X axis is along-track and Y axis is cross-track for LRO's
# actual mounted/flown orientation -- opposite the naive "columns=X=cross-track" assumption. This
# also matters in camera.compute_n_frames_for_square_crop's width estimate; the synthetic image's
# own pinhole projection is axis-agnostic (it uses the R matrix directly).

import warnings

import numpy as np
import rasterio
import rasterio.errors
import rasterio.warp
import spiceypy as spice
from matplotlib.path import Path

from trntest import isis_wac, lunaserv, wac, wac_camera_model
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
    """Longitude/latitude (degrees) -> Moon-fixed ground point (km)."""
    return np.array(spice.latrec(moon_radius_km, np.radians(lon_deg), np.radians(lat_deg)))


def _crop_footprint_corners_spice_approx(
    frame_timing: FrameTiming, start_frame: float, n_frames: float, half_angle_rad: float
) -> dict:
    """**Deprecated** -- superseded by `crop_footprint_corners_for_camera`'s `campt`-based footprint.
    Kept for reference/comparison only.

    Ray-traces +/- `half_angle_rad` along the camera's cross-track (Y) axis at the crop's first frame
    (top) and last frame (bottom) to approximate the CDR crop's ground footprint.

    :returns: dict of `{corner_name: (lon_deg, lat_deg)}`, keys from `CORNER_NAMES` plus `"center"`.
    """

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
    """The WAC crop's ground footprint, queried via ISIS's own camera model (`campt` image-to-ground,
    `isis_wac.ground_point_at_pixel`) at the cropped cube's pixels, `_CROP_EDGE_MARGIN_PX` in from
    each edge -- not the deprecated `_crop_footprint_corners_spice_approx`'s SPICE ray-trace.

    Requires `isis_wac.run_pipeline` and `isis_wac.crop_for_camera`'s output to exist.

    :returns: dict of `{corner_name: (lon_deg, lat_deg)}`, keys from `CORNER_NAMES` plus `"center"`.
    """
    # Queries the cropped cube, not the stitched one, and not the cube's exact edge pixels: campt
    # extrapolates past the stitched cube's own declared extent, so a footprint from it can claim
    # coverage the cropped cube doesn't have. campt's ground-to-image solve also doesn't reliably
    # converge within _CROP_EDGE_MARGIN_PX of the cropped cube's edge (image-to-ground at the exact
    # edge pixel succeeds; ground-to-image at that same resulting lon/lat then fails) -- an
    # edge-region numerical limitation in the tool itself.
    #
    # By the time this runs (from select_tie_points/orientation.compute_display_rotations/
    # dataset.generate_dataset, all after camera.build_camera(), which already runs run_pipeline
    # internally to re-aim the synthetic boresight), the stitched cube already exists; crop_for_camera
    # is a cheap plain ISIS `crop`, idempotently reused if already run for this product.
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
    axis-agnostic (it uses `r_cam_to_me` directly)."""
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
    """**Deprecated** -- superseded by `isis_wac.ground_to_image_pixel`. Kept for reference/comparison
    only, not called by `select_tie_points`/`resolve_crop_pixels`.

    Cross-track column (pinhole formula, cross-track = camera Y) + row (linear frame-to-row mapping)
    for a ground point, given the along-track-matching frame index has already been found.

    :param reverse: must match `wac.fetch_vis_mosaic`'s `camera_pose.reverse_crop_along_track` for
        this product/pose -- when the mosaic's frames were stacked in reverse along-track order, row
        is measured from the far end (`start_frame + n_frames`) instead of `start_frame`.
    """
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
    """**Deprecated** -- superseded by `isis_wac.ground_to_image_pixel`. Kept for reference/comparison
    only, not called by `select_tie_points`/`resolve_crop_pixels`.

    The crop mixes many poses (one per frame), so finding which pixel a ground point falls on
    requires locating which frame's along-track position it matches. Bisects over frame index for
    where the along-track (camera X) component crosses zero, then resolves pixel coordinates via
    `_crop_pixel_at_frame`.

    :raises ValueError: if the along-track tangent doesn't change sign over
        `[start_frame, start_frame + n_frames]`.
    """
    # Along-track tangent is monotonic over this short span (confirmed empirically). `reverse` only
    # affects the row _crop_pixel_at_frame maps the solved frame index to, not this bisection, which
    # is purely about locating the matching frame independent of stacking order.

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
    """Approximate (not maximum-area) axis-aligned rectangle inscribed in the polygon defined by
    `corners`: binary-searches a single isotropic shrink factor from the corners' bounding box
    (centered on `interior_point`, which must be inside the polygon) until all 4 shrunk corners test
    as inside.

    Purely planar -- `select_tie_points` feeds it local Orthographic meters, not lon/lat degrees (see
    the module comment)."""
    # No closed form for the true largest-inscribed-rectangle-in-a-quadrilateral; this is a
    # deliberate simplification, adequate for placing visualization tie points.
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
    """Intersection of two `(lon_min, lon_max, lat_min, lat_max)` boxes.

    :raises AssertionError: if the boxes don't overlap.
    """
    lon_min, lon_max = max(a[0], b[0]), min(a[1], b[1])
    lat_min, lat_max = max(a[2], b[2]), min(a[3], b[3])
    assert lon_min < lon_max and lat_min < lat_max, (
        f"shared bounding box is empty: lon [{lon_min}, {lon_max}], lat [{lat_min}, {lat_max}]"
    )
    return lon_min, lon_max, lat_min, lat_max


def die5_points(bbox: tuple, center: tuple, margin_frac: float = 0.1) -> dict:
    """5 ground points in a die's-5 pattern, each placed as a `margin_frac`-shrunk offset from
    `center` towards its own corner of `bbox` (`lon_min, lon_max, lat_min, lat_max`) -- not `bbox`'s
    own midpoint, which can drift from `center` when the footprint `bbox` was inscribed within is
    asymmetric around it.

    :param center: must already be inside `bbox`.
    """
    # A bbox-midpoint "center" (instead of the true shared boresight) fell outside the WAC crop's
    # pushframe FOV once solve_corrected_fov's corrected FOV made the footprint asymmetric around it
    # -- the 4 corner points, similarly unanchored, failed too (5/5 resolving tie points dropped to
    # 1/5). Scaling each corner offset from its own reach to bbox's edge, rather than a single shared
    # box half-width, keeps every point safely inside bbox even when it's asymmetric relative to
    # center (near/far corners at different distances, see solve_corrected_fov's docstring).
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


def _footprint_to_local_m(corners: dict, center_lon_deg: float, center_lat_deg: float) -> dict:
    """Projects a `{name: (lon_deg, lat_deg)}` footprint dict (e.g. `camera.footprint_lonlat_deg`,
    `crop_footprint_corners_for_camera`'s return) into a local Orthographic frame (meters) centered
    on `(center_lon_deg, center_lat_deg)`, via `rasterio.warp.transform` -- the same tool
    `control_network.map_points_to_lonlat` uses for the point-wise (not bbox) case."""
    names = list(corners)
    lons = [corners[n][0] for n in names]
    lats = [corners[n][1] for n in names]
    ortho_crs = lunaserv.local_orthographic_crs(center_lon_deg, center_lat_deg)
    xs, ys = rasterio.warp.transform(lunaserv.geographic_crs(), ortho_crs, lons, lats)
    return dict(zip(names, zip(xs, ys, strict=True), strict=True))


def _local_m_to_lonlat(points_m: dict, center_lon_deg: float, center_lat_deg: float) -> dict:
    """Inverse of `_footprint_to_local_m`: local Orthographic meters -> `(lon_deg, lat_deg)`, in this
    project's 0-360 Positive-East convention.

    `rasterio.warp.transform` returns longitude in -180..180 regardless of the destination CRS's own
    definition; normalized here via `% 360.0`, the same fix-up `control_network.map_points_to_lonlat`
    applies."""
    names = list(points_m)
    xs = [points_m[n][0] for n in names]
    ys = [points_m[n][1] for n in names]
    ortho_crs = lunaserv.local_orthographic_crs(center_lon_deg, center_lat_deg)
    lons, lats = rasterio.warp.transform(ortho_crs, lunaserv.geographic_crs(), xs, ys)
    return {n: (lon % 360.0, lat) for n, lon, lat in zip(names, lons, lats, strict=True)}


def select_tie_points(frame_timing: FrameTiming, camera: Camera, config: TrntestConfig | None = None) -> dict:
    """Pick 5 ground points visible in both images (die's-5 pattern -- see the module docstring) and
    project each into the synthetic image's pixel coordinates. Requires the WAC crop cube to exist.

    Call `resolve_crop_pixels` once the crop is cropped (`isis_wac.crop_for_camera`'s output) to fill
    in each point's `crop_px`.

    :returns: `{name: {"lonlat": (lon, lat), "synthetic_px": (px, py)}}` -- no `crop_px` yet.
    """
    # camera.build_camera() already runs the WAC pipeline internally to re-aim the synthetic
    # boresight, so requiring the crop cube here (via crop_footprint_corners_for_camera) doesn't add
    # new expensive ISIS work.
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
    center_lon_deg, center_lat_deg = synthetic_center

    # Do the box-inscribing/intersection/placement geometry in local isotropic meters, not raw
    # lon/lat degrees (see the module comment for why). Both footprints share one local frame,
    # centered on the synthetic camera's own boresight ground point, so `intersect_bbox` compares
    # like with like; `synthetic_corners_m["center"]` is that frame's own origin, (0.0, 0.0).
    synthetic_corners_m = _footprint_to_local_m(synthetic_corners, center_lon_deg, center_lat_deg)
    crop_corners_m = _footprint_to_local_m(crop_corners, center_lon_deg, center_lat_deg)

    inscribed_synthetic_m = inscribed_bbox(synthetic_corners_m, synthetic_corners_m["center"])
    inscribed_crop_m = inscribed_bbox(crop_corners_m, crop_corners_m["center"])
    shared_bbox_m = intersect_bbox(inscribed_synthetic_m, inscribed_crop_m)

    points_m = die5_points(shared_bbox_m, synthetic_corners_m["center"])
    points = _local_m_to_lonlat(points_m, center_lon_deg, center_lat_deg)

    results = {}
    for name, (lon, lat) in points.items():
        ground_km = lonlat_to_ground_km(lon, lat)
        px, py = project_ground_to_synthetic_pixel(ground_km, c_km, r_cam_to_me, fu, fv, cu, cv)

        image_size = config.image_size
        assert 0 <= px < image_size and 0 <= py < image_size, f"tie point {name} outside synthetic image: ({px}, {py})"

        results[name] = {"lonlat": (lon, lat), "synthetic_px": (px, py)}

    return results


def resolve_crop_pixels(tie_points: dict, crop: "isis_wac.CropResult", config: TrntestConfig | None = None) -> dict:
    """Fill in each selected tie point's `crop_px`, via `wac_camera_model.find_framelet_and_project`
    -- validated to exact (0.000px) agreement with ISIS `campt` output.

    :raises RuntimeError: if a tie point doesn't project into the crop under its own camera model --
        `select_tie_points` places every candidate inside the shared FOV's local-meters inscribed
        box, so this means something is fundamentally wrong, not an expected case.
    """
    # Used instead of isis_wac.ground_to_image_pixel/resolve_ground_to_image_model's campt-based
    # query because campt's own ground-to-image solve has a ~38% failure rate for WAC's Pushframe
    # sensor on this project's default candidate -- a known upstream ISIS bug
    # (PushFrameCameraGroundMap::GetLocalNormal, DOI-USGS/ISIS3#4256), not an edge-of-crop artifact.
    # find_framelet_and_project sidesteps it with a from-scratch containment check instead of ISIS's
    # own buggy solve. See docs/wac-jigsaw-investigation.md for the full investigation.
    #
    # Converts (sample, line) from ISIS's 1-based, pixel-center convention to this project's 0-based,
    # pixel-corner convention (- 0.5), matching project_ground_to_synthetic_pixel's pinhole formulas,
    # so both images' tie points plot consistently. No PoseCorrection is applied -- this uses the
    # existing SPICE-derived pose, not a1's fitted correction (a separate, opt-in refinement, see
    # isis_wac.apply_pose_correction_to_crop).
    config = config or load_config()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        with rasterio.open(crop.cub_path) as src:
            n_lines = src.height
    n_framelets = n_lines // wac_camera_model.FRAMELET_HEIGHT
    et0, et_per_line = wac_camera_model.calibrate_et_per_crop_line(crop.cub_path, n_lines)

    resolved = {}
    for name, info in tie_points.items():
        lon, lat = info["lonlat"]
        ground_me_m = lonlat_to_ground_km(lon, lat) * 1000.0
        pixel = wac_camera_model.find_framelet_and_project(ground_me_m, n_framelets, et0, et_per_line)
        if pixel is None:
            raise RuntimeError(
                f"tie point {name!r} at (lon={lon}, lat={lat}) doesn't project into the real WAC "
                "crop under its actual camera model -- select_tie_points places every point inside "
                "the shared FOV's own local-meters inscribed box, so this means something is "
                "fundamentally wrong, not an expected edge case"
            )
        sample, line = pixel
        resolved[name] = {**info, "crop_px": (sample - 0.5, line - 0.5)}
    return resolved
