import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds as transform_from_bounds

from trntest import lunaserv_wms


def _write_native_radius_tif(path, radius_value, native_bbox_deg, width, height):
    minlon, minlat, maxlon, maxlat = native_bbox_deg
    transform = transform_from_bounds(minlon, minlat, maxlon, maxlat, width, height)
    data = np.full((height, width), radius_value, dtype="float32")
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=1, dtype="float32", transform=transform
    ) as dst:
        dst.write(data, 1)


def test_reproject_dem_to_local_grid_shape_matches_destination(tmp_path):
    moon_radius_m = 1_737_400.0
    center_lon, center_lat = 10.0, 5.0
    native_width, native_height = 64, 64
    native_bbox_deg = (center_lon - 1.0, center_lat - 1.0, center_lon + 1.0, center_lat + 1.0)
    native_path = tmp_path / "native.tif"
    _write_native_radius_tif(native_path, moon_radius_m, native_bbox_deg, native_width, native_height)

    dst_bbox_m = (-5_000.0, -5_000.0, 5_000.0, 5_000.0)
    dst_width, dst_height = 32, 32
    output_path = tmp_path / "reprojected.tif"

    result_path = lunaserv_wms.reproject_dem_to_local_grid(
        native_path,
        native_bbox_deg,
        native_width,
        native_height,
        dst_bbox_m,
        dst_width,
        dst_height,
        center_lon,
        center_lat,
        moon_radius_m,
        output_path,
    )

    with rasterio.open(result_path) as src:
        result = src.read(1)
    assert result.shape == (dst_height, dst_width)


def test_reproject_dem_to_local_grid_preserves_constant_field(tmp_path):
    # A uniform native "radius" input (no real terrain variation) should stay ~uniform after
    # reprojection -- the projection math itself shouldn't introduce an artificial gradient/artifact
    # for trivial input, and the destination grid (a small AOI well within the native bbox's
    # coverage) should be fully populated, no nodata gaps.
    moon_radius_m = 1_737_400.0
    center_lon, center_lat = 10.0, 5.0
    native_width, native_height = 64, 64
    native_bbox_deg = (center_lon - 1.0, center_lat - 1.0, center_lon + 1.0, center_lat + 1.0)
    native_path = tmp_path / "native.tif"
    _write_native_radius_tif(native_path, moon_radius_m, native_bbox_deg, native_width, native_height)

    dst_bbox_m = (-5_000.0, -5_000.0, 5_000.0, 5_000.0)
    dst_width, dst_height = 32, 32
    output_path = tmp_path / "reprojected.tif"

    result_path = lunaserv_wms.reproject_dem_to_local_grid(
        native_path,
        native_bbox_deg,
        native_width,
        native_height,
        dst_bbox_m,
        dst_width,
        dst_height,
        center_lon,
        center_lat,
        moon_radius_m,
        output_path,
    )

    with rasterio.open(result_path) as src:
        result = src.read(1)
    assert not np.isnan(result).any()
    assert result == pytest.approx(moon_radius_m, rel=1e-4)
