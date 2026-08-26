"""Whole-database crater-depth precompute, tiled for cache coherence -- runs
`crater_depth.crater_depth_m` across every Robbins crater in GLD100's own +-79 deg coverage, not
just one camera footprint's worth (unlike `crater_depth.crater_depths_for_footprint`), sharing one
DEM reprojection across every crater a tile owns instead of paying a fresh reprojection per crater.

Two deliberately separate concepts, not one, because conflating them is exactly what makes a crater
near a tile boundary get truncated:

- **Ownership**: which tile is responsible for computing a given crater, decided purely by the
  crater's own center point falling inside that tile's *nominal* (unpadded) bounds
  (`craters.query_craters_in_bbox`, no padding) -- the same center-point key `craters.py`'s own
  spatial index is already built on, so a crater is graded by exactly one tile, never duplicated.
- **Raster extent**: how much DEM data that tile actually reads, which is *not* bounded by the
  nominal tile size at all -- each tile's DEM is reprojected over a larger, fixed *padded* bbox
  (`padded_tile_size_deg`, independently tunable from `tile_size_deg`). A crater whose real ellipse
  (plus `crater_depth.crater_depth_m`'s own small pixel-diagonal buffer) doesn't fit entirely inside
  its own tile's padded raster gets `depth_m=None` -- kept as a row, not dropped, same convention
  `crater_depth.crater_depths_for_footprint` already uses. Both tile sizes are fixed, global
  constants here (not sized per-crater/per-tile from the data), a deliberate simplification: rare,
  large outlier craters near a tile edge going ungraded is an accepted cost, not a correctness bug --
  this precompute's actual purpose is finding *sharper* (shallower-relative, smaller) craters for a
  debug view, where the rare large-crater miss doesn't matter.

Each tile's real DEM read comes straight out of the already-locally-cached GLD100 flat file
(`cache.fetch_astropedia_gld100`, ~10GB, downloaded once and shared across every tile in a run --
`grade_database` resolves it once, not per tile) via the same
`lunaserv.reproject_astropedia_elevation_to_local_grid` reprojection the per-camera-footprint path
already uses, onto a fresh local Orthographic CRS centered on that tile -- **not** a direct window
read off GLD100's own native Equirectangular grid. That distinction matters for correctness, not
just convenience: `crater_depth.crater_depth_m` was deliberately simplified to skip Breton et al.'s
original per-pixel area-weighting because it assumes an isotropic-meters grid (every pixel covers
the same real ground area), which only holds reprojected -- GLD100's own native grid has real,
latitude-dependent east-west compression (down to ~19% of nominal at 79 deg, `cos(latitude)`), so a
tile small enough for its own local-Orthographic reprojection to stay low-distortion is also what
keeps `crater_depth_m`'s isotropic assumption valid.

**Known, deliberately deferred gap**: no antimeridian handling. A tile whose padded bounds straddle
the 0/360 deg longitude seam isn't specially unwrapped here (unlike e.g.
`lunaserv.footprint_bbox_deg`'s own antimeridian unwrapping) -- a narrow band of craters right at
that seam may be missed or get a wrong-looking (but `None`-guarded, never silently wrong) result.
Not fixed yet since it's a small fraction of the database and this precompute's own consumer
(prioritizing sharper craters for a debug view) doesn't depend on any specific longitude.

Output is one small CSV file per tile (`CRATER_ID`, `diameter_km`, `depth_m`, `depth_diameter_ratio`,
`arc_img` -- no geometry column, unlike `crater_depths_for_footprint`'s GeoDataFrame, since nothing
downstream of this precompute needs per-crater shape). CSV, not Parquet: this table's schema is
simple and each tile's row count small, so Parquet's columnar/typed advantages aren't worth a new
real dependency (`pyarrow`, pandas' Parquet engine) here -- plain `pandas.DataFrame.to_csv`/`read_csv`
needs none. Atomically published (`product_registry.atomic_publish`) under a directory whose own
name encodes the tuning parameters that determine its content (`_tile_output_dir_name` -- principle
1's "intentional-variant artifacts" from `docs/intermediate-product-discipline.md`: two runs under
different `tile_size_deg`/`padded_tile_size_deg`/`target_gsd_m` are different artifacts, not the
same one silently overwritten). One file per tile, not one growing table, specifically so
`grade_database` is resumable (skip a tile whose output file already exists) and so a `limit`
parameter can split a long run across multiple invocations, matching
`trn_dataset.TrnTestDataSet.populate(limit=...)`'s own existing convention -- appending to one
shared file wouldn't get either property for free under principle 4's atomic-publish-once model.
Deliberately stores measured depth only, not a sharpness grade -- the depth measurement is the slow,
DEM-dependent part; combining it with a reference/fresh depth (e.g.
`crater_depth.stoffler_fresh_depth_km`) into an actual sharpness score is cheap and not yet decided,
so it's left to `load_graded_database`'s caller rather than baked into this precompute (a formula
change shouldn't require re-running the whole multi-hour DEM pass).

**`grade_database_via_workers` is the real multi-worker path** -- measured live (10 diverse real
tiles spanning pole to pole), a single-threaded `grade_database` run over the whole non-polar grid
(14,220 tiles, ~1.25M in-coverage craters) costs ~13-14 hours: ~2.2s/tile fixed overhead (mostly the
`dem_mosaic` hole-fill subprocess) plus ~0.014s/crater, dominated by the fixed part, not crater
count. Reuses `trn_dataset.TrnTestDataSet.populate_via_workers`'s own established pattern exactly
(`tasks.start_consumer`/`stop_consumer` managing a real `huey_consumer -k process` subprocess) --
generalized `tasks.start_consumer` to take a `huey_module` argument rather than duplicate that
subprocess-management code for a second task domain. A dedicated `huey_crater_depth` instance (own
sqlite file, own task), not `tasks.huey_parallel` -- that one's task (`generate_product_parallel`)
takes a `TrnTestImage`, a different domain entirely; per `tasks.py`'s own docstring, one `Huey`
instance per real use case, not shared. `-k process` (real worker processes, not threads) for the
same reason `tasks.py` already gives: SPICE/spiceypy-adjacent process-global state (this module
doesn't touch SPICE directly, but `craters.py`/`lunaserv.py` do) isn't safe to share within one
process.

**`consolidate_graded_geopackage` is the read-side consolidation step** -- left-joins
`load_graded_database`'s combined depth table onto the full Robbins GeoDataFrame by `CRATER_ID` and
writes it as its own GeoPackage, so a per-footprint query (this project's more common case) can
reuse `craters.query_craters_in_bbox`'s exact same fast `bbox=`-restricted, spatial-index-backed
pattern directly against "Robbins plus depth" rather than needing to know tiling ever happened.
GeoPackage, not Parquet, specifically because that spatial-index reuse is worth more here than
Parquet's columnar/compression advantage would be for the less-frequent whole-database-scan case --
see the function's own docstring for the full reasoning. Also computes and stores the actual
sharpness grade itself here (`sharpness`, `crater_depth.sharpness_ratio` -- measured depth over
Stoffler et al. 2006's reference "fresh crater" depth for the same diameter), since that combination
is cheap and, unlike the depth measurement itself, safe to recompute on every consolidation without
re-running the multi-hour DEM pass. A snapshot, not auto-synced -- re-run it after grading more
tiles."""

