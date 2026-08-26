import numpy as np
import pytest
import rasterio
import rasterio.transform

from trntest.plotting import (
    _fill_dead_columns_for_display,
    _prep_overlay_rasters,
    compute_brightness_matched_diff,
    plot_incidence_validation,
    plot_sfs_comparison,
)


def _write_raster(path, value: float):
    transform = rasterio.transform.from_origin(0, 10, 1, 1)
    with rasterio.open(
        path, "w", driver="GTiff", height=10, width=10, count=1, dtype="float32", crs="EPSG:4326", transform=transform
    ) as dst:
        dst.write(np.full((10, 10), value, dtype="float32"), 1)


def _write_pattern_raster(path, data: np.ndarray, origin_x: float = 0.0, origin_y: float = 10.0):
    height, width = data.shape
    transform = rasterio.transform.from_origin(origin_x, origin_y, 1, 1)
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


def test_fill_dead_columns_interpolates_single_gap():
    band = np.array([[1.0, 1.0, 99.0, 3.0, 3.0]])
    valid = np.array([[True, True, False, True, True]])
    filled = _fill_dead_columns_for_display(band, valid)
    assert filled[0, 2] == 2.0  # midpoint between the neighboring 1.0/3.0 valid values


def test_fill_dead_columns_clamps_at_edge():
    # column 0 has no left neighbor -- should clamp to the nearest valid value, not extrapolate.
    band = np.array([[99.0, 5.0, 5.0]])
    valid = np.array([[False, True, True]])
    filled = _fill_dead_columns_for_display(band, valid)
    assert filled[0, 0] == 5.0


def test_fill_dead_columns_leaves_fully_valid_row_untouched():
    band = np.array([[1.0, 2.0, 3.0]])
    valid = np.array([[True, True, True]])
    filled = _fill_dead_columns_for_display(band, valid)
    assert np.array_equal(filled, band)


def test_fill_dead_columns_nans_out_fully_invalid_row():
    band = np.array([[9.0, 9.0], [1.0, 2.0]])
    valid = np.array([[False, False], [True, True]])
    filled = _fill_dead_columns_for_display(band, valid)
    assert np.all(np.isnan(filled[0]))
    assert np.array_equal(filled[1], [1.0, 2.0])


def test_prep_overlay_rasters_brightness_matches_overlay_median_to_base(tmp_path):
    base_path, overlay_path = tmp_path / "base.tif", tmp_path / "overlay.tif"
    _write_raster(base_path, value=100.0)
    _write_raster(overlay_path, value=20.0)  # 5x dimmer than base -- scale should correct this

    base, overlay, overlay_display, base_vmin, base_vmax, overlay_vmin, overlay_vmax = _prep_overlay_rasters(
        base_path, overlay_path, fill_overlay_nodata=False
    )

    assert np.nanmedian(overlay_display.values) == pytest.approx(np.nanmedian(base.values))
    # Both panels share the same display range post-match, not independently-normalized ones --
    # an independent stretch would silently re-normalize the brightness match away.
    assert overlay_vmin == base_vmin
    assert overlay_vmax == base_vmax


def test_prep_overlay_rasters_skips_scaling_when_overlay_median_is_zero(tmp_path):
    base_path, overlay_path = tmp_path / "base.tif", tmp_path / "overlay.tif"
    _write_raster(base_path, value=100.0)
    _write_raster(overlay_path, value=0.0)

    base, overlay, overlay_display, base_vmin, base_vmax, overlay_vmin, overlay_vmax = _prep_overlay_rasters(
        base_path, overlay_path, fill_overlay_nodata=False
    )

    # Division by a zero median is guarded against, not attempted -- overlay stays unscaled (all-zero).
    assert np.nanmedian(overlay_display.values) == 0.0


def test_compute_brightness_matched_diff_uniform_rasters_diff_to_zero_regardless_of_absolute_level(tmp_path):
    # Two spatially-uniform rasters at very different absolute brightness levels -- after the single
    # multiplicative brightness match, both are uniform and equal, so the real diff is exactly zero
    # regardless of the original 5x level difference.
    base_path, overlay_path = tmp_path / "base.tif", tmp_path / "overlay.tif"
    _write_raster(base_path, value=100.0)
    _write_raster(overlay_path, value=20.0)

    result = compute_brightness_matched_diff(base_path, overlay_path)
    assert result.mean_abs_diff == pytest.approx(0.0, abs=1e-6)
    assert result.median_abs_diff == pytest.approx(0.0, abs=1e-6)
    assert result.valid_pixel_count == 100


def test_compute_brightness_matched_diff_reflects_real_pattern_difference_after_brightness_match(tmp_path):
    # Same median (so brightness-match scale is exactly 1, isolating the real pattern mismatch): base
    # alternates 90/110 around a median of 100, overlay is uniformly 100 everywhere -- the real,
    # physically meaningful difference this metric exists to catch, not just an absolute-level offset.
    base_path, overlay_path = tmp_path / "base.tif", tmp_path / "overlay.tif"
    checkerboard = np.indices((10, 10)).sum(axis=0) % 2
    base_data = np.where(checkerboard == 0, 90.0, 110.0)
    _write_pattern_raster(base_path, base_data)
    _write_raster(overlay_path, value=100.0)

    result = compute_brightness_matched_diff(base_path, overlay_path)
    assert result.mean_abs_diff == pytest.approx(10.0, abs=1e-4)
    assert result.valid_pixel_count == 100


def test_compute_brightness_matched_diff_aligns_rasters_with_different_extents_by_real_coordinate(tmp_path):
    # base covers a full 10x10 extent; overlay covers only a smaller 4x4 window inside it, at a
    # different origin -- the real shape this project's own rasters take (e.g. a padded DEM/ortho AOI
    # vs. a real crop's own smaller footprint). Must align by real coordinate (not raise on shape
    # mismatch, not silently misalign by raw array position) and restrict the diff to the real overlap.
    base_path, overlay_path = tmp_path / "base.tif", tmp_path / "overlay.tif"
    _write_raster(base_path, value=100.0)
    # 4x4 window starting at (x=2, y=8) in the same 1-degree-pixel grid as base -- entirely inside it.
    _write_pattern_raster(overlay_path, np.full((4, 4), 20.0), origin_x=2.0, origin_y=8.0)

    result = compute_brightness_matched_diff(base_path, overlay_path)
    assert result.valid_pixel_count == 16
    assert result.mean_abs_diff == pytest.approx(0.0, abs=1e-6)  # uniform rasters -> brightness match closes the gap


def test_plot_sfs_comparison_brightness_matches_both_overlays_to_the_real_panel(tmp_path):
    real_path = tmp_path / "real.tif"
    ours_path = tmp_path / "ours.tif"
    sim_path = tmp_path / "sim.tif"
    _write_raster(real_path, value=100.0)
    _write_raster(ours_path, value=20.0)  # 5x dimmer -- brightness match should undo this
    _write_raster(sim_path, value=50.0)  # 2x dimmer

    fig = plot_sfs_comparison(real_path, ours_path, sim_path, title="test")

    axes = fig.axes
    assert len(axes) == 3
    # Each overlay panel's displayed image should be brightness-matched up to the real panel's own
    # level (100.0), not left at its own raw 20.0/50.0 -- confirms the scaling actually ran, not just
    # that the function returned without error.
    np.testing.assert_allclose(axes[1].images[0].get_array(), 100.0)
    np.testing.assert_allclose(axes[2].images[0].get_array(), 100.0)


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
