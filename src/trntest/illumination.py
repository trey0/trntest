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


def _azimuth_elevation_from_direction(direction: np.ndarray, lon_deg: float, lat_deg: float) -> tuple[float, float]:
    """Azimuth (degrees clockwise from local north) and elevation (degrees above the local horizon)
    of a unit `direction` vector (in the same frame `lon_deg`/`lat_deg` are expressed in), via an
    exact local East-North-Up frame built from the ground point's lon/lat. Pure geometry, no SPICE
    call -- split out from `sun_azimuth_elevation_deg` so it's unit-testable without furnished
    kernels, matching this module's other pure-math functions (e.g. `terminator_offset_deg`)."""
    lon_rad, lat_rad = np.radians(lon_deg), np.radians(lat_deg)
    east = np.array([-np.sin(lon_rad), np.cos(lon_rad), 0.0])
    north = np.array([-np.sin(lat_rad) * np.cos(lon_rad), -np.sin(lat_rad) * np.sin(lon_rad), np.cos(lat_rad)])
    up = np.array([np.cos(lat_rad) * np.cos(lon_rad), np.cos(lat_rad) * np.sin(lon_rad), np.sin(lat_rad)])

    e, n, u = np.dot(direction, east), np.dot(direction, north), np.dot(direction, up)
    azimuth_deg = np.degrees(np.arctan2(e, n)) % 360.0
    elevation_deg = np.degrees(np.arcsin(u))
    return azimuth_deg, elevation_deg


def sun_azimuth_elevation_deg(lon_deg: float, lat_deg: float, et: float) -> tuple[float, float]:
    """Sun's azimuth (degrees clockwise from local north) and elevation (degrees above the local
    horizon) at a MOON_ME lon/lat, for lighting a hillshade with the real sun geometry of a given
    frame/epoch (see `lunaserv.fetch_dem_and_ortho`). SPICE has no single "local azimuth" call --
    azimuth is inherently local-frame-relative -- so this uses the same real-ephemeris-vector idiom
    as `spacecraft_lonlat_deg` (`spkpos`, not a derived/approximate direction), projected (via
    `_azimuth_elevation_from_direction`) into an exact local East-North-Up frame. The Sun's ~150
    million km distance makes its direction from the Moon's center effectively identical to its
    direction from any surface point (parallax over a ~1737 km lunar radius is negligible), so no
    separate surface-point ephemeris lookup is needed. Elevation is derived from this same local
    frame (not `sun_elevation_deg`'s separate ellipsoid-normal method) so azimuth and elevation are
    always mutually consistent."""
    sun_dir, _ = spice.spkpos("SUN", et, "MOON_ME", "NONE", "MOON")
    sun_dir = np.array(sun_dir) / np.linalg.norm(sun_dir)
    return _azimuth_elevation_from_direction(sun_dir, lon_deg, lat_deg)


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


def circular_distance_deg(a_deg: float, b_deg: float) -> float:
    """Shortest angular distance (degrees, always >= 0) between two longitudes/angles, accounting
    for wraparound -- e.g. -179 and +179 are 2 degrees apart, not 358."""
    return abs(_wrap_deg(a_deg - b_deg))


def unwrap_relative_deg(reference_deg: float, angle_deg: float) -> float:
    """`angle_deg`, shifted by a multiple of 360 so it's within 180 degrees of `reference_deg` --
    unlike `circular_mean_deg`/`circular_distance_deg`, the result is NOT wrapped back into
    (-180, 180]. For drawing a 2-point line between two longitudes without it spuriously crossing
    the whole plot at a +/-180 wraparound: draw from (reference_deg, ...) to
    (unwrap_relative_deg(reference_deg, angle_deg), ...) in this unwrapped coordinate, then clip/
    split that segment wherever it crosses +/-180."""
    return reference_deg + _wrap_deg(angle_deg - reference_deg)


def circular_mean_deg(a_deg: float, b_deg: float) -> float:
    """Circular mean of two angles (degrees), wrapped to (-180, 180] -- e.g. -170 and +160 average
    to +175, not the -5 a plain arithmetic mean would give. Picks the wraparound branch where the
    two inputs are within 180 degrees of each other (`_wrap_deg(b_deg - a_deg)`) before averaging,
    rather than averaging their raw values directly."""
    return _wrap_deg(a_deg + _wrap_deg(b_deg - a_deg) / 2.0)


