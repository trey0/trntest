"""Orbit-level TRN-OD dataset selection (`notebooks/select_datasets.py`) -- distinct from
`dataset.py`'s catalog-driven *single-image* evaluation (`images_for_window()`/`generate_dataset()`,
the live demo pipeline's own EDR picker), which `resolve_orbit_sequence` below hands an
already-selected orbit-sequence window to resolve, not the other way around. This module answers a
different question: which
*multi-day spans of consecutive orbits* make good maneuver-free TRN orbit-determination test data,
picked to be jointly diverse in solar hour angle -- not which one EDR to render.

Pipeline (each function one notebook cell): `find_orbits` -> `add_maneuver_flags` ->
`add_acceptable_edr_counts` -> `enumerate_candidate_datasets` -> `select_diverse_datasets` ->
`resolve_orbit_sequence`. See `docs/plan.md`'s architecture table and the notebook's own markdown
cells for the per-step rationale (the "illuminated node" concept, the circular-mean "center"
statistics, the greedy farthest-point diversity criterion).
"""

from datetime import datetime

import numpy as np
import pandas as pd
import spiceypy as spice

from trntest import catalog, dataset, illumination, maneuver_detection, spice_kernels, tie_points
from trntest.config import TrntestConfig


def find_orbits(period_start: datetime, period_end: datetime, config: TrntestConfig) -> pd.DataFrame:
    """Every orbit completing strictly inside `[period_start, period_end)`, one row each, with its
    **illuminated node** (whichever of the ascending/descending node pair has the higher sun
    elevation) statistics: `illum_lon_deg`, `illum_is_ascending`, `illum_sun_elev_deg`,
    `hour_angle_deg` (see `illumination.hour_angle_deg`), plus `asc_et`/`next_asc_et`/`illum_et`/
    `illum_utc` for downstream epoch bookkeeping. Furnishes a full year of SPK/CK coverage on a cold
    cache -- the slow step (several minutes), cached afterward per `docs/caching.md`."""
    spice_kernels.fetch_and_furnish(period_start, config)  # LSK/PCK/frame kernels
    et_start = spice.utc2et(period_start.strftime("%Y-%m-%dT%H:%M:%S"))
    et_end = spice.utc2et(period_end.strftime("%Y-%m-%dT%H:%M:%S"))

    crossings = illumination.find_node_crossings(et_start, et_end, config)
    ascending_ets = [et for et, is_ascending in crossings if is_ascending]
    descending_ets = [et for et, is_ascending in crossings if not is_ascending]

    orbit_windows = []  # (asc_et, desc_et, next_asc_et)
    for asc_et, next_asc_et in zip(ascending_ets, ascending_ets[1:], strict=False):
        between = [et for et in descending_ets if asc_et < et < next_asc_et]
        if len(between) != 1:
            continue  # a gap in node-crossing detection -- skip rather than guess
        orbit_windows.append((asc_et, between[0], next_asc_et))

    orbit_rows = []
    for asc_et, desc_et, next_asc_et in orbit_windows:
        a_lon, a_lat = illumination.spacecraft_lonlat_deg(asc_et)
        d_lon, d_lat = illumination.spacecraft_lonlat_deg(desc_et)
        a_sun_elev = illumination.sun_elevation_deg(tie_points.lonlat_to_ground_km(a_lon, a_lat), asc_et)
        d_sun_elev = illumination.sun_elevation_deg(tie_points.lonlat_to_ground_km(d_lon, d_lat), desc_et)

        if a_sun_elev >= d_sun_elev:
            illum_et, illum_lon, illum_is_ascending, illum_sun_elev = asc_et, a_lon, True, a_sun_elev
        else:
            illum_et, illum_lon, illum_is_ascending, illum_sun_elev = desc_et, d_lon, False, d_sun_elev

        sub_solar_lon, _ = illumination.sub_solar_lonlat_deg(illum_et)
        orbit_rows.append(
            {
                "asc_et": asc_et,
                "next_asc_et": next_asc_et,
                "illum_et": illum_et,
                "illum_lon_deg": illum_lon,
                "illum_is_ascending": illum_is_ascending,
                "illum_sun_elev_deg": illum_sun_elev,
                "hour_angle_deg": illumination.hour_angle_deg(illum_lon, sub_solar_lon),
            }
        )

    orbits_df = pd.DataFrame(orbit_rows)
    orbits_df["illum_utc"] = [illumination.et_to_datetime(et) for et in orbits_df["illum_et"]]
    print(f"{len(orbit_windows)} complete orbits in {period_start.date()}..{period_end.date()}")
    return orbits_df


