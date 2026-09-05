import numpy as np
import rasterio
import rasterio.transform

from trntest.sfs_plotting import plot_incidence_validation, plot_sfs_comparison


def _write_raster(path, value: float):
    transform = rasterio.transform.from_origin(0, 10, 1, 1)
    with rasterio.open(
        path, "w", driver="GTiff", height=10, width=10, count=1, dtype="float32", crs="EPSG:4326", transform=transform
    ) as dst:
        dst.write(np.full((10, 10), value, dtype="float32"), 1)


def test_plot_sfs_comparison_normalizes_each_panel_to_its_own_median(tmp_path):
    real_path = tmp_path / "real.tif"
    ours_path = tmp_path / "ours.tif"
    sim_path = tmp_path / "sim.tif"
    _write_raster(real_path, value=100.0)
    _write_raster(ours_path, value=20.0)  # 5x dimmer -- normalization should undo this
    _write_raster(sim_path, value=50.0)  # 2x dimmer

    fig = plot_sfs_comparison(real_path, ours_path, sim_path, title="test")

    axes = fig.axes
    assert len(axes) == 3
    # All three panels' displayed images should land at 1.0 -- each independently normalized to its
    # own median, not `ours`/`sim` matched up to `real`'s own raw 100.0 level (confirms the
    # normalization actually ran on all three, not just that the function returned without error).
    np.testing.assert_allclose(axes[0].images[0].get_array(), 1.0)
    np.testing.assert_allclose(axes[1].images[0].get_array(), 1.0)
    np.testing.assert_allclose(axes[2].images[0].get_array(), 1.0)


def test_plot_incidence_validation_shows_the_real_difference_field():
    incidence_sfs_deg = np.array([[10.0, 20.0], [np.nan, 40.0]])
    incidence_ours_deg = np.array([[10.5, 19.5], [np.nan, 39.0]])

    fig = plot_incidence_validation(incidence_sfs_deg, incidence_ours_deg, title="test")

    # 3 image panels + 3 colorbar axes (one per panel, unlike plot_sfs_comparison's shared scale).
    image_axes = [ax for ax in fig.axes if ax.images]
    assert len(image_axes) == 3
    np.testing.assert_allclose(image_axes[0].images[0].get_array(), incidence_sfs_deg)
    np.testing.assert_allclose(image_axes[1].images[0].get_array(), incidence_ours_deg)
    np.testing.assert_allclose(image_axes[2].images[0].get_array(), incidence_sfs_deg - incidence_ours_deg)
