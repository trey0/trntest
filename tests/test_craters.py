import math
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np
import pyproj
import pytest
import rasterio
import rasterio.transform

from trntest import craters
from trntest.config import TrntestConfig

_MOON_RADIUS_M = 1737400.0


def _config(cache_root):
    return TrntestConfig(cache_root=cache_root, robbins_craters_url="https://example.com/robbins.zip")


def _write_robbins_zip(zip_path, rows):
    """A minimal stand-in for the real Robbins bundle zip -- just enough structure
    (`_csv_member_name`'s own selection criteria) plus real header columns for `_convert_to_geopackage`."""
    header = "CRATER_ID,LAT_ELLI_IMG,LON_ELLI_IMG,DIAM_ELLI_MAJOR_IMG,DIAM_ELLI_MINOR_IMG,DIAM_ELLI_ANGLE_IMG"
    lines = [header] + [",".join(str(v) for v in row) for row in rows]
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("bundle/data/lunar_crater_database_robbins_2018.csv", "\n".join(lines))
        zf.writestr("bundle/data/collection_data_inventory.csv", "not crater data")


def test_csv_member_name_selects_the_one_real_data_csv(tmp_path):
    zip_path = tmp_path / "robbins.zip"
    _write_robbins_zip(zip_path, rows=[("00-1-000000", -19.8, 264.7, 940.0, 900.0, 36.0)])
    with zipfile.ZipFile(zip_path) as zf:
        assert craters._csv_member_name(zf) == "bundle/data/lunar_crater_database_robbins_2018.csv"


def test_csv_member_name_raises_when_not_exactly_one_match(tmp_path):
    zip_path = tmp_path / "empty.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("bundle/data/collection_data_inventory.csv", "not crater data")
    with zipfile.ZipFile(zip_path) as zf, pytest.raises(ValueError, match="expected exactly one"):
        craters._csv_member_name(zf)


def test_ellipse_polygon_area_and_center():
    polygon = craters._ellipse_polygon(center_x_m=1000.0, center_y_m=-500.0, major_km=2.0, minor_km=1.0, angle_deg=0.0)
    semi_major_m, semi_minor_m = 1000.0, 500.0
    assert polygon.area == pytest.approx(math.pi * semi_major_m * semi_minor_m, rel=0.01)
    assert polygon.centroid.x == pytest.approx(1000.0, abs=1.0)
    assert polygon.centroid.y == pytest.approx(-500.0, abs=1.0)
    # angle_deg=0 -> major axis along x: bounding box should be wider (x) than tall (y).
    minx, miny, maxx, maxy = polygon.bounds
    assert (maxx - minx) > (maxy - miny)


def test_convert_to_geopackage_then_query_bbox_round_trip(tmp_path):
    zip_path = tmp_path / "robbins.zip"
    _write_robbins_zip(
        zip_path,
        rows=[
            ("00-1-000000", -19.8, 264.7, 940.0, 900.0, 36.0),  # inside the query box
            ("00-1-000001", 44.7, 328.6, 250.0, 246.0, 127.0),  # outside the query box
            ("00-1-000002", -20.0, 265.0, 10.0, 9.0, 90.0),  # inside, near the first
        ],
    )
    gpkg_path = craters.geopackage_path(zip_path)
    craters._convert_to_geopackage(zip_path, gpkg_path, moon_radius_m=_MOON_RADIUS_M)
    assert gpkg_path.exists()

    with mock.patch.object(craters, "ensure_geopackage", return_value=gpkg_path):
        gdf = craters.query_craters_in_bbox((260.0, -25.0, 270.0, -15.0), config=_config(tmp_path))

    assert set(gdf["CRATER_ID"]) == {"00-1-000000", "00-1-000002"}


def test_crater_overlay_layer_builds_ellipses_in_raster_crs(tmp_path):
    zip_path = tmp_path / "robbins.zip"
    center_lon, center_lat = 264.757, -19.8304
    _write_robbins_zip(
        zip_path,
        rows=[
            ("00-1-000000", center_lat, center_lon, 2.0, 1.0, 36.0),
            ("00-1-000002", center_lat + 0.05, center_lon + 0.05, 1.0, 0.8, 90.0),
            ("00-1-000001", 44.7, 328.6, 250.0, 246.0, 127.0),  # far away, must not appear
        ],
    )
    gpkg_path = craters.geopackage_path(zip_path)
    craters._convert_to_geopackage(zip_path, gpkg_path, moon_radius_m=_MOON_RADIUS_M)

    crs = f"+proj=ortho +lon_0={center_lon} +lat_0={center_lat} +R={_MOON_RADIUS_M} +units=m +no_defs"
    transform = rasterio.transform.from_origin(-50_000, 50_000, 100, 100)
    raster_path = tmp_path / "raster.tif"
    with rasterio.open(
        raster_path, "w", driver="GTiff", height=1000, width=1000, count=1, dtype="uint8", crs=crs, transform=transform
    ) as dst:
        dst.write(np.zeros((1000, 1000), dtype="uint8"), 1)

    with mock.patch.object(craters, "ensure_geopackage", return_value=gpkg_path):
        layer = craters.crater_overlay_layer(raster_path, config=_config(tmp_path), color="orange")

    assert layer is not None
    assert len(layer.geoseries) == 2
    assert layer.color == "orange"
    with rasterio.open(raster_path) as src:
        assert layer.geoseries.crs == pyproj.CRS.from_wkt(src.crs.to_wkt())


def test_crater_overlay_layer_returns_none_when_nothing_in_view(tmp_path):
    zip_path = tmp_path / "robbins.zip"
    _write_robbins_zip(zip_path, rows=[("00-1-000001", 44.7, 328.6, 250.0, 246.0, 127.0)])
    gpkg_path = craters.geopackage_path(zip_path)
    craters._convert_to_geopackage(zip_path, gpkg_path, moon_radius_m=_MOON_RADIUS_M)

    # A raster far from the one fixture crater above.
    crs = f"+proj=ortho +lon_0=10 +lat_0=10 +R={_MOON_RADIUS_M} +units=m +no_defs"
    transform = rasterio.transform.from_origin(-10_000, 10_000, 100, 100)
    raster_path = tmp_path / "raster.tif"
    with rasterio.open(
        raster_path, "w", driver="GTiff", height=200, width=200, count=1, dtype="uint8", crs=crs, transform=transform
    ) as dst:
        dst.write(np.zeros((200, 200), dtype="uint8"), 1)

    with mock.patch.object(craters, "ensure_geopackage", return_value=gpkg_path):
        layer = craters.crater_overlay_layer(raster_path, config=_config(tmp_path))

    assert layer is None


def test_geopackage_path_matches_zip_basename():
    assert craters.geopackage_path(Path("/x/robbins.zip")) == Path("/x/robbins.gpkg")