import math
import tempfile
from collections.abc import Iterator
from pathlib import Path

import geopandas
import pandas as pd
from huey import SqliteHuey
from huey.exceptions import TaskException

from trntest import cache, crater_depth, craters, lunaserv, product_registry, tasks
from trntest.config import MOON_RADIUS_M, TrntestConfig, load_config

_config = load_config()
_huey_dir = _config.output_dir / ".huey"
_huey_dir.mkdir(parents=True, exist_ok=True)  # SqliteHuey does not create its own parent dir
huey_crater_depth = SqliteHuey(filename=str(_huey_dir / "crater_depth_tasks.db"), immediate=False)

DEFAULT_TILE_SIZE_DEG = 2.0
DEFAULT_PADDED_TILE_SIZE_DEG = 3.0
# Matches GLD100's own native resolution (see docs/data-sources.md) -- no point resampling finer,
# and coarser would waste real detail `crater_depth_m`'s percentiles rely on.
DEFAULT_TARGET_GSD_M = 100.0
_FULL_LONGITUDE_RANGE_DEG = 360.0


def iter_tile_origins(
    tile_size_deg: float = DEFAULT_TILE_SIZE_DEG,
    max_abs_lat_deg: float = lunaserv.ASTROPEDIA_MAX_ABS_LATITUDE_DEG,
) -> Iterator[tuple[float, float]]:
    """Nominal `(lon_min, lat_min)` tile origins tiling the full `[0, 360) x [-max_abs_lat_deg,
    max_abs_lat_deg)` grid, row-major (south to north, west to east within a row -- deterministic
    order, useful for resuming a `limit`-bounded run predictably). The northernmost row is clipped to
    `max_abs_lat_deg` by `tile_bounds_deg` below, not here -- this just yields origins, not full
    bounds, so a possibly-shorter last row's origin is still a clean multiple of `tile_size_deg` from
    `-max_abs_lat_deg`."""
    lat = -max_abs_lat_deg
    while lat < max_abs_lat_deg:
        lon = 0.0
        while lon < _FULL_LONGITUDE_RANGE_DEG:
            yield (lon, lat)
            lon += tile_size_deg
        lat += tile_size_deg


