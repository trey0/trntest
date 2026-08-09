import math

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds as transform_from_bounds

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


def test_orthographic_xy_m_center_is_origin():
    x, y = lunaserv.orthographic_xy_m(30.0, -12.0, center_lon_deg=30.0, center_lat_deg=-12.0)
    assert (x, y) == pytest.approx((0.0, 0.0), abs=1e-6)


def test_orthographic_xy_m_small_offsets_match_arc_length():
    # Near the tangent point, the projection is ~locally flat -- a small angular offset along one
    # axis should map to ~radius * offset_rad along the matching axis, ~0 on the other.
    radius_m = 1_737_400.0
    x_lon, y_lon = lunaserv.orthographic_xy_m(1.0, 0.0, center_lon_deg=0.0, center_lat_deg=0.0, radius_m=radius_m)
    assert x_lon == pytest.approx(radius_m * math.radians(1.0), rel=1e-4)
    assert y_lon == pytest.approx(0.0, abs=1.0)

    x_lat, y_lat = lunaserv.orthographic_xy_m(0.0, 1.0, center_lon_deg=0.0, center_lat_deg=0.0, radius_m=radius_m)
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
    minx, miny, maxx, maxy = lunaserv.footprint_bbox_local_m(footprint, center_lon, center_lat, radius_m)
    # Roughly symmetric around the origin (center) for a footprint symmetric in lon/lat.
    assert minx == pytest.approx(-maxx, rel=1e-3)
    assert miny == pytest.approx(-maxy, rel=1e-3)
    assert maxx > 0
    assert maxy > 0


def test_footprint_bbox_local_m_skips_none_entries():
    footprint = {"a": (10.0, 5.0), "b": None, "c": (10.2, 5.2)}
    bbox = lunaserv.footprint_bbox_local_m(footprint, center_lon_deg=10.0, center_lat_deg=5.0)
    assert len(bbox) == 4


