import dataclasses
import types
from pathlib import Path

import numpy as np
import pytest
import rasterio

from trntest import isis_wac, wac_resample
from trntest.config import TrntestConfig
from trntest.pose_alignment import wac_camera_model
from trntest.wac_format import VIS_BLOCK_HEIGHT

N_COLS = 40
N_FRAMELETS = 6
STRIDE = 10  # ground lines between framelet starts; < VIS_BLOCK_HEIGHT, so framelets overlap


def _band_from(f, n_framelets=N_FRAMELETS):
    # Framelet k's within-line j (1-based) sees global ground line STRIDE*k + j.
    band = np.empty((n_framelets * VIS_BLOCK_HEIGHT, N_COLS))
    samples = np.arange(1, N_COLS + 1)
    for k in range(n_framelets):
        for j in range(1, VIS_BLOCK_HEIGHT + 1):
            band[k * VIS_BLOCK_HEIGHT + j - 1] = f(samples, STRIDE * k + j)
    return band


def _fake_geometry(monkeypatch, ground_xy):
    # Ground point = (sample, global line, 0); "projecting" through framelet k subtracts its offset.
    ground = np.zeros((*ground_xy.shape[:2], 3))
    ground[..., :2] = ground_xy
    positions = np.array([[0.0, STRIDE * k, 0.0] for k in range(N_FRAMELETS)])
    rotations = np.repeat(np.eye(3)[None], N_FRAMELETS, axis=0)
    monkeypatch.setattr(wac_resample, "framelet_poses", lambda crop_cub, n_lines: (positions, rotations))
    monkeypatch.setattr(wac_resample, "shape_model_radius_on_grid", lambda grid, config: None)
    monkeypatch.setattr(wac_resample, "grid_ground_points_me_m", lambda grid, radius: ground)
    monkeypatch.setattr(wac_resample, "project", lambda g, pos, rot: (g[:, 0] - pos[:, 0], g[:, 1] - pos[:, 1]))
    return wac_resample.MapGrid(shape=ground_xy.shape[:2], transform=rasterio.Affine.identity(), crs=None)