def add_maneuver_flags(
    orbits_df: pd.DataFrame, period_start: datetime, period_end: datetime, config: TrntestConfig
) -> pd.DataFrame:
    """Returns a copy of `orbits_df` with a `has_maneuver` column, via one `find_maneuver_candidates`
    call over the whole period -- not per-orbit, since the detector's own before/after noise-floor
    calibration needs enough background samples to be reliable (confirmed: the same detector run on
    a short, single-week window produces many spurious candidates that don't reproduce over the full
    period; see `docs/history.md`)."""
    maneuver_candidates = maneuver_detection.find_maneuver_candidates(period_start, period_end, config)
    maneuver_ets = np.array([c.et for c in maneuver_candidates])
    asc_ets = orbits_df["asc_et"].to_numpy()
    next_asc_ets = orbits_df["next_asc_et"].to_numpy()

    orbits_df = orbits_df.copy()
    orbits_df["has_maneuver"] = [
        bool(np.any((maneuver_ets >= a) & (maneuver_ets < b))) for a, b in zip(asc_ets, next_asc_ets, strict=True)
    ]
    print(
        f"{len(maneuver_candidates)} maneuver candidates over the whole period, "
        f"{orbits_df['has_maneuver'].sum()} orbits flagged"
    )
    return orbits_df


def add_acceptable_edr_counts(
    orbits_df: pd.DataFrame,
    period_start: datetime,
    period_end: datetime,
    config: TrntestConfig,
    min_sun_elevation_deg: float = 15.0,
    max_emission_angle_deg: float = 15.0,
) -> pd.DataFrame:
    """Returns a copy of `orbits_df` with an `acceptable_edr_count` column: real WAC EDRs in each
    orbit meeting `min_sun_elevation_deg`/`max_emission_angle_deg` ("typical nadir mapping-mode").
    One catalog query for the whole period (paginated internally by `catalog.list_products`), then a
    vectorized per-EDR filter and a `searchsorted` bucketing into orbits by epoch -- looping
    per-orbit queries against the live ODE API would be both slow and needlessly chatty."""
    edrs_df = catalog.list_products(config, catalog.EDR_PRODUCT_TYPE, period_start, period_end)
    edrs_df["sun_elevation_deg"] = 90.0 - edrs_df["incidence_angle_deg"]
    acceptable = edrs_df[
        (edrs_df["sun_elevation_deg"] > min_sun_elevation_deg)
        & (edrs_df["emission_angle_deg"] < max_emission_angle_deg)
    ]

    orbit_start_utc = np.array([illumination.et_to_datetime(et) for et in orbits_df["asc_et"]])
    orbit_idx = np.searchsorted(orbit_start_utc, acceptable["start_time"].to_numpy(), side="right") - 1
    valid = (orbit_idx >= 0) & (orbit_idx < len(orbits_df))
    edr_counts = np.bincount(orbit_idx[valid], minlength=len(orbits_df))

    orbits_df = orbits_df.copy()
    orbits_df["acceptable_edr_count"] = edr_counts
    print(
        f"{len(edrs_df)} WAC EDRs in the period, {len(acceptable)} acceptable "
        f"(sun_elev > {min_sun_elevation_deg} deg, emission < {max_emission_angle_deg} deg)"
    )
    return orbits_df


