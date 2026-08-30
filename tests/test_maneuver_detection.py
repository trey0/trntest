from datetime import datetime

import numpy as np
import pytest

from trntest import maneuver_detection
from trntest.config import load_config
from trntest.maneuver_detection import DEFAULT_SIGMA_THRESHOLD, detect_discontinuities, find_maneuver_candidates

GM_KM3_S2 = 4902.80007  # matches trntest.config.MOON_GM_KM3_S2
A_KM = 1830.0
ECC = 0.02  # matches LRO's rough real post-2016 drift-orbit eccentricity
DT_S = 60.0
ORBITAL_PERIOD_S = 2 * np.pi * np.sqrt(A_KM**3 / GM_KM3_S2)


def _two_body_accel(r: np.ndarray, gm: float) -> np.ndarray:
    return -gm * r / np.linalg.norm(r) ** 3


def _rk4_step(r: np.ndarray, v: np.ndarray, dt: float, gm: float) -> tuple[np.ndarray, np.ndarray]:
    def deriv(r, v):
        return v, _two_body_accel(r, gm)

    k1r, k1v = deriv(r, v)
    k2r, k2v = deriv(r + 0.5 * dt * k1r, v + 0.5 * dt * k1v)
    k3r, k3v = deriv(r + 0.5 * dt * k2r, v + 0.5 * dt * k2v)
    k4r, k4v = deriv(r + dt * k3r, v + dt * k3v)
    r_next = r + dt / 6 * (k1r + 2 * k2r + 2 * k3r + k4r)
    v_next = v + dt / 6 * (k1v + 2 * k2v + 2 * k3v + k4v)
    return r_next, v_next


def _propagate(
    n_samples: int, dt_s: float, r0, v0, gm: float, impulse_at: int | None = None, dv=None, substeps: int = 10
):
    """RK4-integrates the exact two-body problem, injecting an instantaneous impulse `dv` (km/s)
    into the velocity right before propagating sample `impulse_at` -- a genuinely physically
    self-consistent trajectory with a real embedded maneuver (post-burn (r, v) actually continues on
    the new orbit, so h/eps genuinely stay constant thereafter), unlike naively adding `dv` only to
    a velocity array while leaving position on its old, now-inconsistent path -- which would show a
    spurious *oscillating* artifact instead of `detect_discontinuities`'s expected clean, persistent
    step, and wouldn't actually test what a real maneuver looks like. A tiny (mm-scale position,
    sub-mm/s velocity) noise floor is added throughout, matching real reconstructed-SPK data (never
    *exactly* analytically periodic) -- without it the before/after median diff is identically 0.0
    off any injected step, degenerating `detect_discontinuities`'s MAD-based threshold to exactly
    0.0 too (its own `sigmas <= 0` guard) and masking real steps instead of flagging them."""
    rng = np.random.default_rng(42)
    ets = np.arange(n_samples) * dt_s
    r = np.empty((n_samples, 3))
    v = np.empty((n_samples, 3))
    r_cur, v_cur = np.array(r0, dtype=float), np.array(v0, dtype=float)
    sub_dt = dt_s / substeps
    for i in range(n_samples):
        if i == impulse_at:
            v_cur = v_cur + np.array(dv, dtype=float)
        r[i], v[i] = r_cur, v_cur
        for _ in range(substeps):
            r_cur, v_cur = _rk4_step(r_cur, v_cur, sub_dt, gm)
    r = r + rng.normal(0, 1e-6, r.shape)  # ~1 m position noise floor
    v = v + rng.normal(0, 1e-9, v.shape)  # ~1 um/s velocity noise floor
    return ets, r, v


def _periapsis_state(a_km: float, e: float, gm: float) -> tuple[np.ndarray, np.ndarray]:
    """State at periapsis, orbit in the xy-plane (so the orbit-normal direction is exactly +z)."""
    r_p = a_km * (1 - e)
    v_p = np.sqrt(gm * (1 + e) / (a_km * (1 - e)))
    return np.array([r_p, 0.0, 0.0]), np.array([0.0, v_p, 0.0])


N_SAMPLES = int(round(15 * ORBITAL_PERIOD_S / DT_S))  # ~15 orbits
MID_SAMPLE = N_SAMPLES // 2
# ~1/4 orbit past periapsis (not an exact multiple of the period) -- away from both apsides, where
# radial velocity is near its maximum, for a fair test of the eps channel's radial sensitivity.
QUARTER_PHASE_SAMPLE = int(round(2.25 * ORBITAL_PERIOD_S / DT_S))


