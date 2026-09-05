"""Whole-database crater-depth precompute, tiled for cache coherence -- runs
`crater_depth.crater_depth_m` across every Robbins crater in GLD100's own +-79 deg coverage (unlike
`crater_depth.crater_depths_for_footprint`, which covers just one camera footprint), sharing one DEM
reprojection across every crater a tile owns instead of paying a fresh reprojection per crater.

`grade_database`/`grade_database_via_workers` grade the whole database; `grade_footprint` grades just
the tiles one raster's footprint touches. `load_graded_database` reads the per-tile CSV output back;
`consolidate_graded_geopackage` joins it onto the full Robbins GeoPackage for per-footprint queries.
"""
# Two deliberately separate concepts, not one -- conflating them is exactly what would truncate a
# crater near a tile boundary:
#
# - Ownership: which tile is responsible for grading a given crater, decided purely by the crater's
#   own center point falling inside that tile's *nominal* (unpadded) bounds
#   (`craters.query_craters_in_bbox`, no padding) -- the same center-point key `craters.py`'s spatial
#   index is built on, so a crater is graded by exactly one tile, never duplicated.
# - Raster extent: how much DEM data a tile actually reads, sized independently from the nominal
#   tile (`padded_tile_size_deg`, tunable separately from `tile_size_deg`). A crater whose ellipse
#   (plus `crater_depth.crater_depth_m`'s own pixel-diagonal buffer) doesn't fit entirely inside its
#   tile's padded raster gets `depth_m=None` -- kept as a row, not dropped (same convention
#   `crater_depth.crater_depths_for_footprint` uses). Both sizes are fixed global constants, not
#   sized per-crater/per-tile: a rare large crater near a tile edge going ungraded is an accepted
#   cost for this precompute's purpose (finding sharper, smaller craters for a debug view).
#
# Known, deliberately deferred gap: no antimeridian handling. A tile whose padded bounds straddle
# the 0/360 seam isn't specially unwrapped (unlike `geo_utils.footprint_bbox_deg`) -- a narrow band of
# craters right at that seam may be missed or get a `None`-guarded (never silently wrong) result. Not
# worth fixing yet: it's a small fraction of the database, and this precompute's consumer
# (prioritizing sharper craters for a debug view) doesn't depend on any specific longitude.
#
# Output is one small CSV file per tile (`CRATER_ID`, `diameter_km`, `depth_m`,
# `depth_diameter_ratio`, `arc_img` -- no geometry column, unlike `crater_depths_for_footprint`'s
# GeoDataFrame). CSV, not Parquet: this table's schema is simple and each tile's row count small, not
# worth a new dependency (`pyarrow`) for. Atomically published under a directory whose name encodes
# the tuning parameters that determine its content (`_tile_output_dir_name` --
# docs/intermediate-product-discipline.md's "intentional-variant artifacts" principle): a run under
# different `tile_size_deg`/`padded_tile_size_deg`/`target_gsd_m` is a different artifact, not a
# silent overwrite. One file per tile, not one growing table, so `grade_database` is resumable (skip
# a tile whose output already exists) and a `limit` can split a long run across invocations, matching
# `trn_dataset.TrnTestDataSet.populate(limit=...)`'s convention. Stores measured depth only, not a
# sharpness grade: the depth measurement is the slow, DEM-dependent part, while combining it with a
# reference depth into a sharpness score (`consolidate_graded_geopackage`) is cheap enough to leave to
# the read side, so a formula change doesn't require re-running the multi-hour DEM pass.

import math
import tempfile
from collections.abc import Iterator
from pathlib import Path

import geopandas
import pandas as pd
from huey import SqliteHuey
from huey.exceptions import TaskException

from trntest import cache, crater_depth, craters, dem_gld100, dem_ortho, geo_utils, product_registry, tasks
from trntest.config import MOON_RADIUS_M, TrntestConfig, load_config

_config = load_config()
_huey_dir = _config.output_dir / ".huey"
_huey_dir.mkdir(parents=True, exist_ok=True)  # SqliteHuey does not create its own parent dir
huey_crater_depth = SqliteHuey(filename=str(_huey_dir / "crater_depth_tasks.db"), immediate=False)

DEFAULT_TILE_SIZE_DEG = 2.0
DEFAULT_PADDED_TILE_SIZE_DEG = 3.0
# Matches GLD100's own native resolution (docs/data-sources/astropedia-gld100.md) -- no point
# resampling finer, and coarser would waste detail `crater_depth_m`'s percentiles rely on.
DEFAULT_TARGET_GSD_M = 100.0
_FULL_LONGITUDE_RANGE_DEG = 360.0


