"""A real edge-brightening defect in the archived WAC_EMP equirect/polar tiles themselves, right at
their shared ±60 deg boundary -- not something this project's own mosaicking introduces (see
docs/proposed-tasks/open-items.md). `notebooks/wac_emp_seam_edge_model.py` measured both shapes
directly in each tile's own native pixels and fit the polar side's: a clean single-row spike for the
equirect tile (masked outright, no model needed), and a damped-oscillation overshoot/undershoot for
the polar tile (masked through its own worst-corrected pixels, then modeled).

Confirmed on **both** hemispheres, independently fit for each (`_SOUTH`/`_NORTH` below) -- south from
two known-bad `trntest1` entries, north from checking the archived tiles directly after noticing
`trntest2` has 23 entries straddling +60° with no correction applied, 11 of them showing a >3x
elevated row-to-row jump right at that boundary (up to 15.6x for `M1309348984CE`). North's own
amplitude came out ~4x smaller than south's on the one longitude zone sampled for each -- reusing
south's fitted numbers for north would have over-corrected. The mask/correction/reference *widths*
below (`_POLAR_EDGE_MASK_MAX_PX`/`_CORRECTION_MAX_PX`/`_REFERENCE_MAX_PX`) were only iteratively tuned
against south's own two known-bad entries (`wac_emp_seam_correction_validation.py`) -- north reuses
those same widths structurally, not because they were independently re-validated there, only because
there was no equivalent north tuning pass. `trntest2`'s own affected entries are a real resource for
that validation later; none of them are regenerated as part of this change.

Deliberately kept in its own module, separate from `ortho_wac_emp.py`'s general tile-fetch/reprojection
machinery: this is a correction for a specific, empirically-measured defect in archived data products,
not a structural part of the reprojection pipeline. `ortho_wac_emp.py` calls this module's functions
only when `TrntestConfig.wac_emp_edge_correction_enabled` is true (the default) -- set it false to get
the raw, uncorrected archive data back, e.g. if USGS/ASU fix the underlying tiles.
"""

import dataclasses
import math

import numpy as np
from rasterio.warp import transform as warp_transform
from scipy import ndimage

from trntest.geo_utils import geographic_crs

# Real reflectance is never anywhere near this -- distinguishes real data from either nodata
# convention a source tile's windowed read can carry (a numeric sentinel around -3.4e38, or literal
# NaN from `boundless=True`'s own fill) without needing to know which applies, matching
# `notebooks/wac_emp_seam_edge_model.py`'s own convention (also robust to the embedded sentinel not
# always comparing equal to itself bit-for-bit, a PDS3-label precision quirk noted there).
_REAL_REFLECTANCE_MIN = -1e30


@dataclasses.dataclass(frozen=True)
class _EdgeParams:
    """One hemisphere's own fitted/tuned correction, all in the damped-cosine model
    (`baseline + amplitude * exp(-x/tau) * cos(omega*x)`) `mask_and_correct_polar_edge` subtracts,
    plus the mask/correction/reference zone widths (native pixels) both functions use. `boundary_lat_deg`
    is the literal signed latitude (`-60.0`/`60.0`), not a magnitude -- a local value rather than
    importing `ortho_wac_emp.WAC_EMP_MAX_ABS_LATITUDE_DEG`, to avoid a circular import
    (`ortho_wac_emp.py` imports *this* module, not the other way around).
    """

    boundary_lat_deg: float
    polar_edge_amplitude: float
    polar_edge_tau_px: float
    polar_edge_omega: float
    polar_edge_mask_max_px: float
    polar_edge_correction_max_px: float
    polar_edge_reference_max_px: float


