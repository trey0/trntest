"""Cast-shadow occlusion: which parts of a DEM are blocked from the Sun by *other* terrain.

`illumination_fraction` returns a per-pixel 0-1 multiplier (`1` = fully lit, `0` = fully
cast-shadowed) that `hapke.despeckle_and_shade_ortho` applies on top of its per-facet shading, so
`hillshade` renders get real shadows cast across crater floors, not just dark sun-facing-away walls.

The method is a sun-aligned sweep. Build a Cartesian frame with the Sun at infinity along `+x`.
Every sun ray is then parallel to the x-axis, so occlusion along one row of that frame (fixed `y`)
reduces to a running maximum of height (`z`), swept from the sun-facing edge inward: a point is lit
iff nothing closer to the Sun on its row is taller. That is one `np.maximum.accumulate` per row
rather than a per-pixel ray march against the DEM.

Pure math -- no SPICE, no file I/O, no config -- so it runs on synthetic DEMs in tests.
"""

import dataclasses

import numpy as np
from scipy import ndimage

from trntest.config import MOON_RADIUS_M
from trntest.geo_utils import local_enu_basis, local_grid_positions_moon_me, pixel_center_coords_m

# DEM refinement factor before projecting into the sun-aligned frame. The refinement (a cubic spline)
# is what makes the output fractional: each native pixel's illumination is the mean over its
# `UPSAMPLE_FACTOR**2` sub-samples, which antialiases shadow edges.
UPSAMPLE_FACTOR = 2

# Sun-frame bin size, as a multiple of the upsampled grid's own spacing. Must be meaningfully larger
# than 1: the upsampled grid, rotated into the sun frame, is a rotated lattice, and binning it onto an
# axis-aligned grid of the *same* spacing leaves most bins holding exactly one sample (measured: 78%
# at 1.0x). The per-bin max is then just "whichever sample landed there", and the output shows a
# periodic Moire "screen door" pattern wherever illumination is partial. 2.0 raises the mean samples
# per occupied bin to ~4 and removes the pattern. Raising `UPSAMPLE_FACTOR` instead does not help --
# the aliasing ratio is scale-invariant while bin size is tied 1:1 to sample spacing -- and blurring
# the output would only hide the pattern along with real shadow-edge detail.
BIN_SIZE_SAFETY_FACTOR = 2.0

# Native DEM rows per streaming chunk. Bounds the per-sample working arrays; see `sun_sweep`.
CHUNK_ROWS = 128

# `sun_aligned_basis` treats the Sun as overhead (no cast shadows possible) when the Sun direction is
# this close to parallel with local up -- the frame's `z` axis is undefined there.
_OVERHEAD_SUN_COS_THRESHOLD = 1.0 - 1e-9


@dataclasses.dataclass(frozen=True)
class SunSweep:
    """`sun_sweep`'s result.

    :ivar illumination_fraction: float32, the DEM's own shape. `1` = fully lit, `0` = fully
        cast-shadowed, NaN where the DEM itself is NaN.
    :ivar basis: `(x_hat, y_hat, z_hat)`, MOON_ME unit vectors; `x_hat` points at the Sun.
    :ivar bin_size_m: Sun-frame bin size, meters.
    :ivar bin_counts: int32, `(nx, ny)` sun-frame raster of samples per bin (axis 0 = `x`, toward the
        Sun). Diagnostic only.
    """

    illumination_fraction: np.ndarray
    basis: tuple[np.ndarray, np.ndarray, np.ndarray]
    bin_size_m: float
    bin_counts: np.ndarray

    def summary(self) -> str:
        """A few lines describing the sun-frame raster and its bin occupancy."""
        occupied = self.bin_counts > 0
        n_occupied = int(occupied.sum())
        lines = [
            f"sun-frame raster: {self.bin_counts.shape[0]} (x, toward Sun) x {self.bin_counts.shape[1]} (y), "
            f"bin size {self.bin_size_m:.1f} m",
            f"bins with >=1 sample: {n_occupied / self.bin_counts.size:.3f}",
        ]
        if n_occupied:
            lines.append(
                f"samples per occupied bin: mean {self.bin_counts[occupied].mean():.2f}, "
                f"exactly one: {(self.bin_counts == 1).sum() / n_occupied:.3f}"
            )
        lines.append(f"mean illumination fraction: {np.nanmean(self.illumination_fraction):.3f}")
        return "\n".join(lines)


