import zipfile
from unittest import mock

import numpy as np
import pandas as pd
import pytest
import rasterio
import rasterio.transform

from trntest import crater_depth, craters
from trntest.config import TrntestConfig

_MOON_RADIUS_M = 1737400.0


def _config(cache_root):
    return TrntestConfig(cache_root=cache_root, robbins_craters_url="https://example.com/robbins.zip")


def _write_robbins_zip(zip_path, rows):
    """Like `test_craters._write_robbins_zip`, plus `DIAM_CIRC_IMG` -- `crater_depths_for_footprint`'s
    own `diameter_km` column, not exercised by `test_craters.py`'s fixtures. `rows` are
    `(CRATER_ID, LAT_ELLI_IMG, LON_ELLI_IMG, DIAM_ELLI_MAJOR_IMG, DIAM_ELLI_MINOR_IMG,
    DIAM_ELLI_ANGLE_IMG, ARC_IMG, DIAM_CIRC_IMG)` tuples."""
    header = (
        "CRATER_ID,LAT_ELLI_IMG,LON_ELLI_IMG,DIAM_ELLI_MAJOR_IMG,DIAM_ELLI_MINOR_IMG,"
        "DIAM_ELLI_ANGLE_IMG,ARC_IMG,DIAM_CIRC_IMG"
    )
    lines = [header] + [",".join(str(v) for v in row) for row in rows]
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("bundle/data/lunar_crater_database_robbins_2018.csv", "\n".join(lines))
        zf.writestr("bundle/data/collection_data_inventory.csv", "not crater data")


def _write_flat_floor_rim_dem(
    path,
    crs,
    pixel_size_m=10.0,
    half_size_m=1000.0,
    floor_radius_m=400.0,
    rim_inner_m=450.0,
    rim_outer_m=550.0,
    floor_elev=0.0,
    rim_elev=50.0,
    nodata=-9999.0,
):
    """A synthetic local-meters DEM, centered on the origin, with an exact known depth: a flat disc
    at `floor_elev` out to `floor_radius_m`, a flat annulus at `rim_elev` between `rim_inner_m`/
    `rim_outer_m`, and `nodata` everywhere else -- so `crater_depth_m`'s floor/rim percentiles are
    each over a *constant* array, giving an exact expected `rim_elev - floor_elev`, not a
    percentile-estimated approximation of a continuous surface. A real crater polygon of radius `R`
    (with `floor_radius_m < R - half_diag` and `rim_inner_m <= R +- half_diag <= rim_outer_m`) then
    has its floor/rim masks land entirely inside these two flat regions."""
    n = int(2 * half_size_m / pixel_size_m)
    transform = rasterio.transform.from_origin(-half_size_m, half_size_m, pixel_size_m, pixel_size_m)
    cols, rows = np.meshgrid(np.arange(n), np.arange(n))
    x = -half_size_m + (cols + 0.5) * pixel_size_m
    y = half_size_m - (rows + 0.5) * pixel_size_m
    r = np.sqrt(x**2 + y**2)

    dem = np.full((n, n), nodata, dtype="float32")
    dem[r <= floor_radius_m] = floor_elev
    dem[(r >= rim_inner_m) & (r <= rim_outer_m)] = rim_elev

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=n,
        width=n,
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=nodata,
    ) as dst:
        dst.write(dem, 1)


_LOCAL_CRS = f"+proj=ortho +lon_0=0 +lat_0=0 +R={_MOON_RADIUS_M} +units=m +no_defs"


def test_crater_depth_m_recovers_flat_floor_rim_depth(tmp_path):
    dem_path = tmp_path / "dem.tif"
    _write_flat_floor_rim_dem(dem_path, crs=_LOCAL_CRS)
    # major_km=1.0 -> semi-axis 500m (see `_ellipse_polygon`'s km->semi-axis convention), squarely
    # between the floor (400m) and rim (450-550m) regions above.
    crater_polygon = craters._ellipse_polygon(0.0, 0.0, major_km=1.0, minor_km=1.0, angle_deg=0.0)

    depth = crater_depth.crater_depth_m(dem_path, crater_polygon)

    assert depth == pytest.approx(50.0, abs=0.01)


def test_crater_depth_m_returns_none_outside_dem_extent(tmp_path):
    dem_path = tmp_path / "dem.tif"
    _write_flat_floor_rim_dem(dem_path, crs=_LOCAL_CRS)
    far_away_polygon = craters._ellipse_polygon(1_000_000.0, 1_000_000.0, major_km=1.0, minor_km=1.0, angle_deg=0.0)

    assert crater_depth.crater_depth_m(dem_path, far_away_polygon) is None


def test_stoffler_fresh_depth_km_matches_simple_regime_below_crossover():
    # D=1km is well below the ~10.58km crossover -- the simple-crater formula should be selected
    # (and should be the smaller of the two raw formula values there).
    d = crater_depth.stoffler_fresh_depth_km(1.0)
    simple = 0.196 * 1.0**1.010
    complex_ = 1.044 * 1.0**0.301
    assert simple < complex_
    assert d == pytest.approx(simple)