# South: fit to `WAC_EMP_643NM_E300S1350_304P`/`WAC_EMP_643NM_P900S0000_304P`, the two `trntest1`
# known-bad entries' own tile pair. The real peak (`wac_emp_seam_edge_model.py`'s own ~0.132 vs. this
# model's ~0.127 at x=0) sits at x ~ -0.5, outside the fit domain (too few samples there to fit
# reliably) -- masking through x=2 rather than just the sub-pixel x<0 overshoot also removes the
# first two, worst-corrected native pixels outright instead of under-correcting them, at the cost of a
# slightly wider gap for `fill_nearby_gaps` to close. Widening from 1px to 2px measurably grew that gap
# without any further improvement to the seam itself (`wac_emp_seam_correction_validation.py`) -- 2px
# is kept as the best trade-off found, not because more masking keeps helping. 8px covers the damped
# cosine's own residual wobble out past one full period (~6.9px, `2*pi/omega`) -- a shorter 5px cutoff
# leaves that secondary dip/rise uncorrected. The next 5px past it (8-13px) is used as the "already
# clean" reference baseline.
_SOUTH = _EdgeParams(
    boundary_lat_deg=-60.0,
    polar_edge_amplitude=0.00706,
    polar_edge_tau_px=1.541,
    polar_edge_omega=0.912,
    polar_edge_mask_max_px=2.0,
    polar_edge_correction_max_px=8.0,
    polar_edge_reference_max_px=13.0,
)
# North: fit to the matched-longitude-zone tile pair (`WAC_EMP_643NM_E300N1350_304P`/
# `WAC_EMP_643NM_P900N0000_304P`, same 135 deg E zone as south's own tiles) after `trntest2` was found
# to have 23 entries straddling +60° with 11 showing a real, visible artifact (up to 15.6x the typical
# row-to-row jump right at the boundary) -- not a hypothetical extension. Amplitude came out ~4x
# smaller than south's own fit on this one zone, with a slower decay (larger `tau`) -- reusing south's
# fitted amplitude/tau/omega here would over-correct. The mask/correction/reference widths are carried
# over unchanged from south's own tuning (see `_SOUTH`'s comment) rather than independently re-tuned --
# `trntest2`'s own affected entries would be the natural data to validate/refine them against.
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


def mask_equirect_edge_row(reflectance: np.ndarray, transform, crs, moon_radius_m: float, src_nodata: float) -> None:
    """Mask (in place) this window's own last real row before whichever ±60 deg equirect/polar
    boundary it's actually near (checks both south and north -- a real tile can only ever be near one
    of them, so the other check is always a no-op) -- a single-row brightening artifact baked into the
    archived equirect tile itself (`notebooks/wac_emp_seam_edge_model.py`), not this project's own
    mosaicking. A no-op whenever this window doesn't reach anywhere near either boundary, the common
    case for most footprints.

    :param reflectance: This tile's own native-resolution windowed read, modified in place.
    :param transform: `reflectance`'s own affine transform (in `crs`).
    :param crs: This tile's own real source CRS (not a longitude-shifted reinterpretation of it).
    :param moon_radius_m: Sphere radius, meters.
    :param src_nodata: This tile's own nodata value -- masked pixels are set to this (not `NaN`
        unconditionally), so the `rasterio.warp.reproject` call after this still recognizes them as
        nodata via its own `src_nodata` parameter.
    """
    height = reflectance.shape[0]
    for params in _BOUNDARIES:
        (x_boundary_m,), (y_boundary_m,) = warp_transform(
            geographic_crs(moon_radius_m), crs, [0.0], [params.boundary_lat_deg]
        )
        _, boundary_row = ~transform * (x_boundary_m, y_boundary_m)
        # Exactly one of these two candidate rows can hold this tile's own real data: whichever side
        # of the boundary this particular tile's own coverage falls on -- checking for real
        # (non-nodata) data rather than assuming a row-index direction keeps this correct regardless.
        for row in (int(math.floor(boundary_row)), int(math.floor(boundary_row)) + 1):
            if 0 <= row < height and (reflectance[row] > _REAL_REFLECTANCE_MIN).any():
                reflectance[row] = src_nodata
                return


