import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trntest import sfs_validation


def test_hapke_params_to_asp_model_coeffs_orders_as_omega_b_c_b0_h():
    hapkehen_params = {"wh": 0.4, "hg1": 0.2, "hg2": 0.5, "hh": 0.05, "b0": 1.6, "theta": 24.0}
    result = sfs_validation.hapke_params_to_asp_model_coeffs(hapkehen_params)
    assert result == "0.4 0.2 0.5 1.6 0.05"


def test_true_albedo_map_divides_shaded_ortho_by_real_reflectance():
    shaded_ortho = np.array([[0.0, 127.5], [255.0, 63.75]])
    real_reflectance = np.full((2, 2), 2.0)
    albedo = sfs_validation.true_albedo_map(shaded_ortho, real_reflectance)
    np.testing.assert_allclose(albedo, [[0.0, 0.25], [0.5, 0.125]])


def test_true_albedo_map_recovers_raw_ortho_over_reference_reflectance_algebraically():
    # shaded = raw_norm * H(real)/H(reference); dividing back out H(real) (this function's own
    # argument) should algebraically recover raw_norm/H(reference) exactly -- the actual
    # reference-geometry-normalization-undone quantity this function exists to compute (see its own
    # docstring for the real double-counting bug an earlier version had by dividing by the constant
    # H(reference) instead of this per-pixel H(real)).
    raw_norm = np.array([[0.4, 0.8]])
    h_real = np.array([[1.5, 0.6]])
    h_reference = 1.2
    shaded_ortho = np.clip(raw_norm * h_real / h_reference * 255.0, 0, 255)

    albedo = sfs_validation.true_albedo_map(shaded_ortho, h_real)

    np.testing.assert_allclose(albedo, raw_norm / h_reference, atol=1e-6)


def test_true_albedo_map_guards_against_zero_real_reflectance():
    shaded_ortho = np.array([[100.0, 200.0]])
    real_reflectance = np.array([[0.0, 2.0]])
    albedo = sfs_validation.true_albedo_map(shaded_ortho, real_reflectance)
    np.testing.assert_allclose(albedo, [[0.0, (200.0 / 255.0) / 2.0]])


def test_true_albedo_map_guards_against_non_finite_real_reflectance():
    shaded_ortho = np.array([[100.0, 200.0]])
    real_reflectance = np.array([[np.nan, 2.0]])
    albedo = sfs_validation.true_albedo_map(shaded_ortho, real_reflectance)
    np.testing.assert_allclose(albedo, [[0.0, (200.0 / 255.0) / 2.0]])


def _write_raster(path, data: np.ndarray):
    height, width = data.shape
    transform = from_origin(0.0, 10.0, 1, 1)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(data.astype("float32"), 1)


def test_mask_sfs_uncovered_replaces_zero_with_nan(tmp_path):
    sim_path, out_path = tmp_path / "sim.tif", tmp_path / "sim-masked.tif"
    _write_raster(sim_path, np.array([[0.0, 5.0], [10.0, 0.0]]))

    sfs_validation.mask_sfs_uncovered(sim_path, out_path)

    with rasterio.open(out_path) as src:
        data = src.read(1)
        assert src.nodata is not None and np.isnan(src.nodata)
    assert np.isnan(data[0, 0])
    assert np.isnan(data[1, 1])
    assert data[0, 1] == 5.0
    assert data[1, 0] == 10.0


def test_mask_sfs_uncovered_leaves_all_nonzero_data_untouched(tmp_path):
    sim_path, out_path = tmp_path / "sim.tif", tmp_path / "sim-masked.tif"
    data_in = np.array([[1.0, 2.0], [3.0, 4.0]])
    _write_raster(sim_path, data_in)

    sfs_validation.mask_sfs_uncovered(sim_path, out_path)

    with rasterio.open(out_path) as src:
        np.testing.assert_allclose(src.read(1), data_in)


def test_incidence_deg_from_lambertian_sim_intensity_inverts_exposure_times_cos(tmp_path):
    exposure = 140.0
    known_incidence_deg = np.array([[0.0, 30.0], [60.0, 89.0]])
    sim_path = tmp_path / "sim.tif"
    _write_raster(sim_path, exposure * np.cos(np.radians(known_incidence_deg)))

    incidence_deg = sfs_validation.incidence_deg_from_lambertian_sim_intensity(sim_path, exposure)

    np.testing.assert_allclose(incidence_deg, known_incidence_deg, atol=1e-4)


def test_incidence_deg_from_lambertian_sim_intensity_masks_zero_as_no_coverage(tmp_path):
    exposure = 140.0
    sim_path = tmp_path / "sim.tif"
    _write_raster(sim_path, np.array([[0.0, exposure], [exposure * 0.5, 0.0]]))

    incidence_deg = sfs_validation.incidence_deg_from_lambertian_sim_intensity(sim_path, exposure)

    assert np.isnan(incidence_deg[0, 0])
    assert np.isnan(incidence_deg[1, 1])
    assert incidence_deg[0, 1] == pytest.approx(0.0, abs=1e-4)
    assert incidence_deg[1, 0] == pytest.approx(60.0, abs=1e-4)
