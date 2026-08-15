"""Robbins (2019) lunar crater database: fetch/cache (`cache.fetch_robbins_craters`), one-time
conversion to a spatially-indexed GeoPackage for fast per-camera AOI queries, and ellipse-polygon
construction for `plotting.OverlayLayer`. See `docs/data-sources.md`'s "Robbins lunar crater
database" section for the source URL/format/units/schema facts this module encodes.
"""

import zipfile
from pathlib import Path

import geopandas
import pandas as pd
import rasterio
import rasterio.warp
import shapely.affinity
import shapely.geometry

from trntest import cache, lunaserv, plotting
from trntest.config import TrntestConfig, load_config

# The database's own declared CRS (see docs/data-sources.md): Planetocentric latitude, Positive-East
# longitude, on a perfect sphere -- `config.moon_radius_m` already matches this exactly (1,737,400m),
# same convention `lunaserv.py` uses throughout.
_MOON_GEOGRAPHIC_CRS_TEMPLATE = "+proj=longlat +R={radius_m} +no_defs"

# A generous query-bbox pad (fraction of the AOI's own size) so an ellipse whose center sits just
# outside the exact AOI, but whose extent still overlaps it, isn't wrongly dropped -- this module
# indexes/filters by each crater's own center *point* (see `_convert_to_geopackage`), not its real
# ellipse extent, so a bare exact-AOI query could clip a partially-visible crater at the edge.
# Cheaper than storing/reprojecting real ellipse polygons for the *whole* 1.3M-row database just to
# get exact-extent filtering, given how small these ellipses are relative to a single camera's
# tens-of-km AOI -- the same "pad generously rather than filter exactly" tradeoff
# `lunaserv.fetch_dem_and_ortho`'s own `dem_padding_fraction` already makes.
_QUERY_BBOX_PADDING_FRACTION = 0.05


def _csv_member_name(zf: zipfile.ZipFile) -> str:
    """The bundle's one real crater-data file. A plain `*.csv` glob isn't enough --
    `collection_data_inventory.csv` is bundle metadata, not crater rows -- so this also requires
    `/data/` in the path and excludes anything with "inventory" in the name; exactly one real file
    in the actual download matches both (see `docs/data-sources.md`)."""
    candidates = [n for n in zf.namelist() if n.endswith(".csv") and "/data/" in n and "inventory" not in n]
    if len(candidates) != 1:
        raise ValueError(f"expected exactly one crater-data CSV in the Robbins zip, found: {candidates}")
    return candidates[0]


def geopackage_path(zip_path: Path) -> Path:
    """The converted GeoPackage's path -- alongside the raw zip, same basename with `.gpkg`."""
    return zip_path.with_suffix(".gpkg")


def _convert_to_geopackage(zip_path: Path, gpkg_path: Path, moon_radius_m: float) -> None:
    """One-time conversion (see `ensure_geopackage`): reads the real CSV directly out of the zip (no
    unzip-to-disk step needed), builds Point geometry from each crater's own ellipse-fit center
    (`LAT_ELLI_IMG`/`LON_ELLI_IMG` -- the same center `crater_overlay_layer`'s ellipse construction
    itself uses, not the separate circle-fit center, so indexing and drawing stay consistent), and
    writes a GeoPackage. GDAL builds a real spatial index (`rtree`) for GeoPackage output by
    default -- that index is what makes `query_craters_in_bbox`'s `geopandas.read_file(...,
    bbox=...)` a fast index seek instead of a full-file scan; the raw CSV has no spatial index at
    all to begin with, so this step is the one that actually matters at ~1.3M rows.

    38 of 1,296,796 rows have NaN ellipse-fit columns (confirmed empirically against the real file,
    not assumed) -- dropped, since a crater with no fitted ellipse can't be drawn as one anyway."""
    with zipfile.ZipFile(zip_path) as zf, zf.open(_csv_member_name(zf)) as f:
        df = pd.read_csv(f)
    ellipse_cols = ["LAT_ELLI_IMG", "LON_ELLI_IMG", "DIAM_ELLI_MAJOR_IMG", "DIAM_ELLI_MINOR_IMG", "DIAM_ELLI_ANGLE_IMG"]
    df = df.dropna(subset=ellipse_cols)
    geometry = geopandas.points_from_xy(df["LON_ELLI_IMG"], df["LAT_ELLI_IMG"])
    crs = _MOON_GEOGRAPHIC_CRS_TEMPLATE.format(radius_m=moon_radius_m)
    gdf = geopandas.GeoDataFrame(df, geometry=geometry, crs=crs)
    gpkg_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(gpkg_path, driver="GPKG")


def ensure_geopackage(config: TrntestConfig | None = None) -> Path:
    """Downloads the raw zip (`cache.fetch_robbins_craters`, itself cached) and converts it to a
    spatially-indexed GeoPackage once, also cached alongside the zip -- idempotent, like every other
    fetch helper in this project (`gpkg_path.exists()` short-circuits the real conversion cost)."""
    config = config or load_config()
    zip_path = cache.fetch_robbins_craters(config.cache_root, config.robbins_craters_url)
    gpkg_path = geopackage_path(zip_path)
    if not gpkg_path.exists():
        _convert_to_geopackage(zip_path, gpkg_path, config.moon_radius_m)
    return gpkg_path


