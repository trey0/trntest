import math

import pytest

from trntest import geo_utils
from trntest.config import MOON_RADIUS_M


def test_geographic_crs_default_radius():
    assert geo_utils.geographic_crs() == f"+proj=longlat +R={MOON_RADIUS_M} +no_defs"


def test_geographic_crs_explicit_radius():
    assert geo_utils.geographic_crs(1_000_000.0) == "+proj=longlat +R=1000000.0 +no_defs"


def test_local_orthographic_crs_default_radius():
    assert geo_utils.local_orthographic_crs(10.0, -20.0) == (
        f"+proj=ortho +lon_0=10.0 +lat_0=-20.0 +R={MOON_RADIUS_M} +units=m +no_defs"
    )


def test_local_orthographic_crs_explicit_radius():
    assert geo_utils.local_orthographic_crs(10.0, -20.0, 1_000_000.0) == (
        "+proj=ortho +lon_0=10.0 +lat_0=-20.0 +R=1000000.0 +units=m +no_defs"
    )


def test_moon_geocentric_crs_default_radius():
    assert geo_utils.moon_geocentric_crs() == f"+proj=geocent +R={MOON_RADIUS_M} +units=m +no_defs"


def test_moon_geocentric_crs_explicit_radius():
    assert geo_utils.moon_geocentric_crs(1_000_000.0) == "+proj=geocent +R=1000000.0 +units=m +no_defs"


def test_footprint_bbox_deg_no_wraparound():
    footprint = {"a": (170.0, 40.0), "b": (175.0, 42.0), "c": (172.0, 41.0), "d": (178.0, 39.0)}
    bbox = geo_utils.footprint_bbox_deg(footprint)
    assert bbox == pytest.approx((170.0, 39.0, 178.0, 42.0))


def test_footprint_bbox_deg_antimeridian_crossing():
    # Corners straddling +-180: naive min/max would give (-179, .., 178), a ~357 deg span, instead
    # of the true ~15 deg span on the far side of the seam.
    footprint = {"a": (178.0, 65.0), "b": (-179.0, 66.0), "c": (170.0, 65.5), "d": (-175.0, 66.5)}
    minx, miny, maxx, maxy = geo_utils.footprint_bbox_deg(footprint)
    assert maxx - minx == pytest.approx(15.0)
    assert miny == pytest.approx(65.0)
    assert maxy == pytest.approx(66.5)


def test_footprint_bbox_deg_skips_none_entries():
    footprint = {"a": (170.0, 40.0), "b": None, "c": (172.0, 41.0)}
    bbox = geo_utils.footprint_bbox_deg(footprint)
    assert bbox == pytest.approx((170.0, 40.0, 172.0, 41.0))


def test_orthographic_xy_m_center_is_origin():
    x, y = geo_utils.orthographic_xy_m(30.0, -12.0, center_lon_deg=30.0, center_lat_deg=-12.0)
    assert (x, y) == pytest.approx((0.0, 0.0), abs=1e-6)


def test_orthographic_xy_m_small_offsets_match_arc_length():
    # Near the tangent point, the projection is ~locally flat -- a small angular offset along one
    # axis should map to ~radius * offset_rad along the matching axis, ~0 on the other.
    radius_m = 1_737_400.0
    x_lon, y_lon = geo_utils.orthographic_xy_m(1.0, 0.0, center_lon_deg=0.0, center_lat_deg=0.0, radius_m=radius_m)
    assert x_lon == pytest.approx(radius_m * math.radians(1.0), rel=1e-4)
    assert y_lon == pytest.approx(0.0, abs=1.0)

    x_lat, y_lat = geo_utils.orthographic_xy_m(0.0, 1.0, center_lon_deg=0.0, center_lat_deg=0.0, radius_m=radius_m)
    assert y_lat == pytest.approx(radius_m * math.radians(1.0), rel=1e-4)
    assert x_lat == pytest.approx(0.0, abs=1.0)


def test_footprint_bbox_local_m_symmetric_footprint():
    radius_m = 1_737_400.0
    center_lon, center_lat = 10.0, 5.0
    footprint = {
        "center": (center_lon, center_lat),
        "top_left": (center_lon - 0.1, center_lat + 0.1),
        "top_right": (center_lon + 0.1, center_lat + 0.1),
        "bottom_left": (center_lon - 0.1, center_lat - 0.1),
        "bottom_right": (center_lon + 0.1, center_lat - 0.1),
    }
    minx, miny, maxx, maxy = geo_utils.footprint_bbox_local_m(footprint, center_lon, center_lat, radius_m)
    # Roughly symmetric around the origin (center) for a footprint symmetric in lon/lat.
    assert minx == pytest.approx(-maxx, rel=1e-3)
    assert miny == pytest.approx(-maxy, rel=1e-3)
    assert maxx > 0
    assert maxy > 0


def test_footprint_bbox_local_m_skips_none_entries():
    footprint = {"a": (10.0, 5.0), "b": None, "c": (10.2, 5.2)}
    bbox = geo_utils.footprint_bbox_local_m(footprint, center_lon_deg=10.0, center_lat_deg=5.0)
    assert len(bbox) == 4


def test_pixel_dims_for_gsd_isotropic_for_square_bbox():
    # Unlike the old lon/lat-degree version, a square meter bbox should give equal width/height --
    # no cos(lat) correction needed since the local Orthographic CRS is already isotropic.
    bbox = (-10_000.0, -10_000.0, 10_000.0, 10_000.0)
    width_px, height_px = geo_utils.pixel_dims_for_gsd(bbox, target_gsd_m=100.0)
    assert width_px == height_px == 200
