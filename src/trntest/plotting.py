"""Matplotlib display helpers for the notebook. No SPICE/network/subprocess calls -- pure
consumption of already-computed values, reading image files by path where needed."""

import base64
import io
import warnings

import geopandas
import IPython.display
import matplotlib.pyplot as plt
import matplotlib.ticker
import numpy as np
import rasterio
import rasterio.errors
import rasterio.features
import rasterio.fill
import rasterio.transform
import rasterio.windows
import rioxarray
import shapely.geometry
import shapely.ops
import xarray
from PIL import Image

from trntest import lunaserv, orientation, wac
from trntest.camera import Camera
from trntest.lunaserv import DemOrthoResult
from trntest.orientation import DisplayRotations

# Visually-distinct, high-contrast marker per die-5 tie-point position, shared between the two
# comparison panels.
MARKER_STYLES = {
    "top_left": dict(marker="o", color="red"),
    "top_right": dict(marker="s", color="cyan"),
    "center": dict(marker="^", color="yellow"),
    "bottom_left": dict(marker="D", color="magenta"),
    "bottom_right": dict(marker="*", color="lime"),
}


# ISIS special pixels (NULL/LRS/LIS/HIS/HRS) are finite but huge-magnitude (~+-3.4e38) float32
# sentinels -- `np.isfinite` doesn't catch them. Generic threshold rather than the exact 5 bit
# patterns, since other fill-value conventions (e.g. `wac.MISSING_CONSTANT`) are similarly
# huge-magnitude, not just ISIS's.
_FILL_VALUE_MAGNITUDE_THRESHOLD = 1e37


def valid_pixel_mask(data: np.ndarray) -> np.ndarray:
    """True where `data` is finite and not a huge-magnitude fill-value sentinel."""
    return np.isfinite(data) & (np.abs(data) < _FILL_VALUE_MAGNITUDE_THRESHOLD)


