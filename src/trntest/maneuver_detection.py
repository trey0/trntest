"""Detects likely propulsive discontinuities (stationkeeping burns, reaction-wheel momentum
unloads, eclipse-phasing maneuvers -- anything a mission's flight-dynamics team's "small forces
file" would log, which this project has no access to for LRO, and no public source is known to
exist) directly from LRO's public reconstructed-orbit SPK product, by looking for step changes in
two conserved quantities of two-body motion: specific angular momentum (h) and specific orbital
energy (eps).

`sample_orbit_state`/`find_maneuver_candidates` need live SPICE kernels and network access (via
`spice_kernels`) -- run only inside Docker, and marked `@pytest.mark.heavy` in
`tests/test_maneuver_detection.py` (see README's Tests section for the heavy/fast split).
`detect_discontinuities` itself is pure/deterministic (plain numpy over an already-sampled state
series), so it's covered by ordinary fast unit tests against a synthetic two-body-propagated orbit.
"""
# Why h and eps, not the classical elements (a, e, i, ...): an impulsive burn's effect on the state
# vector is exact and simple --
#
#   h_after = r x (v + dv) = h_before + r x dv        (exact, any dv magnitude, r unchanged mid-burn)
#   eps_after = eps_before + v.dv + |dv|^2/2           (eps = v^2/2 - GM/r; the |dv|^2/2 term is
#                                                        3+ orders of magnitude smaller than v.dv for
#                                                        the cm/s-to-few-m/s burns this module cares
#                                                        about, so eps_after - eps_before ~= v.dv)
#
# r x dv is a linear, always-exact map from dv to Delta-h with a clean, phase-independent null along
# the radial direction only (r x (anything parallel to r) = 0, always -- not "at some orbital
# phases", always) -- so Delta-h alone fully captures the tangential and normal components of any
# impulse. v.dv picks up the remaining radial (+ some tangential) sensitivity, weak only very close
# to periapsis/apoapsis passage (where the radial velocity itself is briefly ~0), a narrow window,
# not a wide phase range.
#
# This matters because a single classical element doesn't have this property: semi-major axis /
# energy alone is exactly blind to a purely-normal impulse (a normal dv does zero work, full stop --
# and per Mesarch et al., AAS-23-234, LRO's momentum unloads were performed "in the +/- orbit normal
# direction to minimize the along-track perturbative effects" early in the mission, deliberately
# designed to be invisible to exactly this kind of check). Inclination alone would fix that but
# reintroduces a different phase-dependent blind spot of its own (di/dt in the Gauss variational
# equations is scaled by cos(argument of latitude) -- exactly zero at node crossings, which a
# momentum unload has no reason to avoid). Tracking the full h vector instead of a scalar derived
# from it (like inclination) avoids this: Delta h = r x dv has no such modulation.
#
# Together, (h_x, h_y, h_z, eps) span the full 3-DOF impulse direction space, with one honest
# residual gap: eps's radial sensitivity is v_R (the pre-burn radial velocity), which for an orbit
# as close to circular as LRO's current one (e ~ 0.02) stays small (a few % of orbital speed) across
# the entire orbit, not just right at periapsis/apoapsis passage as a first guess might suggest --
# confirmed directly (see tests/test_maneuver_detection.py's radial-impulse test) by checking the
# actual singular values of the (h, eps) measurement matrix even at the point of maximum radial
# velocity. Detection is unaffected (the combined statistic doesn't need this direction to be
# well-conditioned to cross threshold), but this module doesn't claim an accurate radial-component
# magnitude/direction for a candidate -- see _reconstruct_candidate's own comment for why its rcond
# cutoff deliberately reports ~0 there instead. This is a narrower gap than the
# single-scalar/single-classical-element alternatives it replaces -- but is still a gap, not a
# solved problem, for a purely-radial impulse specifically.
#
# Validated against Mesarch et al.'s AAS-23-234 ("Long-Term Orbit Operations for the Lunar
# Reconnaissance Orbiter") published momentum-unload cadence/magnitude for the second half of 2019,
# and against much larger (>1 m/s) stationkeeping-scale events in 2010, before LRO's frozen-orbit
# transition.

import dataclasses
from datetime import datetime

import numpy as np
import spiceypy as spice

from trntest import spice_kernels
from trntest.config import MOON_GM_KM3_S2, TrntestConfig, load_config

# ~117 min, LRO's rough low-lunar-orbit period across its post-2016 "drift" era (a ~1830 km) --
# only sizes the before/after comparison window, not used for any precision astrodynamics, so
# doesn't need to track the mission's actual slow altitude drift year to year.
ORBITAL_PERIOD_S = 7030.0

DEFAULT_SAMPLE_DT_S = 120.0
DEFAULT_SIGMA_THRESHOLD = 6.0


