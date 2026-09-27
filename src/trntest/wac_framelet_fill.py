"""Fills the NULL pixels `lrowaccal` leaves on the first line of every WAC VIS framelet, before a
crop is map-projected. Two fills: row-wise interpolation along the line, and a donor fill from the
previous framelet, whose last lines overlap the same ground. See
`notebooks/wac_framelet_null_fill.py` for how the two compare.

Works on a framestitched cube (or a crop of one) as a float array with NaN marking invalid pixels,
line 0 being a framelet's first line.
"""

import dataclasses
import shutil
import tempfile
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from scipy.ndimage import map_coordinates

from trntest import isis_campt
from trntest.dem_ortho import DemOrthoResult
from trntest.isis_wac import _orthographic_map_pvl
from trntest.plotting import read_raster_band, valid_pixel_mask
from trntest.pose_alignment import wac_camera_model
from trntest.subprocess_utils import run_quiet
from trntest.tie_points import lonlat_to_ground_km
from trntest.wac_format import VIS_BLOCK_HEIGHT

ISIS_NULL = np.float32(-3.4028226550889045e38)


def read_band(cub_path: Path, band: int = 1) -> np.ndarray:
    """One band of a cube as float32, NaN where not a valid pixel.

    :param cub_path: ISIS cube (or any GDAL-readable raster).
    :param band: 1-based band index.
    :returns: The band.
    """
    data = read_raster_band(cub_path, band).astype(np.float32)
    return np.where(valid_pixel_mask(data), data, np.nan)


