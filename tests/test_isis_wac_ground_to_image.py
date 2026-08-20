import csv
import dataclasses
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pvl
import pytest
from scipy.spatial.transform import Rotation

from trntest import isis_wac, wac_camera_model
from trntest.config import TrntestConfig

# Trimmed real InstrumentPointing Table label (same fixture as test_isis_wac_parsing.py's
# _TABLES_LABEL_TEXT, captured live against product M1327210646CE's cropped cube) -- what
# apply_pose_correction_to_crop's own `_catlab` call sees.
_POINTING_LABEL_TEXT = """
Object = Table
  Name                = InstrumentPointing
  StartByte           = 13863937
  Bytes               = 16576
  Records             = 259
  ByteOrder           = Lsb
  TimeDependentFrames = (-85620, -85000, 1)
  ConstantFrames      = (-85621, -85620)
  ConstantRotation    = (0.99982051808596, 0.0014619008152411,
                         -0.018889003688109, -0.0013858576920097,
                         0.99999088592261, 0.0040382508789192,
                         0.01889473505452, -0.0040113486148665,
                         0.99981343163088)
  CkTableStartTime    = 625843448.25011
  CkTableEndTime      = 625843811.06261
  CkTableOriginalSize = 259
  FrameTypeCode       = 3
  Description         = "Created by spiceinit"
  Kernels             = ($lro/kernels/ck/lrolc_2019304_2019335_v01.bc,
                         $lro/kernels/ck/moc42r_2019304_2019335_v01.bc,
                         $lro/kernels/fk/lro_frames_2014049_v01.tf)

  Group = Field
    Name = J2000Q0
    Type = Double
    Size = 1
  End_Group
End_Object
"""

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


def test_ground_point_at_pixel_raises_and_prints_diagnostic_on_campt_failure(capsys):
    fake_result = subprocess.CompletedProcess(
        args=["campt"], returncode=1, stdout="some stdout", stderr="**ERROR** bad kernel."
    )
    with patch("subprocess.run", return_value=fake_result):
        with pytest.raises(subprocess.CalledProcessError):
            isis_wac.ground_point_at_pixel(Path("/fake/stitched.cub"), sample=352.0, line=1806.0)
    captured = capsys.readouterr()
    assert "some stdout" in captured.out
    assert "**ERROR** bad kernel." in captured.out


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


def test_apply_pose_correction_to_crop_copies_and_runs_tabledump_then_csv2table(tmp_path):
    crop_cub = tmp_path / "crop.cub"
    crop_cub.write_text("fake cube contents")
    crop = isis_wac.CropResult(cub_path=crop_cub)
    correction = wac_camera_model.PoseCorrection.identity()

    with patch("subprocess.run", return_value=_completed_process(0, stdout=_POINTING_LABEL_TEXT)):
        with patch.object(isis_wac, "run_quiet") as mock_run_quiet:
            result = isis_wac.apply_pose_correction_to_crop(crop, correction, config=TrntestConfig())

    assert result.cub_path == tmp_path / "crop.corrected.cub"
    assert result.cub_path.exists()  # shutil.copy actually ran
    assert result.cub_path.read_text() == "fake cube contents"

    assert mock_run_quiet.call_count == 2
    tabledump_cmd, csv2table_cmd = (call.args[0] for call in mock_run_quiet.call_args_list)
    assert tabledump_cmd[0] == "tabledump"
    assert f"from={crop_cub}" in tabledump_cmd
    assert "name=InstrumentPointing" in tabledump_cmd
    assert csv2table_cmd[0] == "csv2table"
    assert f"to={result.cub_path}" in csv2table_cmd
    assert "tablename=InstrumentPointing" in csv2table_cmd


def test_apply_pose_correction_to_crop_leaves_constant_rotation_unchanged_for_identity_correction(tmp_path):
    crop_cub = tmp_path / "crop.cub"
    crop_cub.write_text("fake cube contents")
    crop = isis_wac.CropResult(cub_path=crop_cub)
    correction = wac_camera_model.PoseCorrection.identity()

    with patch("subprocess.run", return_value=_completed_process(0, stdout=_POINTING_LABEL_TEXT)):
        with patch.object(isis_wac, "run_quiet") as mock_run_quiet:
            isis_wac.apply_pose_correction_to_crop(crop, correction, config=TrntestConfig())

    _, csv2table_cmd = mock_run_quiet.call_args_list
    label_arg = next(arg for arg in csv2table_cmd.args[0] if arg.startswith("label="))
    written_label = pvl.loads(Path(label_arg.removeprefix("label=")).read_text())
    c_orig = np.array(pvl.loads(_POINTING_LABEL_TEXT).getlist("Table")[0]["ConstantRotation"]).reshape(3, 3)
    # pvl.dumps uppercases keywords by default (ISIS's own PVL convention -- confirmed live, matches
    # the real csv2table label= file this session's scratch validation produced).
    c_written = np.array(written_label["CONSTANTROTATION"]).reshape(3, 3)
    assert c_written == pytest.approx(c_orig)