def query_craters_in_bbox(bbox_lonlat_deg: tuple, config: TrntestConfig | None = None) -> geopandas.GeoDataFrame:
    """`bbox_lonlat_deg`: `(minlon, minlat, maxlon, maxlat)` degrees -- pad before calling if padding
    is wanted (see `crater_overlay_layer`'s own `_QUERY_BBOX_PADDING_FRACTION`). Two-stage filter
    (see `docs/plan.md`'s open items for why this matters at ~1.3M rows): `bbox=` pushes down to
    GDAL's own spatial index (`ensure_geopackage`'s GeoPackage `rtree`) so the full database is
    never materialized in Python, then `.cx[]` (geopandas' own index-backed accessor) trims to the
    exact box -- `bbox=`'s own filtering is a fast prefilter, not guaranteed pixel-exact depending on
    driver internals, so both stages matter."""
    config = config or load_config()
    gpkg_path = ensure_geopackage(config)
    gdf = geopandas.read_file(gpkg_path, bbox=bbox_lonlat_deg)
    minlon, minlat, maxlon, maxlat = bbox_lonlat_deg
    return gdf.cx[minlon:maxlon, minlat:maxlat]


def _ellipse_polygon(
    center_x_m: float, center_y_m: float, major_km: float, minor_km: float, angle_deg: float
) -> shapely.geometry.Polygon:
    """A `shapely.geometry.Polygon` ellipse in local isotropic-meters CRS -- exact, no projection
    distortion, since the reprojection into this CRS already happened on the *center point* before
    this is called (see `crater_overlay_layer`), not on a pre-built ellipse shape. `major_km`/
    `minor_km` are full axis lengths (`DIAM_ELLI_*_IMG`'s own "diameter" naming, matching
    `DIAM_CIRC_IMG`'s convention -- confirmed empirically, see `docs/data-sources.md`), hence the
    `/2` down to semi-axes below.

    `angle_deg`'s rotation reference (measured from which axis, which direction) isn't documented in
    the PDS4 label and hasn't yet been visually cross-checked against a real crater rim in the
    hillshade basemap (the same kind of visual QA this notebook already does for other geometry,
    e.g. Phase 5/6's tie points) -- treat ellipse *size*/*position* as confirmed, *orientation* as
    unverified until checked against a real render."""
    unit_circle = shapely.geometry.Point(0, 0).buffer(1.0, quad_segs=32)
    semi_major_m, semi_minor_m = major_km * 500.0, minor_km * 500.0  # km -> m, then diameter -> radius
    ellipse = shapely.affinity.scale(unit_circle, semi_major_m, semi_minor_m)
    ellipse = shapely.affinity.rotate(ellipse, angle_deg, origin=(0, 0))
    return shapely.affinity.translate(ellipse, center_x_m, center_y_m)


def crater_overlay_layer(
    raster_path, config: TrntestConfig | None = None, **layer_kwargs
) -> plotting.OverlayLayer | None:
    """The single entry point notebook cells call: builds a `plotting.OverlayLayer` of Robbins
    crater ellipses covering `raster_path`'s own real footprint, in `raster_path`'s own CRS -- ready
    to pass straight into `plotting.plot_overlay`/`plot_overlay_toggle`'s `layers=[...]`. Returns
    `None` if no craters fall in view (e.g. a very small or unlucky footprint) rather than an empty
    layer, so callers should build their `layers` list conditionally
    (`layers=[l for l in (craters.crater_overlay_layer(...),) if l is not None]` or similar).

    `raster_path` is read purely for its own real georeferencing (`rasterio`'s `crs`/`bounds`) -- the
    same "raster's own georeferencing is authoritative" pattern
    `isis_wac._orthographic_map_pvl`/`lunaserv.result_from_files` already rely on elsewhere, not a
    new convention. The query AOI is derived directly from that raster's real bounds
    (`rasterio.warp.transform_bounds` into the crater database's own geographic CRS), the same
    "derive the padded bbox from the destination grid's own real extent, not an independently
    computed one" approach `lunaserv.astropedia_coverage_bbox_deg` already uses (and documents why:
    two independently-padded bboxes aren't guaranteed to agree)."""
    config = config or load_config()
    with rasterio.open(raster_path) as src:
        raster_crs, raster_bounds = src.crs, src.bounds

    geo_crs = _MOON_GEOGRAPHIC_CRS_TEMPLATE.format(radius_m=config.moon_radius_m)
    minlon, minlat, maxlon, maxlat = rasterio.warp.transform_bounds(raster_crs, geo_crs, *raster_bounds)
    # `transform_bounds` returns longitude in the standard -180..180 convention regardless of the
    # destination CRS's own definition -- confirmed empirically (a real AOI centered at 264.757 deg
    # East came back as -95.243) -- but the database's LON_ELLI_IMG/LON_CIRC_IMG columns (and this
    # project's own lon/lat convention throughout lunaserv.py) are 0-360 deg Positive-East. Not
    # normalized here: an AOI that straddles the 0/360 meridian would still come out with minlon >
    # maxlon after this -- a known, unhandled edge case (no camera footprint used by this project
    # currently lands there; see docs/plan.md's open items if this ever needs revisiting).
    bbox_deg = (minlon % 360.0, minlat, maxlon % 360.0, maxlat)
    padded_bbox_deg = lunaserv.pad_bbox(bbox_deg, _QUERY_BBOX_PADDING_FRACTION)

    gdf = query_craters_in_bbox(padded_bbox_deg, config)
    if gdf.empty:
        return None

    centers_in_raster_crs = gdf.to_crs(raster_crs)
    polygons = [
        _ellipse_polygon(pt.x, pt.y, major_km, minor_km, angle_deg)
        for pt, major_km, minor_km, angle_deg in zip(
            centers_in_raster_crs.geometry,
            gdf["DIAM_ELLI_MAJOR_IMG"],
            gdf["DIAM_ELLI_MINOR_IMG"],
            gdf["DIAM_ELLI_ANGLE_IMG"],
            strict=True,
        )
    ]
    ellipses = geopandas.GeoSeries(polygons, crs=raster_crs)
    return plotting.OverlayLayer(geoseries=ellipses, **layer_kwargs)