def read_raster_band(path, band: int = 1, window: rasterio.windows.Window | None = None) -> np.ndarray:
    """Read one band of any raster GDAL can open by path (GeoTIFF, ISIS `.cub`, ...), optionally
    windowed to a crop. Shared by `plot_raster` and any notebook code that needs the raw array
    directly (e.g. picking a crop window from real data), so both get the same warning suppression.

    Two non-actionable, rasterio-internal warnings suppressed narrowly here (not module-wide):
    `NotGeoreferencedWarning` is expected for ISIS `.cub`s at this pipeline stage (no geotransform
    yet, not a bug), and the numpy-shape `DeprecationWarning` is from inside rasterio's own code."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        with rasterio.open(path) as src:
            return src.read(band, window=window)


def plot_raster(
    path,
    band: int = 1,
    window: rasterio.windows.Window | None = None,
    cmap: str = "gray",
    stretch: bool = True,
):
    """Display one band of any raster GDAL can open by path (GeoTIFF, ISIS `.cub`, ...), optionally
    windowed to a crop. Generic on purpose -- not ISIS-specific -- so it's reusable wherever a
    notebook just needs to look at a raster file.

    `stretch=True` (default) contrast-stretches to the data's own 2nd/98th percentile, excluding
    both non-finite pixels and huge-magnitude fill-value sentinels (ISIS's NULL/LOW/HIGH special
    pixels are finite but ~+-3.4e38, similar in spirit to `wac.MISSING_CONSTANT` -- `np.isfinite`
    alone doesn't catch them) -- calibrated I/F values are small floats near zero, so leaving them
    in wrecks the stretch (and, upstream, any `mean`/`sum` reduction can silently overflow)."""
    data = read_raster_band(path, band=band, window=window)

    valid = valid_pixel_mask(data)
    # fill-value sentinels would otherwise overflow imshow's own float32 normalization
    display_data = np.where(valid, data, np.nan)

    vmin = vmax = None
    if stretch:
        finite = data[valid]
        if finite.size:
            vmin, vmax = np.percentile(finite, [2, 98])

    fig, ax = plt.subplots(figsize=(6, 6))
    ax.imshow(display_data, cmap=cmap, vmin=vmin, vmax=vmax)
    ax.set_title(str(path))
    ax.set_xlabel("sample")
    ax.set_ylabel("line")
    fig.tight_layout()
    return fig


def plot_dem_ortho(dem_ortho_result: DemOrthoResult, camera: Camera):
    """Left: the Lunaserv ortho mosaic with the SPICE-derived camera's ground footprint (the 4
    corner rays' Moon intersections, connected into a closed quad, plus a center marker) overlaid,
    to visually confirm the pose lands where expected. Right: the GLD100 DEM (elevation). Both share
    one figure (previously two separate plots/cells -- merged since the right panel's DEM tile is
    exactly the left panel's own AOI, no information lost by combining them) and the same km-scaled
    Easting/Northing axes.

    The footprint overlay reprojects `camera.footprint_lonlat_deg` (plain geographic lon/lat) into
    the ortho's own local Orthographic CRS via `geopandas`/`pyproj` (one `.to_crs()` call), then
    plots both in real georeferenced coordinates -- not a previous version's manual
    `rasterio.transform.rowcol(ortho_transform, lon, lat)`, which passed raw lon/lat *degrees*
    straight in as if they were already the ortho's own local-CRS *meters*, collapsing every point
    near the map's center once the ortho switched from a native lon/lat grid to a local per-camera
    CRS (see docs/history.md's dated entry)."""
    with rasterio.open(dem_ortho_result.ortho) as src:
        ortho = src.read(1)
        ortho_crs = src.crs
        ortho_bounds = src.bounds
    with rasterio.open(dem_ortho_result.dem) as src:
        dem = src.read(1)
        dem_bounds = src.bounds

    moon_geographic_crs = "+proj=longlat +R=1737400 +no_defs"
    min_polygon_points = 3
    corners = [camera.footprint_lonlat_deg[name] for name in ("top_left", "top_right", "bottom_right", "bottom_left")]
    corners = [c for c in corners if c is not None]

    fig, axes = plt.subplots(1, 2, figsize=(11, 5))
    axes[0].imshow(
        ortho, cmap="gray", extent=(ortho_bounds.left, ortho_bounds.right, ortho_bounds.bottom, ortho_bounds.top)
    )
    if len(corners) >= min_polygon_points:
        footprint = geopandas.GeoSeries([shapely.geometry.Polygon(corners)], crs=moon_geographic_crs)
        footprint.to_crs(ortho_crs).boundary.plot(ax=axes[0], color="yellow", linewidth=1.5)
    center_lonlat = camera.footprint_lonlat_deg["center"]
    if center_lonlat is not None:
        center = geopandas.GeoSeries([shapely.geometry.Point(center_lonlat)], crs=moon_geographic_crs)
        center.to_crs(ortho_crs).plot(ax=axes[0], color="red", markersize=30)
    axes[0].set_title("Lunaserv WAC global mosaic (ROI) with camera footprint")

    im = axes[1].imshow(
        dem, cmap="terrain", extent=(dem_bounds.left, dem_bounds.right, dem_bounds.bottom, dem_bounds.top)
    )
    axes[1].set_title("GLD100 DEM (elevation, m)")
    fig.colorbar(im, ax=axes[1], shrink=0.8)

    km_formatter = matplotlib.ticker.FuncFormatter(lambda x, _: f"{x / 1000:.0f}")
    for ax in axes:
        ax.xaxis.set_major_formatter(km_formatter)
        ax.yaxis.set_major_formatter(km_formatter)
        ax.set_xlabel("Easting (km)")
        ax.set_ylabel("Northing (km)")
    fig.tight_layout()
    return fig


def plot_synthetic_render(rendered_tif_path):
    synthetic = read_raster_band(rendered_tif_path)

    fig = plt.figure(figsize=(5, 5))
    plt.imshow(synthetic, cmap="gray")
    plt.title("Synthetic sat_sim render")
    plt.xlabel("sample")
    plt.ylabel("line")
    return fig


def _plot_tie_point_marker(ax, name, px, py, k_rotation, height, width, width_km, height_km):
    """Rotate (for north-up display) + scale to real km + plot one tie-point marker. Shared by
    `plot_comparison` and `plot_isis_comparison` -- same math either way, just different
    height/width (px and km) per panel."""
    style = MARKER_STYLES[name]
    px_r, py_r = orientation.rotate_pixel_coords(px, py, k_rotation, height, width)
    ax.plot(
        px_r / width * width_km,
        py_r / height * height_km,
        markersize=14,
        markeredgecolor="black",
        markeredgewidth=1.5,
        **style,
    )


def plot_comparison(
    camera: Camera,
    tie_point_results: dict,
    vis_mosaic: np.ndarray,
    rotations: DisplayRotations,
    rendered_tif_path,
):
    synthetic = read_raster_band(rendered_tif_path)

    valid_mask = vis_mosaic != wac.MISSING_CONSTANT
    # Match the real WAC crop's overall brightness to the synthetic render via a single
    # multiplicative scale at the median -- not an affine/percentile stretch, which would remap the
    # darkest/brightest values and stop reflecting the pipeline's actual relative brightness. Both
    # panels are then displayed on the same fixed 0-255 scale (imshow's default auto-normalize to
    # each panel's own min/max is itself an implicit stretch -- silent, but real -- which is what
    # made the two panels' brightness levels incomparable before).
    scale = np.median(synthetic[valid_pixel_mask(synthetic)]) / np.median(vis_mosaic[valid_mask])
    # Scale only the valid pixels -- vis_mosaic's invalid entries hold wac.MISSING_CONSTANT (a
    # huge-magnitude float32 sentinel, see wac.py), which overflows float32 if multiplied by scale.
    scaled_valid = vis_mosaic[valid_mask] * scale
    low_fill = np.percentile(scaled_valid, 2)
    display_mosaic = np.full_like(vis_mosaic, low_fill)  # fill missing edge columns with the low end
    display_mosaic[valid_mask] = scaled_valid

    # Both panels cover the same real square ground area (see docs/data-sources.md, "Current
    # image-pipeline algorithm"), but at different native pixel resolution per axis -- plot in real
    # km (not raw pixel index) so both display as square and are directly, visually comparable.
    # Also apply the north-up rotation computed above
    # (display only).
    n_frames = camera.n_frames_for_square_crop
    synthetic_width_km = camera.cross_track_width_km
    crop_width_km = camera.cross_track_width_km
    crop_height_km = n_frames * camera.km_per_frame

    synthetic_rot = np.rot90(synthetic, rotations.k_synthetic)
    mosaic_rot = np.rot90(display_mosaic, rotations.k_crop)
    h_syn, w_syn = synthetic.shape
    h_crop, w_crop = display_mosaic.shape

    fig, axes = plt.subplots(1, 2, figsize=(11, 6))
    axes[0].imshow(synthetic_rot, cmap="gray", vmin=0, vmax=255, extent=[0, synthetic_width_km, synthetic_width_km, 0])
    axes[0].set_title("Synthetic (sat_sim, SPICE-posed, north-up)")
    axes[1].imshow(mosaic_rot, cmap="gray", vmin=0, vmax=255, extent=[0, crop_width_km, crop_height_km, 0])
    axes[1].set_title("Real WAC CDR, band-separated (brightness-matched to synthetic, north-up)")
    for ax in axes:
        ax.set_xlabel("km")
        ax.set_ylabel("km")

    for name, r in tie_point_results.items():
        px, py = r["synthetic_px"]
        _plot_tie_point_marker(
            axes[0], name, px, py, rotations.k_synthetic, h_syn, w_syn, synthetic_width_km, synthetic_width_km
        )
        col, row = r["crop_px"]
        _plot_tie_point_marker(axes[1], name, col, row, rotations.k_crop, h_crop, w_crop, crop_width_km, crop_height_km)

    fig.tight_layout()
    return fig


def _fill_dead_columns_for_display(band: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Row-wise linear interpolation across invalid (no-data) pixels, for display only -- doesn't
    touch the real calibrated data used anywhere else. Unlike `lunaserv.despeckle()` (a randomly-
    scattered-outlier filter over otherwise-present values), ISIS's `lrowaccal` "SpecialPixels"
    correction marks genuinely *missing* pixels at a small, fixed, deterministic set of detector
    columns on each VIS framelet's first line (confirmed empirically: the exact same 56 columns
    recur, unchanged, at every 14-line framelet boundary across a full cube -- see
    docs/data-sources.md's ISIS3/CSM spike section) -- narrow (1-3 columns), within otherwise real,
    locally-smooth rows, so a simple per-row linear fill across each gap is a reasonable, standard
    dead-pixel-column interpolation. `np.interp` also handles the edge case (`column 0` has no left
    neighbor -- always dead, see the same docs section) by clamping to the nearest valid value
    rather than extrapolating."""
    filled = band.copy()
    cols = np.arange(band.shape[1])
    for row in range(band.shape[0]):
        row_valid = valid[row]
        if row_valid.all():
            continue
        if not row_valid.any():
            # no valid pixel anywhere in this row to interpolate from -- fall back to NaN
            # (transparent), same as the pre-fill behavior, rather than leaving raw sentinel values
            # unmasked.
            filled[row] = np.nan
            continue
        filled[row, ~row_valid] = np.interp(cols[~row_valid], cols[row_valid], band[row, row_valid])
    return filled


def plot_isis_comparison(
    camera: Camera,
    tie_point_results: dict,
    rendered_tif_path,
    stitched_cub_path,
    rotations: DisplayRotations,
    window: rasterio.windows.Window | None = None,
):
    """Synthetic render next to a same-real-footprint crop of the ISIS-processed WAC image
    (`isis_wac.crop_for_camera`) -- an ad hoc real-km/north-up comparison, tie-pointed the same way
    `plot_comparison`'s wac.py version is (see below), not true pixel-for-pixel geo-registration;
    for that, see `plot_overlay`'s `cam2map`-based overlay of this same ISIS-processed cube
    (`isis_wac.run_cam2map_for_crop`) instead. `window` is optional -- `stitched_cub_path` is typically
    already `isis_wac.crop_for_camera`'s real, standalone crop cube (no further windowing needed);
    pass a `rasterio.windows.Window` only if handed the full, uncropped stitched cube instead.

    Applies the same north-up display rotation and real-km extent scaling `plot_comparison` already
    uses, for the same two reasons: the sensor's fixed pixel-axis convention needs a pass-dependent
    rotation to display north-up, and WAC's along-track/cross-track pixel GSDs differ (the crop's
    along-track axis is oversampled relative to cross-track -- see `crop_window_for_camera`), so a
    plain 1:1 pixel `imshow` visibly stretches/compresses it. `rotations.k_crop` -- computed
    purely from real SPICE geometry (`camera`/`frame_timing`), never from `wac.py`'s own pixel
    array -- applies equally well here: the ISIS cube shares `wac.py`'s exact line/sample
    convention (confirmed in `crop_window_for_camera`'s docstring), and both `wac.py`'s stacking
    order and `isis_wac.run_pipeline`'s `framestitch` FLIP are driven by the same
    `camera.reverse_crop_along_track` signal `k_crop` itself depends on.

    `tie_point_results` (from `session.select_tie_points` + `tie_points.resolve_crop_pixels`,
    already computed by Phase 6) are reused as-is, not recomputed -- `tie_points.py`'s "crop_px" is
    a real ISIS `campt` ground-to-image query against `stitched_cub_path` itself (see that module's
    docstring), so its row/col origin already matches this exact cube with no transformation needed.

    A tie point the real camera doesn't see (see `tie_points.resolve_crop_pixels`'s docstring) is
    simply absent from `tie_point_results` -- this function draws whatever's present, no special
    handling needed for a missing point.

    Brightness-matches the real panel to the synthetic one via the same single-multiplicative-
    median-scale technique `plot_comparison` uses (see its docstring for the full rationale) -- not
    an affine/percentile stretch, which would independently renormalize each panel's own contrast
    and hide any real relative-brightness difference between them. Necessary here for the same
    reason: the ISIS cube (`real`, calibrated I/F, ~0.01-0.2) and the synthetic render (`synthetic`,
    a rendered-texture brightness value, ~0-255) are on entirely different numeric scales, not just
    different units of the same thing."""
    synthetic = read_raster_band(rendered_tif_path)
    real = read_raster_band(stitched_cub_path, window=window)
    valid = valid_pixel_mask(real)
    real_filled = _fill_dead_columns_for_display(real, valid) if valid.any() else real
    # Median computed over the real, un-filled valid pixels only (matching plot_comparison's
    # technique exactly) -- the fill above is a display convenience for isolated dead columns
    # (see _fill_dead_columns_for_display's docstring), not something that should influence the
    # brightness match.
    scale = np.median(synthetic[valid_pixel_mask(synthetic)]) / np.median(real[valid]) if valid.any() else 1.0
    real_display = real_filled * scale

    h_syn, w_syn = synthetic.shape
    h_crop, w_crop = real.shape
    synthetic_rot = np.rot90(synthetic, rotations.k_synthetic)
    real_rot = np.rot90(real_display, rotations.k_crop)

    synthetic_width_km = camera.cross_track_width_km
    crop_width_km = camera.cross_track_width_km
    crop_height_km = camera.n_frames_for_square_crop * camera.km_per_frame

    fig, axes = plt.subplots(1, 2, figsize=(11, 6))
    axes[0].imshow(synthetic_rot, cmap="gray", vmin=0, vmax=255, extent=[0, synthetic_width_km, synthetic_width_km, 0])
    axes[0].set_title("Synthetic (sat_sim, SPICE-posed, north-up)")
    axes[1].imshow(real_rot, cmap="gray", vmin=0, vmax=255, extent=[0, crop_width_km, crop_height_km, 0])
    axes[1].set_title("Real WAC (ISIS-processed, brightness-matched to synthetic, north-up)")
    for ax in axes:
        ax.set_xlabel("km")
        ax.set_ylabel("km")

    for name, r in tie_point_results.items():
        px, py = r["synthetic_px"]
        _plot_tie_point_marker(
            axes[0], name, px, py, rotations.k_synthetic, h_syn, w_syn, synthetic_width_km, synthetic_width_km
        )
        col, row = r["crop_px"]
        _plot_tie_point_marker(axes[1], name, col, row, rotations.k_crop, h_crop, w_crop, crop_width_km, crop_height_km)

    fig.tight_layout()
    return fig


def plot_render_vs_basemap(
    render_array: np.ndarray,
    rotation_k: int,
    render_width_km: float,
    render_height_km: float,
    footprint_lonlat_deg: dict,
    base_raster_path,
    title: str,
    render_label: str,
    tie_point_results: dict | None = None,
    render_px_key: str = "synthetic_px",
):
    """Raw, north-up-rotated, real-km-scaled side-by-side of a render's own unprojected pixels
    (`render_array` -- genuine sensor/render image quality, not a resampled reprojection) against a
    plain pixel crop of the hillshade basemap (`base_raster_path`, e.g. `DemOrthoResult.ortho`)
    covering the same real ground footprint. This is the "A"-style geometry check: a quick ad hoc
    quality/rough-alignment look -- for true pixel-for-pixel geo-registration against the same
    basemap, see `plot_overlay`'s "B"-style `mapproject`-based overlay instead.

    `render_array` is passed through `_fill_dead_columns_for_display` before display, same as
    `plot_isis_comparison`'s real panel -- a no-op for the synthetic render (no dead pixels to begin
    with), but necessary for the real WAC crop: without it, the ~1% framelet-boundary dead-pixel
    pattern (see docs/data-sources.md's ISIS3/CSM spike section) shows up as visible speckle.

    `footprint_lonlat_deg` is the render's own real ground footprint (corners + center, matching
    `Camera.footprint_lonlat_deg`'s shape) -- `Camera.footprint_lonlat_deg` itself for the synthetic
    render, `tie_points.crop_footprint_corners()` for the real WAC crop (its own independently
    ray-traced footprint, not assumed identical to the synthetic camera's). `lunaserv.footprint_bbox_local_m`
    (already used to size the original WMS fetch -- see its docstring) converts the corners to the
    basemap's own local Orthographic CRS (centered on this same footprint's own center, see
    `lunaserv.fetch_dem_and_ortho`) to find the matching pixel window -- a plain windowed read, no
    resampling. Unlike the render (fixed sensor-pixel axes, needing a pass-dependent rotation for
    north-up display), the basemap crop needs no rotation: the local Orthographic CRS is already
    north-referenced by construction (+Y = north).

    `tie_point_results` (from `session.select_tie_points` + `tie_points.resolve_crop_pixels` for the
    real crop's `"crop_px"`, see `plot_comparison`/
    `plot_isis_comparison`'s identical dict shape) marks the same 5 ground points on both panels, if
    given. On the render panel, `render_px_key` selects which of each point's two pre-computed pixel
    coordinates applies (`"synthetic_px"` or `"crop_px"`) -- same technique `plot_comparison`/
    `plot_isis_comparison` already use. On the basemap panel, each point's real `"lonlat"` is
    projected directly into the crop's own local-CRS offset (`lunaserv.orthographic_xy_m`, same
    center as the crop itself) -- no pixel coordinates needed there, since that panel is a plain,
    unrotated crop of an already-georeferenced raster."""
    center_lon, center_lat = footprint_lonlat_deg["center"]
    minx, miny, maxx, maxy = lunaserv.footprint_bbox_local_m(footprint_lonlat_deg, center_lon, center_lat)

    with rasterio.open(base_raster_path) as src:
        window = rasterio.windows.from_bounds(minx, miny, maxx, maxy, transform=src.transform)
        base_crop = src.read(1, window=window)

    base_width_km = (maxx - minx) / 1000.0
    base_height_km = (maxy - miny) / 1000.0

    valid = valid_pixel_mask(render_array)
    render_height, render_width = render_array.shape
    render_filled = _fill_dead_columns_for_display(render_array, valid) if valid.any() else render_array
    render_rot = np.rot90(render_filled, rotation_k)

    # Linear stretch through 0 (vmin=0), not an affine min-max stretch -- an affine stretch
    # (vmin=some low percentile) shifts the black point up, which clips genuinely dark-but-real
    # terrain to pure black. Only vmax is derived from the data, from the 99.9th percentile (not a
    # naive max) so a handful of extreme outlier pixels (e.g. a real saturated-crater highlight)
    # don't pull it out far enough to make the bulk of genuine terrain look uniformly dark. Each
    # panel stretched independently (unlike plot_isis_comparison's cross-panel brightness match),
    # since this function doesn't assume the two panels share comparable units to begin with.
    render_vmin, render_vmax = 0, np.nanpercentile(render_rot, 99.9)
    base_vmin, base_vmax = 0, np.nanpercentile(base_crop, 99.9)

    fig, axes = plt.subplots(1, 2, figsize=(11, 6))
    axes[0].imshow(
        render_rot, cmap="gray", vmin=render_vmin, vmax=render_vmax, extent=[0, render_width_km, render_height_km, 0]
    )
    axes[0].set_title(f"{render_label} (north-up)")
    axes[1].imshow(base_crop, cmap="gray", vmin=base_vmin, vmax=base_vmax, extent=[0, base_width_km, base_height_km, 0])
    axes[1].set_title("Hillshade-based basemap (same real footprint)")
    for ax in axes:
        ax.set_xlabel("km")
        ax.set_ylabel("km")

    if tie_point_results:
        for name, r in tie_point_results.items():
            px, py = r[render_px_key]
            _plot_tie_point_marker(
                axes[0], name, px, py, rotation_k, render_height, render_width, render_width_km, render_height_km
            )
            lon, lat = r["lonlat"]
            x, y = lunaserv.orthographic_xy_m(lon, lat, center_lon, center_lat)
            axes[1].plot(
                (x - minx) / 1000.0,
                (maxy - y) / 1000.0,
                markersize=14,
                markeredgecolor="black",
                markeredgewidth=1.5,
                **MARKER_STYLES[name],
            )

    fig.suptitle(title)
    fig.tight_layout()
    return fig


def _open_raster_dataarray(path):
    """`rioxarray.open_rasterio` is typed to return a `Dataset`/`list[Dataset]` for some inputs
    (e.g. multi-file), but a single-band single-file GeoTIFF (this project's only use so far) always
    yields a `DataArray` -- assert that so mypy can narrow it, rather than a `# type: ignore`.
    `masked=True` converts nodata to real NaN based on the file's own embedded `nodata` tag --
    necessary because `mapproject`'s nodata convention depends on its *input* format: a synthetic
    render (plain GeoTIFF source) comes out as real NaN already, but an ISIS `.cub` source (e.g. the
    real-WAC overlay) carries ISIS's own huge-magnitude NULL sentinel (~-3.4e38) straight through
    into the output, with a `nodata` tag set to match -- confirmed empirically: without `masked=True`
    here, that sentinel dominates `plot.imshow`'s automatic vmin/vmax and washes the real 0.01-0.13
    I/F signal out to a uniform flat gray."""
    opened = rioxarray.open_rasterio(path, masked=True)
    assert isinstance(opened, xarray.DataArray)
    return opened.squeeze()


def _valid_data_outline(raster_da):
    """The real-image (non-NaN) footprint of `raster_da` as a single Shapely geometry in the
    raster's own real (already-georeferenced) coordinates -- e.g. `run_mapproject`'s output is
    NaN outside the actual reprojected camera footprint (see docs/data-sources.md), so this traces
    that footprint's true outline rather than the raster's full (padded, mostly-nodata) pixel grid.
    Interior holes (isolated nodata pixels from real DEM ray-intersection speckle -- see
    `render.DEM_HEIGHT_ERROR_TOL_M`'s docstring) are dropped: they're display noise, not meaningful
    "outline" content."""
    mask = ~np.isnan(raster_da.values)
    polygons = [
        shapely.geometry.shape(geom)
        for geom, value in rasterio.features.shapes(
            mask.astype(np.uint8), mask=mask, transform=raster_da.rio.transform()
        )
        if value == 1
    ]
    merged = shapely.ops.unary_union(polygons)
    parts = merged.geoms if isinstance(merged, shapely.geometry.MultiPolygon) else [merged]
    return shapely.geometry.MultiPolygon([shapely.geometry.Polygon(p.exterior) for p in parts])


def _fill_overlay_nodata_for_display(overlay_da, max_search_distance: int = 10):
    """Fill small nodata gaps in `overlay_da` for display, via GDAL's inverse-distance-weighted
    `rasterio.fill.fillnodata` -- orientation-agnostic (unlike `_fill_dead_columns_for_display`'s
    row-wise interpolation, which only helps gaps that are narrow *within a row*), needed here
    because a `mapproject` output's real-but-sparse defects (e.g. the same framelet-boundary
    dead-detector-columns `_fill_dead_columns_for_display` handles pre-reprojection -- see its
    docstring) trace diagonal "dash" streaks once reprojected into map space, following the
    sensor's ground track rather than the image's row/column axes. Confirmed empirically: a ~1%
    dead-pixel rate this regular (the exact same ~56 columns recurring at every framelet boundary,
    hundreds of times across a swath) reads as severe, dense-looking striping once mapprojected,
    even though the raw fraction is small -- `max_search_distance` (pixels) only needs to bridge
    those few-pixel-wide dashes, not the genuine, much larger nodata region outside the real
    footprint entirely (left untouched, since it's far beyond this search radius)."""
    filled = rasterio.fill.fillnodata(
        overlay_da.values.astype(np.float32).copy(),
        mask=(~np.isnan(overlay_da.values)).astype(np.uint8),
        max_search_distance=max_search_distance,
        smoothing_iterations=0,
    )
    return overlay_da.copy(data=filled)


def plot_overlay(
    base_raster_path,
    overlay_raster_path,
    overlay_cmap: str = "gray",
    overlay_alpha: float = 1.0,
    title: str = "Overlay (geo-aligned)",
    show_overlay_outline: bool = True,
    overlay_outline_color: str = "red",
    fill_overlay_nodata: bool = True,
):
    """Overlay `overlay_raster_path` on `base_raster_path`, both read with `rioxarray` so the real
    geographic coordinates in each file's own georeferencing drive the plot -- unlike
    `plot_comparison`'s side-by-side panels (aligned only by matching real-km extent and a north-up
    display rotation), this is genuine pixel-for-pixel geo-registration: both rasters are expected to
    already share the same map grid (e.g. `render.run_mapproject`'s `--ref-map` output alongside
    `DemOrthoResult.ortho`), not reprojected/aligned here. `overlay_cmap` defaults to `"gray"`
    (matching the base) since the overlay is typically also a real image, not categorical/scalar
    data -- a high-chroma colormap like `"inferno"` visually exaggerates what's actually a mild,
    real brightness gradient (e.g. real-sun hillshade) into a distracting "rainbow" look.
    `show_overlay_outline` traces the overlay's real (non-NaN) footprint with `geopandas` and draws
    it as a vector outline -- useful both as a sanity check that the overlay is actually where it
    claims to be, and as a template for future vector-layer overlays (e.g. the Robbins crater
    database; see `docs/plan.md`'s open items) on top of this same raster display.
    `fill_overlay_nodata` applies `_fill_overlay_nodata_for_display` before the overlay is drawn --
    display only (the outline above is still traced from the real, unfilled data, so it reflects the
    genuine sensor footprint, not the filled result). Both base and overlay are displayed with a
    `vmin=0`/`vmax=`99.9th-percentile linear stretch (same technique as `plot_render_vs_basemap`'s
    "6A darkness" fix) rather than `imshow`'s naive min/max autoscale -- without it, a real calibrated
    overlay's actual valid-data footprint can visually read as a thin sliver near `show_overlay_outline`'s
    boundary line rather than the majority of the frame it actually covers, since the naive-autoscaled
    overlay blends into the base almost invisibly at `overlay_alpha`.

    `overlay_alpha` defaults to fully opaque (`1.0`), not a blend -- per explicit user feedback, a
    partial blend (the original default, `0.6`) makes it genuinely hard to tell which pixels are the
    overlay's own content versus the base showing through, especially when debugging a
    not-yet-fully-correct overlay (exactly when that distinction matters most). `show_overlay_outline`
    still marks the overlay's real footprint boundary regardless of alpha.

    `overlay_raster_path` is expected to already cover only the real ground footprint actually being
    compared (e.g. `isis_wac.crop_for_camera`'s real, single crop cube run through
    `isis_wac.run_cam2map_for_crop`), the same way the synthetic render's own mapprojected overlay already
    does (`sat_sim` only ever renders the camera's own FOV, never more) -- no view-restricting
    parameter is needed here as a result. An earlier version of this function tried to paper over a
    too-large overlay (the *entire* WAC swath, not just the crop) with a `zoom_footprint_lonlat_deg`
    parameter that only restricted the displayed *view*, leaving `show_overlay_outline`'s trace still
    running on the full un-clipped raster -- confirmed not to work (the outline still only showed a
    partial cross-section of a much longer boundary, not a closed shape) and removed; see
    `docs/history.md`'s dated entry for the full story.

    See also `plot_overlay_toggle`, which renders this same overlay twice (`overlay_alpha=0` and
    `overlay_alpha=1`) as an auto-blinking animated GIF rather than a single fixed `overlay_alpha`."""
    base, overlay, overlay_display, base_vmin, base_vmax, overlay_vmin, overlay_vmax = _prep_overlay_rasters(
        base_raster_path, overlay_raster_path, fill_overlay_nodata
    )
    outline_geoseries = _overlay_outline_geoseries(overlay) if show_overlay_outline else None
    return _render_overlay_figure(
        base,
        overlay_display,
        base_vmin,
        base_vmax,
        overlay_vmin,
        overlay_vmax,
        overlay_cmap,
        overlay_alpha,
        title,
        outline_geoseries,
        overlay_outline_color,
    )


def _prep_overlay_rasters(base_raster_path, overlay_raster_path, fill_overlay_nodata: bool):
    """Shared data-prep for `plot_overlay`/`plot_overlay_toggle`: open both rasters, optionally fill
    the overlay's small nodata gaps for display, and compute each raster's own 0/99.9th-percentile
    stretch (see `plot_overlay`'s docstring for why a naive min/max autoscale washes out real
    calibrated-I/F data). Split out so `plot_overlay_toggle` can do this once and reuse it for both of
    its two renders, rather than re-opening/re-stretching the same rasters twice."""
    base = _open_raster_dataarray(base_raster_path)
    overlay = _open_raster_dataarray(overlay_raster_path)
    overlay_display = _fill_overlay_nodata_for_display(overlay) if fill_overlay_nodata else overlay
    base_vmin, base_vmax = 0, np.nanpercentile(base.values, 99.9)
    overlay_vmin, overlay_vmax = 0, np.nanpercentile(overlay_display.values, 99.9)
    return base, overlay, overlay_display, base_vmin, base_vmax, overlay_vmin, overlay_vmax


def _overlay_outline_geoseries(overlay):
    """The overlay's real-data footprint (see `_valid_data_outline`) as a `GeoSeries`, ready to draw
    via `.boundary.plot(...)` -- split out of `_render_overlay_figure` so `plot_overlay_toggle` can
    compute this once and pass the same `GeoSeries` into both of its two renders, rather than
    re-tracing the same footprint (a real rasterio/shapely computation) twice."""
    return geopandas.GeoSeries([_valid_data_outline(overlay)], crs=overlay.rio.crs)


def _render_overlay_figure(
    base,
    overlay_display,
    base_vmin,
    base_vmax,
    overlay_vmin,
    overlay_vmax,
    overlay_cmap,
    overlay_alpha,
    title,
    outline_geoseries,
    overlay_outline_color,
):
    """Builds one `Figure` for `plot_overlay`/`plot_overlay_toggle` -- identical rendering path
    (figsize, draw order, axis-limit restore, km tick formatting) regardless of `overlay_alpha`, so
    two calls with only `overlay_alpha` varying produce pixel-aligned images (same figure size, same
    dpi, same bbox -- required for `plot_overlay_toggle`'s two GIF frames to align pixel-for-pixel)."""
    fig, ax = plt.subplots(figsize=(9, 9))
    base.plot.imshow(ax=ax, cmap="gray", vmin=base_vmin, vmax=base_vmax, add_colorbar=False)
    # xarray's plot.imshow resets the axes' xlim/ylim to whatever it just plotted -- without
    # restoring the base's own (larger) extent afterward, the overlay's plot call (its extent is
    # necessarily smaller, since it's the reprojected render, not the padded fetch AOI) would leave
    # the view cropped to just the overlay, hiding the surrounding base context entirely.
    xlim, ylim = ax.get_xlim(), ax.get_ylim()
    overlay_display.plot.imshow(
        ax=ax, cmap=overlay_cmap, alpha=overlay_alpha, vmin=overlay_vmin, vmax=overlay_vmax, add_colorbar=False
    )
    if outline_geoseries is not None:
        outline_geoseries.boundary.plot(ax=ax, color=overlay_outline_color, linewidth=1.5)
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)
    ax.set_title(title)
    # Both rasters are in a local projected CRS (meters), not raw lon/lat -- see
    # `lunaserv.fetch_dem_and_ortho`'s docstring for why (a genuinely isotropic-meter grid, unlike
    # Lunaserv's native unprojected geographic layer). Displayed in km (matching
    # `plot_comparison`/`plot_isis_comparison`'s real-km scaling) via a tick formatter -- the
    # underlying data/geometry stay in meters (real CRS units), only the tick labels are rescaled.
    km_formatter = matplotlib.ticker.FuncFormatter(lambda x, _: f"{x / 1000:.0f}")
    ax.xaxis.set_major_formatter(km_formatter)
    ax.yaxis.set_major_formatter(km_formatter)
    ax.set_xlabel("Easting (km)")
    ax.set_ylabel("Northing (km)")
    fig.tight_layout()
    return fig


def plot_overlay_toggle(
    base_raster_path,
    overlay_raster_path,
    overlay_cmap: str = "gray",
    title: str = "Overlay (geo-aligned)",
    show_overlay_outline: bool = True,
    overlay_outline_color: str = "red",
    fill_overlay_nodata: bool = True,
    initial_visible: bool = True,
    blink_interval_ms: int = 700,
):
    """Like `plot_overlay`, but instead of a single fixed `overlay_alpha`, renders the overlay at both
    `overlay_alpha=0` and `overlay_alpha=1` -- two complete, independently valid frames ("base only"
    and "with overlay"), not a transparent layer meant to be blended by the browser -- and encodes
    them as a single looping animated GIF that automatically blinks between the two: the classic
    image-analyst "blink comparator" technique for spotting registration differences, showing each
    frame at full clarity in turn rather than a partial blend of both at once (a blend makes it hard
    to tell which pixels are the overlay's own content versus the base showing through -- see
    `plot_overlay`'s docstring -- exactly when that distinction matters most, e.g. debugging a
    not-yet-correct overlay).

    This is this function's third mechanism. Two earlier, click-driven-toggle versions (a single
    `<details>` element, then a CSS `:target` scheme built from two `<a href="#...">` links) were each
    built and validated against what looked like GitHub's real `.ipynb`-rendering sanitizer, and both
    still failed live on github.com anyway -- see docs/history.md Phases 33-35 for the full trail.
    Phase 35's fetch of GitHub's actual pre-sanitization notebook payload (not just its sanitizer's
    source code) found the real, previously-unknown reason: a server-side rendering pass, upstream of
    GitHub's client-side DOMPurify sanitizer entirely, strips every `<style>` tag outright and rewrites
    same-page `href="#fragment"` links into absolute, filename-dropping URLs -- independently breaking
    both halves any CSS `:target`-based toggle needs (no CSS rule can exist without a surviving
    `<style>` tag; no click can change the in-page URL fragment once its href points elsewhere). A
    single self-contained `<img src="data:image/gif;...">` sidesteps both failure modes at once -- no
    `<style>` block, no anchor links, nothing left for either sanitizer layer to strip -- and, unlike
    the pixel-registered `:target` stacking (a live-kernel-only concession even when the toggle itself
    worked), renders identically on both platforms since it's the exact same one HTML element either
    way.

    `initial_visible` (default `True`, matching `plot_overlay`'s own `overlay_alpha=1.0` default)
    picks which frame plays first in the loop. `blink_interval_ms` sets how long each frame is shown
    before switching -- fast enough to compare by eye, slow enough not to strobe.

    Returns an `IPython.display.HTML` object (not a `Figure`) -- must be the bare last expression of a
    cell (no trailing `;`) to actually display."""
    base, overlay, overlay_display, base_vmin, base_vmax, overlay_vmin, overlay_vmax = _prep_overlay_rasters(
        base_raster_path, overlay_raster_path, fill_overlay_nodata
    )
    outline_geoseries = _overlay_outline_geoseries(overlay) if show_overlay_outline else None

    base_frame, width_px, height_px = _render_overlay_frame(
        base,
        overlay_display,
        base_vmin,
        base_vmax,
        overlay_vmin,
        overlay_vmax,
        overlay_cmap,
        0.0,
        title,
        outline_geoseries,
        overlay_outline_color,
    )
    overlay_frame, _, _ = _render_overlay_frame(
        base,
        overlay_display,
        base_vmin,
        base_vmax,
        overlay_vmin,
        overlay_vmax,
        overlay_cmap,
        1.0,
        title,
        outline_geoseries,
        overlay_outline_color,
    )

    gif_b64 = _blink_gif_b64(base_frame, overlay_frame, initial_visible, blink_interval_ms)
    html = f'<img src="data:image/gif;base64,{gif_b64}" width="{width_px}" height="{height_px}">'
    return IPython.display.HTML(html)


def _render_overlay_frame(
    base,
    overlay_display,
    base_vmin,
    base_vmax,
    overlay_vmin,
    overlay_vmax,
    overlay_cmap,
    overlay_alpha,
    title,
    outline_geoseries,
    overlay_outline_color,
):
    """Renders one `_render_overlay_figure(...)` frame to a `PIL.Image`. Deliberately no
    `bbox_inches="tight"` and no per-call `dpi=` override on `savefig` -- both frames must use plain,
    consistent full-figure export so the two frames `plot_overlay_toggle` produces are pixel-dimension-
    identical (a content-dependent tight-bbox crop could differ between the transparent and opaque
    frames, breaking the GIF's frame alignment). `plt.close(fig)` is required, not cleanup hygiene: the
    notebook's inline matplotlib backend auto-displays any figure left open at cell-end, so without it
    this would leak two extra static images into the cell's output alongside the intended GIF."""
    fig = _render_overlay_figure(
        base,
        overlay_display,
        base_vmin,
        base_vmax,
        overlay_vmin,
        overlay_vmax,
        overlay_cmap,
        overlay_alpha,
        title,
        outline_geoseries,
        overlay_outline_color,
    )
    width_px, height_px = fig.get_size_inches() * fig.dpi
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB"), round(width_px), round(height_px)


def _blink_gif_b64(base_frame, overlay_frame, initial_visible: bool, interval_ms: int) -> str:
    """Encodes `base_frame`/`overlay_frame` (same-size `PIL.Image`s, from `_render_overlay_frame`) as a
    base64-encoded, looping animated GIF. Both frames are quantized onto one shared 256-color palette
    (built from the two frames pasted side by side, then each re-quantized onto that same palette via
    `Image.quantize(palette=...)`) rather than each picking its own independently -- letting GIF
    encoding choose per-frame palettes would recolor the unchanged base-image pixels slightly
    differently in each frame, showing up as a flicker across the *whole* image on every blink instead
    of only where the overlay actually differs."""
    width, height = base_frame.size
    combined = Image.new("RGB", (width * 2, height))
    combined.paste(base_frame, (0, 0))
    combined.paste(overlay_frame, (width, 0))
    shared_palette = combined.quantize(colors=256)

    base_frame_p = base_frame.quantize(palette=shared_palette)
    overlay_frame_p = overlay_frame.quantize(palette=shared_palette)
    frames = [overlay_frame_p, base_frame_p] if initial_visible else [base_frame_p, overlay_frame_p]

    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=interval_ms, loop=0)
    return base64.b64encode(buf.getvalue()).decode("ascii")