def read_all_bands(cub_path: Path) -> list[np.ndarray]:
    """Every band of a cube, as `read_band` reads each.

    :param cub_path: ISIS cube (or any GDAL-readable raster).
    :returns: One array per band, band 1 first.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        with rasterio.open(cub_path) as src:
            n_bands = src.count
    return [read_band(cub_path, band) for band in range(1, n_bands + 1)]


def framelet_boundary_nulls(band: np.ndarray, framelet_height: int = VIS_BLOCK_HEIGHT) -> np.ndarray:
    """Invalid pixels on framelet first lines, excluding columns invalid on every line.

    :param band: Float array, NaN where invalid.
    :param framelet_height: Lines per framelet.
    :returns: Boolean mask, `band`'s shape.
    """
    invalid = np.isnan(band)
    always_dead = invalid.all(axis=0)
    mask = np.zeros_like(invalid)
    mask[::framelet_height] = invalid[::framelet_height] & ~always_dead
    return mask


def fill_row_interp(band: np.ndarray, fill_mask: np.ndarray) -> np.ndarray:
    """Linear interpolation along each line across `fill_mask` pixels.

    :param band: Float array, NaN where invalid.
    :param fill_mask: Pixels to fill.
    :returns: A filled copy of `band`.
    """
    filled = band.copy()
    cols = np.arange(band.shape[1])
    for row in np.flatnonzero(fill_mask.any(axis=1)):
        good = np.isfinite(band[row]) & ~fill_mask[row]
        if good.any():
            targets = fill_mask[row]
            filled[row, targets] = np.interp(cols[targets], cols[good], band[row, good])
    return filled


@dataclasses.dataclass(frozen=True)
class FrameletOverlap:
    """Where a framelet's first line falls within the previous framelet, per column.

    `line_offset[c]` is the (fractional) line of the previous framelet, counted from its own first
    line, that sees the same ground as column `c` of this framelet's first line; `sample_shift[c]`
    is the matching cross-track shift in samples.
    """

    line_offset: np.ndarray
    sample_shift: np.ndarray
    block_centers: np.ndarray
    block_rmse: np.ndarray


def _previous_framelet_samples(
    band: np.ndarray, rows: np.ndarray, cols: np.ndarray, line_offset, sample_shift, framelet_height: int
) -> np.ndarray:
    lines = rows - framelet_height + line_offset
    samples = cols + sample_shift
    return map_coordinates(band, [lines, samples], order=1, mode="nearest")


def fit_framelet_overlap(
    band: np.ndarray,
    framelet_height: int = VIS_BLOCK_HEIGHT,
    block_width: int = 64,
    line_offsets: np.ndarray | None = None,
    sample_shifts: np.ndarray | None = None,
) -> FrameletOverlap:
    """Fit `FrameletOverlap` by grid search, per block of columns, minimizing the mean squared
    difference between every framelet's first line and the previous framelet resampled at that
    offset. Linearly interpolated between block centers.

    :param band: Float array, NaN where invalid. At least two framelets tall.
    :param framelet_height: Lines per framelet.
    :param block_width: Columns per fitted block.
    :param line_offsets: Candidate line offsets; default 8 to `framelet_height - 1` in 0.1 steps.
    :param sample_shifts: Candidate sample shifts; default -2 to 2 in 0.25 steps.
    :returns: The fitted overlap.
    """
    if line_offsets is None:
        line_offsets = np.arange(8.0, framelet_height - 1 + 1e-9, 0.1)
    if sample_shifts is None:
        sample_shifts = np.arange(-2.0, 2.0 + 1e-9, 0.25)
    n_rows, n_cols = band.shape
    first_lines = np.arange(framelet_height, n_rows, framelet_height)
    centers, offsets, shifts, rmses = [], [], [], []
    for start in range(0, n_cols, block_width):
        cols = np.arange(start, min(start + block_width, n_cols))
        rows_grid, cols_grid = np.meshgrid(first_lines, cols, indexing="ij")
        target = band[rows_grid, cols_grid]
        best = (np.inf, np.nan, np.nan)
        for offset in line_offsets:
            for shift in sample_shifts:
                donor = _previous_framelet_samples(band, rows_grid, cols_grid, offset, shift, framelet_height)
                mse = np.nanmean((donor - target) ** 2)
                if mse < best[0]:
                    best = (mse, offset, shift)
        centers.append(cols.mean())
        rmses.append(np.sqrt(best[0]))
        offsets.append(best[1])
        shifts.append(best[2])
    all_cols = np.arange(n_cols)
    return FrameletOverlap(
        line_offset=np.interp(all_cols, centers, offsets),
        sample_shift=np.interp(all_cols, centers, shifts),
        block_centers=np.array(centers),
        block_rmse=np.array(rmses),
    )


def camera_model_overlap(
    crop_cub: Path, framelet_step: int = 6, column_step: int = 24, framelet_height: int = VIS_BLOCK_HEIGHT
) -> tuple[FrameletOverlap, pd.DataFrame]:
    """`FrameletOverlap` predicted by the camera model instead of fitted to pixels: first-line pixels
    of sampled framelets go to ground via `campt`, then into the previous framelet via
    `wac_camera_model`. SPICE kernels for the crop must already be furnished.

    :param crop_cub: Crop cube.
    :param framelet_step: Use every this-many framelets, starting at the second.
    :param column_step: Use every this-many columns.
    :param framelet_height: Lines per framelet.
    :returns: The overlap (per-column means, interpolated), and one row per sampled pixel with
        `framelet`, `column`, `line_offset`, `sample_shift`, plus `self_*` round-trip residuals from
        projecting back into the pixel's own framelet (should be ~0).
    """
    n_rows, n_cols = read_band(crop_cub).shape
    et0, et_per_line = wac_camera_model.calibrate_et_per_crop_line(crop_cub, n_rows)
    framelets = np.arange(1, n_rows // framelet_height, framelet_step)
    columns = np.arange(column_step // 2, n_cols, column_step)
    # ISIS 1-based pixel centers.
    pixels = np.array([(c + 1.0, k * framelet_height + 1.0) for k in framelets for c in columns])
    grounds = isis_campt.image_to_ground_points_batch(crop_cub, pixels)
    rows = []
    for (sample, line), ground in zip(pixels, grounds, strict=True):
        if ground is None:
            continue
        lon_deg, lat_deg, radius_m = ground
        ground_me_m = lonlat_to_ground_km(lon_deg, lat_deg, radius_m / 1000.0) * 1000.0
        k = int((line - 1) // framelet_height)
        self_sample, self_line = wac_camera_model.project_at_framelet(ground_me_m, k, et0, et_per_line)
        prev_sample, prev_line = wac_camera_model.project_at_framelet(ground_me_m, k - 1, et0, et_per_line)
        rows.append(
            {
                "framelet": k,
                "column": int(sample - 1),
                "line_offset": prev_line - 1.0,
                "sample_shift": prev_sample - sample,
                "self_line_error": self_line - 1.0,
                "self_sample_error": self_sample - sample,
            }
        )
    points = pd.DataFrame(rows)
    per_column = points.groupby("column")[["line_offset", "sample_shift"]].mean()
    all_cols = np.arange(n_cols)
    overlap = FrameletOverlap(
        line_offset=np.interp(all_cols, per_column.index, per_column["line_offset"]),
        sample_shift=np.interp(all_cols, per_column.index, per_column["sample_shift"]),
        block_centers=per_column.index.to_numpy(dtype=float),
        block_rmse=np.full(len(per_column), np.nan),
    )
    return overlap, points


def fill_from_previous_framelet(
    band: np.ndarray, fill_mask: np.ndarray, overlap: FrameletOverlap, framelet_height: int = VIS_BLOCK_HEIGHT
) -> np.ndarray:
    """Fill `fill_mask` pixels from the previous framelet's overlapping pixel, per `overlap`. Pixels
    with no usable donor (first framelet, or an invalid donor) fall back to `fill_row_interp`.

    :param band: Float array, NaN where invalid.
    :param fill_mask: Pixels to fill.
    :param overlap: `fit_framelet_overlap`'s result for this band.
    :param framelet_height: Lines per framelet.
    :returns: A filled copy of `band`.
    """
    filled = band.copy()
    rows, cols = np.nonzero(fill_mask)
    has_previous = rows >= framelet_height
    rows, cols = rows[has_previous], cols[has_previous]
    donor = _previous_framelet_samples(
        band, rows, cols, overlap.line_offset[cols], overlap.sample_shift[cols], framelet_height
    )
    filled[rows, cols] = donor
    return fill_row_interp(filled, fill_mask & np.isnan(filled))


def write_filled_cube(src_cub: Path, dst_cub: Path, filled: np.ndarray, band: int = 1) -> Path:
    """Copy `src_cub` to `dst_cub` (labels, SPICE tables and all), replacing one band's pixels.

    :param src_cub: Source ISIS cube.
    :param dst_cub: Destination path.
    :param filled: Replacement pixels, NaN written as ISIS NULL.
    :param band: 1-based band index.
    :returns: `dst_cub`.
    """
    shutil.copy(src_cub, dst_cub)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        with rasterio.open(dst_cub, "r+") as dst:
            dst.write(np.where(np.isnan(filled), ISIS_NULL, filled).astype(np.float32), band)
    return dst_cub


def cam2map_source_pixels(
    crop_cub: Path, dem_ortho_result: DemOrthoResult, out_tif: Path
) -> tuple[np.ndarray, np.ndarray]:
    """For each pixel of `isis_wac.run_cam2map_for_crop`'s output grid, the crop pixel it was drawn
    from: `cam2map` (same arguments, nearest-neighbor) of a copy of `crop_cub` whose band 1 holds each
    pixel's own flat index.

    :param crop_cub: Crop cube.
    :param dem_ortho_result: The map projection to clone, as for `run_cam2map_for_crop`.
    :param out_tif: Where to write the single-band flat-index GeoTIFF.
    :returns: `(line, sample)`, 0-based crop indices per output pixel, NaN outside coverage.
    """
    # One band, not line and sample in two: `cam2map` maps each WAC filter band through that band's
    # own camera geometry, so a second band's trace wouldn't describe band 1's.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        n_rows, n_cols = read_band(crop_cub).shape
        flat_index = np.arange(n_rows * n_cols, dtype=np.float32).reshape(n_rows, n_cols)  # exact below 2**24
        index_cub = write_filled_cube(crop_cub, tmp_dir / "pixel_index.cub", flat_index)
        map_path = tmp_dir / "ortho.map"
        map_path.write_text(_orthographic_map_pvl(dem_ortho_result))
        mapped = tmp_dir / "pixel_index-cam2map.cub"
        run_quiet(
            [
                "cam2map",
                f"from={index_cub}",
                f"map={map_path}",
                f"to={mapped}",
                "pixres=map",
                "defaultrange=camera",
                "warpalgorithm=forwardpatch",
                "patchsize=1",
                "interp=nearestneighbor",
            ]
        )
        run_quiet(["gdal_translate", "-b", "1", "-mask", "none", str(mapped), str(out_tif)])
    traced = read_band(out_tif)
    return np.floor(traced / n_cols), traced % n_cols