def iter_tile_origins(
    tile_size_deg: float = DEFAULT_TILE_SIZE_DEG,
    max_abs_lat_deg: float = dem_gld100.ASTROPEDIA_MAX_ABS_LATITUDE_DEG,
) -> Iterator[tuple[float, float]]:
    """Nominal `(lon_min, lat_min)` tile origins tiling the full `[0, 360) x [-max_abs_lat_deg,
    max_abs_lat_deg)` grid, row-major (south to north, west to east within a row).

    :param tile_size_deg: Tile size, degrees.
    :param max_abs_lat_deg: Latitude extent to tile, degrees (+-).
    :returns: An iterator of `(lon_min, lat_min)` origins.
    """
    # Row-major order is deterministic -- useful for resuming a `limit`-bounded run predictably. The
    # northernmost row is clipped to `max_abs_lat_deg` by `tile_bounds_deg`, not here: this just
    # yields origins, so a possibly-shorter last row's origin is still a clean multiple of
    # `tile_size_deg` from `-max_abs_lat_deg`.
    lat = -max_abs_lat_deg
    while lat < max_abs_lat_deg:
        lon = 0.0
        while lon < _FULL_LONGITUDE_RANGE_DEG:
            yield (lon, lat)
            lon += tile_size_deg
        lat += tile_size_deg


def tile_id(lon_min: float, lat_min: float) -> str:
    """Filename-safe identity for the tile whose nominal bounds start at `(lon_min, lat_min)`.

    :param lon_min: Tile origin longitude, degrees.
    :param lat_min: Tile origin latitude, degrees.
    :returns: A fixed-decimal-formatted (not `repr`) identity string, so float-formatting noise can't
        produce two different filenames for what `iter_tile_origins` intends as the same tile.
    """
    return f"lon{lon_min:06.2f}_lat{lat_min:+06.2f}"


def tile_bounds_deg(
    lon_min: float,
    lat_min: float,
    tile_size_deg: float = DEFAULT_TILE_SIZE_DEG,
    padded_tile_size_deg: float = DEFAULT_PADDED_TILE_SIZE_DEG,
    max_abs_lat_deg: float = dem_gld100.ASTROPEDIA_MAX_ABS_LATITUDE_DEG,
) -> tuple[tuple, tuple]:
    """Nominal and padded bounds for the tile at `(lon_min, lat_min)` -- see the module comment above
    for what each is for.

    :param lon_min: Tile origin longitude, degrees.
    :param lat_min: Tile origin latitude, degrees.
    :param tile_size_deg: Nominal tile size, degrees.
    :param padded_tile_size_deg: Padded tile size, degrees.
    :param max_abs_lat_deg: Latitude extent to clip to, degrees (+-).
    :returns: `(nominal_bounds, padded_bounds)`, both `(minlon, minlat, maxlon, maxlat)` degrees.
    """
    # Both clipped to +-max_abs_lat_deg since that's GLD100's own coverage limit; longitude is not
    # similarly clipped/wrapped (see the module comment's antimeridian caveat).
    nominal = (lon_min, lat_min, lon_min + tile_size_deg, min(lat_min + tile_size_deg, max_abs_lat_deg))
    pad_deg = (padded_tile_size_deg - tile_size_deg) / 2.0
    padded = (
        lon_min - pad_deg,
        max(lat_min - pad_deg, -max_abs_lat_deg),
        lon_min + tile_size_deg + pad_deg,
        min(lat_min + tile_size_deg + pad_deg, max_abs_lat_deg),
    )
    return nominal, padded


def _tile_output_dir_name(tile_size_deg: float, padded_tile_size_deg: float, target_gsd_m: float) -> str:
    """Encode the tuning parameters that determine this precompute's content into an output
    directory name (see the module comment's "intentional-variant artifacts" note).

    :param tile_size_deg: Nominal tile size, degrees.
    :param padded_tile_size_deg: Padded tile size, degrees.
    :param target_gsd_m: DEM reprojection resolution, meters/pixel.
    :returns: The directory name.
    """
    return f"crater_depth_tiles_t{tile_size_deg:g}_p{padded_tile_size_deg:g}_g{target_gsd_m:g}"


