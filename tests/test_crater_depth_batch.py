import math
import zipfile
from unittest import mock

import numpy as np
import pandas as pd
import pytest
import rasterio
import rasterio.transform

from trntest import cache, crater_depth_batch, craters, lunaserv, tasks
from trntest.config import TrntestConfig

_MOON_RADIUS_M = 1737400.0
# Meters -> degrees, flat-Earth approximation -- fine for these fixtures, which deliberately stay
# near the equator to avoid needing a real cos(lat) correction.
_DEG_PER_M = 180.0 / (math.pi * _MOON_RADIUS_M)


def _config(cache_root):
    return TrntestConfig(cache_root=cache_root, robbins_craters_url="https://example.com/robbins.zip")


def _write_robbins_zip(zip_path, rows):
    """Same shape as `test_crater_depth._write_robbins_zip` -- `rows` are `(CRATER_ID, LAT_ELLI_IMG,
    LON_ELLI_IMG, DIAM_ELLI_MAJOR_IMG, DIAM_ELLI_MINOR_IMG, DIAM_ELLI_ANGLE_IMG, ARC_IMG,
    DIAM_CIRC_IMG)` tuples."""
    header = (
        "CRATER_ID,LAT_ELLI_IMG,LON_ELLI_IMG,DIAM_ELLI_MAJOR_IMG,DIAM_ELLI_MINOR_IMG,"
        "DIAM_ELLI_ANGLE_IMG,ARC_IMG,DIAM_CIRC_IMG"
    )
    lines = [header] + [",".join(str(v) for v in row) for row in rows]
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("bundle/data/lunar_crater_database_robbins_2018.csv", "\n".join(lines))
        zf.writestr("bundle/data/collection_data_inventory.csv", "not crater data")


def _write_flat_floor_rim_astropedia_like(
    path,
    bounds_deg,
    crater_lon_deg,
    crater_lat_deg,
    pixel_size_m=100.0,
    floor_radius_m=9000.0,
    rim_inner_m=9700.0,
    rim_outer_m=10300.0,
    floor_elev=0.0,
    rim_elev=50.0,
    nodata=-32768.0,
):
    """A synthetic stand-in for the real GLD100 flat file: a plain geographic (lon/lat degrees) raster
    covering `bounds_deg`, with a flat floor disc / flat rim annulus around `(crater_lon_deg,
    crater_lat_deg)` at exact known elevations, `nodata` everywhere else -- same "exact, not
    percentile-estimated" trick `test_crater_depth._write_flat_floor_rim_dem` uses, adapted to degree
    space near the equator (where a flat-Earth meters<->degrees approximation is fine)."""
    minlon, minlat, maxlon, maxlat = bounds_deg
    deg_per_px = pixel_size_m * _DEG_PER_M
    width = int(math.ceil((maxlon - minlon) / deg_per_px))
    height = int(math.ceil((maxlat - minlat) / deg_per_px))
    transform = rasterio.transform.from_origin(minlon, maxlat, deg_per_px, deg_per_px)

    cols, rows = np.meshgrid(np.arange(width), np.arange(height))
    lon = minlon + (cols + 0.5) * deg_per_px
    lat = maxlat - (rows + 0.5) * deg_per_px
    dx_m = (lon - crater_lon_deg) * math.cos(math.radians(crater_lat_deg)) / _DEG_PER_M
    dy_m = (lat - crater_lat_deg) / _DEG_PER_M
    r = np.sqrt(dx_m**2 + dy_m**2)

    dem = np.full((height, width), nodata, dtype="float32")
    dem[r <= floor_radius_m] = floor_elev
    dem[(r >= rim_inner_m) & (r <= rim_outer_m)] = rim_elev

    crs = lunaserv.geographic_crs(_MOON_RADIUS_M)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=nodata,
    ) as dst:
        dst.write(dem, 1)


