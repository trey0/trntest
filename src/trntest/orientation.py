"""Display-only north-up rotation for the comparison figure.

Purely cosmetic: rotates already-rendered/extracted image arrays (and remaps tie-point plot
coordinates to match) so north is approximately "up" in the notebook's comparison figure. Does
**not** touch the sensor model, the `.tsai`, or the crop-extraction code -- kept strictly separate
from the fixed synthetic-camera axis convention in `camera.py` (see docs/data-sources.md for why:
the sensor model's convention must be a fixed, hardware-motivated choice, while which way is "north"
varies by pass -- ascending vs. descending -- and shouldn't influence the model at all).
"""

import dataclasses

import numpy as np

from trntest import tie_points
from trntest.camera import Camera, FrameTiming, camera_pose_moon_me, frame_et
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
    """Local north-pointing tangent unit vector (toward increasing latitude) at a MOON_ME point."""
    p_hat = ground_km / np.linalg.norm(ground_km)
    polar = np.array([0.0, 0.0, 1.0])
    north = polar - np.dot(polar, p_hat) * p_hat
    return north / np.linalg.norm(north)


def best_k_for_north_up(right_orig: np.ndarray, up_orig: np.ndarray, north: np.ndarray, candidates=(0, 1, 2, 3)):
    """Among `candidates` (90-degree-multiple `np.rot90` rotations of the array), pick the one whose
    resulting on-screen "up" direction is closest to true north. `right_orig`/`up_orig` are the
    *original* (unrotated) array's screen-right/screen-up directions, as real-world unit vectors.

    Rotating the displayed array by `np.rot90(arr, k)` physically rotates the image k*90 degrees
    counter-clockwise; the new "up" direction (in the original right/up basis) is then
    `sin(k*90deg)*right_orig + cos(k*90deg)*up_orig` -- verified against `np.rot90` directly (see
    docs/data-sources.md).

    Returns (best_k, deviation_deg) -- the angular deviation of that best "up" from true north.
    """
    best_k, best_dot, best_dev = candidates[0], -2.0, 180.0
    for k in candidates:
        theta = np.radians(90.0 * k)
        new_up = np.sin(theta) * right_orig + np.cos(theta) * up_orig
        dot = np.clip(np.dot(new_up, north), -1.0, 1.0)
        if dot > best_dot:
            best_dot, best_k, best_dev = dot, k, np.degrees(np.arccos(dot))
    return best_k, best_dev


def rotate_pixel_coords(col: float, row: float, k: int, height: int, width: int) -> tuple:
    """Where (col, row) in an original height x width array lands after `np.rot90(arr, k)`.
    Verified numerically against `np.rot90` directly (see docs/data-sources.md)."""
    k = k % 4
    h, w = height, width
    for _ in range(k):
        col, row, h, w = row, (w - 1) - col, w, h
    return col, row


def compute_display_rotations(
    camera: Camera, frame_timing: FrameTiming, config: TrntestConfig | None = None
) -> DisplayRotations:
    """For each image (synthetic render, real WAC CDR crop) independently, pick the multiple of 90
    degrees whose on-screen "up" is closest to true north -- for display only, see module docstring."""
    config = config or load_config()
    half_angle_rad = np.radians(config.wac_vis_color_fov_deg / 2.0)
    n_frames = camera.n_frames_for_square_crop

    _, r_crop_raw, _, _ = camera_pose_moon_me(frame_et(frame_timing, camera.center_frame_index))
    crop_corners = tie_points.crop_footprint_corners(frame_timing, config.target_frame_index, n_frames, half_angle_rad)

    r_synthetic = np.array(camera.r_cam_to_me)
    synthetic_center = camera.footprint_lonlat_deg["center"]
    assert synthetic_center is not None, "synthetic camera's own boresight does not intersect the Moon"
    synthetic_center_lon, synthetic_center_lat = synthetic_center
    north_synthetic = north_tangent_km(
        tie_points.lonlat_to_ground_km(synthetic_center_lon, synthetic_center_lat, config.moon_radius_km)
    )
    k_synthetic, dev_synthetic = best_k_for_north_up(
        r_synthetic[:, 0], -r_synthetic[:, 1], north_synthetic, candidates=(0, 1, 2, 3)
    )

    crop_center_lon, crop_center_lat = crop_corners["center"]
    north_crop = north_tangent_km(
        tie_points.lonlat_to_ground_km(crop_center_lon, crop_center_lat, config.moon_radius_km)
    )
    k_crop, dev_crop = best_k_for_north_up(r_crop_raw[:, 1], r_crop_raw[:, 0], north_crop, candidates=(0, 2))

    return DisplayRotations(
        k_synthetic=k_synthetic,
        dev_synthetic_deg=dev_synthetic,
        k_crop=k_crop,
        dev_crop_deg=dev_crop,
    )