@dataclasses.dataclass(frozen=True)
class ManeuverCandidate:
    """One detected step in (h, eps) -- a likely maneuver, with its impulse reconstructed (weighted
    least squares, see `_reconstruct_candidate`) and decomposed into radial/tangential/normal
    components. `estimated_dv_m_s` is the reconstructed vector's magnitude; a stationkeeping burn is
    ~5.5 m/s, a momentum unload is 0.05-0.3 m/s (Mesarch et al., AAS-23-234)."""

    et: float  # SPICE ephemeris time (TDB seconds past J2000) of the detected step's peak sample
    estimated_dv_m_s: float
    dv_radial_m_s: float
    dv_tangential_m_s: float
    dv_normal_m_s: float
    combined_z: float  # peak combined (quadrature-summed) detection statistic, for diagnostics/tuning


def candidate_utc(candidate: ManeuverCandidate) -> str:
    """Human-readable UTC timestamp for a candidate.

    :param candidate: A candidate, e.g. from `find_maneuver_candidates`.
    :returns: The UTC timestamp.
    """
    # Needs an LSK already furnished (true for any candidate produced via
    # `find_maneuver_candidates`) -- kept separate from the dataclass itself rather than
    # precomputed, to keep `detect_discontinuities` SPICE-free.
    return spice.et2utc(candidate.et, "ISOC", 0)


