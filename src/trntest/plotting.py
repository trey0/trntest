"""Matplotlib display helpers for the notebook. No SPICE/network/subprocess calls -- pure
consumption of already-computed values, reading image files by path where needed."""

import warnings

import geopandas
import matplotlib.pyplot as plt
import numpy as np
import rasterio
import rasterio.errors
import rasterio.features
import rasterio.transform
import rasterio.windows
import rioxarray
import shapely.geometry
import shapely.ops
import xarray

from trntest import orientation, wac
from trntest.camera import Camera
from trntest.lunaserv import LunaservResult
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


def plot_dem_ortho(lunaserv_result: LunaservResult):
    with rasterio.open(lunaserv_result.ortho) as src:
        ortho = src.read(1)
    with rasterio.open(lunaserv_result.dem) as src:
        dem = src.read(1)

    fig, axes = plt.subplots(1, 2, figsize=(11, 5))
    axes[0].imshow(ortho, cmap="gray")
    axes[0].set_title("Lunaserv WAC global mosaic (ROI)")
    im = axes[1].imshow(dem, cmap="terrain")
    axes[1].set_title("GLD100 DEM (elevation, m)")
    fig.colorbar(im, ax=axes[1], shrink=0.8)
    for ax in axes:
        ax.set_xlabel("sample")
        ax.set_ylabel("line")
    fig.tight_layout()
    return fig


def plot_camera_footprint(lunaserv_result: LunaservResult, camera: Camera):
    with rasterio.open(lunaserv_result.ortho) as src:
        ortho = src.read(1)
        ortho_transform = src.transform

    fig, ax = plt.subplots(figsize=(6, 6))
    ax.imshow(ortho, cmap="gray")
    for name, lonlat in camera.footprint_lonlat_deg.items():
        if lonlat is None:
            continue
        lon, lat = lonlat
        row, col = rasterio.transform.rowcol(ortho_transform, lon, lat)
        ax.plot(col, row, "o", color="red" if name == "center" else "yellow")
        ax.annotate(name, (col, row), color="white", fontsize=8)
    ax.set_title("Camera footprint over Lunaserv ortho mosaic")
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
    window: rasterio.windows.Window,
    rotations: DisplayRotations,
):
    """Synthetic render next to a same-real-footprint crop of the ISIS-processed WAC image
    (`isis_wac.run_pipeline`/`isis_wac.crop_window_for_camera`) -- not a tie-pointed comparison like
    `plot_comparison`'s wac.py version, since the ISIS cube isn't reprojected onto the DEM yet
    (no `mapproject` step -- see docs/plan.md's open items).

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

    `tie_point_results` (from `session.compute_tie_points`, already computed for Phase 5) are
    reused as-is, not recomputed -- `tie_points.py`'s "crop_px" was never CSM/ISD-based to begin
    with (pure SPICE frame-index geometry), and its row/col origin and `wac.VIS_BLOCK_HEIGHT`
    scaling are exactly what `crop_window_for_camera` already uses, so the same pixel coordinates
    land correctly in this window with no transformation."""
    synthetic = read_raster_band(rendered_tif_path)
    real = read_raster_band(stitched_cub_path, window=window)
    valid = valid_pixel_mask(real)
    # vmin/vmax come from the real valid data, before any display-only fill -- the fill below is
    # cosmetic (see _fill_dead_columns_for_display's docstring) and shouldn't skew the contrast
    # stretch.
    vmin, vmax = np.percentile(real[valid], [2, 98]) if valid.any() else (None, None)
    real_display = _fill_dead_columns_for_display(real, valid) if valid.any() else real

    h_syn, w_syn = synthetic.shape
    h_crop, w_crop = real.shape
    synthetic_rot = np.rot90(synthetic, rotations.k_synthetic)
    real_rot = np.rot90(real_display, rotations.k_crop)

    synthetic_width_km = camera.cross_track_width_km
    crop_width_km = camera.cross_track_width_km
    crop_height_km = camera.n_frames_for_square_crop * camera.km_per_frame

    fig, axes = plt.subplots(1, 2, figsize=(11, 6))
    axes[0].imshow(synthetic_rot, cmap="gray", extent=[0, synthetic_width_km, synthetic_width_km, 0])
    axes[0].set_title("Synthetic (sat_sim, SPICE-posed, north-up)")
    axes[1].imshow(real_rot, cmap="gray", vmin=vmin, vmax=vmax, extent=[0, crop_width_km, crop_height_km, 0])
    axes[1].set_title("Real WAC (ISIS-processed, north-up)")
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


