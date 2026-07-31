import math

import pytest

from trntest import illumination


def test_terminator_offset_deg_at_subsolar_meridian():
    assert illumination.terminator_offset_deg(0.0, 0.0) == pytest.approx(90.0)


def test_terminator_offset_deg_at_terminator():
    assert illumination.terminator_offset_deg(90.0, 0.0) == pytest.approx(0.0, abs=1e-9)
    assert illumination.terminator_offset_deg(-90.0, 0.0) == pytest.approx(0.0, abs=1e-9)


def test_terminator_offset_deg_at_antisolar_meridian():
    assert illumination.terminator_offset_deg(180.0, 0.0) == pytest.approx(90.0)


def test_terminator_offset_deg_handles_wraparound():
    assert illumination.terminator_offset_deg(270.0, 0.0) == pytest.approx(0.0, abs=1e-9)


def test_terminator_offset_deg_nonzero_sub_solar_longitude():
    assert illumination.terminator_offset_deg(200.0, 110.0) == pytest.approx(0.0, abs=1e-9)
    assert illumination.terminator_offset_deg(110.0, 110.0) == pytest.approx(90.0)


def test_terminator_offset_deg_partway_to_terminator():
    assert illumination.terminator_offset_deg(100.0, 0.0) == pytest.approx(10.0)


def test_find_sign_change_crossings_linear():
    crossings = illumination.find_sign_change_crossings(lambda t: t - 5.0, 0.0, 10.0, coarse_step_s=1.0, tol_s=1e-6)
    assert len(crossings) == 1
    assert crossings[0] == pytest.approx(5.0, abs=1e-5)


def test_find_sign_change_crossings_no_crossing_returns_empty():
    crossings = illumination.find_sign_change_crossings(lambda t: t + 5.0, 0.0, 10.0, coarse_step_s=1.0, tol_s=1e-6)
    assert crossings == []


def test_find_sign_change_crossings_periodic():
    offset = 0.001  # avoid an exact zero landing on a coarse sample boundary
    crossings = illumination.find_sign_change_crossings(
        lambda t: math.sin(t - offset), 0.0, 4 * math.pi, coarse_step_s=0.05, tol_s=1e-6
    )
    expected = [offset, math.pi + offset, 2 * math.pi + offset, 3 * math.pi + offset]
    assert len(crossings) == len(expected)
    for actual, exp in zip(crossings, expected, strict=True):
        assert actual == pytest.approx(exp, abs=1e-4)
