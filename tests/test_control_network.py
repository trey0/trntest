import csv
import subprocess
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import rasterio

from trntest import control_network, isis_wac, wac_camera_model
from trntest.config import TrntestConfig

_ORTHO_CRS = "+proj=ortho +lon_0=0 +lat_0=0 +R=1737400 +units=m +no_defs"


def _write_fake_crop_cube(path: Path, n_framelets: int = 5) -> None:
    """A minimal real (not mocked) single-band raster, just so resolve_control_points can read a
    real `n_lines` off it via `rasterio` -- matches this project's existing test convention (see
    e.g. test_tie_points_geometry.py's own identically-named helper)."""
    n_lines = n_framelets * wac_camera_model.FRAMELET_HEIGHT
    with rasterio.open(path, "w", driver="GTiff", height=n_lines, width=10, count=1, dtype="uint8") as dst:
        dst.write(np.zeros((n_lines, 10), dtype="uint8"), 1)


def test_map_points_to_lonlat_maps_the_projection_center_to_lon0_lat0():
    points_map = np.array([[0.0, 0.0]])

    lons, lats = control_network.map_points_to_lonlat(points_map, _ORTHO_CRS, TrntestConfig())

    assert lons[0] == pytest.approx(0.0, abs=1e-6)
    assert lats[0] == pytest.approx(0.0, abs=1e-6)


def test_map_points_to_lonlat_normalizes_to_0_360_positive_east():
    # A point west of the projection center (negative map x) should come back as a longitude
    # near 360, not a negative one -- this project's own 0-360 Positive-East convention throughout
    # (matches isis_wac.ground_to_image_pixel's PositiveEast360Longitude expectation).
    points_map = np.array([[-50000.0, 0.0]])

    lons, _ = control_network.map_points_to_lonlat(points_map, _ORTHO_CRS, TrntestConfig())

    assert lons[0] > 350.0


def test_resolve_control_points_pairs_and_drops_unresolved_points(tmp_path):
    wac_points_map = np.array([[0.0, 0.0], [1000.0, 0.0], [2000.0, 0.0]])
    basemap_points_map = np.array([[10.0, 10.0], [1010.0, 10.0], [2010.0, 10.0]])
    cub_path = tmp_path / "crop.cub"
    _write_fake_crop_cube(cub_path)
    model = isis_wac.GroundToImageModel(cub_path=cub_path, name_model="fake", used_csm=False)

    # The middle point's implied ground point fails to project into any real framelet
    # (find_framelet_and_project returns None); the other two resolve to distinct, recognizable
    # pixels. The mocked return is a plain list, in the same order resolve_control_points iterates
    # the (paired) input points -- avoids needing to know the exact projected lon/lat values
    # map_points_to_lonlat produces for the fixture.
    with patch.object(isis_wac, "sample_lunar_dem_radii_batch", return_value=np.full(3, 1737400.0)):
        with patch.object(wac_camera_model, "calibrate_et_per_crop_line", return_value=(0.0, 1.0)):
            with patch.object(
                wac_camera_model, "find_framelet_and_project", side_effect=[(101.0, 201.0), None, (103.0, 203.0)]
            ):
                observed_pixels, ground_lonlat = control_network.resolve_control_points(
                    wac_points_map, basemap_points_map, _ORTHO_CRS, model, TrntestConfig()
                )

    assert len(observed_pixels) == 2
    assert len(ground_lonlat) == 2
    # The dropped (middle) point's basemap counterpart must be dropped too, not just the WAC-side
    # one -- pairing must stay intact through the filter. Confirmed two ways: the surviving
    # observed_pixels are exactly the 1st/3rd fake projector results (not 1st/2nd, which a
    # pairing bug that just truncated the arrays instead of filtering by index could produce), and
    # both surviving basemap points share the same real map y-coordinate (10.0), so their resolved
    # latitudes must match each other.
    assert observed_pixels[0] == pytest.approx((101.0, 201.0))
    assert observed_pixels[1] == pytest.approx((103.0, 203.0))
    assert ground_lonlat[0][1] == pytest.approx(ground_lonlat[1][1])


def test_resolve_control_points_raises_if_nothing_resolves(tmp_path):
    wac_points_map = np.array([[0.0, 0.0]])
    basemap_points_map = np.array([[0.0, 0.0]])
    cub_path = tmp_path / "crop.cub"
    _write_fake_crop_cube(cub_path)
    model = isis_wac.GroundToImageModel(cub_path=cub_path, name_model="fake", used_csm=False)

    with patch.object(isis_wac, "sample_lunar_dem_radii_batch", return_value=np.full(1, 1737400.0)):
        with patch.object(wac_camera_model, "calibrate_et_per_crop_line", return_value=(0.0, 1.0)):
            with patch.object(wac_camera_model, "find_framelet_and_project", return_value=None):
                with pytest.raises(RuntimeError):
                    control_network.resolve_control_points(
                        wac_points_map, basemap_points_map, _ORTHO_CRS, model, TrntestConfig()
                    )


def test_write_control_network_writes_a_correct_csv_and_invokes_the_isis_python_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("ISISROOT", "/fake/isisroot")
    # ISIS's 1-based pixel-center sample/line -- write_control_network must subtract 0.5 before
    # handing off to plio's own writer, which re-adds 0.5 itself (see the module docstring).
    observed_pixels = np.array([[100.5, 200.5], [50.0, 60.0]])
    ground_lonlat = np.array([[10.0, 20.0], [30.0, 40.0]])
    out_path = tmp_path / "out.net"

    with patch.object(isis_wac, "cube_serial_number", return_value="TEST_SN"):
        with patch("trntest.control_network.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            result = control_network.write_control_network(
                observed_pixels, ground_lonlat, Path("/fake/crop.cub"), out_path, TrntestConfig()
            )

    assert result == out_path
    csv_path = out_path.with_suffix(".csv")
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["serialnumber"] == "TEST_SN"
    assert rows[0]["pointType"] == "4"  # Fixed
    assert rows[0]["measureType"] == "2"  # RegisteredPixel
    assert float(rows[0]["sample"]) == pytest.approx(100.0)  # 100.5 - 0.5
    assert float(rows[0]["line"]) == pytest.approx(200.0)  # 200.5 - 0.5
    # aprioriX/Y/Z and adjustedX/Y/Z must agree (a Fixed point isn't allowed to move).
    assert rows[0]["aprioriX"] == rows[0]["adjustedX"]

    isis_python_arg, script_arg = mock_run.call_args[0][0][0], mock_run.call_args[0][0][1]
    assert isis_python_arg == "/fake/isisroot/bin/python"
    assert script_arg.endswith("isis_write_control_network.py")
    assert "--csv" in mock_run.call_args[0][0]


def test_write_control_network_raises_on_writer_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("ISISROOT", "/fake/isisroot")
    observed_pixels = np.array([[100.5, 200.5]])
    ground_lonlat = np.array([[10.0, 20.0]])

    with patch.object(isis_wac, "cube_serial_number", return_value="TEST_SN"):
        with patch("trntest.control_network.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
            with pytest.raises(subprocess.CalledProcessError):
                control_network.write_control_network(
                    observed_pixels, ground_lonlat, Path("/fake/crop.cub"), tmp_path / "out.net", TrntestConfig()
                )
