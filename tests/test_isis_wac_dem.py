import csv
import dataclasses
import subprocess
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import rasterio

from trntest import isis_wac
from trntest.config import MOON_RADIUS_M, TrntestConfig
from trntest.lunaserv import DemOrthoResult, local_orthographic_crs

_POINTING_LABEL_WITH_INSTRUMENT_POINTING = """
Object = IsisCube
  Group = Kernels
    InstrumentPointing = (Table, $lro/kernels/ck/lrolc_2019334_2020001_v01.bc,
                          $lro/kernels/ck/moc42r_2019334_2020001_v01.bc,
                          $lro/kernels/fk/lro_frames_2014049_v01.tf)
  End_Group
End_Object
"""

_LABEL_WITHOUT_INSTRUMENT_POINTING = """
Object = IsisCube
  Group = Kernels
    TargetPosition = (Table, $base/kernels/spk/de430.bsp)
  End_Group
End_Object
"""


def test_run_lrowac2isis_publishes_all_four_outputs_atomically(tmp_path):
    config = dataclasses.replace(TrntestConfig(), output_dir=tmp_path / "_work" / "SOMEPRODUCT")
    edr = isis_wac.EdrFetchResult(img_path=Path("/fake/SOMEPRODUCT.IMG"))

    def fake_run_quiet(cmd):
        assert cmd[0] == "lrowac2isis"
        to_arg = next(a for a in cmd if a.startswith("to="))
        prefix = Path(to_arg.removeprefix("to="))
        # A real temp build directory, not the final canonical isis/ dir directly.
        assert prefix.parent != isis_wac._spike_dir(config)
        for suffix in isis_wac._LROWAC2ISIS_SUFFIXES:
            (prefix.parent / (prefix.name + suffix)).write_text(f"fake{suffix}")

    with patch.object(isis_wac, "run_quiet", side_effect=fake_run_quiet):
        result = isis_wac.run_lrowac2isis(edr, config)

    spike_dir = isis_wac._spike_dir(config)
    assert result.uv_even == spike_dir / "SOMEPRODUCT.uv.even.cub"
    assert result.vis_even == spike_dir / "SOMEPRODUCT.vis.even.cub"
    assert result.uv_odd == spike_dir / "SOMEPRODUCT.uv.odd.cub"
    assert result.vis_odd == spike_dir / "SOMEPRODUCT.vis.odd.cub"
    for path in (result.uv_even, result.vis_even, result.uv_odd, result.vis_odd):
        assert path.exists()
    # No leftover temp build directories.
    assert [p for p in spike_dir.iterdir() if p.is_dir()] == []


def test_is_spiceinit_complete_true_when_instrument_pointing_present():
    fake_result = subprocess.CompletedProcess(
        args=["catlab"], returncode=0, stdout=_POINTING_LABEL_WITH_INSTRUMENT_POINTING, stderr=""
    )
    with patch("subprocess.run", return_value=fake_result):
        assert isis_wac._is_spiceinit_complete(Path("/fake/vis.even.cub")) is True


def test_is_spiceinit_complete_false_when_instrument_pointing_missing():
    fake_result = subprocess.CompletedProcess(
        args=["catlab"], returncode=0, stdout=_LABEL_WITHOUT_INSTRUMENT_POINTING, stderr=""
    )
    with patch("subprocess.run", return_value=fake_result):
        assert isis_wac._is_spiceinit_complete(Path("/fake/vis.even.cub")) is False


def test_is_spiceinit_complete_false_when_catlab_fails():
    fake_result = subprocess.CompletedProcess(args=["catlab"], returncode=1, stdout="", stderr="**ERROR**")
    with patch("subprocess.run", return_value=fake_result):
        assert isis_wac._is_spiceinit_complete(Path("/fake/vis.even.cub")) is False


