"""Corrects a defect in the archived WAC_EMP equirect/polar tiles: both sides carry an
edge-brightening artifact right at their shared ±60° boundary (see
`docs/proposed-tasks/open-items.md`), not something this project's mosaicking introduces. The
equirect side is a single bright native row (masked outright); the polar side is a damped-oscillation
overshoot/undershoot (masked through its worst pixels, then modeled and subtracted). See
`notebooks/wac_emp_seam_correction.py` for how this was characterized and validated.

The polar model (`_fit_local_polar_edge_model`) is fit live, per window, from that window's real
near-boundary pixels rather than one fixed number per hemisphere -- fitted amplitude varies more than
2x between windows at the same latitude, which a single hemisphere-wide constant can't represent.
`_SOUTH`/`_NORTH` below serve two narrower roles instead: `curve_fit`'s initial guess, and the
fallback when a window doesn't carry enough near-boundary pixels for a trustworthy fit of its own. The
mask/correction/reference zone widths (`_POLAR_EDGE_MASK_MAX_PX`/`_CORRECTION_MAX_PX`/
`_REFERENCE_MAX_PX`) are still shared, hemisphere-wide constants tuned only against two known-bad
south entries -- not yet revisited for the same per-window variation the model fit now tracks.

Kept in its own module, separate from `ortho_wac_emp.py`'s reprojection machinery: this is a fix for
one specific archive defect, not a structural part of reprojection. `ortho_wac_emp.py` calls it only
when `TrntestConfig.wac_emp_edge_correction_enabled` is true (the default) -- set it false to get the
raw, uncorrected archive data, e.g. if USGS/ASU fix the underlying tiles.
"""

import dataclasses
import math

import numpy as np
from rasterio.warp import transform as warp_transform
from scipy import ndimage
from scipy.optimize import curve_fit

from trntest.geo_utils import geographic_crs

# Real reflectance is never anywhere near this -- distinguishes real data from either nodata
# convention a windowed read can carry (a numeric sentinel around -3.4e38, or NaN from
# `boundless=True`'s fill) without needing to know which applies. Also robust to the embedded
# sentinel not always comparing equal to itself bit-for-bit (a PDS3-label precision quirk).
_REAL_REFLECTANCE_MIN = -1e30

# How far equatorward of the boundary `mask_and_correct_polar_edge`'s mask is allowed to reach -- the
# sub-pixel raster-alignment slop between the two tiles' coverage, not unbounded. Without this lower
# bound, `distance_px < polar_edge_mask_max_px` alone is also satisfied by every pixel anywhere
# equatorward of the boundary (typically already nodata there, so no pixel *values* were ever
# corrupted by the missing bound -- but it inflated `masked_out`'s footprint by orders of magnitude,
# up to 65% of an entire destination frame in one case, defeating `fill_nearby_gaps`'s `eligible`
# scoping).
_POLAR_EDGE_EQUATORWARD_SLOP_PX = 1.0


@dataclasses.dataclass(frozen=True)
class _EdgeParams:
    """One hemisphere's fallback/initial-guess correction: the damped-cosine model
    (`baseline + amplitude * exp(-x/tau) * cos(omega*x)`) `mask_and_correct_polar_edge` subtracts,
    plus the mask/correction/reference zone widths (native pixels) both functions use.
    `boundary_lat_deg` is the signed latitude (-60.0/60.0), a local value rather than importing
    `ortho_wac_emp.WAC_EMP_MAX_ABS_LATITUDE_DEG` to avoid a circular import (`ortho_wac_emp.py`
    imports this module, not the reverse).
    """

    boundary_lat_deg: float
    polar_edge_amplitude: float
    polar_edge_tau_px: float
    polar_edge_omega: float
    polar_edge_mask_max_px: float
    polar_edge_correction_max_px: float
    polar_edge_reference_max_px: float