def test_iter_tile_origins_covers_grid():
    origins = list(crater_depth_batch.iter_tile_origins(tile_size_deg=30.0, max_abs_lat_deg=60.0))
    assert len(origins) == 4 * 12  # 4 latitude rows (-60,-30,0,30) x 12 longitude columns
    assert origins[0] == (0.0, -60.0)
    assert origins[-1] == pytest.approx((330.0, 30.0))


def test_tile_id_is_deterministic():
    assert crater_depth_batch.tile_id(10.0, -5.5) == crater_depth_batch.tile_id(10.0, -5.5)
    assert crater_depth_batch.tile_id(10.0, 5.5) != crater_depth_batch.tile_id(10.0, -5.5)


def test_tile_bounds_deg_nominal_and_padded():
    nominal, padded = crater_depth_batch.tile_bounds_deg(
        10.0, 20.0, tile_size_deg=2.0, padded_tile_size_deg=3.0, max_abs_lat_deg=79.0
    )
    assert nominal == pytest.approx((10.0, 20.0, 12.0, 22.0))
    assert padded == pytest.approx((9.5, 19.5, 12.5, 22.5))


def test_tile_bounds_deg_padded_clips_to_max_latitude():
    # Nominal top edge already sits at max_abs_lat_deg (77+2=79) -- padding would push to 79.5, which
    # must clip back to 79.0 (GLD100's own real coverage limit), not silently exceed it.
    _, padded = crater_depth_batch.tile_bounds_deg(
        0.0, 77.0, tile_size_deg=2.0, padded_tile_size_deg=3.0, max_abs_lat_deg=79.0
    )
    assert padded[3] == pytest.approx(79.0)


def test_default_output_dir_encodes_tuning_parameters(tmp_path):
    config = _config(tmp_path)
    dir_a = crater_depth_batch.default_output_dir(
        config, tile_size_deg=2.0, padded_tile_size_deg=3.0, target_gsd_m=100.0
    )
    dir_b = crater_depth_batch.default_output_dir(
        config, tile_size_deg=4.0, padded_tile_size_deg=6.0, target_gsd_m=100.0
    )
    assert dir_a != dir_b
    assert dir_a.parent == config.cache_root


def test_grade_tile_returns_empty_without_dem_fetch_when_tile_has_no_craters(tmp_path):
    zip_path = tmp_path / "robbins.zip"
    # Crater centered far outside the tile under test (10..12, 0..2).
    _write_robbins_zip(zip_path, rows=[("00-1-000000", 50.0, 200.0, 1.0, 1.0, 0.0, 1.0, 1.0)])
    gpkg_path = craters.geopackage_path(zip_path)
    craters._convert_to_geopackage(zip_path, gpkg_path, moon_radius_m=_MOON_RADIUS_M)

    with (
        mock.patch.object(craters, "ensure_geopackage", return_value=gpkg_path),
        mock.patch.object(cache, "fetch_astropedia_gld100", side_effect=AssertionError("should not be called")),
    ):
        df = crater_depth_batch.grade_tile(10.0, 0.0, config=_config(tmp_path))

    assert df.empty
    assert list(df.columns) == ["CRATER_ID", "diameter_km", "depth_m", "depth_diameter_ratio", "arc_img"]


