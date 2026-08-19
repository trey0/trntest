"""Catalog-driven WAC dataset selection and generation: `images_for_window()` queries the real LROC
catalog for real EDR candidates in a given time window and returns a throttled, illumination-
filtered image list for it; `generate_dataset()` takes that list and generates real synthetic
images for it, reusing the existing single-image pipeline (`camera.build_camera`,
`lunaserv.fetch_dem_and_ortho`, `render.run_sat_sim`), just parameterized per image.
`generate_dataset()` also computes each image's real WAC crop footprint
(`tie_points.crop_footprint_corners_for_camera`) and passes it to `fetch_dem_and_ortho` so the
DEM/ortho AOI is always sized to cover both the synthetic camera's own footprint and the real WAC
crop's -- not just a notebook-local concern, since any caller of `generate_dataset()` may want to
display the real WAC crop later (see `GenerationResult.crop_footprint`).

The window itself is picked elsewhere: `dataset_selection.py`'s orbit-level pipeline
(`find_orbits` -> ... -> `select_diverse_datasets`) picks it, and `dataset_selection.
resolve_orbit_sequence` is the usual caller of `images_for_window()` here. An earlier
`select_dataset()` (its own from-scratch geometry search over a fresh date range, not a
pre-selected window) filled this role before `dataset_selection.py` existed; removed once nothing
called it anymore (see `docs/history.md`'s dated entry) -- `images_for_window()`'s window-scoped
catalog query/evaluate/finalize tail is what it factored out of that removed function.
"""

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import spiceypy as spice

from trntest import cache, camera, catalog, illumination, lunaserv, render, tie_points
from trntest.camera import Camera, FrameTiming
from trntest.config import TrntestConfig, load_config
from trntest.lunaserv import DemOrthoResult
from trntest.render import RenderResult

DATASET_COLUMNS = [
    "product_id",
    "edr_volume",
    "edr_subdir",
    "edr_doy",
    "edr_product",
    "cdr_volume",
    "cdr_subdir",
    "cdr_doy",
    "cdr_product",
    "orbit_number",
    "start_time",
    "stop_time",
    "start_frame",
    "center_frame_index",
    "n_frames_for_square_crop",
    "sun_elevation_deg",
    "incidence_angle_deg",
    "center_lat_deg",
    "center_lon_deg",
]


@dataclasses.dataclass(frozen=True)
class GenerationResult:
    selected_image: pd.Series  # the manifest row this was generated from
    config: TrntestConfig  # the per-image config actually used -- feed this to Session/the
    # tie-points/orientation calls downstream, e.g. in the notebook
    frame_timing: FrameTiming
    camera: Camera
    crop_footprint: dict  # the real WAC crop's own footprint (tie_points.crop_footprint_corners_for_camera)
    # -- computed once here (needed to size dem_ortho_result's own AOI fetch below) and reused by
    # Phase 6, rather than recomputed later
    dem_ortho_result: DemOrthoResult
    render_result: RenderResult


def anchor_start_frame_for_centered_crop(frame_timing: FrameTiming, config: TrntestConfig) -> tuple[float, dict]:
    """Compute the crop start frame that centers the ~70-framelet square along-track crop on the
    product's own temporal midpoint (nframes/2), rather than a hand-picked offset. ODE's (and
    presumably the PDS3 index's) per-product summary geometry fields -- Center_latitude/longitude,
    Incidence_angle -- most plausibly characterize the observation's midpoint, not its start, so
    anchoring our own square crop there keeps our precise SPICE-computed sun-elevation check aligned
    with what the catalog already reports (confirmed empirically: a real product's SPICE-computed
    midpoint boresight point landed within ~1.3 deg longitude / ~0.05 deg latitude of ODE's own
    reported Center_longitude/Center_latitude, and the SPICE-computed incidence angle there matched
    ODE's reported Incidence_angle almost exactly)."""
    midpoint = frame_timing.nframes / 2.0
    crop_info = camera.compute_n_frames_for_square_crop(frame_timing, round(midpoint), config)
    n_frames = crop_info["n_frames_for_square_crop"]
    start_frame = midpoint - n_frames / 2.0
    return start_frame, crop_info
    # Note: by construction start_frame + n_frames/2 == midpoint exactly.


