"""Real-data validation + throughput profiling for `crater_depth.crater_depth_m`: carves a
512x512-pixel (~51.2km x 51.2km at GLD100's 100 m/px) tile directly out of the real, cached GLD100
file, finds every real Robbins crater whose own required footprint (its ellipse polygon plus
`crater_depth_m`'s own half-pixel-diagonal margin) is fully contained within it, and times
`crater_depth_m` per crater to extrapolate a rough, order-of-magnitude estimate for processing the
whole non-polar (`|lat| < lunaserv.ASTROPEDIA_MAX_ABS_LATITUDE_DEG`) Robbins database against this
same DEM.

**Deliberately centered at (lon=180, lat=0)** -- GLD100's own central meridian and standard
parallel. `crater_depth_m` assumes locally isotropic meters (`pixel_size_m = abs(transform.a)`),
which only holds exactly there: away from the standard parallel, this Equidistant Cylindrical
file's longitude-direction pixel spacing shrinks by `cos(latitude)` in real ground distance while
the latitude-direction spacing does not (the same meridian-convergence effect `docs/plan.md`
already documents for Lunaserv's own geographic-CRS `mapproject` round-trip -- see its "Resolved"
item on switching to a per-camera local Orthographic CRS). `crater_depths_for_footprint`'s normal
callers avoid this by working against a per-camera local Orthographic reprojection
(`lunaserv.fetch_dem_and_ortho`), not GLD100's raw global CRS directly, the way this test does. This
test's equatorial tile sidesteps the distortion rather than fixing it. **Empirically quantified**
(real craters at 30/45/60/78.5 deg latitude, ellipse built the same way this file does then measured
via a local-Orthographic reprojection): a polygon's true east-west extent comes out ~0.87x/0.71x/
0.50x/0.20x its real size at those latitudes -- `cos(latitude)`, not a small margin effect. Any
future batch job that (unlike this test) reads raw global tiles directly to cover the *whole*
non-polar database would need a per-tile/per-crater local reprojection first -- not optional, a real
requirement this test's own equatorial choice sidesteps entirely rather than validates away.

Marked `@pytest.mark.heavy`: reads the real, cached GLD100 file (`cache.fetch_astropedia_gld100` --
already-cached-once is fast; a cold cache means a real ~10GB download) and the real Robbins
GeoPackage (`craters.query_craters_for_raster`, similarly already-cached-once vs. a real ~92MB
download + one-time GeoPackage conversion)."""

import math
import time

import numpy as np
import pytest
import rasterio
import rasterio.transform
import rasterio.warp
import rasterio.windows
import shapely.geometry

from trntest import cache, crater_depth, craters, lunaserv
from trntest.config import MOON_RADIUS_M, load_config

_TILE_SIZE_PX = 512
# GLD100's own central meridian (180) and standard parallel (0, the equator) -- see this module's
# own docstring for why this specific point matters, not just any equatorial point.
_TILE_CENTER_LON_DEG = 180.0
_TILE_CENTER_LAT_DEG = 0.0
_MIN_FULLY_CONTAINED_CRATERS = 10
# The real Robbins database's own real row count for craters D>=1km, confirmed live against the
# actual downloaded file (`docs/data-sources.md`'s Robbins section) -- used only to extrapolate this
# test's own measured per-crater time to a whole-database estimate, not re-fetched/re-counted live
# (a second, redundant read of the whole real GeoPackage this profiling pass doesn't need).
_ROBBINS_TOTAL_CRATERS = 1_296_796


def _fully_contained_ellipse_polygons(tile_path, config):
    """Every real Robbins crater in `tile_path`'s own AOI whose ellipse polygon, buffered by the same
    half-pixel-diagonal margin `crater_depth_m` itself uses for its outer ring, lands entirely
    inside `tile_path`'s real bounds -- "completely within the tile, with margin" from a caller's
    own request, using the exact margin the depth computation already needs rather than an
    arbitrary extra one."""
    with rasterio.open(tile_path) as src:
        tile_crs = src.crs
        tile_box = shapely.geometry.box(*src.bounds)
        half_diag_m = abs(src.transform.a) * math.sqrt(2) / 2.0

    gdf = craters.query_craters_for_raster(tile_path, config)
    centers_in_tile_crs = gdf.to_crs(tile_crs)
    polygons = []
    for (_, row), center in zip(gdf.iterrows(), centers_in_tile_crs.geometry, strict=True):
        polygon = craters._ellipse_polygon(
            center.x, center.y, row["DIAM_ELLI_MAJOR_IMG"], row["DIAM_ELLI_MINOR_IMG"], row["DIAM_ELLI_ANGLE_IMG"]
        )
        if tile_box.contains(polygon.buffer(half_diag_m)):
            polygons.append(polygon)
    return polygons