def _interior_ground(n=30, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.uniform(4, N_COLS - 4, n)
    y = rng.uniform(STRIDE + 3, STRIDE * (N_FRAMELETS - 1), n)
    return np.stack([x, y], axis=-1)[None]


def test_cubic_reproduces_a_quadratic_exactly_within_a_framelet():
    def f(s, line):
        return 0.3 * s**2 - 0.2 * s * line + 0.1 * line**2 + s

    band = _band_from(f)
    framelet = np.array([2, 2, 3])
    sample = np.array([5.3, 20.75, 11.5])
    within_line = np.array([3.2, 7.9, 10.4])
    value = wac_resample._cubic_within_framelet(band, framelet, sample, within_line)
    np.testing.assert_allclose(value, f(sample, STRIDE * framelet + within_line), rtol=1e-12)


def test_cubic_never_reads_another_framelet():
    band = np.zeros((2 * VIS_BLOCK_HEIGHT, N_COLS))
    band[VIS_BLOCK_HEIGHT:] = 1e6
    value = wac_resample._cubic_within_framelet(band, np.array([0]), np.array([10.5]), np.array([13.6]))
    assert value[0] == 0.0


def test_cubic_is_nan_when_a_weighted_tap_is_nan():
    band = np.ones((VIS_BLOCK_HEIGHT, N_COLS))
    band[5, 12] = np.nan  # 0-based row 5 = within-line 6; col 12 = sample 13
    near = wac_resample._cubic_within_framelet(band, np.array([0]), np.array([12.5]), np.array([5.5]))
    far = wac_resample._cubic_within_framelet(band, np.array([0]), np.array([30.5]), np.array([5.5]))
    assert np.isnan(near[0]) and far[0] == 1.0


def test_keys_weights_sum_to_one():
    frac = np.linspace(0.0, 1.0, 11)
    total = sum(wac_resample._keys_weight(frac - d) for d in (-1, 0, 1, 2))
    np.testing.assert_allclose(total, 1.0, rtol=1e-12)


def test_resample_crop_is_exact_on_a_quadratic(monkeypatch):
    def f(s, line):
        return 0.01 * s**2 + 0.02 * line**2 - 0.03 * s * line

    ground_xy = _interior_ground()
    grid = _fake_geometry(monkeypatch, ground_xy)
    result = wac_resample.resample_crop(Path("fake.cub"), _band_from(f), grid, TrntestConfig())
    np.testing.assert_allclose(result.value, f(ground_xy[..., 0], ground_xy[..., 1]), rtol=1e-10)
    # Chosen framelet: the one whose center line is nearest.
    within = result.line - result.framelet * VIS_BLOCK_HEIGHT
    assert np.all(np.abs(within - (VIS_BLOCK_HEIGHT + 1) / 2) <= STRIDE / 2 + 1e-9)


def test_resample_crop_steers_around_a_null_line(monkeypatch):
    def f(s, line):
        return 2.0 * s - 0.5 * line

    band = _band_from(f)
    ground_xy = np.array([[[15.3, STRIDE * 2 + 12.2]]])  # overlap: framelet 2 line 12.2, framelet 3 line 2.2
    grid = _fake_geometry(monkeypatch, ground_xy)
    assert wac_resample.resample_crop(Path("fake.cub"), band, grid, TrntestConfig()).framelet[0, 0] == 2
    band[2 * VIS_BLOCK_HEIGHT + 11, :] = np.nan  # NULL line 12 of framelet 2
    result = wac_resample.resample_crop(Path("fake.cub"), band, grid, TrntestConfig())
    assert result.framelet[0, 0] == 3
    np.testing.assert_allclose(result.value[0, 0], f(15.3, STRIDE * 2 + 12.2), rtol=1e-12)


def test_resample_crop_falls_back_to_valid_tap_average_next_to_a_dead_column(monkeypatch):
    band = _band_from(lambda s, line: np.full_like(s, 3.0, dtype=float))
    band[:, 0] = np.nan  # dead first column, in every framelet
    ground_xy = np.array([[[1.6, STRIDE * 2 + 7.0]]])  # between columns 1 (dead) and 2
    grid = _fake_geometry(monkeypatch, ground_xy)
    result = wac_resample.resample_crop(Path("fake.cub"), band, grid, TrntestConfig())
    assert result.value[0, 0] == pytest.approx(3.0)


def test_footprint_window_trims_and_keeps_the_lattice():
    grid = wac_resample.MapGrid(
        shape=(10, 12), transform=rasterio.Affine(100.0, 0.0, -600.0, 0.0, -100.0, 500.0), crs=None
    )
    values = np.full(grid.shape, np.nan)
    values[3:7, 2:9] = 1.0
    window, trimmed = wac_resample._footprint_window(values, grid)
    assert trimmed.shape == (4, 7) and np.all(values[window] == 1.0)
    assert (trimmed.transform.c, trimmed.transform.f) == (-600.0 + 2 * 100.0, 500.0 - 3 * 100.0)


def test_map_project_crop_dispatches_on_config(monkeypatch):
    calls = []
    monkeypatch.setattr(wac_resample, "resample_crop_to_map", lambda *a: calls.append("wac_resample") or "a")
    monkeypatch.setattr(isis_wac, "run_cam2map_for_crop", lambda *a: calls.append("cam2map") or "b")
    for method in ("wac_resample", "cam2map"):
        wac_resample.map_project_crop(None, None, dataclasses.replace(TrntestConfig(), crop_map_projection=method))
    assert calls == ["wac_resample", "cam2map"]
    with pytest.raises(ValueError):
        wac_resample.map_project_crop(None, None, dataclasses.replace(TrntestConfig(), crop_map_projection="nope"))


def test_distort_matches_the_scalar_camera_model():
    ux = np.array([0.0, 1.3, -4.2, 5.9, -0.01])
    uy = np.array([0.0, -2.2, 3.1, 0.4, 6.3])
    xt, yt = wac_resample._distort(ux, uy)
    rr = xt * xt + yt * yt
    k1, k2, k3 = wac_camera_model.OD_K
    dr = 1.0 + k1 * rr + k2 * rr**2 + k3 * rr**3
    np.testing.assert_allclose((xt * dr, yt * dr), (ux, uy), atol=1e-12)
    # The scalar fixed-point version stops at its iteration cap ~5e-6 mm short near the detector edge.
    for i in range(ux.size):
        expected = wac_camera_model._distort(float(ux[i]), float(uy[i]))
        np.testing.assert_allclose((xt[i], yt[i]), expected, atol=1e-5)


def test_fill_small_holes_fills_enclosed_holes_only():
    values = np.ones((8, 8))
    values[3, 3] = np.nan  # enclosed
    values[0, 5] = np.nan  # touches the outside
    filled, mask = wac_resample.fill_small_holes(values)
    assert filled[3, 3] == 1.0 and mask[3, 3]
    assert np.isnan(filled[0, 5]) and not mask[0, 5]


def _fake_resample_to_map(monkeypatch, tmp_path, values):
    grid = wac_resample.MapGrid(
        shape=values.shape,
        transform=rasterio.Affine(100.0, 0.0, 0.0, 0.0, -100.0, 0.0),
        crs=rasterio.crs.CRS.from_proj4("+proj=ortho +lat_0=0 +lon_0=0 +R=1737400 +units=m"),
    )
    monkeypatch.setattr(wac_resample.spice_kernels, "fetch_and_furnish", lambda *a: None)
    monkeypatch.setattr(wac_resample.isis_wac, "cube_start_time", lambda cub: None)
    monkeypatch.setattr(wac_resample.MapGrid, "from_raster", staticmethod(lambda path: grid))
    monkeypatch.setattr(wac_resample, "read_band", lambda cub: None)
    monkeypatch.setattr(
        wac_resample,
        "resample_crop",
        lambda *a: wac_resample.Resampled(value=values, framelet=None, sample=None, line=None),
    )
    return isis_wac.CropResult(cub_path=tmp_path / "X_crop.cub"), types.SimpleNamespace(dem=None)


@pytest.mark.parametrize("write_mask", [False, True])
def test_resample_crop_to_map_records_filled_pixels(monkeypatch, tmp_path, capsys, write_mask):
    values = np.full((12, 12), np.nan)
    values[2:10, 2:10] = 1.0
    values[4, 4] = np.nan  # small hole: filled
    values[6:9, 6:9] = np.nan  # 9 px hole: left as nodata, reported
    crop, dem = _fake_resample_to_map(monkeypatch, tmp_path, values)
    config = dataclasses.replace(TrntestConfig(), output_dir=tmp_path, crop_map_write_fill_mask=write_mask)

    out = wac_resample.resample_crop_to_map(crop, dem, config)

    with rasterio.open(out) as src:
        assert src.shape == (8, 8) and src.tags()["FILLED_PERCENT"] == "1.6%"  # 1 of 64 px
    mask_path = out.with_name("X_crop-resampled-filled.tif")
    assert mask_path.exists() == write_mask
    if write_mask:
        with rasterio.open(mask_path) as src:
            mask = src.read(1)
        assert mask.sum() == 1 and mask[2, 2] == 1
    assert "14% of the footprint (9 px) is in enclosed holes larger than 4 px" in capsys.readouterr().out