def evaluate_candidate_image(edr_row: pd.Series, config: TrntestConfig, min_sun_elevation_deg: float) -> dict | None:
    """Pose the camera for `edr_row` at its midpoint-anchored crop, check sun elevation at the
    resulting image center (spherical-ish, via illumination.sun_elevation_deg), and return the new
    manifest columns if illuminated, else None. Uses the lower-level camera.py functions, not
    build_camera(), so this never writes a .tsai file or touches Lunaserv during selection.

    Forces `wac_ck_source="naif_metakernel"` on `per_row_config` -- `compute_n_frames_for_square_crop`
    (via `anchor_start_frame_for_centered_crop`) calls `spice_kernels.fetch_and_furnish` once per
    candidate row here, each with a *different* `edr_product` (this function is called once per
    catalog candidate, potentially hundreds per search window). The live default
    (`wac_ck_source="isis_resolved"`) resolves CK by running a real, uncached EDR-fetch +
    `lrowac2isis` + `spiceinit` pipeline per distinct `edr_product` -- fine for the handful of
    deliberate, final camera-pose computations elsewhere (`build_camera`, `isis_wac.run_pipeline`),
    but a real O(candidates) blowup here (confirmed live: >100 candidates each triggering their own
    ~15-30s ISIS round-trip in one `images_for_window()` sweep). Safe to force the cheap NAIF path for
    this specific bulk/exploratory use: confirmed (`docs/history.md`'s Phase 27) that both sources
    give numerically identical pointing for this product/date range -- `isis_resolved`'s only real
    advantage is matching ISIS's own resolution by construction, not accuracy, so it isn't worth
    paying for here."""
    per_row_config = dataclasses.replace(
        config,
        edr_volume=edr_row["volume"],
        edr_subdir=edr_row["subdir"],
        edr_doy=str(edr_row["doy"]),
        edr_product=edr_row["product"],
        wac_ck_source="naif_metakernel",
    )
    frame_timing = camera.fetch_frame_timing(per_row_config)
    start_frame, crop_info = anchor_start_frame_for_centered_crop(frame_timing, per_row_config)
    n_frames = crop_info["n_frames_for_square_crop"]
    center_frame_index = start_frame + n_frames / 2.0
    et = camera.frame_et(frame_timing, center_frame_index)
    c_meters, r_cam_to_me, _, _ = camera.camera_pose_moon_me(et)
    # Raw (unrotated) r_cam_to_me is fine here -- rotation_about_boresight only relabels px/py, it
    # doesn't move the boresight direction, so the ground point is identical either way.
    ground_km = camera.boresight_ground_point_km(c_meters / 1000.0, r_cam_to_me)
    sun_elevation_deg = illumination.sun_elevation_deg(ground_km, et)
    if sun_elevation_deg < min_sun_elevation_deg:
        return None
    return {
        "start_frame": start_frame,
        "center_frame_index": center_frame_index,
        "n_frames_for_square_crop": n_frames,
        "sun_elevation_deg": sun_elevation_deg,
    }


def throttle_by_time(images: pd.DataFrame, min_gap_minutes: float) -> pd.DataFrame:
    """Sort by start_time and greedily keep a row only if its start_time is >= min_gap_minutes after
    the previously KEPT row's start_time."""
    sorted_images = images.sort_values("start_time").reset_index(drop=True)
    min_gap = timedelta(minutes=min_gap_minutes)
    keep_positions = []
    last_kept_time = None
    for position, start_time in enumerate(sorted_images["start_time"]):
        if last_kept_time is None or (start_time - last_kept_time) >= min_gap:
            keep_positions.append(position)
            last_kept_time = start_time
    return sorted_images.iloc[keep_positions].reset_index(drop=True)