def sample_orbit_state(
    start_dt: datetime,
    end_dt: datetime,
    config: TrntestConfig | None = None,
    dt_s: float = DEFAULT_SAMPLE_DT_S,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Furnishes LRO's reconstructed-orbit SPK across `[start_dt, end_dt]` (`spice_kernels.
    fetch_and_furnish` for the small always-needed kernels, `furnish_spk_range` for SPK coverage).
    Needs live SPICE kernels/network access -- not for use from fast/pure tests.

    :returns: `(ets, r_km, v_km_s)` -- uniform `dt_s`-cadence sample epochs and each one's
        position/velocity (MOON_ME frame, `(n, 3)` arrays).
    """
    config = config or load_config()
    spice_kernels.fetch_and_furnish(start_dt, config)
    spice_kernels.furnish_spk_range(start_dt, end_dt, config)

    et0 = spice.utc2et(start_dt.strftime("%Y-%m-%dT%H:%M:%S"))
    et1 = spice.utc2et(end_dt.strftime("%Y-%m-%dT%H:%M:%S"))
    ets = np.arange(et0, et1, dt_s)

    r = np.empty((len(ets), 3))
    v = np.empty((len(ets), 3))
    for i, et in enumerate(ets):
        state, _ = spice.spkezr("LRO", et, "MOON_ME", "NONE", "MOON")
        r[i] = state[:3]
        v[i] = state[3:]
    return ets, r, v


def _skew(vec: np.ndarray) -> np.ndarray:
    """3x3 skew-symmetric matrix S such that S @ x == cross(vec, x) for any x."""
    x, y, z = vec
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])


def _reconstruct_candidate(
    et: float,
    r_peak: np.ndarray,
    v_peak: np.ndarray,
    dh_km2_s: np.ndarray,
    deps_km2_s2: float,
    sigmas: np.ndarray,
    combined_z: float,
) -> ManeuverCandidate:
    """Inverts the observed (Delta-h, Delta-eps) at one candidate peak for the 3D impulse that
    produced it -- weighted least squares (weights = 1/each channel's own robust noise floor, the
    same `sigmas` used for detection), then decomposes into radial/tangential/normal (RTN)
    components using the orbital state at the peak sample. Replaces a cruder "assume the impulse was
    tangential" magnitude estimate, which would be systematically wrong (too small) for a normal- or
    radial-dominant event.
    """
    m = np.vstack([_skew(r_peak), v_peak])  # (4, 3): rows = [r x] (Delta-h) then v. (Delta-eps)
    b = np.concatenate([dh_km2_s, [deps_km2_s2]])  # (4,)
    scale = 1.0 / sigmas  # standard GLS: scale each row by 1/sigma before an ordinary least-squares solve
    # rcond=None (numpy's machine-epsilon-scaled default) isn't aggressive enough here: right near
    # apsis passage, v_R -> 0 and both equations lose sensitivity to a purely-radial component at
    # once ([r x] is exactly, always blind to it; eps's v.dv term needs a nonzero v_R) -- the
    # weighted system's smallest singular value can be ~1e-6x the largest without ever being
    # "machine-precision zero," so an unregularized solve amplifies whatever noise happens to be in
    # that direction into a wildly implausible reconstructed component (confirmed on a 2019-07
    # candidate: an unregularized solve reported +373 m/s radial for what every other channel says is
    # an ordinary ~cm/s-scale momentum unload). Explicit rcond=1e-2 treats any direction that weak as
    # genuinely unconstrained (contributes ~0) rather than over-confidently inverting it -- narrow
    # apsis-adjacent radial sensitivity is a physical limit (see module docstring's trailing
    # comment), not a bug to paper over with a bigger number.
    dv_est_km_s, *_ = np.linalg.lstsq(m * scale[:, None], b * scale, rcond=1e-2)

    r_hat = r_peak / np.linalg.norm(r_peak)
    n_hat = np.cross(r_peak, v_peak)
    n_hat = n_hat / np.linalg.norm(n_hat)
    t_hat = np.cross(n_hat, r_hat)

    return ManeuverCandidate(
        et=float(et),
        estimated_dv_m_s=float(np.linalg.norm(dv_est_km_s) * 1000),
        dv_radial_m_s=float(np.dot(dv_est_km_s, r_hat) * 1000),
        dv_tangential_m_s=float(np.dot(dv_est_km_s, t_hat) * 1000),
        dv_normal_m_s=float(np.dot(dv_est_km_s, n_hat) * 1000),
        combined_z=float(combined_z),
    )


def detect_discontinuities(
    ets: np.ndarray,
    r: np.ndarray,
    v: np.ndarray,
    dt_s: float,
    gm_km3_s2: float,
    orbital_period_s: float = ORBITAL_PERIOD_S,
    sigma_threshold: float = DEFAULT_SIGMA_THRESHOLD,
) -> list[ManeuverCandidate]:
    """Pure, SPICE-free step-change detector over an already-sampled `(ets, r, v)` series from
    `sample_orbit_state`. Compares the median just-after vs. just-before a one-orbital-period window
    for each of `(h, eps)`'s four channels; a burn shows up as a persistent shift (post-burn
    dynamics continue from the new state), cancelling most of the periodic two-body oscillation.
    Each channel's diff series is normalized by its own robust (MAD-based) noise floor and combined
    via quadrature (`sqrt(sum of squared per-channel z-scores)`), self-calibrating to each channel's
    own noise level rather than needing hand-derived, phase-dependent analytic sensitivity weights.

    Consecutive over-threshold samples (a step stays flagged for close to a full window's width) are
    deduplicated to their single peak-magnitude sample.
    """
    h = np.cross(r, v)  # (n, 3) km^2/s
    r_norm = np.linalg.norm(r, axis=1)
    eps = np.sum(v * v, axis=1) / 2 - gm_km3_s2 / r_norm  # (n,) km^2/s^2
    channels = np.concatenate([h, eps[:, None]], axis=1)  # (n, 4): h_x, h_y, h_z, eps

    win = max(1, round(orbital_period_s / dt_s))
    n = len(ets)
    if n < 2 * win + 1:
        return []

    diffs = np.full((n, 4), np.nan)
    for i in range(win, n - win):
        diffs[i] = np.median(channels[i : i + win], axis=0) - np.median(channels[i - win : i], axis=0)

    valid = ~np.isnan(diffs[:, 0])
    sigmas = 1.4826 * np.median(np.abs(diffs[valid] - np.median(diffs[valid], axis=0)), axis=0)
    if np.any(sigmas <= 0):
        return []

    z = diffs[valid] / sigmas
    combined = np.zeros(n)
    combined[valid] = np.sqrt(np.sum(z**2, axis=1))

    candidates = []
    i = win
    while i < n - win:
        if combined[i] > sigma_threshold:
            j_end = i
            while j_end < n - win and combined[j_end] > sigma_threshold:
                j_end += 1
            peak_i = i + int(np.argmax(combined[i:j_end]))
            candidates.append(
                _reconstruct_candidate(
                    ets[peak_i], r[peak_i], v[peak_i], diffs[peak_i, :3], diffs[peak_i, 3], sigmas, combined[peak_i]
                )
            )
            # Skip a full window past the peak, not just past this contiguous over-threshold run --
            # a step's before/after window comparison can dip briefly back under threshold and pop
            # back up again while still within one window of the true step (edge-transition ripple
            # as the sliding windows straddle it in different ways), which would otherwise register
            # as spurious extra candidates a few percent of the event's magnitude. Distinct
            # maneuvers are always far more than one window apart (momentum unloads are weeks apart;
            # the window is ~one orbital period), so this can't merge two genuine events.
            i = peak_i + win
        else:
            i += 1
    return candidates


def find_maneuver_candidates(
    start_dt: datetime,
    end_dt: datetime,
    config: TrntestConfig | None = None,
    dt_s: float = DEFAULT_SAMPLE_DT_S,
    orbital_period_s: float = ORBITAL_PERIOD_S,
    sigma_threshold: float = DEFAULT_SIGMA_THRESHOLD,
) -> list[ManeuverCandidate]:
    """Convenience wrapper: sample + detect in one call. Needs live SPICE kernels/network access,
    same as `sample_orbit_state`."""
    config = config or load_config()
    ets, r, v = sample_orbit_state(start_dt, end_dt, config, dt_s)
    return detect_discontinuities(ets, r, v, dt_s, MOON_GM_KM3_S2, orbital_period_s, sigma_threshold)
