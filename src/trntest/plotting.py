"""Matplotlib display helpers for the notebook: generic raster-display primitives
(`plot_raster`/`read_raster_band`/`valid_pixel_mask`) plus the generator-comparison figures
`image_generation.py`/reports need (`plot_render_vs_basemap`, `plot_overlay*`, `plot_zoom_blink`,
`compute_brightness_matched_diff`). SFS-validation-only plots live in `sfs_plotting.py`;
dataset-selection scatter plots live in `dataset_selection_plots.py` -- both split out since neither
audience touches the generator-comparison/report path this module serves. No SPICE/network/
subprocess calls -- pure consumption of already-computed values, reading image files by path where
needed."""

import base64
import dataclasses
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

from trntest import geo_utils, orientation
from trntest.camera import Camera
from trntest.dem_ortho import DemOrthoResult
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


def mathtt(name: str) -> str:
    """A generator name (`"hillshade"`/`"crop"`/`"reproject"`), formatted as matplotlib mathtext
    monospace for a plot title -- `image_generation.ipynb`'s own short-name title convention (e.g.
    `r"Phase 5A: $\\mathtt{hillshade}$ vs. basemap"`), factored out for reuse by any default title
    built in this module or `trn_products.py`.
    """
    return rf"$\mathtt{{{name}}}$"


# ISIS special pixels (NULL/LRS/LIS/HIS/HRS) are finite but huge-magnitude (~+-3.4e38) float32
# sentinels -- `np.isfinite` doesn't catch them. Generic threshold rather than the exact 5 bit
# patterns, since other fill-value conventions are similarly huge-magnitude, not just ISIS's.
_FILL_VALUE_MAGNITUDE_THRESHOLD = 1e37


def valid_pixel_mask(data: np.ndarray) -> np.ndarray:
    """True where `data` is finite and not a huge-magnitude fill-value sentinel."""
    return np.isfinite(data) & (np.abs(data) < _FILL_VALUE_MAGNITUDE_THRESHOLD)


def robust_median(values) -> float:
    """`np.nanmedian(values)`, returning `nan` for the degenerate case (empty, or a zero/
    non-finite median) every brightness-normalization call site in this module needs to guard
    against before dividing by it.

    :param values: Input array, or an already-valid-only selection (`numpy.ndarray` or the
        `.values` of an `xarray.DataArray`).
    :returns: The median, or `nan` if it's zero, non-finite, or `values` is empty -- pair with
        `normalize_to_median`, which leaves its input unscaled on `nan` rather than dividing by it.
    """
    if values.size == 0:
        return float("nan")
    median = np.nanmedian(values)
    return float(median) if median and np.isfinite(median) else float("nan")


def normalize_to_median(data, median: float):
    """Divide `data` by `median`, or return it unchanged if `median` is `nan` (see
    `robust_median`) -- the per-side half of this module's shared brightness normalization: two
    images being compared each call this independently with their own median, putting both on a
    scale-invariant, directly-interpretable-as-fraction-of-their-own-median basis (`0.05` off ==
    "5% of typical brightness"), rather than matching one to the other's absolute level.

    :param data: Array to scale (`numpy.ndarray` or `xarray.DataArray`) -- not necessarily the same
        selection `median` was computed over (e.g. a valid-pixels-only median applied to a
        dead-column-filled full array).
    :param median: From `robust_median`.
    :returns: `data / median`, or `data` itself if `median` is `nan`.
    """
    return data / median if np.isfinite(median) else data


