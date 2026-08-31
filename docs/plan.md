# trntest — architecture & status

## What this is

Explores techniques for generating candidate test imagery for terrain-relative navigation (TRN)
testing (hence the repo name) — images that could stand in for real spacecraft imagery when testing
a TRN algorithm. Two candidate pipelines: a synthetic lunar satellite image (with a CSM/"ISD" JSON
camera sidecar) generated via NASA's Ames Stereo Pipeline (ASP) `sat_sim` tool, fed by real DEM +
visible imagery pulled live from the Lunaserv WMS server; and the same real footprint's actual LROC
WAC image, processed through ISIS3's own EDR-to-calibrated-cube pipeline. Both share one thing that
makes them usable as TRN test images at all: the camera pose is derived from the **real LRO
spacecraft trajectory** (NAIF SPICE kernels) at the time of a real LROC WAC image, so each
candidate's own geometry can be validated against a real hillshade-based basemap — two ways (a raw
image-quality check, and a true pixel-for-pixel `mapproject` overlay) — rather than just asserted.
This is a demo/exercise in AI-assisted coding on a real geospatial engineering task.

All heavy tooling/build/test happens inside a Docker container (Ubuntu 24.04), built from the
checked-in `docker/Dockerfile`, so it's reproducible off this host.

## Status

The demo runs end-to-end and is stable: real LRO SPICE trajectory → posed synthetic camera →
`sat_sim` render + CSM/ISD sidecar, and, from the same real footprint's EDR, ISIS3's own calibrated
real-WAC product. Each candidate TRN test image's geometry is validated against a real hillshade
basemap two ways — a raw, north-up-rotated quality check (`plotting.plot_render_vs_basemap`) and a
true pixel-for-pixel `mapproject` overlay (`plotting.plot_overlay`) — and the two candidates are also
compared directly against each other with explicit SPICE-derived tie points. Packaged as an
installable library (`src/trntest/`) with config, tests, and style tooling.

The **live default image comes from a real, catalog-driven multi-orbit search**, not a single
hardcoded product: `notebooks/dataset_manifest.csv` (checked in, frozen -- see `docs/history.md`'s
dated entry for why the notebook that used to regenerate it was removed) holds the throttled,
illumination-filtered result of one such search; `generate_dataset()` renders the chosen one(s)
through the pipeline described above. `dataset.images_for_window()` is the still-live version of
that same catalog-query/evaluate logic, now used to resolve an already-selected orbit-sequence
window (`dataset_selection.resolve_orbit_sequence`) rather than to search fresh. There is no
current dependency on any one specific EDR product or framelet index — see `docs/data-sources.md`
for the couple of specific products still used as regression-test fixtures, and `docs/history.md`
if you're curious how the demo evolved from a single hand-picked product to this.

## Architecture (`src/trntest/`)