def default_output_dir(
    config: TrntestConfig,
    tile_size_deg: float = DEFAULT_TILE_SIZE_DEG,
    padded_tile_size_deg: float = DEFAULT_PADDED_TILE_SIZE_DEG,
    target_gsd_m: float = DEFAULT_TARGET_GSD_M,
) -> Path:
    """Default output directory for this precompute's tile files.

    :param config: Project config.
    :param tile_size_deg: Nominal tile size, degrees.
    :param padded_tile_size_deg: Padded tile size, degrees.
    :param target_gsd_m: DEM reprojection resolution, meters/pixel.
    :returns: `config.cache_root / _tile_output_dir_name(...)`.
    """
    # Alongside the other Robbins-derived cache artifacts (`craters.ensure_geopackage`'s own
    # `.gpkg`), not under a dataset's `_tmp/` hierarchy -- this precompute is scoped to the whole
    # crater database, not to any one dataset entry.
    return config.cache_root / _tile_output_dir_name(tile_size_deg, padded_tile_size_deg, target_gsd_m)


def _crater_ellipse_fits(polygon_local, dst_bbox_m: tuple, buffer_m: float) -> bool:
    """Whether `polygon_local`, outward-buffered by `buffer_m`, lies entirely inside `dst_bbox_m`.

    :param polygon_local: Crater ellipse, already in the tile's own local-meters CRS.
    :param dst_bbox_m: The tile's padded raster bounds, local meters.
    :param buffer_m: Outward buffer, meters -- matches `crater_depth.crater_depth_m`'s own
        pixel-diagonal ring buffer.
    :returns: Whether the buffered polygon fits entirely inside `dst_bbox_m`.
    """
    # dst_bbox_m comes directly from the destination grid this precompute built
    # (`geo_utils.footprint_bbox_local_m`), not by reopening the written file to ask its own bounds.
    minx, miny, maxx, maxy = dst_bbox_m
    outer_minx, outer_miny, outer_maxx, outer_maxy = polygon_local.buffer(buffer_m).bounds
    return outer_minx >= minx and outer_miny >= miny and outer_maxx <= maxx and outer_maxy <= maxy