def read_raster_band(path, band: int = 1, window: rasterio.windows.Window | None = None) -> np.ndarray:
    """Read one band of any raster GDAL can open by path (GeoTIFF, ISIS `.cub`, ...), optionally
    windowed to a crop.

    :param path: Raster file path.
    :param band: Band index (1-based).
    :param window: Optional crop window.
    :returns: The band as an array.
    """
    # Shared by `plot_raster` and any notebook code that needs the raw array directly (e.g. picking a
    # crop window from data), so both get the same warning suppression.
    #
    # Two non-actionable, rasterio-internal warnings suppressed narrowly here (not module-wide):
    # `NotGeoreferencedWarning` is expected for ISIS `.cub`s at this pipeline stage (no geotransform
    # yet, not a bug), and the numpy-shape `DeprecationWarning` is from inside rasterio's own code.
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
    windowed to a crop.

    :param path: Raster file path.
    :param band: Band index (1-based).
    :param window: Optional crop window.
    :param cmap: Matplotlib colormap name.
    :param stretch: Contrast-stretch to the data's own 2nd/98th percentile (excluding non-finite
        pixels and huge-magnitude fill-value sentinels).
    :returns: The `Figure`.
    """
    # Generic on purpose -- not ISIS-specific -- so it's reusable wherever a notebook just needs to
    # look at a raster file.
    #
    # `stretch=True`'s percentile calculation excludes fill-value sentinels (ISIS's NULL/LOW/HIGH
    # special pixels are finite but ~+-3.4e38 -- `np.isfinite` alone doesn't catch them): calibrated
    # I/F values are small floats near zero, so
    # leaving them in wrecks the stretch (and, upstream, any `mean`/`sum` reduction can silently
    # overflow).
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
    """Left: the ortho mosaic with the SPICE-derived camera's ground footprint overlaid, to visually
    confirm the pose lands where expected. Right: the GLD100 DEM, elevation in km. Both share one
    figure and the same km-scaled Easting/Northing axes.

    :param dem_ortho_result: DEM/ortho pair to display.
    :param camera: Camera whose footprint to overlay on the ortho panel.
    :returns: The `Figure`.
    """
    # The footprint overlay (the 4 corner rays' Moon intersections, connected into a closed quad,
    # plus a center marker) reprojects `camera.footprint_lonlat_deg` (plain geographic lon/lat) into
    # the ortho's own local Orthographic CRS via `geopandas`/`pyproj` (one `.to_crs()` call), then
    # plots both in georeferenced coordinates -- not a manual `rasterio.transform.rowcol(ortho_
    # transform, lon, lat)`, which would pass raw lon/lat degrees straight in as if they were already
    # the ortho's own local-CRS meters, collapsing every point near the map's center once the ortho
    # switched from a native lon/lat grid to a local per-camera CRS.
    with rasterio.open(dem_ortho_result.ortho) as src:
        ortho = src.read(1)
        ortho_crs = src.crs
        ortho_bounds = src.bounds
    with rasterio.open(dem_ortho_result.dem) as src:
        dem = src.read(1)
        dem_bounds = src.bounds

    moon_geographic_crs = geo_utils.geographic_crs()
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
    axes[0].set_title("Ortho mosaic (ROI) with camera footprint")

    dem_km = dem * 0.001
    im = axes[1].imshow(
        dem_km, cmap="terrain", extent=(dem_bounds.left, dem_bounds.right, dem_bounds.bottom, dem_bounds.top)
    )
    axes[1].set_title("GLD100 DEM")
    fig.colorbar(im, ax=axes[1], shrink=0.8, label="Elevation (km)")

    km_formatter = matplotlib.ticker.FuncFormatter(lambda x, _: f"{x / 1000:.0f}")
    for ax in axes:
        ax.xaxis.set_major_formatter(km_formatter)
        ax.yaxis.set_major_formatter(km_formatter)
        ax.set_xlabel("Easting (km)")
        ax.set_ylabel("Northing (km)")
    fig.tight_layout()
    return fig


def plot_synthetic_render(rendered_tif_path, label: str = "Synthetic sat_sim render"):
    """Display the synthetic `sat_sim` render.

    :param rendered_tif_path: Rendered GeoTIFF path.
    :param label: Figure title.
    :returns: The `Figure`.
    """
    synthetic = read_raster_band(rendered_tif_path)

    fig = plt.figure(figsize=(5, 5))
    plt.imshow(synthetic, cmap="gray")
    plt.title(label)
    plt.xlabel("sample")
    plt.ylabel("line")
    return fig


def _plot_tie_point_marker(ax, name, px, py, k_rotation, height, width, width_km, height_km):
    """Rotate (for north-up display) + scale to km + plot one tie-point marker.

    :param ax: Axes to plot on.
    :param name: Tie-point position name (a `MARKER_STYLES` key).
    :param px: Raw pixel x.
    :param py: Raw pixel y.
    :param k_rotation: `np.rot90` rotation count for north-up display.
    :param height: Image height, pixels (pre-rotation).
    :param width: Image width, pixels (pre-rotation).
    :param width_km: Displayed image width, km.
    :param height_km: Displayed image height, km.
    """
    # Shared by `plot_isis_comparison` and `plot_render_vs_basemap` -- same math either way, just
    # different height/width (px and km) per panel.
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


def _fill_dead_columns_for_display(band: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Row-wise linear interpolation across invalid (no-data) pixels, for display only -- doesn't
    touch the calibrated data used anywhere else.

    :param band: Input band.
    :param valid: Boolean mask, same shape as `band`, `True` where valid.
    :returns: `band` with invalid pixels filled by per-row linear interpolation (or `NaN`, for a row
        with no valid pixel to interpolate from).
    """
    # Unlike `hapke.despeckle()` (a randomly-scattered-outlier filter over otherwise-present
    # values), ISIS's `lrowaccal` "SpecialPixels" correction marks genuinely missing pixels at a
    # small, fixed, deterministic set of detector columns on each VIS framelet's first line (the same
    # 56 columns recur, unchanged, at every 14-line framelet boundary across a full cube -- see
    # docs/external-tools.md's "ISIS Pushframe pipeline" section) -- narrow (1-3 columns), within
    # otherwise smooth rows, so a simple per-row linear fill across each gap is a reasonable, standard
    # dead-pixel-column interpolation. `np.interp` also handles the edge case (`column 0` has no left
    # neighbor -- always dead, see the same docs section) by clamping to the nearest valid value
    # rather than extrapolating.
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
    synthetic_label: str = "Synthetic (sat_sim, SPICE-posed)",
    real_label: str = "Real WAC (ISIS-processed)",
):
    """Synthetic render next to a same-footprint crop of the ISIS-processed WAC image
    (`isis_wac.crop_for_camera`) -- an ad hoc km/north-up comparison, not true pixel-for-pixel
    geo-registration; for that, see `plot_overlay`'s `cam2map`-based overlay of this same
    ISIS-processed cube (`isis_wac.run_cam2map_for_crop`) instead.

    :param camera: Camera whose pose drove both the synthetic render and the crop window.
    :param tie_point_results: From `session.select_tie_points` + `tie_points.resolve_crop_pixels`
        (`{"synthetic_px", "crop_px"}` per tie-point name); reused as-is, not recomputed.
    :param rendered_tif_path: Synthetic render GeoTIFF path.
    :param stitched_cub_path: ISIS-processed WAC cube path.
    :param rotations: North-up display rotations for each panel.
    :param window: Optional crop window into `stitched_cub_path`, if it's the full, uncropped
        stitched cube rather than an already-cropped one (`isis_wac.crop_for_camera`'s own output
        needs no further windowing).
    :param synthetic_label: Synthetic render panel title (no rotation/normalization note appended).
    :param real_label: Real WAC panel title (no rotation/normalization note appended).
    :returns: The `Figure`.
    """
    # Applies the same north-up display rotation and km extent scaling `plot_render_toggle` uses,
    # for the same two reasons: the sensor's fixed pixel-axis convention needs a pass-dependent
    # rotation to display north-up, and WAC's along-track/cross-track pixel GSDs differ (the crop's
    # along-track axis is oversampled relative to cross-track -- see `crop_window_for_camera`), so a
    # plain 1:1 pixel `imshow` visibly stretches/compresses it. `rotations.k_crop` -- computed purely
    # from SPICE geometry (`camera`/`frame_timing`), never from a pixel array -- applies equally well
    # here: the ISIS cube's own line/sample convention is exactly the raw WAC frame layout
    # (confirmed in `crop_window_for_camera`'s docstring), and `isis_wac.run_pipeline`'s
    # `framestitch` FLIP is driven by the same `camera.reverse_crop_along_track` signal `k_crop`
    # itself depends on.
    #
    # `tie_points.py`'s "crop_px" is a `campt` ground-to-image query against `stitched_cub_path`
    # itself (see that module's docstring), so its row/col origin already matches this exact cube
    # with no transformation needed. A tie point the camera doesn't see (see
    # `tie_points.resolve_crop_pixels`'s docstring) is simply absent from `tie_point_results` -- this
    # function draws whatever's present, no special handling needed for a missing point.
    #
    # Each panel independently normalized to its own valid-pixel median = 1.0 (`robust_median`/
    # `normalize_to_median`, same technique `_prep_overlay_rasters` uses) rather than real matched
    # to synthetic's absolute scale -- necessary since the ISIS cube (calibrated I/F, ~0.01-0.2) and
    # the synthetic render (a rendered-texture brightness value, ~0-255) are on entirely different
    # numeric scales, and matching one to the other's fixed range risks oversaturating whichever
    # side needed the bigger correction (e.g. a much darker real crop scaled way up to match).
    synthetic = read_raster_band(rendered_tif_path)
    real = read_raster_band(stitched_cub_path, window=window)
    valid = valid_pixel_mask(real)
    real_filled = _fill_dead_columns_for_display(real, valid) if valid.any() else real

    synthetic_valid = valid_pixel_mask(synthetic)
    synthetic_norm = normalize_to_median(synthetic, robust_median(synthetic[synthetic_valid]))
    # Median computed over the real, un-filled valid pixels (the fill above is a display convenience
    # for isolated dead columns -- see _fill_dead_columns_for_display's docstring -- not something
    # that should influence the brightness normalization).
    real_median = robust_median(real[valid]) if valid.any() else float("nan")
    real_norm = normalize_to_median(real_filled, real_median)

    h_syn, w_syn = synthetic.shape
    h_crop, w_crop = real.shape
    synthetic_rot = np.rot90(synthetic_norm, rotations.k_synthetic)
    real_rot = np.rot90(real_norm, rotations.k_crop)

    # synthetic_width_km != synthetic_height_km in general once camera.solve_corrected_fov shrinks
    # the FOV (see its docstring) -- and, independently, no longer necessarily equal to
    # crop_width_km/crop_height_km either, since that correction only shrinks the synthetic render,
    # not the real crop's own (unrelated) window.
    synthetic_width_km = camera.render_cross_track_km
    synthetic_height_km = camera.render_along_track_km
    crop_width_km = camera.cross_track_width_km
    crop_height_km = camera.n_frames_for_square_crop * camera.km_per_frame

    # vmax is the larger of the two panels' own post-normalization 99.9th percentile -- not a fixed
    # 255 sized only for the synthetic render's own native uint8 scale -- so the real panel's own
    # highlights get enough headroom if it needed a big correction, instead of clipping against a
    # ceiling that was never sized for it.
    vmax = max(
        np.nanpercentile(synthetic_norm[synthetic_valid], 99.9),
        np.nanpercentile(real_norm[valid], 99.9) if valid.any() else 0.0,
    )

    fig, axes = plt.subplots(1, 2, figsize=(11, 6))
    axes[0].imshow(
        synthetic_rot, cmap="gray", vmin=0, vmax=vmax, extent=[0, synthetic_width_km, synthetic_height_km, 0]
    )
    axes[0].set_title(f"{synthetic_label} (north-up)")
    axes[1].imshow(real_rot, cmap="gray", vmin=0, vmax=vmax, extent=[0, crop_width_km, crop_height_km, 0])
    axes[1].set_title(f"{real_label} (median-normalized, north-up)")
    for ax in axes:
        ax.set_xlabel("km")
        ax.set_ylabel("km")

    for name, r in tie_point_results.items():
        px, py = r["synthetic_px"]
        _plot_tie_point_marker(
            axes[0], name, px, py, rotations.k_synthetic, h_syn, w_syn, synthetic_width_km, synthetic_height_km
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
    """North-up, km-scaled side-by-side of a render's own unprojected pixels against a plain pixel
    crop of the hillshade basemap covering the same ground footprint.

    :param render_array: The render's own pixels, unprojected (genuine sensor/render image quality,
        not a resampled reprojection).
    :param rotation_k: North-up display rotation for the render panel.
    :param render_width_km: Displayed render width, km.
    :param render_height_km: Displayed render height, km.
    :param footprint_lonlat_deg: The render's own ground footprint (corners + center, matching
        `Camera.footprint_lonlat_deg`'s shape) -- `Camera.footprint_lonlat_deg` itself for the
        synthetic render, `tie_points.crop_footprint_corners()` for the WAC crop.
    :param base_raster_path: Basemap raster to crop (e.g. `DemOrthoResult.ortho`).
    :param title: Figure title.
    :param render_label: Render panel title.
    :param tie_point_results: From `session.select_tie_points` + `tie_points.resolve_crop_pixels`;
        marks the same tie points on both panels if given.
    :param render_px_key: Which of each tie point's two pre-computed pixel coordinates to use on the
        render panel (`"synthetic_px"` or `"crop_px"`).
    :returns: The `Figure`.
    """
    # This is the "A"-style geometry check: a quick ad hoc quality/rough-alignment look -- for true
    # pixel-for-pixel geo-registration against the same basemap, see `plot_overlay`'s "B"-style
    # `mapproject`-based overlay instead.
    #
    # `render_array` is passed through `_fill_dead_columns_for_display` before display, same as
    # `plot_isis_comparison`'s real panel -- a no-op for the synthetic render (no dead pixels to
    # begin with), but necessary for the WAC crop: without it, the ~1% framelet-boundary dead-pixel
    # pattern (see docs/external-tools.md's "ISIS Pushframe pipeline" section) shows up as visible
    # speckle.
    #
    # `geo_utils.footprint_bbox_local_m` (already used to size the original WMS fetch -- see its
    # docstring) converts `footprint_lonlat_deg`'s corners to the basemap's own local Orthographic
    # CRS (centered on this same footprint's own center, see `dem_ortho.fetch_dem_and_ortho`) to find
    # the matching pixel window -- a plain windowed read, no resampling. Unlike the render (fixed
    # sensor-pixel axes, needing a pass-dependent rotation for north-up display), the basemap crop
    # needs no rotation: the local Orthographic CRS is already north-referenced by construction (+Y =
    # north).
    #
    # On the basemap panel, each tie point's `"lonlat"` is projected directly into the crop's own
    # local-CRS offset (`geo_utils.orthographic_xy_m`, same center as the crop itself) -- no pixel
    # coordinates needed there, since that panel is a plain, unrotated crop of an already-georeferenced
    # raster.
    center_lon, center_lat = footprint_lonlat_deg["center"]
    minx, miny, maxx, maxy = geo_utils.footprint_bbox_local_m(footprint_lonlat_deg, center_lon, center_lat)

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
    axes[1].set_title("Basemap")
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
            x, y = geo_utils.orthographic_xy_m(lon, lat, center_lon, center_lat)
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


def cellsize_m(raster_da) -> float:
    """Pixel size, meters, from `raster_da`'s own `x` coordinate spacing.

    :param raster_da: A `rioxarray`-opened `DataArray`.
    :returns: Pixel size, meters.
    """
    # Shared by `compute_brightness_matched_diff` and `plot_sfs_comparison`'s own `reindex_like`
    # alignment tolerance (half a pixel, generous enough to absorb floating-point/rounding
    # differences between two independently-computed windows, tight enough to never match two
    # genuinely different grid cells -- see `compute_brightness_matched_diff`'s own docstring).
    return float(abs(raster_da.x.values[1] - raster_da.x.values[0]))


def open_raster_dataarray(path):
    """Open `path` as a single-band `xarray.DataArray` via `rioxarray`.

    :param path: Raster file path.
    :returns: The opened `DataArray`, `masked=True` so nodata reads as `NaN`.
    """
    # `rioxarray.open_rasterio` is typed to return a `Dataset`/`list[Dataset]` for some inputs (e.g.
    # multi-file), but a single-band single-file GeoTIFF (this project's only use so far) always
    # yields a `DataArray` -- assert that so mypy can narrow it, rather than a `# type: ignore`.
    #
    # `masked=True` converts nodata to NaN based on the file's own embedded `nodata` tag -- necessary
    # because `mapproject`'s nodata convention depends on its input format: a synthetic render (plain
    # GeoTIFF source) comes out as NaN already, but an ISIS `.cub` source (e.g. the WAC overlay)
    # carries ISIS's own huge-magnitude NULL sentinel (~-3.4e38) straight through into the output,
    # with a `nodata` tag set to match. Without `masked=True`, that sentinel dominates
    # `plot.imshow`'s automatic vmin/vmax and washes the 0.01-0.13 I/F signal out to a uniform flat
    # gray.
    opened = rioxarray.open_rasterio(path, masked=True)
    assert isinstance(opened, xarray.DataArray)
    return opened.squeeze()


def _valid_data_outline(raster_da):
    """The non-NaN footprint of `raster_da` as a single Shapely geometry, in the raster's own
    (already-georeferenced) coordinates.

    :param raster_da: A `rioxarray`-opened `DataArray`.
    :returns: The outline as a `MultiPolygon`, with interior holes dropped.
    """
    # E.g. `run_mapproject`'s output is NaN outside the actual reprojected camera footprint (see
    # docs/external-tools.md's ASP `mapproject` section), so this traces that footprint's true outline
    # rather than the raster's full (padded, mostly-nodata) pixel grid. Interior holes (isolated
    # nodata pixels from DEM ray-intersection speckle -- see `render.DEM_HEIGHT_ERROR_TOL_M`'s
    # docstring) are dropped:
    # they're display noise, not meaningful "outline" content.
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
    `rasterio.fill.fillnodata`.

    :param overlay_da: A `rioxarray`-opened `DataArray`.
    :param max_search_distance: Pixels; only needs to bridge a few-pixel-wide gap, not the much
        larger nodata region outside the actual footprint entirely (left untouched, since it's far
        beyond this search radius).
    :returns: `overlay_da` with small gaps filled.
    """
    # Orientation-agnostic (unlike `_fill_dead_columns_for_display`'s row-wise interpolation, which
    # only helps gaps that are narrow within a row), needed here because a `mapproject` output's
    # sparse defects (e.g. the same framelet-boundary dead-detector-columns
    # `_fill_dead_columns_for_display` handles pre-reprojection -- see its docstring) trace diagonal
    # "dash" streaks once reprojected into map space, following the sensor's ground track rather than
    # the image's row/column axes. A ~1% dead-pixel rate this regular (the exact same ~56 columns
    # recurring at every framelet boundary, hundreds of times across a swath) reads as severe,
    # dense-looking striping once mapprojected, even though the raw fraction is small.
    filled = rasterio.fill.fillnodata(
        overlay_da.values.astype(np.float32).copy(),
        mask=(~np.isnan(overlay_da.values)).astype(np.uint8),
        max_search_distance=max_search_distance,
        smoothing_iterations=0,
    )
    return overlay_da.copy(data=filled)


@dataclasses.dataclass
class OverlayLayer:
    """One optional vector-data annotation layer for `plot_overlay`/`plot_overlay_toggle` -- e.g.
    `craters.crater_overlay_layer`'s Robbins crater database ellipses, the concrete case this was
    added for.

    :ivar geoseries: Must already be in the same CRS as the base/overlay raster and already filtered
        down to the relevant AOI -- this module stays consumption-only (no fetch/filter/reprojection
        here, per its own module docstring).
    :ivar color: Line/fill color.
    :ivar linewidth: Boundary line width, when `fill=False`.
    :ivar alpha: Opacity.
    :ivar fill: Draw as a filled shape rather than just the boundary.
    :ivar linestyle: Any matplotlib `Line2D` linestyle -- a named style (`"solid"`, `"dashed"`,
        `"dotted"`) or a custom `(offset, (on_pt, off_pt, ...))` dash tuple.
    """

    # Deliberately a plain `geoseries` + a handful of style fields rather than named
    # `plot_overlay(..., crater_geoseries=...)` parameters -- an earlier version did exactly that for
    # craters alone, and adding a second annotation-layer type would have meant threading two more
    # parameters through all four of `plot_overlay`/`plot_overlay_toggle`/`_render_overlay_figure`/
    # `_render_overlay_frame` again. A `layers: list[OverlayLayer]` on all four instead scales to any
    # number of layers with no further signature changes -- the footprint outline
    # (`outline_geoseries`/`overlay_outline_color`) deliberately stays a separate, dedicated,
    # always-present parameter rather than folding into this list: it's the actual geometry
    # validation reference the whole comparison exists to show, not an optional annotation.
    #
    # `fill=False` (default) draws just the boundary, matching the existing footprint-outline style --
    # outlines read better than fills stacked on top of imagery at `plot_overlay`'s typical
    # `overlay_alpha=1.0`.
    #
    # `linestyle` was added because a solid outline at full opacity/width can itself obscure the very
    # rim it's meant to help visually verify -- a sparse dotted line (e.g. `(0, (1, 10))`: a 1pt dash
    # every 10pt) leaves most of the underlying image visible between dots while still marking the
    # boundary at full color/opacity, unlike turning down `alpha`/`linewidth` instead (which fades the
    # boundary itself, not just how much it covers).

    geoseries: geopandas.GeoSeries
    color: str = "orange"
    linewidth: float = 1.0
    alpha: float = 1.0
    fill: bool = False
    linestyle: str | tuple = "solid"

    def plot(self, ax):
        """Draw this layer on `ax`."""
        if self.fill:
            self.geoseries.plot(ax=ax, color=self.color, alpha=self.alpha)
        else:
            self.geoseries.boundary.plot(
                ax=ax, color=self.color, linewidth=self.linewidth, alpha=self.alpha, linestyle=self.linestyle
            )


def plot_overlay(
    base_raster_path,
    overlay_raster_path,
    overlay_cmap: str = "gray",
    overlay_alpha: float = 1.0,
    title: str = "Overlay (geo-aligned)",
    show_overlay_outline: bool = True,
    overlay_outline_color: str = "red",
    fill_overlay_nodata: bool = True,
    layers: list[OverlayLayer] | None = None,
    margin_frac: float = 0.3,
):
    """Overlay `overlay_raster_path` on `base_raster_path`, both read with `rioxarray` so the
    georeferenced coordinates in each file drive the plot -- genuine pixel-for-pixel geo-registration,
    unlike `plot_isis_comparison`'s side-by-side panels.

    :param base_raster_path: Base raster (e.g. `DemOrthoResult.ortho`).
    :param overlay_raster_path: Overlay raster, expected to already share the same map grid as the
        base (e.g. `render.run_mapproject`'s `--ref-map` output) -- not reprojected/aligned here.
    :param overlay_cmap: Overlay colormap.
    :param overlay_alpha: Overlay opacity.
    :param title: Figure title.
    :param show_overlay_outline: Trace the overlay's non-NaN footprint and draw it as a vector
        outline.
    :param overlay_outline_color: Outline color.
    :param fill_overlay_nodata: Fill the overlay's small nodata gaps for display
        (`_fill_overlay_nodata_for_display`) before drawing; the outline (if shown) is still traced
        from the unfilled data.
    :param layers: Additional vector annotation layers (see `OverlayLayer`), drawn on top of the
        footprint outline, in list order.
    :param margin_frac: How much of the base raster's padding beyond the overlay's own footprint to
        display, `1.0` showing the base's full extent and `0.0` cropping tight to the overlay's own
        bounding box -- display-only, doesn't affect what's fetched/rendered. Defaults to `0.3`, not
        `1.0`: the full padded AOI (`config.dem_padding_fraction`) devotes more of a fixed-size
        figure to basemap context than to the overlay itself, most of it never used for actual
        comparison. See `_render_overlay_figure`'s "why not smaller" note for why this never crops
        past the overlay's own extent the way the removed `zoom_footprint_lonlat_deg` parameter once
        did.
    :returns: The `Figure`.
    """
    # `overlay_cmap` defaults to `"gray"` (matching the base) since the overlay is typically also an
    # image, not categorical/scalar data -- a high-chroma colormap like `"inferno"` visually
    # exaggerates what's actually a mild brightness gradient (e.g. a sun-lit hillshade) into a
    # distracting "rainbow" look. `show_overlay_outline` is also the template `OverlayLayer` (e.g.
    # `craters.crater_overlay_layer`'s Robbins crater ellipses) draws vector-layer overlays with, on
    # top of this same raster display.
    #
    # Base and overlay are each independently normalized to their own median (`_prep_overlay_rasters`)
    # rather than overlay matched to base's absolute level. Necessary here since `overlay_raster_path`
    # and `base_raster_path` can come from different pipelines on different numeric scales (e.g. an
    # ISIS-calibrated I/F crop vs. the hillshade-based basemap): each panel's own independent
    # percentile stretch doesn't guarantee the two end up looking similarly bright even though each
    # is individually well-exposed -- distracting when `plot_overlay_toggle` blinks between them --
    # and matching one to the other's fixed range risks oversaturating whichever side needed the
    # bigger correction (e.g. one side much darker than the other).
    # Both are then displayed on the same `vmin=0`/`vmax=` linear stretch (the larger of the two
    # sides' own 99.9th percentile -- same technique as `plot_render_vs_basemap`'s darkness fix, now
    # protecting either side, not just base) rather than `imshow`'s naive min/max autoscale --
    # without it, a calibrated overlay's actual valid-data footprint can visually read as a thin
    # sliver near `show_overlay_outline`'s boundary line rather than the majority of the frame it
    # actually covers, since the naive-autoscaled overlay blends into the base almost invisibly at
    # `overlay_alpha`.
    #
    # `overlay_alpha` defaults to fully opaque (`1.0`), not a blend -- per explicit user feedback, a
    # partial blend (the original default, `0.6`) makes it hard to tell which pixels are the
    # overlay's own content versus the base showing through, especially when debugging a
    # not-yet-fully-correct overlay (exactly when that distinction matters most). `show_overlay_outline`
    # still marks the overlay's footprint boundary regardless of alpha.
    #
    # `overlay_raster_path` is expected to already cover only the ground footprint actually being
    # compared (e.g. `isis_wac.crop_for_camera`'s single crop cube run through
    # `isis_wac.run_cam2map_for_crop`), the same way the synthetic render's own mapprojected overlay
    # already does (`sat_sim` only ever renders the camera's own FOV, never more) -- no
    # view-restricting parameter is needed here as a result. An earlier version tried to paper over a
    # too-large overlay (the entire WAC swath, not just the crop) with a `zoom_footprint_lonlat_deg`
    # parameter that only restricted the displayed view, leaving `show_overlay_outline`'s trace still
    # running on the full un-clipped raster -- didn't work (the outline still only showed a partial
    # cross-section of a much longer boundary, not a closed shape) and was removed.
    #
    # See also `plot_overlay_toggle`, which renders this same overlay twice (`overlay_alpha=0` and
    # `overlay_alpha=1`) as an auto-blinking animated GIF rather than a single fixed `overlay_alpha`.
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
        layers,
        margin_frac,
    )


def _prep_overlay_rasters(base_raster_path, overlay_raster_path, fill_overlay_nodata: bool):
    """Shared data-prep for `plot_overlay`/`plot_overlay_toggle`/`compute_brightness_matched_diff`:
    open both rasters, optionally fill the overlay's small nodata gaps for display, normalize both
    to their shared overlap region's median, and compute a shared display stretch.

    :param base_raster_path: Base raster path.
    :param overlay_raster_path: Overlay raster path.
    :param fill_overlay_nodata: Fill the overlay's small nodata gaps for display.
    :returns: `(base, overlay, overlay_display, base_vmin, base_vmax, overlay_vmin, overlay_vmax)` --
        `base`/`overlay_display` are the normalized (median=1.0) rasters actually used for display
        and, by `compute_brightness_matched_diff`, for its diff; `overlay` is the original,
        un-normalized raster (only its NaN footprint matters to `_overlay_outline_geoseries`, not
        its brightness).
    """
    # Split out so plot_overlay_toggle can do this once and reuse it for both of its two renders,
    # rather than re-opening/re-normalizing the same rasters twice.
    base = open_raster_dataarray(base_raster_path)
    overlay = open_raster_dataarray(overlay_raster_path)
    overlay_display = _fill_overlay_nodata_for_display(overlay) if fill_overlay_nodata else overlay

    # Each side's normalizing median is taken only from the region where *both* rasters have valid
    # data, not each side's own full coverage. `base` is typically a padded AOI well beyond the
    # overlay's own footprint (e.g. `dem_padding_fraction`'s 30%-per-side basemap padding vs. the
    # overlay's camera-FOV-only extent), and that extra padding ring's average brightness isn't
    # necessarily representative of the region actually being compared -- taking each side's
    # full-frame median instead put the two "median = 1.0" baselines at visibly different absolute
    # brightness levels whenever the padding ring and the overlap region differed, showing up as a
    # visible mismatch right in the overlap area (the only place both sides are on screen at once).
    # `overlay` is reindexed onto `base`'s grid first (same approach `compute_brightness_matched_diff`
    # uses to align the two for its own diff) purely to build this overlap mask -- the actual
    # normalization below still divides each side's own full raster by its own overlap-derived median.
    overlay_on_base_grid = overlay.reindex_like(base, method="nearest", tolerance=cellsize_m(base) / 2.0)
    overlap_mask = np.isfinite(base.values) & np.isfinite(overlay_on_base_grid.values)

    # Each side independently normalized to its own overlap-region median = 1.0 (`robust_median`/
    # `normalize_to_median`), not overlay matched to base's absolute level -- this is what makes
    # `compute_brightness_matched_diff`'s own |diff| a scale-invariant fraction-of-median number
    # (comparable across candidates at different absolute brightness levels, not base's own
    # arbitrary units), and lets the vmax below protect either side from oversaturating, not just
    # base.
    base = normalize_to_median(base, robust_median(base.values[overlap_mask]))
    overlay_display = normalize_to_median(overlay_display, robust_median(overlay_on_base_grid.values[overlap_mask]))

    # vmax is the larger of the two sides' own post-normalization 99.9th percentile, not base's
    # alone -- so a side that needed a bigger correction (e.g. a much darker overlay) still gets
    # enough headroom for its own highlights, instead of clipping against a ceiling sized only for
    # base. An independent per-side percentile stretch instead of one shared vmax would silently
    # re-normalize the comparison away (each side re-stretched to its own visual range, hiding a
    # real relative brightness difference that should stay visible).
    vmax = max(np.nanpercentile(base.values, 99.9), np.nanpercentile(overlay_display.values, 99.9))
    base_vmin, base_vmax = 0, vmax
    overlay_vmin, overlay_vmax = 0, vmax
    return base, overlay, overlay_display, base_vmin, base_vmax, overlay_vmin, overlay_vmax


@dataclasses.dataclass(frozen=True)
class BrightnessMatchedDiffResult:
    """`compute_brightness_matched_diff`'s return."""

    mean_abs_diff: float
    median_abs_diff: float
    valid_pixel_count: int


def compute_brightness_matched_diff(base_raster_path, overlay_raster_path) -> BrightnessMatchedDiffResult:
    """The quantitative counterpart to `plot_overlay`'s visual comparison: a reusable
    brightness-normalized mean|diff| between two geo-aligned rasters on the same map grid.

    Both rasters are independently normalized to their own valid-pixel median = 1.0 before diffing
    (`_prep_overlay_rasters`) -- so `mean_abs_diff`/`median_abs_diff` are scale-invariant fractions
    of each raster's own median brightness (`0.05` == "5% of typical brightness"), comparable
    across candidates at different absolute brightness levels, not a raw diff in one raster's own
    arbitrary absolute units.

    :param base_raster_path: Base raster (typically the full padded DEM/ortho fetch AOI).
    :param overlay_raster_path: Overlay raster, expected to share the base's CRS/pixel size but not
        necessarily the same window/extent (e.g. a crop's own smaller footprint).
    :returns: A `BrightnessMatchedDiffResult`.
    """
    # Factored out so comparisons are reproducible and mutually comparable, rather than each
    # investigation notebook hand-recomputing this metric ad hoc with no guarantee of matching any
    # other attempt's exact methodology.
    #
    # Reuses `_prep_overlay_rasters`'s exact normalization (see its own docstring for why) with
    # `fill_overlay_nodata=False`: unlike `plot_overlay`'s display use, a quantitative diff must never
    # include interpolated/filled pixels, only real data.
    #
    # The two rasters are aligned by coordinate (`reindex_like`), not raw array indexing, since they
    # aren't guaranteed to share the same window/extent -- naively diffing the two underlying arrays
    # by raw position raises a shape-mismatch error, or worse, would silently misalign them if the
    # shapes happened to match by coincidence. `tolerance` is half the base raster's own pixel size,
    # derived from its `x` coordinate spacing -- generous enough to absorb any floating-point/rounding
    # difference between how each raster's own window was independently computed, tight enough to
    # never accidentally match two genuinely different grid cells.
    base, _, overlay_display, *_ = _prep_overlay_rasters(
        base_raster_path, overlay_raster_path, fill_overlay_nodata=False
    )
    overlay_aligned = overlay_display.reindex_like(base, method="nearest", tolerance=cellsize_m(base) / 2.0)

    a = base.values.astype(np.float64)
    b = overlay_aligned.values.astype(np.float64)
    valid = np.isfinite(a) & np.isfinite(b)
    diffs = np.abs(a[valid] - b[valid])
    return BrightnessMatchedDiffResult(
        mean_abs_diff=float(np.mean(diffs)) if diffs.size else float("nan"),
        median_abs_diff=float(np.median(diffs)) if diffs.size else float("nan"),
        valid_pixel_count=int(valid.sum()),
    )


def _overlay_outline_geoseries(overlay):
    """The overlay's non-NaN footprint (`_valid_data_outline`) as a `GeoSeries`.

    :param overlay: A `rioxarray`-opened `DataArray`.
    :returns: The footprint as a single-entry `GeoSeries`.
    """
    # Split out of `_render_overlay_figure` so `plot_overlay_toggle` can compute this once and pass
    # the same `GeoSeries` into both of its two renders, rather than re-tracing the same footprint (a
    # rasterio/shapely computation) twice.
    return geopandas.GeoSeries([_valid_data_outline(overlay)], crs=overlay.rio.crs)


def _crop_limits(
    full_lim: tuple[float, float], overlay_lo: float, overlay_hi: float, margin_frac: float
) -> tuple[float, float]:
    """Shrink an axis's `full_lim` toward `[overlay_lo, overlay_hi]` by `margin_frac`.

    `margin_frac=1.0` returns `full_lim` unchanged; `0.0` returns exactly `(overlay_lo, overlay_hi)`
    -- never tighter, so a closed footprint outline traced from the overlay's own extent always
    stays fully inside the cropped view (see `_render_overlay_figure`'s call site for why that
    matters). Handles either axis direction (`full_lim` may be given high-to-low, e.g. image y-axes).

    :param full_lim: `(lo, hi)` or `(hi, lo)` -- the uncropped axis limits.
    :param overlay_lo: Overlay's own extent minimum, same units/order-independent.
    :param overlay_hi: Overlay's own extent maximum.
    :param margin_frac: `0.0`-`1.0` fraction of the base-to-overlay padding to keep.
    :returns: New limits, in the same order as `full_lim`.
    """
    a, b = full_lim
    forward = a <= b
    span_lo, span_hi = (a, b) if forward else (b, a)
    new_lo = overlay_lo - margin_frac * (overlay_lo - span_lo)
    new_hi = overlay_hi + margin_frac * (span_hi - overlay_hi)
    return (new_lo, new_hi) if forward else (new_hi, new_lo)


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
    layers: list[OverlayLayer] | None = None,
    margin_frac: float = 0.3,
):
    """Build one `Figure` for `plot_overlay`/`plot_overlay_toggle`.

    :param base: Base `DataArray`.
    :param overlay_display: Overlay `DataArray` (already brightness-normalized/nodata-filled).
    :param base_vmin: Base display stretch minimum.
    :param base_vmax: Base display stretch maximum.
    :param overlay_vmin: Overlay display stretch minimum.
    :param overlay_vmax: Overlay display stretch maximum.
    :param overlay_cmap: Overlay colormap.
    :param overlay_alpha: Overlay opacity.
    :param title: Figure title.
    :param outline_geoseries: Overlay footprint outline to draw, or `None` to skip.
    :param overlay_outline_color: Outline color.
    :param layers: Additional vector annotation layers (see `OverlayLayer`), drawn after
        `outline_geoseries`, in list order.
    :param margin_frac: See `plot_overlay`'s docstring.
    :returns: The `Figure`.
    """
    # Identical rendering path (figsize, draw order, axis-limit restore, km tick formatting)
    # regardless of `overlay_alpha`, so two calls with only `overlay_alpha` varying produce
    # pixel-aligned images (same figure size, same dpi, same bbox -- required for
    # `plot_overlay_toggle`'s two GIF frames to align pixel-for-pixel).
    #
    # `layers`' default color (`OverlayLayer`'s `"orange"`) is distinct from both the footprint
    # outline's `"red"` and `MARKER_STYLES`'s tie-point colors so a default-styled layer is never
    # hidden underneath the footprint boundary line.
    fig, ax = plt.subplots(figsize=(9, 9))
    base.plot.imshow(ax=ax, cmap="gray", vmin=base_vmin, vmax=base_vmax, add_colorbar=False)
    # xarray's plot.imshow resets the axes' xlim/ylim to whatever it just plotted -- without
    # restoring the base's own (larger) extent afterward, the overlay's plot call (its extent is
    # necessarily smaller, since it's the reprojected render, not the padded fetch AOI) would leave
    # the view cropped to just the overlay, hiding the surrounding base context entirely.
    xlim, ylim = ax.get_xlim(), ax.get_ylim()
    # `margin_frac < 1.0` shrinks that restored extent back down toward the overlay's own bounding
    # box (never past it -- see `_crop_limits`), trading surrounding basemap context for more of a
    # fixed-size figure's pixels spent on the overlay itself. Unlike the removed
    # `zoom_footprint_lonlat_deg` parameter this replaces (see `plot_overlay`'s docstring), this can
    # never truncate `outline_geoseries`'s closed footprint shape, since it never crops tighter than
    # the overlay's own extent.
    if margin_frac < 1.0:
        xlim = _crop_limits(xlim, float(overlay_display.x.min()), float(overlay_display.x.max()), margin_frac)
        ylim = _crop_limits(ylim, float(overlay_display.y.min()), float(overlay_display.y.max()), margin_frac)
    overlay_display.plot.imshow(
        ax=ax, cmap=overlay_cmap, alpha=overlay_alpha, vmin=overlay_vmin, vmax=overlay_vmax, add_colorbar=False
    )
    if outline_geoseries is not None:
        outline_geoseries.boundary.plot(ax=ax, color=overlay_outline_color, linewidth=1.5)
    for layer in layers or []:
        layer.plot(ax)
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)
    ax.set_title(title)
    # Both rasters are in a local projected CRS (meters), not raw lon/lat -- see
    # `dem_ortho.fetch_dem_and_ortho`'s docstring for why (an isotropic-meter grid, unlike Lunaserv's
    # native unprojected geographic layer). Displayed in km (matching
    # `plot_isis_comparison`'s real-km scaling) via a tick formatter -- the
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
    overlay_label: str | None = None,
    show_overlay_outline: bool = True,
    overlay_outline_color: str = "red",
    fill_overlay_nodata: bool = True,
    initial_visible: bool = True,
    blink_interval_ms: int = 700,
    layers: list[OverlayLayer] | None = None,
    margin_frac: float = 0.3,
):
    """Like `plot_overlay`, but renders the overlay at both `overlay_alpha=0` and `overlay_alpha=1`
    and encodes them as a single looping animated GIF that automatically blinks between the two -- the
    classic image-analyst "blink comparator" technique for spotting registration differences.

    :param base_raster_path: Base raster.
    :param overlay_raster_path: Overlay raster.
    :param overlay_cmap: Overlay colormap.
    :param title: Figure title (each frame gets a checkbox-glyph suffix, see below).
    :param overlay_label: If given, the suffix names the overlay directly
        (`"{title}: ☑ {overlay_label}"`/`"{title}: ☐ {overlay_label}"`, checkbox right next to the
        name of the thing blinking on/off) instead of the generic "Overlay Visibility" toggle.
    :param show_overlay_outline: Trace and draw the overlay's footprint outline.
    :param overlay_outline_color: Outline color.
    :param fill_overlay_nodata: Fill the overlay's small nodata gaps for display.
    :param initial_visible: Which frame plays first in the loop (matching `plot_overlay`'s own
        `overlay_alpha=1.0` default).
    :param blink_interval_ms: How long each frame is shown before switching.
    :param layers: Additional vector annotation layers (see `OverlayLayer`); drawn identically in
        both GIF frames.
    :param margin_frac: See `plot_overlay`'s docstring; applied identically to both frames (computed
        from the same `base`/`overlay_display`, so the two stay pixel-aligned).
    :returns: An `IPython.display.HTML` object -- must be the bare last expression of a cell (no
        trailing `;`) to actually display. Only actually animates in a GIF-rendering viewer (a live
        Jupyter kernel, GitHub's `.ipynb` blob view) -- a static screenshot or a non-animating
        viewer only shows one frame.
    """
    # Two complete, independently valid frames ("base only" and "with overlay"), not a transparent
    # layer meant to be blended by the browser -- showing each frame at full clarity in turn rather
    # than a partial blend of both at once (a blend makes it hard to tell which pixels are the
    # overlay's own content versus the base showing through -- see `plot_overlay`'s docstring --
    # exactly when that distinction matters most, e.g. debugging a not-yet-correct overlay).
    #
    # This is this function's third toggle mechanism. Two earlier, click-driven-toggle versions (a
    # single `<details>` element, then a CSS `:target` scheme built from two `<a href="#...">` links)
    # both failed live on github.com: a server-side rendering pass, upstream of GitHub's client-side
    # sanitizer, strips every `<style>` tag outright and rewrites same-page `href="#fragment"` links
    # into absolute, filename-dropping URLs -- independently breaking both halves any CSS
    # `:target`-based toggle needs. A single self-contained `<img src="data:image/gif;...">`
    # sidesteps both failure modes at once -- no `<style>` block, no anchor links, nothing left for
    # either sanitizer layer to strip -- and renders identically on both platforms since it's the
    # exact same one HTML element either way.
    #
    # Each frame's title gets a `" - ☑ Overlay Visibility"`/`" - ☐ Overlay Visibility"` suffix
    # (Unicode's checked/unchecked ballot-box glyphs) marking which frame is showing. An earlier
    # version used the GFM `[x]`/`[ ]` task-list convention instead, but this isn't a fixed-width
    # font, so swapping `"x"` for `" "` inside literal brackets shifted the trailing words -- the two
    # ballot-box glyphs render at identical bounding-box width in this font, so this swap is
    # genuinely static, not just visually close. Deliberately only the glyph changes between frames,
    # not the surrounding words -- per explicit user feedback, the goal is for the blinking GIF to
    # visually read as a checkbox ticking on/off in place, not as title text jumping around alongside
    # the image.
    if overlay_label is not None:
        base_title = f"{title}: ☐ {overlay_label}"
        overlay_title = f"{title}: ☑ {overlay_label}"
    else:
        base_title = f"{title} - ☐ Overlay Visibility"
        overlay_title = f"{title} - ☑ Overlay Visibility"

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
        base_title,
        outline_geoseries,
        overlay_outline_color,
        layers,
        margin_frac,
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
        overlay_title,
        outline_geoseries,
        overlay_outline_color,
        layers,
        margin_frac,
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
    layers: list[OverlayLayer] | None = None,
    margin_frac: float = 0.3,
):
    """Render one `_render_overlay_figure(...)` frame to a `PIL.Image`.

    :param base: Base `DataArray`.
    :param overlay_display: Overlay `DataArray`.
    :param base_vmin: Base display stretch minimum.
    :param base_vmax: Base display stretch maximum.
    :param overlay_vmin: Overlay display stretch minimum.
    :param overlay_vmax: Overlay display stretch maximum.
    :param overlay_cmap: Overlay colormap.
    :param overlay_alpha: Overlay opacity.
    :param title: Frame title.
    :param outline_geoseries: Overlay footprint outline to draw, or `None` to skip.
    :param overlay_outline_color: Outline color.
    :param layers: Additional vector annotation layers.
    :param margin_frac: See `plot_overlay`'s docstring.
    :returns: `(image, width_px, height_px)`.
    """
    # Deliberately no `bbox_inches="tight"` and no per-call `dpi=` override on `savefig` -- both
    # frames must use plain, consistent full-figure export so the two frames `plot_overlay_toggle`
    # produces are pixel-dimension-identical (a content-dependent tight-bbox crop could differ
    # between the transparent and opaque frames, breaking the GIF's frame alignment). `plt.close(fig)`
    # is required, not cleanup hygiene: the notebook's inline matplotlib backend auto-displays any
    # figure left open at cell-end, so without it this would leak two extra static images into the
    # cell's output alongside the intended GIF.
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
        layers,
        margin_frac,
    )
    width_px, height_px = fig.get_size_inches() * fig.dpi
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB"), round(width_px), round(height_px)


