"""DEM sources, and the elevation mosaic `dem_ortho.fetch_dem` builds from them.

A source is a set of Equidistant Cylindrical tiles (GLD100 is one global tile, SLDEM2015 32). A DEM
is a `DemMosaic` (`DEM_SOURCES`, selected by `TrntestConfig.dem_source`): sources in precedence
order, each pixel from the first with data there, plus any seam treatments. See docs/data-sources/
and docs/map-seams.md.
"""

import dataclasses
import math
from collections.abc import Callable
from pathlib import Path

import numpy as np
from pyproj import Transformer
from rasterio.fill import fillnodata
from rasterio.warp import Resampling, transform_bounds

from trntest import cache, dem_gld100
from trntest.config import DEFAULT_DEM_SOURCE, MOON_RADIUS_M, SLDEM2015_TILE_NAMES, TrntestConfig
from trntest.geo_utils import (
    DEM_FETCH_SAFETY_MARGIN_FRACTION,
    geographic_crs,
    local_orthographic_crs,
    merge_local_grid_arrays,
    pad_bbox,
    pixel_center_coords_m,
    read_eqc_raster_to_local_grid_array,
)


@dataclasses.dataclass(frozen=True)
class LocalGrid:
    """A local Orthographic working grid, as `dem_ortho.fetch_dem` builds one per entry.

    :ivar bbox_m: `(minx, miny, maxx, maxy)`, meters.
    :ivar width: Pixels.
    :ivar height: Pixels.
    :ivar center_lon_deg: Tangent point longitude, degrees.
    :ivar center_lat_deg: Tangent point latitude, degrees.
    """

    bbox_m: tuple
    width: int
    height: int
    center_lon_deg: float
    center_lat_deg: float

    @property
    def gsd_m(self) -> float:
        return (self.bbox_m[2] - self.bbox_m[0]) / self.width


@dataclasses.dataclass(frozen=True)
class DemTile:
    """One Equidistant Cylindrical raster of a source.

    :ivar tile_id: Identifier passed to the source's `fetch`.
    :ivar lon_range_deg: `(west, east)` longitude, degrees in `[0, 360]`.
    :ivar lat_range_deg: `(south, north)` latitude, degrees.
    """

    tile_id: str
    lon_range_deg: tuple[float, float]
    lat_range_deg: tuple[float, float]


@dataclasses.dataclass(frozen=True)
class DemSource:
    """A DEM made of `DemTile`s.

    :ivar name: Short name, e.g. `"gld100"`.
    :ivar tiles: Every tile.
    :ivar fetch: `(tile_id, config) -> path` GDAL opens; caches the tile on first use.
    :ivar to_meters: Factor from the raster's values to elevation in meters.
    :ivar pixel_m: Native pixel size at the equator, meters; decides averaging vs. bilinear.
    :ivar check_coverage: For a source that must cover the whole grid (the last in a `DEM_SOURCES`
        entry), `(grid) -> None` raising `ValueError` if it can't.
    """

    name: str
    tiles: tuple[DemTile, ...]
    fetch: Callable[[str, TrntestConfig], Path]
    to_meters: float
    pixel_m: float
    check_coverage: Callable[[LocalGrid], None]


def _check_gld100_coverage(grid: LocalGrid) -> None:
    dem_gld100.check_astropedia_coverage(grid.bbox_m, grid.center_lon_deg, grid.center_lat_deg, MOON_RADIUS_M)


def _no_full_coverage(grid: LocalGrid) -> None:
    raise ValueError("SLDEM2015 covers only +-60 deg; use it with a fallback source")


GLD100 = DemSource(
    name="gld100",
    tiles=(
        DemTile(
            "gld100",
            (0.0, 360.0),
            (-dem_gld100.ASTROPEDIA_MAX_ABS_LATITUDE_DEG, dem_gld100.ASTROPEDIA_MAX_ABS_LATITUDE_DEG),
        ),
    ),
    fetch=lambda tile_id, config: cache.fetch_astropedia_gld100(config.cache_root, config.astropedia_gld100_url),
    to_meters=1.0,
    pixel_m=100.0,
    check_coverage=_check_gld100_coverage,
)
"""USGS Astropedia's GLD100: one global file, integer meters, +-79 deg."""


def _sldem2015_tile(name: str) -> DemTile:
    # e.g. "SLDEM2015_512_30S_00S_090_135_FLOAT": latitude band, then longitude range.
    _, _, south_north, north_south, west, east, _ = name.split("_")

    def lat(token: str) -> float:
        return float(token[:-1]) * (1 if token.endswith("N") else -1)

    south, north = sorted((lat(south_north), lat(north_south)))
    return DemTile(name, (float(west), float(east)), (south, north))