def enumerate_candidate_datasets(
    orbits_df: pd.DataFrame, dataset_length_orbits: int = 24, min_edr_count_per_orbit: int = 4
) -> pd.DataFrame:
    """Every `dataset_length_orbits`-orbit sliding window that's **acceptable**: every orbit in it
    has no maneuver and at least `min_edr_count_per_orbit` acceptable EDRs, and it contains no
    illuminated-node flip (ascending/descending) -- the no-flip rule is what makes "center"
    statistics (the circular mean of the first and last orbit's value) actually behave like an
    average of nearby values, rather than splitting across two ~180-degree-apart node longitudes.
    One row per acceptable window: `start_idx`/`end_idx` (into `orbits_df`), `start_utc`/`end_utc`,
    `center_lon_deg`/`center_hour_angle_deg` (`illumination.circular_mean_deg` of the first and last
    orbit), `min_edr_count` (the window's weakest orbit -- used both to seed `select_diverse_datasets`
    and as a robustness readout)."""
    orbit_acceptable = (
        ~orbits_df["has_maneuver"] & (orbits_df["acceptable_edr_count"] >= min_edr_count_per_orbit)
    ).to_numpy()
    print(f"{orbit_acceptable.sum()} / {len(orbits_df)} individually acceptable orbits")

    lon = orbits_df["illum_lon_deg"].to_numpy()
    hour_angle_arr = orbits_df["hour_angle_deg"].to_numpy()
    is_ascending_arr = orbits_df["illum_is_ascending"].to_numpy()
    edr_count_arr = orbits_df["acceptable_edr_count"].to_numpy()

    candidate_rows = []
    for start in range(len(orbits_df) - dataset_length_orbits + 1):
        end = start + dataset_length_orbits - 1  # inclusive
        if not orbit_acceptable[start : end + 1].all():
            continue
        if not (is_ascending_arr[start : end + 1] == is_ascending_arr[start]).all():
            continue
        candidate_rows.append(
            {
                "start_idx": start,
                "end_idx": end,
                "start_utc": orbits_df["illum_utc"].iat[start],
                "end_utc": orbits_df["illum_utc"].iat[end],
                "center_lon_deg": illumination.circular_mean_deg(lon[start], lon[end]),
                "center_hour_angle_deg": illumination.circular_mean_deg(hour_angle_arr[start], hour_angle_arr[end]),
                "min_edr_count": int(edr_count_arr[start : end + 1].min()),
            }
        )
    candidates_df = pd.DataFrame(candidate_rows)
    n_possible_windows = len(orbits_df) - dataset_length_orbits + 1
    print(
        f"{len(candidates_df)} acceptable candidate datasets out of {n_possible_windows} possible "
        f"{dataset_length_orbits}-orbit windows"
    )
    return candidates_df


def select_diverse_datasets(
    candidates: pd.DataFrame, min_center_lon_separation_deg: float = 12.0, n_datasets: int = 20
) -> pd.DataFrame:
    """Greedy farthest-point selection: repeatedly pick the acceptable, still-available candidate
    whose minimum center-hour-angle distance to every already-chosen dataset is largest, excluding
    (from future consideration) anything that overlaps orbits with or is too close in center
    longitude to the pick just made. The first pick, with nothing chosen yet to be far from, is
    seeded as the single most robust candidate (highest `min_edr_count`).

    Raises `RuntimeError` if the exclusion constraints exhaust the candidate pool before
    `n_datasets` picks are made, rather than silently returning fewer than asked for."""
    start_idx = candidates["start_idx"].to_numpy()
    end_idx = candidates["end_idx"].to_numpy()
    center_lon = candidates["center_lon_deg"].to_numpy()
    center_hour_angle = candidates["center_hour_angle_deg"].to_numpy()
    min_edr_count = candidates["min_edr_count"].to_numpy()

    def circular_distance_arr(a_deg: float, b_deg: np.ndarray) -> np.ndarray:
        return np.abs(((a_deg - b_deg + 180.0) % 360.0) - 180.0)

    def exclude(available: np.ndarray, picked_i: int) -> np.ndarray:
        overlaps = (start_idx <= end_idx[picked_i]) & (start_idx[picked_i] <= end_idx)
        too_close = circular_distance_arr(center_lon[picked_i], center_lon) < min_center_lon_separation_deg
        available &= ~(overlaps | too_close)
        return available

    def require_available(available: np.ndarray, chosen: list[int]) -> None:
        if not available.any():
            raise RuntimeError(
                f"only {len(chosen)} of the requested {n_datasets} datasets could be selected -- "
                f"ran out of candidates at least {min_center_lon_separation_deg} deg apart in "
                "center longitude and not overlapping any already-chosen dataset. Try a smaller "
                "n_datasets, a smaller min_center_lon_separation_deg, or a lower "
                "min_edr_count_per_orbit (more acceptable candidates to choose from)."
            )

    available = np.ones(len(candidates), dtype=bool)
    chosen: list[int] = []
    require_available(available, chosen)
    first = int(np.argmax(min_edr_count))
    chosen.append(first)
    available = exclude(available, first)
    min_dist_to_chosen = circular_distance_arr(center_hour_angle[first], center_hour_angle)

    while len(chosen) < n_datasets:
        require_available(available, chosen)
        scores = np.where(available, min_dist_to_chosen, -np.inf)
        best = int(np.argmax(scores))
        chosen.append(best)
        available = exclude(available, best)
        min_dist_to_chosen = np.minimum(
            min_dist_to_chosen, circular_distance_arr(center_hour_angle[best], center_hour_angle)
        )

    return candidates.iloc[chosen].reset_index(drop=True)


