from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import rasterio

from trntest import isis_wac, tie_points, wac_camera_model
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
    points = tie_points.die5_points(bbox, center=(5.0, 5.0), margin_frac=0.1)

    assert set(points) == {"top_left", "top_right", "center", "bottom_left", "bottom_right"}
    assert points["center"] == (5.0, 5.0)
    assert points["top_left"] == (0.5, 9.5)
    assert points["bottom_right"] == (9.5, 0.5)


def test_die5_points_anchors_on_center_not_bbox_midpoint():
    """A bbox whose own naive midpoint would differ from the true `center` -- every point should
    still be placed relative to `center`, not `(lon_min+lon_max)/2, (lat_min+lat_max)/2` -- the real
    bug this anchoring fixed (see `die5_points`'s own docstring)."""
    bbox = (0.0, 10.0, 0.0, 20.0)  # naive midpoint would be (5.0, 10.0)
    points = tie_points.die5_points(bbox, center=(2.0, 4.0), margin_frac=0.0)

    assert points["center"] == (2.0, 4.0)
    assert points["top_left"] == (0.0, 20.0)
    assert points["bottom_right"] == (10.0, 0.0)


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


def _write_fake_crop_cube(path: Path, n_framelets: int = 5) -> None:
    """A minimal real (not mocked) single-band raster, just so `resolve_crop_pixels` can read a
    real `n_lines` off it via `rasterio` -- matches this project's existing test convention of real
    small fixture files over mocking rasterio's own internals (see e.g. test_craters.py)."""
    n_lines = n_framelets * wac_camera_model.FRAMELET_HEIGHT
    with rasterio.open(path, "w", driver="GTiff", height=n_lines, width=10, count=1, dtype="uint8") as dst:
        dst.write(np.zeros((n_lines, 10), dtype="uint8"), 1)


def _fake_crop_result(tmp_path: Path) -> "isis_wac.CropResult":
    cub_path = tmp_path / "crop.cub"
    _write_fake_crop_cube(cub_path)
    return isis_wac.CropResult(cub_path=cub_path)


def test_resolve_crop_pixels_merges_successful_points_with_isis_convention_offset(tmp_path, monkeypatch):
    points = {"center": {"lonlat": (10.0, 20.0), "synthetic_px": (5.0, 6.0)}}
    monkeypatch.setattr(wac_camera_model, "calibrate_et_per_crop_line", lambda cub_path, n_lines: (0.0, 1.0))
    monkeypatch.setattr(wac_camera_model, "find_framelet_and_project", lambda *args, **kwargs: (100.0, 50.0))

    resolved = tie_points.resolve_crop_pixels(points, _fake_crop_result(tmp_path), config=TrntestConfig())

    assert resolved["center"]["synthetic_px"] == (5.0, 6.0)  # other fields preserved
    assert resolved["center"]["crop_px"] == (99.5, 49.5)  # ISIS's 1-based Sample/Line -> 0-based corner


def test_resolve_crop_pixels_drops_points_that_dont_project(tmp_path, monkeypatch, capsys):
    points = {
        "top_left": {"lonlat": (1.0, 1.0), "synthetic_px": (0.0, 0.0)},
        "center": {"lonlat": (2.0, 2.0), "synthetic_px": (1.0, 1.0)},
    }

    calls = []

    def fake_find_framelet_and_project(ground_me_m, n_framelets, et0, et_per_line, correction=None):
        calls.append(ground_me_m)
        return None if len(calls) == 1 else (10.0, 10.0)

    monkeypatch.setattr(wac_camera_model, "calibrate_et_per_crop_line", lambda cub_path, n_lines: (0.0, 1.0))
    monkeypatch.setattr(wac_camera_model, "find_framelet_and_project", fake_find_framelet_and_project)

    resolved = tie_points.resolve_crop_pixels(points, _fake_crop_result(tmp_path), config=TrntestConfig())

    assert set(resolved) == {"center"}
    assert "top_left" in capsys.readouterr().out


def test_resolve_crop_pixels_raises_if_none_resolve(tmp_path, monkeypatch):
    points = {"center": {"lonlat": (1.0, 1.0), "synthetic_px": (0.0, 0.0)}}
    monkeypatch.setattr(wac_camera_model, "calibrate_et_per_crop_line", lambda cub_path, n_lines: (0.0, 1.0))
    monkeypatch.setattr(wac_camera_model, "find_framelet_and_project", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError):
        tie_points.resolve_crop_pixels(points, _fake_crop_result(tmp_path), config=TrntestConfig())


def _fake_camera(n_frames_for_square_crop: int, reverse: bool) -> Camera:
    return Camera(
        et=0.0,
        center_frame_index=100.0,
        camera_center_moon_me_m=[0.0, 0.0, 0.0],
        camera_along_track_direction_moon_me=[0.0, 1.0, 0.0],
        r_cam_to_me=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        boresight_rotation_k=3 if reverse else 1,
        slant_range_km=100.0,
        off_nadir_deg=5.0,
        focal_length_u_px=700.0,
        focal_length_v_px=700.0,
        principal_point_u_px=512.0,
        principal_point_v_px=512.0,
        footprint_lonlat_deg={},
        render_cross_track_km=140.0,
        render_along_track_km=140.0,
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