| Module | Responsibility |
|---|---|
| `config.py` | `TrntestConfig`/`load_config()` — endpoints, paths, product IDs, tunables. TOML file + `TRNTEST_*` env var overrides. |
| `cache.py` | Local-mirror disk caching for all external fetches (NAIF, Lunaserv, LROC) — see `docs/caching.md`. |
| `product_registry.py` | Intermediate-product access-discipline primitives (`docs/intermediate-product-discipline.md`'s principles, rolled out per `docs/history.md`'s Phase 79/80 entries): `writes_product`/`reads_product`/`deletes_product` decorators (a lightweight registry, `writes_product` raising at decoration time on a duplicate label) and `atomic_publish`/`atomic_publish_path`/`atomic_publish_prefix` (write-to-uniquely-named-temp-then-atomic-rename, generalizing `cache.cached_get`'s own pattern from fetched files to generated ones, in three shapes for three real writer conventions — a caller-chosen exact path, a tool's own `to=`/`-o` exact-path parameter, and a tool's own output-*prefix* parameter with its own fixed appended suffix, e.g. `dem_mosaic`'s `<prefix>-tile-0.tif`). `atomic_publish_path`/`atomic_publish_prefix` preserve the destination's real file suffix on the temp path — confirmed live to matter twice: a generic `.tmp` suffix made real ISIS `to=` calls silently write nothing at all, and (separately) `isis_wac.run_lrowac2isis`'s 4-file, non-atomic output plus a bare-existence idempotency check in `_spiceinit_vis_even_cube` produced a real, reproducible `KeyError` under two concurrent workers. Applied to `lunaserv.fetch_dem`/`fetch_and_shade_ortho`/`hole_fill_dem`, `isis_wac.run_lrowac2isis`/`run_framestitch`/`crop_for_camera`/`run_cam2map_for_crop`, `render.run_sat_sim` — see `docs/history.md`'s Phase 79/80 entries for the full rollout and what's still open (the DEM output's own filename still isn't footprint-aware, so two `fetch_dem` calls with genuinely different footprints against the same output dir can still silently disagree about "the" DEM; two workers both `spiceinit`-ing the exact same physical cube at once is a narrower, unconfirmed residual risk; `TrnTestImage`'s *published* `crop`/`hillshade`/`reproject` outputs aren't registry-covered yet either). |
| `spice_kernels.py` | Selects/downloads the minimal SPICE kernel set for a date, furnishes it (`fetch_and_furnish`, `furnish_spk_range`). WAC CK (pointing) kernel selection defaults to asking a real ISIS `spiceinit` run what it resolves (`select_isis_wac_ck_kernels`, via `isis_wac.resolve_wac_ck_kernels`), falling back to a NAIF-metakernel-based heuristic (`select_naif_wac_ck_kernels`, deprecated but numerically equivalent) for dates outside that resolution's own coverage — see `docs/data-sources.md`. |
| `camera.py` | Poses the synthetic camera from real SPICE trajectory/orientation data; `build_camera()`, `FrameTiming`/`fetch_frame_timing()` (EDR label parsing), sensor-axis convention (`boresight_rotation_k`). `build_camera()`'s final boresight is *re-aimed* (`look_at_rotation`) at a real, ISIS-determined target ground point (`isis_wac.run_pipeline`/`ground_point_at_pixel`) rather than trusted directly from `spice.pxform`'s raw `[0,0,1]` -- that raw assumption is confirmed measurably wrong for WAC-VIS (see `docs/history.md`'s dated entry). Camera *position* (`camera_pose_moon_me`) is untouched, already confirmed exactly correct. `build_camera()` also solves a corrected, isotropic `(f, f, cu, cv)` (`solve_corrected_fov`, `Camera.focal_length_u_px`/`focal_length_v_px`/`principal_point_u_px`/`principal_point_v_px` -- `cv` is no longer necessarily `image_size/2`, though `fu`/`fv` are always equal and `cu` always is) so the render's FOV stays inside the real WAC crop's own footprint, applied once and shared by every product type built from the resulting `Camera` -- an earlier anisotropic (`fu != fv`) version of this correction was tried and reverted after causing real downstream friction; see its own docstring and `docs/reproject-fov-investigation.md`. `footprint_lonlat`'s `"center"` entry is the boresight ray `(cu, cv)`, not a hardcoded `(size/2, size/2)` -- the two only coincide when the principal point is centered. `footprint_width_height_km` measures a footprint's real ground width/height directly from its corners (`Camera.render_cross_track_km`/`render_along_track_km`) -- distinct from `cross_track_width_km` (crop-window-derived, still describes the real WAC crop's own extent, not the synthetic render's, now that the two can differ). |
| `illumination.py` | Sun/orbit geometry via real SPICE functions — sun elevation/azimuth, sub-solar point, node-crossing search (`find_node_crossings`/`find_ascending_node_crossings`, `gfposc`; deliberately doesn't call `fetch_and_furnish` per crossing — pure position, no CK needed, and doing so used to cost ~70% of the function's own runtime and could crash on a wide multi-month sweep, see its docstring). `hour_angle_deg` (signed solar hour angle, -90/0/+90 = sunrise/noon/sunset) and the circular-arithmetic trio `circular_distance_deg`/`circular_mean_deg`/`unwrap_relative_deg` (e.g. -179 and +179 are 2 degrees apart, not 358; used throughout `dataset_selection.py` and `plotting.py`'s dataset-underline wraparound handling) round out the angle-math helpers `terminator_offset_deg` already established this pattern for. |
| `maneuver_detection.py` | Detects likely propulsive discontinuities (stationkeeping burns, momentum unloads, eclipse-phasing maneuvers) directly from LRO's public reconstructed-orbit SPK, since no public source of the flight-dynamics team's own maneuver log ("small forces file") is known to exist for LRO. `detect_discontinuities` (pure/SPICE-free, fast-tested against a synthetic RK4-propagated two-body orbit with injected impulses) flags joint step changes across specific angular momentum `h = r x v` and specific orbital energy `eps = v^2/2 - GM/r` (`sample_orbit_state`/`find_maneuver_candidates`, SPICE- and network-dependent, `@pytest.mark.heavy`-tested) — chosen over the classical orbital elements (a, e, i) for having a clean, phase-independent null only on the radial impulse component, unlike inclination's node-crossing blind spot; each detected candidate's impulse is reconstructed (weighted least squares, `rcond`-truncated against the one real residual gap — radial-component recovery on a near-circular orbit) and decomposed into radial/tangential/normal components. See the module docstring for the full derivation and `docs/dataset-selection.md` for the literature validation, including a genuine finding this redesign surfaced (several H2 2019 momentum unloads are normal-direction-dominant, several times larger in total than a semi-major-axis-only estimate would report). Wired into `dataset_selection.add_maneuver_flags` (flags a whole orbit-level table at once, not per-candidate) — not into `dataset.images_for_window()` itself. |
| `catalog.py` | PDS ODE REST API client — lists real EDR/CDR products by time range, matches EDR↔CDR pairs. `list_products`'s pagination continuation is decided from the server's own raw `<Product>` count in each page, not `len(page_df)` — a page can have a few entries `parse_catalog_entries` drops for a missing/malformed field, landing the parsed count just under `_PAGE_SIZE` even though the server sent a genuinely full page with more still to come; using the parsed count as the "last page" signal silently truncated a real year-long query to its first 5000 raw entries (4996 parsed) out of what should have been ~53k (found live via `dataset_selection.py`'s full-year query; regression-tested). |
| `dataset_selection.py` | Orbit-level TRN-OD dataset selection (`notebooks/select_datasets.py`) — distinct from `dataset.py`'s catalog-driven *single-image* evaluation below, which it hands an already-selected orbit-sequence window to resolve, not the other way around. Answers a different question: which multi-day spans of consecutive orbits make good maneuver-free TRN-OD test data, jointly diverse in solar hour angle. Pipeline: `find_orbits` (every orbit's "illuminated node" — whichever of the ascending/descending node pair has the higher sun elevation — longitude/hour-angle/sun-elevation) → `add_maneuver_flags` (`maneuver_detection.find_maneuver_candidates`, once over the whole period, not per-orbit — a short window doesn't give the detector's noise-floor calibration enough background samples to be reliable) → `add_acceptable_edr_counts` (real WAC EDRs per orbit meeting tunable sun-elevation/emission-angle thresholds) → `enumerate_candidate_datasets` (every acceptable sliding window of consecutive orbits, with no illuminated-node flip inside it — what makes the circular-mean "center" statistics behave like an average of nearby values rather than splitting across ~180-degree-apart nodes) → `select_diverse_datasets` (greedy farthest-point selection: each pick maximizes its own minimum center-hour-angle distance to every dataset already chosen, excluding anything too close in center longitude or overlapping orbits with a prior pick; raises rather than silently returning fewer than requested if the pool is exhausted first) → `resolve_orbit_sequence` (the bridge back to `dataset.py`'s EDR-list world: turns exactly one selected orbit-window row into a real `dataset.DATASET_COLUMNS` images table, via `dataset.images_for_window`; deliberately takes one row at a time, not the whole `select_diverse_datasets` table, to keep iteration fast — see `docs/history.md`'s dated entry for the real rate-limit incident this one-at-a-time discipline and the catalog-metadata pre-filter below were both driven by). See the module's own docstrings for the full per-step rationale. |
| `dataset.py` | Public multi-image API: `images_for_window(start_dt, end_dt, ...)` (real per-candidate camera-pose/sun-elevation evaluation over an already-chosen time window, throttled/illumination-filtered result columns: `DATASET_COLUMNS`) and `generate_dataset()` (renders selected images through the single-image pipeline). Also computes each image's real WAC crop footprint (`tie_points.crop_footprint_corners_for_camera`) and passes it to `lunaserv.fetch_dem_and_ortho` so the DEM/ortho AOI covers both the synthetic camera's footprint and the real WAC crop's — exposed on `GenerationResult.crop_footprint` for reuse (e.g. by the notebook's Phase 6). `images_for_window`'s usual caller is `dataset_selection.resolve_orbit_sequence`, given one already-selected orbit-sequence window; an earlier `select_dataset()` searched for its own window from a fresh date range and shared this same evaluate/finalize tail (`_finalize_images`/`_evaluate_illuminated_candidates`) internally, removed once nothing called it anymore (see `docs/history.md`'s dated entry). Runs a cheap `_prefilter_by_catalog_metadata` pass first (sun-elevation/emission-angle thresholds computed straight off catalog fields, no per-candidate fetch) before the real per-candidate SPICE evaluation — cuts the candidate count before any network round-trip, not just a redundant re-check; added after a real one-window resolve without it tripped a live LROC rate limit (see `docs/history.md`'s dated entry). `attach_cdr=False` is `images_for_window`'s default-off opt-in for its own CDR-matching step — confirmed the `cdr_*` columns' only real consumer anywhere in this codebase is `wac.py`, itself already superseded by `isis_wac.py`, so skipping it avoids an unnecessary per-candidate network round-trip for callers (like `resolve_orbit_sequence`) that don't need it. |
| `lunaserv.py` | Fetches ortho imagery and the DEM, both reprojected onto one shared per-camera local Orthographic CRS (real Moon radius) centered on that camera's footprint (optionally unioned with an extra footprint via `union_bbox`, see `dataset.py`) — genuinely isotropic meter pixels either way. **Live default ortho source (`fetch_dem_and_ortho`'s `ortho_source="wac_emp_pds"`, Phase 78) is WAC_EMP's own real reflectance, fetched directly from its PDS4 archive** (`wac_emp_tile_id_for_bbox`/`fetch_wac_emp_reflectance`/`reproject_wac_emp_reflectance_to_local_grid`) — replaces the original Lunaserv-WMS ortho fetch (`luna_wac_normalized_reflectance`), confirmed (Phase 78) to carry a real, uncorrected affine display stretch, not raw reflectance. `wac_emp_tile_id_for_bbox` resolves the covering 60°×90° equirect tile (raises beyond `WAC_EMP_MAX_ABS_LATITUDE_DEG = 60.0`, an equator straddle, or a lon-zone-boundary straddle — no multi-tile mosaic); the tile-naming scheme was confirmed live via the archive's own real S3 directory listing, not guessed. The deprecated `ortho_source="lunaserv_wms"` path is kept reachable for comparison, only numerically coherent with `hapke=False` (see `fetch_dem_and_ortho`'s own docstring). The DEM is fetched separately from USGS Astropedia's flat-file GLD100 (`fetch_dem_astropedia`/`reproject_astropedia_elevation_to_local_grid` — Lunaserv's own DTM layer has a real, unfixable-client-side artifact, see `docs/data-sources.md`). `astropedia_coverage_bbox_deg` derives the Astropedia fetch's degree-space AOI directly from the destination Orthographic grid's own (already-padded) meters bbox (`rasterio.warp.transform_bounds`), not by independently padding a second, separate degree-space bbox — the old approach could (and did) undershoot the destination grid's own corners, leaving real nodata triangles there regardless of how generous `dem_padding_fraction` was (see `docs/history.md`'s dated entry). `fetch_dem_native`/`reproject_dem_to_local_grid` (the deprecated Lunaserv-DEM path) are kept for reference/comparison, no longer called by the default pipeline. Despeckles the ortho and blends in a real-sun-lit hillshade (`sat_sim` applies no illumination model of its own) — defaults (`DEFAULT_HAPKE_SHADING`) to a real Hapke BRDF via ISIS `photomet` (`hapke_shade_ortho`), using the camera's own real position (not a nadir approximation) for true per-pixel emission/phase, with the original plain Lambertian blend (`shade_ortho`) kept as an explicit fallback (`hapke=False`). **Since Phase 78, `hapke_shade_ortho` returns real relit reflectance directly (float, physical units), not a display-ready `uint8` image** — `ortho.astype(np.float64) / 255.0`'s old DN-un-scaling assumption is gone now that the default ortho source is real, unstretched reflectance; a new, explicit `stretch_reflectance_to_uint8` (`DISPLAY_STRETCH_REFLECTANCE_MIN`/`_MAX`, a fixed linear range) is the one place the cosmetic display stretch happens, applied by `despeckle_and_shade_ortho` at the very end of the pipeline. `shade_ortho` itself is deliberately unchanged (still DN-`[0,255]`-only) — `despeckle_and_shade_ortho`'s own `ortho_source` parameter decides whether to pre-stretch the cleaned array before handing it to `shade_ortho`, fixing a real bug (an all-black Lambertian ortho) the Phase 78 migration caught live. `along_track_correction` (`DEFAULT_ALONG_TRACK_CORRECTION`) further corrects for this project's single-frozen-camera-pose approximation of a real multi-second pushframe scan, using the camera's own along-track attitude axis (`Camera.camera_along_track_direction_moon_me`) — also on by default, with the uncorrected geometry kept as an explicit fallback (`along_track_correction=False`). `_terrain_photometric_angles` is fully MOON_ME-native (Phase 77): each DEM pixel's true 3D position (`ground`) comes from one vectorized `rasterio.warp.transform` call from `local_orthographic_crs` to a new `moon_geocentric_crs` (a real `+proj=geocent` CRS), replacing three successive hand-derived closed-form corrections found across Phases 70/72/76 (a sagitta term for ground position, a normal-tilt correction for the surface normal, then a relief-displacement fix for both together) — confirmed live to be numerically identical to the last of those (Phase 76), since the closed form was already exact, just harder to trust by inspection than one call to well-tested library code. `Camera.camera_center_moon_me_m`/`camera_along_track_direction_moon_me` (already real MOON_ME) are used directly with no local-frame rotation at all; only the sun's `azimuth_deg`/`elevation_deg` (the one remaining human-readable, non-position input) is still rotated into MOON_ME once, via `_moon_me_direction_from_local_enu` — exact and lossless since it's a free direction, not a position embedding. Independently confirmed against real-`campt` ground truth (`tests/test_lunaserv_campt_validation.py`, ~0.018 deg max diff), despite an unresolved real-WAC-crop regression (see below). `hapke_shade_ortho` also relights the ortho texture by the ratio H(i,e,g)/H(reference-geometry), not a bare rescaled H(i,e,g) (Phase 72 fix, also unconditional — `config.lunaserv_ortho_layer` was confirmed to be ASU's WAC_EMP product, itself photometrically normalized to a fixed i=30/e=0/g=30 geometry via an empirical, non-Hapke function, so the old bare-rescale blend wasn't a relighting operation at all). **Regression opened at Phase 70/72, now looking resolved as of Phase 78 (not definitively proven why)**: at the time, this combination measurably made the brightness-matched diff against the real WAC crop worse, not better, on the one candidate tested — both corrections were independently believed correct on physical/geometric grounds, but neither was confirmed to help, and their real-image interaction wasn't understood. **Phase 78's WAC_EMP-PDS ortho-source migration** (this row's own opening paragraph) re-measured the same diff on the same candidate, using the codebase's own established `compute_brightness_matched_diff` calling convention, and got `mean_abs_diff = 0.00382` — squarely back inside Phase 74's own recorded pre-regression "healthy" range (~0.0032-0.0043), not the elevated values the regression showed. Consistent with (not definitive proof of) the WMS ortho's confirmed affine display stretch having been a real contributor — the geometry corrections themselves are untouched by Phase 78 and their own independent residuals (`campt` ~0.018 deg, `sfs` Lambertian ~0.0005 deg) are confirmed unchanged. See `docs/history.md`'s Phase 70/71/72/78 entries. `hapke_shade_ortho`'s Hapke coefficients themselves default (`DEFAULT_REAL_HAPKE_PARAMS`) to `fetch_real_hapke_params` — real, spatially-resolved lunar Hapke calibration (Sato et al. 2014's fit) sampled from ISIS's own `$ISISDATA/lro/calibration/WAC_global_7bands_1x1_wbhs70NS_const_each_pole.cub` (already part of the `lro` ISIS data package `isis_wac.ensure_isisdata` fetches, not a new download) at the candidate's own footprint center — replacing `_HAPKE_PLACEHOLDER_PARAMS`'s illustrative constants, kept as the explicit `real_hapke_params=False` fallback. A single value per image, not per-pixel, despite real (if secondary, next to the placeholder-vs-real gap) spatial variation within one footprint — see `fetch_real_hapke_params`'s own docstring. `reference_hapke_reflectance`/`hapkehen_params_from_source`/`real_geometry_hapke_reflectance`/`real_geometry_photometric_angles` (Phase 74/75) factor the H(reference-geometry) evaluation, the 6-key HAPKEHEN param slice, the full real-per-pixel-geometry H(real) evaluation, and the bare `(incidence_deg, emission_deg, phase_deg)` computation out of `hapke_shade_ortho` into shared, reusable functions — `sfs_validation.py`'s own "true albedo" map reuses `real_geometry_hapke_reflectance` directly (dividing by H(real), not the constant H(reference) — an earlier version divided by the constant instead, a real double-counting bug, see `docs/history.md`'s Phase 74 follow-up), and its Lambertian-mode incidence cross-check reuses `real_geometry_photometric_angles` directly. Independently confirmed against `sfs`'s own ray-DEM-intersection incidence angle to ~0.0005 deg mean/max across a whole real candidate's coverage region — essentially exact agreement, not just "within known caveats" (see `tests/test_sfs_validation_lambertian_incidence.py`'s own `@pytest.mark.heavy` test, this project's first genuine DEM-aware ground-truth check, Phase 75; the same ~0.0005 deg result held before and after the Phase 77 MOON_ME-native refactor above, confirming it's numerically equivalent, not a further correctness fix). See `docs/history.md`'s dated entries and `notebooks/hapke_hillshade.ipynb`/`notebooks/along_track_correction.ipynb`/`notebooks/real_hapke_params.ipynb`. |
| `render.py` | Runs `sat_sim`/`cam_gen` to produce the rendered `.tif` + CSM/ISD JSON sidecar. `run_mapproject_image` stays generalized (`camera_path`/`camera_type` rather than hardcoded to CSM) as good hygiene, even though its one live caller (`trn_dataset.TrnTestHillshadeImage._mapprojected_path`) always uses the CSM sidecar (`camera_type="csm"`, the default) now: `camera.solve_corrected_fov` is isotropic (`fu == fv` always), so `cam_gen`'s Pinhole -> CSM Frame conversion has no per-axis anisotropy to collapse or lose. An earlier anisotropic (`fu != fv`) version of the FOV correction *did* need a dedicated post-hoc fix here (`_correct_csm_focal_length_anisotropy`, rescaling `m_iTransL`/`m_transY` to restore what `cam_gen` silently averaged away) -- removed once the FOV correction reverted to isotropic and the function became permanent dead code. `run_mapproject` (the `RenderResult`-based CSM-sidecar wrapper) has no live caller, kept as-is. See `docs/reproject-fov-investigation.md`. |
| `wac.py` | Extracts a band-separated, along-track-stacked VIS mosaic from a real WAC CDR product via manual, hand-derived byte offsets. Superseded by `isis_wac.py` as the demo notebook's real-WAC comparison method (see the open items below) but left in place, untouched, with its own unit test coverage. |
| `isis_wac.py` | Steps a real WAC EDR through ISIS3's own pipeline (`lrowac2isis`/`spiceinit`/`lrowaccal`/`framestitch`) -- calibrated, band-separated, and framelet-interleaved through a genuine camera-model-aware toolchain. `crop_for_camera` then crops the stitched cube (ISIS's own `crop` app) down to the real footprint being compared -- a single, real "WAC crop" cube both the notebook's raw-pixel display and `run_cam2map_for_crop` consume, with no special-casing. `run_cam2map_for_crop` reprojects it onto the DEM via ISIS's own *native* Pushframe camera model (`cam2map`, using a PVL map file cloned from `DemOrthoResult`'s own local Orthographic CRS) rather than ASP's `mapproject`/CSM -- a real bug in `usgscsm`'s `groundToImage` for Pushframe sensors made the CSM route unusable for a crop this size (see `docs/history.md`'s dated entry). `resolve_ground_to_image_model`/`ground_to_image_pixel` generalize that same resolution order (try a CSM ISD sidecar, fall back to the crop's native model only if it resolves to a Pushframe sensor) into a reusable ground-to-image query, now also used for `tie_points.resolve_crop_pixels`. `ground_point_at_pixel` is the reverse (image-to-ground), used by `camera.build_camera()` to re-aim the synthetic camera's boresight at a real target. `run_pipeline`/`crop_for_camera` are both idempotent (take `flip: bool` directly, not a `Camera`, for `run_pipeline` -- avoids a circular data dependency with `build_camera()`; safe to call again from the notebook's own Phase 6 cell or `tie_points.crop_footprint_corners_for_camera` for the same product -- reuses, doesn't redo, the ISIS work). `run_isd_generate` is still called (by `resolve_ground_to_image_model`, to inspect a full-cube ISD's `name_model`) -- not literally unused -- but its *output* ISD is no longer used for actual reprojection (`run_mapproject`'s CSM path), only `run_cam2map_for_crop`'s native-model path is. `run_isd_generate_for_crop` is a distinct, still-in-use function: an accurately-scoped (crop dimensions, time-corrected) but similarly reprojection-unreliable ISD sidecar for `trn_dataset.TrnTestCropImage`'s on-disk product -- see `docs/external-tools.md`'s "The crop ISD sidecar's real accuracy" section. Must run against the stitched (interleaved) cube, not a lone even/odd parity (see the open items below). `sample_local_dem_patch` (Phase 73) samples a real 3x3 elevation patch (meters, north-first row order) around a ground point via `sample_lunar_dem_radii_batch` -- built to validate `phocube`'s DEM-mode backplanes against this project's own local DEM, which turned out to be a dead end (see `docs/history.md`'s Phase 73 entry), but kept as generic, tested, reusable infrastructure with no other caller yet. |
| `sfs_validation.py` | Cross-checks `lunaserv.hapke_shade_ortho` against Ames Stereo Pipeline's `sfs` tool run purely as a forward renderer (`--save-sim-intensity-only`, no DEM refinement) -- a fully independent ray-DEM intersection and Hapke evaluation given the same real DEM, the same real `fetch_real_hapke_params` coefficients (remapped to ASP's own `omega/b/c/B0/h` order via `hapke_params_to_asp_model_coeffs` -- ISIS's 6th HAPKEHEN parameter, macroscopic roughness `theta`, has no ASP equivalent and is silently dropped, a real permanent gap), and a `true_albedo_map` that recovers `raw_ortho/H(reference)` by dividing `hapke_shade_ortho`'s already-shaded output back down by `lunaserv.real_geometry_hapke_reflectance` (the *same* real-geometry H(real) factor `hapke_shade_ortho` itself multiplied in) -- **not** the constant `reference_hapke_reflectance`, which an earlier version used and which double-counted the reflectance ratio (see `docs/history.md`'s Phase 74 follow-up). Must use this project's own reconstructed CSM Frame camera (`render.run_sat_sim`/`patch_sun_position`), not the real WAC crop's own native ISIS camera model -- confirmed live, `sfs` refuses that camera type outright; this camera also has **no way to represent `along_track_correction`** (a single frozen pose, unlike the real WAC's multi-framelet pushframe scan), which -- more than the double-counting bug -- is the dominant driver of `sfs`'s own excess brightening in a low-phase region, since it implicitly computes something much closer to `along_track_correction=False`'s wider, uncorrected phase swing. `mask_sfs_uncovered` converts `sfs`'s own literal-`0.0` "outside camera coverage" convention to real `nodata` before any brightness comparison (`plotting.compute_brightness_matched_diff`/`plot_sfs_comparison`), or the comparison is dominated by that region instead of real signal. **`run_sfs_lambertian_incidence`/`incidence_deg_from_lambertian_sim_intensity` (Phase 75)** sidestep the Hapke-parameterization/along-track concerns above entirely: `sfs --reflectance-type 0` (Lambertian, `image = exposure * albedo * cos(incidence)`, no emission/phase term) with a uniform `albedo=1` makes `sfs`'s raw output exactly `exposure * cos(incidence)` (`exposure` read back from `sfs`'s own `<prefix>-exposures.txt`, not assumed to be `1.0`), giving `sfs`'s own independent incidence angle directly — confirmed to match `lunaserv.real_geometry_photometric_angles` to ~0.02 deg mean/~0.5 deg max across a whole candidate's real coverage region, this project's first genuine DEM-aware ground-truth check (`tests/test_sfs_validation_lambertian_incidence.py`, `@pytest.mark.heavy`). `_camera_cub_for_sfs` factors the shared camera-attachment steps out of both this and `run_sfs_forward_render`. See `docs/history.md`'s Phase 74 entry and its Phase 74/75 follow-ups (including a real `rasterio`-silently-crops-on-shape-mismatch bug caught and fixed while building this) and `notebooks/sfs_validation.ipynb`. |
| `tie_points.py` | Ground tie points for the comparison figure: `select_tie_points` picks 5 points (real, campt-derived footprint, used only to choose plausible candidates) and projects them into the synthetic image's exact pixel coordinates; `resolve_crop_pixels` fills in each point's real WAC-crop pixel coordinate via `wac_camera_model.find_framelet_and_project` (a from-scratch WAC-VIS camera model reimplementation, validated to exact 0.000px agreement with real ISIS `campt` output -- see `docs/wac-jigsaw-investigation.md`), dropping (with a warning) any point the real camera doesn't actually see. Switched from a direct `isis_wac.ground_to_image_pixel` (`campt`) query after `campt`'s own ground-to-image solve was found to have a real, scattered ~38% failure rate for WAC's Pushframe sensor -- a known upstream ISIS bug (`PushFrameCameraGroundMap::GetLocalNormal`, DOI-USGS/ISIS3#4256), not an edge-of-crop artifact -- which `find_framelet_and_project`'s own containment check sidesteps entirely; see `docs/reproject-fov-investigation.md`'s dated entry for the live investigation that found this. `crop_footprint_corners_for_camera` queries the real WAC crop's own actual ground footprint directly (`isis_wac.ground_point_at_pixel`, inset `_CROP_EDGE_MARGIN_PX` from the cropped cube's edges -- a real `campt` numerical-stability limit near cube boundaries, not a stylistic choice), for `plotting.plot_render_vs_basemap` and `dataset.generate_dataset`'s DEM/ortho AOI sizing; `_crop_footprint_corners_spice_approx` (deprecated) is the old SPICE ray-trace, kept for reference. `die5_points` places its 5 points as a `margin_frac`-shrunk offset from an explicit `center` argument (each of the 4 corners scaled by its own reach from `center` to its own side of the shared bbox), not `bbox`'s own naive `(lon_min+lon_max)/2` midpoint -- found and fixed live after `camera.solve_corrected_fov`'s now-asymmetric synthetic footprint exposed a real bug in the old midpoint approach (a naive-midpoint "center" test point landed entirely outside the real crop's pushframe FOV, dropping the demo's own default candidate from 5-of-5 to 1-of-5 resolving tie points -- see `docs/reproject-fov-investigation.md`). |
| `orientation.py` | Notebook-display-only north-up rotation (does not touch the sensor model). |
| `plotting.py` | Comparison-figure plotting. `plot_render_vs_basemap` is the "A"-style geometry check: a render's own raw pixels next to a plain crop of the hillshade basemap covering the same real footprint (no resampling, no rotation on the basemap side -- its local Orthographic CRS is already north-referenced), both optionally marked with the same SPICE-derived tie points. `plot_overlay` is the "B"-style check: two geo-aligned rasters (e.g. a `mapproject` output over `DemOrthoResult.ortho`) displayed via `rioxarray`, using each file's own real coordinates rather than pixel indices -- expects `overlay_raster_path` to already cover just the real footprint being compared (true for both the synthetic render's own mapproject and `isis_wac.crop_for_camera`'s output), no view-restricting parameter needed. `plot_overlay_toggle` renders the overlay at both alpha endpoints as two complete frames, encoded as a single self-contained, auto-looping animated GIF (`<img src="data:image/gif;...">`) that blinks between them -- no `<style>` block, no anchor links, nothing for either GitHub sanitizer layer to strip, so it renders identically on a live kernel and GitHub's static `.ipynb` viewer. Two earlier click-driven-toggle mechanisms (a `<details>` element, then a CSS `:target` scheme) were each built and validated against what looked like the right sanitizer and still failed live on GitHub -- traced to a server-side rendering pass, upstream of GitHub's client-side DOMPurify sanitizer entirely, that strips `<style>` tags and rewrites same-page `href="#fragment"` links, independently breaking both halves any `:target`-based toggle needs -- see `docs/history.md`'s dated entries (Phases 33-35) for the full trail. `plot_isis_comparison` is the direct candidate-vs-candidate comparison (median-normalized, dead-pixel-filled). **Since Phase 92, every brightness comparison in this module normalizes each side independently to its own valid-pixel median = 1.0** (`_robust_median`/`_normalize_to_median`), rather than matching one side to the other's absolute level -- so a shared display `vmax` (the largest of all panels' own post-normalization percentile, not one panel's alone) can't let any panel oversaturate, even a much darker one that needed a big correction. `compute_brightness_matched_diff` (Phase 72, redefined Phase 92) is the quantitative counterpart to `plot_overlay`'s visual comparison -- a real, reusable mean\|diff\| between two geo-aligned rasters (reuses `_prep_overlay_rasters`, aligns mismatched extents by real coordinate via `reindex_like`), built specifically because every prior such number in this project's history had been a hand-rolled, unreproducible throwaway script (see `docs/history.md`'s Phase 72 entry). **Its numbers are dimensionless fractions of each raster's own median brightness as of Phase 92** (`0.05` == "5% of typical brightness"), not raw diffs in one raster's own absolute units -- every such number cited anywhere in this file or `docs/history.md` from before Phase 92 is not comparable to a fresh run; see that phase's entry for the full rationale and a freshly regenerated reference number. `plot_sfs_comparison` (Phase 74) is a 3-panel real-WAC/our-hillshade/`sfs`-forward-render comparison, `sfs_validation.py`'s own visual counterpart to `compute_brightness_matched_diff` -- shares the same normalization technique and a small refactored-out `_cellsize_m` helper. `plot_incidence_validation` (Phase 75) is a 3-panel sfs-incidence/our-incidence/diff comparison for `sfs_validation`'s Lambertian-mode DEM-aware incidence cross-check -- takes plain in-memory arrays (not raster paths) since both fields are already computed by the time a caller has something to compare, per-panel colorbars (unlike `plot_sfs_comparison`'s shared scale) since the diff panel's own range is deliberately much tighter. `plot_illuminated_node_scatter`/`plot_sun_elevation_vs_edr_count` (`notebooks/select_datasets.py`) are unrelated to the render/WAC comparison figures above -- one marker per orbit (viridis-colored by acceptable-EDR count, red X for a maneuver, optional black/grey "underline" per `dataset_selection.select_diverse_datasets` pick, wraparound-broken via `illumination.unwrap_relative_deg`/`_underline_segments`) and a sun-elevation-vs-EDR-count 2D histogram, respectively. `plot_zoom_blink` is `plot_render_toggle`'s blink mechanism applied to two geo-aligned, map-projected rasters instead of two same-grid renders -- reindexes the first onto the second's own pixel grid and brightness-matches it, then blinks a full-resolution square crop (`crop_px`, default 200px) from the middle of the *second* raster's own footprint, so fine per-pixel detail isn't compressed away the way `plot_overlay_toggle`'s whole-footprint figure does. The second raster (not the first) anchors the crop deliberately -- a padded/unioned basemap AOI's own array center isn't guaranteed to sit on a given candidate's own footprint center (confirmed live: up to ~10km off for this project's default candidate), so anchoring on it instead risks cropping mostly nodata. `TrnTestImage.plot_zoom_blink_over` (`trn_dataset.py`) is its notebook-facing wrapper -- always passes its own already-generated raster as the anchor (second/`raster_b` slot) and an optional `other: TrnTestImage | None` as the first/left-hand slot, `None` (default) standing in for the basemap (`self.entry.dem_ortho_result.ortho`) -- the same round-trip pair `plot_overlay`'s own `(base_raster_path, overlay_raster_path)` shows, at full pixel detail instead of squeezed into one fixed-size figure; passing another `TrnTestImage` compares directly against that generator's own map-projected raster instead. Driven by `generator_name` (`"hillshade"`/`"crop"`/`"reproject"`) + `plotting.mathtt` for its default short-name labels -- also now `plot_vs_basemap`/`plot_overlay`'s own default title/label source, instead of the longer `render_label`. Live in `image_generation.ipynb` as Phase 5C/6C (`entry.hillshade.plot_zoom_blink_over()` / `entry.crop.plot_zoom_blink_over()`). |
| `tasks.py` | Two `huey` (sqlite-backed) task queues -- `huey` (`populate()`'s, `immediate=True` + `immediate_use_memory=False` so it executes synchronously in-process with no separate consumer needed, while a failure still persists to real sqlite and survives a fresh `docker compose run`'s `status()` call) and `huey_parallel` (`populate_via_workers()`'s, `immediate=False`, its own sqlite file) -- replaced the old filesystem lock/error files (see `docs/history.md`'s Phase 66/67 entries for both the migration and the worker-pool addition). Two instances, not one, because huey fixes `immediate` at construction time and the two real use cases need opposite values; each has its own thin `@task()` wrapper (`generate_product`/`generate_product_parallel`) around a shared `_generate_entry(entry, product_types)` helper. **One task per *entry*, not per `(entry, product_type)`** (`docs/history.md`'s dated entry) -- covers every requested-and-pending product type for that entry, attempting each independently (one's failure doesn't block the others; a new `EntryGenerationError` carries every failure, raised only after all types were attempted) and returning `{product_type: raster_path}` for the ones that succeeded. This is what lets `populate()`'s/a single `-k process` worker's own `functools.cached_property`-cached `entry.camera`/`entry.dem_ortho_result` be computed once and reused across that entry's product types, rather than redundantly rebuilt per type when two types of the same entry used to land on two different worker processes -- must still return a non-`None` value or huey never stores a result for a successful run, confirmed empirically after it caused `populate()` to hang forever on retry. `task_id(dataset_folder, product_id)` (no `product_type` parameter anymore) is the deterministic id this stores under. `start_consumer`/`stop_consumer` manage a real `huey_consumer trntest.tasks.huey_parallel -w N -k process` subprocess -- `Consumer.start()` refuses to run against an `immediate=True` instance and isn't safely embeddable in a thread (registers OS signal handlers), confirmed via its own source, so real worker-pool execution needs a genuinely separate OS process. |
| `trn_dataset.py` | `TrnTestDataSet`/`TrnTestEntry`/`TrnTestImage` — a structured, self-contained dataset folder (`manifest.csv` + `crop`/`hillshade`/`reproject` subfolders) replacing `dataset.generate_dataset()`'s flat, all-at-once output layout for the notebook-facing generation path. `TrnTestEntry` holds one manifest row's shared, `functools.cached_property`-cached state (camera, frame timing, the real WAC pipeline's stitched/cropped cubes, DEM/ortho); `TrnTestImage` (abstract; `TrnTestCropImage`/`TrnTestHillshadeImage`/`TrnTestReprojectImage` concrete) owns the shared generate/plot logic once per product type. `TrnTestDataSet.populate()` drives `tasks.py`'s `huey` queue sequentially, in-process -- no longer safe to run from more than one process concurrently against the same dataset folder (the old cross-process claim-file safety is gone; see `trn_dataset.py`'s own module docstring). `populate_via_workers(product_types, retry_failed, limit, workers)` is `populate()`'s real multi-worker equivalent -- same signature/semantics, but enqueues into `tasks.huey_parallel` and manages its own `huey_consumer -k process` subprocess for the call's duration, so `image.generate()` actually runs across `workers` separate OS processes; `status(huey_instance=tasks.huey_parallel)` sees its failures, a plain `status()` doesn't (independent queues). Since `tasks.py`'s entry-level task granularity (`docs/history.md`'s dated entry), `workers` parallelizes across *entries* only -- every requested, still-pending product type for one entry always runs together, sequentially, in the same task/process, never split across two workers. `task_state()`'s stored-huey-result fallback is therefore also entry-level now: it still reports each product type correctly (`image.exists()` is checked first), just with coarser `failed`-attribution if more than one product type in the same entry's task didn't complete. `TrnTestReprojectImage(TrnTestHillshadeImage)` -- `sat_sim` fed by the real WAC crop's own reflectance (`isis_wac.run_cam2map_for_crop`) instead of the Lunaserv/Astropedia basemap, through the exact same `Camera` `hillshade` renders with (byte-identical `(fu,fv,cu,cv)`, deliberate, for future pixel-grid-identical SSIM/diff comparison -- see `camera.solve_corrected_fov`'s docstring); only `raster_path`/`sidecar_json_path`/`render_label`/`_generate_impl` are overridden, everything else (including `_mapprojected_path`) is inherited via dynamic dispatch. Implemented but deliberately kept out of `PRODUCT_TYPES` (`populate()`'s default) -- opt-in only (`product_types=(..., "reproject")`) until wired into a notebook and validated at dataset scale. See `docs/reproject-fov-investigation.md` for `reproject`'s own history. |
| `session.py` | `Session` facade — thin one-line delegators so notebook cells don't repeat `config=...`. Doesn't wrap `trn_dataset.py` -- notebooks call `TrnTestDataSet.create()` directly. |

`notebooks/image_generation.ipynb` reads the checked-in, now-frozen `notebooks/dataset_manifest.csv`
(see `docs/history.md`'s dated entry for why the notebook that used to regenerate it,
`data_set_selection.ipynb`, was removed), populates and reads from the shared `trn_dataset` folder
via `TrnTestDataSet`/`TrnTestEntry`/`TrnTestImage`, and drives the rest of the pipeline end to end —
see `README.md` to run it, and AGENTS.md's "Working conventions" for how to validate changes
against it.

`notebooks/select_datasets.py` is a separate, early-stage/exploratory notebook (`dataset_selection.py`
+ `plotting.py`'s new functions) for picking *multiple* maneuver-free multi-orbit TRN-OD test
datasets, jointly diverse in solar hour angle -- not wired into the demo pipeline above, and doesn't
touch `dataset_manifest.csv`. Does now bridge one selected orbit-sequence into the older EDR-list
world, though: its last two cells call `dataset_selection.resolve_orbit_sequence` on
`selected_datasets.iloc[0]` (one selected window, not all of them, same iterate-fast-on-one
discipline as elsewhere in this project) and then `TrnTestDataSet.create()` on the result, into its
own `orbit_sequence_dataset` folder -- separate from `image_generation.ipynb`'s `trn_dataset`, since
this pipeline isn't the demo's canonical dataset yet. Also writes `orbit_sequence.csv` (the
selected window's own row) alongside `manifest.csv` in that folder, for debugging/provenance. Stops
short of `populate()` -- no rendering from this notebook yet.

## Known open items (resolve as encountered, record findings in `docs/data-sources.md`)

- **Resolved: `TrnTestDataSet`/`TrnTestEntry`/`TrnTestImage` implemented** — see `trn_dataset.py`'s
  row in the Architecture table above for the design.
  `image_generation.ipynb` rewired onto it; `dataset.generate_dataset()` itself is untouched, just
  no longer the notebook-facing generation path. `TrnTestDataSet.populate()` grew a `limit` parameter (not in the original planning session, added
  once a real need for it came up): stops after genuinely new work on `limit` distinct entries, for
  splitting a large dataset's population across multiple separate worker invocations against the
  same folder. `image_generation.ipynb` uses `populate(limit=1)` against the *full* manifest
  (`TrnTestDataSet.create(..., images, ...)`) to keep its own single-image scope — this was
  originally worked around by passing `images.head(1)` instead (before `limit` existed); that
  workaround is gone now. One remaining deliberate deviation from the original planning session's
  literal pseudocode: `TrnTestImage.plot_overlay()` calls `plotting.plot_overlay_toggle` (the auto-blinking
  overlay/base GIF), not the plain `plotting.plot_overlay` that pseudocode named, since the notebook
  had already adopted the toggle version and reverting it would be a real UX regression, not a
  neutral relocation. `reproject` remains unimplemented (reserved folder only), as designed.
- **Resolved (on `feature/reproject` branch, not yet merged to `main`): `reproject` implemented.**
  `sat_sim` fed by the real WAC crop instead of the Lunaserv basemap, through the same camera as
  `hillshade`. The synthetic-camera FOV bug found along the way (the naive symmetric FOV didn't
  fully fit inside the real crop's own footprint) is now folded directly into `camera.build_camera()`
  itself (`solve_corrected_fov`), not a separate `reproject`-only camera variant -- decided after
  the user's own requirement that `hillshade`/`reproject` stay pixel-grid-identical (for future
  SSIM/diff-style comparison between them), which only a shared, corrected `Camera` can guarantee;
  `crop` (real source data) is unaffected and naturally larger, providing the margin `reproject`
  needs. Validated unchanged across 4 real candidates spanning 38.5°N to -67.5°S (~100% valid-pixel
  coverage), then live end-to-end through the real `TrnTestReprojectImage(TrnTestHillshadeImage)`
  class and the flagship `image_generation.ipynb` notebook -- which also surfaced and fixed a real,
  separate regression in `tie_points.die5_points`'s point-placement anchoring (see `tie_points.py`'s
  row above). A separate boresight-bias tangent raised along the way (modeling the *existing*
  boresight correction as a `cv` bias instead of a rotation) is still open, not started -- see
  `docs/reproject-fov-investigation.md` for the full trail, including what's left before merging to
  `main` (notebook wiring, more validation at dataset scale).

  **Resolved (2026-08-21): `reproject` is now permanently wired into `notebooks/image_generation.py`**
  as Phase 8 -- mirrors Phase 5's own A/B geometry checks (raw quality vs. basemap, then a
  `mapproject` overlay) against `entry.reproject`, reusing Phase 4's `tie_point_results` and Phase
  5's crater layer as-is (both already type-agnostic across `TrnTestImage` subclasses), plus a
  valid-pixel-fraction print as the direct, permanent answer to this investigation's own coverage
  question. Phase 2's `dataset.truncate`/`populate` calls now pass `product_types=("crop",
  "hillshade", "reproject")` explicitly to include it -- still opt-in, `trn_dataset.PRODUCT_TYPES`
  itself is unchanged (just `crop`+`hillshade`). **Still open**: only validated end-to-end on this
  one entry (`M1327210646CE`) through the real class and notebook -- dataset-scale validation across
  the rest of the manifest remains a separate follow-up, not done in this pass.

  **The FOV correction was briefly anisotropic (`fu != fv`), then reverted back to isotropic.** The
  anisotropic version (kept a bit more of the real crop's margin) surfaced a small, real, never-fully-
  explained residual (~1-8px, <0.6% of the footprint, constant not growing with distance) between the
  CSM and Pinhole `mapproject` reprojections of the same corrected camera -- confirmed specific to the
  asymmetric case (a symmetric `fu=fv` camera matches exactly, 0px) and, on a later pass, confirmed
  *invariant* to how the anisotropy is encoded across the CSM state's fields (three different but
  mathematically-equivalent encodings all gave the identical residual), ruling out an encoding bug on
  this project's side -- root cause is some deeper compiled-`usgscsm` quirk with anisotropic Frame
  models, not fixable without its source. Combined with two *other* real bugs the anisotropy caused
  along the way (`cam_gen` silently averaging `fu`/`fv` into one isotropic `m_focalLength`, and the
  `die5_points` anchoring regression above) and the concern that other downstream CSM/ISIS consumers
  might hit the same kind of friction, the user's own call was to revert: `solve_corrected_fov` now
  solves `fu`/`fv` exactly as before but collapses them to one shared, isotropic
  `f = max(fu, fv)` rather than keeping them separate. Live-validated: 100.0% coverage on all 4
  candidates (actually improving one from 99.83%), at the cost of a ~4-6% smaller cross-track
  footprint (along-track was already the tighter constraint on every candidate, so along-track extent
  is essentially unaffected); the CSM/Pinhole residual is now exactly 0px again, confirmed live.
  `render._correct_csm_focal_length_anisotropy` (and its dedicated tests) are deleted -- dead code
  once `fu == fv` always. See `docs/reproject-fov-investigation.md`'s "RESOLVED: reverted to an
  isotropic FOV" section for the full trail, including the anisotropic derivation kept for rationale.
- Whether `--save-as-csm` state JSON is an acceptable stand-in for a literal ISD file for whatever
  comes after this demo.
- Confirm the lunar frame kernel defining `MOON_ME` loads correctly so SPICE can output that frame
  directly; sanity-check against the known GLD100/LOLA convention.
- **Resolved, with a corrected premise: the "missing CK kernel" pointing discrepancy doesn't
  actually reproduce.** Originally diagnosed while validating Phase 6B's `cam2map` switch: at the
  exact same instant, this project's own SPICE-based pointing (`camera.camera_pose_moon_me`) and
  ISIS's own camera model (via `spiceinit web=yes`) appeared to disagree by ~11-13km, traced to a
  second CK kernel (`moc42r_*.bc`, spacecraft bus attitude) that ISIS furnishes alongside the usual
  `lrolc_*` one but that `spice_kernels.py`'s `WAC_CK_PREFIXES` never fetched. Built
  `spice_kernels.select_isis_wac_ck_kernels`/`isis_wac.resolve_wac_ck_kernels` to fetch exactly the
  kernel set a real `spiceinit` run resolves (rather than reimplementing USGS's own kernel-selection
  algorithm — see `docs/data-sources.md`). **Direct re-verification then found the original premise
  didn't hold**: comparing our SPICE computation against real `campt` output at several independent
  points across the frame showed **zero** measurable pointing discrepancy, with or without `moc42r`
  furnished — and separately, `moc42r` turns out to have **no effect at all** on
  `camera.camera_pose_moon_me`'s computed pointing, because plain SPICE frame resolution for
  `LRO_LROCWAC_VIS` (-85620) depends entirely on `lrolc`'s own direct CK segments for that frame ID,
  never on the bus-attitude (-85000) kernel `moc42r`/`lrosc` provide (confirmed via a real
  `SPICE(NOFRAMECONNECT)` failure when `lrolc` is omitted even with `moc42r` present). The true cause
  of the originally-observed ~11-13km number was never pinned down — most likely conflated with the
  *other*, since-fixed bug found in the same investigation (`cam2map`'s `WARPALGORITHM=AUTOMATIC`
  striping issue, see the "Resolved" item below). The ISIS-kernel-matching mechanism was kept anyway
  (`TrntestConfig.wac_ck_source`, default `"isis_resolved"`) for its own independent value — it makes
  our furnished kernel set match ISIS's real resolution by construction rather than a hand-picked
  prefix list, which is more principled/future-proof even though it isn't fixing a live bug. The
  deprecated NAIF-metakernel path (`select_naif_wac_ck_kernels`) is confirmed numerically equivalent
  and kept for reference/comparison — and is deliberately still used (forced via
  `wac_ck_source="naif_metakernel"`) in `dataset.evaluate_candidate_image`'s per-candidate sweep,
  since `isis_resolved`'s real-`spiceinit`-per-`edr_product` cost is fine for the handful of
  deliberate final camera-pose calls elsewhere but was a genuine, confirmed O(candidates) performance
  regression there (>100 candidates each triggering their own uncached ISIS pipeline run). See
  `docs/history.md`'s dated entry for the full investigation,
  including the real, decisive empirical tests that overturned the original diagnosis.
- **Resolved**: Phase 6A's real WAC crop appeared systematically misaligned from its own tie points
  (real features consistently south of their matching marker). Root cause: `tie_points.py`'s crop-
  side pixel projection (`project_ground_to_crop_pixel`, now deprecated) was a hand-rolled SPICE
  frame-index bisection, entirely decoupled from whatever pipeline actually produced the crop's real
  pixels -- confirmed live, on this project's real default candidate, to disagree with the crop's
  actual embedded camera model by ~92-96px (out of 994 total lines, ~10%, along-track), and 2 of the
  5 SPICE-chosen die5 points weren't even visible to the real camera at all. Fixed by switching the
  crop-side projection to a genuine ISIS `campt` ground-to-image query (`tie_points.
  resolve_crop_pixels`/`isis_wac.ground_to_image_pixel`) against the real, already-produced crop
  cube. Generalized the camera-model choice into `isis_wac.resolve_ground_to_image_model`, following
  the same resolution order 6B's `cam2map` switch already established: try a CSM ISD sidecar first,
  fall back to the crop's native, SPICE-embedded model only if the ISD resolves to a Pushframe
  sensor (the class `usgscsm`'s `groundToImage` is known unreliable for) -- derived from a real ISD
  each call, not hardcoded, so it stays correct if this pipeline is ever pointed at a non-Pushframe
  instrument. Point *selection* (`select_tie_points`, still SPICE-approximate) is unaffected; a
  selected point the real camera doesn't see is now dropped with a warning rather than crashing the
  notebook. Phase 5's synthetic-image tie points are untouched -- that projection is already exact
  (the same fixed pose that rendered the image), not an approximation of some other camera model, so
  there was no discrepancy to fix there. See `docs/history.md`'s dated entry for the full
  investigation, including the real pixel-offset numbers and why Phase 5 was deliberately left out
  of this pass.
- **Resolved**: the item just above's "Phase 5 was deliberately left out" turned out to have its own
  real bug, found while debugging why the WAC crop's own real map-projected footprint didn't roughly
  match the synthetic render's footprint (as it should, both being TRN candidates for the same real
  ground area) -- they disagreed by ~11-15% in extent, and the exact "center" point by ~6-12km.
  Root cause: `build_camera()`'s synthetic-camera boresight trusted `spice.pxform`'s raw `[0,0,1]`
  directly, but that's confirmed measurably not WAC-VIS's real optical boresight (~5-6 degree real,
  roughly frame-constant angular offset -- not a timing or line-selection artifact, and not
  reproducible from any SPICE-visible kernel data, including the IAK). A first fix attempt (a
  constant correction rotation, empirically fit via a Wahba/Kabsch cross-check) was fully built,
  tested, and then found live-invalid: the correction came out ~identity by construction, since that
  same Wahba fit had already proven the *attitude* (rotation matrix) exactly correct -- the real bug
  was never the rotation, it was that `[0,0,1]` isn't any real pixel's actual look direction in a way
  a rotation can fix without being a no-op. Reverted cleanly. The real fix: `build_camera()` now runs
  the real WAC pipeline (`isis_wac.run_pipeline`, refactored to take `flip: bool` directly instead of
  a `Camera`, to break the resulting circular data dependency, and made idempotent so it's safe to
  call again later, e.g. by Phase 6's own explicit call) and re-aims the boresight (`camera.
  look_at_rotation`) at the real, ISIS-determined ground point at the crop's true center pixel
  (`isis_wac.ground_point_at_pixel`) -- camera *position* is untouched, already proven exact. A real
  ~10-20s cost added to `build_camera()`, traded deliberately for actual accuracy. Live-validated:
  0.000km residual on two different real candidates; Phase 5A's synthetic-vs-basemap alignment
  visibly excellent post-fix. See `docs/history.md`'s dated entry for the full investigation,
  including exactly why the first (reverted) fix attempt couldn't have worked.
- **Resolved**: the stripe/crosshatch artifact reported in the synthetic render (worse in
  darker/shadowed areas, sometimes crosshatched). Root cause turned out to be **Lunaserv's DTM WMS
  layer itself**, not the float32-quantization theory this item originally proposed (tested directly
  and ruled out) -- a real, axis-aligned periodic artifact confirmed baked into Lunaserv's own native
  DTM tile via FFT/periodicity analysis, present regardless of requested resolution, CRS, or
  resampling kernel, and not fixable client-side (the server exposes no resampling control and no
  backing-store metadata). Fixed by switching the live default DEM source to USGS Astropedia's
  flat-file GLD100 distribution instead (confirmed, via the same FFT diagnostic and direct user
  inspection of a real reprojected render, to have none of Lunaserv's artifact). See
  `docs/data-sources/astropedia-gld100.md` and `docs/history.md`'s dated entry
  for the full investigation -- including several dead ends (a notch filter, a native-ppd sweep, a
  GDAL approximate-transformer tolerance check) worth reading before re-deriving them from scratch.
- **Open, future enhancement: Astropedia's GLD100 only covers ±79° latitude** (`lunaserv.
  ASTROPEDIA_MAX_ABS_LATITUDE_DEG`) -- `fetch_dem_and_ortho` raises rather than silently falling back
  to the deprecated, artifact-affected Lunaserv path for any camera footprint that needs data beyond
  it, so a catalog-driven selection landing near either pole currently just fails outright for that
  image. NASA's VIRA project (`github.com/nasa/vira`, `scripts/download_dems.sh`) points at genuinely
  higher-resolution *polar* DEM data for exactly this gap -- real LOLA-derived polar mosaics from
  `pgda.gsfc.nasa.gov`/`imbrium.mit.edu`, down to 5 m/px near 87°S (LOLA ground tracks converge near
  the poles, giving denser altimetry there than equatorial GLD100 -- ironically *better* resolution
  right where Astropedia's coverage ends, not worse). Not implemented -- would need its own flat-file
  fetch/caching (a different host/product per polar region, `curl`-based like
  `cache.fetch_astropedia_gld100`, likely a comparable one-time size) and a coverage-based dispatch
  in `fetch_dem_and_ortho` (Astropedia inside ±79°, a polar LOLA product beyond it) rather than the
  current hard latitude guard.
- **Resolved**: Lunaserv's native geographic projection is fine for `sat_sim`'s forward render, but
  turned out to break the `mapproject --ref-map` round-trip (anisotropic degree-pixels away from the
  equator, not preserved by `--ref-map`) — fixed by requesting a per-camera local Orthographic CRS
  directly from Lunaserv (`IAU2000:30166`, still a single WMS fetch, no separate `gdalwarp` step).
  See `docs/data-sources/lunaserv-wms.md` and `docs/history.md`'s dated entry.
- **Resolved**: Phase 3's displayed GLD100 DEM had small but real nodata gaps (~-3e38 sentinel
  values once passed through `dem_mosaic`'s hole-filling) right at the map's corners — the source
  Astropedia AOI fetch (a degree-space bbox, independently padded around the raw footprint) and the
  destination Orthographic working grid (a separate, also independently padded meters-space bbox)
  were never guaranteed to cover each other: a square's diagonal corners sit ~41% farther from center
  than its edge midpoints, so the degree-space padding — confirmed live, even at the generous default
  30% — undershot the destination grid's own corners by up to ~5km in some directions, regardless of
  how much padding was applied, since the padding was never actually being checked against what it
  needed to cover. Fixed by deriving the Astropedia fetch bbox directly from the destination grid's
  own bbox (`lunaserv.astropedia_coverage_bbox_deg`, via `rasterio.warp.transform_bounds`) instead of
  computing it independently — now structurally impossible for the two to disagree, since there's
  only one padded bbox. Live-validated: zero nodata pixels anywhere in the fetched DEM (was nonzero
  at all four corners). See `docs/history.md`'s dated entry.
- **Resolved**: whether a real WAC swath can be reprojected onto the DEM via a genuine ISIS/CSM
  camera model (`mapproject`). The previously-reported "severe framelet-boundary striping"
  (`docs/history.md` Phase 12, `docs/external-tools.md`'s "ISIS Pushframe pipeline" section) turned out to be
  mostly a methodological artifact, not a fundamental CSM Pushframe limitation: mapprojecting a lone
  even/odd parity cube (each only ~50% populated — WAC alternates which nominal frame slot gets real
  data) leaves `mapproject` to resample across that sparsity, producing the smearing. Mapprojecting
  the properly interleaved *stitched* cube instead resolves the vast majority of it (31% valid
  coverage/no recognizable terrain → 81% valid coverage/real craters throughout, same real product).
  `isis_wac.py` now implements the full chain: EDR fetch → `lrowac2isis` → `spiceinit web=yes` →
  `lrowaccal` → `framestitch` → `crop_for_camera` → `isd_generate` → `mapproject` (via
  `render.run_mapproject_image`, shared with the synthetic render's own mapproject step). Given a
  genuine, validated camera model now exists end-to-end, `isis_wac.py` replaced `wac.py`'s manual
  framelet-stacking as the demo notebook's real-WAC comparison method entirely (`wac.py` itself is
  untouched, still covered by its own unit tests, just no longer used by either notebook — see
  `docs/history.md`'s dated entry for the rationale). `notebooks/image_generation.py`'s Phase
  5A/5B/6A/6B demonstrate the result: Phase 5 is the synthetic render's own geometry check (5A raw
  quality, 5B `mapproject` overlay), Phase 6 is the real WAC crop's (6A raw quality, 6B `mapproject`
  overlay) — 5B/6B share `plotting.plot_overlay` with no special-casing, since both overlays are
  already cropped to just the real footprint being compared by the time they reach it. See
  `docs/external-tools.md`'s "ISIS Pushframe pipeline" section and `docs/history.md`'s dated entries for the
  full investigation, including the ISD ephemeris-time bug found and fixed when reprojecting a
  cropped (not full-swath) cube; `notebooks/wac_isis.py` remains the step-by-step version for
  isolating pipeline stages.
  **Correction: the ISD ephemeris-time fix above was a false positive** (0.999 correlation on a
  too-small sample), caught by the user's manual visual inspection of the actual notebook output,
  not by this project's own automated checks. Deeper investigation traced the real cause to a bug
  in `usgscsm`'s `UsgsAstroPushFrameSensorModel::groundToImage` itself (an unbracketed secant search
  over framelet index, unreliable for Pushframe images generally and badly so for a short crop) --
  not fixable via ISD authoring at all. Phase 6B now uses ISIS's own native camera model
  (`cam2map`) instead of ASP `mapproject`/CSM for the real WAC crop; `run_isd_generate`/
  `run_mapproject` are kept in `isis_wac.py` for reference but no longer used by the notebook. See
  `docs/history.md`'s dated entry for the full investigation.
- `geopandas` (added alongside `rioxarray` for `plotting.plot_overlay`) now has a concrete caller:
  `plot_overlay(show_overlay_outline=True)` traces the overlay raster's real (non-NaN) footprint and
  draws it as a vector boundary.
  **Done**: `plot_overlay`/`plot_overlay_toggle`/`TrnTestImage.plot_overlay` (covering both 5B and
  6B) now take a generic `layers: list[plotting.OverlayLayer] | None` parameter — each layer is a
  `geoseries` + style (`color`/`linewidth`/`alpha`/`fill`), drawn via `.boundary.plot(...)` or
  `.plot(...)`. Chosen over per-layer-type named parameters (an earlier version added
  `crater_geoseries`/`crater_outline_color` directly) specifically because that approach doesn't
  scale — one layer type already cost 2 params × 4 function signatures
  (`plot_overlay`/`plot_overlay_toggle`/`_render_overlay_figure`/`_render_overlay_frame`), and a
  `layers` list absorbs any number of future layer types with zero further signature changes. The
  existing footprint outline (`outline_geoseries`/`overlay_outline_color`) deliberately stayed a
  separate, dedicated, always-present parameter rather than folding into `layers` — it's the actual
  geometry-validation reference the Phase 5/6 comparison exists to show, not an optional annotation.
  Verified with hand-built toy `GeoSeries` layers (single-layer, multi-layer, filled) through both
  `plot_overlay` and `plot_overlay_toggle`; full test suite (156 tests) and lint still pass.
  **Done**: source URL confirmed (found by the user navigating the current live catalog page
  directly — every URL findable via search engines or third-party docs 404s live on
  `astrogeology.usgs.gov` now, a real site reorganization, not bot-protection; see
  `docs/data-sources/robbins-craters.md` for the full investigation) and
  fetch/cache wired up: `config.robbins_craters_url`/`cache.fetch_robbins_craters` (plain
  `cache.cached_get`, ~92MB zip, not `fetch_astropedia_gld100`'s special resumable-curl path).
  Downloaded and inspected the real file: a PDS4 bundle whose only data is one CSV, 1,296,796 rows
  (D≥1km), POINT-only geometry (`LAT_CIRC_IMG`/`LON_CIRC_IMG` center, `DIAM_CIRC_IMG` in km — not
  radians/differently-named as a third-party library's docs implied, both directly confirmed
  against the real downloaded data) with `DIAM_ELLI_MAJOR_IMG`/`DIAM_ELLI_MINOR_IMG`/
  `DIAM_ELLI_ANGLE_IMG` as separate attribute columns — confirms the ellipse polygon must be
  constructed at render time, not read off the shelf. Full field list/CRS/units in
  `docs/data-sources.md`.
  **Done**: `src/trntest/craters.py` — `ensure_geopackage()` converts the raw CSV (no native
  spatial index) to a GeoPackage once (point geometry at each crater's ellipse-fit center,
  `LAT_ELLI_IMG`/`LON_ELLI_IMG`; ~10s real conversion time, 374MB output, cached alongside the raw
  zip), giving GDAL's own `rtree` spatial index for free. `query_craters_in_bbox()` does the
  two-stage filter this section already planned: `geopandas.read_file(..., bbox=...)` pushdown (so
  the ~1.3M-row database is never materialized in Python — confirmed live: querying a real ~10°×10°
  box took 0.05s, 1395 rows), then `.cx[]` for the exact box. `crater_overlay_layer(raster_path,
  config)` is the single entry point notebook cells will call: derives the query AOI from
  `raster_path`'s own real bounds (`rasterio.warp.transform_bounds`, then normalized from the
  standard -180..180 output to this project's 0-360 Positive-East convention — a real bug caught by
  a live smoke test, not just reasoned about: the unnormalized bbox silently matched zero rows),
  pads it (`lunaserv.pad_bbox`, reused rather than reinvented), queries, and builds each surviving
  crater's real ellipse polygon in `raster_path`'s own local-meters CRS via
  `shapely.affinity.scale`/`.rotate`/`.translate` on a unit circle (reprojecting the *center point*
  first, then building the ellipse exactly in isotropic meters — no projection distortion, unlike
  building the ellipse in geographic degrees first). Returns a ready-to-use `plotting.OverlayLayer`,
  or `None` if nothing's in view. Live-validated against the real ~1.3M-row database (a 100km×100km
  synthetic AOI centered on a real crater found 152 ellipses, sane sizes/positions) and covered by
  7 real unit tests (`tests/test_craters.py`, no network — synthetic zip/CSV/raster fixtures);
  168 tests total pass, lint clean.
  **Done**: notebook wiring (`notebooks/image_generation.py`) — Phase 5B/6B each call
  `craters.crater_overlay_layer(dem_ortho_result.ortho, entry.per_image_config)` once (both share
  the same base raster/CRS) and pass the result into `plot_overlay`'s `layers=[...]`.
  **Resolved**: `DIAM_ELLI_ANGLE_IMG`'s rotation reference (which axis, which direction) wasn't
  documented in the PDS4 label, so orientation (unlike size/position) was unconfirmed until this
  wiring made a real visual cross-check possible — a live run over a real ~250km×250km AOI (4,633
  craters, unfiltered) showed ellipses landing tightly on real crater rims throughout the hillshade
  basemap, including visibly elongated (non-circular) craters matching their ellipse's long axis,
  not perpendicular to it — confirms `_ellipse_polygon`'s current interpretation is correct as-is,
  no code change needed (see `craters._ellipse_polygon`'s docstring for the interpretation itself).
  **Done**: `crater_overlay_layer`'s `min_major_km`/`min_arc_img` filter params (the unfiltered
  4,633-crater view was too visually dense to read as an annotation) — `notebooks/image_generation.py`
  currently uses `min_major_km=9.0, min_arc_img=0.75` (~80 craters). `ARC_IMG` (fraction of a
  crater's rim actually traceable/used in its ellipse fit) is this database's only real proxy for a
  quality/"grade" field — confirmed against the real PDS4 bundle's own archive-description PDF that
  no dedicated degradation/sharpness field exists at all (the database's stated purpose is a
  position/size census for crater-count studies, not per-crater freshness grading) — but `ARC_IMG`
  is confounded with size (41% of *all* craters have `ARC_IMG==1.0` vs. 2.5% of craters ≥20km major
  axis), so it's only a meaningful filter *within* a size band, not applied to the whole database;
  see `crater_overlay_layer`'s own docstring. Overlay styling (`OverlayLayer.color`/`linewidth`/
  `linestyle`, the last a new field — any matplotlib linestyle, incl. custom dash tuples) was also
  tuned for legibility: a solid full-opacity line was found to obscure the very rim it's meant to
  help verify, so the current notebook uses a sparse dashed line (`linestyle=(0, (1, 6))`) in a
  light, warm color (`#ffddbb`) instead of fading `alpha`/`linewidth` (which made alignment *harder*
  to judge, not easier).