def _open_raster_dataarray(path):
    """`rioxarray.open_rasterio` is typed to return a `Dataset`/`list[Dataset]` for some inputs
    (e.g. multi-file), but a single-band single-file GeoTIFF (this project's only use so far) always
    yields a `DataArray` -- assert that so mypy can narrow it, rather than a `# type: ignore`."""
    opened = rioxarray.open_rasterio(path)
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


def plot_overlay(
    base_raster_path,
    overlay_raster_path,
    overlay_cmap: str = "gray",
    overlay_alpha: float = 0.6,
    title: str = "Overlay (geo-aligned)",
    show_overlay_outline: bool = True,
    overlay_outline_color: str = "red",
):
    """Overlay `overlay_raster_path` on `base_raster_path`, both read with `rioxarray` so the real
    geographic coordinates in each file's own georeferencing drive the plot -- unlike
    `plot_comparison`'s side-by-side panels (aligned only by matching real-km extent and a north-up
    display rotation), this is genuine pixel-for-pixel geo-registration: both rasters are expected to
    already share the same map grid (e.g. `render.run_mapproject`'s `--ref-map` output alongside
    `LunaservResult.ortho`), not reprojected/aligned here. `overlay_cmap` defaults to `"gray"`
    (matching the base) since the overlay is typically also a real image, not categorical/scalar
    data -- a high-chroma colormap like `"inferno"` visually exaggerates what's actually a mild,
    real brightness gradient (e.g. real-sun hillshade) into a distracting "rainbow" look.
    `show_overlay_outline` traces the overlay's real (non-NaN) footprint with `geopandas` and draws
    it as a vector outline -- useful both as a sanity check that the overlay is actually where it
    claims to be, and as a template for future vector-layer overlays (e.g. the Robbins crater
    database; see `docs/plan.md`'s open items) on top of this same raster display."""
    base = _open_raster_dataarray(base_raster_path)
    overlay = _open_raster_dataarray(overlay_raster_path)

    fig, ax = plt.subplots(figsize=(9, 9))
    base.plot.imshow(ax=ax, cmap="gray", add_colorbar=False)
    # xarray's plot.imshow resets the axes' xlim/ylim to whatever it just plotted -- without
    # restoring the base's own (larger) extent afterward, the overlay's plot call (its extent is
    # necessarily smaller, since it's the reprojected render, not the padded fetch AOI) would leave
    # the view cropped to just the overlay, hiding the surrounding base context entirely.
    xlim, ylim = ax.get_xlim(), ax.get_ylim()
    overlay.plot.imshow(ax=ax, cmap=overlay_cmap, alpha=overlay_alpha, add_colorbar=False)
    if show_overlay_outline:
        outline = _valid_data_outline(overlay)
        geopandas.GeoSeries([outline], crs=overlay.rio.crs).boundary.plot(
            ax=ax, color=overlay_outline_color, linewidth=1.5
        )
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)
    ax.set_title(title)
    # Both rasters are in a local projected CRS (meters), not raw lon/lat -- see
    # `lunaserv.fetch_dem_and_ortho`'s docstring for why (a genuinely isotropic-meter grid, unlike
    # Lunaserv's native unprojected geographic layer).
    ax.set_xlabel("x (m, local projected CRS)")
    ax.set_ylabel("y (m, local projected CRS)")
    fig.tight_layout()
    return fig
