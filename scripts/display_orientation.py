"""Display-only north-up rotation for the comparison figure.

Purely cosmetic: rotates already-rendered/extracted image arrays (and remaps tie-point plot
coordinates to match) so north is approximately "up" in the notebook's comparison figure. Does
**not** touch the sensor model, the `.tsai`, or the crop-extraction code -- kept strictly separate
from the fixed synthetic-camera axis convention in `build_camera_from_spice.py` (see
docs/data-sources.md for why: the sensor model's convention must be a fixed, hardware-motivated
choice, while which way is "north" varies by pass -- ascending vs. descending -- and shouldn't
influence the model at all).
"""
import numpy as np


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