def mask_and_correct_polar_edge(
    reflectance: np.ndarray, transform, crs, moon_radius_m: float, src_nodata: float
) -> None:
    """Mask (in place) the polar tile's own real coverage from whichever ±60 deg boundary it's
    actually near (checks both south and north -- a real polar tile only ever has coverage near its
    own pole's boundary, so the other check's `correction_zone`/`reference_zone` always end up empty
    and it returns immediately) through that boundary's own `polar_edge_mask_max_px` native pixels
    poleward of it (sub-pixel raster-alignment slop equatorward of the boundary, plus the first,
    worst-corrected native pixels poleward of it -- see `_SOUTH`/`_NORTH`'s own comments), then
    subtracts that boundary's own fitted edge-brightening/undershoot model from the next
    `polar_edge_correction_max_px` native pixels past the mask, leveled to match this window's own real
    local baseline (the mean of the next `polar_edge_reference_max_px` pixels past the correction zone,
    already clean) rather than trusting the fitted model's own absolute baseline parameter for this
    specific window. A no-op whenever this window doesn't reach anywhere near either boundary.

    :param reflectance: This tile's own native-resolution windowed read, modified in place.
    :param transform: `reflectance`'s own affine transform (in `crs`).
    :param crs: This tile's own real source CRS (not a longitude-shifted reinterpretation of it).
    :param moon_radius_m: Sphere radius, meters.
    :param src_nodata: This tile's own nodata value -- the mask is set to this (not `NaN`
        unconditionally), so the `rasterio.warp.reproject` call after this still recognizes it as
        nodata via its own `src_nodata` parameter. Pixels already nodata going in (either convention)
        are left untouched by the correction, not corrupted by arithmetic against them.
    """
    height, width = reflectance.shape
    # This CRS's own projected origin is the pole (false easting/northing both 0) -- inverting the
    # transform there gives the pole's exact location in this window's own pixel grid, matching
    # notebooks/wac_emp_seam_edge_model.py's own technique. Shared across both boundary checks below:
    # the pole location doesn't depend on which hemisphere's own boundary latitude we're testing.
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
        distance_px = boundary_radius_px - radius_px  # positive = poleward, matches the notebook's sign

        correction_zone = (distance_px >= params.polar_edge_mask_max_px) & (
            distance_px <= params.polar_edge_correction_max_px
        )
        reference_zone = (distance_px > params.polar_edge_correction_max_px) & (
            distance_px <= params.polar_edge_reference_max_px
        )
        if not correction_zone.any() and not (distance_px < params.polar_edge_mask_max_px).any():
            continue  # this window is nowhere near this particular boundary -- try the other one

        reflectance[distance_px < params.polar_edge_mask_max_px] = src_nodata
        if not correction_zone.any():
            return
        zone_values = reflectance[correction_zone]
        zone_valid = zone_values > _REAL_REFLECTANCE_MIN
        if not zone_valid.any():
            return
        zone_distance = distance_px[correction_zone]
        model = (
            params.polar_edge_amplitude
            * np.exp(-zone_distance[zone_valid] / params.polar_edge_tau_px)
            * np.cos(params.polar_edge_omega * zone_distance[zone_valid])
        )
        corrected = zone_values.copy()
        corrected[zone_valid] = zone_values[zone_valid] - model

        reference_values = reflectance[reference_zone]
        reference_valid = reference_values > _REAL_REFLECTANCE_MIN
        if reference_valid.any():
            level_offset = reference_values[reference_valid].mean() - corrected[zone_valid].mean()
            corrected[zone_valid] += level_offset

        reflectance[correction_zone] = corrected
        return


# Masking the equirect tile's own bad edge row (`mask_equirect_edge_row`) and the polar tile's own edge
# (`mask_and_correct_polar_edge`) both remove real pixels that used to carry (bad) data -- at any
# destination pixel where that was the *only* tile with real coverage there (a real effect, since the
# two tiles' coverage boundary is a jagged, locally-diagonal line in the destination grid, not a clean
# cut: `notebooks/wac_emp_seam_dem_mosaic.py`), `ortho_wac_emp`'s merge has no real value for it at
# all. `notebooks/wac_emp_seam_correction_validation.py` measured this gap directly (ASP `dem_mosaic
# --count`), for the south case: small (well under 0.15% of pixels) and, following the seam itself, can
# run wide (up to ~180px along the row) but stays thin *across* it (no more than ~6px tall in either
# entry tested) -- the dimension that matters for a nearest-neighbor fill, since distance to the
# nearest real pixel is measured in any direction, not along the seam. `GAP_FILL_MAX_RADIUS_PX` is
# sized generously above that observed cross-seam thickness -- a genuine no-coverage region (e.g. a
# footprint extending past both tiles' real extent) is thick in every direction and is correctly left
# as `NaN` rather than papered over. Not independently re-measured for the north boundary.
GAP_FILL_MAX_RADIUS_PX = 6.0


def fill_nearby_gaps(array: np.ndarray, max_radius_px: float) -> np.ndarray:
    """Fill each `NaN` pixel from its own nearest real neighbor, but only within `max_radius_px` --
    bounded so this only closes small, artifact-scale gaps (see `GAP_FILL_MAX_RADIUS_PX`'s own
    comment), never a genuinely large area with no real coverage at all.

    :param array: The merged local-grid array, `NaN` wherever no source tile covers a pixel.
    :param max_radius_px: Pixels farther than this from the nearest real pixel are left `NaN`.
    :returns: A new array (`array` itself is not modified) with only near-boundary gaps filled.
    """
    invalid = np.isnan(array)
    if not invalid.any():
        return array
    distance, nearest_index = ndimage.distance_transform_edt(invalid, return_indices=True)
    fillable = invalid & (distance <= max_radius_px)
    filled = array.copy()
    filled[fillable] = array[tuple(index[fillable] for index in nearest_index)]
    return filled
