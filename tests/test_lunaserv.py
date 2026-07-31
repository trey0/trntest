import pytest

from trntest import lunaserv


def test_footprint_bbox_deg_no_wraparound():
    footprint = {"a": (170.0, 40.0), "b": (175.0, 42.0), "c": (172.0, 41.0), "d": (178.0, 39.0)}
    bbox = lunaserv.footprint_bbox_deg(footprint)
    assert bbox == pytest.approx((170.0, 39.0, 178.0, 42.0))


def test_footprint_bbox_deg_antimeridian_crossing():
    # Corners straddling +-180: naive min/max would give (-179, .., 178), a ~357 deg span, instead
    # of the true ~15 deg span on the far side of the seam.
    footprint = {"a": (178.0, 65.0), "b": (-179.0, 66.0), "c": (170.0, 65.5), "d": (-175.0, 66.5)}
    minx, miny, maxx, maxy = lunaserv.footprint_bbox_deg(footprint)
    assert maxx - minx == pytest.approx(15.0)
    assert miny == pytest.approx(65.0)
    assert maxy == pytest.approx(66.5)


def test_footprint_bbox_deg_skips_none_entries():
    footprint = {"a": (170.0, 40.0), "b": None, "c": (172.0, 41.0)}
    bbox = lunaserv.footprint_bbox_deg(footprint)
    assert bbox == pytest.approx((170.0, 40.0, 172.0, 41.0))