def test_grade_tile_grades_fitting_crater_and_excludes_oversized_one(tmp_path):
    zip_path = tmp_path / "robbins.zip"
    # Tile nominal bounds: (10, 0, 12, 2); center (11, 1); padded bounds: (9.5, -0.5, 12.5, 2.5).
    crater_lon, crater_lat = 11.0, 1.0
    _write_robbins_zip(
        zip_path,
        rows=[
            # major_km=20 -> semi-axis 10km, comfortably inside the ~1.5deg (~45km) padded-tile margin.
            ("00-1-fits", crater_lat, crater_lon, 20.0, 20.0, 0.0, 1.0, 20.0),
            # major_km=200 -> semi-axis 100km, far bigger than the padded tile itself -- must not fit.
            ("00-1-toobig", crater_lat, crater_lon, 200.0, 200.0, 0.0, 1.0, 200.0),
        ],
    )
    gpkg_path = craters.geopackage_path(zip_path)
    craters._convert_to_geopackage(zip_path, gpkg_path, moon_radius_m=_MOON_RADIUS_M)

    astropedia_path = tmp_path / "astropedia_like.tif"
    # A little extra margin beyond the padded tile bounds so the reprojection's own source window
    # read never lands exactly on this fixture's edge.
    fixture_bounds = (9.0, -1.0, 13.0, 3.0)
    _write_flat_floor_rim_astropedia_like(astropedia_path, fixture_bounds, crater_lon, crater_lat)

    with mock.patch.object(craters, "ensure_geopackage", return_value=gpkg_path):
        df = crater_depth_batch.grade_tile(10.0, 0.0, config=_config(tmp_path), astropedia_path=astropedia_path)

    assert len(df) == 2
    by_id = df.set_index("CRATER_ID")
    assert by_id.loc["00-1-fits", "depth_m"] == pytest.approx(50.0, abs=1.0)
    assert by_id.loc["00-1-fits", "depth_diameter_ratio"] == pytest.approx(50.0 / 20000.0, rel=0.05)
    assert pd.isna(by_id.loc["00-1-toobig", "depth_m"])
    assert pd.isna(by_id.loc["00-1-toobig", "depth_diameter_ratio"])


def test_grade_database_is_resumable_and_respects_limit(tmp_path):
    config = _config(tmp_path)
    output_dir = tmp_path / "out"
    fake_origins = [(0.0, -10.0), (2.0, -10.0), (4.0, -10.0)]

    with (
        mock.patch.object(crater_depth_batch, "iter_tile_origins", return_value=fake_origins),
        mock.patch.object(cache, "fetch_astropedia_gld100", return_value=tmp_path / "astropedia.tif"),
        mock.patch.object(
            crater_depth_batch,
            "grade_tile",
            return_value=pd.DataFrame(
                [{"CRATER_ID": "x", "diameter_km": 1.0, "depth_m": 1.0, "depth_diameter_ratio": 0.001, "arc_img": 1.0}]
            ),
        ) as grade_tile_mock,
    ):
        graded_first = crater_depth_batch.grade_database(config, output_dir=output_dir, limit=2)
        assert graded_first == 2
        assert grade_tile_mock.call_count == 2

        # Re-running should skip the 2 already-written tiles and only grade the 1 remaining.
        graded_second = crater_depth_batch.grade_database(config, output_dir=output_dir)
        assert graded_second == 1
        assert grade_tile_mock.call_count == 3

    written = sorted(output_dir.glob("*.csv"))
    assert len(written) == 3


def test_grade_and_publish_tile_writes_csv_and_is_idempotent(tmp_path):
    config = _config(tmp_path)
    dest = tmp_path / "tile.csv"
    df = pd.DataFrame(
        [{"CRATER_ID": "x", "diameter_km": 1.0, "depth_m": 1.0, "depth_diameter_ratio": 0.001, "arc_img": 1.0}]
    )

    with mock.patch.object(crater_depth_batch, "grade_tile", return_value=df) as grade_tile_mock:
        result_path = crater_depth_batch._grade_and_publish_tile(
            0.0, 0.0, config, tmp_path / "astropedia.tif", 2.0, 3.0, 100.0, dest
        )
        assert result_path == str(dest)
        assert dest.exists()
        assert grade_tile_mock.call_count == 1

        # dest already exists -- must short-circuit without recomputing.
        result_path_again = crater_depth_batch._grade_and_publish_tile(
            0.0, 0.0, config, tmp_path / "astropedia.tif", 2.0, 3.0, 100.0, dest
        )
        assert result_path_again == str(dest)
        assert grade_tile_mock.call_count == 1