_ILLUMINATED_COLUMNS = [
    *catalog.CATALOG_COLUMNS,
    "start_frame",
    "center_frame_index",
    "n_frames_for_square_crop",
    "sun_elevation_deg",
]


def _evaluate_illuminated_candidates(
    edr_candidates: pd.DataFrame, config: TrntestConfig, min_sun_elevation_deg: float
) -> pd.DataFrame:
    """Run evaluate_candidate_image over every candidate, dropping unilluminated ones and any that
    fail for one of two specific, anticipated per-candidate geometry/coverage reasons -- not any
    exception whatsoever. Narrowed deliberately (docs/history.md's Phase 37 follow-up): checked
    against this project's one real large-scale run (the incident that motivated this function in
    the first place), and every one of its 1,354 skips was a fetch failure, none were a genuine
    geometry/coverage edge case -- so a bare `except Exception` was catching a far broader class of
    problems (a real bug included) than it was ever observed to actually need. The two kept here are
    concrete, not hypothetical, both reachable from `evaluate_candidate_image`'s own call graph for a
    real candidate at extreme geometry (e.g. near the limb or a pole), not a code defect:

    - `AssertionError`, from `camera.boresight_ground_point_km`'s "camera boresight does not
      intersect the Moon" check.
    - `spiceypy.utils.exceptions.SpiceyError`, from any of the real SPICE calls in this same chain
      (`camera.camera_pose_moon_me`, `illumination.sun_elevation_deg`) -- e.g. a furnished kernel
      not actually covering this one candidate's exact timestamp, even though it's within the
      broader search window.

    Anything else -- a `KeyError`/`TypeError`/`AttributeError` from a real bug, for instance -- is
    *not* caught here and aborts the sweep, same as `cache.FetchError` (also not caught here, and for
    the same underlying reason: it means a fetch failed systemically -- rate-limited, server down,
    network unreachable -- after `cache.cached_get`'s own retries, not that this one candidate is
    bad, and this loop calls `evaluate_candidate_image`, a real network fetch, up to ~1600 times for
    a full 7-day sweep, so letting a systemic failure propagate and abort the whole sweep -- rather
    than logging "skipping" and immediately firing the next of hundreds of further requests at an
    already-refusing server -- is the whole point; this exact catch-and-continue is what turned one
    real rate-limit response into ~1350 more of them in one incident).

    Every skip is collected, not just printed inline, and reported as one prominent summary block at
    the end (not scattered one-line-at-a-time through the sweep's other output) -- so a real geometry
    edge case, however rare, stays visible rather than scrolling past unnoticed."""
    rows = []
    dropped = []
    for _, edr_row in edr_candidates.iterrows():
        try:
            extra = evaluate_candidate_image(edr_row, config, min_sun_elevation_deg)
        except cache.FetchError:
            raise
        except (AssertionError, spice.utils.exceptions.SpiceyError) as exc:
            dropped.append((edr_row["product_id"], type(exc).__name__, str(exc)))
            continue
        if extra is not None:
            rows.append({**edr_row.to_dict(), **extra})
    if dropped:
        print(
            f"dataset: {len(dropped)} of {len(edr_candidates)} candidate(s) skipped "
            "(geometry/coverage edge case, not a fetch failure):"
        )
        for product_id, exc_type, message in dropped:
            print(f"  {product_id}: {exc_type}: {message}")
    return pd.DataFrame(rows, columns=_ILLUMINATED_COLUMNS)