def test_detect_discontinuities_no_false_positive_on_unperturbed_orbit():
    r0, v0 = _periapsis_state(A_KM, ECC, GM_KM3_S2)
    ets, r, v = _propagate(N_SAMPLES, DT_S, r0, v0, GM_KM3_S2)
    candidates = detect_discontinuities(ets, r, v, DT_S, GM_KM3_S2, orbital_period_s=ORBITAL_PERIOD_S)
    assert candidates == []


def test_detect_discontinuities_finds_normal_direction_impulse():
    """The scenario this module was specifically redesigned to catch: a purely-normal impulse does
    zero work (no energy/semi-major-axis change at all) and, per Mesarch et al. AAS-23-234, is
    exactly how LRO's momentum unloads were flown early in the mission -- "in the +/- orbit normal
    direction to minimize the along-track perturbative effects." An 'a'-only detector would see
    nothing here."""
    r0, v0 = _periapsis_state(A_KM, ECC, GM_KM3_S2)
    dv_n = 2e-4  # km/s == 0.2 m/s, momentum-unload scale
    ets, r, v = _propagate(N_SAMPLES, DT_S, r0, v0, GM_KM3_S2, impulse_at=MID_SAMPLE, dv=[0, 0, dv_n])

    candidates = detect_discontinuities(ets, r, v, DT_S, GM_KM3_S2, orbital_period_s=ORBITAL_PERIOD_S)

    assert len(candidates) == 1
    (candidate,) = candidates
    assert candidate.et == pytest.approx(ets[MID_SAMPLE], abs=ORBITAL_PERIOD_S)
    assert candidate.dv_normal_m_s == pytest.approx(dv_n * 1000, rel=0.2)
    assert abs(candidate.dv_radial_m_s) < 0.3 * abs(candidate.dv_normal_m_s)
    assert abs(candidate.dv_tangential_m_s) < 0.3 * abs(candidate.dv_normal_m_s)


def test_detect_discontinuities_finds_tangential_impulse():
    r0, v0 = _periapsis_state(A_KM, ECC, GM_KM3_S2)
    dv_t = 2e-4  # km/s
    # Velocity direction *at the injection point* (near apoapsis, given MID_SAMPLE -- not v0's own
    # periapsis direction, which points the opposite way along the orbit by then): get it from an
    # unperturbed reference propagation first, same approach as the radial-impulse test below.
    _, _, v_ref = _propagate(N_SAMPLES, DT_S, r0, v0, GM_KM3_S2)
    v_hat_at_injection = v_ref[MID_SAMPLE] / np.linalg.norm(v_ref[MID_SAMPLE])
    ets, r, v = _propagate(N_SAMPLES, DT_S, r0, v0, GM_KM3_S2, impulse_at=MID_SAMPLE, dv=dv_t * v_hat_at_injection)

    candidates = detect_discontinuities(ets, r, v, DT_S, GM_KM3_S2, orbital_period_s=ORBITAL_PERIOD_S)

    assert len(candidates) == 1
    (candidate,) = candidates
    assert candidate.dv_tangential_m_s == pytest.approx(dv_t * 1000, rel=0.2)
    assert abs(candidate.dv_radial_m_s) < 0.3 * abs(candidate.dv_tangential_m_s)
    assert abs(candidate.dv_normal_m_s) < 0.3 * abs(candidate.dv_tangential_m_s)


def test_detect_discontinuities_finds_radial_impulse_without_blowing_up():
    """A purely-radial impulse is a genuinely hard case for this method: `[r x]` is *exactly*
    blind to it always (not just near apsis), and eps's sensitivity (~v_R) is only ever a few
    percent of tangential sensitivity for an orbit this close to circular (e=0.02, matching LRO's
    real current eccentricity) -- even at the point of maximum radial velocity (queried directly:
    singular-value ratio ~2e-5), nowhere close to well-conditioned. So this test doesn't assert
    accurate radial-magnitude *recovery* (structurally unreliable here) -- it asserts the event is
    still correctly *detected* (the combined statistic doesn't need the ill-conditioned direction to
    cross threshold) and that the reconstruction's explicit `rcond` truncation does its job rather
    than amplifying that near-null direction into a wildly implausible value. This is a regression
    guard for exactly that failure mode: an early version of this reconstruction (no `rcond` cutoff)
    reported +373 m/s radial for a real 2019-07-02 momentum unload every other channel agreed was an
    ordinary ~cm/s-scale event -- see docs/history.md's dated entry."""
    r0, v0 = _periapsis_state(A_KM, ECC, GM_KM3_S2)
    dv_r = 2e-4  # km/s
    ets, r, v = _propagate(N_SAMPLES, DT_S, r0, v0, GM_KM3_S2)
    r_hat_at_injection = r[QUARTER_PHASE_SAMPLE] / np.linalg.norm(r[QUARTER_PHASE_SAMPLE])
    ets, r, v = _propagate(
        N_SAMPLES, DT_S, r0, v0, GM_KM3_S2, impulse_at=QUARTER_PHASE_SAMPLE, dv=dv_r * r_hat_at_injection
    )

    candidates = detect_discontinuities(ets, r, v, DT_S, GM_KM3_S2, orbital_period_s=ORBITAL_PERIOD_S)

    assert len(candidates) == 1
    (candidate,) = candidates
    assert candidate.combined_z > DEFAULT_SIGMA_THRESHOLD
    assert candidate.estimated_dv_m_s < 2.0  # nowhere near a spurious hundreds-of-m/s blowup