def hour_angle_deg(longitude_deg: float, sub_solar_longitude_deg: float) -> float:
    """Solar hour angle (degrees) at a MOON_ME `longitude_deg`, wrapped to (-180, 180]: 0 = local
    solar noon (longitude_deg == sub_solar_longitude_deg), negative = morning (sun still to the
    east, hasn't reached this meridian yet), positive = afternoon (sun already swept past, to the
    west) -- +/-90 is the "sunrise"/"sunset" convention this project uses (exact only at the
    sub-solar latitude on an equinox; a convenient approximation elsewhere, same spirit as
    `terminator_offset_deg` below). Sign confirmed against real SPICE data, not just derived: a
    point just past the visible morning terminator reports an hour angle just past -90."""
    return _wrap_deg(longitude_deg - sub_solar_longitude_deg)


def terminator_offset_deg(longitude_deg: float, sub_solar_longitude_deg: float) -> float:
    """Angular distance (degrees) from `longitude_deg` to the NEARER of the two terminator
    meridians (`sub_solar_longitude_deg` +/- 90) -- this is the "d" in the ">=30 deg off the
    terminator" criterion. Large d means `longitude_deg` is well inside either the lit hemisphere
    (d measured near the sub-solar meridian itself) or well inside the dark hemisphere (meaning the
    antipodal meridian, 180 deg away, is well lit instead) -- matching "either pass will have
    favorable illumination" for an ascending/descending node pair.
    """
    return abs(abs(hour_angle_deg(longitude_deg, sub_solar_longitude_deg)) - 90.0)


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


def find_node_crossings(start_et: float, end_et: float, config: TrntestConfig) -> list[tuple[float, bool]]:
    """All latitude=0 (node) crossing epochs in [start_et, end_et], as (et, is_ascending) pairs
    sorted by et -- found via SPICE's gfposc geometry-finder (LRO's MOON_ME-frame latitude crossing
    zero), SPICE's own compiled adaptive root-finder over the whole window in one call, rather than
    a hand-rolled sample-and-bisect loop making thousands of Python<->SPICE round trips. Cross-
    checked against a prior hand-rolled implementation: identical crossing counts, epochs agreeing
    to within ~0.5s (well under that implementation's own 1s bisection tolerance).

    Needs SPK coverage for the whole window furnished at once (gfposc searches the whole confinement
    window in a single call) -- spice_kernels.furnish_spk_range does this; see its docstring for why
    this differs from fetch_and_furnish's per-epoch just-in-time pattern used elsewhere. LSK/PCK/frame
    kernels (spice_kernels.ALWAYS_KERNELS) are assumed already furnished by the caller, same
    convention `utc_to_et` documents -- deliberately does NOT call `fetch_and_furnish` per crossing:
    the classification below (`spacecraft_lonlat_deg`) is pure position (`spkezr`), no pointing/CK
    needed at all, so doing that per-crossing would (and, before this was found, actually did) both
    cost real overhead for nothing (profiled: ~70% of this function's own runtime) AND risk a real
    crash across a wide multi-month sweep -- `fetch_and_furnish`'s default `wac_ck_source=
    "isis_resolved"` CK selection is cached per a single, fixed `config.edr_product`, and for a date
    far from that product's own narrow window its filename-encoded date range can nominally overlap
    the query epoch while the file's *actual* `ckcov` coverage doesn't, tripping `fetch_and_furnish`'s
    own trust-but-verify check. `dataset.py`'s per-candidate sweep already avoids this the other way
    (forcing `wac_ck_source="naif_metakernel"`) precisely because `isis_resolved` isn't meant for
    sweeping many distinct dates -- not needing CK at all here sidesteps the question entirely.
    Exposes both node types (not just ascending) so callers can pair each orbit's two nodes together
    -- e.g. notebooks/select_datasets.py's per-orbit "illuminated node" statistics, which need the
    descending node too to pick whichever of the pair has the higher sun elevation."""
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
        return spacecraft_lonlat_deg(et)[1]

    eps_s = 5.0
    return [(et, latitude_at(et - eps_s) < 0 < latitude_at(et + eps_s)) for et in all_crossings]