def sun_aligned_basis(sun_direction_moon_me, up_moon_me) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """The sun-aligned frame: `x` toward the Sun, `z` as close to local up as possible.

    :param sun_direction_moon_me: Sun direction, MOON_ME (need not be unit length).
    :param up_moon_me: Local up (radial) direction at the DEM's tangent point, MOON_ME.
    :returns: `(x_hat, y_hat, z_hat)`, a right-handed orthonormal MOON_ME triad, or `None` if the Sun
        is overhead (no `z` axis can be defined, and no cast shadows are possible).
    """
    # `z` is `up` Gram-Schmidt-orthogonalized against `x`; `y = z x x` completes a right-handed frame
    # (`x x y = x x (z x x) = z`).
    x_hat = np.asarray(sun_direction_moon_me, dtype=np.float64)
    x_hat = x_hat / np.linalg.norm(x_hat)
    up = np.asarray(up_moon_me, dtype=np.float64)
    up = up / np.linalg.norm(up)
    if abs(float(np.dot(up, x_hat))) > _OVERHEAD_SUN_COS_THRESHOLD:
        return None
    z_hat = up - np.dot(up, x_hat) * x_hat
    z_hat /= np.linalg.norm(z_hat)
    y_hat = np.cross(z_hat, x_hat)
    return x_hat, y_hat, z_hat


def sweep_illuminated(z_max: np.ndarray) -> np.ndarray:
    """Per-bin lit/shadowed classification of a sun-frame height raster.

    :param z_max: `(nx, ny)` per-bin max height, axis 0 = `x` increasing toward the Sun; NaN for an
        empty bin.
    :returns: float array, same shape: `1.0` lit, `0.0` shadowed, NaN for an empty bin.
    """
    # Light travels in `-x`, so sweep from the highest `x` (the sun-facing edge) inward. An empty bin
    # is `-inf` in the running max -- a gap can't occlude anything -- and stays NaN in the output,
    # since there is nothing there to classify. A bin is lit iff its height is at least the running
    # max *including itself*, i.e. nothing closer to the Sun on its row is strictly taller.
    #
    # Bins on the sun-facing edge are always lit: terrain beyond the DEM's own extent isn't modeled,
    # so an occluder just outside it is invisible.
    toward_sun_first = z_max[::-1, :]
    filled = np.where(np.isnan(toward_sun_first), -np.inf, toward_sun_first)
    running_max = np.maximum.accumulate(filled, axis=0)
    lit = np.where(np.isnan(toward_sun_first), np.nan, (filled >= running_max).astype(np.float64))
    return lit[::-1, :]