def test_apply_pose_correction_to_crop_applies_delta_rotation_transpose(tmp_path):
    crop_cub = tmp_path / "crop.cub"
    crop_cub.write_text("fake cube contents")
    crop = isis_wac.CropResult(cub_path=crop_cub)
    delta_rotation = Rotation.from_rotvec(np.radians([1.0, -0.6, 0.3])).as_matrix()
    correction = wac_camera_model.PoseCorrection(delta_position_m=np.zeros(3), delta_rotation=delta_rotation)

    with patch("subprocess.run", return_value=_completed_process(0, stdout=_POINTING_LABEL_TEXT)):
        with patch.object(isis_wac, "run_quiet") as mock_run_quiet:
            isis_wac.apply_pose_correction_to_crop(crop, correction, config=TrntestConfig())

    _, csv2table_cmd = mock_run_quiet.call_args_list
    label_arg = next(arg for arg in csv2table_cmd.args[0] if arg.startswith("label="))
    written_label = pvl.loads(Path(label_arg.removeprefix("label=")).read_text())
    c_orig = np.array(pvl.loads(_POINTING_LABEL_TEXT).getlist("Table")[0]["ConstantRotation"]).reshape(3, 3)
    # pvl.dumps uppercases keywords by default (ISIS's own PVL convention -- confirmed live, matches
    # the real csv2table label= file this session's scratch validation produced).
    c_written = np.array(written_label["CONSTANTROTATION"]).reshape(3, 3)
    assert c_written == pytest.approx(delta_rotation.T @ c_orig)


def _fake_campt_batch_run_quiet(rows):
    """A `run_quiet` stand-in for `ground_to_image_pixels_batch`: writes `rows` (dicts with
    Sample/Line/Error keys) as a FLAT-format CSV to whatever `to=` path the real call would have
    written, standing in for the real `campt usecoordlist=true` subprocess call."""

    def _side_effect(cmd):
        to_arg = next(a for a in cmd if a.startswith("to="))
        out_path = Path(to_arg.removeprefix("to="))
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["Sample", "Line", "Error"])
            writer.writeheader()
            writer.writerows(rows)

    return _side_effect


def test_ground_to_image_pixels_batch_parses_rows_and_writes_coordlist_in_lat_lon_order():
    model = isis_wac.GroundToImageModel(cub_path=Path("/fake/crop.cub"), name_model="fake", used_csm=False)
    lonlat_deg = np.array([[169.5, 38.5], [170.0, 39.0]])
    rows = [
        {"Sample": "345.8", "Line": "548.5", "Error": "NULL"},
        {
            "Sample": "176.4",  # stale carryover from the prior successful row -- must be ignored
            "Line": "866.3",
            "Error": "Requested position does not project in camera model; no surface intersection",
        },
    ]
    written_coordlist = {}

    def fake_run_quiet(cmd):
        coordlist_arg = next(a for a in cmd if a.startswith("coordlist="))
        written_coordlist["text"] = Path(coordlist_arg.removeprefix("coordlist=")).read_text()
        _fake_campt_batch_run_quiet(rows)(cmd)

    with patch.object(isis_wac, "run_quiet", side_effect=fake_run_quiet):
        pixels = isis_wac.ground_to_image_pixels_batch(model, lonlat_deg)

    assert pixels == [(345.8, 548.5), None]
    # campt.xml documents ground COORDLIST rows as (latitude, longitude), not (lon, lat) -- confirmed
    # live this matters: got a stale, wrong result for every row until this order was fixed.
    assert written_coordlist["text"] == "38.5,169.5\n39.0,170.0\n"


def test_ground_to_image_pixels_batch_passes_the_right_campt_flags():
    model = isis_wac.GroundToImageModel(cub_path=Path("/fake/crop.cub"), name_model="fake", used_csm=False)
    rows = [{"Sample": "1.0", "Line": "2.0", "Error": "NULL"}]

    with patch.object(isis_wac, "run_quiet", side_effect=_fake_campt_batch_run_quiet(rows)) as mock_run_quiet:
        isis_wac.ground_to_image_pixels_batch(model, np.array([[0.0, 0.0]]))

    cmd = mock_run_quiet.call_args[0][0]
    assert cmd[0] == "campt"
    assert "usecoordlist=true" in cmd
    assert "coordtype=ground" in cmd
    assert "format=flat" in cmd
    assert "append=false" in cmd  # campt's own APPEND default (true) would corrupt a reused path
    assert "allowerror=true" in cmd  # one bad point must not abort the whole batch
    assert "allowoutside=false" in cmd


