import math

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trntest import dem_sources
from trntest.config import MOON_RADIUS_M, TrntestConfig
from trntest.dem_sources import DemSource, DemTile, LocalGrid

_M_PER_DEG = math.pi * MOON_RADIUS_M / 180.0


def _grid(center_lon, center_lat, half_m=50_000.0, gsd_m=1_000.0) -> LocalGrid:
    size = round(2 * half_m / gsd_m)
    return LocalGrid((-half_m, -half_m, half_m, half_m), size, size, center_lon, center_lat)


def _write_eqc_tile(path, lon_range, lat_range, pixel_deg, values_fn, nodata=-3.4028226550889045e38):
    # Like the real files: Equidistant Cylindrical, lon_0=180, meters, float32.
    (west, east), (south, north) = lon_range, lat_range
    width, height = round((east - west) / pixel_deg), round((north - south) / pixel_deg)
    lon = west + (np.arange(width) + 0.5) * pixel_deg
    lat = north - (np.arange(height) + 0.5) * pixel_deg
    values = values_fn(*np.meshgrid(lon, lat)).astype("float32")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="float32",
        crs=f"+proj=eqc +lat_ts=0 +lon_0=180 +R={MOON_RADIUS_M} +units=m +no_defs",
        transform=from_origin(
            (west - 180.0) * _M_PER_DEG, north * _M_PER_DEG, pixel_deg * _M_PER_DEG, pixel_deg * _M_PER_DEG
        ),
        nodata=nodata,
    ) as dst:
        dst.write(values, 1)


def _source(tmp_path, name, tiles, values_fn, pixel_deg=0.01, to_meters=1.0, covers=True):
    for tile in tiles:
        _write_eqc_tile(tmp_path / f"{tile.tile_id}.tif", tile.lon_range_deg, tile.lat_range_deg, pixel_deg, values_fn)

    def check(grid):
        if not covers:
            raise ValueError("doesn't cover")

    return DemSource(
        name=name,
        tiles=tuple(tiles),
        fetch=lambda tile_id, config: tmp_path / f"{tile_id}.tif",
        to_meters=to_meters,
        pixel_m=pixel_deg * _M_PER_DEG,
        check_coverage=check,
    )


def test_sldem2015_tiles_parse_their_extents():
    by_id = {tile.tile_id: tile for tile in dem_sources.SLDEM2015.tiles}
    assert len(by_id) == 32
    tile = by_id["SLDEM2015_512_30S_00S_090_135_FLOAT"]
    assert tile.lon_range_deg == (90.0, 135.0)
    assert tile.lat_range_deg == (-30.0, 0.0)
    assert by_id["SLDEM2015_512_30N_60N_315_360_FLOAT"].lat_range_deg == (30.0, 60.0)


@pytest.mark.parametrize(
    ("center", "expected"),
    [
        # Four tiles meet at (0, 0), across the 0/360 wrap.
        (
            (0.0, 0.0),
            {
                "SLDEM2015_512_00N_30N_000_045_FLOAT",
                "SLDEM2015_512_00N_30N_315_360_FLOAT",
                "SLDEM2015_512_30S_00S_000_045_FLOAT",
                "SLDEM2015_512_30S_00S_315_360_FLOAT",
            },
        ),
        ((20.0, 15.0), {"SLDEM2015_512_00N_30N_000_045_FLOAT"}),
        # Straddles 60N: only the part below it has a tile.
        ((10.0, 60.0), {"SLDEM2015_512_30N_60N_000_045_FLOAT"}),
        ((10.0, 70.0), set()),
        # A negative center longitude is the same place as its 0-360 equivalent.
        ((-100.0, -45.0), {"SLDEM2015_512_60S_30S_225_270_FLOAT"}),
    ],
)
def test_tiles_for_grid_picks_overlapping_sldem2015_tiles(center, expected):
    tiles = dem_sources.tiles_for_grid(dem_sources.SLDEM2015, _grid(*center))
    assert {tile.tile_id for tile in tiles} == expected


def test_source_elevation_joins_tiles_across_the_wrap_and_scales_to_meters(tmp_path):
    # Two tiles meeting at 0/360, values in km of a smooth function of longitude: a missing or
    # misplaced tile shows as NaN or a non-monotonic row.
    tiles = [DemTile("east", (0.0, 10.0), (-10.0, 10.0)), DemTile("west", (350.0, 360.0), (-10.0, 10.0))]
    source = _source(tmp_path, "km", tiles, lambda lon, lat: np.sin(np.radians(lon)), to_meters=1000.0)
    elevation = dem_sources.source_elevation(source, _grid(0.0, 0.0), TrntestConfig())
    assert not np.isnan(elevation).any()
    row = elevation[elevation.shape[0] // 2]
    assert np.all(np.diff(row) > 0)
    # Columns span about +-1.65 deg of longitude; sin of that, in meters.
    assert row[0] == pytest.approx(-1000 * math.sin(math.radians(1.65)), rel=0.05)
    assert row[-1] == pytest.approx(1000 * math.sin(math.radians(1.65)), rel=0.05)


def test_source_elevation_averages_a_finer_source(tmp_path):
    # A checkerboard at 0.01 deg (~300 m) onto a 1 km grid: averaging lands near its mean,
    # bilinear would pick up individual cells.
    tiles = [DemTile("checker", (0.0, 10.0), (-10.0, 10.0))]
    checker = lambda lon, lat: ((np.floor(lon / 0.01) + np.floor(lat / 0.01)) % 2) * 100.0  # noqa: E731
    source = _source(tmp_path, "fine", tiles, checker)
    elevation = dem_sources.source_elevation(source, _grid(5.0, 0.0), TrntestConfig())
    assert np.nanstd(elevation) < 15.0
    assert np.nanmean(elevation) == pytest.approx(50.0, abs=5.0)


def test_mosaic_elevation_takes_each_pixel_from_the_first_source_with_data(tmp_path):
    first = _source(
        tmp_path, "first", [DemTile("first", (0.0, 10.0), (-10.0, 0.0))], lambda lon, lat: lon * 0 + 1.0, covers=False
    )
    second = _source(
        tmp_path, "second", [DemTile("second", (0.0, 10.0), (-10.0, 10.0))], lambda lon, lat: lon * 0 + 2.0
    )
    elevation = dem_sources.mosaic_elevation((first, second), _grid(5.0, 0.0), TrntestConfig())
    assert not np.isnan(elevation).any()
    north, south = elevation[: elevation.shape[0] // 2 - 2], elevation[elevation.shape[0] // 2 + 2 :]
    assert np.all(north == 2.0)
    assert np.all(south == 1.0)


def test_mosaic_elevation_raises_when_the_last_source_cannot_cover(tmp_path):
    only = _source(tmp_path, "only", [DemTile("only", (0.0, 10.0), (-10.0, 10.0))], lambda lon, lat: lon, covers=False)
    with pytest.raises(ValueError, match="doesn't cover"):
        dem_sources.mosaic_elevation((only,), _grid(5.0, 0.0), TrntestConfig())


def test_dem_source_suffix():
    assert dem_sources.dem_source_suffix("gld100") == ""
    assert dem_sources.dem_source_suffix("sldem2015_gld100") == "_dem-sldem2015_gld100"
    with pytest.raises(ValueError):
        dem_sources.dem_source_suffix("nope")


def test_every_dem_source_ends_with_a_source_that_can_cover():
    for sources in dem_sources.DEM_SOURCES.values():
        sources[-1].check_coverage(_grid(10.0, 20.0))