def test_pixel_dims_for_gsd_isotropic_for_square_bbox():
    # Unlike the old lon/lat-degree version, a square meter bbox should give equal width/height --
    # no cos(lat) correction needed since the local Orthographic CRS is already isotropic.
    bbox = (-10_000.0, -10_000.0, 10_000.0, 10_000.0)
    width_px, height_px = lunaserv.pixel_dims_for_gsd(bbox, target_gsd_m=100.0)
    assert width_px == height_px == 200


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

    result_path = lunaserv.reproject_dem_to_local_grid(
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

    result_path = lunaserv.reproject_dem_to_local_grid(
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


def test_astropedia_coverage_bbox_deg_within_range():
    footprint = {"center": (10.0, 5.0), "corner": (10.2, 5.2)}
    bbox = lunaserv.astropedia_coverage_bbox_deg(footprint, dem_padding_fraction=0.3)
    assert len(bbox) == 4


def test_astropedia_coverage_bbox_deg_raises_beyond_max_latitude():
    footprint = {"center": (10.0, 85.0), "corner": (10.2, 85.2)}
    with pytest.raises(ValueError, match="beyond Astropedia"):
        lunaserv.astropedia_coverage_bbox_deg(footprint, dem_padding_fraction=0.3)


def test_astropedia_coverage_bbox_deg_considers_extra_footprint():
    footprint = {"center": (10.0, 5.0)}
    extra_footprint = {"far": (10.0, 85.0)}
    with pytest.raises(ValueError, match="beyond Astropedia"):
        lunaserv.astropedia_coverage_bbox_deg(
            footprint, dem_padding_fraction=0.0, extra_footprint_lonlat_deg=extra_footprint
        )


def _write_astropedia_style_tif(path, elevation_value, bbox_m, width, height, moon_radius_m):
    """Synthetic fixture matching Astropedia's real file: an Equidistant Cylindrical ("Equirectangular")
    projected CRS (lon_0=180, standard parallel 0 -- same as the real
    `Lunar_LRO_WAC_GLD100_DTM_79S79N_100m_v1.1.tif`), already-elevation values (not planetocentric
    radius), with real embedded georeferencing -- `reproject_astropedia_elevation_to_local_grid`
    trusts the file's own `crs`/`transform` directly, so the fixture needs a genuine one, unlike
    Lunaserv's GetMap responses which this project never trusted for that."""
    crs = f"+proj=eqc +lat_ts=0 +lon_0=180 +R={moon_radius_m} +units=m +no_defs"
    transform = transform_from_bounds(*bbox_m, width, height)
    data = np.full((height, width), elevation_value, dtype="int16")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="int16",
        crs=crs,
        transform=transform,
        nodata=-32768,
    ) as dst:
        dst.write(data, 1)


def _eqc_deg_bbox_for_meters_bbox(bbox_m, moon_radius_m):
    """Invert the same `+proj=eqc +lat_ts=0 +lon_0=180` forward formula (lon_0=180, standard
    parallel 0 makes it a simple linear relationship: x = R*radians(lon-180), y = R*radians(lat)) to
    get the lon/lat bbox that maps onto a chosen meters bbox in that CRS -- avoids needing `pyproj`
    directly in the test, and keeps the fixture's AOI request exactly aligned with its own data."""
    minx, miny, maxx, maxy = bbox_m
    minlon = 180.0 + math.degrees(minx / moon_radius_m)
    maxlon = 180.0 + math.degrees(maxx / moon_radius_m)
    minlat = math.degrees(miny / moon_radius_m)
    maxlat = math.degrees(maxy / moon_radius_m)
    return minlon, minlat, maxlon, maxlat


def test_reproject_astropedia_elevation_to_local_grid_preserves_constant_field(tmp_path):
    # Mirrors test_reproject_dem_to_local_grid_preserves_constant_field, but for the Astropedia-style
    # source (Equirectangular meters CRS, real elevation already -- not radius) -- confirms the
    # windowed-read + reproject path works correctly and doesn't need `radius_to_elevation`.
    moon_radius_m = 1_737_400.0
    elevation_value = 500.0
    native_bbox_m = (-350_000.0, 550_000.0, -250_000.0, 650_000.0)  # ~100km x 100km
    native_width, native_height = 64, 64
    native_path = tmp_path / "astropedia_native.tif"
    _write_astropedia_style_tif(native_path, elevation_value, native_bbox_m, native_width, native_height, moon_radius_m)

    # AOI well within the native file's coverage (a smaller, centered sub-region).
    minx, miny, maxx, maxy = native_bbox_m
    aoi_bbox_m = (
        minx + (maxx - minx) * 0.25,
        miny + (maxy - miny) * 0.25,
        maxx - (maxx - minx) * 0.25,
        maxy - (maxy - miny) * 0.25,
    )
    deg_bbox = _eqc_deg_bbox_for_meters_bbox(aoi_bbox_m, moon_radius_m)
    center_lon = 180.0 + math.degrees(((minx + maxx) / 2) / moon_radius_m)
    center_lat = math.degrees(((miny + maxy) / 2) / moon_radius_m)

    dst_bbox_m = (-5_000.0, -5_000.0, 5_000.0, 5_000.0)
    dst_width, dst_height = 32, 32
    output_path = tmp_path / "reprojected.tif"

    result_path = lunaserv.reproject_astropedia_elevation_to_local_grid(
        native_path,
        deg_bbox,
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
    assert not np.isnan(result).any()
    # Elevation preserved directly -- no planetocentric-radius offset subtracted, unlike the
    # deprecated Lunaserv path.
    assert result == pytest.approx(elevation_value, abs=1.0)


def test_despeckle_replaces_isolated_spike():
    data = np.full((20, 20), 100, dtype=np.uint8)
    data[10, 10] = 250
    cleaned = lunaserv.despeckle(data)
    assert cleaned[10, 10] == 100


def test_despeckle_leaves_smooth_constant_region_untouched():
    data = np.full((20, 20), 100, dtype=np.uint8)
    data[10, 10] = 250
    cleaned = lunaserv.despeckle(data)
    # everywhere but the spike's own 3x3 neighborhood is unaffected
    mask = np.ones_like(data, dtype=bool)
    mask[9:12, 9:12] = False
    assert np.array_equal(cleaned[mask], data[mask])


def test_despeckle_leaves_smooth_gradient_untouched():
    # a real gradient has no isolated single-pixel deviations -- shouldn't false-positive anywhere
    data = np.linspace(0, 255, 20 * 20, dtype=np.uint8).reshape(20, 20)
    cleaned = lunaserv.despeckle(data)
    assert np.array_equal(cleaned, data)


def test_despeckle_leaves_large_blob_interior_untouched():
    # simulates a real saturated-crater feature: a large uniform region, not an isolated pixel
    data = np.full((20, 20), 50, dtype=np.uint8)
    data[5:15, 5:15] = 255
    cleaned = lunaserv.despeckle(data)
    interior = cleaned[8:12, 8:12]
    assert np.all(interior == 255)
