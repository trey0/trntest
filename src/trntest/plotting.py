"""Matplotlib display helpers for the notebook. No SPICE/network/subprocess calls -- pure
consumption of already-computed values, reading image files by path where needed."""

import warnings

import matplotlib.pyplot as plt
import numpy as np
import rasterio
import rasterio.errors
import rasterio.transform

from trntest import orientation, wac
from trntest.camera import Camera
from trntest.config import TrntestConfig, load_config
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


def _open_rendered_tif(rendered_tif_path):
    """`rasterio.open` for the synthetic `sat_sim` render specifically -- it's a plain pinhole
    render, not a georeferenced product, so it genuinely has no geotransform (expected, not a bug).
    Suppresses just that one, otherwise-noisy, non-actionable warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        return rasterio.open(rendered_tif_path)


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
    with _open_rendered_tif(rendered_tif_path) as src:
        synthetic = src.read(1)

    fig = plt.figure(figsize=(5, 5))
    plt.imshow(synthetic, cmap="gray")
    plt.title("Synthetic sat_sim render")
    plt.xlabel("sample")
    plt.ylabel("line")
    return fig


def plot_comparison(
    camera: Camera,
    tie_point_results: dict,
    vis_mosaic: np.ndarray,
    rotations: DisplayRotations,
    rendered_tif_path,
    config: TrntestConfig | None = None,
):
    config = config or load_config()

    with _open_rendered_tif(rendered_tif_path) as src:
        synthetic = src.read(1)

    valid_mask = vis_mosaic != wac.MISSING_CONSTANT
    p2, p98 = np.percentile(vis_mosaic[valid_mask], [2, 98])
    display_mosaic = np.where(valid_mask, vis_mosaic, p2)  # fill missing edge columns with the low end of the stretch

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
    axes[0].imshow(synthetic_rot, cmap="gray", extent=[0, synthetic_width_km, synthetic_width_km, 0])
    axes[0].set_title("Synthetic (sat_sim, SPICE-posed, north-up)")
    axes[1].imshow(mosaic_rot, cmap="gray", vmin=p2, vmax=p98, extent=[0, crop_width_km, crop_height_km, 0])
    axes[1].set_title("Real WAC CDR, band-separated (I/F, contrast-stretched, north-up)")
    for ax in axes:
        ax.set_xlabel("km")
        ax.set_ylabel("km")

    for name, r in tie_point_results.items():
        style = MARKER_STYLES[name]
        px, py = r["synthetic_px"]
        px_r, py_r = orientation.rotate_pixel_coords(px, py, rotations.k_synthetic, h_syn, w_syn)
        axes[0].plot(
            px_r / config.image_size * synthetic_width_km,
            py_r / config.image_size * synthetic_width_km,
            markersize=14,
            markeredgecolor="black",
            markeredgewidth=1.5,
            **style,
        )
        col, row = r["crop_px"]
        col_r, row_r = orientation.rotate_pixel_coords(col, row, rotations.k_crop, h_crop, w_crop)
        axes[1].plot(
            col_r / wac.SAMPLES * crop_width_km,
            row_r / (n_frames * wac.VIS_BLOCK_HEIGHT) * crop_height_km,
            markersize=14,
            markeredgecolor="black",
            markeredgewidth=1.5,
            **style,
        )

    fig.tight_layout()
    return fig
