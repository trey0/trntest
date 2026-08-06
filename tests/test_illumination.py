import numpy as np
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


def test_azimuth_elevation_from_direction_overhead_is_90_elevation():
    # direction == the local "up" vector at lon=0, lat=0 is straight overhead, any azimuth is valid
    _, elevation = illumination._azimuth_elevation_from_direction(np.array([1.0, 0.0, 0.0]), 0.0, 0.0)
    assert elevation == pytest.approx(90.0)


def test_azimuth_elevation_from_direction_due_east_at_horizon():
    azimuth, elevation = illumination._azimuth_elevation_from_direction(np.array([0.0, 1.0, 0.0]), 0.0, 0.0)
    assert azimuth == pytest.approx(90.0)
    assert elevation == pytest.approx(0.0, abs=1e-9)


def test_azimuth_elevation_from_direction_due_north_at_horizon():
    azimuth, elevation = illumination._azimuth_elevation_from_direction(np.array([0.0, 0.0, 1.0]), 0.0, 0.0)
    assert azimuth == pytest.approx(0.0, abs=1e-9)
    assert elevation == pytest.approx(0.0, abs=1e-9)


def test_azimuth_elevation_from_direction_overhead_at_nonzero_lonlat():
    # "up" at lon=90, lat=0 is +Y; confirms the local frame construction generalizes past lon=0/lat=0
    _, elevation = illumination._azimuth_elevation_from_direction(np.array([0.0, 1.0, 0.0]), 90.0, 0.0)
    assert elevation == pytest.approx(90.0)
