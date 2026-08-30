"""Display-only north-up rotation for the comparison figure -- rotates already-rendered/extracted
image arrays (and remaps tie-point plot coordinates to match) so north is approximately "up" in the
notebook's comparison figure. Never touches the sensor model, the `.tsai`, or the crop-extraction
code. See docs/image-pipeline.md's "North-up display rotation" section for the full design.
"""
# Kept strictly separate from the synthetic-camera axis convention in `camera.py`: which way is
# "north" varies by pass (ascending vs. descending) and shouldn't influence the sensor model itself,
# even though both this module and `camera.boresight_rotation_k` independently need the same per-pass
# "forward in time" ground-track direction (from `camera.ground_track_step_km`) to get their own,
# separate rotation choices right. See docs/data-sources/lroc-wac-edr-cdr.md's "Pass-dependent sensor
# axis convention" for why that direction is pass-dependent in the first place.

import dataclasses

import numpy as np

from trntest import tie_points
from trntest.camera import Camera, FrameTiming, camera_pose_moon_me, frame_et, ground_track_step_km
from trntest.config import TrntestConfig, load_config


@dataclasses.dataclass(frozen=True)
class DisplayRotations:
    """Multiple-of-90-degree `np.rot90` rotation (and residual deviation from true north) to apply
    to each image for display, as computed by `compute_display_rotations`."""

    k_synthetic: int
    dev_synthetic_deg: float
    k_crop: int
    dev_crop_deg: float


def north_tangent_km(ground_km: np.ndarray) -> np.ndarray:
    """Local north-pointing tangent unit vector at a MOON_ME point.

    :param ground_km: Ground point, body-fixed km.
    :returns: Unit vector toward increasing latitude.
    """
    p_hat = ground_km / np.linalg.norm(ground_km)
    polar = np.array([0.0, 0.0, 1.0])
    north = polar - np.dot(polar, p_hat) * p_hat
    return north / np.linalg.norm(north)


def best_k_for_north_up(right_orig: np.ndarray, up_orig: np.ndarray, north: np.ndarray, candidates=(0, 1, 2, 3)):
    """Among `candidates`, pick the 90-degree-multiple `np.rot90` rotation whose resulting on-screen
    "up" direction is closest to true north.

    :param right_orig: The original (unrotated) array's screen-right direction, real-world unit
        vector.
    :param up_orig: The original (unrotated) array's screen-up direction, real-world unit vector.
    :param north: True north direction at the same point, real-world unit vector.
    :param candidates: `np.rot90` `k` values to consider.
    :returns: `(best_k, deviation_deg)` -- the angular deviation of that best "up" from true north.
    """
    # Rotating the displayed array by `np.rot90(arr, k)` physically rotates the image k*90 degrees
    # counter-clockwise; the new "up" direction (in the original right/up basis) is then
    # `sin(k*90deg)*right_orig + cos(k*90deg)*up_orig` -- verified against `np.rot90` directly (see
    # docs/image-pipeline.md's "North-up display rotation" section).
    best_k, best_dot, best_dev = candidates[0], -2.0, 180.0
    for k in candidates:
        theta = np.radians(90.0 * k)
        new_up = np.sin(theta) * right_orig + np.cos(theta) * up_orig
        dot = np.clip(np.dot(new_up, north), -1.0, 1.0)
        if dot > best_dot:
            best_dot, best_k, best_dev = dot, k, np.degrees(np.arccos(dot))
    return best_k, best_dev


def rotate_pixel_coords(col: float, row: float, k: int, height: int, width: int) -> tuple:
    """Where `(col, row)` in an original `height x width` array lands after `np.rot90(arr, k)`.

    :param col: Original column.
    :param row: Original row.
    :param k: `np.rot90` rotation count.
    :param height: Original array height.
    :param width: Original array width.
    :returns: `(col, row)` in the rotated array.
    """
    # Verified numerically against `np.rot90` directly (see docs/image-pipeline.md's "North-up
    # display rotation" section).
    k = k % 4
    h, w = height, width
    for _ in range(k):
        col, row, h, w = row, (w - 1) - col, w, h
    return col, row


def compute_display_rotations(
    camera: Camera, frame_timing: FrameTiming, config: TrntestConfig | None = None
) -> DisplayRotations:
    """For each image (synthetic render, WAC CDR crop) independently, pick the multiple of 90
    degrees whose on-screen "up" is closest to true north.

    :param camera: The synthetic camera for this pose.
    :param frame_timing: Frame timing for the same product.
    :param config: Project config; `load_config()` if not given.
    :returns: The chosen rotations, for display only (see the module docstring).
    """
    config = config or load_config()

    _, r_crop_raw, _, _ = camera_pose_moon_me(frame_et(frame_timing, camera.center_frame_index))
    crop_corners = tie_points.crop_footprint_corners_for_camera(frame_timing, camera, config)

    r_synthetic = np.array(camera.r_cam_to_me)
    synthetic_center = camera.footprint_lonlat_deg["center"]
    assert synthetic_center is not None, "synthetic camera's own boresight does not intersect the Moon"
    synthetic_center_lon, synthetic_center_lat = synthetic_center
    north_synthetic = north_tangent_km(tie_points.lonlat_to_ground_km(synthetic_center_lon, synthetic_center_lat))
    k_synthetic, dev_synthetic = best_k_for_north_up(
        r_synthetic[:, 0], -r_synthetic[:, 1], north_synthetic, candidates=(0, 1, 2, 3)
    )

    crop_center_lon, crop_center_lat = crop_corners["center"]
    north_crop = north_tangent_km(tie_points.lonlat_to_ground_km(crop_center_lon, crop_center_lat))
    # "Up" for k=0 (row 0 at the top) is backward in time when `wac.fetch_vis_mosaic` stacked frames
    # in their natural order, but forward in time when it stacked them in reverse
    # (`camera.reverse_crop_along_track`) -- must track whichever that module actually did for this
    # pass, not a fixed raw-camera-axis assumption (pass/yaw-dependent, not hardware-fixed -- see
    # `camera.boresight_rotation_k`'s docstring and docs/data-sources/lroc-wac-edr-cdr.md's
    # "Pass-dependent sensor axis convention").
    forward_step_km = ground_track_step_km(frame_timing, camera.center_frame_index)
    forward_in_time = forward_step_km / np.linalg.norm(forward_step_km)
    up_orig = forward_in_time if camera.reverse_crop_along_track else -forward_in_time
    k_crop, dev_crop = best_k_for_north_up(r_crop_raw[:, 1], up_orig, north_crop, candidates=(0, 2))

    return DisplayRotations(
        k_synthetic=k_synthetic,
        dev_synthetic_deg=dev_synthetic,
        k_crop=k_crop,
        dev_crop_deg=dev_crop,
    )