def test_stoffler_fresh_depth_km_matches_complex_regime_above_crossover():
    # D=100km is well above the crossover -- the complex-crater formula should be selected (and
    # should be the smaller of the two raw formula values there).
    d = crater_depth.stoffler_fresh_depth_km(100.0)
    simple = 0.196 * 100.0**1.010
    complex_ = 1.044 * 100.0**0.301
    assert complex_ < simple
    assert d == pytest.approx(complex_)


def test_stoffler_fresh_depth_km_continuous_at_crossover():
    d_cross = crater_depth.STOFFLER_CROSSOVER_DIAMETER_KM
    assert d_cross == pytest.approx(10.58, abs=0.01)
    # Both raw formulas should agree with each other, and with `stoffler_fresh_depth_km`, exactly at
    # the crossover -- confirms `min()` introduces no discontinuity there.
    simple = 0.196 * d_cross**1.010
    complex_ = 1.044 * d_cross**0.301
    assert simple == pytest.approx(complex_, rel=1e-6)
    assert crater_depth.stoffler_fresh_depth_km(d_cross) == pytest.approx(simple, rel=1e-6)


def test_stoffler_fresh_depth_km_vectorized_over_array():
    diameters = np.array([1.0, 100.0])
    depths = crater_depth.stoffler_fresh_depth_km(diameters)
    assert depths[0] == pytest.approx(crater_depth.stoffler_fresh_depth_km(1.0))
    assert depths[1] == pytest.approx(crater_depth.stoffler_fresh_depth_km(100.0))


def test_too_close_to_astropedia_pole():
    # major_km=1 -> negligible half-extent, well clear of the 79 deg limit at 78.5 deg.
    assert not crater_depth._too_close_to_astropedia_pole(78.5, major_km=1.0)
    # major_km=120 -> ~60km half-extent (~1.98 deg), pushes 78.5 deg past 79.0 deg.
    assert crater_depth._too_close_to_astropedia_pole(78.5, major_km=120.0)


def test_crater_depths_for_footprint_returns_depth_table(tmp_path):
    zip_path = tmp_path / "robbins.zip"
    _write_robbins_zip(zip_path, rows=[("00-1-000000", 0.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0)])
    gpkg_path = craters.geopackage_path(zip_path)
    craters._convert_to_geopackage(zip_path, gpkg_path, moon_radius_m=_MOON_RADIUS_M)

    dem_path = tmp_path / "dem.tif"
    _write_flat_floor_rim_dem(dem_path, crs=_LOCAL_CRS)

    with mock.patch.object(craters, "ensure_geopackage", return_value=gpkg_path):
        table = crater_depth.crater_depths_for_footprint(dem_path, config=_config(tmp_path))

    assert len(table) == 1
    row = table.iloc[0]
    assert row["depth_m"] == pytest.approx(50.0, abs=0.01)
    assert row["diameter_km"] == pytest.approx(1.0)
    assert row["depth_diameter_ratio"] == pytest.approx(50.0 / 1000.0, rel=0.01)


def test_crater_depths_for_footprint_excludes_near_pole_craters_without_dropping_them(tmp_path):
    zip_path = tmp_path / "robbins.zip"
    _write_robbins_zip(
        zip_path,
        rows=[
            # Both centered at (lon=0, lat=78.5), matching the raster's own local-CRS origin below.
            ("00-1-000000", 78.5, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0),  # small margin, kept, depth measured
            ("00-1-000001", 78.5, 0.0, 120.0, 100.0, 0.0, 1.0, 110.0),  # large margin, excluded
        ],
    )
    gpkg_path = craters.geopackage_path(zip_path)
    craters._convert_to_geopackage(zip_path, gpkg_path, moon_radius_m=_MOON_RADIUS_M)

    # Put the whole raster's own local-CRS origin at 78.5 deg latitude, close enough to the 79 deg
    # cap that the second (120km-major) crater's own half-extent margin pushes it over, but the
    # first (1km-major) crater's negligible margin doesn't.
    crs = f"+proj=ortho +lon_0=0 +lat_0=78.5 +R={_MOON_RADIUS_M} +units=m +no_defs"
    dem_path = tmp_path / "dem.tif"
    _write_flat_floor_rim_dem(dem_path, crs=crs)

    with mock.patch.object(craters, "ensure_geopackage", return_value=gpkg_path):
        table = crater_depth.crater_depths_for_footprint(dem_path, config=_config(tmp_path))

    assert len(table) == 2
    by_id = table.set_index("CRATER_ID")
    assert by_id.loc["00-1-000000", "depth_m"] == pytest.approx(50.0, abs=0.01)
    assert pd.isna(by_id.loc["00-1-000001", "depth_m"])
    assert pd.isna(by_id.loc["00-1-000001", "depth_diameter_ratio"])
