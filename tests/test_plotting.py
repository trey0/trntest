import numpy as np

from trntest.plotting import _fill_dead_columns_for_display


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
