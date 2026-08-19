import csv
import dataclasses
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from trntest import isis_wac
from trntest.config import TrntestConfig


def test_ensure_lunar_shape_model_reuses_existing_file_without_fetching(tmp_path):
    config = dataclasses.replace(TrntestConfig(), cache_root=tmp_path)
    shape_model_path = tmp_path / "isisdata" / isis_wac._LUNAR_SHAPE_MODEL_REL_PATH
    shape_model_path.parent.mkdir(parents=True)
    shape_model_path.write_text("fake shape cube")

    with patch.object(isis_wac, "run_quiet") as mock_run_quiet:
        result = isis_wac.ensure_lunar_shape_model(config)

    assert result == shape_model_path
    mock_run_quiet.assert_not_called()


def test_ensure_lunar_shape_model_fetches_the_cube_and_its_small_index_dependencies(tmp_path):
    config = dataclasses.replace(TrntestConfig(), cache_root=tmp_path)

    with patch.object(isis_wac, "ensure_isisdata"):
        with patch.object(isis_wac, "run_quiet") as mock_run_quiet:
            result = isis_wac.ensure_lunar_shape_model(config)

    assert result == tmp_path / "isisdata" / isis_wac._LUNAR_SHAPE_MODEL_REL_PATH
    assert mock_run_quiet.call_count == 3
    includes = [call.args[0][call.args[0].index("--include") + 1] for call in mock_run_quiet.call_args_list]
    assert "dems/ldem_128ppd_Mar2011_clon180_radius_pad.cub" in includes
    assert "dems/kernels.*.db" in includes
    assert "kernels/spk/*.db" in includes


def test_attach_dem_shape_model_copies_the_crop_and_runs_spiceinit_shape_user(tmp_path):
    crop_cub = tmp_path / "crop.cub"
    crop_cub.write_text("fake cube contents")
    crop = isis_wac.CropResult(cub_path=crop_cub)
    config = dataclasses.replace(TrntestConfig(), cache_root=tmp_path)
    shape_model_path = tmp_path / "isisdata" / isis_wac._LUNAR_SHAPE_MODEL_REL_PATH

    with patch.object(isis_wac, "ensure_lunar_shape_model", return_value=shape_model_path):
        with patch.object(isis_wac, "run_quiet") as mock_run_quiet:
            result = isis_wac.attach_dem_shape_model(crop, config)

    assert result.cub_path == tmp_path / "crop.dem.cub"
    assert result.cub_path.exists()  # shutil.copy actually ran
    assert result.cub_path.read_text() == "fake cube contents"

    mock_run_quiet.assert_called_once()
    cmd = mock_run_quiet.call_args[0][0]
    assert cmd[0] == "spiceinit"
    assert f"from={result.cub_path}" in cmd
    assert "web=yes" in cmd
    assert "shape=user" in cmd
    assert f"model={shape_model_path}" in cmd


def test_attach_dem_shape_model_is_idempotent(tmp_path):
    crop_cub = tmp_path / "crop.cub"
    crop_cub.write_text("fake cube contents")
    crop = isis_wac.CropResult(cub_path=crop_cub)
    out_path = tmp_path / "crop.dem.cub"
    out_path.write_text("already-attached cube")

    with patch.object(isis_wac, "ensure_lunar_shape_model", return_value=tmp_path / "ldem.cub"):
        with patch.object(isis_wac, "run_quiet") as mock_run_quiet:
            result = isis_wac.attach_dem_shape_model(crop, TrntestConfig())

    assert result.cub_path == out_path
    assert result.cub_path.read_text() == "already-attached cube"  # untouched, not re-copied
    mock_run_quiet.assert_not_called()


def _fake_mappt_run_quiet(rows):
    def _side_effect(cmd):
        to_arg = next(a for a in cmd if a.startswith("to="))
        out_path = Path(to_arg.removeprefix("to="))
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["PixelValue"])
            writer.writeheader()
            writer.writerows(rows)

    return _side_effect


def test_sample_lunar_dem_radii_batch_parses_pixel_value_directly_and_writes_lat_lon_order(tmp_path):
    lonlat_deg = np.array([[168.004501, 39.827978], [169.575314, 38.710263]])
    rows = [{"PixelValue": "1738504.5"}, {"PixelValue": "1740153.0"}]
    written_coordlist = {}

    def fake_run_quiet(cmd):
        coordlist_arg = next(a for a in cmd if a.startswith("coordlist="))
        written_coordlist["text"] = Path(coordlist_arg.removeprefix("coordlist=")).read_text()
        _fake_mappt_run_quiet(rows)(cmd)

    with patch.object(isis_wac, "ensure_lunar_shape_model", return_value=tmp_path / "ldem.cub"):
        with patch.object(isis_wac, "run_quiet", side_effect=fake_run_quiet):
            radii = isis_wac.sample_lunar_dem_radii_batch(lonlat_deg, TrntestConfig())

    assert radii == pytest.approx([1738504.5, 1740153.0])
    # mappt.xml documents ground COORDLIST rows as (latitude, longitude), like campt's own.
    assert written_coordlist["text"] == "39.827978,168.004501\n38.710263,169.575314\n"


def test_sample_lunar_dem_radii_batch_does_not_reapply_base_multiplier():
    # PixelValue from mappt is already the calibrated real radius (confirmed live) -- a caller
    # re-applying the cube's own Base/Multiplier on top would be a real, ~867km-off bug.
    rows = [{"PixelValue": "1738504.5"}]
    with patch.object(isis_wac, "ensure_lunar_shape_model", return_value=Path("/fake/ldem.cub")):
        with patch.object(isis_wac, "run_quiet", side_effect=_fake_mappt_run_quiet(rows)):
            radii = isis_wac.sample_lunar_dem_radii_batch(np.array([[168.0, 39.8]]), TrntestConfig())
    assert radii[0] == pytest.approx(1738504.5)


def test_sample_lunar_dem_radii_batch_raises_on_row_count_mismatch():
    rows = [{"PixelValue": "1738504.5"}]  # only 1 row for 2 input points
    with patch.object(isis_wac, "ensure_lunar_shape_model", return_value=Path("/fake/ldem.cub")):
        with patch.object(isis_wac, "run_quiet", side_effect=_fake_mappt_run_quiet(rows)):
            with pytest.raises(RuntimeError):
                isis_wac.sample_lunar_dem_radii_batch(np.array([[168.0, 39.8], [169.0, 38.0]]), TrntestConfig())