# South: fit to `WAC_EMP_643NM_E300S1350_304P`/`WAC_EMP_643NM_P900S0000_304P`'s combined full-circle
# profile (see notebooks/wac_emp_seam_correction.py) -- now used only as `curve_fit`'s initial guess
# and this hemisphere's fallback when a window's live fit isn't trustworthy. The mask/correction/
# reference widths remain in real use, not just fallback: 2px removes the first two, worst-corrected
# native pixels outright rather than leaving them under-corrected by a model whose peak (at x=0)
# undershoots the true, unfit peak just before it (too few samples at x<0 to fit reliably). 8px covers
# the damped cosine's residual wobble past one full period (~6.9px on the original fit); the next 5px
# past it (8-13px) is the "already clean" reference baseline.
_SOUTH = _EdgeParams(
    boundary_lat_deg=-60.0,
    polar_edge_amplitude=0.00706,
    polar_edge_tau_px=1.541,
    polar_edge_omega=0.912,
    polar_edge_mask_max_px=2.0,
    polar_edge_correction_max_px=8.0,
    polar_edge_reference_max_px=13.0,
)
# North: fit to the matched-longitude-zone tile pair (135°E, same zone as south's original fit).
# Amplitude came out ~4x smaller than south's, with a slower decay (larger tau) -- now used only as
# `curve_fit`'s initial guess and this hemisphere's fallback; reusing south's numbers directly would
# over-correct most north windows. Widths carried over unchanged from south's tuning, same caveat as
# `_SOUTH`'s comment.
_NORTH = _EdgeParams(
    boundary_lat_deg=60.0,
    polar_edge_amplitude=0.00179,
    polar_edge_tau_px=4.047,
    polar_edge_omega=1.021,
    polar_edge_mask_max_px=2.0,
    polar_edge_correction_max_px=8.0,
    polar_edge_reference_max_px=13.0,
)
_BOUNDARIES = (_SOUTH, _NORTH)

# Live per-window fit settings -- same methodology notebooks/wac_emp_seam_correction.py used offline
# (bin by distance from the boundary, fit only the poleward side since x<0 bins sit on the tile's true
# sub-pixel edge and are too sparse to trust), run per window instead of once per hemisphere.
# `_FIT_MAX_DISTANCE_PX` matches both hemispheres' `polar_edge_reference_max_px` (13px) -- the model
# has nothing to say past where the correction stops using it. `_MIN_FIT_BIN_COUNT`/`_MIN_FIT_BINS`
# gate against a window that grazes the boundary too narrowly for a trustworthy fit; every window
# checked so far populated 127-130 of the 130 possible 0.1px bins with thousands of samples each, so
# 50 populated bins is a conservative floor.
_FIT_BIN_WIDTH_PX = 0.1
_FIT_MAX_DISTANCE_PX = 13.0
_MIN_FIT_BIN_COUNT = 100
_MIN_FIT_BINS = 50


def _damped_cosine(x: np.ndarray, baseline: float, amplitude: float, tau: float, omega: float) -> np.ndarray:
    return baseline + amplitude * np.exp(-x / tau) * np.cos(omega * x)


