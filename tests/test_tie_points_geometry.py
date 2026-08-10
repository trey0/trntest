from pathlib import Path
from unittest.mock import patch

import pytest

from trntest import isis_wac, tie_points
from trntest.camera import Camera
from trntest.config import TrntestConfig


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


_FAKE_MODEL = isis_wac.GroundToImageModel(cub_path=Path("/fake/crop.cub"), name_model="fake", used_csm=False)


def test_resolve_crop_pixels_merges_successful_points_with_isis_convention_offset(monkeypatch):
    points = {"center": {"lonlat": (10.0, 20.0), "synthetic_px": (5.0, 6.0)}}
    monkeypatch.setattr(isis_wac, "ground_to_image_pixel", lambda model, lon, lat: (100.0, 50.0))

    resolved = tie_points.resolve_crop_pixels(points, _FAKE_MODEL)

    assert resolved["center"]["synthetic_px"] == (5.0, 6.0)  # other fields preserved
    assert resolved["center"]["crop_px"] == (99.5, 49.5)  # ISIS's 1-based Sample/Line -> 0-based corner


def test_resolve_crop_pixels_drops_points_that_dont_project(monkeypatch, capsys):
    points = {
        "top_left": {"lonlat": (1.0, 1.0), "synthetic_px": (0.0, 0.0)},
        "center": {"lonlat": (2.0, 2.0), "synthetic_px": (1.0, 1.0)},
    }

    def fake_ground_to_image_pixel(model, lon, lat):
        return None if lon == 1.0 else (10.0, 10.0)

    monkeypatch.setattr(isis_wac, "ground_to_image_pixel", fake_ground_to_image_pixel)

    resolved = tie_points.resolve_crop_pixels(points, _FAKE_MODEL)

    assert set(resolved) == {"center"}
    assert "top_left" in capsys.readouterr().out


def test_resolve_crop_pixels_raises_if_none_resolve(monkeypatch):
    points = {"center": {"lonlat": (1.0, 1.0), "synthetic_px": (0.0, 0.0)}}
    monkeypatch.setattr(isis_wac, "ground_to_image_pixel", lambda model, lon, lat: None)

    with pytest.raises(RuntimeError):
        tie_points.resolve_crop_pixels(points, _FAKE_MODEL)


def _fake_camera(n_frames_for_square_crop: int, reverse: bool) -> Camera:
    return Camera(
        et=0.0,
        center_frame_index=100.0,
        camera_center_moon_me_m=[0.0, 0.0, 0.0],
        r_cam_to_me=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        boresight_rotation_k=3 if reverse else 1,
        slant_range_km=100.0,
        off_nadir_deg=5.0,
        focal_length_px=700.0,
        footprint_lonlat_deg={},
        cross_track_width_km=140.0,
        km_per_frame=0.2,
        n_frames_for_square_crop=n_frames_for_square_crop,
        tsai_path=Path("/fake/camera.tsai"),
    )


def test_crop_footprint_corners_for_camera_queries_inset_from_edges(monkeypatch):
    camera = _fake_camera(n_frames_for_square_crop=10, reverse=False)
    height = camera.n_frames_for_square_crop * isis_wac.VIS_BLOCK_HEIGHT  # 140
    margin = tie_points._CROP_EDGE_MARGIN_PX

    queried = []

    def fake_ground_point_at_pixel(cub_path, sample, line):
        queried.append((sample, line))
        return (sample, line)  # stand-in, just need to see what was queried

    fake_crop = isis_wac.CropResult(cub_path=Path("/fake/crop.cub"))
    with patch.object(isis_wac, "run_pipeline", return_value="fake-stitched"):
        with patch.object(isis_wac, "crop_for_camera", return_value=fake_crop):
            monkeypatch.setattr(isis_wac, "ground_point_at_pixel", fake_ground_point_at_pixel)
            result = tie_points.crop_footprint_corners_for_camera(
                frame_timing=None, camera=camera, config=TrntestConfig()
            )

    assert result["top_left"] == (1 + margin, 1 + margin)
    assert result["top_right"] == (isis_wac.SAMPLES - margin, 1 + margin)
    assert result["bottom_left"] == (1 + margin, height - margin)
    assert result["bottom_right"] == (isis_wac.SAMPLES - margin, height - margin)
    assert result["center"] == (isis_wac.SAMPLES / 2.0, height / 2.0)
    assert len(queried) == 5