def _attach_cdr_fields(throttled: pd.DataFrame, config: TrntestConfig) -> pd.DataFrame:
    """Look up each throttled row's matching CDR product, skipping (with a warning) any that has
    none rather than crashing the whole selection."""
    rows = []
    for _, edr_row in throttled.iterrows():
        cdr_row = catalog.find_matching_cdr(edr_row, config)
        if cdr_row is None:
            print(f"dataset: no matching CDR found for {edr_row['product_id']}, skipping")
            continue
        merged = edr_row.to_dict()
        merged["cdr_volume"] = cdr_row["volume"]
        merged["cdr_subdir"] = cdr_row["subdir"]
        merged["cdr_doy"] = cdr_row["doy"]
        merged["cdr_product"] = cdr_row["product"]
        rows.append(merged)
    return pd.DataFrame(rows)


def _finalize_images(
    illuminated: pd.DataFrame, config: TrntestConfig, throttle_minutes: float | None, attach_cdr: bool = True
) -> pd.DataFrame:
    """`images_for_window()`'s tail: optional throttling, CDR matching (`_attach_cdr_fields`), and
    rename/select into `DATASET_COLUMNS`. Split out from `images_for_window()` on its own merits
    (a plain, reusable finishing step), though it's currently only called from there.

    `attach_cdr=False` skips `_attach_cdr_fields` (one real network round-trip per candidate) and
    fills the four `cdr_*` columns with `None` instead -- confirmed (grep) that `wac.py` is the only
    real consumer of them anywhere in this codebase, and `wac.py` is itself already superseded by
    `isis_wac.py` as the live real-WAC comparison method (kept only for its own test coverage, not
    called by either notebook). `TrnTestEntry`/`TrnTestImage` never read `cdr_*` at all. Still
    included as `DATASET_COLUMNS`-shaped `None`s (not dropped from the schema) so callers that do
    want CDR fields and callers that don't (`dataset_selection.resolve_orbit_sequence`, the default
    everywhere else) share one manifest schema."""
    images = throttle_by_time(illuminated, throttle_minutes) if throttle_minutes is not None else illuminated
    if attach_cdr:
        result = _attach_cdr_fields(images, config)
    else:
        result = images.copy()
        result["cdr_volume"] = result["cdr_subdir"] = result["cdr_doy"] = result["cdr_product"] = None
    result = result.rename(
        columns={"volume": "edr_volume", "subdir": "edr_subdir", "doy": "edr_doy", "product": "edr_product"}
    )
    return result[DATASET_COLUMNS].reset_index(drop=True)


def _prefilter_by_catalog_metadata(
    edr_candidates: pd.DataFrame,
    min_sun_elevation_deg: float,
    max_emission_angle_deg: float | None,
    margin_deg: float,
) -> pd.DataFrame:
    """Cheap pre-filter using catalog metadata already in hand (no network fetch, no SPICE) --
    applied before the real per-candidate `evaluate_candidate_image` cost (one real HTTP request +
    a SPICE camera-pose computation each), which is what actually tripped a real rate limit on the
    LROC EDR host resolving one real 24-orbit window (a raw window can have several hundred candidate
    EDRs -- most of them nowhere near acceptable -- not just the handful that end up illuminated).

    Sun elevation is derived from the catalog's own `Incidence_angle` -- confirmed elsewhere in this
    codebase (`anchor_start_frame_for_centered_crop`'s docstring) to closely match the real
    SPICE-computed value at the midpoint-anchored crop center this project actually poses the camera
    at, not just a rough approximation. Still, `margin_deg` widens both cutoffs (lower for sun
    elevation, higher for emission angle) rather than using the exact same threshold
    `evaluate_candidate_image`/`add_acceptable_edr_counts` apply -- a false *negative* here silently
    drops a real candidate with no way to notice, whereas a false positive just costs one wasted (but
    still cheap, paced) real evaluation downstream.

    `max_emission_angle_deg` has no real-evaluation recheck downstream at all (unlike sun elevation,
    which `evaluate_candidate_image` re-derives precisely) -- `evaluate_candidate_image` doesn't
    compute emission angle, so if a caller passes this, the catalog value (plus margin) is the
    *only* emission-angle enforcement applied, matching how `dataset_selection.
    add_acceptable_edr_counts` already uses it directly. `None` (default) skips emission filtering
    entirely, matching this project's original, long-standing "illuminated" definition (sun
    elevation only) -- opt-in territory for callers that want it (`dataset_selection.
    resolve_orbit_sequence` does)."""
    sun_elevation_deg = 90.0 - edr_candidates["incidence_angle_deg"]
    keep = sun_elevation_deg > (min_sun_elevation_deg - margin_deg)
    if max_emission_angle_deg is not None:
        keep &= edr_candidates["emission_angle_deg"] < (max_emission_angle_deg + margin_deg)
    return edr_candidates[keep].reset_index(drop=True)