SLDEM2015 = DemSource(
    name="sldem2015",
    tiles=tuple(_sldem2015_tile(name) for name in SLDEM2015_TILE_NAMES),
    fetch=lambda tile_id, config: cache.fetch_sldem2015_tile(tile_id, config.cache_root, config.sldem2015_base_url),
    to_meters=1000.0,
    pixel_m=2 * math.pi * MOON_RADIUS_M / 360 / 512,
    check_coverage=_no_full_coverage,
)
"""SLDEM2015 at 512 ppd: 32 tiles, float32 km above the 1737.4 km sphere, +-60 deg."""


@dataclasses.dataclass(frozen=True)
class LatSeam:
    """Treatment of the seam where a mosaic's first source stops at a latitude (both hemispheres) and
    the next takes over.

    :ivar abs_lat_deg: The seam's |latitude|, degrees.
    :ivar feather_deg: Width of the band equatorward of the seam, degrees, over which the first source
        blends linearly into the next (both have data there), so a vertical offset between them
        becomes a gentle slope instead of a step.
    :ivar reject_deg: `(equatorward, poleward)` extent around the seam, degrees, where the result is
        discarded and filled from its surroundings: the next source's own defective rows there.
    """

    abs_lat_deg: float
    feather_deg: float
    reject_deg: tuple[float, float]


@dataclasses.dataclass(frozen=True)
class DemMosaic:
    """A DEM built from sources in precedence order.

    :ivar sources: Precedence order. The last must cover any grid it's used for (its
        `check_coverage`).
    :ivar lat_seams: Treatments where the first source meets the next; none means a hard cut.
    """

    sources: tuple[DemSource, ...]
    lat_seams: tuple[LatSeam, ...] = ()


# GLD100 is itself assembled at +-60 deg: its first ~2 rows poleward of 60 carry a line (gradient
# ~2x its surroundings) and, at 60N, a nodata row, and its two sides there disagree locally (row-to-row
# change ~19 m vs. 3-5 m nearby at 60S). SLDEM2015 stops at exactly 60, so the cut can't move away
# from them. The reject band covers those rows and spreads the local disagreement over ~7 px of the
# 100 m grid: the narrowest band that brings every probe under `seam_probes.dem_thresholds`' gradient limit
# (worst 1.28, vs. 1.46 for a 3 px band); wider ones smooth the band below its surroundings' texture
# (ratio ~0.75). SLDEM2015 runs 4-15 m above GLD100 near 60 (regional, constant up to the seam) and
# carries more texture; the 0.1 deg (~3 km) feather turns both into gradual changes. Measured in
# `notebooks/dem_seams.ipynb`.
_SLDEM2015_GLD100_SEAM = LatSeam(abs_lat_deg=60.0, feather_deg=0.1, reject_deg=(0.007, 0.015))

DEM_SOURCES: dict[str, DemMosaic] = {
    "gld100": DemMosaic((GLD100,)),
    "sldem2015_gld100": DemMosaic((SLDEM2015, GLD100), (_SLDEM2015_GLD100_SEAM,)),
    "sldem2015_gld100_hardcut": DemMosaic((SLDEM2015, GLD100)),
}
"""`TrntestConfig.dem_source` -> its mosaic. `"sldem2015_gld100_hardcut"` is the same mosaic without
its seam treatment, kept for the seam inventory's pass without mitigations (docs/map-seams.md)."""


def aoi_lonlat_range_deg(grid: LocalGrid) -> tuple[float, float, float, float]:
    """The padded grid's extent: `(west, east, south, north)`, degrees. `west`/`east` are continuous
    across 0/360 deg (`west` may be negative or `east` over 360), within 180 deg of the grid's center.

    :param grid: The grid.
    :returns: The extent.
    """
    # Longitude from an Equirectangular CRS centered on the grid (no branch cut inside the AOI),
    # latitude from the geographic CRS; see `geo_utils.read_eqc_raster_to_local_grid_array`.
    padded = pad_bbox(grid.bbox_m, DEM_FETCH_SAFETY_MARGIN_FRACTION)
    ortho = local_orthographic_crs(grid.center_lon_deg, grid.center_lat_deg, MOON_RADIUS_M)
    centered = f"+proj=eqc +lat_ts=0 +lon_0={grid.center_lon_deg} +R={MOON_RADIUS_M} +units=m +no_defs"
    x_west, _, x_east, _ = transform_bounds(ortho, centered, *padded)
    _, south, _, north = transform_bounds(ortho, geographic_crs(MOON_RADIUS_M), *padded)
    center = grid.center_lon_deg % 360.0
    return center + math.degrees(x_west / MOON_RADIUS_M), center + math.degrees(x_east / MOON_RADIUS_M), south, north


def tiles_for_grid(source: DemSource, grid: LocalGrid) -> list[DemTile]:
    """The tiles of `source` that overlap `grid` (padded).

    :param source: The source.
    :param grid: The grid.
    :returns: Overlapping tiles, in `source.tiles` order; empty if none.
    """
    west, east, south, north = aoi_lonlat_range_deg(grid)

    def overlaps(tile: DemTile) -> bool:
        tile_south, tile_north = tile.lat_range_deg
        if north <= tile_south or south >= tile_north:
            return False
        tile_west, tile_east = tile.lon_range_deg
        return any(west < tile_east + k and east > tile_west + k for k in (-360.0, 0.0, 360.0))

    return [tile for tile in source.tiles if overlaps(tile)]