@product_registry.writes_product("crater_depth_tile")
def grade_tile(
    lon_min: float,
    lat_min: float,
    config: TrntestConfig | None = None,
    astropedia_path: Path | None = None,
    tile_size_deg: float = DEFAULT_TILE_SIZE_DEG,
    padded_tile_size_deg: float = DEFAULT_PADDED_TILE_SIZE_DEG,
    target_gsd_m: float = DEFAULT_TARGET_GSD_M,
) -> pd.DataFrame:
    """Grade every Robbins crater owned by the tile at `(lon_min, lat_min)` -- one shared DEM
    reprojection for the whole tile, not one per crater.

    :param lon_min: Tile origin longitude, degrees.
    :param lat_min: Tile origin latitude, degrees.
    :param config: Project config; `load_config()` if not given.
    :param astropedia_path: The cached GLD100 file path, if already resolved -- lets a batch caller
        (`grade_database`) resolve it once and pass it to every tile, rather than re-resolving per
        call.
    :param tile_size_deg: Nominal tile size, degrees.
    :param padded_tile_size_deg: Padded tile size, degrees.
    :param target_gsd_m: DEM reprojection resolution, meters/pixel.
    :returns: One row per owned crater: `CRATER_ID`, `diameter_km` (`DIAM_CIRC_IMG`, matching
        `crater_depth.crater_depths_for_footprint`'s own column), `depth_m`, `depth_diameter_ratio`,
        `arc_img`. `depth_m`/`depth_diameter_ratio` are `None` for a crater whose ellipse (plus
        buffer) doesn't fit entirely inside this tile's padded raster (see the module comment) --
        kept as a row, not dropped. Empty (no DEM fetch at all) if the tile owns no craters.
    """
    # Reprojects onto a fresh local Orthographic CRS centered on this tile (same as the
    # per-camera-footprint path), rather than a direct window read off GLD100's own native
    # Equirectangular grid -- that distinction matters for correctness, not just convenience:
    # `crater_depth.crater_depth_m` skips Breton et al.'s original per-pixel area-weighting, which
    # assumes an isotropic-meters grid (every pixel the same ground area). GLD100's native grid has
    # latitude-dependent east-west compression (down to ~19% of nominal at 79 deg, `cos(latitude)`),
    # so only a tile small enough for low-distortion local-Orthographic reprojection keeps that
    # isotropic assumption valid.
    config = config or load_config()
    nominal, padded = tile_bounds_deg(lon_min, lat_min, tile_size_deg, padded_tile_size_deg)

    gdf = craters.query_craters_in_bbox(nominal, config)
    if len(gdf) == 0:
        return pd.DataFrame(columns=["CRATER_ID", "diameter_km", "depth_m", "depth_diameter_ratio", "arc_img"])

    center_lon = lon_min + tile_size_deg / 2.0
    center_lat = min(lat_min + tile_size_deg / 2.0, dem_gld100.ASTROPEDIA_MAX_ABS_LATITUDE_DEG)
    padded_lon_min, padded_lat_min, padded_lon_max, padded_lat_max = padded
    corners = {
        "sw": (padded_lon_min, padded_lat_min),
        "se": (padded_lon_max, padded_lat_min),
        "nw": (padded_lon_min, padded_lat_max),
        "ne": (padded_lon_max, padded_lat_max),
    }
    dst_bbox_m = geo_utils.footprint_bbox_local_m(corners, center_lon, center_lat, MOON_RADIUS_M)
    dst_width, dst_height = geo_utils.pixel_dims_for_gsd(dst_bbox_m, target_gsd_m)
    local_crs = geo_utils.local_orthographic_crs(center_lon, center_lat, MOON_RADIUS_M)

    astropedia_path = astropedia_path or cache.fetch_astropedia_gld100(config.cache_root, config.astropedia_gld100_url)

    local_crs_gdf = gdf.to_crs(local_crs)
    buffer_m = target_gsd_m * math.sqrt(2) / 2.0

    with tempfile.TemporaryDirectory() as tmp_dir:
        dem_elevation_path = Path(tmp_dir) / "dem_elevation.tif"
        dem_gld100.reproject_astropedia_elevation_to_local_grid(
            astropedia_path,
            padded,
            dst_bbox_m,
            dst_width,
            dst_height,
            center_lon,
            center_lat,
            MOON_RADIUS_M,
            dem_elevation_path,
        )
        # `reproject_astropedia_elevation_to_local_grid`'s own output has no `nodata` tag set (even
        # though gaps -- e.g. GLD100's own small internal nodata cells, see
        # docs/data-sources/astropedia-gld100.md -- are filled with literal NaN), so
        # `crater_depth_m`'s masked read wouldn't mask them, leaking NaN into the percentile as
        # elevation. `dem_ortho.fetch_dem` never hits this because it always runs `hole_fill_dem`
        # first for exactly this reason; same fix applied here.
        dem_path = Path(tmp_dir) / "dem_filled-tile-0.tif"
        dem_ortho.hole_fill_dem(dem_elevation_path, dem_path)

        records = []
        for (_, row), center in zip(gdf.iterrows(), local_crs_gdf.geometry, strict=True):
            polygon = craters._ellipse_polygon(
                center.x, center.y, row["DIAM_ELLI_MAJOR_IMG"], row["DIAM_ELLI_MINOR_IMG"], row["DIAM_ELLI_ANGLE_IMG"]
            )
            if _crater_ellipse_fits(polygon, dst_bbox_m, buffer_m):
                depth_m = crater_depth.crater_depth_m(dem_path, polygon)
            else:
                depth_m = None
            diameter_km = row["DIAM_CIRC_IMG"]
            depth_diameter_ratio = None if depth_m is None else depth_m / (diameter_km * 1000.0)
            records.append(
                {
                    "CRATER_ID": row["CRATER_ID"],
                    "diameter_km": diameter_km,
                    "depth_m": depth_m,
                    "depth_diameter_ratio": depth_diameter_ratio,
                    "arc_img": row["ARC_IMG"],
                }
            )
    return pd.DataFrame(records)


def tiles_covering_bbox(
    bbox_deg: tuple,
    tile_size_deg: float = DEFAULT_TILE_SIZE_DEG,
    max_abs_lat_deg: float = dem_gld100.ASTROPEDIA_MAX_ABS_LATITUDE_DEG,
) -> list[tuple[float, float]]:
    """Nominal `(lon_min, lat_min)` tile origins whose nominal bounds intersect `bbox_deg`.

    :param bbox_deg: `(minlon, minlat, maxlon, maxlat)` degrees, e.g. `craters.raster_bbox_deg`'s
        output.
    :param tile_size_deg: Nominal tile size, degrees.
    :param max_abs_lat_deg: Latitude extent to tile, degrees (+-).
    :returns: A subset of what `iter_tile_origins` would yield for the same `tile_size_deg`/
        `max_abs_lat_deg` -- for grading just enough of the database to cover one footprint
        (`grade_footprint`) rather than the whole grid.
    """
    # Snaps to the same grid `iter_tile_origins` defines (latitude rows starting exactly at
    # `-max_abs_lat_deg`, not at a multiple of `tile_size_deg` from zero), so a tile this returns is
    # always one `grade_database`/`grade_database_via_workers` would also reach -- same
    # resumability, same output filename, whichever entry point graded it first. Same antimeridian
    # caveat as `craters.raster_bbox_deg`: a `bbox_deg` that straddles the 0/360 seam
    # (`minlon > maxlon`) isn't specially unwrapped here either.
    minlon, minlat, maxlon, maxlat = bbox_deg
    lat_row_first = math.floor((minlat - (-max_abs_lat_deg)) / tile_size_deg)
    lat_row_last = math.floor((maxlat - (-max_abs_lat_deg)) / tile_size_deg)
    lon_col_first = math.floor(minlon / tile_size_deg)
    lon_col_last = math.floor(maxlon / tile_size_deg)

    origins = []
    for lat_row in range(lat_row_first, lat_row_last + 1):
        lat_min = -max_abs_lat_deg + lat_row * tile_size_deg
        if not (-max_abs_lat_deg <= lat_min < max_abs_lat_deg):
            continue
        for lon_col in range(lon_col_first, lon_col_last + 1):
            origins.append(((lon_col * tile_size_deg) % _FULL_LONGITUDE_RANGE_DEG, lat_min))
    return origins