def images_for_window(
    start_dt: datetime,
    end_dt: datetime,
    config: TrntestConfig,
    min_sun_elevation_deg: float,
    throttle_minutes: float | None = None,
    attach_cdr: bool = True,
    max_emission_angle_deg: float | None = None,
    prefilter_margin_deg: float = 5.0,
) -> pd.DataFrame:
    """Resolve every acceptable WAC EDR in `[start_dt, end_dt)` into a `TrnTestDataSet`-ready images
    table (columns: `DATASET_COLUMNS`) -- the real per-candidate camera-pose/sun-elevation evaluation
    (`_evaluate_illuminated_candidates`, not just catalog metadata like
    `dataset_selection.add_acceptable_edr_counts` computes for counting purposes) plus, by default,
    CDR matching (see `_finalize_images`'s docstring for why `attach_cdr=False` is worth passing if
    the caller doesn't need it).

    Runs `_prefilter_by_catalog_metadata` first (see its own docstring) so the expensive real
    per-candidate step only ever runs on candidates already plausible by catalog metadata alone --
    a raw window's candidate count is typically dominated by clearly-unacceptable EDRs.
    `max_emission_angle_deg=None` (default) matches this project's original sun-elevation-only
    "illuminated" definition; pass it to also enforce a nadir/"typical mapping mode" cutoff, as
    `dataset_selection.resolve_orbit_sequence` does, matching what made its source window acceptable
    in the first place.

    Takes an already-chosen window, not a search range -- `dataset_selection.resolve_orbit_sequence`
    is the usual caller, given one selected orbit-sequence span from `dataset_selection.
    select_diverse_datasets`. An earlier `select_dataset()` searched for its own window from a fresh
    date range and reused this same evaluate/finalize tail internally; removed once nothing called
    it anymore (see `docs/history.md`'s dated entry)."""
    edr_candidates = catalog.list_products(config, catalog.EDR_PRODUCT_TYPE, start_dt, end_dt)
    edr_candidates = _prefilter_by_catalog_metadata(
        edr_candidates, min_sun_elevation_deg, max_emission_angle_deg, prefilter_margin_deg
    )
    illuminated_df = _evaluate_illuminated_candidates(edr_candidates, config, min_sun_elevation_deg)
    return _finalize_images(illuminated_df, config, throttle_minutes, attach_cdr)


def _per_image_config(row: pd.Series, config: TrntestConfig, output_dir: Path) -> TrntestConfig:
    """Shared per-row `dataclasses.replace(...)` construction, keyed on one manifest row's
    edr_volume/edr_subdir/edr_doy/edr_product/cdr_volume/cdr_product/start_frame -- used by
    `generate_dataset()`'s loop (`output_dir=<its own output_dir>/product_id`) and
    `trn_dataset.TrnTestEntry.per_image_config` (`output_dir=dataset_folder/"_work"/edr_product`)."""
    return dataclasses.replace(
        config,
        edr_volume=row["edr_volume"],
        edr_subdir=row["edr_subdir"],
        edr_doy=str(row["edr_doy"]),
        edr_product=row["edr_product"],
        cdr_volume=row["cdr_volume"],
        cdr_product=row["cdr_product"],
        target_frame_index=round(row["start_frame"]),
        output_dir=output_dir,
    )


