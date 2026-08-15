"""Catalog-driven WAC dataset selection and generation: `select_dataset()` queries the real LROC
catalog for a multi-orbit window with favorable illumination geometry and returns a throttled,
illumination-filtered image list; `generate_dataset()` takes that list and generates real synthetic
images for it, reusing the existing single-image pipeline (`camera.build_camera`,
`lunaserv.fetch_dem_and_ortho`, `render.run_sat_sim`), just parameterized per image.
`generate_dataset()` also computes each image's real WAC crop footprint
(`tie_points.crop_footprint_corners_for_camera`) and passes it to `fetch_dem_and_ortho` so the
DEM/ortho AOI is always sized to cover both the synthetic camera's own footprint and the real WAC
crop's -- not just a notebook-local concern, since any caller of `generate_dataset()` may want to
display the real WAC crop later (see `GenerationResult.crop_footprint`).
"""

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import spiceypy as spice

from trntest import cache, camera, catalog, illumination, lunaserv, render, spice_kernels, tie_points
from trntest.camera import Camera, FrameTiming
from trntest.config import TrntestConfig, load_config
from trntest.lunaserv import LunaservResult
from trntest.render import RenderResult

# Same neighborhood as this repo's original single-demo product, so calling select_dataset() with
# no arguments is reproducible and "just works."
DEFAULT_SEARCH_START = datetime(2019, 11, 1, tzinfo=UTC)

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
    # -- computed once here (needed to size lunaserv_result's own AOI fetch below) and reused by
    # Phase 6, rather than recomputed later
    lunaserv_result: LunaservResult
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
    ~15-30s ISIS round-trip in one `select_dataset()` sweep). Safe to force the cheap NAIF path for
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


def _candidate_geometry_windows(
    node_ets: list[float], num_orbits: int, min_lan_offset_deg: float
) -> list[tuple[float, float]]:
    """Every sliding window of num_orbits *consecutive* ascending-node crossings (N+1 crossings
    bound N orbits) where every crossing in the window is >= min_lan_offset_deg off the terminator."""
    node_ets = sorted(node_ets)
    passing = {et for et in node_ets if illumination.node_terminator_offset_deg(et) >= min_lan_offset_deg}
    windows = []
    for i in range(len(node_ets) - num_orbits):
        window_nodes = node_ets[i : i + num_orbits + 1]
        if all(et in passing for et in window_nodes):
            windows.append((window_nodes[0], window_nodes[-1]))
    return windows


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
            f"select_dataset: {len(dropped)} of {len(edr_candidates)} candidate(s) skipped "
            "(geometry/coverage edge case, not a fetch failure):"
        )
        for product_id, exc_type, message in dropped:
            print(f"  {product_id}: {exc_type}: {message}")
    return pd.DataFrame(rows, columns=_ILLUMINATED_COLUMNS)


def _pick_best_window(
    candidate_windows: list[tuple[float, float]],
    illuminated_df: pd.DataFrame,
    num_orbits: int,
    min_images_per_orbit: int | None,
) -> tuple[datetime, datetime, pd.DataFrame]:
    """Pick the geometry-valid window with the highest minimum per-orbit illuminated-image count
    (or, if min_images_per_orbit is given, the first window that clears that floor)."""
    best_window: tuple[datetime, datetime, pd.DataFrame] | None = None
    best_floor = -1
    for window_start_et, window_end_et in candidate_windows:
        window_start_dt = illumination.et_to_datetime(window_start_et)
        window_end_dt = illumination.et_to_datetime(window_end_et)
        in_window = illuminated_df[
            (illuminated_df["start_time"] >= window_start_dt) & (illuminated_df["start_time"] < window_end_dt)
        ]
        if in_window.empty:
            continue
        per_orbit_counts = in_window.groupby("orbit_number").size()
        if len(per_orbit_counts) < num_orbits:
            continue
        floor = int(per_orbit_counts.min())
        if min_images_per_orbit is not None:
            if floor >= min_images_per_orbit:
                return window_start_dt, window_end_dt, in_window
        elif floor > best_floor:
            best_floor = floor
            best_window = (window_start_dt, window_end_dt, in_window)

    if best_window is None:
        raise ValueError(
            "No geometry-valid window had sufficient per-orbit WAC image availability -- try a "
            "larger max_search_days, or lower min_sun_elevation_deg/min_lan_offset_deg."
        )
    return best_window


