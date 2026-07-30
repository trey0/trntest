import pytest

from trntest import tie_points


def test_intersect_bbox_overlapping():
    a = (0.0, 10.0, 0.0, 10.0)
    b = (5.0, 15.0, 5.0, 15.0)
    assert tie_points.intersect_bbox(a, b) == (5.0, 10.0, 5.0, 10.0)


def test_intersect_bbox_empty_raises():
    a = (0.0, 1.0, 0.0, 1.0)
    b = (2.0, 3.0, 2.0, 3.0)
    with pytest.raises(AssertionError):
        tie_points.intersect_bbox(a, b)


def test_die5_points_layout_and_margin():
    bbox = (0.0, 10.0, 0.0, 10.0)
    points = tie_points.die5_points(bbox, margin_frac=0.1)

    assert set(points) == {"top_left", "top_right", "center", "bottom_left", "bottom_right"}
    assert points["center"] == (5.0, 5.0)
    assert points["top_left"] == (1.0, 9.0)
    assert points["bottom_right"] == (9.0, 1.0)


def test_inscribed_bbox_within_square_returns_same_square():
    corners = {
        "top_left": (0.0, 10.0),
        "top_right": (10.0, 10.0),
        "bottom_right": (10.0, 0.0),
        "bottom_left": (0.0, 0.0),
    }
    bbox = tie_points.inscribed_bbox(corners, interior_point=(5.0, 5.0))
    lon_min, lon_max, lat_min, lat_max = bbox
    assert lon_min == pytest.approx(0.0, abs=1e-6)
    assert lon_max == pytest.approx(10.0, abs=1e-6)
    assert lat_min == pytest.approx(0.0, abs=1e-6)
    assert lat_max == pytest.approx(10.0, abs=1e-6)


def test_inscribed_bbox_shrinks_for_skewed_quadrilateral():
    # A diamond -- its own axis-aligned bounding box corners fall outside it, so the inscribed
    # rectangle must shrink from that starting box.
    corners = {
        "top_left": (0.0, 5.0),
        "top_right": (5.0, 10.0),
        "bottom_right": (10.0, 5.0),
        "bottom_left": (5.0, 0.0),
    }
    bbox = tie_points.inscribed_bbox(corners, interior_point=(5.0, 5.0))
    lon_min, lon_max, lat_min, lat_max = bbox
    assert lon_max - lon_min < 10.0
    assert lat_max - lat_min < 10.0
