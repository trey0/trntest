"""Map-projects a WAC VIS crop without `cam2map`: for each output map pixel, find the framelet that
sees its ground point, and sample that framelet alone. `map_project_crop` is the entry point; it
uses this or `isis_wac.run_cam2map_for_crop` per `TrntestConfig.crop_map_projection`. Replaces
`cam2map` because `cam2map` lets each framelet's first line (NULL in 53 columns) win over the
previous framelet's overlap and misplaces each framelet's last line by 3-5 map px along-track; see
`notebooks/wac_framelet_null_fill.py`.

Framelet choice in an overlap is the mid-overlap seam: whichever framelet puts the point closest to
its own center line, unless that framelet's pixels are NULL there, in which case the other one.
Interpolation is cubic convolution (Keys, a = -0.5) and never reaches
across a framelet boundary. Where no overlapping framelet has all 16 cubic taps valid, it falls back
to bilinear, and where no framelet has all four bilinear taps valid either (next to the detector's
dead edge columns, or a NULL line no other framelet covers), it averages the valid taps instead, as
long as the pixel containing the point is valid.

Geometry is `pose_alignment.wac_camera_model`'s optics chain, vectorized, and ground heights come from
the same ISIS lunar shape model `spiceinit` attaches to the crop. `resample_crop` needs the crop's
SPICE kernels already furnished; `resample_crop_to_map` furnishes them itself.
"""

import dataclasses
from pathlib import Path

import numpy as np
import rasterio
import rasterio.warp
from pyproj import Transformer
from scipy import ndimage

from trntest import isis_wac, spice_kernels
from trntest.camera import camera_pose_moon_me
from trntest.config import TrntestConfig, load_config
from trntest.dem_ortho import DemFetchResult, DemOrthoResult
from trntest.geo_utils import geographic_crs
from trntest.pose_alignment import wac_camera_model as wcm
from trntest.product_io import atomic_publish_path, writes_product
from trntest.wac_format import VIS_BLOCK_HEIGHT
from trntest.wac_framelet_fill import ISIS_NULL, read_band

_SHAPE_MODEL_RADIUS_BASE_M = 1737400.0
_SHAPE_MODEL_RADIUS_MULTIPLIER = 0.5
_CENTER_LINE = (VIS_BLOCK_HEIGHT + 1) / 2.0  # 1-based within-framelet line
_HALF_PIXEL = 0.5  # 1-based pixel centers: a pixel spans center ± this
# Cubic convolution parameter. -0.5 is the only value that reproduces linear and quadratic surfaces
# exactly. `cam2map`'s CUBICCONVOLUTION measures as -1: sharper-looking, from boosted high
# frequencies, but off by ~2% of a pixel step on a plane.
_KEYS_A = -0.5
_KEYS_SUPPORT = 2.0  # kernel half-width, px
# Largest enclosed hole `resample_crop_to_map` fills. Bigger ones mean something upstream went wrong,
# so they stay nodata and get reported.
_MAX_FILLED_HOLE_PX = 4


@dataclasses.dataclass(frozen=True)
class MapGrid:
    """An output map grid: shape, affine transform and CRS."""

    shape: tuple[int, int]
    transform: rasterio.Affine
    crs: rasterio.crs.CRS

    @staticmethod
    def from_raster(path: Path) -> "MapGrid":
        with rasterio.open(path) as src:
            return MapGrid(shape=(src.height, src.width), transform=src.transform, crs=src.crs)


@dataclasses.dataclass(frozen=True)
class Resampled:
    """`resample_crop`'s output, all on the map grid; NaN/-1 where nothing was sampled.

    `sample`/`line` are the chosen crop coordinates (ISIS 1-based, continuous `line` in the crop's
    own line numbering); `framelet` is the chosen framelet index.
    """

    value: np.ndarray
    framelet: np.ndarray
    sample: np.ndarray
    line: np.ndarray


