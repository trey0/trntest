import math

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import transform as warp_transform

from trntest import lunaserv
from trntest.config import DEFAULT_MOON_RADIUS_M


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


def test_camera_local_enu_m_directly_overhead_tangent_point():
    radius_m = 1_737_400.0
    altitude_m = 100_000.0
    lon0_deg, lat0_deg = 30.0, 0.0
    lon0, lat0 = math.radians(lon0_deg), math.radians(lat0_deg)
    camera_moon_me_m = [
        (radius_m + altitude_m) * math.cos(lat0) * math.cos(lon0),
        (radius_m + altitude_m) * math.cos(lat0) * math.sin(lon0),
        (radius_m + altitude_m) * math.sin(lat0),
    ]
    east, north, up = lunaserv._camera_local_enu_m(camera_moon_me_m, lon0_deg, lat0_deg, radius_m)
    assert (east, north) == pytest.approx((0.0, 0.0), abs=1e-6)
    assert up == pytest.approx(altitude_m, rel=1e-9)


def test_camera_local_enu_m_matches_orthographic_xy_for_on_sphere_point():
    # A point *on* the sphere (zero altitude) at a nearby (lon, lat) should land at the same
    # (east, north) `orthographic_xy_m` already computes for it (both are the same tangent-plane
    # projection, just derived two different ways -- see `_camera_local_enu_m`'s docstring), with
    # up ~0 (small, curvature-only 2nd-order error for a nearby point).
    radius_m = 1_737_400.0
    center_lon_deg, center_lat_deg = 10.0, 5.0
    point_lon_deg, point_lat_deg = 10.2, 5.1
    point_moon_me_m = [
        radius_m * math.cos(math.radians(point_lat_deg)) * math.cos(math.radians(point_lon_deg)),
        radius_m * math.cos(math.radians(point_lat_deg)) * math.sin(math.radians(point_lon_deg)),
        radius_m * math.sin(math.radians(point_lat_deg)),
    ]
    east, north, up = lunaserv._camera_local_enu_m(point_moon_me_m, center_lon_deg, center_lat_deg, radius_m)
    expected_x, expected_y = lunaserv.orthographic_xy_m(
        point_lon_deg, point_lat_deg, center_lon_deg, center_lat_deg, radius_m
    )
    assert (east, north) == pytest.approx((expected_x, expected_y), rel=1e-4)
    # Curvature (sagitta) term, ~radius * angle_rad**2 / 2 for a ~0.22 deg separation -- ~13m here,
    # not exactly 0 (the point is *on* the sphere, but the tangent *plane* dips below it away from
    # the tangent point itself).
    assert up == pytest.approx(0.0, abs=20.0)


def test_terrain_photometric_angles_flat_dem_directly_below_camera():
    # Flat terrain -> constant surface normal -> incidence should be exactly (90 - elevation_deg)
    # everywhere, regardless of position. At the one pixel directly below the camera (odd grid size
    # so a pixel center lands exactly at x=y=0), the view direction is exactly straight up too, so
    # emission there should be ~0 and phase should coincide with incidence.
    width = height = 11
    bbox = (-100.0, -100.0, 100.0, 100.0)
    dem = np.zeros((height, width))
    altitude_m = 1_000.0
    camera_local_enu_m = np.array([0.0, 0.0, altitude_m])
    azimuth_deg, elevation_deg = 90.0, 30.0

    incidence_deg, emission_deg, phase_deg = lunaserv._terrain_photometric_angles(
        dem, bbox, camera_local_enu_m, azimuth_deg, elevation_deg, cellsize_m=200.0 / width
    )
    assert incidence_deg == pytest.approx(90.0 - elevation_deg, abs=1e-6)

    center = height // 2, width // 2
    assert emission_deg[center] == pytest.approx(0.0, abs=1e-6)
    assert phase_deg[center] == pytest.approx(90.0 - elevation_deg, abs=1e-6)


def test_terrain_photometric_angles_emission_grows_with_offset_from_nadir():
    # Off-center pixel, still flat terrain: emission from a finite-altitude camera directly
    # overhead the grid's own center should match the exact flat-ground formula
    # atan(horizontal_distance / altitude) -- confirms real parallax (not just a nadir
    # approximation) drives emission here.
    width = height = 11
    minx, miny, maxx, maxy = bbox = (-100.0, -100.0, 100.0, 100.0)
    dem = np.zeros((height, width))
    altitude_m = 1_000.0
    camera_local_enu_m = np.array([0.0, 0.0, altitude_m])

    _, emission_deg, _ = lunaserv._terrain_photometric_angles(
        dem, bbox, camera_local_enu_m, azimuth_deg=0.0, elevation_deg=45.0, cellsize_m=200.0 / width
    )

    row = height // 2
    col = width - 1
    x = minx + (col + 0.5) * (maxx - minx) / width
    expected_emission_deg = math.degrees(math.atan(abs(x) / altitude_m))
    assert emission_deg[row, col] == pytest.approx(expected_emission_deg, rel=1e-6)


