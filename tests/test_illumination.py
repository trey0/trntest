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