def shape_model_radius_on_grid(grid: MapGrid, config: TrntestConfig | None = None) -> np.ndarray:
    """Body-fixed radius, meters, of ISIS's lunar shape model (`isis_wac.ensure_lunar_shape_model`),
    bilinearly resampled onto `grid`.

    :param grid: Output grid.
    :param config: Project config; `load_config()` if not given.
    :returns: Radius per grid pixel.
    """
    dn = np.full(grid.shape, np.nan, dtype=np.float64)
    with rasterio.open(isis_wac.ensure_lunar_shape_model(config or load_config())) as src:
        rasterio.warp.reproject(
            source=rasterio.band(src, 1),
            destination=dn,
            dst_transform=grid.transform,
            dst_crs=grid.crs,
            dst_nodata=np.nan,
            resampling=rasterio.warp.Resampling.bilinear,
        )
    return dn * _SHAPE_MODEL_RADIUS_MULTIPLIER + _SHAPE_MODEL_RADIUS_BASE_M


def grid_ground_points_me_m(grid: MapGrid, radius_m: np.ndarray) -> np.ndarray:
    """Body-fixed ground point, meters, at each grid pixel center.

    :param grid: Output grid.
    :param radius_m: Radius per grid pixel, `grid.shape`.
    :returns: `(rows, cols, 3)` array.
    """
    rows, cols = np.indices(grid.shape)
    x, y = rasterio.transform.xy(grid.transform, rows.ravel(), cols.ravel(), offset="center")
    to_geographic = Transformer.from_crs(grid.crs, geographic_crs(), always_xy=True)
    lon, lat = to_geographic.transform(np.asarray(x), np.asarray(y))
    lon, lat = np.radians(lon), np.radians(lat)
    r = radius_m.ravel()
    ground = np.stack([r * np.cos(lat) * np.cos(lon), r * np.cos(lat) * np.sin(lon), r * np.sin(lat)], axis=-1)
    return ground.reshape(*grid.shape, 3)


def _distort(ux_mm: np.ndarray, uy_mm: np.ndarray, max_iter: int = 20, tol: float = 1e-9):
    # Vectorized `wac_camera_model._distort`, solved for the distorted radius r by Newton's method
    # rather than that function's fixed-point iteration (same root, far fewer passes over millions
    # of points): r * (1 + k1 r^2 + k2 r^4 + k3 r^6) = undistorted radius. All k are positive, so the
    # left side increases monotonically and the root is unique.
    k1, k2, k3 = wcm.OD_K
    r_u = np.hypot(ux_mm, uy_mm)
    r = r_u.copy()
    for _ in range(max_iter):
        rr = r * r
        f = r * (1.0 + k1 * rr + k2 * rr**2 + k3 * rr**3) - r_u
        step = f / (1.0 + 3.0 * k1 * rr + 5.0 * k2 * rr**2 + 7.0 * k3 * rr**3)
        r = r - step
        if np.nanmax(np.abs(step)) < tol:
            break
    with np.errstate(invalid="ignore", divide="ignore"):
        scale = np.where(r_u > 0, r / r_u, 1.0)
    return ux_mm * scale, uy_mm * scale


def project(ground_me_m: np.ndarray, position_me_m: np.ndarray, r_cam_to_me: np.ndarray):
    """Vectorized `wac_camera_model.project_in_known_framelet`.

    :param ground_me_m: `(N, 3)` ground points.
    :param position_me_m: `(N, 3)` camera positions.
    :param r_cam_to_me: `(N, 3, 3)` camera-to-MOON_ME rotations.
    :returns: `(cube_sample, within_framelet_line)`, each `(N,)`, ISIS 1-based.
    """
    look_cam = np.einsum("nji,nj->ni", r_cam_to_me, ground_me_m - position_me_m)
    ux_mm = wcm.FOCAL_LENGTH_MM * look_cam[:, 0] / look_cam[:, 2]
    uy_mm = wcm.FOCAL_LENGTH_MM * look_cam[:, 1] / look_cam[:, 2]
    dx_mm, dy_mm = _distort(ux_mm, uy_mm)
    raw_sample = wcm.ITRANSS[0] + wcm.ITRANSS[1] * dx_mm + wcm.ITRANSS[2] * dy_mm + (wcm.BORESIGHT_SAMPLE + 1.0)
    raw_line = wcm.ITRANSL[0] + wcm.ITRANSL[1] * dx_mm + wcm.ITRANSL[2] * dy_mm + (wcm.BORESIGHT_LINE + 1.0)
    return raw_sample - wcm.COLOR_SAMPLE_OFFSET, raw_line - wcm.BAND_START_LINE