def tile_id(lon_min: float, lat_min: float) -> str:
    """Filename-safe identity for the tile whose nominal bounds start at `(lon_min, lat_min)` --
    fixed decimal formatting (not `repr`) so float-formatting noise can't produce two different
    filenames for what `iter_tile_origins` intends as the same tile."""
    return f"lon{lon_min:06.2f}_lat{lat_min:+06.2f}"


def tile_bounds_deg(
    lon_min: float,
    lat_min: float,
    tile_size_deg: float = DEFAULT_TILE_SIZE_DEG,
    padded_tile_size_deg: float = DEFAULT_PADDED_TILE_SIZE_DEG,
    max_abs_lat_deg: float = lunaserv.ASTROPEDIA_MAX_ABS_LATITUDE_DEG,
) -> tuple[tuple, tuple]:
    """`(nominal_bounds, padded_bounds)`, both `(minlon, minlat, maxlon, maxlat)` degrees. Nominal
    bounds decide crater *ownership* (query craters by center within these, unpadded); padded bounds
    decide how much DEM this tile actually reprojects/reads -- independently sized, clipped to
    `+-max_abs_lat_deg` since that's GLD100's own real coverage limit (see module docstring's
    antimeridian caveat for why longitude is *not* similarly clipped/wrapped)."""
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
    """Encodes the parameters that determine this precompute's own content into the output
    directory's name (`docs/intermediate-product-discipline.md`'s principle 1) -- a run under
    different tuning knobs is a genuinely different artifact, not a silent overwrite of this one."""
    return f"crater_depth_tiles_t{tile_size_deg:g}_p{padded_tile_size_deg:g}_g{target_gsd_m:g}"


def default_output_dir(
    config: TrntestConfig,
    tile_size_deg: float = DEFAULT_TILE_SIZE_DEG,
    padded_tile_size_deg: float = DEFAULT_PADDED_TILE_SIZE_DEG,
    target_gsd_m: float = DEFAULT_TARGET_GSD_M,
) -> Path:
    """Alongside the other Robbins-derived cache artifacts (`craters.ensure_geopackage`'s own
    `.gpkg`), not under a dataset's `_tmp/` hierarchy -- this precompute is scoped to the whole
    crater database, not to any one dataset entry."""
    return config.cache_root / _tile_output_dir_name(tile_size_deg, padded_tile_size_deg, target_gsd_m)