def _fit_local_polar_edge_model(
    distance_px: np.ndarray, values: np.ndarray, initial_guess: tuple[float, float, float, float]
) -> tuple[float, float, float] | None:
    """Fit this window's edge-brightening/undershoot shape directly from its poleward (`x >= 0`)
    pixels, instead of trusting one number for the whole hemisphere -- see the module docstring for
    why. Returns `None` (fall back to the hemisphere-wide default) when the window doesn't carry
    enough near-boundary pixels for a trustworthy fit, or `curve_fit` fails to converge within the
    physically-plausible bounds below.

    :param distance_px: Same-shape signed distance from the boundary (native pixels, positive =
        poleward) as `values` -- the whole window's array, not pre-filtered to any zone.
    :param values: This window's real-or-nodata pixel values, same shape as `distance_px`.
    :param initial_guess: `(baseline, amplitude, tau, omega)` starting point for `curve_fit` -- the
        relevant hemisphere's fallback values, since the shape is the same kind of damped cosine
        everywhere, just scaled/timed differently per window.
    :returns: `(amplitude, tau, omega)` -- `baseline` is discarded; the caller re-levels against this
        window's real reference-zone mean instead of trusting the fitted absolute level.
    """
    keep = (values > _REAL_REFLECTANCE_MIN) & (distance_px >= 0) & (distance_px <= _FIT_MAX_DISTANCE_PX)
    if not keep.any():
        return None
    d, v = distance_px[keep], values[keep]
    bin_edges = np.arange(0.0, _FIT_MAX_DISTANCE_PX + _FIT_BIN_WIDTH_PX, _FIT_BIN_WIDTH_PX)
    bin_idx = np.digitize(d, bin_edges)
    centers, means, sems, counts = [], [], [], []
    for b in range(1, len(bin_edges)):
        bin_mask = bin_idx == b
        n = int(bin_mask.sum())
        if n == 0:
            continue
        vals = v[bin_mask]
        centers.append((bin_edges[b - 1] + bin_edges[b]) / 2)
        means.append(vals.mean())
        sems.append(vals.std(ddof=1) / math.sqrt(n) if n > 1 else np.nan)
        counts.append(n)
    counts_arr = np.array(counts)
    fit_mask = counts_arr >= _MIN_FIT_BIN_COUNT
    if fit_mask.sum() < _MIN_FIT_BINS:
        return None
    centers_fit = np.array(centers)[fit_mask]
    means_fit = np.array(means)[fit_mask]
    sems_fit = np.array(sems)[fit_mask]
    try:
        fit_params, _ = curve_fit(
            _damped_cosine,
            centers_fit,
            means_fit,
            p0=initial_guess,
            sigma=sems_fit,
            absolute_sigma=True,
            bounds=([-1.0, -0.1, 0.01, 0.01], [2.0, 0.1, 100.0, 20.0]),
            maxfev=10000,
        )
    except (RuntimeError, ValueError):
        return None
    if not np.isfinite(fit_params).all():
        return None
    _, amplitude, tau, omega = fit_params
    return amplitude, tau, omega


def mask_equirect_edge_row(
    reflectance: np.ndarray, transform, crs, moon_radius_m: float, src_nodata: float, masked_out: np.ndarray
) -> None:
    """Mask (in place) this window's last real row before whichever ±60° equirect/polar boundary it's
    actually near (checks both south and north -- a tile can only be near one, so the other check is
    always a no-op): a single-row brightening artifact baked into the archived equirect tile itself,
    not this project's mosaicking. A no-op whenever this window doesn't reach either boundary, the
    common case for most footprints.

    :param reflectance: This tile's native-resolution windowed read, modified in place.
    :param transform: `reflectance`'s affine transform (in `crs`).
    :param crs: This tile's real source CRS (not a longitude-shifted reinterpretation of it).
    :param moon_radius_m: Sphere radius, meters.
    :param src_nodata: This tile's nodata value -- masked pixels are set to this (not `NaN`
        unconditionally), so the `rasterio.warp.reproject` call after this still recognizes them as
        nodata via its `src_nodata` parameter.
    :param masked_out: Same-shape boolean array, set `True` (in place) wherever this call masks a
        pixel -- lets the reprojection pipeline track which destination pixels came from a masked
        native pixel, so `fill_nearby_gaps` can be scoped to just the coverage gap this correction
        opens, not any other gap nearby.
    """
    height = reflectance.shape[0]
    for params in _BOUNDARIES:
        (x_boundary_m,), (y_boundary_m,) = warp_transform(
            geographic_crs(moon_radius_m), crs, [0.0], [params.boundary_lat_deg]
        )
        _, boundary_row = ~transform * (x_boundary_m, y_boundary_m)
        # Exactly one of these two candidate rows can hold this tile's real data: whichever side of
        # the boundary this tile's coverage falls on -- checking for real (non-nodata) data rather
        # than assuming a row-index direction keeps this correct regardless.
        for row in (int(math.floor(boundary_row)), int(math.floor(boundary_row)) + 1):
            if 0 <= row < height and (reflectance[row] > _REAL_REFLECTANCE_MIN).any():
                reflectance[row] = src_nodata
                masked_out[row] = True
                return


