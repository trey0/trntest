"""Cast-shadow occlusion: which parts of a DEM are blocked from the Sun by *other* terrain.

`illumination_fraction` returns a per-pixel 0-1 multiplier (`1` = fully lit, `0` = fully
cast-shadowed) that `hapke.despeckle_and_shade_ortho` applies on top of its per-facet shading, so
`hillshade` renders get real shadows cast across crater floors, not just dark sun-facing-away walls.

The method is a sun-aligned sweep. Build a Cartesian frame with the Sun at infinity along `+x`,
`y` horizontal and perpendicular to the Sun's azimuth, and `z` perpendicular to the sun rays. Every
ray stays at fixed `y` and fixed `z`, so a point is shadowed iff some terrain on its `y` row that is
horizontally closer to the Sun has a larger `z`. Binning rows by horizontal distance toward the Sun
(`d`) makes that a running maximum of `z`, swept from the sun-facing edge inward: one
`np.maximum.accumulate` per row rather than a per-pixel ray march against the DEM.

Pure math -- no SPICE, no file I/O, no config -- so it runs on synthetic DEMs in tests.
"""

import dataclasses

import numpy as np
from scipy import ndimage

from trntest.config import MOON_RADIUS_M
from trntest.geo_utils import (
    local_enu_basis,
    local_grid_coords_from_moon_me,
    local_grid_positions_moon_me,
    local_orthographic_crs,
    pixel_center_coords_m,
)

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

# `horizon_sweep`: a sample counts as lit when its `Z` is within this of the horizon. Covers float32
# horizon storage (~1 mm at the ~10 km `Z` magnitudes curvature reaches across a DEM) and the
# exactly-grazing case, where the sample and its horizon lie on the same plane.
HORIZON_TOLERANCE_M = 0.01

# `horizon_sweep`'s terrain solve: iterate until every grid point's height mismatch is below this.
TERRAIN_SOLVE_TOLERANCE_M = 1e-3
_TERRAIN_SOLVE_MAX_ITERATIONS = 10

# `horizon_sweep`: sun-grid points whose zero-elevation location falls more than this far outside the
# DEM's bbox are skipped outright, rather than solved and then discarded. Must exceed the largest
# horizontal shift the terrain solve can make: relief times the tangent of curvature's tilt of local
# vertical (~5 km x tan 4 deg = ~350 m at this project's DEM sizes).
_TERRAIN_SOLVE_BBOX_MARGIN_M = 2000.0

# `horizon_sweep`: target sun-grid points per streaming chunk.
_HORIZON_CHUNK_POINTS = 1 << 20

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
    :ivar bin_counts: int32, `(nd, ny)` sun-frame raster of samples per bin (axis 0 = `d`, horizontal
        distance toward the Sun). Diagnostic only.
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
            f"sun-frame raster: {self.bin_counts.shape[0]} (d, toward Sun) x {self.bin_counts.shape[1]} (y), "
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


