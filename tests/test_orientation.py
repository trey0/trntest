import numpy as np
import pytest

from trntest import orientation


def test_north_tangent_km_at_equator_points_toward_pole():
    ground = np.array([1737.4, 0.0, 0.0])  # a point on the equator
    north = orientation.north_tangent_km(ground)
    np.testing.assert_allclose(north, np.array([0.0, 0.0, 1.0]), atol=1e-12)
    assert np.linalg.norm(north) == pytest.approx(1.0)


def test_best_k_for_north_up_picks_exact_match():
    north = np.array([0.0, 1.0, 0.0])
    right = np.array([1.0, 0.0, 0.0])
    up = np.array([0.0, 1.0, 0.0])
    k, dev = orientation.best_k_for_north_up(right, up, north, candidates=(0, 1, 2, 3))
    assert k == 0
    assert dev == pytest.approx(0.0, abs=1e-6)


def test_best_k_for_north_up_picks_90deg_rotation():
    north = np.array([1.0, 0.0, 0.0])
    right = np.array([1.0, 0.0, 0.0])
    up = np.array([0.0, 1.0, 0.0])
    k, dev = orientation.best_k_for_north_up(right, up, north, candidates=(0, 1, 2, 3))
    assert k == 1
    assert dev == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_rotate_pixel_coords_matches_np_rot90(k):
    height, width = 5, 7
    arr = np.arange(height * width).reshape(height, width)
    rotated = np.rot90(arr, k)

    for row in range(height):
        for col in range(width):
            new_col, new_row = orientation.rotate_pixel_coords(col, row, k, height, width)
            assert rotated[new_row, new_col] == arr[row, col]