def source_elevation(source: DemSource, grid: LocalGrid, config: TrntestConfig) -> np.ndarray:
    """`source`'s elevation on `grid`, fetching the tiles it needs.

    :param source: The source.
    :param grid: The grid.
    :param config: Project config (cache location, URLs).
    :returns: `(height, width)` float32 elevation, meters; `NaN` where the source has no data.
    """
    # Averaging where the source is finer than the grid, so the downsampled texture matches what a
    # coarser source shows on the other side of a seam (docs/map-seams.md); bilinear otherwise.
    resampling = Resampling.average if grid.gsd_m > 1.5 * source.pixel_m else Resampling.bilinear
    arrays = [
        read_eqc_raster_to_local_grid_array(
            source.fetch(tile.tile_id, config),
            grid.bbox_m,
            grid.width,
            grid.height,
            grid.center_lon_deg,
            grid.center_lat_deg,
            MOON_RADIUS_M,
            resampling,
        )
        for tile in tiles_for_grid(source, grid)
    ]
    if not arrays:
        return np.full((grid.height, grid.width), np.nan, dtype="float32")
    return merge_local_grid_arrays(arrays) * np.float32(source.to_meters)


def grid_latitudes_deg(grid: LocalGrid) -> np.ndarray:
    """Each pixel center's latitude.

    :param grid: The grid.
    :returns: `(height, width)` latitudes, degrees.
    """
    xs, ys = pixel_center_coords_m(grid.bbox_m, grid.width, grid.height)
    to_geo = Transformer.from_crs(
        local_orthographic_crs(grid.center_lon_deg, grid.center_lat_deg, MOON_RADIUS_M),
        geographic_crs(MOON_RADIUS_M),
        always_xy=True,
    )
    _, lat = to_geo.transform(*np.meshgrid(xs, ys))
    return lat


def mosaic_elevation(mosaic: DemMosaic, grid: LocalGrid, config: TrntestConfig) -> np.ndarray:
    """Elevation on `grid` from `mosaic`: each pixel from the first source with data there, then the
    mosaic's seam treatments.

    :param mosaic: E.g. a `DEM_SOURCES` value.
    :param grid: The grid.
    :param config: Project config.
    :returns: `(height, width)` float32 elevation, meters; `NaN` where no source has data (gaps
        `dem_ortho.hole_fill_dem` then fills).
    :raises ValueError: If the last source can't cover `grid` (its `check_coverage`).
    """
    sources = mosaic.sources
    sources[-1].check_coverage(grid)
    first = source_elevation(sources[0], grid, config)
    merged = first.copy()
    next_source: np.ndarray | None = None
    for source in sources[1:]:
        missing = np.isnan(merged)
        if not missing.any() and not mosaic.lat_seams:
            break
        elevation = source_elevation(source, grid, config)
        if next_source is None:
            next_source = elevation
        merged[missing] = elevation[missing]
    if not mosaic.lat_seams or next_source is None:
        return merged
    distance = np.abs(grid_latitudes_deg(grid))
    rejected = np.zeros(merged.shape, dtype=bool)
    for seam in mosaic.lat_seams:
        from_seam = distance - seam.abs_lat_deg  # negative = equatorward
        feather = (from_seam > -seam.feather_deg) & (from_seam < 0) & np.isfinite(first) & np.isfinite(next_source)
        weight = ((from_seam[feather] + seam.feather_deg) / seam.feather_deg).astype(np.float32)
        merged[feather] = (1 - weight) * first[feather] + weight * next_source[feather]
        rejected |= (from_seam >= -seam.reject_deg[0]) & (from_seam <= seam.reject_deg[1])
    if not rejected.any():
        return merged
    # Fill only the rejected band (inverse-distance weighting from its edges); any other gap is left
    # for `dem_ortho.hole_fill_dem`, which doesn't fill a band that reaches the raster's edge.
    candidate = np.where(rejected, np.nan, merged)
    filled = fillnodata(np.nan_to_num(candidate, nan=0.0), mask=np.isfinite(candidate), max_search_distance=20)
    merged[rejected] = filled[rejected]
    return merged


def dem_source_suffix(dem_source: str) -> str:
    """Filename suffix identifying a non-default `dem_source` (`""` for the default), shared by every
    product built from the DEM.

    :param dem_source: A `DEM_SOURCES` key.
    :returns: The suffix.
    :raises ValueError: If `dem_source` isn't a `DEM_SOURCES` key.
    """
    if dem_source not in DEM_SOURCES:
        raise ValueError(f"dem_source={dem_source!r} is not one of {tuple(DEM_SOURCES)!r}")
    return "" if dem_source == DEFAULT_DEM_SOURCE else f"_dem-{dem_source}"