def test_detect_discontinuities_too_short_series_returns_empty():
    r0, v0 = _periapsis_state(A_KM, ECC, GM_KM3_S2)
    ets, r, v = _propagate(10, DT_S, r0, v0, GM_KM3_S2)
    assert detect_discontinuities(ets, r, v, DT_S, GM_KM3_S2, orbital_period_s=ORBITAL_PERIOD_S) == []


@pytest.mark.heavy
def test_h2_2019_momentum_unload_candidates_match_published_cadence_and_magnitude():
    """Ground-truth check against Mesarch et al., AAS-23-234 ("Long-Term Orbit Operations for the
    Lunar Reconnaissance Orbiter"): H2 2019 has had no stationkeeping maneuvers of any kind (LRO's
    orbit has been unmaintained/drifting since 2016) and no eclipse-phasing maneuver (that paper's
    own EPM table has a gap from 2019-06-24 to 2021-05-03) -- so real momentum unloads, "every 2-4
    weeks" per that paper, are the only maneuver type this window could contain. Also covers this
    repo's own fixture EDR (M1329714703CE, 2019-11-30)."""
    config = load_config()
    candidates = find_maneuver_candidates(datetime(2019, 7, 1), datetime(2019, 12, 31, 23, 59, 59), config)

    # ~183 days / (14-28 day cadence) -> roughly 6-13 expected; wide-ish bounds so this isn't
    # brittle to the detector's exact threshold tuning, while still ruling out "found nothing" or
    # "found something at every sample" failure modes.
    assert 6 <= len(candidates) <= 20

    for candidate in candidates:
        # This module's own reconstruction reveals these events are NOT purely tangential the way
        # the old (semi-major-axis-only) detector's estimate implied -- several are dominated by a
        # substantial normal-direction component (up to ~2.1 m/s total, vs. the ~0.07-0.25 m/s the
        # old tangential-only estimate reported for the same dates), consistent with Mesarch et al.
        # noting momentum unloads were flown "in the +/- orbit normal direction." Still unambiguously
        # NOT stationkeeping scale: real 2010 SK pairs (see the sibling test below) show combined_z
        # in the hundreds and ~5.2-5.6 m/s, vs. <=~20 and <3 m/s for every H2 2019 candidate here.
        assert 0.02 <= candidate.estimated_dv_m_s <= 3.0, (
            f"{maneuver_detection.candidate_utc(candidate)}: {candidate.estimated_dv_m_s} m/s -- "
            "outside expected momentum-unload range"
        )
        assert candidate.combined_z < 50, (
            f"{maneuver_detection.candidate_utc(candidate)}: combined_z={candidate.combined_z} -- "
            "stationkeeping-burn-scale detection statistic in a window with none expected"
        )

    ets = [c.et for c in candidates]
    assert ets == sorted(ets)


@pytest.mark.heavy
def test_2010_finds_stationkeeping_scale_events():
    """Positive control: before LRO's Dec-2011 frozen-orbit transition, stationkeeping pairs
    (~5.5 m/s each, per Mesarch Figure 11) were performed roughly every 28 days. Confirms the same
    detector correctly scales up to real orbit-shaping burns, not just small momentum unloads."""
    config = load_config()
    candidates = find_maneuver_candidates(datetime(2010, 3, 1), datetime(2010, 4, 15), config)

    # Real SK pairs land far above either threshold on their own (observed: ~5.2-5.6 m/s,
    # combined_z ~300+) -- both checked so this test can't accidentally pass on some other
    # large-but-spurious value.
    large = [c for c in candidates if c.estimated_dv_m_s > 2.0 and c.combined_z > 100]
    assert large, (
        f"expected at least one stationkeeping-scale (>2 m/s, combined_z>100) candidate in this "
        f"window, got: {[(maneuver_detection.candidate_utc(c), c.estimated_dv_m_s, c.combined_z) for c in candidates]}"
    )