@pytest.mark.heavy
def test_crater_depth_throughput_over_a_real_gld100_tile(tmp_path):
    config = load_config()
    gld100_path = cache.fetch_astropedia_gld100(config.cache_root, config.astropedia_gld100_url)

    with rasterio.open(gld100_path) as src:
        geo_crs = lunaserv.geographic_crs(MOON_RADIUS_M)
        (center_x,), (center_y,) = rasterio.warp.transform(
            geo_crs, src.crs, [_TILE_CENTER_LON_DEG], [_TILE_CENTER_LAT_DEG]
        )
        center_row, center_col = rasterio.transform.rowcol(src.transform, center_x, center_y)
        window = rasterio.windows.Window(
            int(center_col) - _TILE_SIZE_PX // 2, int(center_row) - _TILE_SIZE_PX // 2, _TILE_SIZE_PX, _TILE_SIZE_PX
        )
        tile = src.read(1, window=window)
        tile_transform = rasterio.windows.transform(window, src.transform)
        tile_crs, nodata = src.crs, src.nodata

    tile_path = tmp_path / "gld100_tile.tif"
    with rasterio.open(
        tile_path,
        "w",
        driver="GTiff",
        height=_TILE_SIZE_PX,
        width=_TILE_SIZE_PX,
        count=1,
        dtype=tile.dtype,
        crs=tile_crs,
        transform=tile_transform,
        nodata=nodata,
    ) as dst:
        dst.write(tile, 1)

    polygons = _fully_contained_ellipse_polygons(tile_path, config)
    assert len(polygons) >= _MIN_FULLY_CONTAINED_CRATERS, (
        f"only {len(polygons)} fully-contained real craters in this real equatorial tile -- too few "
        "for a meaningful timing sample"
    )

    elapsed_per_crater = []
    depths = []
    for polygon in polygons:
        start = time.perf_counter()
        depths.append(crater_depth.crater_depth_m(tile_path, polygon))
        elapsed_per_crater.append(time.perf_counter() - start)

    elapsed = np.array(elapsed_per_crater)
    n_valid_depth = sum(d is not None for d in depths)
    mean_s, median_s = float(elapsed.mean()), float(np.median(elapsed))

    # Rough order-of-magnitude extrapolation only -- see this module's own docstring for the
    # equatorial-tile caveat, and docs/data-sources.md's "Crater depth" section for the batch-scale
    # open items (GLD100's own row-strip, not tiled, I/O layout; no per-crater-window caching) this
    # simple, single-threaded, one-tile timing loop doesn't measure or address.
    non_polar_fraction = math.sin(math.radians(lunaserv.ASTROPEDIA_MAX_ABS_LATITUDE_DEG))
    estimated_non_polar_craters = _ROBBINS_TOTAL_CRATERS * non_polar_fraction
    estimated_total_hours = estimated_non_polar_craters * mean_s / 3600.0

    print(
        f"\ncrater_depth_m throughput over a real {_TILE_SIZE_PX}x{_TILE_SIZE_PX}px GLD100 tile "
        f"(centered lon={_TILE_CENTER_LON_DEG}, lat={_TILE_CENTER_LAT_DEG}):\n"
        f"  fully-contained real craters tested: {len(polygons)} ({n_valid_depth} got a real depth)\n"
        f"  mean / median per-crater time: {mean_s * 1000:.2f} ms / {median_s * 1000:.2f} ms\n"
        f"  extrapolated to ~{estimated_non_polar_craters:,.0f} non-polar Robbins craters "
        f"(sin({lunaserv.ASTROPEDIA_MAX_ABS_LATITUDE_DEG} deg) of the real "
        f"{_ROBBINS_TOTAL_CRATERS:,} total, D>=1km): ~{estimated_total_hours:.1f} hours, "
        "single-threaded, naive independent per-crater reads, no batching/parallelism/re-tiling\n"
    )

    assert mean_s < 5.0, f"per-crater depth computation unexpectedly slow: {mean_s:.3f}s mean"