def _crater_ellipse_fits(polygon_local, dst_bbox_m: tuple, buffer_m: float) -> bool:
    """Whether `polygon_local` (already in the tile's own local-meters CRS), outward-buffered by
    `buffer_m` (matching `crater_depth.crater_depth_m`'s own pixel-diagonal ring buffer), lies
    entirely inside `dst_bbox_m` -- the tile's padded raster bounds, known directly from the
    destination grid this precompute built (`lunaserv.footprint_bbox_local_m`), not by reopening the
    written file just to ask it its own bounds back."""
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
    """Grades every Robbins crater owned by the tile at `(lon_min, lat_min)` (see `tile_bounds_deg`)
    -- one shared DEM reprojection for the whole tile, not one per crater. Returns a plain (no
    geometry) `pandas.DataFrame`, one row per owned crater: `CRATER_ID`, `diameter_km`
    (`DIAM_CIRC_IMG`, matching `crater_depth.crater_depths_for_footprint`'s own column), `depth_m`,
    `depth_diameter_ratio`, `arc_img`. `depth_m`/`depth_diameter_ratio` are `None` for a crater whose
    real ellipse (plus buffer) doesn't fit entirely inside this tile's own padded raster (see module
    docstring) -- kept as a row, not dropped. Returns an empty `DataFrame` (no DEM fetch at all) if
    the tile owns no craters -- the common case is skipping this cheaply, not paying a reprojection
    for empty ocean-of-craters gaps that don't actually exist at ~1.3M rows, but a real short-circuit
    all the same.

    `astropedia_path` lets a batch caller (`grade_database`) resolve the ~10GB cached GLD100 file
    once and pass it to every tile, rather than re-resolving (cheap, but not free) per call."""
    config = config or load_config()
    nominal, padded = tile_bounds_deg(lon_min, lat_min, tile_size_deg, padded_tile_size_deg)

    gdf = craters.query_craters_in_bbox(nominal, config)
    if len(gdf) == 0:
        return pd.DataFrame(columns=["CRATER_ID", "diameter_km", "depth_m", "depth_diameter_ratio", "arc_img"])

    center_lon = lon_min + tile_size_deg / 2.0
    center_lat = min(lat_min + tile_size_deg / 2.0, lunaserv.ASTROPEDIA_MAX_ABS_LATITUDE_DEG)
    padded_lon_min, padded_lat_min, padded_lon_max, padded_lat_max = padded
    corners = {
        "sw": (padded_lon_min, padded_lat_min),
        "se": (padded_lon_max, padded_lat_min),
        "nw": (padded_lon_min, padded_lat_max),
        "ne": (padded_lon_max, padded_lat_max),
    }
    dst_bbox_m = lunaserv.footprint_bbox_local_m(corners, center_lon, center_lat, MOON_RADIUS_M)
    dst_width, dst_height = lunaserv.pixel_dims_for_gsd(dst_bbox_m, target_gsd_m)
    local_crs = lunaserv.local_orthographic_crs(center_lon, center_lat, MOON_RADIUS_M)

    astropedia_path = astropedia_path or cache.fetch_astropedia_gld100(config.cache_root, config.astropedia_gld100_url)

    local_crs_gdf = gdf.to_crs(local_crs)
    buffer_m = target_gsd_m * math.sqrt(2) / 2.0

    with tempfile.TemporaryDirectory() as tmp_dir:
        dem_elevation_path = Path(tmp_dir) / "dem_elevation.tif"
        lunaserv.reproject_astropedia_elevation_to_local_grid(
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
        # `reproject_astropedia_elevation_to_local_grid`'s own raw output has no `nodata` tag set on
        # the file (even though real gaps -- e.g. GLD100's own small internal nodata cells, see
        # docs/data-sources.md -- are filled with literal NaN), so `crater_depth_m`'s masked read
        # wouldn't mask them at all, leaking NaN into the percentile as if it were real elevation.
        # `lunaserv.fetch_dem` never hits this because it always runs `hole_fill_dem` first for
        # exactly this reason -- same fix applied here, not skipped.
        dem_path = Path(tmp_dir) / "dem_filled-tile-0.tif"
        lunaserv.hole_fill_dem(dem_elevation_path, dem_path)

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
    max_abs_lat_deg: float = lunaserv.ASTROPEDIA_MAX_ABS_LATITUDE_DEG,
) -> list[tuple[float, float]]:
    """Nominal `(lon_min, lat_min)` tile origins (a subset of what `iter_tile_origins` would yield
    for the same `tile_size_deg`/`max_abs_lat_deg`) whose nominal bounds intersect `bbox_deg`
    (`minlon, minlat, maxlon, maxlat`, e.g. `craters.raster_bbox_deg`'s output) -- for grading just
    enough of the database to cover one footprint (`grade_footprint`) rather than the whole grid.
    Snaps to the same grid `iter_tile_origins` defines (latitude rows starting exactly at
    `-max_abs_lat_deg`, not at a multiple of `tile_size_deg` from zero), so a tile this returns is
    always one `grade_database`/`grade_database_via_workers` would also reach -- same resumability,
    same output filename, whichever entry point graded it first. Same antimeridian caveat as
    `craters.raster_bbox_deg`: a `bbox_deg` that straddles the 0/360 seam (`minlon > maxlon`) isn't
    specially unwrapped here either."""
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
    """Grades just the tiles whose nominal bounds intersect `raster_path`'s own real footprint
    (`craters.raster_bbox_deg`, `tiles_covering_bbox`) -- for reviewing/validating sharpness grading
    against one candidate image (e.g. `dem_ortho_result.ortho`) without paying the whole-database
    precompute's cost. Sequential, matching `grade_database`'s own single-process shape -- a
    footprint's worth of tiles is small enough this doesn't need `grade_database_via_workers`'s real
    parallelism. Writes into the exact same `output_dir` tile CSVs `grade_database`/
    `grade_database_via_workers` use (skip-if-exists, same resumability), so a later full-database
    run doesn't redo this footprint's tiles, and `consolidate_graded_geopackage` picks up whatever's
    graded here with no special-casing. Returns the number of tiles actually graded this call."""
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
    """Drives `grade_tile` across the whole `iter_tile_origins` grid, sequentially in this one
    process, writing one atomically-published CSV file per tile (`tile_id(lon_min, lat_min) +
    ".csv"`, under `output_dir` or `default_output_dir`). Resumable: a tile whose output file
    already exists is skipped without calling `grade_tile` at all, so re-running this after an
    interruption (or splitting a long run across multiple invocations via `limit`, matching
    `trn_dataset.TrnTestDataSet.populate(limit=...)`'s own convention) only does genuinely new work.
    Returns the number of tiles actually graded this call (not the running total).

    Single-threaded and slow at full scale (~13-14 hours for the whole grid -- see module
    docstring) -- `grade_database_via_workers` is the real multi-worker equivalent for an actual
    full-database run; this sequential version stays useful for a quick/small/`limit`-bounded pass
    and as the simpler reference implementation `_grade_and_publish_tile` (shared by both) is
    tested against."""
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
    """Shared body for `grade_database`'s sequential loop and `grade_tile_task`'s worker-process
    body below -- grades one tile (`grade_tile`) and atomically publishes its CSV to `dest`, same
    two steps either way. Factored out (rather than duplicated, or left only inside the huey task)
    specifically so it stays directly unit-testable without needing a live huey consumer -- calling
    a `huey.task()`-decorated function directly on an `immediate=False` instance enqueues rather
    than runs it, so the real logic has to live somewhere callable on its own. Returns `str(dest)`,
    not `None` -- huey only stores a result for a non-`None` return (confirmed empirically, see
    `tasks._generate`'s own docstring for the same gotcha), and `grade_database_via_workers` needs a
    real result to wait on."""
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
    """What `grade_database_via_workers` enqueues -- thin wrapper so a `-k process` worker runs
    `_grade_and_publish_tile` through huey's own queue/result machinery (stored exceptions, a real
    process boundary) rather than in this calling process. `config`/`astropedia_path`/`dest` all
    pickle cleanly (a plain dataclass and two `Path`s -- no SPICE/open-file state), confirmed the
    same way `tasks.generate_product_parallel`'s own docstring already establishes for its own
    (different) task argument."""
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
    """`grade_database`'s real multi-worker equivalent -- same `output_dir`/tile-size/`limit`
    semantics (a tile whose output file already exists is skipped, same resumability), but each
    tile's `grade_tile` + atomic-publish (`_grade_and_publish_tile`) runs in one of `workers`
    separate `-k process` worker processes instead of sequentially in this one, via a real
    `huey_consumer` subprocess this call starts and tears down for its own duration -- see the
    module docstring's own paragraph on this function and `trn_dataset.TrnTestDataSet.
    populate_via_workers`'s docstring, whose exact pattern this mirrors. Blocks until every enqueued
    tile finishes (or fails); one tile's failure doesn't abort the batch (`TaskException` caught, not
    raised, same as `trn_dataset._await_result`) -- real DEM/ISIS calls at this scale are expected to
    have occasional real failures. Returns the number of tiles enqueued this call (0 if every tile
    up to `limit` -- or the whole grid, if `limit` is `None` -- was already done)."""
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
    """Concatenates every tile's CSV file under `output_dir` (`grade_database`'s own output) into one
    `pandas.DataFrame` -- the read-side counterpart callers join a sharpness formula (e.g.
    `crater_depth.stoffler_fresh_depth_km`) onto by `CRATER_ID`. Returns an empty frame with the
    expected columns if `output_dir` has no tile files yet, rather than raising."""
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
    """Left-joins `load_graded_database`'s combined depth table onto the *full* Robbins GeoDataFrame
    (`craters.ensure_geopackage`, every column, real geometry, no bbox restriction) by `CRATER_ID`,
    and writes the result as its own atomically-published GeoPackage -- a single, ready-to-query
    "Robbins database, now with depth" artifact, `output_dir / "robbins_with_depth.gpkg"`.

    GeoPackage, not Parquet, deliberately: every existing query function in this codebase
    (`craters.query_craters_in_bbox`, `crater_overlay_layer`, `crater_depths_for_footprint`, this
    module's own `grade_tile`) already relies on GeoPackage's real `rtree` spatial index for fast
    `bbox=`-restricted queries -- confirmed live elsewhere at ~0.05s for a 10x10 deg box against the
    full ~1.3M-row table. A caller wanting per-footprint sharpness (this project's own more common
    case, per the user's own framing) can query this file with that exact same pattern, unchanged.
    An occasional whole-database load (`geopandas.read_file(path)`, no `bbox=`) is slower and heavier
    than a lean Parquet would be, but workable for what's expected to stay the less-frequent case --
    not worth a second, separately-maintained artifact/format until that's confirmed to actually
    matter in practice.

    Only `depth_m`/`depth_diameter_ratio` are joined in, not `load_graded_database`'s own
    `diameter_km`/`arc_img` columns -- those already exist on the Robbins side as `DIAM_CIRC_IMG`/
    `ARC_IMG`, so merging them in too would just create redundant, confusingly-named duplicate
    columns. A **left** join, not inner: every real Robbins crater is kept, including any not yet
    graded (`grade_database`/`grade_database_via_workers` still mid-run, or never reached) --
    `depth_m`/`depth_diameter_ratio` are `NaN` for those rows, indistinguishable from a crater that
    *was* graded but didn't fit its tile's padded raster (`grade_tile`'s own `None` convention,
    already an accepted ambiguity, not a new one introduced here).

    Also adds `sharpness` (`crater_depth.sharpness_ratio(depth_m, DIAM_CIRC_IMG)`, the actual grade)
    here rather than leaving every caller to recompute it -- cheap (no DEM read, just arithmetic
    against columns already in hand) and, unlike `depth_m` itself, not something worth protecting
    from a multi-hour re-run if the formula ever changes: `NaN` propagates through automatically for
    any row without a depth, so no extra handling is needed for the same not-yet-graded/didn't-fit
    rows above.

    **This is a snapshot, not kept in sync automatically** -- re-run this after grading more tiles to
    refresh it; nothing in `grade_database`/`grade_database_via_workers` calls this on its own,
    matching this project's existing pattern for other generated-not-auto-synced artifacts (e.g.
    `notebooks/dataset_manifest.csv`)."""
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
