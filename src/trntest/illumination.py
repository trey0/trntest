"""Sun/illumination geometry for dataset selection -- built on real SPICE geometry functions
(`ilumin`, `subslr`) rather than hand-rolled vector math, using the real reference ellipsoid
already loaded via `pck00010.tpc` (method `"ELLIPSOID"`), not a forced sphere: the real ellipsoid
and a sphere are close enough here that forcing them to match wouldn't be worth a kernel-pool
mutation, and `"ELLIPSOID"` (a smooth reference shape, not a DSK/terrain model) keeps this
independent of local topography -- appropriate given the ~70 km square FOV this is filtering for is
far coarser than individual-crater-scale terrain relief.
"""

from collections.abc import Callable
from datetime import UTC, datetime

import numpy as np
import spiceypy as spice

from trntest import spice_kernels
from trntest.config import TrntestConfig


def sun_elevation_deg(ground_km: np.ndarray, et: float) -> float:
    """Sun elevation (degrees above the local horizon) at a ground point, via SPICE's `ilumin`."""
    _, _, _, incidence_rad, _ = spice.ilumin("ELLIPSOID", "MOON", et, "MOON_ME", "NONE", "LRO", ground_km)
    return 90.0 - np.degrees(incidence_rad)


def sub_solar_lonlat_deg(et: float) -> tuple[float, float]:
    """Sub-solar point's lon/lat (degrees) via SPICE's `subslr`."""
    spoint, _, _ = spice.subslr("NEAR POINT/ELLIPSOID", "MOON", et, "MOON_ME", "NONE", "LRO")
    _, lon, lat = spice.reclat(spoint)
    return np.degrees(lon), np.degrees(lat)


def spacecraft_lonlat_deg(et: float) -> tuple[float, float]:
    """Sub-spacecraft point's lon/lat (degrees) -- pure position-vector direction, no shape model
    needed at all: exact for "which hemisphere is LRO over" on a body-fixed frame."""
    state, _ = spice.spkezr("LRO", et, "MOON_ME", "NONE", "MOON")
    _, lon, lat = spice.reclat(np.array(state[:3]))
    return np.degrees(lon), np.degrees(lat)


def _wrap_deg(angle_deg: float) -> float:
    """Wrap an angle in degrees to (-180, 180]."""
    return ((angle_deg + 180.0) % 360.0) - 180.0


def terminator_offset_deg(longitude_deg: float, sub_solar_longitude_deg: float) -> float:
    """Angular distance (degrees) from `longitude_deg` to the NEARER of the two terminator
    meridians (`sub_solar_longitude_deg` +/- 90) -- this is the "d" in the ">=30 deg off the
    terminator" criterion. Large d means `longitude_deg` is well inside either the lit hemisphere
    (d measured near the sub-solar meridian itself) or well inside the dark hemisphere (meaning the
    antipodal meridian, 180 deg away, is well lit instead) -- matching "either pass will have
    favorable illumination" for an ascending/descending node pair.
    """
    offset_from_subsolar = _wrap_deg(longitude_deg - sub_solar_longitude_deg)
    return abs(abs(offset_from_subsolar) - 90.0)


def find_sign_change_crossings(
    f: Callable[[float], float], t0: float, t1: float, coarse_step_s: float, tol_s: float = 1.0
) -> list[float]:
    """Coarse-sample `f` over [t0, t1] at `coarse_step_s`, then bisect each sign-change bracket to
    `tol_s` precision. Generic (not SPICE-specific) -- mirrors the bisection style already used in
    `tie_points.project_ground_to_crop_pixel`. Returns crossing times in ascending order, regardless
    of crossing direction."""
    n_steps = max(1, int((t1 - t0) / coarse_step_s))
    times = [t0 + i * coarse_step_s for i in range(n_steps + 1)]
    if times[-1] < t1:
        times.append(t1)
    values = [f(t) for t in times]

    crossings = []
    if values[0] == 0.0:
        crossings.append(times[0])

    for i in range(len(times) - 1):
        t_lo, v_lo = times[i], values[i]
        t_hi, v_hi = times[i + 1], values[i + 1]
        if v_hi == 0.0:
            # Claimed by this bracket's upper endpoint -- if the next bracket starts here too, its
            # own v_lo == 0.0 check below skips it, so an exact zero on an interior sample point
            # isn't double-counted.
            crossings.append(t_hi)
            continue
        if v_lo == 0.0:
            continue
        if (v_lo < 0) == (v_hi < 0):
            continue  # no sign change in this bracket
        while (t_hi - t_lo) > tol_s:
            t_mid = (t_lo + t_hi) / 2.0
            v_mid = f(t_mid)
            if (v_mid < 0) == (v_lo < 0):
                t_lo, v_lo = t_mid, v_mid
            else:
                t_hi, v_hi = t_mid, v_mid
        crossings.append((t_lo + t_hi) / 2.0)
    return crossings


def et_to_datetime(et: float) -> datetime:
    iso = spice.et2utc(et, "ISOC", 3)
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


def utc_to_et(dt: datetime) -> float:
    """Convert an aware UTC datetime to SPICE ET (requires the LSK to already be furnished)."""
    return spice.utc2et(dt.strftime("%Y-%m-%dT%H:%M:%S.%f"))


def find_ascending_node_crossings(start_et: float, end_et: float, config: TrntestConfig) -> list[float]:
    """Ascending-node (latitude crossing from south to north) epochs in [start_et, end_et], derived
    directly from real SPICE trajectory data -- no assumed orbital period.

    Furnishes kernels just-in-time per sampled epoch (via spice_kernels.fetch_and_furnish, which
    only reloads/unloads when the epoch's date actually needs a different date-ranged CK/SPK set)
    rather than pre-furnishing the whole range up front -- a wide search range can span more kernel
    date-ranges than the SPICE kernel pool can hold loaded simultaneously (its character-value
    buffer is fixed-size), so keeping only the currently-relevant chunk loaded is required, not just
    an optimization. Consecutive samples are almost always within the same chunk, so this is a cheap
    no-op most of the time."""

    def latitude_at(et: float) -> float:
        spice_kernels.fetch_and_furnish(et_to_datetime(et), config)
        return spacecraft_lonlat_deg(et)[1]

    all_crossings = find_sign_change_crossings(latitude_at, start_et, end_et, coarse_step_s=60.0, tol_s=1.0)

    ascending = []
    eps_s = 5.0
    for et in all_crossings:
        if latitude_at(et - eps_s) < 0 < latitude_at(et + eps_s):
            ascending.append(et)
    return ascending


def node_terminator_offset_deg(et: float) -> float:
    node_lon, _ = spacecraft_lonlat_deg(et)
    sub_solar_lon, _ = sub_solar_lonlat_deg(et)
    return terminator_offset_deg(node_lon, sub_solar_lon)