def grade_footprint(
    raster_path,
    config: TrntestConfig | None = None,
    output_dir: Path | None = None,
    tile_size_deg: float = DEFAULT_TILE_SIZE_DEG,
    padded_tile_size_deg: float = DEFAULT_PADDED_TILE_SIZE_DEG,
    target_gsd_m: float = DEFAULT_TARGET_GSD_M,
) -> int:
    """Grade just the tiles whose nominal bounds intersect `raster_path`'s own footprint.

    :param raster_path: A raster (e.g. `dem_ortho_result.ortho`) to grade craters within.
    :param config: Project config; `load_config()` if not given.
    :param output_dir: Where tile CSVs are read/written; `default_output_dir(...)` if not given.
    :param tile_size_deg: Nominal tile size, degrees.
    :param padded_tile_size_deg: Padded tile size, degrees.
    :param target_gsd_m: DEM reprojection resolution, meters/pixel.
    :returns: The number of tiles actually graded this call.
    """
    # For reviewing/validating sharpness grading against one candidate image without paying the
    # whole-database precompute's cost. Sequential, matching `grade_database`'s own single-process
    # shape -- a footprint's worth of tiles is small enough this doesn't need
    # `grade_database_via_workers`'s parallelism. Writes into the exact same `output_dir` tile CSVs
    # `grade_database`/`grade_database_via_workers` use (skip-if-exists, same resumability), so a
    # later full-database run doesn't redo this footprint's tiles, and
    # `consolidate_graded_geopackage` picks up whatever's graded here with no special-casing.
    config = config or load_config()
    output_dir = output_dir or default_output_dir(config, tile_size_deg, padded_tile_size_deg, target_gsd_m)
    astropedia_path = cache.fetch_astropedia_gld100(config.cache_root, config.astropedia_gld100_url)

    graded = 0
    for lon_min, lat_min in tiles_covering_bbox(craters.raster_bbox_deg(raster_path), tile_size_deg):
        dest = output_dir / f"{tile_id(lon_min, lat_min)}.csv"
        if dest.exists():
            continue
        _grade_and_publish_tile(
            lon_min, lat_min, config, astropedia_path, tile_size_deg, padded_tile_size_deg, target_gsd_m, dest
        )
        graded += 1
    return graded


def grade_database(
    config: TrntestConfig | None = None,
    output_dir: Path | None = None,
    tile_size_deg: float = DEFAULT_TILE_SIZE_DEG,
    padded_tile_size_deg: float = DEFAULT_PADDED_TILE_SIZE_DEG,
    target_gsd_m: float = DEFAULT_TARGET_GSD_M,
    limit: int | None = None,
) -> int:
    """Drive `grade_tile` across the whole `iter_tile_origins` grid, sequentially in this process.

    :param config: Project config; `load_config()` if not given.
    :param output_dir: Where tile CSVs are written; `default_output_dir(...)` if not given.
    :param tile_size_deg: Nominal tile size, degrees.
    :param padded_tile_size_deg: Padded tile size, degrees.
    :param target_gsd_m: DEM reprojection resolution, meters/pixel.
    :param limit: Stop after grading this many new tiles, if given -- splits a long run across
        multiple invocations, matching `trn_dataset.TrnTestDataSet.populate(limit=...)`'s convention.
    :returns: The number of tiles actually graded this call (not the running total).
    """
    # Writes one atomically-published CSV per tile (`tile_id(lon_min, lat_min) + ".csv"`). Resumable:
    # a tile whose output file already exists is skipped without calling `grade_tile` at all, so
    # re-running this after an interruption only does genuinely new work.
    #
    # Single-threaded and slow at full scale: ~13-14 hours for the whole non-polar grid (14,220
    # tiles, ~1.25M in-coverage craters), measured live across 10 diverse tiles spanning pole to
    # pole -- ~2.2s/tile fixed overhead (mostly the `dem_mosaic` hole-fill subprocess) plus
    # ~0.014s/crater, dominated by the fixed part, not crater count. `grade_database_via_workers` is
    # the multi-worker equivalent for an actual full-database run; this sequential version stays
    # useful for a quick/small/`limit`-bounded pass and as the simpler reference implementation
    # `_grade_and_publish_tile` (shared by both) is tested against.
    config = config or load_config()
    output_dir = output_dir or default_output_dir(config, tile_size_deg, padded_tile_size_deg, target_gsd_m)
    astropedia_path = cache.fetch_astropedia_gld100(config.cache_root, config.astropedia_gld100_url)

    graded = 0
    for lon_min, lat_min in iter_tile_origins(tile_size_deg):
        dest = output_dir / f"{tile_id(lon_min, lat_min)}.csv"
        if dest.exists():
            continue
        _grade_and_publish_tile(
            lon_min, lat_min, config, astropedia_path, tile_size_deg, padded_tile_size_deg, target_gsd_m, dest
        )
        graded += 1
        if limit is not None and graded >= limit:
            break
    return graded


