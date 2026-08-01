"""Sun/illumination geometry for dataset selection -- built on real SPICE geometry functions
(`ilumin`, `subslr`) rather than hand-rolled vector math, using the real reference ellipsoid
already loaded via `pck00010.tpc` (method `"ELLIPSOID"`), not a forced sphere: the real ellipsoid
and a sphere are close enough here that forcing them to match wouldn't be worth a kernel-pool
mutation, and `"ELLIPSOID"` (a smooth reference shape, not a DSK/terrain model) keeps this
independent of local topography -- appropriate given the ~70 km square FOV this is filtering for is
far coarser than individual-crater-scale terrain relief.
"""

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


def et_to_datetime(et: float) -> datetime:
    iso = spice.et2utc(et, "ISOC", 3)
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


def utc_to_et(dt: datetime) -> float:
    """Convert an aware UTC datetime to SPICE ET (requires the LSK to already be furnished)."""
    return spice.utc2et(dt.strftime("%Y-%m-%dT%H:%M:%S.%f"))


_NODE_SEARCH_STEP_S = 60.0  # gfposc's step must stay under half the minimum gap between successive
# latitude=0 crossings (~half an LRO orbit, ~56 min) or it can miss one -- same correctness
# requirement as the coarse-sample step this replaced, now enforced inside SPICE's own C search.
_LRO_ORBITAL_PERIOD_S = 113.0 * 60.0  # approximate -- used only to size gfposc's result-window
# workspace with margin, not for any precision-sensitive computation.


def find_ascending_node_crossings(start_et: float, end_et: float, config: TrntestConfig) -> list[float]:
    """Ascending-node (latitude crossing from south to north) epochs in [start_et, end_et], found via
    SPICE's gfposc geometry-finder (LRO's MOON_ME-frame latitude crossing zero) -- SPICE's own
    compiled adaptive root-finder over the whole window in one call, rather than a hand-rolled
    sample-and-bisect loop making thousands of Python<->SPICE round trips. Cross-checked against the
    prior hand-rolled implementation: identical crossing counts, epochs agreeing to within ~0.5s
    (well under the old implementation's own 1s bisection tolerance).

    Needs SPK coverage for the whole window furnished at once (gfposc searches the whole confinement
    window in a single call) -- spice_kernels.furnish_spk_range handles that; see its docstring for
    why this differs from fetch_and_furnish's per-epoch just-in-time pattern used elsewhere."""
    spice_kernels.furnish_spk_range(et_to_datetime(start_et), et_to_datetime(end_et), config)

    cnfine = spice.cell_double(2)
    spice.wninsd(start_et, end_et, cnfine)

    # ~2 latitude=0 crossings per orbit (ascending + descending), with a 2x safety margin.
    max_crossings = max(200, int((end_et - start_et) / _LRO_ORBITAL_PERIOD_S * 4) + 20)
    result = spice.gfposc(
        "LRO",
        "MOON_ME",
        "NONE",
        "MOON",
        "LATITUDINAL",
        "LATITUDE",
        "=",
        0.0,
        0.0,
        _NODE_SEARCH_STEP_S,
        max_crossings,
        cnfine,
        spice.cell_double(2 * max_crossings),
    )
    all_crossings = [spice.wnfetd(result, i)[0] for i in range(spice.wncard(result))]

    def latitude_at(et: float) -> float:
        spice_kernels.fetch_and_furnish(et_to_datetime(et), config)
        return spacecraft_lonlat_deg(et)[1]

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