def _spline_coefficients(dem: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Cubic-spline coefficients for `dem`, and its NaN mask.

    :param dem: Elevation, float64.
    :returns: `(coefficients, nan_mask)`, both `dem`'s shape.
    """
    # A NaN would spread through the whole spline, so fit a nearest-valid-filled copy; `_fine_heights`
    # re-masks afterward (a sample is NaN iff the native pixel it lies in is NaN). `spline_filter`
    # runs once, whole-grid, so per-chunk evaluation is identical to a whole-grid spline -- chunk
    # seams can't appear.
    nan_mask = np.isnan(dem)
    if nan_mask.all():
        raise ValueError("dem is entirely NaN")
    if nan_mask.any():
        nearest = ndimage.distance_transform_edt(nan_mask, return_distances=False, return_indices=True)
        dem = dem[tuple(nearest)]
    return ndimage.spline_filter(dem, order=3, mode="mirror"), nan_mask


def _fine_heights(spline_coeffs: np.ndarray, nan_mask: np.ndarray, row0: int, row1: int, f: int) -> np.ndarray:
    """Upsampled heights for native rows `[row0, row1)`, at the fine grid's own pixel centers.

    :param spline_coeffs: `_spline_coefficients`'s coefficients.
    :param nan_mask: `_spline_coefficients`'s NaN mask.
    :param row0: First native row.
    :param row1: One past the last native row.
    :param f: Upsample factor.
    :returns: `((row1 - row0) * f, width * f)` heights.
    """
    # Fine pixel center `j` sits at native pixel index `(j + 0.5) / f - 0.5` -- pixel centers, not
    # corners, on both grids, matching `geo_utils.pixel_center_coords_m`. (`ndimage.zoom`'s default
    # corner-aligned mapping would put fine heights up to a quarter native pixel out of register with
    # their own x/y at the DEM edges.) Samples past the outermost native centers are mirrored.
    width = spline_coeffs.shape[1]
    row_coords = (np.arange(row0 * f, row1 * f) + 0.5) / f - 0.5
    col_coords = (np.arange(width * f) + 0.5) / f - 0.5
    rr, cc = np.meshgrid(row_coords, col_coords, indexing="ij")
    z = ndimage.map_coordinates(spline_coeffs, [rr, cc], order=3, mode="mirror", prefilter=False)
    z[np.repeat(np.repeat(nan_mask[row0:row1], f, axis=0), f, axis=1)] = np.nan
    return z


def sun_sweep(
    dem: np.ndarray,
    bbox: tuple,
    center_lon_deg: float,
    center_lat_deg: float,
    sun_direction_moon_me,
    radius_m: float = MOON_RADIUS_M,
    upsample_factor: int = UPSAMPLE_FACTOR,
    bin_size_factor: float = BIN_SIZE_SAFETY_FACTOR,
    chunk_rows: int = CHUNK_ROWS,
) -> SunSweep:
    """Cast-shadow illumination fraction for `dem`, plus the sun-frame diagnostics behind it.

    :param dem: Elevation, meters, on the north-up local Orthographic grid described by `bbox`.
    :param bbox: `(minx, miny, maxx, maxy)`, meters, `geo_utils.local_orthographic_crs` frame.
    :param center_lon_deg: That frame's tangent point longitude, degrees.
    :param center_lat_deg: That frame's tangent point latitude, degrees.
    :param sun_direction_moon_me: Sun direction, MOON_ME (`illumination.sun_direction_moon_me`).
    :param radius_m: Sphere radius, meters.
    :param upsample_factor: See `UPSAMPLE_FACTOR`.
    :param bin_size_factor: See `BIN_SIZE_SAFETY_FACTOR`.
    :param chunk_rows: See `CHUNK_ROWS`. Any value gives bit-identical output.
    :returns: A `SunSweep`.
    """
    # Positions are true 3D MOON_ME points (`geo_utils.local_grid_positions_moon_me`), not a flat
    # tangent-plane approximation. Treating the DEM as a flat (east, north, height) sheet and folding
    # the sun elevation into a per-column height correction is wrong at this project's DEM scale: the
    # Moon's curvature (sagitta) over half a ~240 km-wide DEM is ~4.2 km, the same order as typical
    # terrain relief.
    #
    # The sweep treats terrain as a height field along `z_hat`: everything below a bin's max height is
    # solid. That holds whenever `z_hat` is close to local up, which is exactly the low-sun geometry
    # where cast shadows matter.
    #
    # Self-shadow (a facet whose own slope faces away from the Sun) is deliberately not part of this
    # output. The per-facet reflectance models this multiplies against already render such a facet
    # dark, so marking it here too would change nothing.
    #
    # Streaming, in three passes, so memory stays bounded by `chunk_rows` rather than by the upsampled
    # grid (~23 M samples for a typical ~2400 px DEM at 2x, over 1 GB as whole-grid float64 arrays):
    #   1. per chunk of native rows: interpolate the chunk's upsampled heights, transform to MOON_ME,
    #      project into the sun frame, and reduce into a whole-raster per-bin max height. The
    #      sun-frame raster is small (bins are ~native resolution), so it's held whole. Each sample's
    #      bin index is kept (int32), so pass 3 doesn't repeat the transform, the costly step.
    #   2. the sweep, over the whole sun-frame raster.
    #   3. per chunk again: gather each sample's lit/shadowed value through its stored bin index and
    #      average each native pixel's `upsample_factor**2` samples.
    dem = np.asarray(dem, dtype=np.float64)
    height, width = dem.shape
    up = local_enu_basis(center_lon_deg, center_lat_deg)[2]
    basis = sun_aligned_basis(sun_direction_moon_me, up)
    bin_size_m = (bbox[2] - bbox[0]) / width / upsample_factor * bin_size_factor
    if basis is None:
        illumination = np.where(np.isnan(dem), np.nan, 1.0).astype(np.float32)
        return SunSweep(illumination, (up, up, up), bin_size_m, np.zeros((0, 0), dtype=np.int32))

    frame = _SunFrame(basis, center_lon_deg, center_lat_deg, radius_m)
    chunks = [(r, min(r + chunk_rows, height)) for r in range(0, height, chunk_rows)]
    grid = _sun_grid(frame, dem, bbox, chunks, bin_size_m)
    z_max, counts, bin_index = _bin_heights(frame, grid, dem, bbox, chunks, upsample_factor)
    lit = sweep_illuminated(np.where(counts > 0, z_max, np.nan))
    illumination = _gather_illumination(lit, bin_index, chunks, width, upsample_factor)
    return SunSweep(illumination, basis, bin_size_m, counts)


@dataclasses.dataclass(frozen=True)
class _SunFrame:
    """Projection from the local Orthographic frame (plus elevation) into sun-frame `(X, Y, Z)`."""

    basis: tuple[np.ndarray, np.ndarray, np.ndarray]
    center_lon_deg: float
    center_lat_deg: float
    radius_m: float

    def project(self, x_m: np.ndarray, y_m: np.ndarray, elevation_m: np.ndarray) -> np.ndarray:
        """`(..., 3)` sun-frame coordinates, relative to the tangent point (keeps magnitudes small)."""
        args = (self.center_lon_deg, self.center_lat_deg, self.radius_m)
        zero = np.array(0.0)
        origin = local_grid_positions_moon_me(zero, zero, zero, *args)
        positions = local_grid_positions_moon_me(x_m, y_m, elevation_m, *args)
        return (positions - origin) @ np.stack(self.basis).T


@dataclasses.dataclass(frozen=True)
class _SunGrid:
    """The sun-frame raster: `(nx, ny)` bins of `bin_size_m`, lower corner `lo`."""

    lo: np.ndarray
    bin_size_m: float
    nx: int
    ny: int

    def flat_index(self, sun_xyz: np.ndarray) -> np.ndarray:
        """int32 flat bin index (`ix * ny + iy`) of each sample, clipped into range."""
        ix = np.clip(((sun_xyz[..., 0] - self.lo[0]) / self.bin_size_m).astype(np.int64), 0, self.nx - 1)
        iy = np.clip(((sun_xyz[..., 1] - self.lo[1]) / self.bin_size_m).astype(np.int64), 0, self.ny - 1)
        return (ix * self.ny + iy).astype(np.int32)


def _sun_grid(frame: _SunFrame, dem: np.ndarray, bbox: tuple, chunks: list, bin_size_m: float) -> _SunGrid:
    """Size the sun-frame raster to cover every sample.

    :param frame: The sun frame.
    :param dem: Elevation, meters.
    :param bbox: `dem`'s bbox.
    :param chunks: `(row0, row1)` native row ranges.
    :param bin_size_m: Bin size, meters.
    :returns: A `_SunGrid`.
    """
    # From the native grid (1/f**2 as many points as the fine grid), padded by a few bins for the
    # half-pixel fine-grid overhang and spline overshoot. `_SunGrid.flat_index` also clips into range,
    # so an under-estimate can only fold a stray sample into an edge bin.
    height, width = dem.shape
    dem_filled = np.where(np.isnan(dem), np.nanmean(dem), dem)
    x_native, y_native = pixel_center_coords_m(bbox, width, height)
    lo, hi = np.full(2, np.inf), np.full(2, -np.inf)
    for row0, row1 in chunks:
        xg, yg = np.meshgrid(x_native, y_native[row0:row1])
        xy = frame.project(xg, yg, dem_filled[row0:row1])[..., :2].reshape(-1, 2)
        lo, hi = np.minimum(lo, xy.min(axis=0)), np.maximum(hi, xy.max(axis=0))
    pad = 3 * bin_size_m
    nx, ny = (int(np.ceil(n)) for n in (hi - lo + 2 * pad) / bin_size_m)
    return _SunGrid(lo - pad, bin_size_m, nx, ny)


def _bin_heights(
    frame: _SunFrame, grid: _SunGrid, dem: np.ndarray, bbox: tuple, chunks: list, f: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pass 1: per-bin max height and sample count, plus each fine sample's bin index.

    :param frame: The sun frame.
    :param grid: The sun-frame raster.
    :param dem: Elevation, meters.
    :param bbox: `dem`'s bbox.
    :param chunks: `(row0, row1)` native row ranges.
    :param f: Upsample factor.
    :returns: `(z_max, counts, bin_index)`: `(nx, ny)` float64 (`-inf` where empty), `(nx, ny)` int32,
        and `(height * f, width * f)` int32 (`-1` for a NaN sample).
    """
    height, width = dem.shape
    spline_coeffs, nan_mask = _spline_coefficients(dem)
    x_fine, y_fine = pixel_center_coords_m(bbox, width * f, height * f)
    z_max = np.full(grid.nx * grid.ny, -np.inf)
    counts = np.zeros(grid.nx * grid.ny, dtype=np.int32)
    bin_index = np.empty((height * f, width * f), dtype=np.int32)
    for row0, row1 in chunks:
        z = _fine_heights(spline_coeffs, nan_mask, row0, row1, f)
        xg, yg = np.meshgrid(x_fine, y_fine[row0 * f : row1 * f])
        valid = ~np.isnan(z)
        sun_xyz = frame.project(xg, yg, np.where(valid, z, 0.0))  # PROJ rejects NaN; masked out below
        flat = grid.flat_index(sun_xyz)
        flat[~valid] = -1
        np.maximum.at(z_max, flat[valid], sun_xyz[..., 2][valid])
        np.add.at(counts, flat[valid], 1)
        bin_index[row0 * f : row1 * f] = flat
    return z_max.reshape(grid.nx, grid.ny), counts.reshape(grid.nx, grid.ny), bin_index


def _gather_illumination(lit: np.ndarray, bin_index: np.ndarray, chunks: list, width: int, f: int) -> np.ndarray:
    """Pass 3: each fine sample takes its bin's lit value; each native pixel averages its samples.

    :param lit: `sweep_illuminated`'s output.
    :param bin_index: `_bin_heights`'s per-sample bin index.
    :param chunks: `(row0, row1)` native row ranges.
    :param width: Native width.
    :param f: Upsample factor.
    :returns: float32 `(height, width)` illumination fraction, NaN where every sample is NaN.
    """
    lit_flat = lit.ravel()
    illumination = np.empty((bin_index.shape[0] // f, width), dtype=np.float32)
    for row0, row1 in chunks:
        flat = bin_index[row0 * f : row1 * f]
        samples = np.where(flat >= 0, lit_flat[np.maximum(flat, 0)], np.nan)
        blocks = samples.reshape(row1 - row0, f, width, f)
        n_valid = np.sum(~np.isnan(blocks), axis=(1, 3))
        total = np.nansum(blocks, axis=(1, 3))
        with np.errstate(invalid="ignore", divide="ignore"):
            illumination[row0:row1] = np.where(n_valid > 0, total / n_valid, np.nan)
    return illumination


def illumination_fraction(
    dem: np.ndarray,
    bbox: tuple,
    center_lon_deg: float,
    center_lat_deg: float,
    sun_direction_moon_me,
    radius_m: float = MOON_RADIUS_M,
) -> np.ndarray:
    """Cast-shadow illumination fraction for `dem`: `sun_sweep`'s main output alone.

    :param dem: See `sun_sweep`.
    :param bbox: See `sun_sweep`.
    :param center_lon_deg: See `sun_sweep`.
    :param center_lat_deg: See `sun_sweep`.
    :param sun_direction_moon_me: See `sun_sweep`.
    :param radius_m: See `sun_sweep`.
    :returns: float32, `dem`'s shape: `1` = fully lit, `0` = fully cast-shadowed, NaN where `dem` is.
    """
    return sun_sweep(dem, bbox, center_lon_deg, center_lat_deg, sun_direction_moon_me, radius_m).illumination_fraction
