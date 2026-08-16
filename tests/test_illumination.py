import numpy as np
import pytest

from trntest import illumination


def test_circular_distance_deg_across_the_antimeridian():
    assert illumination.circular_distance_deg(-179.0, 179.0) == pytest.approx(2.0)


def test_circular_distance_deg_no_wraparound_needed():
    assert illumination.circular_distance_deg(10.0, 40.0) == pytest.approx(30.0)


def test_circular_distance_deg_is_symmetric():
    assert illumination.circular_distance_deg(-179.0, 179.0) == illumination.circular_distance_deg(179.0, -179.0)


def test_circular_mean_deg_across_the_antimeridian():
    assert illumination.circular_mean_deg(-170.0, 160.0) == pytest.approx(175.0)


def test_circular_mean_deg_no_wraparound_needed():
    assert illumination.circular_mean_deg(10.0, 40.0) == pytest.approx(25.0)


def test_circular_mean_deg_matches_plain_average_far_from_the_antimeridian():
    assert illumination.circular_mean_deg(-30.0, 30.0) == pytest.approx(0.0)


def test_unwrap_relative_deg_pushes_the_angle_past_the_antimeridian():
    # From a reference near +180, an angle just past -180 is really just a few degrees further on.
    assert illumination.unwrap_relative_deg(175.0, -179.0) == pytest.approx(181.0)


def test_unwrap_relative_deg_is_a_no_op_far_from_the_antimeridian():
    assert illumination.unwrap_relative_deg(10.0, 40.0) == pytest.approx(40.0)


def test_unwrap_relative_deg_round_trips_through_wrap_deg():
    unwrapped = illumination.unwrap_relative_deg(175.0, -179.0)
    assert illumination._wrap_deg(unwrapped) == pytest.approx(-179.0)


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