def framelet_poses(crop_cub: Path, n_lines: int) -> tuple[np.ndarray, np.ndarray]:
    """Camera position and rotation at each framelet of a crop.

    :param crop_cub: Crop cube.
    :param n_lines: Its line count.
    :returns: `(positions (F, 3), rotations (F, 3, 3))`.
    """
    et0, et_per_line = wcm.calibrate_et_per_crop_line(crop_cub, n_lines)
    poses = [camera_pose_moon_me(et0 + et_per_line * wcm.center_line(f)) for f in range(n_lines // VIS_BLOCK_HEIGHT)]
    return np.array([p[0] for p in poses]), np.array([p[1] for p in poses])


def _bilinear_within_framelet(band: np.ndarray, framelet: np.ndarray, sample: np.ndarray, within_line: np.ndarray):
    # Taps clamp to the framelet's own first/last line and the detector's first/last column, so the
    # interpolation never mixes in another framelet. Returns `(strict, partial)`: `strict` is NaN if
    # any tap with nonzero weight is invalid; `partial` renormalizes the weights over the valid taps,
    # but only where the pixel containing the point is itself valid, so it never extrapolates into a
    # dead or NULL pixel's ground.
    n_cols = band.shape[1]
    x = np.clip(sample - 1.0, 0.0, n_cols - 1.0)
    y = np.clip(within_line - 1.0, 0.0, VIS_BLOCK_HEIGHT - 1.0)
    x0 = np.minimum(np.floor(x).astype(int), n_cols - 2)
    y0 = np.minimum(np.floor(y).astype(int), VIS_BLOCK_HEIGHT - 2)
    fx, fy = x - x0, y - y0
    row0 = framelet * VIS_BLOCK_HEIGHT + y0
    taps = (
        (band[row0, x0], (1 - fx) * (1 - fy)),
        (band[row0, x0 + 1], fx * (1 - fy)),
        (band[row0 + 1, x0], (1 - fx) * fy),
        (band[row0 + 1, x0 + 1], fx * fy),
    )
    strict = np.zeros_like(x)
    weighted_sum = np.zeros_like(x)
    weight_sum = np.zeros_like(x)
    for tap, weight in taps:
        strict = strict + np.where(weight > 0, tap * weight, 0.0)
        valid = np.isfinite(tap) & (weight > 0)
        weighted_sum = weighted_sum + np.where(valid, tap * weight, 0.0)
        weight_sum = weight_sum + np.where(valid, weight, 0.0)
    containing = band[framelet * VIS_BLOCK_HEIGHT + np.rint(y).astype(int), np.rint(x).astype(int)]
    with np.errstate(invalid="ignore", divide="ignore"):
        partial = np.where(np.isfinite(containing) & (weight_sum > 0), weighted_sum / weight_sum, np.nan)
    return strict, partial


def _keys_weight(t: np.ndarray, a: float = _KEYS_A) -> np.ndarray:
    t = np.abs(t)
    return np.where(
        t <= 1.0,
        (a + 2.0) * t**3 - (a + 3.0) * t**2 + 1.0,
        np.where(t < _KEYS_SUPPORT, a * t**3 - 5.0 * a * t**2 + 8.0 * a * t - 4.0 * a, 0.0),
    )


def _cubic_within_framelet(band: np.ndarray, framelet: np.ndarray, sample: np.ndarray, within_line: np.ndarray):
    # 4x4 cubic convolution, taps clamped like `_bilinear_within_framelet`'s. NaN if any tap with
    # nonzero weight is invalid.
    n_cols = band.shape[1]
    x = np.clip(sample - 1.0, 0.0, n_cols - 1.0)
    y = np.clip(within_line - 1.0, 0.0, VIS_BLOCK_HEIGHT - 1.0)
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    fx, fy = x - x0, y - y0
    value = np.zeros_like(x)
    for dy in (-1, 0, 1, 2):
        wy = _keys_weight(fy - dy)
        row = framelet * VIS_BLOCK_HEIGHT + np.clip(y0 + dy, 0, VIS_BLOCK_HEIGHT - 1)
        for dx in (-1, 0, 1, 2):
            weight = wy * _keys_weight(fx - dx)
            tap = band[row, np.clip(x0 + dx, 0, n_cols - 1)]
            value = value + np.where(weight != 0, tap * weight, 0.0)
    return value


def resample_crop(crop_cub: Path, band: np.ndarray, grid: MapGrid, config: TrntestConfig | None = None) -> Resampled:
    """Map-project one band of a crop onto `grid`.

    :param crop_cub: Crop cube (for its SPICE timing).
    :param band: The band to resample, float, NaN where invalid (e.g. `wac_framelet_fill.read_band`).
    :param grid: Output grid.
    :param config: Project config; `load_config()` if not given.
    :returns: The resampled band and the chosen crop coordinates.
    """
    n_lines = band.shape[0]
    n_framelets = n_lines // VIS_BLOCK_HEIGHT
    positions, rotations = framelet_poses(crop_cub, n_lines)
    ground = grid_ground_points_me_m(grid, shape_model_radius_on_grid(grid, config)).reshape(-1, 3)
    n = ground.shape[0]

    def project_at(framelet: np.ndarray, points: np.ndarray | slice = slice(None)):
        return project(ground[points], positions[framelet], rotations[framelet])

    # Bisection over framelet index, as `wac_camera_model.find_framelet_and_project` does: a ground
    # point's within-framelet line is monotonic in framelet index, but far from linear, so a linear
    # estimate from one framelet can land several framelets off. Direction measured per point, and
    # each step only projects points still searching.
    lo = np.zeros(n, dtype=int)
    hi = np.full(n, n_framelets - 1)
    _, line_lo = project_at(lo)
    _, line_hi = project_at(hi)
    increasing = line_hi > line_lo
    active = np.flatnonzero(lo < hi)
    while active.size:
        mid = (lo[active] + hi[active]) // 2
        _, line_mid = project_at(mid, active)
        inside = (line_mid >= 1.0) & (line_mid <= VIS_BLOCK_HEIGHT)
        go_up = ~inside & ((line_mid < 1.0) == increasing[active])
        lo[active] = np.where(inside, mid, np.where(go_up, mid + 1, lo[active]))
        hi[active] = np.where(inside | ~go_up, mid, hi[active])
        active = active[lo[active] < hi[active]]
    estimate = lo

    # Candidates: the estimate and its two neighbors, ranked by distance from their own center line.
    n_cols = band.shape[1]
    candidates = []
    for offset in (-1, 0, 1):
        framelet = np.clip(estimate + offset, 0, n_framelets - 1)
        sample, within_line = project_at(framelet)
        inside = (
            (sample >= _HALF_PIXEL)
            & (sample <= n_cols + _HALF_PIXEL)
            & (within_line >= _HALF_PIXEL)
            & (within_line <= VIS_BLOCK_HEIGHT + _HALF_PIXEL)
        )
        inside &= (estimate + offset >= 0) & (estimate + offset < n_framelets)
        cubic = _cubic_within_framelet(band, framelet, sample, within_line)
        strict, partial = _bilinear_within_framelet(band, framelet, sample, within_line)
        rank = np.where(inside, np.abs(within_line - _CENTER_LINE), np.inf)
        candidates.append((rank, framelet, sample, within_line, cubic, strict, partial))

    order = np.argsort(np.stack([c[0] for c in candidates]), axis=0)
    out = {k: np.full(n, np.nan) for k in ("value", "sample", "line")}
    out_framelet = np.full(n, -1)
    chosen = np.zeros(n, dtype=bool)
    # First pass: the most central framelet whose cubic taps are all valid. Second pass: the same
    # with bilinear's four taps. Third pass, for what's left (a framelet edge next to a dead column
    # or a NULL line with no other framelet covering it): the most central framelet with any valid
    # bilinear tap, weights renormalized.
    for value_index in (4, 5, 6):
        for position in range(len(candidates)):
            pick = order[position]
            for index, candidate in enumerate(candidates):
                rank, framelet, sample, within_line = candidate[:4]
                value = candidate[value_index]
                take = ~chosen & (pick == index) & np.isfinite(rank) & np.isfinite(value)
                out["value"][take] = value[take]
                out["sample"][take] = sample[take]
                out["line"][take] = framelet[take] * VIS_BLOCK_HEIGHT + within_line[take]
                out_framelet[take] = framelet[take]
                chosen |= take
    return Resampled(
        value=out["value"].reshape(grid.shape),
        framelet=out_framelet.reshape(grid.shape),
        sample=out["sample"].reshape(grid.shape),
        line=out["line"].reshape(grid.shape),
    )


def fill_small_holes(values: np.ndarray, max_hole_px: int = 4) -> tuple[np.ndarray, np.ndarray]:
    """Fill enclosed NaN holes of at most `max_hole_px` pixels from the mean of their valid
    8-neighbors, working inward until closed. NaN connected to the footprint's outside is left alone.

    :param values: Resampled values, NaN where nothing was sampled.
    :param max_hole_px: Largest hole (8-connected pixel count) to fill.
    :returns: `(filled copy of values, mask of pixels filled)`.
    """
    invalid = ~np.isfinite(values)
    holes = ndimage.binary_fill_holes(~invalid) & invalid
    labels, n_holes = ndimage.label(holes, structure=np.ones((3, 3)))
    if n_holes:
        sizes = ndimage.sum(holes, labels, index=np.arange(1, n_holes + 1))
        holes &= np.isin(labels, np.flatnonzero(sizes <= max_hole_px) + 1)
    filled = values.copy()
    remaining = holes.copy()
    kernel = np.ones((3, 3))
    while remaining.any():
        known = np.isfinite(filled)
        total = ndimage.convolve(np.where(known, filled, 0.0), kernel, mode="constant")
        count = ndimage.convolve(known.astype(float), kernel, mode="constant")
        ready = remaining & (count > 0)
        filled[ready] = total[ready] / count[ready]
        remaining &= ~ready
    return filled, holes


def write_geotiff(values: np.ndarray, grid: MapGrid, path: Path, nodata: float | None, dtype: str = "float32") -> Path:
    """Write `values` on `grid` as a single-band GeoTIFF, NaN written as `nodata`.

    :param values: `grid.shape` array.
    :param grid: Its grid.
    :param path: Output path.
    :param nodata: Nodata value to write and declare, or `None` for none (then `values` has no NaN).
    :param dtype: Output data type.
    :returns: `path`.
    """
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=grid.shape[0],
        width=grid.shape[1],
        count=1,
        dtype=dtype,
        crs=grid.crs,
        transform=grid.transform,
        nodata=nodata,
    ) as dst:
        if nodata is not None:
            values = np.where(np.isfinite(values), values, nodata)
        dst.write(values.astype(dtype), 1)
    return path


def _footprint_window(values: np.ndarray, grid: MapGrid) -> tuple[tuple[slice, slice], MapGrid]:
    # The valid footprint's bounding box, and `grid` trimmed to it on the same pixel lattice.
    rows, cols = np.nonzero(np.isfinite(values))
    if rows.size == 0:
        raise ValueError("crop footprint does not overlap the DEM grid")
    r0, r1, c0, c1 = int(rows.min()), int(rows.max()) + 1, int(cols.min()), int(cols.max()) + 1
    trimmed = MapGrid(
        shape=(r1 - r0, c1 - c0), transform=grid.transform @ rasterio.Affine.translation(c0, r0), crs=grid.crs
    )
    return (slice(r0, r1), slice(c0, c1)), trimmed


@writes_product("crop_resampled")
def resample_crop_to_map(
    crop: isis_wac.CropResult, dem_ortho_result: DemFetchResult | DemOrthoResult, config: TrntestConfig | None = None
) -> Path:
    """Map-project `crop`'s band 1 onto `dem_ortho_result.dem`'s pixel lattice via `resample_crop`,
    trimmed to the crop's footprint, with `fill_small_holes` applied. The output's `FILLED_PERCENT` tag
    gives the share of the footprint's pixels that fill invented; `config.crop_map_write_fill_mask`
    also writes them as a `-filled.tif` mask (1 = filled) beside it.

    :param crop: Cropped cube to reproject.
    :param dem_ortho_result: DEM (or DEM/ortho pair) whose grid to land on; only its `.dem` is read.
    :param config: Project config; `load_config()` if not given.
    :returns: Path to the single-band float32 GeoTIFF, nodata `ISIS_NULL` (as `cam2map`'s output).
    """
    # Poses come from the SPICE kernels, not the cube's own SPICE tables, so a crop whose tables were
    # edited (e.g. `pose_alignment`'s pose-corrected crops) needs `isis_wac.run_cam2map_for_crop`.
    config = config or load_config()
    # Furnishes the kernels `framelet_poses` needs; `fetch_and_furnish` skips any already loaded.
    spice_kernels.fetch_and_furnish(isis_wac.cube_start_time(crop.cub_path), config)
    grid = MapGrid.from_raster(dem_ortho_result.dem)
    resampled = resample_crop(crop.cub_path, read_band(crop.cub_path), grid, config)
    values, filled = fill_small_holes(resampled.value, _MAX_FILLED_HOLE_PX)
    footprint = ndimage.binary_fill_holes(np.isfinite(values))
    left_open = int((footprint & ~np.isfinite(values)).sum())
    if left_open:
        print(
            f"resample_crop_to_map: {crop.cub_path.name}: {_percent(left_open, footprint)} of the footprint "
            f"({left_open} px) is in enclosed holes larger than {_MAX_FILLED_HOLE_PX} px, left as nodata"
        )
    window, trimmed = _footprint_window(values, grid)

    out_dir = config.output_dir / "crop"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_tif = out_dir / (crop.cub_path.stem + "-resampled.tif")
    with atomic_publish_path(out_tif) as tmp_tif:
        write_geotiff(values[window], trimmed, tmp_tif, float(ISIS_NULL))
        with rasterio.open(tmp_tif, "r+") as dst:
            dst.update_tags(FILLED_PERCENT=_percent(int(filled.sum()), footprint))
    if config.crop_map_write_fill_mask:
        mask_tif = out_tif.with_name(out_tif.stem + "-filled.tif")
        with atomic_publish_path(mask_tif) as tmp_mask:
            write_geotiff(filled[window], trimmed, tmp_mask, nodata=None, dtype="uint8")
    return out_tif


def _percent(count: int, footprint: np.ndarray) -> str:
    return f"{100.0 * count / max(int(footprint.sum()), 1):.2g}%"


def map_project_crop(
    crop: isis_wac.CropResult, dem_ortho_result: DemFetchResult | DemOrthoResult, config: TrntestConfig | None = None
) -> Path:
    """Map-project `crop` into `dem_ortho_result`'s projection, by `config.crop_map_projection`'s
    method (`resample_crop_to_map` or `isis_wac.run_cam2map_for_crop`).

    :param crop: Cropped cube to reproject.
    :param dem_ortho_result: DEM (or DEM/ortho pair) whose projection to use.
    :param config: Project config; `load_config()` if not given.
    :returns: Path to the single-band GeoTIFF.
    """
    config = config or load_config()
    if config.crop_map_projection == "wac_resample":
        return resample_crop_to_map(crop, dem_ortho_result, config)
    if config.crop_map_projection == "cam2map":
        return isis_wac.run_cam2map_for_crop(crop, dem_ortho_result, config)
    raise ValueError(f"unknown crop_map_projection {config.crop_map_projection!r}")


def crop_reflectance_on_dem_grid(
    crop: isis_wac.CropResult, dem: DemFetchResult | DemOrthoResult, config: TrntestConfig | None = None
) -> np.ndarray:
    """The real WAC crop's calibrated reflectance, resampled pixel-for-pixel onto `dem`'s own grid.

    :param crop: Cropped cube (e.g. `TrnTestEntryEdr.crop_result`).
    :param dem: DEM whose exact grid (not just projection) to land on.
    :param config: Project config; `load_config()` if not given.
    :returns: float32 array, `dem`'s shape; NaN outside the crop's coverage.
    """
    # `map_project_crop`'s output shares `dem`'s projection but covers only the crop's footprint (and
    # for `cam2map`, a pixel lattice of its own), so it still goes through a bilinear `reproject`
    # onto the DEM's exact transform. For `resample_crop_to_map`, whose output is on the DEM's
    # lattice, that step only pads.
    mapped_tif = map_project_crop(crop, dem, config)
    with rasterio.open(dem.dem) as dst:
        dst_transform, dst_crs, dst_shape = dst.transform, dst.crs, dst.shape
    with rasterio.open(mapped_tif) as src:
        source = src.read(1, masked=True).filled(np.nan).astype(np.float32)
        on_dem_grid = np.full(dst_shape, np.nan, dtype=np.float32)
        rasterio.warp.reproject(
            source=source,
            destination=on_dem_grid,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            src_nodata=np.nan,
            dst_nodata=np.nan,
            resampling=rasterio.warp.Resampling.bilinear,
        )
    return on_dem_grid