def test_spiceinit_vis_even_cube_rebuilds_when_file_exists_but_not_yet_spiceinit(tmp_path):
    # The real bug this is a regression test for: a concurrent worker's own run_lrowac2isis call can
    # make vis_even.cub exist (real, complete, atomically published) before spiceinit has run on it --
    # a bare existence check used to treat that as "already done" and return a not-yet-spiceinit'd
    # cube, causing a real KeyError('InstrumentPointing') downstream.
    config = dataclasses.replace(TrntestConfig(), output_dir=tmp_path / "_work" / "SOMEPRODUCT")
    spike_dir = isis_wac._spike_dir(config)
    vis_even_path = spike_dir / "SOMEPRODUCT.vis.even.cub"
    vis_even_path.write_text("real but not yet spiceinit'd")

    with patch.object(
        isis_wac, "fetch_edr_img", return_value=isis_wac.EdrFetchResult(img_path=Path("SOMEPRODUCT.IMG"))
    ):
        with patch.object(isis_wac, "_is_spiceinit_complete", return_value=False):
            with patch.object(isis_wac, "ensure_isisdata"):
                with patch.object(
                    isis_wac,
                    "run_lrowac2isis",
                    return_value=isis_wac.Lrowac2IsisResult(
                        uv_even=Path("uv_even"), vis_even=vis_even_path, uv_odd=Path("uv_odd"), vis_odd=Path("vis_odd")
                    ),
                ) as mock_run_lrowac2isis:
                    with patch.object(isis_wac, "run_spiceinit") as mock_run_spiceinit:
                        result = isis_wac._spiceinit_vis_even_cube(config)

    assert result == vis_even_path
    mock_run_lrowac2isis.assert_called_once()
    mock_run_spiceinit.assert_called_once_with(vis_even_path, config)


def test_spiceinit_vis_even_cube_reuses_when_already_spiceinit(tmp_path):
    config = dataclasses.replace(TrntestConfig(), output_dir=tmp_path / "_work" / "SOMEPRODUCT")
    spike_dir = isis_wac._spike_dir(config)
    vis_even_path = spike_dir / "SOMEPRODUCT.vis.even.cub"
    vis_even_path.write_text("already spiceinit'd")

    with patch.object(
        isis_wac, "fetch_edr_img", return_value=isis_wac.EdrFetchResult(img_path=Path("SOMEPRODUCT.IMG"))
    ):
        with patch.object(isis_wac, "_is_spiceinit_complete", return_value=True):
            with patch.object(isis_wac, "run_lrowac2isis") as mock_run_lrowac2isis:
                with patch.object(isis_wac, "run_spiceinit") as mock_run_spiceinit:
                    result = isis_wac._spiceinit_vis_even_cube(config)

    assert result == vis_even_path
    mock_run_lrowac2isis.assert_not_called()
    mock_run_spiceinit.assert_not_called()


def test_spike_dir_lands_under_work_entry_isis(tmp_path):
    # docs/intermediate-product-plan.md's Phase 3: _work/<entry>/isis/, not the old, workspace-level
    # scratch_dir/isis_wac/<edr_product>/ -- driven by output_dir (the entry's own root), not
    # scratch_dir, which this call deliberately leaves untouched.
    config = dataclasses.replace(TrntestConfig(), output_dir=tmp_path / "_work" / "SOMEPRODUCT", scratch_dir=tmp_path)
    d = isis_wac._spike_dir(config)
    assert d == tmp_path / "_work" / "SOMEPRODUCT" / "isis"
    assert d.is_dir()


def _write_ortho_style_tif(path, center_lon_deg, center_lat_deg, resolution_m):
    crs = local_orthographic_crs(center_lon_deg, center_lat_deg, MOON_RADIUS_M)
    transform = rasterio.transform.from_origin(-500.0, 500.0, resolution_m, resolution_m)
    with rasterio.open(
        path, "w", driver="GTiff", height=10, width=10, count=1, dtype="float32", crs=crs, transform=transform
    ) as dst:
        dst.write(np.zeros((10, 10), dtype="float32"), 1)


