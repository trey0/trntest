import numpy as np
import pytest
import rasterio
import rasterio.transform

from trntest.plotting import _fill_dead_columns_for_display, _prep_overlay_rasters


def _write_raster(path, value: float):
    transform = rasterio.transform.from_origin(0, 10, 1, 1)
    with rasterio.open(
        path, "w", driver="GTiff", height=10, width=10, count=1, dtype="float32", crs="EPSG:4326", transform=transform
    ) as dst:
        dst.write(np.full((10, 10), value, dtype="float32"), 1)


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
