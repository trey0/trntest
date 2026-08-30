"""Crater depth measurement -- the input to grading crater sharpness. Adapted from Breton et al. 2019
("Semi-Automated Crater Depth Measurements", *MethodsX* 6, 2293-2304, DOI `10.1016/j.mex.2019.08.007`):
depth = the 60th percentile of elevation in a ring around a crater's rim, minus the 3rd percentile of
elevation inside the crater. Adopted because it's already validated in the literature rather than
derived from scratch here.

Deliberately scoped to GLD100 (`lunaserv`'s live default DEM source, ±79 deg latitude coverage) --
see `docs/crater-grading.md` for the full rationale, including the global-DEM alternatives considered
and set aside.
"""

import math

import geopandas
import numpy as np
import rasterio
import rasterio.features
import rasterio.windows

from trntest import craters, lunaserv
from trntest.config import MOON_RADIUS_M, TrntestConfig, load_config


def crater_depth_m(
    dem_path, crater_polygon, floor_percentile: float = 3.0, rim_percentile: float = 60.0
) -> float | None:
    """One crater's depth, meters.

    :param dem_path: DEM raster path.
    :param crater_polygon: The crater's ellipse polygon, already in `dem_path`'s own CRS (e.g. from
        `craters._ellipse_polygon`, reprojected to the DEM's raster CRS).
    :param floor_percentile: Percentile of elevation inside the crater used as the floor.
    :param rim_percentile: Percentile of elevation in the ring just outside the crater used as the
        rim.
    :returns: `rim_percentile` of elevation in the ring, minus `floor_percentile` of elevation
        inside -- or `None` if either region has zero valid (in-DEM, non-nodata) pixels (crater not
        covered by this DEM tile, or too small relative to the DEM's own resolution for a pixel
        center to land inside one or both regions).
    """
    # `floor_percentile`/`rim_percentile` are Breton et al. 2019's own chosen values -- low-but-not-
    # minimum and high-but-not-maximum, both robust to a single noisy/misclassified pixel in a way
    # the true min/max wouldn't be.
    #
    # The interior/edge split uses a `pixel_size_m * sqrt(2) / 2` (half a pixel's diagonal) buffer of
    # the crater polygon -- the same buffer distance the original paper's own circle-radius
    # inside/edge test uses, applied to the ellipse directly instead of an equivalent-circle
    # approximation, since this project already has the ellipse shape on hand.
    # `rasterio.features.geometry_mask` (GDAL's own pixel-center-in-polygon test) selects pixels for
    # each region -- simpler than the original paper's own per-pixel area-intersection test, and
    # exact rather than approximate here because this project's DEMs are fetched onto a local,
    # isotropic-meters Orthographic CRS (`lunaserv.fetch_dem_and_ortho`), where every pixel already
    # covers the same ground area. The original's own per-pixel area-weighting (needed on its
    # lon/lat raster, where non-square degree-pixels don't all cover the same ground area) is
    # dropped entirely here, not approximated -- it's a provable no-op on a uniform grid.
    # `rim_percentile` is unweighted either way, matching the original (its own rim percentile was
    # never area-weighted to begin with).
    with rasterio.open(dem_path) as src:
        transform = src.transform
        pixel_size_m = abs(transform.a)
        half_diag_m = pixel_size_m * math.sqrt(2) / 2.0

        outer_polygon = crater_polygon.buffer(half_diag_m)
        floor_polygon = crater_polygon.buffer(-half_diag_m)
        ring_polygon = outer_polygon if floor_polygon.is_empty else outer_polygon.difference(floor_polygon)

        window = rasterio.windows.from_bounds(*outer_polygon.bounds, transform=transform)
        window = window.round_offsets().round_lengths()
        window_transform = rasterio.windows.transform(window, transform)
        dem = src.read(1, window=window, boundless=True, fill_value=src.nodata, masked=True)

    out_shape = dem.shape
    if out_shape[0] == 0 or out_shape[1] == 0:
        return None
    valid = ~np.ma.getmaskarray(dem)

    ring_mask = rasterio.features.geometry_mask([ring_polygon], out_shape, window_transform, invert=True)
    ring_values = dem.data[ring_mask & valid]
    if ring_values.size == 0:
        return None

    if floor_polygon.is_empty:
        floor_values = np.empty(0, dtype=dem.dtype)
    else:
        floor_mask = rasterio.features.geometry_mask([floor_polygon], out_shape, window_transform, invert=True)
        floor_values = dem.data[floor_mask & valid]
    if floor_values.size == 0:
        return None

    floor_depth = np.percentile(floor_values, floor_percentile)
    rim_depth = np.percentile(ring_values, rim_percentile)
    return float(rim_depth - floor_depth)