def test_local_enu_direction_pure_radial_vector_is_pure_up():
    # A vector pointing exactly along the tangent point's own radial direction (no East/North
    # component at all) should land entirely on the "Up" axis in the local frame, regardless of its
    # magnitude -- a direct check of `_local_enu_direction`'s basis vectors alone (no tangent-point
    # subtraction involved, unlike `_camera_local_enu_m`).
    lon0_deg, lat0_deg = 12.0, -34.0
    lon0, lat0 = math.radians(lon0_deg), math.radians(lat0_deg)
    magnitude = 1.6  # km/s-scale, but this function is unit-agnostic
    radial = magnitude * np.array([math.cos(lat0) * math.cos(lon0), math.cos(lat0) * math.sin(lon0), math.sin(lat0)])
    east, north, up = lunaserv._local_enu_direction(radial, lon0_deg, lat0_deg)
    assert (east, north) == pytest.approx((0.0, 0.0), abs=1e-9)
    assert up == pytest.approx(magnitude, rel=1e-9)


def test_terrain_photometric_angles_along_track_correction_removes_along_track_component():
    # Flat terrain again (normal always [0,0,1]): a camera offset with both an along-track and
    # cross-track component should, once corrected, behave exactly as if only the cross-track
    # component existed -- the along-track correction's whole point.
    width = height = 11
    bbox = (-100.0, -100.0, 100.0, 100.0)
    dem = np.zeros((height, width))
    # Camera 100m east, 200m north, 1000m up from the pixel directly below (grid center).
    camera_local_enu_m = np.array([100.0, 200.0, 1000.0])
    along_track_local_enu = np.array([0.0, 1.0, 0.0])  # due north -- an arbitrary nonzero magnitude is fine too

    _, emission_deg, _ = lunaserv._terrain_photometric_angles(
        dem,
        bbox,
        camera_local_enu_m,
        azimuth_deg=0.0,
        elevation_deg=45.0,
        cellsize_m=200.0 / width,
        along_track_local_enu=along_track_local_enu,
    )

    center = height // 2, width // 2
    # North offset is exactly along the along-track direction, so it's fully removed -- what's left is exactly as if
    # the camera had been at (100, 0, 1000) instead (pure cross-track + altitude).
    expected_emission_deg = math.degrees(math.atan(100.0 / 1000.0))
    assert emission_deg[center] == pytest.approx(expected_emission_deg, rel=1e-6)


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
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    bbox = lunaserv.astropedia_coverage_bbox_deg(dst_bbox_m, 10.0, 5.0, DEFAULT_MOON_RADIUS_M)
    assert len(bbox) == 4
    minlon, minlat, maxlon, maxlat = bbox
    assert minlon < 10.0 < maxlon
    assert minlat < 5.0 < maxlat


def test_astropedia_coverage_bbox_deg_raises_beyond_max_latitude():
    dst_bbox_m = (-50000.0, -50000.0, 50000.0, 50000.0)
    with pytest.raises(ValueError, match="beyond Astropedia"):
        lunaserv.astropedia_coverage_bbox_deg(dst_bbox_m, 10.0, 85.0, DEFAULT_MOON_RADIUS_M)


def test_astropedia_coverage_bbox_deg_covers_dst_bbox_corners():
    """Regression test for the real corner-nodata bug this function's rewrite fixed (see
    docs/history.md's dated entry): the returned degree bbox, transformed back through the same
    local-Orthographic projection, must fully cover `dst_bbox_m`'s own corners, not just its
    center -- independently padding a degree-space bbox around the raw footprint (the old approach)
    used to undershoot them."""
    center_lon, center_lat = 10.0, 5.0
    dst_bbox_m = (-80000.0, -60000.0, 90000.0, 70000.0)  # deliberately asymmetric, not a plain square
    minlon, minlat, maxlon, maxlat = lunaserv.astropedia_coverage_bbox_deg(
        dst_bbox_m, center_lon, center_lat, DEFAULT_MOON_RADIUS_M
    )

    ortho_crs = f"+proj=ortho +lon_0={center_lon} +lat_0={center_lat} +R={DEFAULT_MOON_RADIUS_M} +units=m +no_defs"
    geo_crs = f"+proj=longlat +R={DEFAULT_MOON_RADIUS_M} +no_defs"
    minx, miny, maxx, maxy = dst_bbox_m
    lons, lats = warp_transform(ortho_crs, geo_crs, [minx, maxx, minx, maxx], [miny, miny, maxy, maxy])
    for lon, lat in zip(lons, lats, strict=True):
        assert minlon <= lon <= maxlon
        assert minlat <= lat <= maxlat


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