def mask_and_correct_polar_edge(
    reflectance: np.ndarray, transform, crs, moon_radius_m: float, src_nodata: float, masked_out: np.ndarray
) -> None:
    """Mask (in place) the polar tile's real coverage near whichever ±60° boundary it's actually near
    (checks both south and north -- a polar tile only has coverage near its own pole's boundary, so
    the other check's zones always end up empty and it returns immediately) through
    `polar_edge_mask_max_px` native pixels poleward of the boundary (sub-pixel raster-alignment slop
    equatorward of it, plus the first, worst-corrected pixels poleward -- see `_SOUTH`/`_NORTH`'s
    comments), then subtracts a damped-cosine model -- fit live from this window's real near-boundary
    pixels, falling back to the hemisphere's default when the window doesn't carry enough data for a
    trustworthy fit (`_fit_local_polar_edge_model`) -- from the next `polar_edge_correction_max_px`
    pixels past the mask, leveled to this window's real local baseline (the mean of the next
    `polar_edge_reference_max_px` pixels past the correction zone, already clean) rather than trusting
    the fitted model's absolute baseline parameter. A no-op whenever this window doesn't reach either
    boundary.

    :param reflectance: This tile's native-resolution windowed read, modified in place.
    :param transform: `reflectance`'s affine transform (in `crs`).
    :param crs: This tile's real source CRS (not a longitude-shifted reinterpretation of it).
    :param moon_radius_m: Sphere radius, meters.
    :param src_nodata: This tile's nodata value -- the mask is set to this (not `NaN` unconditionally),
        so the `rasterio.warp.reproject` call after this still recognizes it as nodata via its
        `src_nodata` parameter. Pixels already nodata going in (either convention) are left untouched
        by the correction, not corrupted by arithmetic against them.
    :param masked_out: Same-shape boolean array, set `True` (in place) wherever this call masks a
        pixel -- see `mask_equirect_edge_row`'s parameter of the same name.
    """
    height, width = reflectance.shape
    # This CRS's projected origin is the pole (false easting/northing both 0) -- inverting the
    # transform there gives the pole's exact location in this window's pixel grid. Shared across both
    # boundary checks below: the pole location doesn't depend on which hemisphere's boundary is being
    # tested.
    center_col, center_row = (~transform) * (0.0, 0.0)
    rows = np.arange(height).reshape(-1, 1) - center_row
    cols = np.arange(width).reshape(1, -1) - center_col
    radius_px = np.sqrt(rows.astype(np.float64) ** 2 + cols.astype(np.float64) ** 2)

    for params in _BOUNDARIES:
        (x_boundary_m,), (y_boundary_m,) = warp_transform(
            geographic_crs(moon_radius_m), crs, [0.0], [params.boundary_lat_deg]
        )
        pixel_size_m = abs(transform.a)
        boundary_radius_px = math.hypot(x_boundary_m, y_boundary_m) / pixel_size_m
        distance_px = boundary_radius_px - radius_px  # positive = poleward

        correction_zone = (distance_px >= params.polar_edge_mask_max_px) & (
            distance_px <= params.polar_edge_correction_max_px
        )
        reference_zone = (distance_px > params.polar_edge_correction_max_px) & (
            distance_px <= params.polar_edge_reference_max_px
        )
        mask_zone = (distance_px < params.polar_edge_mask_max_px) & (distance_px > -_POLAR_EDGE_EQUATORWARD_SLOP_PX)
        if not correction_zone.any() and not mask_zone.any():
            continue  # this window is nowhere near this boundary -- try the other one

        reference_values = reflectance[reference_zone]
        reference_valid = reference_values > _REAL_REFLECTANCE_MIN
        baseline_guess = float(reference_values[reference_valid].mean()) if reference_valid.any() else 0.12
        fit = _fit_local_polar_edge_model(
            distance_px,
            reflectance,
            (baseline_guess, params.polar_edge_amplitude, params.polar_edge_tau_px, params.polar_edge_omega),
        )
        amplitude, tau, omega = (
            fit if fit is not None else (params.polar_edge_amplitude, params.polar_edge_tau_px, params.polar_edge_omega)
        )

        reflectance[mask_zone] = src_nodata
        masked_out[mask_zone] = True
        if not correction_zone.any():
            return
        zone_values = reflectance[correction_zone]
        zone_valid = zone_values > _REAL_REFLECTANCE_MIN
        if not zone_valid.any():
            return
        zone_distance = distance_px[correction_zone]
        model = amplitude * np.exp(-zone_distance[zone_valid] / tau) * np.cos(omega * zone_distance[zone_valid])
        corrected = zone_values.copy()
        corrected[zone_valid] = zone_values[zone_valid] - model

        if reference_valid.any():
            level_offset = reference_values[reference_valid].mean() - corrected[zone_valid].mean()
            corrected[zone_valid] += level_offset

        reflectance[correction_zone] = corrected
        return