def _grade_and_publish_tile(
    lon_min: float,
    lat_min: float,
    config: TrntestConfig,
    astropedia_path: Path,
    tile_size_deg: float,
    padded_tile_size_deg: float,
    target_gsd_m: float,
    dest: Path,
) -> str:
    """Grade one tile (`grade_tile`) and atomically publish its CSV to `dest`.

    :param lon_min: Tile origin longitude, degrees.
    :param lat_min: Tile origin latitude, degrees.
    :param config: Project config.
    :param astropedia_path: The cached GLD100 file path.
    :param tile_size_deg: Nominal tile size, degrees.
    :param padded_tile_size_deg: Padded tile size, degrees.
    :param target_gsd_m: DEM reprojection resolution, meters/pixel.
    :param dest: Output CSV path. If it already exists, returns immediately without re-grading.
    :returns: `str(dest)`.
    """
    # Shared body for `grade_database`'s sequential loop and `grade_tile_task`'s worker-process body
    # below -- factored out (rather than duplicated, or left only inside the huey task) so it stays
    # directly unit-testable without a live huey consumer: calling a `huey.task()`-decorated function
    # directly on an `immediate=False` instance enqueues rather than runs it. Returns `str(dest)`, not
    # `None` -- huey only stores a result for a non-`None` return (same gotcha as `tasks._generate`'s
    # own docstring), and `grade_database_via_workers` needs a result to wait on.
    if dest.exists():
        return str(dest)
    df = grade_tile(
        lon_min,
        lat_min,
        config,
        astropedia_path=astropedia_path,
        tile_size_deg=tile_size_deg,
        padded_tile_size_deg=padded_tile_size_deg,
        target_gsd_m=target_gsd_m,
    )
    with product_registry.atomic_publish(dest) as tmp:
        df.to_csv(tmp, index=False)
    return str(dest)


@huey_crater_depth.task()
def grade_tile_task(
    lon_min: float,
    lat_min: float,
    config: TrntestConfig,
    astropedia_path: Path,
    tile_size_deg: float,
    padded_tile_size_deg: float,
    target_gsd_m: float,
    dest: Path,
) -> str:
    """What `grade_database_via_workers` enqueues.

    :param lon_min: Tile origin longitude, degrees.
    :param lat_min: Tile origin latitude, degrees.
    :param config: Project config.
    :param astropedia_path: The cached GLD100 file path.
    :param tile_size_deg: Nominal tile size, degrees.
    :param padded_tile_size_deg: Padded tile size, degrees.
    :param target_gsd_m: DEM reprojection resolution, meters/pixel.
    :param dest: Output CSV path.
    :returns: `str(dest)`, see `_grade_and_publish_tile`.
    """
    # Thin wrapper so a `-k process` worker runs `_grade_and_publish_tile` through huey's own
    # queue/result machinery (stored exceptions, a real process boundary) rather than in this calling
    # process. `config`/`astropedia_path`/`dest` all pickle cleanly (a plain dataclass and two
    # `Path`s -- no SPICE/open-file state), confirmed the same way
    # `tasks.generate_product_parallel`'s own docstring already establishes for its own (different)
    # task argument.
    return _grade_and_publish_tile(
        lon_min, lat_min, config, astropedia_path, tile_size_deg, padded_tile_size_deg, target_gsd_m, dest
    )


