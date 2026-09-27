"""Cast-shadow plots for `notebooks/sun_aligned_shadow_sweep.ipynb` -- split out of `plotting.py` since
none of them are needed by the generator-comparison/report path, only by the notebook that shows how
`cast_shadow`'s illumination fraction behaves and how it compares against ISIS `shadow` and real WAC
imagery.
"""

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LightSource


def lambertian_hillshade(dem: np.ndarray, azimuth_deg: float, elevation_deg: float, cellsize_m: float) -> np.ndarray:
    """A plain per-facet Lambertian hillshade of `dem` (no cast shadows) -- the base layer these plots
    draw shadow masks over.

    :param dem: Elevation, meters.
    :param azimuth_deg: Sun azimuth, degrees.
    :param elevation_deg: Sun elevation, degrees.
    :param cellsize_m: DEM pixel size, meters.
    :returns: `[0, 1]` hillshade, `dem`'s shape.
    """
    return LightSource(azdeg=azimuth_deg, altdeg=elevation_deg).hillshade(
        np.asarray(dem, dtype=np.float64), dx=cellsize_m, dy=cellsize_m
    )


def plot_illumination_composite(hillshade: np.ndarray, illumination_fraction: np.ndarray, title: str | None = None):
    """Per-facet hillshade | cast-shadow illumination fraction | their product.

    :param hillshade: `lambertian_hillshade`'s output.
    :param illumination_fraction: `cast_shadow.illumination_fraction`'s output.
    :param title: Optional figure title.
    :returns: The `Figure`.
    """
    fig, axes = plt.subplots(1, 3, figsize=(21, 7), constrained_layout=True)
    axes[0].imshow(hillshade, cmap="gray", vmin=0, vmax=1)
    axes[0].set_title("Plain Lambertian hillshade")
    axes[1].imshow(illumination_fraction, cmap="gray", vmin=0, vmax=1)
    axes[1].set_title("Sun-aligned sweep: illumination fraction")
    axes[2].imshow(hillshade * illumination_fraction, cmap="gray", vmin=0, vmax=1)
    axes[2].set_title("Composite: hillshade x illumination")
    for ax in axes:
        ax.axis("off")
    if title:
        fig.suptitle(title)
    return fig


def plot_mask_crop_comparison(
    hillshade: np.ndarray,
    isis_lit: np.ndarray,
    sweep_illumination: np.ndarray,
    row_slice: slice,
    col_slice: slice,
    title: str | None = None,
):
    """ISIS `shadow`'s mask (red) and the sweep's (blue) over the same hillshade crop, side by side.

    :param hillshade: `lambertian_hillshade`'s output.
    :param isis_lit: Boolean, `True` where ISIS `shadow` reports lit.
    :param sweep_illumination: The sweep's illumination fraction (with self-shadow folded in, to match
        ISIS's combined semantics).
    :param row_slice: Crop rows.
    :param col_slice: Crop columns.
    :param title: Optional figure title.
    :returns: The `Figure`.
    """
    sub = hillshade[row_slice, col_slice]
    fig, axes = plt.subplots(1, 2, figsize=(14, 7), constrained_layout=True)
    isis_overlay = np.stack([sub] * 3, axis=-1)
    isis_overlay[..., 0] = np.clip(isis_overlay[..., 0] + (~isis_lit)[row_slice, col_slice] * 0.6, 0, 1)
    axes[0].imshow(isis_overlay)
    axes[0].set_title("ISIS shadow (red)")
    sweep_overlay = np.stack([sub] * 3, axis=-1)
    sweep_overlay[..., 2] = np.clip(
        sweep_overlay[..., 2] + np.nan_to_num(1.0 - sweep_illumination[row_slice, col_slice]) * 0.7, 0, 1
    )
    axes[1].imshow(sweep_overlay)
    axes[1].set_title("Sun-aligned sweep (blue)")
    for ax in axes:
        ax.axis("off")
    if title:
        fig.suptitle(title)
    return fig


def plot_row_mean_shadow_fraction(isis_lit: np.ndarray, sweep_illumination: np.ndarray, title: str | None = None):
    """Row-mean shadow fraction, ISIS `shadow` vs. the sweep -- single-row spikes in one curve and not
    the other are row-level streaks in that mask.

    :param isis_lit: Boolean, `True` where ISIS `shadow` reports lit.
    :param sweep_illumination: The sweep's illumination fraction (self-shadow folded in).
    :param title: Optional figure title.
    :returns: The `Figure`.
    """
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(1.0 - isis_lit.mean(axis=1), label="ISIS shadow, row-mean shadow fraction", alpha=0.8)
    ax.plot(
        1.0 - np.nanmean(sweep_illumination, axis=1), label="Sun-aligned sweep, row-mean shadow fraction", alpha=0.8
    )
    ax.set_xlabel("row")
    ax.set_ylabel("shadow fraction")
    ax.legend()
    if title:
        ax.set_title(title)
    return fig


def plot_strip_vs_wac(
    wac_on_dem_grid: np.ndarray,
    hillshade: np.ndarray,
    illumination_fraction: np.ndarray,
    rows: slice,
    cols: slice,
    marked_row: int,
    title: str | None = None,
):
    """A thin strip at strict 1:1 pixel scale: real WAC | hillshade | sweep, with one row marked.

    :param wac_on_dem_grid: `wac_resample.crop_reflectance_on_dem_grid`'s output.
    :param hillshade: `lambertian_hillshade`'s output.
    :param illumination_fraction: The sweep's illumination fraction.
    :param rows: Strip rows.
    :param cols: Strip columns.
    :param marked_row: Absolute row index to mark with a red line.
    :param title: Optional figure title.
    :returns: The `Figure`.
    """
    wac_strip = wac_on_dem_grid[rows, cols]
    valid = wac_strip[np.isfinite(wac_strip)]
    vmin, vmax = np.percentile(valid, [2, 98]) if valid.size else (0, 1)
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), constrained_layout=True)
    strip_kwargs = {"cmap": "gray", "aspect": "auto", "interpolation": "none"}
    axes[0].imshow(wac_strip, vmin=vmin, vmax=vmax, **strip_kwargs)
    axes[0].set_title("Real WAC crop (calibrated reflectance, reprojected)")
    axes[1].imshow(hillshade[rows, cols], **strip_kwargs)
    axes[1].set_title("Plain Lambertian hillshade")
    axes[2].imshow(illumination_fraction[rows, cols], vmin=0, vmax=1, **strip_kwargs)
    axes[2].set_title("Sun-aligned sweep: illumination fraction")
    for ax in axes:
        ax.axhline(marked_row - rows.start, color="red", lw=0.5, alpha=0.7)
    if title:
        fig.suptitle(title)
    return fig