def generate_dataset(
    images: pd.DataFrame,
    config: TrntestConfig | None = None,
    limit: int | None = None,
    output_dir: Path | str | None = None,
) -> list[GenerationResult]:
    """Generate real synthetic images for (up to `limit` of) the given selected images, reusing the
    existing single-image pipeline unchanged, just parameterized per image.

    `cache.FetchError` is deliberately *not* caught here, unlike the one other exception type this
    catches -- it means a fetch failed systemically (rate-limited, server down, network unreachable)
    after `cache.cached_get`'s own retries, not that this one image is bad, so it propagates and
    aborts the whole batch (docs/history.md's Phase 36 follow-up).

    The only other exception caught (and skipped) here is `ValueError` from
    `lunaserv.astropedia_coverage_bbox_deg`'s real, evidenced coverage check: a candidate whose
    padded AOI falls outside Astropedia's GLD100 flat file's +-79-ish deg latitude coverage. Checked
    deliberately narrow (docs/history.md's Phase 37 follow-up), same reasoning as
    `_evaluate_illuminated_candidates` above: this pipeline's other assert-guarded invariants
    (`lunaserv.py`'s `assert center is not None`) are defensive checks on state `build_camera` should
    already guarantee, not conditions expected to trip for a real candidate, and its ISIS `campt`
    calls (`tie_points.crop_footprint_corners_for_camera` -> `isis_wac.ground_point_at_pixel`) use
    `check=True` specifically because *no* failure is expected there (see that function's own
    docstring) -- so none of those get the same treatment; a failure from any of them is exactly as
    likely to mean a real bug as `generate_dataset` failing for any other unanticipated reason, and
    should abort the batch the same way. `generate_dataset` is, in this project's actual usage, always
    called with `limit=1` on an already-screened candidate (see `notebooks/image_generation.py`), so
    in practice this rarely matters either way -- but the same narrow-catch principle applies for
    whenever it's used with a true multi-image batch."""
    config = config or load_config()
    output_dir = Path(output_dir) if output_dir is not None else config.output_dir / "dataset"
    rows = images if limit is None else images.head(limit)

    results = []
    failures = []
    for _, row in rows.iterrows():
        product_id = row["product_id"]
        try:
            per_image_config = _per_image_config(row, config, output_dir / product_id)
            built_camera = camera.build_camera(per_image_config)
            frame_timing = camera.fetch_frame_timing(per_image_config)
            crop_footprint = tie_points.crop_footprint_corners_for_camera(frame_timing, built_camera, per_image_config)
            dem_ortho_result = lunaserv.fetch_dem_and_ortho(
                built_camera, per_image_config, extra_footprint_lonlat_deg=crop_footprint
            )
            render_result = render.run_sat_sim(built_camera, dem_ortho_result, per_image_config)
        except cache.FetchError:
            raise
        except ValueError as exc:
            failures.append((product_id, type(exc).__name__, str(exc)))
            continue
        results.append(
            GenerationResult(
                selected_image=row,
                config=per_image_config,
                frame_timing=frame_timing,
                camera=built_camera,
                crop_footprint=crop_footprint,
                dem_ortho_result=dem_ortho_result,
                render_result=render_result,
            )
        )

    print(f"generate_dataset: {len(results)} of {len(rows)} image(s) succeeded")
    if failures:
        print(f"generate_dataset: {len(failures)} of {len(rows)} image(s) skipped (DEM coverage edge case):")
        for product_id, exc_type, message in failures:
            print(f"  {product_id}: {exc_type}: {message}")
    return results


def write_manifest(images: pd.DataFrame, path: Path | str) -> None:
    images.to_csv(path, index=False)


def read_manifest(path: Path | str) -> pd.DataFrame:
    return pd.read_csv(path, parse_dates=["start_time", "stop_time"])