def grade_database_via_workers(
    config: TrntestConfig | None = None,
    output_dir: Path | None = None,
    tile_size_deg: float = DEFAULT_TILE_SIZE_DEG,
    padded_tile_size_deg: float = DEFAULT_PADDED_TILE_SIZE_DEG,
    target_gsd_m: float = DEFAULT_TARGET_GSD_M,
    limit: int | None = None,
    workers: int = 4,
) -> int:
    """`grade_database`'s multi-worker equivalent -- each tile's `grade_tile` + atomic-publish
    (`_grade_and_publish_tile`) runs in one of `workers` separate `-k process` worker processes
    instead of sequentially in this one.

    :param config: Project config; `load_config()` if not given.
    :param output_dir: Where tile CSVs are written; `default_output_dir(...)` if not given.
    :param tile_size_deg: Nominal tile size, degrees.
    :param padded_tile_size_deg: Padded tile size, degrees.
    :param target_gsd_m: DEM reprojection resolution, meters/pixel.
    :param limit: Enqueue at most this many new tiles, if given.
    :param workers: Worker process count.
    :returns: The number of tiles enqueued this call (0 if every tile up to `limit` -- or the whole
        grid, if `limit` is `None` -- was already done).
    """
    # Same `output_dir`/tile-size/`limit` semantics as `grade_database` (a tile whose output file
    # already exists is skipped, same resumability), via a real `huey_consumer` subprocess this call
    # starts and tears down for its own duration -- see `trn_dataset.TrnTestDataSet.
    # populate_via_workers`'s docstring, whose exact pattern this mirrors (`tasks.start_consumer`/
    # `stop_consumer`; `tasks.start_consumer` was generalized to take a `huey_module` argument rather
    # than duplicate that subprocess-management code for a second task domain). A dedicated
    # `huey_crater_depth` instance (own sqlite file, own task), not `tasks.huey_parallel` -- that
    # one's task (`generate_product_parallel`) takes a `TrnTestImage`, a different domain entirely;
    # per `tasks.py`'s own docstring, one `Huey` instance per use case, not shared. `-k process` (real
    # worker processes, not threads) for the same reason `tasks.py` already gives:
    # SPICE/spiceypy-adjacent process-global state (this module doesn't touch SPICE directly, but
    # `craters.py`/`geo_utils.py` do) isn't safe to share within one process. Blocks until every
    # enqueued tile finishes or fails; one tile's failure doesn't abort the batch (`TaskException`
    # caught, not raised, same as `trn_dataset._await_result`) -- DEM/ISIS calls at this scale are
    # expected to have occasional failures.
    config = config or load_config()
    output_dir = output_dir or default_output_dir(config, tile_size_deg, padded_tile_size_deg, target_gsd_m)
    astropedia_path = cache.fetch_astropedia_gld100(config.cache_root, config.astropedia_gld100_url)

    to_enqueue = []
    for lon_min, lat_min in iter_tile_origins(tile_size_deg):
        dest = output_dir / f"{tile_id(lon_min, lat_min)}.csv"
        if dest.exists():
            continue
        to_enqueue.append((lon_min, lat_min, dest))
        if limit is not None and len(to_enqueue) >= limit:
            break
    if not to_enqueue:
        return 0

    results = []
    for lon_min, lat_min, dest in to_enqueue:
        task = grade_tile_task.s(
            lon_min, lat_min, config, astropedia_path, tile_size_deg, padded_tile_size_deg, target_gsd_m, dest
        )
        task.id = f"crater_depth_tile::{tile_id(lon_min, lat_min)}"
        results.append(huey_crater_depth.enqueue(task))

    consumer = tasks.start_consumer(workers, huey_module="trntest.crater_depth_batch.huey_crater_depth")
    try:
        for result in results:
            try:
                result.get(blocking=True, preserve=True)
            except TaskException:
                pass
    finally:
        tasks.stop_consumer(consumer)
    return len(to_enqueue)


@product_registry.reads_product("crater_depth_tile")
def load_graded_database(output_dir: Path) -> pd.DataFrame:
    """Concatenate every tile's CSV file under `output_dir` into one `DataFrame`.

    :param output_dir: `grade_database`'s own output directory.
    :returns: The combined table (`CRATER_ID`, `diameter_km`, `depth_m`, `depth_diameter_ratio`,
        `arc_img`), or an empty frame with those columns if `output_dir` has no tile files yet.
    """
    # The read-side counterpart callers join a sharpness formula (e.g.
    # `crater_depth.stoffler_fresh_depth_km`) onto by `CRATER_ID`.
    columns = ["CRATER_ID", "diameter_km", "depth_m", "depth_diameter_ratio", "arc_img"]
    tile_paths = sorted(Path(output_dir).glob("*.csv"))
    if not tile_paths:
        return pd.DataFrame(columns=columns)
    return pd.concat([pd.read_csv(p) for p in tile_paths], ignore_index=True)