def _blink_gif_b64(base_frame, overlay_frame, initial_visible: bool, interval_ms: int) -> str:
    """Encode `base_frame`/`overlay_frame` as a base64-encoded, looping animated GIF.

    :param base_frame: A `PIL.Image` (from `_render_overlay_frame`).
    :param overlay_frame: A same-size `PIL.Image` (from `_render_overlay_frame`).
    :param initial_visible: Whether `overlay_frame` plays first.
    :param interval_ms: Milliseconds per frame.
    :returns: The base64-encoded GIF.
    """
    # Both frames are quantized onto one shared 256-color palette (built from the two frames pasted
    # side by side, then each re-quantized onto that same palette via `Image.quantize(palette=...)`)
    # rather than each picking its own independently -- letting GIF encoding choose per-frame
    # palettes would recolor the unchanged base-image pixels slightly differently in each frame,
    # showing up as a flicker across the whole image on every blink instead of only where the overlay
    # actually differs.
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


def plot_render_toggle(
    raster_a_path,
    raster_b_path,
    rotation_k: int,
    width_km: float,
    height_km: float,
    label_a: str,
    label_b: str,
    show_a_first: bool = True,
    blink_interval_ms: int = 700,
):
    """Blink comparator for two renders that already share the exact same pixel grid by
    construction -- e.g. `hillshade` vs. `reproject`, both `sat_sim` renders through one shared
    `Camera`.

    :param raster_a_path: First render raster path.
    :param raster_b_path: Second render raster path.
    :param rotation_k: North-up display rotation, shared by both (same pose, same corrected FOV --
        see `camera.solve_corrected_fov`'s docstring).
    :param width_km: Displayed width, km.
    :param height_km: Displayed height, km.
    :param label_a: First render's label.
    :param label_b: Second render's label.
    :param show_a_first: Which frame plays first in the loop.
    :param blink_interval_ms: How long each frame is shown before switching.
    :returns: An `IPython.display.HTML` object -- same requirements/caveats as
        `plot_overlay_toggle`'s own `:returns:` (bare last expression, GIF-rendering viewer needed).
    """
    # Unlike `plot_overlay_toggle`, this needs no `rioxarray`/geo-registration step at all:
    # `raster_a_path`/`raster_b_path` are read as plain arrays (`read_raster_band`) and rotated
    # north-up with the one shared `rotation_k`, the same technique `plot_render_vs_basemap`'s render
    # panel uses -- no reprojection, since there's nothing to align. No tie-point markers -- unlike
    # the other panels in this notebook, the whole point here is the blink itself; static markers
    # didn't actually help read it and just added clutter.
    #
    # Each render independently normalized to its own median = 1.0 (`robust_median`/
    # `normalize_to_median`, same technique `_prep_overlay_rasters` uses), not `raster_b_path`
    # matched to `raster_a_path`'s absolute level -- necessary even though both are `sat_sim`
    # renders, since the two can land on very different absolute DN scales depending on their own
    # texture source (e.g. an ISIS-calibrated I/F input, ~0.01-0.2, vs. a synthetic basemap
    # texture), and matching one to the other's fixed range risks oversaturating whichever side
    # needed the bigger correction.
    #
    # Reuses `_blink_gif_b64` directly for the actual GIF encoding (shared 256-color palette, `<img
    # src="data:image/gif;...">`, no `<style>`/anchor links for GitHub's sanitizer to strip -- see
    # `plot_overlay_toggle`'s docstring for why that mechanism specifically) -- only the
    # frame-rendering step differs (plain rotated arrays here vs. geo-registered `rioxarray` panels
    # there).
    #
    # Each frame's title shows both labels with a `☑`/`☐` checkbox glyph marking which one is
    # currently showing (`"☑ {label_a} / ☐ {label_b}"`, flipped on the other frame) -- the same
    # stable-width checkbox convention `plot_overlay_toggle` uses (only the two glyphs swap in place;
    # `label_a`/`label_b` themselves never move), generalized from an on/off binary to naming which
    # of two candidates is on screen.
    data_a = read_raster_band(raster_a_path)
    data_b = read_raster_band(raster_b_path)

    valid_a = valid_pixel_mask(data_a)
    valid_b = valid_pixel_mask(data_b)
    filled_a = _fill_dead_columns_for_display(data_a, valid_a) if valid_a.any() else data_a
    filled_b = _fill_dead_columns_for_display(data_b, valid_b) if valid_b.any() else data_b

    norm_a = normalize_to_median(filled_a, robust_median(filled_a[valid_a]) if valid_a.any() else float("nan"))
    norm_b = normalize_to_median(filled_b, robust_median(filled_b[valid_b]) if valid_b.any() else float("nan"))

    # vmax is the larger of the two renders' own post-normalization 99.9th percentile, not raster_a's
    # alone -- so whichever side needed the bigger correction still gets enough headroom for its own
    # highlights.
    vmax_a = np.nanpercentile(norm_a[valid_a], 99.9) if valid_a.any() else 0.0
    vmax_b = np.nanpercentile(norm_b[valid_b], 99.9) if valid_b.any() else 0.0
    vmin, vmax = 0, max(vmax_a, vmax_b)

    def _frame(data, title):
        fig, ax = plt.subplots(figsize=(6, 6))
        ax.imshow(np.rot90(data, rotation_k), cmap="gray", vmin=vmin, vmax=vmax, extent=(0, width_km, height_km, 0))
        ax.set_title(title)
        ax.set_xlabel("km")
        ax.set_ylabel("km")
        fig.tight_layout()
        width_px, height_px = fig.get_size_inches() * fig.dpi
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0)
        return Image.open(buf).convert("RGB"), round(width_px), round(height_px)

    frame_a, width_px, height_px = _frame(norm_a, f"☑ {label_a} / ☐ {label_b}")
    frame_b, _, _ = _frame(norm_b, f"☐ {label_a} / ☑ {label_b}")

    # _blink_gif_b64(base_frame, overlay_frame, initial_visible) plays overlay_frame first when
    # initial_visible -- so frame_b (the "overlay" slot) must be passed first here for
    # show_a_first=True to actually play frame_a first, matching this parameter's own name.
    gif_b64 = _blink_gif_b64(frame_b, frame_a, show_a_first, blink_interval_ms)
    html = f'<img src="data:image/gif;base64,{gif_b64}" width="{width_px}" height="{height_px}">'
    return IPython.display.HTML(html)