# Masking the equirect tile's bad edge row (`mask_equirect_edge_row`) and the polar tile's edge
# (`mask_and_correct_polar_edge`) both remove pixels that used to carry (bad) data -- at any
# destination pixel where that was the only tile with coverage there (a real effect, since the two
# tiles' coverage boundary is a jagged, locally-diagonal line in the destination grid, not a clean
# cut: see notebooks/wac_emp_seam_correction.py), `ortho_wac_emp`'s merge has no value for it at all.
#
# Sized from a scan of every boundary-straddling entry across two populated datasets (23 south + 23
# north), isolating this correction's own gap from two unrelated coverage-gap issues a blanket
# "distance to nearest valid pixel" would otherwise conflate it with (see `open-items.md`): a
# pre-existing small-gap defect already present in many of these archived tiles, and a much larger,
# structurally different gap in the five entries whose footprints straddle both the ±60° boundary and
# the antimeridian at once. With both excluded, the correction's real masking-induced gap tops out at
# 2.0px across 42 clean two-tile entries -- exactly `polar_edge_mask_max_px` itself. 10px is generous
# relative to that (5x), but harmless: the `eligible` scoping below (not this radius) is what keeps
# the fill from ever touching either excluded issue.
GAP_FILL_MAX_RADIUS_PX = 10.0


def eligible_gap_fill_mask(masked_any: np.ndarray, max_radius_px: float = GAP_FILL_MAX_RADIUS_PX) -> np.ndarray:
    """Which destination pixels `fill_nearby_gaps` is allowed to touch: within `max_radius_px` of some
    pixel `mask_equirect_edge_row`/`mask_and_correct_polar_edge` actually masked -- not just any `NaN`
    that happens to have real data nearby. See `fill_nearby_gaps`'s docstring for why this scoping
    matters in practice, not just in principle.

    :param masked_any: Same-shape boolean array -- `True` wherever any source tile's masked-out native
        pixel maps into this destination pixel (the caller ORs each tile's per-tile masked array
        together, since either tile's masking can open a gap at a shared destination pixel).
    :param max_radius_px: Same radius the caller will pass to `fill_nearby_gaps` -- accepted explicitly
        rather than assumed equal, though every call site today uses `GAP_FILL_MAX_RADIUS_PX` for both.
    """
    if not masked_any.any():
        return np.zeros(masked_any.shape, dtype=bool)
    return ndimage.distance_transform_edt(~masked_any) <= max_radius_px


def fill_nearby_gaps(array: np.ndarray, max_radius_px: float, eligible: np.ndarray) -> np.ndarray:
    """Fill each `NaN` pixel from its nearest real neighbor, but only within `max_radius_px` of one
    *and* only where `eligible` allows it -- `eligible` restricts filling to destination pixels
    actually near this correction's own masking (built by the caller,
    `reproject_wac_emp_reflectance_to_local_grid`, from `mask_equirect_edge_row`/
    `mask_and_correct_polar_edge`'s `masked_out` output), not just any `NaN` that happens to sit within
    reach of real data. That scoping matters in practice: two separate coverage-gap issues, unrelated
    to this correction, live in the same archived tiles (`GAP_FILL_MAX_RADIUS_PX`'s comment) --
    without `eligible`, a radius sized to comfortably close this correction's own gaps would also
    paper over either one wherever it sits near enough, hiding a real data problem instead of
    surfacing it as `NaN`.

    :param array: The merged local-grid array, `NaN` wherever no source tile covers a pixel.
    :param max_radius_px: Pixels farther than this from the nearest real pixel are left `NaN`.
    :param eligible: Same-shape boolean array -- only `NaN` pixels where this is `True` are ever
        filled, regardless of how close real data is.
    :returns: A new array (`array` itself is not modified) with only the eligible gaps closed.
    """
    invalid = np.isnan(array)
    if not invalid.any():
        return array
    distance, nearest_index = ndimage.distance_transform_edt(invalid, return_indices=True)
    fillable = invalid & (distance <= max_radius_px) & eligible
    filled = array.copy()
    filled[fillable] = array[tuple(index[fillable] for index in nearest_index)]
    return filled