def test_grade_tile_task_is_registered_on_its_own_huey_instance():
    assert crater_depth_batch.grade_tile_task.huey is crater_depth_batch.huey_crater_depth
    assert crater_depth_batch.huey_crater_depth is not None
    assert crater_depth_batch.huey_crater_depth is not tasks.huey_parallel


def test_grade_database_via_workers_enqueues_resumably_and_respects_limit(tmp_path):
    config = _config(tmp_path)
    output_dir = tmp_path / "out"
    fake_origins = [(0.0, -10.0), (2.0, -10.0), (4.0, -10.0)]

    fake_result = mock.Mock()
    fake_result.get.return_value = None
    fake_consumer = mock.Mock()

    with (
        mock.patch.object(crater_depth_batch, "iter_tile_origins", return_value=fake_origins),
        mock.patch.object(cache, "fetch_astropedia_gld100", return_value=tmp_path / "astropedia.tif"),
        mock.patch.object(crater_depth_batch.huey_crater_depth, "enqueue", return_value=fake_result) as enqueue_mock,
        mock.patch("trntest.tasks.start_consumer", return_value=fake_consumer) as start_mock,
        mock.patch("trntest.tasks.stop_consumer") as stop_mock,
    ):
        enqueued_first = crater_depth_batch.grade_database_via_workers(
            config, output_dir=output_dir, limit=2, workers=3
        )
        assert enqueued_first == 2
        assert enqueue_mock.call_count == 2
        start_mock.assert_called_once_with(3, huey_module="trntest.crater_depth_batch.huey_crater_depth")
        stop_mock.assert_called_once_with(fake_consumer)
        assert fake_result.get.call_count == 2

        # Nothing was actually written to disk by these mocked-out enqueues, so a second call with
        # no limit should still see all 3 origins as pending (real resumability is exercised by
        # test_grade_database_is_resumable_and_respects_limit against the sequential path, which
        # shares this same origin-iteration/skip-if-exists logic).
        enqueued_second = crater_depth_batch.grade_database_via_workers(config, output_dir=output_dir, workers=3)
        assert enqueued_second == 3


def test_grade_database_via_workers_returns_zero_without_starting_a_consumer_when_nothing_pending(tmp_path):
    config = _config(tmp_path)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    fake_origins = [(0.0, -10.0)]
    (output_dir / f"{crater_depth_batch.tile_id(0.0, -10.0)}.csv").write_text("CRATER_ID\n")

    with (
        mock.patch.object(crater_depth_batch, "iter_tile_origins", return_value=fake_origins),
        mock.patch.object(cache, "fetch_astropedia_gld100", return_value=tmp_path / "astropedia.tif"),
        mock.patch("trntest.tasks.start_consumer") as start_mock,
    ):
        enqueued = crater_depth_batch.grade_database_via_workers(config, output_dir=output_dir)

    assert enqueued == 0
    start_mock.assert_not_called()


def test_load_graded_database_concatenates_tile_files(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    pd.DataFrame(
        [{"CRATER_ID": "a", "diameter_km": 1.0, "depth_m": 1.0, "depth_diameter_ratio": 0.001, "arc_img": 1.0}]
    ).to_csv(output_dir / "tile_a.csv", index=False)
    pd.DataFrame(
        [{"CRATER_ID": "b", "diameter_km": 2.0, "depth_m": 2.0, "depth_diameter_ratio": 0.001, "arc_img": 1.0}]
    ).to_csv(output_dir / "tile_b.csv", index=False)

    combined = crater_depth_batch.load_graded_database(output_dir)

    assert sorted(combined["CRATER_ID"]) == ["a", "b"]


def test_load_graded_database_returns_empty_frame_for_missing_dir(tmp_path):
    combined = crater_depth_batch.load_graded_database(tmp_path / "does_not_exist")
    assert combined.empty
    assert list(combined.columns) == ["CRATER_ID", "diameter_km", "depth_m", "depth_diameter_ratio", "arc_img"]