STOFFLER_SIMPLE_COEFF = 0.196
STOFFLER_SIMPLE_EXPONENT = 1.010
STOFFLER_COMPLEX_COEFF = 1.044
STOFFLER_COMPLEX_EXPONENT = 0.301
# Derived, not hardcoded independently, so it can't drift from the two curves above -- solves
# `STOFFLER_SIMPLE_COEFF * D^STOFFLER_SIMPLE_EXPONENT == STOFFLER_COMPLEX_COEFF *
# D^STOFFLER_COMPLEX_EXPONENT` for D. Documentation/test value only (~10.58 km, matching Stoffler et
# al. 2006's own stated crossover) -- `stoffler_fresh_depth_km` itself doesn't branch on it.
STOFFLER_CROSSOVER_DIAMETER_KM = (STOFFLER_COMPLEX_COEFF / STOFFLER_SIMPLE_COEFF) ** (
    1.0 / (STOFFLER_SIMPLE_EXPONENT - STOFFLER_COMPLEX_EXPONENT)
)


def stoffler_fresh_depth_km(diameter_km):
    """Reference ("fresh crater") depth for a crater of `diameter_km`.

    :param diameter_km: Crater diameter, km (scalar or array/Series) -- matches `DIAM_CIRC_IMG`'s own
        units.
    :returns: Reference depth, km.
    """
    # Per Stoffler et al. 2006 ("Cratering History and Lunar Chronology", *Reviews in Mineralogy and
    # Geochemistry* 60(1), 519-596, DOI `10.2138/rmg.2006.60.05`) -- the classic two-regime lunar
    # depth-diameter relation: simple craters follow `STOFFLER_SIMPLE_COEFF *
    # D^STOFFLER_SIMPLE_EXPONENT`, complex craters follow the shallower `STOFFLER_COMPLEX_COEFF *
    # D^STOFFLER_COMPLEX_EXPONENT`, crossing at `STOFFLER_CROSSOVER_DIAMETER_KM` (~10.58 km).
    #
    # Returns `min(simple, complex)` rather than branching on the crossover explicitly -- provably
    # identical to the textbook piecewise form for any `diameter_km > 0`. Since the simple-crater
    # exponent is the larger one, the complex-crater curve decays more slowly and so dominates (is
    # larger) as `D -> 0`; the simple-crater curve eventually overtakes it once, at the crossover, and
    # stays larger for all `D` beyond it. The two curves therefore cross exactly once for `D > 0`, so
    # their elementwise minimum picks the correct regime on both sides with no separate branch, and is
    # exactly continuous at the crossover by construction (both formulas agree there). Vectorized via
    # `np.minimum` -- takes a scalar or an array/Series alike.
    simple_km = STOFFLER_SIMPLE_COEFF * np.power(diameter_km, STOFFLER_SIMPLE_EXPONENT)
    complex_km = STOFFLER_COMPLEX_COEFF * np.power(diameter_km, STOFFLER_COMPLEX_EXPONENT)
    return np.minimum(simple_km, complex_km)


def sharpness_ratio(depth_m, diameter_km):
    """Sharpness grade: measured depth over the Stoffler et al. 2006 reference depth for the same
    diameter.

    :param depth_m: Measured depth, meters (scalar or array/Series, matching `crater_depth_m`'s/
        `crater_depths_for_footprint`'s own `depth_m` column) -- a `None`/`NaN` value (an ungraded or
        doesn't-fit-its-tile crater, see `crater_depth_batch.py`) propagates to a `NaN` result.
    :param diameter_km: Crater diameter, km, same length as `depth_m` if both are arrays.
    :returns: `depth_m` over `stoffler_fresh_depth_km(diameter_km)` -- ~1.0 for a crater as deep as a
        fresh crater of its size, well below 1.0 for a degraded one, and above 1.0 for scatter around
        the reference curve (Stoffler's own relation is a central-tendency fit, not an upper bound).
    """
    # Converted to km to match `stoffler_fresh_depth_km`'s own units before dividing.
    return (np.asarray(depth_m, dtype=float) / 1000.0) / stoffler_fresh_depth_km(diameter_km)


