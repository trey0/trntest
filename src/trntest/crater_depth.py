"""Crater depth measurement -- the actual input to grading crater sharpness (this project's
Robbins-derived crater layer has no real degradation/freshness field of its own, see
`craters.crater_overlay_layer`'s own docstring on `ARC_IMG`), not a standalone validation exercise.
Adapted from Breton et al. 2019 ("Semi-Automated Crater Depth Measurements", *MethodsX* 6,
2293-2304, DOI `10.1016/j.mex.2019.08.007`), whose method was adopted specifically because it's
already validated in the literature rather than derived from scratch here: depth = the 60th
percentile of elevation in a ring around a crater's rim, minus the 3rd percentile of elevation
inside the crater. The original authors' own reference implementation (`depth_meas.py`, not part of
this repo) operates on a lon/lat raster, so every "inside"/"edge" pixel needs its own real-area
weight (computed via per-pixel OGR polygon intersection) before the interior percentile is
meaningful -- non-square degree-pixels don't all cover the same ground area. This project's own
DEMs are fetched onto a local, **isotropic-meters** Orthographic CRS instead
(`lunaserv.fetch_dem_and_ortho`), where every "inside" pixel already covers the same real area --
that area-weighting machinery is dropped entirely here, not approximated, since it's a provable
no-op on a uniform grid. The interior/edge pixel selection itself is also simplified: real
`shapely` ellipse polygons (`craters._ellipse_polygon`) and `rasterio.features.geometry_mask` stand
in for the original's manual per-pixel circle-distance test and OGR intersection calls.

Deliberately scoped to GLD100 (`lunaserv`'s live default DEM source, ±79 deg latitude coverage) --
`crater_depths_for_footprint` stores a `None` depth for any crater whose own extent could reach
past that latitude, rather than reading from a different, coarser, interpolation-artifact-prone
global DEM just to cover the poles too. See `docs/data-sources.md`'s "Crater depth (Breton et al.
2019 method)" section for the full rationale, including the global-DEM alternatives considered and
set aside."""

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
    """One crater's depth (meters): `rim_percentile` of elevation in a ring just outside
    `crater_polygon`'s rim, minus `floor_percentile` of elevation inside it -- Breton et al. 2019's
    own chosen percentiles (a low-but-not-minimum floor percentile and a high-but-not-maximum rim
    percentile, both robust to a single noisy/misclassified pixel in a way the true min/max
    wouldn't be). `crater_polygon` must already be in `dem_path`'s own CRS (e.g. from
    `craters._ellipse_polygon`, reprojected to the DEM's raster CRS).

    The interior/edge split uses a `pixel_size_m * sqrt(2) / 2` (half a pixel's diagonal) buffer of
    the *real* crater polygon -- the same buffer distance the original paper's own circle-radius
    inside/edge test uses, applied to a real ellipse instead of an equivalent-circle approximation,
    since this project already has the real shape on hand. `floor_percentile` is computed over
    pixels whose *center* falls inside the inward-buffered polygon (`rasterio.features.geometry_mask`
    -- GDAL's own pixel-center-in-polygon test, simpler than the original's real per-pixel area
    intersection, and exact rather than approximate on this project's isotropic grid since every
    such pixel already covers the same real area). `rim_percentile` is computed over pixels whose
    center falls in the ring between the inward- and outward-buffered polygons -- unweighted, same
    as the original (its own rim percentile was never area-weighted to begin with).

    Returns `None` (not 0, not a crash) if either the floor or the rim ring has zero valid (in-DEM,
    non-nodata) pixels -- crater not covered by this DEM tile at all, or small enough relative to
    the DEM's own resolution that no pixel center lands inside one or both regions. A real,
    expected limitation worth surfacing explicitly rather than papering over."""
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


def _too_close_to_astropedia_pole(lat_deg: float, major_km: float) -> bool:
    """`True` if a crater centered at `lat_deg` with ellipse-fit major axis `major_km` could extend
    past GLD100's real `lunaserv.ASTROPEDIA_MAX_ABS_LATITUDE_DEG` coverage limit -- i.e. its own
    angular half-extent (great-circle arc, small enough here that the flat approximation
    `distance / MOON_RADIUS_M` is fine) added to its center latitude would cross that limit. Checked
    directly from catalog fields (no DEM read) so a batch pass over many craters can skip the read
    entirely for excluded ones, not just discover the gap empirically per-crater."""
    half_extent_m = (major_km / 2.0) * 1000.0
    margin_deg = math.degrees(half_extent_m / MOON_RADIUS_M)
    return abs(lat_deg) + margin_deg > lunaserv.ASTROPEDIA_MAX_ABS_LATITUDE_DEG


def crater_depths_for_footprint(
    raster_path,
    config: TrntestConfig | None = None,
    min_major_km: float | None = None,
    min_arc_img: float | None = None,
) -> geopandas.GeoDataFrame:
    """`crater_depth_m` for every Robbins crater covering `raster_path`'s own real footprint (via
    `craters.query_craters_for_raster`, the same AOI-derivation `crater_overlay_layer` uses) --
    `min_major_km`/`min_arc_img` are the same filters `crater_overlay_layer` offers, same rationale
    (see its own docstring).

    Returns a `geopandas.GeoDataFrame` with one row per surviving crater: `CRATER_ID`,
    `diameter_km` (`DIAM_CIRC_IMG` -- this database's own circle-fit diameter, independent of the
    ellipse-fit axes used for the depth geometry itself), `depth_m`, `depth_diameter_ratio`,
    `arc_img`, and `geometry` (the crater's real ellipse polygon, in `raster_path`'s own CRS, same
    as `crater_overlay_layer`'s). `depth_m`/`depth_diameter_ratio` are `None` for any crater too
    close to GLD100's own ±79 deg coverage limit (`_too_close_to_astropedia_pole`, checked before
    ever calling `crater_depth_m` -- no DEM read for these) or for any `crater_depth_m` miss (e.g. a
    real nodata gap) -- **kept as rows, not dropped**, so a caller can see how many/which craters
    were excluded and why, rather than that information silently disappearing."""
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
