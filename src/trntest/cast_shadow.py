"""Cast-shadow occlusion: which parts of a DEM are blocked from the Sun by *other* terrain.

`illumination_fraction` returns a per-pixel 0-1 multiplier (`1` = fully lit, `0` = fully
cast-shadowed) that `hapke.despeckle_and_shade_ortho` applies on top of its per-facet shading, so
`hillshade` renders get real shadows cast across crater floors, not just dark sun-facing-away walls.

The method is a sun-aligned sweep. `SunFrame` puts the Sun at infinity along `+x`, with `Y`
horizontal and perpendicular to the Sun's azimuth and `Z` perpendicular to the sun rays. Every ray
stays at fixed `Y` and fixed `Z`, so a point is shadowed iff some terrain at its `Y` that is
horizontally closer to the Sun (larger `D`) has a larger `Z`. Resampling the terrain onto a regular
`(D, Y)` grid makes that a running maximum of `Z` down each column, swept from the sun-facing edge
inward: one `np.maximum.accumulate` rather than a per-pixel ray march against the DEM.

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

# Lit/shadowed samples per output pixel, per axis. Each native pixel's illumination is the mean over
# its `UPSAMPLE_FACTOR**2` samples (heights from a cubic spline), which antialiases shadow edges; the
# sun grid defaults to the same spacing.
UPSAMPLE_FACTOR = 2

# Native DEM rows per streaming chunk. Bounds the per-sample working arrays; see `horizon_sweep`.
CHUNK_ROWS = 128

# A sample counts as lit when its `Z` is within this of the horizon: covers the terrain solve's own
# tolerance, float32 horizon storage (~1 mm at the ~10 km `Z` magnitudes curvature reaches across a
# DEM), and the exactly-grazing case, where a sample and its horizon lie on the same plane.
HORIZON_TOLERANCE_M = 0.03

# Terrain solve (`_terrain_z`): a grid node is done once its height mismatch is below this.
TERRAIN_SOLVE_TOLERANCE_M = 0.01
_TERRAIN_SOLVE_MAX_ITERATIONS = 10

# Sun-grid nodes whose zero-elevation location falls more than this far outside the DEM's bbox are
# skipped outright, rather than solved and then discarded. Must exceed the largest horizontal shift the
# terrain solve can make: relief times the tangent of curvature's tilt of local vertical (~5 km x
# tan 4 deg = ~350 m at this project's DEM sizes).
_TERRAIN_SOLVE_BBOX_MARGIN_M = 2000.0

# Target sun-grid nodes per streaming block.
_HORIZON_CHUNK_POINTS = 1 << 20

# Pixels of odd-reflection padding added around the DEM before fitting its spline (see
# `_spline_coefficients`). A cubic B-spline's dependence on a sample decays by ~0.27 per pixel, so the
# spline's own mirror boundary, this far out, moves heights at the real border by ~2e-6 of the fold
# (at 6 px it was still ~1 mm on a 10 m/px ramp).
_SPLINE_PAD_PX = 10

# `sun_aligned_basis` treats the Sun as overhead (no cast shadows possible) when the Sun direction is
# this close to parallel with local up -- the frame's `z` axis is undefined there.
_OVERHEAD_SUN_COS_THRESHOLD = 1.0 - 1e-9


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
        return (local_grid_positions_moon_me(x_m, y_m, elevation_m, *self._tangent_args) - self.origin) @ self.matrix.T

    def from_sun(self, sun_xyz: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Inverse of `to_sun`: `(x_m, y_m, elevation_m)` of `(..., 3)` sun-frame points."""
        positions = np.asarray(sun_xyz, dtype=np.float64) @ np.linalg.inv(self.matrix).T + self.origin
        return local_grid_coords_from_moon_me(positions, *self._tangent_args)

    def column_point(
        self, d: np.ndarray, y: np.ndarray, elevation_m: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """The point at sun-frame `(d, y)` and the given elevation: `(x_m, y_m, z)`, its local
        Orthographic x/y and sun-frame `Z`. `to_sun(x_m, y_m, elevation_m)` gives back `(d, y, z)`.
        """
        # Fixed `(D, Y)` is a straight line along the tangent point's vertical `u` (`from_sun` is
        # affine, and `d_hat`/`y_hat` are both horizontal there), so `P(t) = P0 + t u`, with `P0` the
        # line's `Z = 0` point. `|P(t)| = R + h` is a quadratic in `t` (take the outer root); `Z` is
        # linear in `t` along the line.
        u = self.origin / self.radius_m
        p0 = np.stack((d, y, np.zeros_like(d)), axis=-1) @ np.linalg.inv(self.matrix).T + self.origin
        b = p0 @ u
        t = -b + np.sqrt(b**2 - (np.sum(p0**2, axis=-1) - (self.radius_m + np.asarray(elevation_m)) ** 2))
        x_m, y_m, _ = local_grid_coords_from_moon_me(p0 + t[..., None] * u, *self._tangent_args)
        return x_m, y_m, t * float(self.matrix[2] @ u)

    @property
    def _tangent_args(self) -> tuple[float, float, float]:
        return self.center_lon_deg, self.center_lat_deg, self.radius_m

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


@dataclasses.dataclass(frozen=True, eq=False)
class HorizonSweep:
    """`horizon_sweep`'s result.

    :ivar illumination_fraction: float32, the DEM's own shape. `1` = fully lit, `0` = fully
        cast-shadowed, NaN where the DEM itself is NaN.
    :ivar frame: The sun frame, or `None` if the Sun is overhead.
    :ivar grid_spacing_m: Sun-grid spacing, meters.
    :ivar grid_shape: `(nd, ny)` sun-grid shape (axis 0 = `D`, toward the Sun).
    :ivar terrain_node_fraction: Fraction of sun-grid nodes that land on the DEM. Diagnostic only.
    :ivar max_terrain_residual_m: Largest height mismatch the terrain solve left on any node that
        lands on the DEM. Diagnostic only.
    """

    illumination_fraction: np.ndarray
    frame: SunFrame | None
    grid_spacing_m: float
    grid_shape: tuple[int, int]
    terrain_node_fraction: float
    max_terrain_residual_m: float

    def summary(self) -> str:
        """A few lines describing the sun grid and the result."""
        return "\n".join(
            [
                f"sun grid: {self.grid_shape[0]} (D, toward Sun) x {self.grid_shape[1]} (Y), "
                f"spacing {self.grid_spacing_m:.1f} m",
                f"nodes on the DEM: {self.terrain_node_fraction:.3f}",
                f"terrain solve max residual: {self.max_terrain_residual_m * 1000:.2f} mm",
                f"mean illumination fraction: {np.nanmean(self.illumination_fraction):.3f}",
            ]
        )


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
    """Cast-shadow illumination fraction for `dem`, plus the sun-grid diagnostics behind it.

    :param dem: Elevation, meters, on the north-up local Orthographic grid described by `bbox`.
    :param bbox: `(minx, miny, maxx, maxy)`, meters, `geo_utils.local_orthographic_crs` frame.
    :param center_lon_deg: That frame's tangent point longitude, degrees.
    :param center_lat_deg: That frame's tangent point latitude, degrees.
    :param sun_direction_moon_me: Sun direction, MOON_ME (`illumination.sun_direction_moon_me`).
    :param radius_m: Sphere radius, meters.
    :param upsample_factor: See `UPSAMPLE_FACTOR`.
    :param grid_spacing_m: Sun-grid spacing, meters. Defaults to the DEM pixel size over
        `upsample_factor`, so the sun grid is as fine as the samples tested against it.
    :param chunk_rows: See `CHUNK_ROWS`. Any value gives bit-identical output.
    :returns: A `HorizonSweep`.
    """
    # Positions are true 3D MOON_ME points (`SunFrame`), not a flat tangent-plane approximation:
    # the Moon's curvature (sagitta) over half a ~240 km-wide DEM is ~4.2 km, the same order as typical
    # terrain relief.
    #
    # Columns are ordered by `D` (horizontal distance toward the Sun), not by distance along the ray.
    # In the vertical plane of the Sun's azimuth, `Z = h cos(e) - D sin(e)`, and terrain Q shadows P
    # iff `D_Q > D_P` and `Z_Q > Z_P`, at any sun elevation `e`. A DEM is single-valued in `D`, so that
    # ordering is always right; ordering along the ray instead folds on any sun-facing slope steeper
    # than `90 - e` degrees, letting terrain on the far side cast false shadows.
    #
    # The terrain is *resampled* onto the sun grid (each node gets the terrain's own height there),
    # not binned: a max over each bin's footprint stands in for a zero-width ray and overstates every
    # occluder by (cross-sun slope) x (bin width). On slopes near the sun elevation, that exceeds the
    # real margin, and which samples happen to share a bin -- periodic in the two grids' relative
    # phase -- decides lit vs. shadowed, producing a regular "screen door" of shadow dots.
    #
    # Self-shadow (a facet facing away from the Sun) also comes out shadowed here, but the per-facet
    # reflectance this multiplies against already renders such a facet dark, so it changes nothing.
    #
    # Three passes, memory bounded by streaming:
    #   1. Resample: each node `(D, Y)` gets the terrain's `Z` there (`_terrain_z`).
    #   2. Sweep: each node's horizon is the max `Z` over nodes strictly closer to the Sun in its
    #      column -- a running max, streamed in blocks of `D` rows from the sun-facing edge inward.
    #      Passes 1 and 2 run together (`_horizon_grid`); only the float32 horizon is kept whole.
    #   3. Test: each fine DEM sample is lit iff its `Z` reaches the horizon at its own `(D, Y)`;
    #      each native pixel averages its `upsample_factor**2` samples (`_gather_horizon`).
    dem = np.asarray(dem, dtype=np.float64)
    height, width = dem.shape
    spacing = grid_spacing_m or (bbox[2] - bbox[0]) / width / upsample_factor
    frame = SunFrame.create(center_lon_deg, center_lat_deg, sun_direction_moon_me, radius_m)
    if frame is None:
        illumination = np.where(np.isnan(dem), np.nan, 1.0).astype(np.float32)
        return HorizonSweep(illumination, None, spacing, (0, 0), 0.0, 0.0)

    chunks = [(r, min(r + chunk_rows, height)) for r in range(0, height, chunk_rows)]
    grid = _sun_grid(frame, dem, bbox, chunks, spacing)
    spline_coeffs, nan_mask = _spline_coefficients(dem)
    horizon, node_fraction, residual = _horizon_grid(frame, grid, spline_coeffs, nan_mask, bbox)
    illumination = _gather_horizon(frame, grid, horizon, spline_coeffs, nan_mask, bbox, chunks, upsample_factor)
    return HorizonSweep(illumination, frame, spacing, (grid.nx, grid.ny), node_fraction, residual)


def _spline_coefficients(dem: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Cubic-spline coefficients for `dem`, padded by `_SPLINE_PAD_PX`; and its (unpadded) NaN mask.

    :param dem: Elevation, float64.
    :returns: `(coefficients, nan_mask)`. Evaluate through `_spline_heights`, which applies the pad
        offset.
    """
    # A NaN would spread through the whole spline, so fit a nearest-valid-filled copy; callers re-mask
    # afterward (a sample is NaN iff the native pixel it lies in is NaN). `spline_filter` runs once,
    # whole-grid, so per-chunk evaluation is identical to a whole-grid spline -- chunk seams can't
    # appear.
    #
    # `ndimage` has no boundary mode that continues the surface past the outermost pixel centers --
    # `mirror` folds it back, which on a slope makes false ridges and false shadows within a pixel or
    # two of the border. Odd reflection through the edge value (`2 h[0] - h[n]`) continues a plane
    # exactly and keeps a curved surface's slope (C1), without amplifying noise the way extrapolating
    # from the last two pixels would; `mirror` then only applies `_SPLINE_PAD_PX` farther out.
    nan_mask = np.isnan(dem)
    if nan_mask.all():
        raise ValueError("dem is entirely NaN")
    if nan_mask.any():
        nearest = ndimage.distance_transform_edt(nan_mask, return_distances=False, return_indices=True)
        dem = dem[tuple(nearest)]
    padded = np.pad(dem, _SPLINE_PAD_PX, mode="reflect", reflect_type="odd")
    return ndimage.spline_filter(padded, order=3, mode="mirror"), nan_mask


def _spline_heights(spline_coeffs: np.ndarray, row: np.ndarray, col: np.ndarray) -> np.ndarray:
    """Spline heights at fractional native `(row, col)` pixel indices (pixel centers are integers)."""
    coords = [np.asarray(row) + _SPLINE_PAD_PX, np.asarray(col) + _SPLINE_PAD_PX]
    return ndimage.map_coordinates(spline_coeffs, coords, order=3, mode="mirror", prefilter=False)


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
    # their own x/y at the DEM edges.)
    width = nan_mask.shape[1]
    row_coords = (np.arange(row0 * f, row1 * f) + 0.5) / f - 0.5
    col_coords = (np.arange(width * f) + 0.5) / f - 0.5
    rr, cc = np.meshgrid(row_coords, col_coords, indexing="ij")
    z = _spline_heights(spline_coeffs, rr, cc)
    z[np.repeat(np.repeat(nan_mask[row0:row1], f, axis=0), f, axis=1)] = np.nan
    return z


@dataclasses.dataclass(frozen=True)
class _SunGrid:
    """The sun grid: `(nx, ny)` nodes at `lo + (i, j) * spacing_m` over `(D, Y)`."""

    lo: np.ndarray
    spacing_m: float
    nx: int
    ny: int


def _sun_grid(frame: SunFrame, dem: np.ndarray, bbox: tuple, chunks: list, spacing_m: float) -> _SunGrid:
    """Size the sun grid to cover every sample.

    :param frame: The sun frame.
    :param dem: Elevation, meters.
    :param bbox: `dem`'s bbox.
    :param chunks: `(row0, row1)` native row ranges.
    :param spacing_m: Node spacing, meters.
    :returns: A `_SunGrid`.
    """
    # From the native grid (1/f**2 as many points as the fine grid), padded by a few nodes for the
    # half-pixel fine-grid overhang and spline overshoot. `_gather_horizon` also clips into range.
    height, width = dem.shape
    dem_filled = np.where(np.isnan(dem), np.nanmean(dem), dem)
    x_native, y_native = pixel_center_coords_m(bbox, width, height)
    lo, hi = np.full(2, np.inf), np.full(2, -np.inf)
    for row0, row1 in chunks:
        xg, yg = np.meshgrid(x_native, y_native[row0:row1])
        xy = frame.to_sun(xg, yg, dem_filled[row0:row1])[..., :2].reshape(-1, 2)
        lo, hi = np.minimum(lo, xy.min(axis=0)), np.maximum(hi, xy.max(axis=0))
    pad = 3 * spacing_m
    nx, ny = (int(np.ceil(n)) for n in (hi - lo + 2 * pad) / spacing_m)
    return _SunGrid(lo - pad, spacing_m, nx, ny)


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

    :returns: `(z, max_residual_m)`, the residual over points on the DEM.
    """
    # Find the elevation `h` where the `(d, y)` column meets the terrain: `h = dem(x(h), y(h))`, with
    # `SunFrame.column_point` giving the exact point at any `h`. The column runs along the tangent
    # point's vertical, which curvature tilts off true vertical by `r / R` (up to a few degrees), so
    # the map location drifts with `h` -- hence iterating. Each pass shrinks the error by about
    # slope x `r / R` (~0.004 for a 0.1 slope 70 km out), starting from `h = 0`; points drop out as
    # they converge.
    shape = nan_mask.shape
    z = np.full(d.shape, np.nan)
    x0, y0, z0 = frame.column_point(d, y, np.zeros_like(d))
    margin = _TERRAIN_SOLVE_BBOX_MARGIN_M
    near = (x0 > bbox[0] - margin) & (x0 < bbox[2] + margin) & (y0 > bbox[1] - margin) & (y0 < bbox[3] + margin)
    if not near.any():
        return z, 0.0
    dn, yn = d[near], y[near]
    h = np.zeros(dn.size)
    xs, ys, zn, mismatch = x0[near], y0[near], z0[near], np.zeros(dn.size)
    active = np.arange(dn.size)
    for iteration in range(_TERRAIN_SOLVE_MAX_ITERATIONS + 1):
        if iteration > 0:  # the `h = 0` points are already in hand
            xs[active], ys[active], zn[active] = frame.column_point(dn[active], yn[active], h[active])
        xa, ya = xs[active], ys[active]
        mismatch[active] = _spline_heights(spline_coeffs, *_dem_pixel_coords(bbox, shape, xa, ya)) - h[active]
        unconverged = np.abs(mismatch[active]) >= TERRAIN_SOLVE_TOLERANCE_M
        if not unconverged.any() or iteration == _TERRAIN_SOLVE_MAX_ITERATIONS:
            break
        active = active[unconverged]
        h[active] += mismatch[active]
    row, col = _dem_pixel_coords(bbox, shape, xs, ys)
    valid = (xs >= bbox[0]) & (xs <= bbox[2]) & (ys >= bbox[1]) & (ys <= bbox[3])
    nearest = (np.clip(np.rint(row), 0, shape[0] - 1).astype(int), np.clip(np.rint(col), 0, shape[1] - 1).astype(int))
    valid &= ~nan_mask[nearest]
    z[near] = np.where(valid, zn, np.nan)
    return z, float(np.abs(mismatch[valid]).max()) if valid.any() else 0.0


def _horizon_grid(
    frame: SunFrame, grid: _SunGrid, spline_coeffs: np.ndarray, nan_mask: np.ndarray, bbox: tuple
) -> tuple[np.ndarray, float, float]:
    """Passes 1 and 2: each sun-grid node's horizon, the max terrain `Z` over nodes strictly closer to
    the Sun in its column.

    :returns: `(horizon, node_fraction, max_residual_m)`: `(nx, ny)` float32, `-inf` where nothing
        closer to the Sun is on the DEM; the fraction of nodes on the DEM; `_terrain_z`'s worst residual.
    """
    s = grid.spacing_m
    y = grid.lo[1] + np.arange(grid.ny) * s
    horizon = np.empty((grid.nx, grid.ny), dtype=np.float32)
    running = np.full(grid.ny, -np.inf)
    residual, n_on_dem = 0.0, 0
    block = max(1, _HORIZON_CHUNK_POINTS // grid.ny)
    for top in range(grid.nx, 0, -block):
        bottom = max(top - block, 0)
        dg, yg = np.meshgrid(grid.lo[0] + np.arange(bottom, top) * s, y, indexing="ij")
        z, block_residual = _terrain_z(frame, dg, yg, spline_coeffs, nan_mask, bbox)
        residual, n_on_dem = max(residual, block_residual), n_on_dem + int(np.isfinite(z).sum())
        toward_sun_first = np.where(np.isnan(z), -np.inf, z)[::-1]
        inclusive = np.maximum.accumulate(np.vstack((running[None], toward_sun_first)), axis=0)
        horizon[bottom:top] = inclusive[:-1][::-1]
        running = inclusive[-1]
    return horizon, n_on_dem / horizon.size, residual


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
    # Along `D`, a sample takes the horizon of the nearest node on its down-sun side (`floor`): that
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
    s = grid.spacing_m
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
    """Cast-shadow illumination fraction for `dem`: `horizon_sweep`'s main output alone.

    :param dem: See `horizon_sweep`.
    :param bbox: See `horizon_sweep`.
    :param center_lon_deg: See `horizon_sweep`.
    :param center_lat_deg: See `horizon_sweep`.
    :param sun_direction_moon_me: See `horizon_sweep`.
    :param radius_m: See `horizon_sweep`.
    :returns: float32, `dem`'s shape: `1` = fully lit, `0` = fully cast-shadowed, NaN where `dem` is.
    """
    sweep = horizon_sweep(dem, bbox, center_lon_deg, center_lat_deg, sun_direction_moon_me, radius_m)
    return sweep.illumination_fraction
