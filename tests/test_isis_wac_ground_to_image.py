import dataclasses
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from trntest import isis_wac
from trntest.config import TrntestConfig

_GROUND_POINT_PVL = """
Group = GroundPoint
  Sample = 100.5
  Line   = 50.25
End_Group
"""

_IMAGE_TO_GROUND_PVL = """
Group = GroundPoint
  PositiveEast360Longitude = 169.575768599 <DEGREE>
  PlanetocentricLatitude   = 38.7726704656 <DEGREE>
End_Group
"""


def _completed_process(returncode, stdout="", stderr=""):
    class _Result:
        pass

    result = _Result()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def test_ground_to_image_pixel_parses_campt_pvl_output():
    model = isis_wac.GroundToImageModel(cub_path=Path("/fake/crop.cub"), name_model="fake", used_csm=False)
    with patch("subprocess.run", return_value=_completed_process(0, stdout=_GROUND_POINT_PVL)):
        pixel = isis_wac.ground_to_image_pixel(model, lon_deg=1.0, lat_deg=2.0)
    assert pixel == (100.5, 50.25)


def test_ground_to_image_pixel_returns_none_on_campt_failure():
    model = isis_wac.GroundToImageModel(cub_path=Path("/fake/crop.cub"), name_model="fake", used_csm=False)
    with patch("subprocess.run", return_value=_completed_process(1, stderr="**ERROR** not inside cube.")):
        pixel = isis_wac.ground_to_image_pixel(model, lon_deg=1.0, lat_deg=2.0)
    assert pixel is None


def test_ground_point_at_pixel_parses_campt_pvl_output():
    with patch("subprocess.run", return_value=_completed_process(0, stdout=_IMAGE_TO_GROUND_PVL)):
        lon, lat = isis_wac.ground_point_at_pixel(Path("/fake/stitched.cub"), sample=352.0, line=1806.0)
    assert lon == pytest.approx(169.575768599)
    assert lat == pytest.approx(38.7726704656)


def test_run_pipeline_reuses_existing_stitched_cube_without_rerunning_lrowac2isis(tmp_path):
    config = dataclasses.replace(TrntestConfig(), scratch_dir=tmp_path, edr_product="TESTPRODUCT")
    out_prefix = isis_wac._spike_dir(config) / "TESTPRODUCT"
    stitched_path = out_prefix.with_name(out_prefix.name + ".vis.cal.stitched.cub")
    stitched_path.write_text("fake cube")

    with patch.object(isis_wac, "ensure_isisdata"):
        with patch.object(
            isis_wac, "fetch_edr_img", return_value=isis_wac.EdrFetchResult(img_path=Path("/fake/TESTPRODUCT.IMG"))
        ):
            with patch.object(isis_wac, "run_lrowac2isis") as mock_lrowac2isis:
                result = isis_wac.run_pipeline(flip=True, frame_timing=None, config=config)

    mock_lrowac2isis.assert_not_called()
    assert result.cub_path == stitched_path
    assert result.flip is True


def test_resolve_ground_to_image_model_falls_back_for_pushframe(tmp_path):
    isd_path = tmp_path / "stitched.json"
    isd_path.write_text(json.dumps({"name_model": "USGS_ASTRO_PUSH_FRAME_SENSOR_MODEL"}))
    crop = isis_wac.CropResult(cub_path=tmp_path / "crop.cub")

    with patch.object(isis_wac, "run_isd_generate", return_value=isis_wac.IsdGenerateResult(json_path=isd_path)):
        with patch.object(isis_wac, "run_quiet") as mock_run_quiet:
            model = isis_wac.resolve_ground_to_image_model(stitched="fake-stitched", crop=crop, config=TrntestConfig())

    assert model.used_csm is False
    assert model.cub_path == crop.cub_path
    assert model.name_model == "USGS_ASTRO_PUSH_FRAME_SENSOR_MODEL"
    mock_run_quiet.assert_not_called()  # csminit must not run for a Pushframe sensor


def test_resolve_ground_to_image_model_attaches_csm_for_non_pushframe(tmp_path):
    isd_path = tmp_path / "stitched.json"
    isd_path.write_text(json.dumps({"name_model": "USGS_ASTRO_FRAME_SENSOR_MODEL"}))
    crop_cub = tmp_path / "crop.cub"
    crop_cub.write_text("fake cube contents")
    crop = isis_wac.CropResult(cub_path=crop_cub)

    with patch.object(isis_wac, "run_isd_generate", return_value=isis_wac.IsdGenerateResult(json_path=isd_path)):
        with patch.object(isis_wac, "run_quiet") as mock_run_quiet:
            model = isis_wac.resolve_ground_to_image_model(stitched="fake-stitched", crop=crop, config=TrntestConfig())

    assert model.used_csm is True
    assert model.cub_path != crop.cub_path
    assert model.cub_path.exists()  # shutil.copy actually ran
    assert model.name_model == "USGS_ASTRO_FRAME_SENSOR_MODEL"
    mock_run_quiet.assert_called_once()
    csminit_cmd = mock_run_quiet.call_args[0][0]
    assert csminit_cmd[0] == "csminit"