@product_registry.writes_product("robbins_with_depth")
def consolidate_graded_geopackage(
    config: TrntestConfig | None = None,
    output_dir: Path | None = None,
    tile_size_deg: float = DEFAULT_TILE_SIZE_DEG,
    padded_tile_size_deg: float = DEFAULT_PADDED_TILE_SIZE_DEG,
    target_gsd_m: float = DEFAULT_TARGET_GSD_M,
) -> Path:
    """Left-join `load_graded_database`'s combined depth table onto the full Robbins GeoDataFrame by
    `CRATER_ID`, and write the result as its own GeoPackage.

    :param config: Project config; `load_config()` if not given.
    :param output_dir: Where tile CSVs are read from; `default_output_dir(...)` if not given.
    :param tile_size_deg: Nominal tile size, degrees.
    :param padded_tile_size_deg: Padded tile size, degrees.
    :param target_gsd_m: DEM reprojection resolution, meters/pixel.
    :returns: `output_dir / "robbins_with_depth.gpkg"`.
    """
    # A single, ready-to-query "Robbins database, now with depth" artifact
    # (`craters.ensure_geopackage`, every column, geometry, no bbox restriction). GeoPackage, not
    # Parquet, deliberately: every existing query function in this codebase
    # (`craters.query_craters_in_bbox`, `crater_overlay_layer`, `crater_depths_for_footprint`, this
    # module's own `grade_tile`) already relies on GeoPackage's `rtree` spatial index for fast
    # `bbox=`-restricted queries (measured live elsewhere at ~0.05s for a 10x10 deg box against the
    # full ~1.3M-row table). A caller wanting per-footprint sharpness -- this project's more common
    # case -- can query this file with that same pattern, unchanged. An occasional whole-database
    # load (`geopandas.read_file(path)`, no `bbox=`) is slower and heavier than a lean Parquet would
    # be, but workable for what's expected to stay the less-frequent case -- not worth a second,
    # separately-maintained artifact/format until that's confirmed to matter in practice.
    #
    # Only `depth_m`/`depth_diameter_ratio` are joined in, not `load_graded_database`'s own
    # `diameter_km`/`arc_img` -- those already exist on the Robbins side as `DIAM_CIRC_IMG`/
    # `ARC_IMG`, so merging them too would create redundant, confusingly-named duplicate columns. A
    # left join, not inner: every Robbins crater is kept, including any not yet graded
    # (`grade_database`/`grade_database_via_workers` still mid-run, or never reached) --
    # `depth_m`/`depth_diameter_ratio` are `NaN` for those rows, indistinguishable from a crater that
    # was graded but didn't fit its tile's padded raster (`grade_tile`'s own `None` convention,
    # already an accepted ambiguity, not a new one).
    #
    # Also adds `sharpness` (`crater_depth.sharpness_ratio(depth_m, DIAM_CIRC_IMG)`) here rather than
    # leaving every caller to recompute it -- cheap (no DEM read, just arithmetic against columns
    # already in hand) and, unlike `depth_m` itself, not worth protecting from a multi-hour re-run if
    # the formula ever changes: `NaN` propagates through automatically for any row without a depth.
    #
    # This is a snapshot, not kept in sync automatically -- re-run this after grading more tiles to
    # refresh it; nothing in `grade_database`/`grade_database_via_workers` calls this on its own,
    # matching this project's pattern for other generated-not-auto-synced artifacts (e.g.
    # `notebooks/dataset_manifest.csv`).
    config = config or load_config()
    output_dir = output_dir or default_output_dir(config, tile_size_deg, padded_tile_size_deg, target_gsd_m)

    depth_df = load_graded_database(output_dir)[["CRATER_ID", "depth_m", "depth_diameter_ratio"]]
    gpkg_path = craters.ensure_geopackage(config)
    robbins_gdf = geopandas.read_file(gpkg_path)
    joined = robbins_gdf.merge(depth_df, on="CRATER_ID", how="left")
    joined["sharpness"] = crater_depth.sharpness_ratio(joined["depth_m"], joined["DIAM_CIRC_IMG"])

    dest = output_dir / "robbins_with_depth.gpkg"
    with product_registry.atomic_publish(dest) as tmp:
        joined.to_file(tmp, driver="GPKG")
    return dest