def _attach_cdr_fields(throttled: pd.DataFrame, config: TrntestConfig) -> pd.DataFrame:
    """Look up each throttled row's matching CDR product, skipping (with a warning) any that has
    none rather than crashing the whole selection."""
    rows = []
    for _, edr_row in throttled.iterrows():
        cdr_row = catalog.find_matching_cdr(edr_row, config)
        if cdr_row is None:
            print(f"select_dataset: no matching CDR found for {edr_row['product_id']}, skipping")
            continue
        merged = edr_row.to_dict()
        merged["cdr_volume"] = cdr_row["volume"]
        merged["cdr_subdir"] = cdr_row["subdir"]
        merged["cdr_doy"] = cdr_row["doy"]
        merged["cdr_product"] = cdr_row["product"]
        rows.append(merged)
    return pd.DataFrame(rows)


def select_dataset(
    config: TrntestConfig | None = None,
    search_start: datetime | None = None,
    num_orbits: int = 12,
    min_lan_offset_deg: float = 30.0,
    throttle_minutes: float = 5.0,
    min_sun_elevation_deg: float = 10.0,
    max_search_days: float = 30.0,
    min_images_per_orbit: int | None = None,
) -> pd.DataFrame:
    """Query the real LROC catalog for a ~num_orbits-orbit window with favorable illumination
    geometry (ascending node >= min_lan_offset_deg off the terminator, so either the ascending or
    descending pass is well-lit) and good WAC data availability, and return the throttled,
    illumination-filtered image list for it as a DataFrame (columns: DATASET_COLUMNS).

    By default (min_images_per_orbit=None) picks the geometry-valid window with the highest minimum
    per-orbit illuminated-image count, evaluated exhaustively over the search range. Pass
    min_images_per_orbit to instead take the first geometry-valid window that clears that floor.
    """
    config = config or load_config()
    search_start = search_start or DEFAULT_SEARCH_START

    spice_kernels.fetch_and_furnish(search_start, config)  # gets LSK etc. loaded for utc_to_et
    start_et = illumination.utc_to_et(search_start)
    end_et = start_et + max_search_days * 86400.0

    node_ets = illumination.find_ascending_node_crossings(start_et, end_et, config)
    candidate_windows = _candidate_geometry_windows(node_ets, num_orbits, min_lan_offset_deg)
    if not candidate_windows:
        raise ValueError(
            f"No {num_orbits}-orbit window found with ascending node >= {min_lan_offset_deg} deg "
            f"off the terminator within {max_search_days} days of {search_start}. Try a larger "
            "max_search_days."
        )

    edr_candidates = catalog.list_products(
        config, catalog.EDR_PRODUCT_TYPE, search_start, search_start + timedelta(days=max_search_days)
    )
    illuminated_df = _evaluate_illuminated_candidates(edr_candidates, config, min_sun_elevation_deg)

    window_start_dt, window_end_dt, window_images = _pick_best_window(
        candidate_windows, illuminated_df, num_orbits, min_images_per_orbit
    )

    throttled = throttle_by_time(window_images, throttle_minutes)
    result = _attach_cdr_fields(throttled, config)
    result = result.rename(
        columns={"volume": "edr_volume", "subdir": "edr_subdir", "doy": "edr_doy", "product": "edr_product"}
    )
    result = result[DATASET_COLUMNS].reset_index(drop=True)

    per_orbit_counts = window_images.groupby("orbit_number").size().to_dict()
    print(
        f"select_dataset: {len(result)} images across {result['orbit_number'].nunique()} orbits "
        f"({window_start_dt} to {window_end_dt}); per-orbit counts: {per_orbit_counts}"
    )
    return result


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
            per_image_config = dataclasses.replace(
                config,
                edr_volume=row["edr_volume"],
                edr_subdir=row["edr_subdir"],
                edr_doy=str(row["edr_doy"]),
                edr_product=row["edr_product"],
                cdr_volume=row["cdr_volume"],
                cdr_product=row["cdr_product"],
                target_frame_index=round(row["start_frame"]),
                output_dir=output_dir / product_id,
            )
            built_camera = camera.build_camera(per_image_config)
            frame_timing = camera.fetch_frame_timing(per_image_config)
            crop_footprint = tie_points.crop_footprint_corners_for_camera(frame_timing, built_camera, per_image_config)
            lunaserv_result = lunaserv.fetch_dem_and_ortho(
                built_camera, per_image_config, extra_footprint_lonlat_deg=crop_footprint
            )
            render_result = render.run_sat_sim(built_camera, lunaserv_result, per_image_config)
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
                lunaserv_result=lunaserv_result,
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