def resolve_orbit_sequence(
    orbit_sequence: pd.Series,
    config: TrntestConfig,
    min_sun_elevation_deg: float = 15.0,
    max_emission_angle_deg: float = 15.0,
    throttle_minutes: float | None = None,
) -> pd.DataFrame:
    """Turns one selected orbit sequence (one row of `select_diverse_datasets`' output -- needs
    `start_utc`/`end_utc`) into a real, `TrnTestDataSet`-ready images table (`dataset.
    DATASET_COLUMNS`). Deliberately takes exactly one row, not the whole table -- resolve one orbit
    sequence into a dataset at a time, the same "iterate fast on one image/one entry" discipline
    this project already follows elsewhere (`image_generation.py`'s `populate(limit=1)`), not all
    `n_datasets` selected sequences at once.

    Thin wrapper around `dataset.images_for_window` -- does NOT fetch full EDR pixel data (`.IMG`)
    for any candidate, only small per-candidate XML labels (see `dataset.evaluate_candidate_image`),
    paced at `cache.py`'s usual `_REQUEST_PACING_SECONDS`, not a bulk data transfer -- and a cheap
    catalog-metadata pre-filter (`dataset._prefilter_by_catalog_metadata`) runs first, so most of a
    raw window's candidates never reach that real per-candidate step at all (confirmed necessary
    live: a real one-window resolve attempt without this pre-filter, several hundred raw candidates,
    tripped a real rate limit on the LROC EDR host). `max_emission_angle_deg=15.0` (matching
    `add_acceptable_edr_counts`'s own default) is passed through so resolving enforces the same
    nadir/"typical mapping mode" criterion that made this orbit sequence's source window acceptable
    in the first place -- unlike `images_for_window()`'s own sun-elevation-only default.
    `attach_cdr=False`: confirmed `wac.py` is the only real consumer of the `cdr_*` columns anywhere
    in this codebase, and it's already superseded by `isis_wac.py` (see `_finalize_images`'s
    docstring) -- `TrnTestEntry`/`TrnTestImage` never read them, so skip that extra per-candidate
    network round-trip here. `throttle_minutes=None` (default) keeps every acceptable candidate --
    unlike the older, now-removed `select_dataset()`'s own 5-minute default (see `docs/history.md`'s
    dated entry); thinning here isn't obviously wanted yet (an
    orbit-sequence window was already chosen for being densely acceptable, not searched fresh), so
    leave it to the caller to opt in."""
    return dataset.images_for_window(
        orbit_sequence["start_utc"],
        orbit_sequence["end_utc"],
        config,
        min_sun_elevation_deg,
        throttle_minutes,
        attach_cdr=False,
        max_emission_angle_deg=max_emission_angle_deg,
    )