@dataclasses.dataclass(frozen=True, eq=False)
class SunFrame:
    """The sun-frame `(D, Y, Z)` coordinates of points given in a `geo_utils.local_orthographic_crs`
    frame plus elevation: `D` horizontal distance toward the Sun, `Y` = `y_hat`, `Z` = `z_hat`, all
    meters relative to the tangent point at zero elevation.

    `proj_pipeline` is the reference definition; `to_sun`/`from_sun` are fast closed forms of its
    forward and inverse directions (pinned to it in `tests/test_cast_shadow.py`).

    :ivar center_lon_deg: Tangent point longitude, degrees.
    :ivar center_lat_deg: Tangent point latitude, degrees.
    :ivar radius_m: Sphere radius, meters.
    :ivar basis: `sun_aligned_basis`'s `(x_hat, y_hat, z_hat)`.
    :ivar matrix: `(3, 3)`, rows `d_hat` (`x_hat` made horizontal at the tangent point), `y_hat`,
        `z_hat`. Not orthogonal (`d_hat` and `z_hat` differ by the sun elevation), but invertible.
    :ivar origin: The tangent point at zero elevation, MOON_ME.
    """

    center_lon_deg: float
    center_lat_deg: float
    radius_m: float
    basis: tuple[np.ndarray, np.ndarray, np.ndarray]
    matrix: np.ndarray
    origin: np.ndarray

    @classmethod
    def create(
        cls, center_lon_deg: float, center_lat_deg: float, sun_direction_moon_me, radius_m: float = MOON_RADIUS_M
    ) -> "SunFrame | None":
        """Build the frame, or `None` if the Sun is overhead (see `sun_aligned_basis`)."""
        up = local_enu_basis(center_lon_deg, center_lat_deg)[2]
        basis = sun_aligned_basis(sun_direction_moon_me, up)
        if basis is None:
            return None
        x_hat, y_hat, z_hat = basis
        d_hat = x_hat - np.dot(x_hat, up) * up
        d_hat = d_hat / np.linalg.norm(d_hat)
        origin = radius_m * up  # == local_grid_positions_moon_me(0, 0, 0, ...)
        return cls(center_lon_deg, center_lat_deg, radius_m, basis, np.stack((d_hat, y_hat, z_hat)), origin)

    def to_sun(self, x_m: np.ndarray, y_m: np.ndarray, elevation_m: np.ndarray) -> np.ndarray:
        """`(..., 3)` sun-frame `(D, Y, Z)` of local Orthographic points plus elevation."""
        args = (self.center_lon_deg, self.center_lat_deg, self.radius_m)
        return (local_grid_positions_moon_me(x_m, y_m, elevation_m, *args) - self.origin) @ self.matrix.T

    def from_sun(self, sun_xyz: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Inverse of `to_sun`: `(x_m, y_m, elevation_m)` of `(..., 3)` sun-frame points."""
        positions = np.asarray(sun_xyz, dtype=np.float64) @ np.linalg.inv(self.matrix).T + self.origin
        return local_grid_coords_from_moon_me(positions, self.center_lon_deg, self.center_lat_deg, self.radius_m)

    def proj_pipeline(self) -> str:
        """PROJ pipeline string: local Orthographic x/y/elevation -> MOON_ME -> `(D, Y, Z)`."""
        # Step 1 inverts `geo_utils.local_orthographic_crs` itself (its `+no_defs` is a CRS-only flag),
        # so this frame can't drift from the one the DEM is gridded on. `affine` computes
        # `matrix @ p + offset`, so the origin shift folds into `offset = -matrix @ origin`.
        ortho = local_orthographic_crs(self.center_lon_deg, self.center_lat_deg, self.radius_m).replace(" +no_defs", "")
        offset = -self.matrix @ self.origin
        terms = [f"+{axis}off={float(offset[i])!r}" for i, axis in enumerate("xyz")]
        terms += [f"+s{i + 1}{j + 1}={float(self.matrix[i, j])!r}" for i in range(3) for j in range(3)]
        return (
            f"+proj=pipeline +step +inv {ortho} +step +proj=cart +R={self.radius_m!r} "
            f"+step +proj=affine {' '.join(terms)}"
        )


def sweep_illuminated(z_max: np.ndarray) -> np.ndarray:
    """Per-bin lit/shadowed classification of a sun-frame height raster.

    :param z_max: `(nd, ny)` per-bin max `z`, axis 0 = `d` increasing toward the Sun; NaN for an
        empty bin.
    :returns: float array, same shape: `1.0` lit, `0.0` shadowed, NaN for an empty bin.
    """
    # Sweep from the highest `d` (the sun-facing edge) inward. An empty bin
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
    # Rows are ordered by `d` (horizontal distance toward the Sun), not by `x` (distance along the
    # ray). In the vertical plane of the Sun's azimuth, `z = h cos(e) - d sin(e)`, and terrain Q shadows
    # P iff `d_Q > d_P` and `z_Q > z_P`, at any sun elevation `e`. A DEM is single-valued in `d`, so
    # that ordering is always right. Ordering by `x = d cos(e) + h sin(e)` instead folds on any
    # sun-facing slope steeper than `90 - e` degrees (6 deg at 84 deg sun), letting terrain on the far
    # side cast false shadows. Curvature tilts local up by up to a few degrees across a DEM, so `d`
    # itself only folds on slopes within that angle of vertical.
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
    frame = SunFrame.create(center_lon_deg, center_lat_deg, sun_direction_moon_me, radius_m)
    bin_size_m = (bbox[2] - bbox[0]) / width / upsample_factor * bin_size_factor
    if frame is None:
        up = local_enu_basis(center_lon_deg, center_lat_deg)[2]
        illumination = np.where(np.isnan(dem), np.nan, 1.0).astype(np.float32)
        return SunSweep(illumination, (up, up, up), bin_size_m, np.zeros((0, 0), dtype=np.int32))

    chunks = [(r, min(r + chunk_rows, height)) for r in range(0, height, chunk_rows)]
    grid = _sun_grid(frame, dem, bbox, chunks, bin_size_m)
    z_max, counts, bin_index = _bin_heights(frame, grid, dem, bbox, chunks, upsample_factor)
    lit = sweep_illuminated(np.where(counts > 0, z_max, np.nan))
    illumination = _gather_illumination(lit, bin_index, chunks, width, upsample_factor)
    return SunSweep(illumination, frame.basis, bin_size_m, counts)


@dataclasses.dataclass(frozen=True)
class _SunGrid:
    """The sun-frame raster: `(nx, ny)` bins of `bin_size_m` over `(D, Y)`, lower corner `lo`."""

    lo: np.ndarray
    bin_size_m: float
    nx: int
    ny: int

    def flat_index(self, sun_xyz: np.ndarray) -> np.ndarray:
        """int32 flat bin index (`ix * ny + iy`) of each sample, clipped into range."""
        ix = np.clip(((sun_xyz[..., 0] - self.lo[0]) / self.bin_size_m).astype(np.int64), 0, self.nx - 1)
        iy = np.clip(((sun_xyz[..., 1] - self.lo[1]) / self.bin_size_m).astype(np.int64), 0, self.ny - 1)
        return (ix * self.ny + iy).astype(np.int32)


def _sun_grid(frame: "SunFrame", dem: np.ndarray, bbox: tuple, chunks: list, bin_size_m: float) -> _SunGrid:
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
        xy = frame.to_sun(xg, yg, dem_filled[row0:row1])[..., :2].reshape(-1, 2)
        lo, hi = np.minimum(lo, xy.min(axis=0)), np.maximum(hi, xy.max(axis=0))
    pad = 3 * bin_size_m
    nx, ny = (int(np.ceil(n)) for n in (hi - lo + 2 * pad) / bin_size_m)
    return _SunGrid(lo - pad, bin_size_m, nx, ny)


def _bin_heights(
    frame: "SunFrame", grid: _SunGrid, dem: np.ndarray, bbox: tuple, chunks: list, f: int
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
        sun_xyz = frame.to_sun(xg, yg, np.where(valid, z, 0.0))  # masked out below
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


@dataclasses.dataclass(frozen=True, eq=False)
class HorizonSweep:
    """`horizon_sweep`'s result.

    :ivar illumination_fraction: float32, the DEM's own shape. `1` = fully lit, `0` = fully
        cast-shadowed, NaN where the DEM itself is NaN.
    :ivar frame: The sun frame, or `None` if the Sun is overhead.
    :ivar grid_spacing_m: Sun-grid spacing, meters.
    :ivar grid_shape: `(nd, ny)` sun-grid shape (axis 0 = `D`, toward the Sun).
    :ivar max_terrain_residual_m: Largest height mismatch left by the terrain solve over valid grid
        points. Diagnostic only.
    """

    illumination_fraction: np.ndarray
    frame: SunFrame | None
    grid_spacing_m: float
    grid_shape: tuple[int, int]
    max_terrain_residual_m: float


def horizon_sweep(
    dem: np.ndarray,
    bbox: tuple,
    center_lon_deg: float,
    center_lat_deg: float,
    sun_direction_moon_me,
    radius_m: float = MOON_RADIUS_M,
    upsample_factor: int = UPSAMPLE_FACTOR,
    grid_spacing_m: float | None = None,
    chunk_rows: int = CHUNK_ROWS,
) -> HorizonSweep:
    """Cast-shadow illumination fraction for `dem`, by resampling the terrain onto a sun-aligned grid.

    :param dem: Elevation, meters, on the north-up local Orthographic grid described by `bbox`.
    :param bbox: `(minx, miny, maxx, maxy)`, meters, `geo_utils.local_orthographic_crs` frame.
    :param center_lon_deg: That frame's tangent point longitude, degrees.
    :param center_lat_deg: That frame's tangent point latitude, degrees.
    :param sun_direction_moon_me: Sun direction, MOON_ME (`illumination.sun_direction_moon_me`).
    :param radius_m: Sphere radius, meters.
    :param upsample_factor: Lit/shadowed samples per output pixel, per axis; see `UPSAMPLE_FACTOR`.
    :param grid_spacing_m: Sun-grid spacing, meters. Defaults to the DEM pixel size over
        `upsample_factor`, so the sun grid is as fine as the samples it's tested against.
    :param chunk_rows: See `CHUNK_ROWS`.
    :returns: A `HorizonSweep`.
    """
    # Unlike `sun_sweep`, nothing here takes a max over a bin's footprint. A 100 m-wide bin max stands in
    # for a zero-width ray, overstating each occluder by (cross-sun slope) x (bin width); where that
    # exceeds a grazing slope's real margin, which samples happen to share a bin decides lit vs.
    # shadowed, and that varies periodically with the two grids' relative phase (the "screen door").
    #
    #   1. Resample: each sun-grid node `(D, Y)` gets the terrain's own `Z` there (`_terrain_z`).
    #   2. Sweep: each node's horizon is the max `Z` over nodes strictly closer to the Sun in its `Y`
    #      column -- a running max, streamed from the sun-facing edge inward.
    #   3. Test: each fine DEM sample is lit iff its `Z` reaches the horizon at its own `(D, Y)`;
    #      each native pixel averages its `upsample_factor**2` samples.
    dem = np.asarray(dem, dtype=np.float64)
    height, width = dem.shape
    spacing = grid_spacing_m or (bbox[2] - bbox[0]) / width / upsample_factor
    frame = SunFrame.create(center_lon_deg, center_lat_deg, sun_direction_moon_me, radius_m)
    if frame is None:
        illumination = np.where(np.isnan(dem), np.nan, 1.0).astype(np.float32)
        return HorizonSweep(illumination, None, spacing, (0, 0), 0.0)

    chunks = [(r, min(r + chunk_rows, height)) for r in range(0, height, chunk_rows)]
    grid = _sun_grid(frame, dem, bbox, chunks, spacing)
    spline_coeffs, nan_mask = _spline_coefficients(dem)
    horizon, residual = _horizon_grid(frame, grid, spline_coeffs, nan_mask, bbox)
    illumination = _gather_horizon(frame, grid, horizon, spline_coeffs, nan_mask, bbox, chunks, upsample_factor)
    return HorizonSweep(illumination, frame, spacing, (grid.nx, grid.ny), residual)


def _dem_pixel_coords(bbox: tuple, shape: tuple, x_m: np.ndarray, y_m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Fractional `(row, col)` native pixel indices of local Orthographic points (pixel centers are
    integers, matching `geo_utils.pixel_center_coords_m` and `_fine_heights`)."""
    height, width = shape
    col = (x_m - bbox[0]) / ((bbox[2] - bbox[0]) / width) - 0.5
    row = (bbox[3] - y_m) / ((bbox[3] - bbox[1]) / height) - 0.5
    return row, col


def _terrain_z(
    frame: SunFrame, d: np.ndarray, y: np.ndarray, spline_coeffs: np.ndarray, nan_mask: np.ndarray, bbox: tuple
) -> tuple[np.ndarray, float]:
    """The terrain's `Z` at sun-frame `(d, y)`: NaN where that point is off the DEM or in a NaN pixel.

    :returns: `(z, max_residual_m)`.
    """
    # Fixed `(D, Y)` is a straight line along the tangent point's vertical (`from_sun` is affine in
    # MOON_ME). Away from the tangent point, curvature tilts true vertical off that line by up to a few
    # degrees, so where the line meets the terrain isn't just "the DEM height at the zero-elevation
    # point" -- solve for it. Each step moves `Z` by the height mismatch times `dZ/dh`, which is
    # `z_hat . up` (= cos sun elevation) at the tangent point; the leftover error shrinks by roughly
    # slope x tan(tilt) (~0.02) per step.
    shape = nan_mask.shape
    dz_dh = float(frame.basis[2] @ (frame.origin / frame.radius_m))
    z = np.full(d.shape, np.nan)
    x0, y0, _ = frame.from_sun(np.stack((d, y, np.zeros_like(d)), axis=-1))
    margin = _TERRAIN_SOLVE_BBOX_MARGIN_M
    near = (x0 > bbox[0] - margin) & (x0 < bbox[2] + margin) & (y0 > bbox[1] - margin) & (y0 < bbox[3] + margin)
    if not near.any():
        return z, 0.0
    dn, yn, zn = d[near], y[near], np.zeros(int(near.sum()))
    for iteration in range(_TERRAIN_SOLVE_MAX_ITERATIONS + 1):
        xs, ys, hs = frame.from_sun(np.stack((dn, yn, zn), axis=-1))
        row, col = _dem_pixel_coords(bbox, shape, xs, ys)
        mismatch = ndimage.map_coordinates(spline_coeffs, [row, col], order=3, mode="mirror", prefilter=False) - hs
        valid = (xs >= bbox[0]) & (xs <= bbox[2]) & (ys >= bbox[1]) & (ys <= bbox[3])
        residual = float(np.abs(mismatch[valid]).max()) if valid.any() else 0.0
        if residual < TERRAIN_SOLVE_TOLERANCE_M or iteration == _TERRAIN_SOLVE_MAX_ITERATIONS:
            break
        zn = zn + mismatch * dz_dh
    nearest = (np.clip(np.rint(row), 0, shape[0] - 1).astype(int), np.clip(np.rint(col), 0, shape[1] - 1).astype(int))
    valid &= ~nan_mask[nearest]
    z[near] = np.where(valid, zn, np.nan)
    return z, residual


def _horizon_grid(
    frame: SunFrame, grid: _SunGrid, spline_coeffs: np.ndarray, nan_mask: np.ndarray, bbox: tuple
) -> tuple[np.ndarray, float]:
    """Pass 1+2: each sun-grid node's horizon, the max terrain `Z` over nodes strictly closer to the
    Sun in its column.

    :returns: `(horizon, max_residual_m)`: `(nx, ny)` float32, `-inf` where nothing closer to the Sun
        is on the DEM; and `_terrain_z`'s worst residual.
    """
    # Node `(i, j)` sits at `grid.lo + (i, j) * spacing`. Streamed in blocks of `D` rows from the
    # sun-facing edge inward, carrying the running max across blocks.
    s = grid.bin_size_m
    y = grid.lo[1] + np.arange(grid.ny) * s
    horizon = np.empty((grid.nx, grid.ny), dtype=np.float32)
    running = np.full(grid.ny, -np.inf)
    residual = 0.0
    block = max(1, _HORIZON_CHUNK_POINTS // grid.ny)
    for top in range(grid.nx, 0, -block):
        bottom = max(top - block, 0)
        dg, yg = np.meshgrid(grid.lo[0] + np.arange(bottom, top) * s, y, indexing="ij")
        z, block_residual = _terrain_z(frame, dg, yg, spline_coeffs, nan_mask, bbox)
        residual = max(residual, block_residual)
        toward_sun_first = np.where(np.isnan(z), -np.inf, z)[::-1]
        inclusive = np.maximum.accumulate(np.vstack((running[None], toward_sun_first)), axis=0)
        horizon[bottom:top] = inclusive[:-1][::-1]
        running = inclusive[-1]
    return horizon, residual


def _gather_horizon(
    frame: SunFrame,
    grid: _SunGrid,
    horizon: np.ndarray,
    spline_coeffs: np.ndarray,
    nan_mask: np.ndarray,
    bbox: tuple,
    chunks: list,
    f: int,
) -> np.ndarray:
    """Pass 3: test each fine sample against the horizon at its own `(D, Y)`; average per native pixel.

    :returns: float32 `(height, width)` illumination fraction, NaN where every sample is NaN.
    """
    # Along `D`, a sample takes the horizon of the nearest node on its down-sun side, `floor`: that
    # node's horizon covers every node strictly up-sun of it, which is exactly every node up-sun of the
    # sample. (Interpolating toward the next node up-sun would drop that node from the horizon.)
    # Across `Y`, it interpolates linearly between the two neighboring columns, so on a planar
    # cross-sun slope the horizon is the plane's own value at the sample's `Y` -- not biased toward
    # either column. If either column has no horizon (`-inf`: nothing up-sun of it is on the DEM),
    # the sample is lit -- falling back to the other column alone would overstate the horizon by the
    # cross-sun slope times the spacing, and terrain beyond the DEM isn't modeled anyway. That only
    # affects samples within one spacing of the DEM's border.
    height, width = nan_mask.shape
    x_fine, y_fine = pixel_center_coords_m(bbox, width * f, height * f)
    s = grid.bin_size_m
    illumination = np.empty((height, width), dtype=np.float32)
    for row0, row1 in chunks:
        z = _fine_heights(spline_coeffs, nan_mask, row0, row1, f)
        xg, yg = np.meshgrid(x_fine, y_fine[row0 * f : row1 * f])
        valid = ~np.isnan(z)
        sun_xyz = frame.to_sun(xg, yg, np.where(valid, z, 0.0))
        i = np.clip(np.floor((sun_xyz[..., 0] - grid.lo[0]) / s).astype(np.int64), 0, grid.nx - 1)
        fy = (sun_xyz[..., 1] - grid.lo[1]) / s
        j = np.clip(np.floor(fy).astype(np.int64), 0, grid.ny - 2)
        w = np.clip(fy - j, 0.0, 1.0)
        h0, h1 = horizon[i, j].astype(np.float64), horizon[i, j + 1].astype(np.float64)
        both = np.isfinite(h0) & np.isfinite(h1)
        local_horizon = np.where(both, (1 - w) * np.where(both, h0, 0.0) + w * np.where(both, h1, 0.0), -np.inf)
        lit = sun_xyz[..., 2] >= local_horizon - HORIZON_TOLERANCE_M
        illumination[row0:row1] = _mean_over_blocks(np.where(valid, lit, np.nan), f)
    return illumination


def _mean_over_blocks(samples: np.ndarray, f: int) -> np.ndarray:
    """Mean of each `f x f` block of `samples`, ignoring NaN; NaN where a whole block is."""
    blocks = samples.reshape(samples.shape[0] // f, f, samples.shape[1] // f, f)
    n_valid = np.sum(~np.isnan(blocks), axis=(1, 3))
    total = np.nansum(blocks, axis=(1, 3))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n_valid > 0, total / n_valid, np.nan).astype(np.float32)


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