- **New (2026-08-23): `src/trntest/crater_depth.py`** — first step toward grading crater sharpness
  (`ARC_IMG`, per the entry just above, isn't a real freshness proxy; this database has no
  degradation field at all). A simplified reimplementation of Breton et al. 2019's depth method
  (60th-percentile rim-ring elevation minus 3rd-percentile interior elevation, off a DEM + crater
  polygon) against this project's own local GLD100 DEM and Robbins ellipse polygons — the original
  authors' own reference script's per-pixel area-weighting collapses to a plain `np.percentile` on
  this project's isotropic-meters grid, where every "inside" pixel already covers the same real
  area. `crater_depths_for_footprint` stores a `None` depth (kept as a row, not dropped) for any
  crater too close to GLD100's own ±79 deg coverage limit, rather than switching to a coarser,
  interpolation-artifact-prone global DEM just for polar coverage — see
  `docs/crater-grading.md` for the full
  DEM-source comparison. Module + synthetic-fixture tests (`tests/test_crater_depth.py`) plus one
  real-data throughput test against a live GLD100 tile (`tests/test_crater_depth_gld100_tile.py`,
  `@pytest.mark.heavy`) — 129/129 real, fully-contained craters in a real equatorial 512x512px tile
  got a valid depth, ~19.4ms mean per crater, extrapolating to a rough ~6.9 hour single-threaded
  estimate for the whole non-polar database (see `docs/crater-grading.md` for the full caveats on
  that number). Not yet wired into `image_generation.ipynb` or related to an actual sharpness grade; both
  are real next steps.

  **Done (2026-08-26): `crater_depth.stoffler_fresh_depth_km`** — the reference-depth half of an
  actual sharpness grade (Stoffler et al. 2006, *Reviews in Mineralogy and Geochemistry* 60(1),
  519-596, DOI `10.2138/rmg.2006.60.05`): a fresh crater's expected depth for a given diameter, the
  classic two-regime lunar depth-diameter relation (simple craters steeper, complex craters
  shallower, crossing at ~10.58 km). Implemented as `min(simple_regime, complex_regime)` rather than
  an explicit branch on the crossover -- provably identical to the textbook piecewise form for any
  `diameter_km > 0` (the simple-crater curve is the smaller of the two below the crossover, the
  complex-crater curve is smaller above it, and they cross exactly once), so the elementwise minimum
  picks the right regime on both sides with no branch and is exactly continuous at the crossover by
  construction. 4 new tests confirm both regimes, exact continuity, and vectorization.

  **Done (2026-08-26): `src/trntest/crater_depth_batch.py`** — the whole-database precompute
  `crater_depth.py`'s own docstring already called out as a real next step, tiled for cache
  coherence rather than a naive independent-per-crater loop. See `docs/crater-grading.md` for the
  full design (ownership-by-center vs.
  padded-raster-extent, why a direct read off GLD100's own raw grid would have been wrong, a real
  `nodata`-tag bug caught and fixed along the way) and real measured timing (~13.6 hours for the
  whole grid single-threaded -- slower than the earlier naive ~6.9-hour estimate, since that number
  never accounted for real per-tile reprojection overhead). `grade_database_via_workers` is the real
  multi-worker path, mirroring `trn_dataset.TrnTestDataSet.populate_via_workers`'s own established
  pattern (`tasks.start_consumer`/`stop_consumer`, generalized to take a `huey_module` argument
  rather than duplicate that subprocess-management code for a second task domain) -- live-validated
  end to end against real data. A third-party dataset that looked like it might shortcut this whole
  precompute (`huggingface.co/datasets/juliensimon/lunar-craters-robbins`, Robbins craters plus a
  pre-computed depth column) was investigated and rejected -- see `docs/crater-grading.md` for the
  concrete red flags found on direct inspection. Not yet combined into an actual sharpness score
  (deliberately: `stoffler_fresh_depth_km` and the measured-depth precompute are kept independent,
  so a future change to the grade formula itself doesn't require re-running the multi-hour DEM pass)
  or wired into any notebook.

  **Done (2026-08-26): the actual sharpness score, a real review notebook, and the pieces needed to
  build it.** `crater_depth.sharpness_ratio(depth_m, diameter_km)` -- the user's own chosen formula,
  measured depth over `stoffler_fresh_depth_km` (units matched, ~1.0 for a crater as deep as a fresh
  one of its size "should" be). `crater_depth_batch.consolidate_graded_geopackage` now computes and
  stores it as a `sharpness` column (cheap, safe to recompute on every consolidation, unlike the
  depth measurement itself). `craters.query_craters_for_raster`'s bbox-deriving logic is factored out
  as `craters.raster_bbox_deg` (unpadded), shared with a new `crater_depth_batch.grade_footprint` --
  grades just the tiles touching one candidate's real footprint (`tiles_covering_bbox`, snapped to
  the same grid `iter_tile_origins` defines) rather than the whole database, for reviewing/validating
  grading against a single image without the multi-hour full-database cost; writes into the same
  per-tile CSVs `grade_database`/`grade_database_via_workers` use, so it's fully compatible with a
  later full run. `notebooks/crater_sharpness_review.py`/`.ipynb` (new, minimal setup matching
  `hapke_hillshade.ipynb`'s pattern -- reuses `image_generation.ipynb`'s Phase 1-2 but skips
  `dataset.populate()` entirely, since only `entry.dem_ortho_result` is needed) grades + consolidates
  the default candidate's footprint, then shows two real results: a sharpness-colored crater overlay
  on the same hillshade basemap/sparse-dashed style Phase 5B/6B use, and a log-log depth-vs-diameter
  2D histogram with the Stoffler curve overlaid. **Real, live result**: the bulk of the measured
  population clusters at or below the reference curve at small diameters -- physically sensible (most
  real craters in a random sample are somewhat degraded, not freshly formed), a real positive signal
  for both the depth measurement and the reference formula, not just a working plot. The histogram
  needed log-log axes/bins after a first linear attempt was genuinely unreadable (one bin near the
  smallest diameters held the vast majority of craters) -- also the standard way this kind of
  power-law crater data is presented in the literature, not just a fix for this one plot. The overlay
  needed `min_major_km` filtering (mirroring Phase 5B/6B's own convention) after the unfiltered
  population (thousands of small craters over one footprint) made the sparse-dashed ellipses collapse
  into an unreadable cloud.
- **Resolved**: `select_tie_points`'s die5 point-selection footprint's high drop rate under
  `resolve_crop_pixels` (2-3 of 5 points on real candidates, the real camera not seeing them at all).
  Root cause was two-layered: (1) `crop_footprint_corners` was still the deprecated SPICE
  approximation (now `_crop_footprint_corners_spice_approx`), disagreeing with the real crop's
  footprint by ~11-15% in extent — fixed by `crop_footprint_corners_for_camera` querying the real
  crop directly via `campt` image-to-ground (`isis_wac.ground_point_at_pixel`), cheap by construction
  since `camera.build_camera()` already runs the real pipeline (see the item above); (2) even after
  that, points still dropped — traced to a real, confirmed numerical instability in `campt`'s own
  ground-to-image solve within ~5-10px of a *cropped* cube's edge (image-to-ground succeeds there,
  but a ground-to-image query at that exact resulting lon/lat then fails) — fixed with a
  `_CROP_EDGE_MARGIN_PX` inset (20px) when querying the crop's own corners. Live-validated: 5 of 5
  tie points now resolve on the real default candidate (was 2-3 of 5), confirmed visually in Phase
  6A. A separate, more extreme near-polar test candidate (~-81 to -83° latitude) still dropped some
  points — the axis-aligned lon/lat bounding-box approximation this module's whole point-selection
  approach relied on broke down that close to a pole (severe longitude convergence). Originally
  accepted as a known limitation rather than fixed (tie points being a debug/QA overlay, not a
  correctness-critical output); **since fixed** (see `docs/history.md`'s later dated entry):
  `select_tie_points` now does the box-inscribing/intersection/placement geometry in a shared local
  Orthographic frame (meters), not raw lon/lat degrees — `inscribed_bbox`/`intersect_bbox`/
  `die5_points` are pure planar-geometry functions, so this only changes what coordinates they're
  fed, via `rasterio.warp.transform` (not a hand-rolled projection). With point *selection* now
  trustworthy near the poles too, `resolve_crop_pixels` no longer degrades gracefully — it raises
  immediately on any unresolved point instead of dropping it with a warning, since a failure now
  means something is fundamentally wrong rather than an expected edge case. See `docs/history.md`'s
  dated entries.
- **Resolved (partially — see "Open" below)**: 6B's real-WAC/basemap overlay is visibly not
  perfectly aligned (small, "not huge" per direct user observation). Investigated whether an ASP
  bundle-adjustment tool could correct the SPICE-derived pose via feature-matching against the
  basemap. **Found a real, unrelated bug, substantially (not fully) improved**:
  `isis_wac.run_cam2map_for_crop`'s own `PATCHSIZE=4` (chosen in an earlier phase using only an
  aggregate crop-vs-full correlation number) introduces a real, visible striping artifact —
  confirmed via a direct `PATCHSIZE` sweep (1/2/4/8/14) at native resolution; switched to
  `PATCHSIZE=1` (no coverage trade-off, ~6s/crop runtime cost). A high-pass quantitative check found
  only a modest ~2.4% reduction in fine-scale energy versus the old default, and a faint residual
  remains visible on close inspection — judged consistent with genuine, modest photometric
  discontinuities at framelet transitions (inherent to any patch-based warp), not the more severe
  missing/bad-data-looking pattern `PATCHSIZE=4` showed, and a reasonable stopping point (diminishing
  returns past here). Distinct from, and not fixed by, the already-known framestitch dead-column
  artifact — confirmed by patching that artifact out of a real cube copy (via GDAL's ISIS3 `rw+`
  write support) and re-running `cam2map`: zero visible change. See `docs/history.md`'s dated entry.
  **Open**: the actual camera-pose-correction question itself is unresolved. Research trail: ASP's
  standard `bundle_adjust`/`pc_align`/`image_align` recipe applies corrections via ASP's own
  `mapproject`, which is the CSM/Pushframe route already abandoned elsewhere in this pipeline for a
  confirmed severe bug — a dead end. ISIS's own `jigsaw` + `findfeatures` (space resection against a
  basemap, staying camera-model-native) is architecturally sound and USGS-documented practice for
  single images, but a real `findfeatures` spike found its control-point-construction step discards
  every match regardless of `TARGET=`/`GEOMTYPE=` settings — likely because the basemap is a plain
  GDAL-exported GeoTIFF, not something ISIS itself map-projected, so it lacks whatever ISIS-native
  geometry metadata that step needs (not yet confirmed or fixed).
  **Implemented, not yet wired into the main pipeline**: a from-scratch 2D approach sidestepping
  both blockers — `src/trntest/pose_alignment.py` feature-matches the already
  map-projected WAC crop directly against the basemap (no camera model, no ISIS control network),
  fits a similarity transform (translation+rotation+uniform scale, deliberately the simplest
  plausible model, not asserted as physically correct — see the module's own docstring) via RANSAC,
  and applies it to the WAC raster's own georeferencing so it drops into the existing
  `plotting.plot_overlay_toggle` unmodified. `notebooks/pose_alignment_spike.py` exercises the whole
  pipeline against the current default candidate — a real signal, not yet validated enough to wire
  into the main pipeline. **Follow-up**: the map-projected WAC crop being matched against turned out
  to be substantially oversampled — `cam2map`'s `PIXRES=map` forces its output onto the basemap's
  ~100 m/px working grid, but a direct measurement (`cam2map PIXRES=camera`) found the crop's own
  real native resolution is ~184 m/px (~1.8x coarser), specific to the WAC side (the basemap ortho is
  genuinely ~100 m/px native). Added `pose_alignment.native_wac_gsd_m`/`downsample_to_gsd`
  (area-averaging) to match at native resolution instead of the interpolated grid; live result:
  matches more than doubled (106 → 259), inliers nearly doubled (53 → 91), and inlier residual
  tightened from 1.45px to 0.84px once measured in native pixels.
  **Concluded (for this exercise): the correspondences are real, not RANSAC noise.** With enough
  inliers to support it, added `fit_affine_correction`/`fit_homography_correction`
  (`apply_homography_correction` for the latter, since a homography isn't an `affine.Affine`) and
  compared all three models' blink overlays side by side. Direct user visual inspection of the
  homography overlay: "beautiful... real benefit to the higher-order model here, not just noise."
  **User's own conclusion, the deliberate stopping point for this branch of investigation**: this
  validates that real, usable correspondences exist here — the next real step is a proper
  projection-informed alignment (an actual camera-model correction — fixing the SPICE-derived pose
  directly, or the `jigsaw`/`findfeatures` space-resection route above, now with real evidence a
  correction is warranted at all) rather than continuing to refine this 2D homography spike further.
  Not picked up yet. **LightGlue (DISK extractor) added as a second matcher**
  (`pose_alignment.match_features_lightglue`, `~3x` SIFT's match/inlier count on the default
  candidate, 767 vs. 259, at a very slightly looser per-point fit — not a quality win on this
  already-easy candidate, but real headroom for future shadowed/low-texture EDRs SIFT might not
  find enough points on at all; direct user visual confirmation the alignment quality holds).
  See `docs/history.md`'s dated entries (Phases 52–55) for the full trail.
  **Done, with a real, honest negative-ish result (as of 2026-08-19): a projection-aware 3D bundle
  adjustment, now fit against real control points and visually compared.** `src/trntest/
  control_network.py` (done, tested) converts tie points into real ISIS control points. ISIS's own
  `jigsaw` was tried and hit a real, root-caused, unfixable bug in its PushFrame framelet search
  (confirmed via a tautological, mathematically-guaranteed-zero-error control network that still
  produced huge `jigsaw` residuals) -- pivoted to a hand-rolled Python ground-to-image forward
  projection instead (`src/trntest/wac_camera_model.py`), complete end-to-end: the optics chain
  (validated to exact 0.000px agreement with real `campt`), the framelet search
  (`find_framelet_and_project`/`calibrate_et_per_crop_line`, live-validated to 0.00m ground error
  round-tripped through `campt`'s trusted inverse), and the optimizer (`fit_pose_correction`, a
  single frozen 6-DOF correction via `scipy.optimize.least_squares`). Fit against the real 477-point
  control network (from 767 LightGlue matches): residual mean 4.42px -> 3.36px, dominated by a small
  (~0.18deg) camera-frame rotation. `isis_wac.apply_pose_correction_to_crop` bakes the fitted
  correction into a copy of the crop cube's cached pointing (patches the `InstrumentPointing`
  Table's `ConstantRotation` via `tabledump`/`csv2table`, live cross-validated to <=0.015px against
  the forward projector's own prediction) so the existing, unmodified `cam2map`/
  `plotting.plot_overlay_toggle` path reprojects and displays it with no new warp code -- wired into
  `notebooks/pose_alignment_spike.py`, reviewed live by the user. **The real finding**: this only
  closes ~24% of the real gap (residual ~813m -> ~618m at this crop's own ~184m/px native GSD, both
  well above the homography spike's own ~150-165m) -- a single frozen 6-DOF rigid pose correction
  doesn't explain most of the true misalignment. Leading suspect, not yet tested: this pipeline's
  ground truth is still ellipsoid-only (no DEM elevation, see `control_network.py`'s own docstring),
  which would show up as spatially-varying error a rigid pose bias can't capture but a flexible 2D
  homography can silently absorb -- a DEM-aware shape model (`spiceinit shape=user`) is the
  planned next step. **See `docs/wac-jigsaw-investigation.md` for the full technical detail** (exact
  ISIS source citations, every constant's provenance, the serial-number bug and its fix) and
  `docs/external-tools.md`'s `campt`/`tabledump`/`csv2table` entries for the reusable mechanism facts.

  **The leading suspect was it (2026-08-20): the ellipsoid was the real bug, not the camera pose.**
  `isis_wac.run_spiceinit` hardcoded `shape=ellipsoid` for every real-WAC cube in the pipeline --
  confirmed to be the actual root cause of the parallax-like crater-rim misalignment that motivated
  this whole investigation in the first place, not a simplification safe to defer. Switched to
  `shape=user model=<ldem>` against ISIS's own real global lunar shape model
  (`isis_wac.ensure_lunar_shape_model`/`attach_dem_shape_model` -- `sample_lunar_dem_radii_batch` for
  camera-independent elevation sampling), one function changed, everything downstream (the 6B
  `cam2map` panel, `run_isd_generate*`, `control_network.resolve_control_points`'s jigsaw ground
  truth) inherits it with no further code changes. Live result on the flagship demo candidate,
  measured with `pose_alignment.py`'s own feature-matching machinery, **zero camera-pose correction
  applied**: mean raw offset 849m (ellipsoid) -> 124m (DEM), an 85% reduction, down near the
  matcher's own noise floor at this basemap's ~100m/px resolution. Everything on the synthetic/
  basemap side (`render.run_sat_sim`'s raytrace, Lunaserv's own orthorectified imagery) was already
  DEM-correct -- the asymmetry was entirely on the real-WAC side.

  **Camera-pose alignment (`pose_alignment.py`/`wac_camera_model.py`/`control_network.py`) is on the
  back burner, not superseded** -- explicitly the user's own framing, not a downgrade to "no longer
  needed." The DEM fix closes the specific gap that was visible on this candidate, but the capability
  to measure real alignment statistics (feature-matched offset, residual pixel error) independent of
  whether a correction gets applied is worth keeping regardless: there's no guarantee a future WAC
  product's initial SPICE-derived registration will be as accurate as this one turned out to be once
  the shape model was fixed, and this tooling is exactly what would catch that.

- **Open**: the user's requested "error-handling/fallback-consistency" quality audit (deliberately
  split into chunks so a single session wouldn't run out of context) only got through **Chunk A**
  (`tie_points.py` + `isis_wac.py`) before spiraling into a real fix (this file's die5/local-meters
  entry above and `docs/history.md`'s Phase 64) rather than staying a survey. Chunks B-E were never
  started, and there's no record elsewhere of which files/modules they were meant to cover -- next
  session should ask the user to re-scope chunks B-E from scratch (or just re-run the audit idea
  fresh) rather than assume a prior chunking plan still applies.
- **New, first minimal prototype built (2026-08-21): per-image Jupyter/HTML reports.** A
  standalone, one-HTML-page-per-entry report, separate from `image_generation.py`'s long
  hand-curated demo notebook -- `notebooks/report_template.py` (real `{{ name }}` text
  substitution via `trntest.report.render_template`, not papermill's `parameters`-tag mechanism,
  after that turned out unable to support a true one-line `load_entry("<path>", "<id>")` call with
  literal values -- see `docs/proposed-tasks/report-plan.md`'s "Mechanism" section; deliberately compact,
  one-liner cells calling `src/trntest/report.py`, no per-cell markdown headers; not committed as
  a paired notebook, since its `{{ }}` source can't execute standalone) +
  `scripts/generate_report.sh`/`scripts/render_report_template.py` (substitute -> jupytext sync ->
  papermill execute -> nbconvert to HTML, `ExtractOutputPreprocessor` on so figures land as real
  files under `images/` rather than base64-embedded, `In[N]:`/`Out[N]:` prompt gutters suppressed
  but code cells left visible). Deliberately minimal first pass (one raster + a couple of manifest
  fields), hand-run repeatedly against multiple entries, confirmed both the images-as-separate-
  files property and that each entry's own data renders (not a stale default). Growing the
  report's content to match `image_generation.py`'s Phase 5/6/8 comparisons and
  building the multi-entry index page (`<iframe>`-based navigation across entries) are both real,
  not-yet-started follow-ups. See `docs/proposed-tasks/report-plan.md` for the full design.

- **Open (2026-08-22, Phase 71/72) — both the normal-tilt fix and a new Hapke-ratio relighting fix are now permanent and unconditional (no opt-out parameter for either), but their combined effect on the real-WAC-crop match is still unresolved, and empirically worse, not better.** `lunaserv._terrain_photometric_angles`'s DEM-gradient surface normal uses `dem + sphere_sag` (not raw `dem`) as its gradient input, closing the tangent-point-fixed-frame gap Phase 70 found, independently validated against real ISIS `campt` ground truth at the ellipsoid limit (max |diff| ~0.018 deg, `tests/test_lunaserv_campt_validation.py`, a new permanent `@pytest.mark.heavy` test, `usgscsm` now in `docker/Dockerfile`). Separately, `hapke_shade_ortho` relights the ortho texture by H(i,e,g)/H(reference-geometry) instead of a bare rescaled H(i,e,g) (Phase 72) — `config.lunaserv_ortho_layer` was confirmed (via its own PDS4 README) to be ASU's WAC_EMP product, itself photometrically normalized to a fixed i=30/e=0/g=30 geometry via an empirical (Boyd et al. 2012), non-Hapke function, so the old blend wasn't relighting at all. Both were made unconditional on the user's own explicit call ("the need for the correction seems very clear") despite neither being confirmed to improve, and the Hapke-ratio fix being confirmed to *worsen*, the brightness-matched diff for the one candidate tested (8.6853 → 9.2425, measured with the new `plotting.compute_brightness_matched_diff`, a reusable tool built specifically because this project's own historical diff numbers turned out to be irreproducible ad hoc scripts). A follow-up diagnostic (not kept) confirmed real `campt`'s own plain angles stay ellipsoid-normal-based even with a DEM shape model attached, so it can't validate the real DEM-aware case the way `phocube`'s own broken `localincidence` flag (Phase 70) was supposed to — that gap remains genuinely open, and is judged the harder, non-"low-hanging" priority relative to further chasing this regression. Three further live visual observations (a real east-brightening gradient our hillshade underrepresents; an apparent ~10 deg shadow "rotation" confirmed *not* to be a sun-azimuth bug via real `campt` cross-check, 0.006 deg agreement; crater floors reading anomalously bright in the real WAC vs. our hillshade) are recorded but not investigated further. Full rationale in `_terrain_photometric_angles`/`hapke_shade_ortho`'s own docstrings and `docs/history.md`'s Phase 70/71/72 entries. **Phase 73 confirmed `phocube`'s DEM-mode backplanes are a dead end even against this project's own local DEM** (not just the coarse global shape model Phase 70 tried) -- the DEM-aware validation gap has no ISIS-side tool left to try. **Phase 74 ran a genuinely independent cross-check instead: ASP `sfs`, used as a pure forward renderer** (`sfs_validation.py`, `notebooks/sfs_validation.ipynb`) — lands in the same ballpark as the real WAC crop (brightness-matched diff ~0.0061 vs. our own pipeline's ~0.0032-0.0043) but doesn't resolve the regression above. The `sfs` panel's own brightening toward one corner is real, explained geometry (a genuine ~50 deg real phase-angle swing across the frame, mostly cross-track parallax) compounded by a real double-counting bug in `true_albedo_map` (found and fixed same session) and, more significantly, by a structural gap: `sfs`'s own reconstructed CSM camera has no way to represent `along_track_correction`, so its implicit geometry runs measurably hotter (lower phase, stronger opposition surge) than `hapke_shade_ortho`'s own default output in exactly the region that differs most — narrowing what this tool is actually useful for as an ongoing cross-check (see `docs/history.md`'s Phase 74 follow-up for the full mechanism). `TrnTestEntry.dem_ortho_result`'s disk-resume path still has no consistency check between its two independently-cached files (a real, if narrow, bug Phase 74 hit and worked around, not fixed at the source). **Phase 75 found a much cleaner win from the same tool: `sfs`'s Lambertian mode (`--reflectance-type 0`, no emission/phase term at all) can be inverted for `sfs`'s own independent incidence angle with zero Hapke-model dependence — confirmed to match `lunaserv.real_geometry_photometric_angles` to ~0.02 deg mean/~0.5 deg max across a whole candidate's real coverage region, closing the DEM-aware ground-truth gap Phase 70/73 left open.** Now a permanent `@pytest.mark.heavy` test (`tests/test_sfs_validation_lambertian_incidence.py`) validating the full-resolution DEM-aware case, not just campt's ellipsoid-only sparse-point check. Validates incidence only (emission/phase depend on the view vector, which Lambert's law has no term for) — the along-track-correction question from Phase 74 remains open for those. **Phase 76 found and fixed the actual source of that ~0.02/~0.5 deg residual: a second, real approximation gap (`dem` embedded along the tangent point's fixed Up axis instead of each point's own true local radial direction — "relief displacement," exact closed form now used) beyond the sagitta/normal-tilt fixes above.** Reduces to the previous formula exactly at `dem=0` (no regression risk for the ellipsoid-limit `campt` test above, confirmed unchanged at 0.018 deg). Post-fix: mean/max diff against `sfs`'s own incidence dropped to ~0.0005 deg both — essentially exact agreement, not just "within known caveats." Checked directly whether this closes the real-WAC-crop regression from Phase 70/72 or the Hapke-model comparison gap from Phase 74: **no, both essentially unchanged** — this fix is real and independently proven correct, but neither of those separate, larger, still-open issues was ever explained by it. **Phase 78 follow-up (2026-08-23, user's own direct visual review of the regenerated notebooks)**: the WAC_EMP-PDS migration improved the aggregate brightness-matched diff (see this row's own opening paragraph) but did **not** resolve the most visually obvious of the three observations above — the real west-to-east brightness gradient our hillshade still underrepresents is still directly visible post-migration. Confirms this gradient is a real, still-open issue independent of the Lunaserv-affine-stretch bug Phase 78 fixed, not explained or subsumed by it — still unexplained and not investigated further.
- **Open, found during the Phase 69 Hapke-calibration work, not implemented**: `lunaserv.fetch_real_hapke_params` samples ISIS's real calibration cube once per image, at the footprint's own center — real spatial variation exists within one footprint (checked directly: `wh`/`b0`/`hg1` a few percent of their own full-Moon range, `hg2`/`hh` somewhat more) but is secondary to the placeholder-vs-real gap this fixed. Per-pixel sampling (reprojecting the calibration cube onto the same working grid `reproject_astropedia_elevation_to_local_grid` builds the DEM/ortho on) would be a real further refinement.
- **Open (2026-08-23, Phase 78), pre-existing, not caused by the WAC_EMP migration but found and partially fixed while validating it**: `lunaserv.fetch_dem_and_ortho`'s DEM output filename (`dem_filled-tile-0.tif`) carries no suffix tied to `extra_footprint_lonlat_deg`, unlike `ortho_shaded_filename`'s own careful suffix discipline — any two calls against the same shared per-candidate output directory with *different* footprints silently clobber each other's DEM file, leaving a mismatched ortho/DEM pair on disk for anything that resumes it later (already a known, unguarded risk per `sfs_validation.run_sfs_forward_render`'s own docstring). Confirmed to trip a real `ValueError` there live, and separately to masquerade as a false *geometry* regression in `test_sfs_validation_lambertian_incidence.py` (0.0005 deg → 0.379 deg) — both traced to the same stale DEM pairing, not an actual geometry bug. **Three concrete call sites that triggered it are now fixed**: `notebooks/hapke_hillshade.py`'s direct Lambertian `fetch_dem_and_ortho` call, `tests/test_wac_emp_ortho_source.py`'s equivalent, and (found live 2026-08-30, via a real `entry.dem_ortho_result` shape mismatch while regenerating `notebooks/sfs_validation.ipynb`) `notebooks/along_track_correction.py`'s own `fetch_dem_and_ortho` call — all now pass `extra_footprint_lonlat_deg=entry.crop_footprint`, matching `entry.dem_ortho_result`'s own internal call. See `docs/history.md`'s Phase 78 entry for the full mechanism. **The underlying design gap remains open**: `dem_filled-tile-0.tif`'s own name still carries no `extra_footprint_lonlat_deg`-aware suffix (or hash), the same class of fix `ortho_shaded_filename` already applies for its own parameters — a future caller that forgets to union in the right footprint can still reintroduce this. **Update (2026-08-23, `docs/history.md`'s Phase 79 entry)**: the DEM fetch was split out into its own function (`lunaserv.fetch_dem`, now `product_registry`-registered under `"dem_filled"`) specifically so this label has one legible, auditable writer — but eliminating `extra_footprint_lonlat_deg` as a caller-suppliable parameter entirely (the plan's original, fuller fix) turned out to need either a `session.py` public-API signature change plus three notebook re-executions, or a resume-check redesign around a bbox-derived filename hash, both bigger than that session's budget — deliberately deferred rather than done partway. Real callers today all still pass the identical footprint derivation, so no live divergence is known, same as before.
- **Resolved (2026-08-29/30)**: `notebooks/along_track_correction.ipynb`/`real_hapke_params.ipynb`/`reproject_spike.ipynb`/`pose_alignment_spike.ipynb` all call `fetch_dem_and_ortho`/`dem_ortho_result`, so were equally affected by the `ortho_source="wac_emp_pds"` default but weren't regenerated in Phase 78's own pass. `along_track_correction.ipynb`/`real_hapke_params.ipynb`/`pose_alignment_spike.ipynb` are now all regenerated under current code; `reproject_spike.ipynb` no longer applies (archived to `old_notebooks/`, frozen, not kept in sync going forward). `real_hapke_params.ipynb`'s regenerated numbers: placeholder Hapke params (`mean|diff|=5.62`) now slightly beat the real-calibration default (`6.25`) on its one candidate — a modest, single-candidate result, not investigated further; the real-calibration default isn't reconsidered on this alone.
- **Open (2026-08-23, Phase 78) — saturation in `stretch_reflectance_to_uint8`'s fixed `[0, 0.30]` display stretch is neither confirmed absent nor a validated, deliberate acceptance; it's an unresolved question.** Two distinct sources of potential saturation, both unresolved:
  1. `hapke_shade_ortho`'s own `relit_reflectance = ortho * ratio` can exceed `DISPLAY_STRETCH_REFLECTANCE_MAX` for geometries near opposition (`ratio > 1`). Phase 72's original docstring characterized a resulting white-saturated patch as "the physically correct behavior... not an artifact to avoid" — on review (2026-08-23), that line was never actually validated or deliberately signed off on as a real decision; it's improvised reasoning that was allowed to stand unchallenged, not a settled design stance, and shouldn't be cited as one.
  2. `DISPLAY_STRETCH_REFLECTANCE_MAX = 0.30` (Phase 78, this migration) was chosen from general knowledge of typical lunar reflectance ranges and confirmed empirically non-saturating (`min=32, max=227`) for exactly **one** real candidate (the frozen default, `M1327210646CE`) — not swept across other candidates/geometries. A brighter real feature (e.g. fresh crater rays, a near-opposition geometry) could plausibly clip under this same fixed constant.

  Saturated pixels bias `sfs_validation.true_albedo_map`'s recovered albedo (an already-documented residual limitation of using the quantized `uint8` output, now triggered at a different absolute reflectance threshold than before this migration) and reduce `compute_brightness_matched_diff`'s discriminating power in any clipped region. Resolving this needs an actual decision — e.g. a real multi-candidate saturation sweep to see how often/how badly it happens, then either accepting a quantified risk, widening the range, or reconsidering the Hapke-ratio clipping behavior itself — not just asserting either combination is fine.

- **Resolved**: `hillshade`/`reproject` used to render at a fixed 256×256 (`config.DEFAULT_IMAGE_SIZE`),
  regardless of footprint size — measurably 2-4x coarser than the ~100 m/px DEM/ortho inputs or the
  real WAC crop's own ~184 m/px, and the main reason `crop` visibly outresolved them in
  `plot_zoom_blink_over()`. `DEFAULT_IMAGE_SIZE` is now `1316`, chosen (not auto-derived per
  candidate — the reference candidate's own footprint size) to land both axes at ~100 m/px on
  `M1327210646CE`, the notebook's default. `reproject` shares this size with `hillshade` by design
  (byte-identical pixel grid), even though its own real texture source (the WAC crop) caps out
  coarser, ~184 m/px — deliberate, so the two stay pixel-aligned. See
  `docs/resolution-investigation.md` for the full numbers and rationale.

## Development history

See `docs/history.md` for the phase-by-phase narrative — what was tried, what broke, and how each
design decision (framelet timing, sensor axis convention, catalog-driven selection, the perf fixes
in the sweep) was actually reached. Background/curiosity reading, not required before making a
change; this file and `docs/data-sources.md`/`docs/external-tools.md` describe current behavior.