def test_run_cam2map_for_crop_writes_under_work_entry_crop(tmp_path):
    # docs/intermediate-product-plan.md's Phase 3: generator-scoped _work/<entry>/crop/<label> --
    # not alongside crop.cub_path itself (which, post-migration, lives under _work/<entry>/isis/).
    config = dataclasses.replace(TrntestConfig(), output_dir=tmp_path / "_work" / "SOMEPRODUCT")
    dem_path = tmp_path / "dem.tif"
    _write_ortho_style_tif(dem_path, center_lon_deg=10.0, center_lat_deg=-5.0, resolution_m=100.0)
    dem_ortho_result = DemOrthoResult(
        ortho=dem_path, dem=dem_path, bbox=(-500.0, -500.0, 500.0, 500.0), width=10, height=10
    )
    crop = isis_wac.CropResult(cub_path=tmp_path / "_work" / "SOMEPRODUCT" / "isis" / "SOMEPRODUCT.crop.cub")

    def fake_run_quiet(cmd):
        # atomic_publish_path yields a temp path, not the final one -- create a file there so the
        # real rename-on-success has something to rename, matching what a real cam2map/gdal_translate
        # call would actually leave behind.
        if cmd[0] == "cam2map":
            to_arg = next(a for a in cmd if a.startswith("to="))
            Path(to_arg.removeprefix("to=")).write_text("fake cub")
        elif cmd[0] == "gdal_translate":
            Path(cmd[-1]).write_text("fake tif")

    with patch.object(isis_wac, "run_quiet", side_effect=fake_run_quiet) as mock_run_quiet:
        mapproj_tif = isis_wac.run_cam2map_for_crop(crop, dem_ortho_result, config)

    expected_dir = config.output_dir / "crop"
    assert mapproj_tif == expected_dir / "SOMEPRODUCT.crop-cam2map.tif"
    assert mapproj_tif.read_text() == "fake tif"
    assert mock_run_quiet.call_count == 2
    cam2map_cmd = mock_run_quiet.call_args_list[0].args[0]
    assert cam2map_cmd[0] == "cam2map"
    to_arg = next(a for a in cam2map_cmd if a.startswith("to="))
    assert Path(to_arg.removeprefix("to=")).parent == expected_dir
    assert any(str(expected_dir) in arg for arg in cam2map_cmd if arg.startswith("map="))


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


def test_sample_local_dem_patch_subtracts_moon_radius_and_reshapes_to_3x3():
    fake_radii = MOON_RADIUS_M + np.arange(9, dtype=float)  # distinct, traceable values
    with patch.object(isis_wac, "sample_lunar_dem_radii_batch", return_value=fake_radii) as mock_sample:
        patch_arr = isis_wac.sample_local_dem_patch(10.0, 20.0, cellsize_m=100.0, config=TrntestConfig())

    assert patch_arr.shape == (3, 3)
    assert patch_arr.flatten() == pytest.approx(np.arange(9, dtype=float))
    # 9 points sampled, one real mappt call (batched), not 9 separate ones.
    assert mock_sample.call_count == 1
    lonlat_deg = mock_sample.call_args[0][0]
    assert lonlat_deg.shape == (9, 2)


def test_sample_local_dem_patch_orders_rows_north_first():
    # Real geographic check (not just reshape mechanics): the first 3 sampled points (row 0 of the
    # returned patch) must be the *northernmost* offset row, matching
    # `_terrain_photometric_angles`'s own "row 0 = north/top" convention -- verified against the
    # actual (lon, lat) coordinates passed to `sample_lunar_dem_radii_batch`, not assumed from the
    # code's own construction order.
    with patch.object(isis_wac, "sample_lunar_dem_radii_batch", return_value=np.zeros(9)) as mock_sample:
        isis_wac.sample_local_dem_patch(10.0, 20.0, cellsize_m=1000.0, config=TrntestConfig())

    lonlat_deg = mock_sample.call_args[0][0]
    lats = lonlat_deg[:, 1]
    north_row_lat = lats[0:3].mean()
    center_row_lat = lats[3:6].mean()
    south_row_lat = lats[6:9].mean()
    assert north_row_lat > center_row_lat > south_row_lat