def plot_zoom_blink(
    raster_a_path,
    raster_b_path,
    label_a: str,
    label_b: str,
    crop_px: int = 200,
    show_a_first: bool = True,
    blink_interval_ms: int = 700,
):
    """`plot_render_toggle`'s blink comparator, for two geo-aligned, **map-projected** rasters
    (different native pixel grids, e.g. `plot_overlay`'s own `overlay_raster_path` for two different
    candidates) instead of two same-grid renders -- and restricted to a full-resolution square crop
    from the middle of `raster_b_path`'s own footprint, since compressing a whole footprint into one
    fixed-size figure (as `plot_overlay_toggle` does) hides real per-pixel detail.

    :param raster_a_path: First map-projected raster path -- reindexed onto `raster_b_path`'s pixel
        grid (nearest-neighbor, half-a-pixel tolerance, same as `compute_brightness_matched_diff`).
    :param raster_b_path: Second, reference map-projected raster path. Its own native pixel grid
        drives both the crop window and the display resolution -- pass whichever raster's footprint
        is trustworthy to center on (e.g. a candidate's own render/crop, not a padded basemap AOI
        that isn't guaranteed to be centered on it).
    :param label_a: First raster's label (e.g. `mathtt("hillshade")`, matching
        `image_generation.ipynb`'s own short-generator-name title convention).
    :param label_b: Second raster's label.
    :param crop_px: Square crop width/height, `raster_b_path`'s own pixels, taken from the middle of
        its array. Named in each frame's own `"Zoomed ({crop_px} px): ..."` title prefix.
    :param show_a_first: Which frame plays first in the loop.
    :param blink_interval_ms: How long each frame is shown before switching.
    :returns: An `IPython.display.HTML` object -- same requirements/caveats as
        `plot_overlay_toggle`'s own `:returns:` (bare last expression, GIF-rendering viewer needed).
    """
    # Reuses plot_overlay_toggle's own geo-alignment (open_raster_dataarray, reindex_like) and
    # normalization (robust_median/normalize_to_median -- see _prep_overlay_rasters's docstring
    # for why) plus _blink_gif_b64's shared-palette GIF encoding; only the windowing (a small square
    # crop, not the whole footprint) and axis handling (real Easting/Northing km ticks, not a
    # 0-based pixel extent -- these are already-georeferenced map-projected rasters, unlike
    # plot_render_toggle's raw sensor-pixel arrays) differ.
    #
    # raster_b_path anchors the crop, not raster_a_path -- e.g. TrnTestImage.plot_zoom_blink_over
    # always passes its own already-generated render/crop as raster_b, since that's the one raster
    # guaranteed to be centered on the actual candidate footprint; a padded/unioned basemap AOI's own
    # array center can sit several km off that footprint's true center (confirmed live: up to ~10km
    # for this project's default candidate), so anchoring on it instead could crop mostly-nodata.
    a = open_raster_dataarray(raster_a_path)
    b = open_raster_dataarray(raster_b_path)

    half = crop_px // 2
    cy, cx = b.sizes["y"] // 2, b.sizes["x"] // 2
    b_crop = b.isel(y=slice(max(0, cy - half), cy - half + crop_px), x=slice(max(0, cx - half), cx - half + crop_px))

    tolerance = cellsize_m(b) / 2.0
    a_crop = a.reindex_like(b_crop, method="nearest", tolerance=tolerance)

    # Each side independently normalized to its own median = 1.0, not a_crop matched to b_crop's
    # absolute level -- see _prep_overlay_rasters's own docstring for why.
    b_crop = normalize_to_median(b_crop, robust_median(b_crop.values))
    a_crop = normalize_to_median(a_crop, robust_median(a_crop.values))

    # vmin=0 (not an affine min-max stretch -- an affine stretch would clip genuinely dark-but-real
    # terrain to black), vmax the larger of the two sides' own post-normalization 99.9th percentile
    # so whichever side needed the bigger correction still gets enough headroom for its own
    # highlights, instead of clipping against a ceiling sized only for the other side.
    vmin = 0
    vmax = max(np.nanpercentile(a_crop.values, 99.9), np.nanpercentile(b_crop.values, 99.9))
    km_formatter = matplotlib.ticker.FuncFormatter(lambda x, _: f"{x / 1000:.1f}")

    def _frame(data, title):
        fig, ax = plt.subplots(figsize=(6, 6))
        data.plot.imshow(ax=ax, cmap="gray", vmin=vmin, vmax=vmax, add_colorbar=False)
        ax.xaxis.set_major_formatter(km_formatter)
        ax.yaxis.set_major_formatter(km_formatter)
        ax.set_xlabel("Easting (km)")
        ax.set_ylabel("Northing (km)")
        ax.set_title(title)
        fig.tight_layout()
        width_px, height_px = fig.get_size_inches() * fig.dpi
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0)
        return Image.open(buf).convert("RGB"), round(width_px), round(height_px)

    prefix = f"Zoomed ({crop_px} px): "
    frame_a, width_px, height_px = _frame(a_crop, f"{prefix}☑ {label_a} / ☐ {label_b}")
    frame_b, _, _ = _frame(b_crop, f"{prefix}☐ {label_a} / ☑ {label_b}")

    # _blink_gif_b64(base_frame, overlay_frame, initial_visible) plays overlay_frame first when
    # initial_visible -- so frame_b (the "overlay" slot) must be passed first here for
    # show_a_first=True to actually play frame_a first, matching this parameter's own name.
    gif_b64 = _blink_gif_b64(frame_b, frame_a, show_a_first, blink_interval_ms)
    html = f'<img src="data:image/gif;base64,{gif_b64}" width="{width_px}" height="{height_px}">'
    return IPython.display.HTML(html)