def test_ground_to_image_pixels_batch_raises_on_row_count_mismatch():
    model = isis_wac.GroundToImageModel(cub_path=Path("/fake/crop.cub"), name_model="fake", used_csm=False)
    rows = [{"Sample": "1.0", "Line": "2.0", "Error": "NULL"}]  # only 1 row for 2 input points

    with patch.object(isis_wac, "run_quiet", side_effect=_fake_campt_batch_run_quiet(rows)):
        with pytest.raises(RuntimeError):
            isis_wac.ground_to_image_pixels_batch(model, np.array([[0.0, 0.0], [1.0, 1.0]]))


def _fake_campt_image_batch_run_quiet(rows):
    """A `run_quiet` stand-in for `image_to_ground_points_batch`: writes `rows` (dicts with
    PositiveEast360Longitude/PlanetocentricLatitude/LocalRadius/Error keys) as a FLAT-format CSV to
    whatever `to=` path the real call would have written."""

    def _side_effect(cmd):
        to_arg = next(a for a in cmd if a.startswith("to="))
        out_path = Path(to_arg.removeprefix("to="))
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=["PositiveEast360Longitude", "PlanetocentricLatitude", "LocalRadius", "Error"]
            )
            writer.writeheader()
            writer.writerows(rows)

    return _side_effect


def test_image_to_ground_points_batch_parses_rows_and_writes_coordlist_in_sample_line_order():
    rows = [
        {
            "PositiveEast360Longitude": "169.5",
            "PlanetocentricLatitude": "38.5",
            "LocalRadius": "1738500.0",
            "Error": "NULL",
        },
        {
            "PositiveEast360Longitude": "999.9",  # stale carryover from the prior row -- must be ignored
            "PlanetocentricLatitude": "999.9",
            "LocalRadius": "999.9",
            "Error": "Requested position does not project in camera model; no surface intersection",
        },
    ]
    written_coordlist = {}

    def fake_run_quiet(cmd):
        coordlist_arg = next(a for a in cmd if a.startswith("coordlist="))
        written_coordlist["text"] = Path(coordlist_arg.removeprefix("coordlist=")).read_text()
        _fake_campt_image_batch_run_quiet(rows)(cmd)

    with patch.object(isis_wac, "run_quiet", side_effect=fake_run_quiet):
        ground_points = isis_wac.image_to_ground_points_batch(
            Path("/fake/crop.cub"), np.array([[345.8, 548.5], [176.4, 866.3]])
        )

    assert ground_points == [(169.5, 38.5, 1738500.0), None]
    # campt.xml documents image COORDLIST rows as (sample, line) -- the opposite convention from
    # ground_to_image_pixels_batch's own (latitude, longitude) for coordtype=ground.
    assert written_coordlist["text"] == "345.8,548.5\n176.4,866.3\n"


def test_image_to_ground_points_batch_passes_the_right_campt_flags():
    rows = [
        {
            "PositiveEast360Longitude": "0.0",
            "PlanetocentricLatitude": "0.0",
            "LocalRadius": "1737400.0",
            "Error": "NULL",
        }
    ]

    with patch.object(isis_wac, "run_quiet", side_effect=_fake_campt_image_batch_run_quiet(rows)) as mock_run_quiet:
        isis_wac.image_to_ground_points_batch(Path("/fake/crop.cub"), np.array([[1.0, 2.0]]))

    cmd = mock_run_quiet.call_args[0][0]
    assert cmd[0] == "campt"
    assert "usecoordlist=true" in cmd
    assert "coordtype=image" in cmd
    assert "format=flat" in cmd
    assert "append=false" in cmd  # campt's own APPEND default (true) would corrupt a reused path
    assert "allowerror=true" in cmd


def test_image_to_ground_points_batch_raises_on_row_count_mismatch():
    rows = [
        {
            "PositiveEast360Longitude": "0.0",
            "PlanetocentricLatitude": "0.0",
            "LocalRadius": "1737400.0",
            "Error": "NULL",
        }
    ]  # only 1 row for 2 input pixels

    with patch.object(isis_wac, "run_quiet", side_effect=_fake_campt_image_batch_run_quiet(rows)):
        with pytest.raises(RuntimeError):
            isis_wac.image_to_ground_points_batch(Path("/fake/crop.cub"), np.array([[0.0, 0.0], [1.0, 1.0]]))