def _too_close_to_astropedia_pole(lat_deg: float, major_km: float) -> bool:
    """Whether a crater could extend past GLD100's own coverage limit.

    :param lat_deg: Crater center latitude, degrees.
    :param major_km: Crater ellipse-fit major axis, full length, km.
    :returns: Whether the crater's angular half-extent, added to its center latitude, would cross
        `lunaserv.ASTROPEDIA_MAX_ABS_LATITUDE_DEG`.
    """
    # Great-circle arc, small enough here that the flat approximation `distance / MOON_RADIUS_M` is
    # fine. Checked directly from catalog fields (no DEM read) so a batch pass over many craters can
    # skip the read entirely for excluded ones, not just discover the gap empirically per-crater.
    half_extent_m = (major_km / 2.0) * 1000.0
    margin_deg = math.degrees(half_extent_m / MOON_RADIUS_M)
    return abs(lat_deg) + margin_deg > lunaserv.ASTROPEDIA_MAX_ABS_LATITUDE_DEG


def crater_depths_for_footprint(
    raster_path,
    config: TrntestConfig | None = None,
    min_major_km: float | None = None,
    min_arc_img: float | None = None,
) -> geopandas.GeoDataFrame:
    """`crater_depth_m` for every Robbins crater covering `raster_path`'s own footprint.

    :param raster_path: A raster (DEM) to grade craters within.
    :param config: Project config; `load_config()` if not given.
    :param min_major_km: Same filter as `craters.crater_overlay_layer`'s own `min_major_km` (see its
        docstring and body comment).
    :param min_arc_img: Same filter as `craters.crater_overlay_layer`'s own `min_arc_img` (see its
        docstring and body comment).
    :returns: One row per surviving crater: `CRATER_ID`, `diameter_km` (`DIAM_CIRC_IMG` -- this
        database's own circle-fit diameter, independent of the ellipse-fit axes used for the depth
        geometry itself), `depth_m`, `depth_diameter_ratio`, `arc_img`, and `geometry` (the crater's
        ellipse polygon, in `raster_path`'s own CRS, same as `crater_overlay_layer`'s).
        `depth_m`/`depth_diameter_ratio` are `None` for any crater too close to GLD100's own coverage
        limit (`_too_close_to_astropedia_pole`, checked before ever calling `crater_depth_m` -- no
        DEM read for these) or for any `crater_depth_m` miss (e.g. a nodata gap) -- kept as rows, not
        dropped, so a caller can see how many/which craters were excluded and why.
    """
    # Uses `craters.query_craters_for_raster`, the same AOI-derivation `crater_overlay_layer` uses.
    config = config or load_config()
    gdf = craters.query_craters_for_raster(raster_path, config)
    if min_major_km is not None:
        gdf = gdf[gdf["DIAM_ELLI_MAJOR_IMG"] >= min_major_km]
    if min_arc_img is not None:
        gdf = gdf[gdf["ARC_IMG"] >= min_arc_img]

    with rasterio.open(raster_path) as src:
        raster_crs = src.crs
    centers_in_raster_crs = gdf.to_crs(raster_crs)

    records = []
    for (_, row), center in zip(gdf.iterrows(), centers_in_raster_crs.geometry, strict=True):
        polygon = craters._ellipse_polygon(
            center.x, center.y, row["DIAM_ELLI_MAJOR_IMG"], row["DIAM_ELLI_MINOR_IMG"], row["DIAM_ELLI_ANGLE_IMG"]
        )
        if _too_close_to_astropedia_pole(row["LAT_ELLI_IMG"], row["DIAM_ELLI_MAJOR_IMG"]):
            depth_m = None
        else:
            depth_m = crater_depth_m(raster_path, polygon)
        diameter_km = row["DIAM_CIRC_IMG"]
        depth_diameter_ratio = None if depth_m is None else depth_m / (diameter_km * 1000.0)
        records.append(
            {
                "CRATER_ID": row["CRATER_ID"],
                "diameter_km": diameter_km,
                "depth_m": depth_m,
                "depth_diameter_ratio": depth_diameter_ratio,
                "arc_img": row["ARC_IMG"],
                "geometry": polygon,
            }
        )
    return geopandas.GeoDataFrame(records, geometry="geometry", crs=raster_crs)
