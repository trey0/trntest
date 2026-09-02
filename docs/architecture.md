# trntest — architecture & status

## What this is

Explores techniques for generating candidate test imagery for terrain-relative navigation (TRN)
testing (hence the repo name) — images that could stand in for real spacecraft imagery when testing
a TRN algorithm. Camera pose for every generated image comes from the **real LRO spacecraft
trajectory** (NAIF SPICE kernels), so each candidate's geometry can be validated against a real
basemap rather than just asserted.

The main product is a populated **dataset**: `notebooks/select_datasets.py` selects a diverse,
maneuver-free multi-orbit window of manifest entries (dozens of images, not one), then
`TrnTestDataSet.populate()`/`populate_via_workers()` runs each entry through three interchangeable
TRN test image generators — see [`docs/generators.md`](generators.md) for what each one does and
why. This is a demo/exercise in AI-assisted coding on a real geospatial engineering task.

See [`README.md`](../README.md) for how to build/run/test it.

## Status

**Untested at dataset scale.** Per-entry report generation (`src/trntest/report.py`/
`notebooks/report_template.py`, via `TrnTestReport`) is wired into `populate()`/
`populate_via_workers()`, but its own content is still a first-pass minimal template — see
[`docs/proposed-tasks/report-plan.md`](proposed-tasks/report-plan.md). A real population run across
a full selected dataset hasn't happened yet, so there's no dataset-scale validation.

`notebooks/image_generation.py` validates the full per-entry pipeline in detail, on one manifest
entry (`notebooks/dataset_manifest.csv`, checked in, frozen): all three generators, two independent
geometry checks against a real hillshade basemap, and explicit tie points comparing the candidates
directly against each other. Predates the dataset-population product above and doesn't exercise
`populate()`/`populate_via_workers()` at scale — the reference for what "correct" looks like at the
single-entry level.

## Architecture (`src/trntest/`)

| Module | Responsibility |
|---|---|
| [`cache.py`](../src/trntest/cache.py) | Local-mirror disk caching for all external fetches (NAIF, Lunaserv, LROC) — see [`docs/caching.md`](caching.md). |
| [`camera.py`](../src/trntest/camera.py) | Poses the synthetic camera from SPICE trajectory/orientation data (`build_camera`) and solves its corrected FOV (`solve_corrected_fov`) — see [`docs/reproject-fov-investigation.md`](reproject-fov-investigation.md). |
| [`catalog.py`](../src/trntest/catalog.py) | PDS ODE REST API client — lists EDR/CDR products by time range, matches EDR↔CDR pairs (`list_products`, `find_matching_cdr`). |
| [`config.py`](../src/trntest/config.py) | `TrntestConfig`/`load_config()` — endpoints, paths, product IDs, tunables. TOML file + `TRNTEST_*` env var overrides. |
| [`control_network.py`](../src/trntest/control_network.py) | Converts `pose_alignment.py`'s 2D tie points into ISIS control points for a `jigsaw` bundle adjustment — see [`docs/wac-jigsaw-investigation.md`](wac-jigsaw-investigation.md). On the back burner, not wired into the main pipeline. |
| [`crater_depth.py`](../src/trntest/crater_depth.py) | Robbins-crater depth measurement off a DEM (Breton et al. 2019) and the Stoffler et al. 2006 fresh-crater reference depth, for a `sharpness_ratio` grade — see [`docs/crater-grading.md`](crater-grading.md). |
| [`crater_depth_batch.py`](../src/trntest/crater_depth_batch.py) | Whole-database crater-depth precompute, tiled for cache coherence — see [`docs/crater-grading.md`](crater-grading.md). Not yet run across the full database. |
| [`craters.py`](../src/trntest/craters.py) | Robbins craters catalog overlay: fetches/caches the PDS4 CSV, builds a spatially-indexed GeoPackage, and returns ellipse polygons for a raster's footprint (`crater_overlay_layer`) — see [`docs/data-sources/robbins-craters.md`](data-sources/robbins-craters.md). |
| [`dataset.py`](../src/trntest/dataset.py) | Public multi-image API: `images_for_window()` evaluates EDR candidates over a time window (throttled/illumination-filtered); `generate_dataset()` renders the selected ones. |
| [`dataset_selection.py`](../src/trntest/dataset_selection.py) | Orbit-level TRN-OD dataset selection (`notebooks/select_datasets.py`): picks multi-day, maneuver-free orbit spans jointly diverse in solar hour angle, then hands one selected window to `dataset.py`. |
| [`illumination.py`](../src/trntest/illumination.py) | Sun/orbit geometry via SPICE (sun elevation/azimuth, sub-solar point, node-crossing search) plus the angle-wraparound math helpers `dataset_selection.py`/`plotting.py` use. |
| [`isis_wac.py`](../src/trntest/isis_wac.py) | Steps a WAC EDR through ISIS3's own pipeline (`lrowac2isis`→`spiceinit`→`lrowaccal`→`framestitch`→`crop`→`cam2map`) as this project's real-WAC comparison path — see [`docs/external-tools.md`](external-tools.md)'s ISIS Pushframe pipeline section. |
| [`lunaserv.py`](../src/trntest/lunaserv.py) | Fetches DEM (Astropedia GLD100) + ortho (WAC_EMP PDS4) imagery for a camera's footprint and preps both for `sat_sim`, including Hapke-relit hillshade blending — see the module docstring and [`docs/data-sources/astropedia-gld100.md`](data-sources/astropedia-gld100.md)/[`wac-emp-pds4.md`](data-sources/wac-emp-pds4.md). |
| [`maneuver_detection.py`](../src/trntest/maneuver_detection.py) | Detects likely propulsive maneuvers in LRO's reconstructed-orbit SPK via step changes in angular momentum/orbital energy (`find_maneuver_candidates`) — see the module docstring for the derivation. |
| [`orientation.py`](../src/trntest/orientation.py) | Notebook-display-only north-up rotation (does not touch the sensor model). |
| [`plotting.py`](../src/trntest/plotting.py) | Comparison-figure plotting: raw-pixel/geometry checks (`plot_render_vs_basemap`, `plot_overlay`/`plot_overlay_toggle`/`plot_zoom_blink`), a quantitative brightness diff (`compute_brightness_matched_diff`), and dataset-selection scatter plots. |
| [`pose_alignment.py`](../src/trntest/pose_alignment.py) | Feature-matches a map-projected WAC crop against the basemap and fits a 2D correction (similarity/affine/homography) — see [`docs/wac-jigsaw-investigation.md`](wac-jigsaw-investigation.md). On the back burner, not wired into the main pipeline. |
| [`product_registry.py`](../src/trntest/product_registry.py) | Intermediate-product access-discipline primitives (`writes_product`/`reads_product`/`deletes_product`, `atomic_publish*`) — see [`docs/intermediate-product-discipline.md`](intermediate-product-discipline.md). |
| [`render.py`](../src/trntest/render.py) | Renders the synthetic image via ASP `sat_sim`, then converts the camera to a CSM Frame sidecar via `cam_gen` (`run_sat_sim`). |
| [`report.py`](../src/trntest/report.py) | Per-entry HTML report helpers/pipeline (`generate_report`, `problem_flags`, ...) for `notebooks/report_template.py`, used by `TrnTestReport` below — see [`docs/proposed-tasks/report-plan.md`](proposed-tasks/report-plan.md). Report content itself is still a first-pass minimal template. |
| [`session.py`](../src/trntest/session.py) | `Session` facade — thin one-line delegators so notebook cells don't repeat `config=...`. |
| [`sfs_validation.py`](../src/trntest/sfs_validation.py) | Cross-checks `lunaserv.hapke_shade_ortho` against ASP `sfs` run as an independent forward renderer, for DEM-aware ground truth on the Hapke shading math. |
| [`spice_kernels.py`](../src/trntest/spice_kernels.py) | Selects/downloads the minimal SPICE kernel set for a date and furnishes it (`fetch_and_furnish`) — see [`docs/data-sources/spice-kernels-isis.md`](data-sources/spice-kernels-isis.md)/[`spice-kernels-naif.md`](data-sources/spice-kernels-naif.md). |
| [`subprocess_utils.py`](../src/trntest/subprocess_utils.py) | `run_quiet` — runs ASP/ISIS subprocesses with captured, on-failure-only output. |
| [`tasks.py`](../src/trntest/tasks.py) | Two `huey` (sqlite-backed) task queues driving `trn_dataset.py`'s `populate()`/`populate_via_workers()`, one per execution mode (`immediate=True` in-process vs. `immediate=False` multi-worker). |
| [`tie_points.py`](../src/trntest/tie_points.py) | Projects the same 5 ground points (4 corners + center) into both the synthetic render and the WAC crop, for the comparison figure's explicit tie points (`select_tie_points`/`resolve_crop_pixels`). |
| [`trn_dataset.py`](../src/trntest/trn_dataset.py) | `TrnTestDataSet`/`TrnTestEntry`/`TrnTestProduct` — a structured, resumable dataset folder; `populate()`/`populate_via_workers()` drive generation sequentially or across worker processes. `TrnTestProduct` covers all four product types (`TrnTestImage` subclasses `crop`/`hillshade`/`reproject`; `TrnTestReport` is the per-entry HTML report, default-on in `PRODUCT_TYPES`, self-ensuring its `hillshade` dependency). `write_index()` writes a dataset-wide `status.csv`/`reports/index.html` nav bar after each `populate*()` call. |
| [`wac.py`](../src/trntest/wac.py) | Extracts a band-separated VIS mosaic from a WAC CDR product via manual byte offsets (`fetch_vis_mosaic`) — superseded by `isis_wac.py` as the demo's real-WAC comparison method, kept for its own test coverage. |
| [`wac_camera_model.py`](../src/trntest/wac_camera_model.py) | Hand-rolled Python forward projector for the WAC Pushframe camera (ground-to-image) — see [`docs/wac-jigsaw-investigation.md`](wac-jigsaw-investigation.md). On the back burner, not wired into the main pipeline. |

## Notebooks (`notebooks/`)

Lightweight pointers — each notebook's own markdown is the tutorial prose; these rows just say what
it's for and where its output goes.

### Primary notebooks

| Notebook | Purpose |
|---|---|
| [`image_generation.ipynb`](../notebooks/image_generation.ipynb) | The single-entry reference: all three generators plus two independent geometry validations against a real basemap, run against [`dataset_manifest.csv`](../notebooks/dataset_manifest.csv)'s one frozen entry — see [`README.md`](../README.md) to run it. |
| [`select_datasets.ipynb`](../notebooks/select_datasets.ipynb) | The dataset-population product's entry point: selects a diverse, maneuver-free multi-orbit window and creates a `TrnTestDataSet` folder from it — see [`docs/batch-generation.md`](batch-generation.md) for the next step (`populate()`). |

### Other notebooks

| Notebook | Purpose |
|---|---|
| [`along_track_correction.ipynb`](../notebooks/along_track_correction.ipynb) | Validates `lunaserv.hapke_shade_ortho`'s along-track motion correction against the frozen-camera-position fallback. |
| [`crater_sharpness_review.ipynb`](../notebooks/crater_sharpness_review.ipynb) | Visual review of crater sharpness grading for one candidate's footprint — see [`docs/crater-grading.md`](crater-grading.md). |
| [`hapke_hillshade.ipynb`](../notebooks/hapke_hillshade.ipynb) | Compares ISIS `photomet` Hapke hillshading against the plain Lambertian fallback. |
| [`pose_alignment_spike.ipynb`](../notebooks/pose_alignment_spike.ipynb) | Exercises the camera-pose-alignment tooling (`pose_alignment.py` row above) — see [`docs/wac-jigsaw-investigation.md`](wac-jigsaw-investigation.md). |
| [`real_hapke_params.ipynb`](../notebooks/real_hapke_params.ipynb) | Compares real, ISIS-calibration-sourced Hapke parameters against the illustrative placeholder defaults. |
| [`report_template.py`](../notebooks/report_template.py) | The `{{ }}`-templated source for per-entry HTML reports (not paired/executable itself) — see `report.py` row above. |
| [`sfs_validation.ipynb`](../notebooks/sfs_validation.ipynb) | Independent forward-render cross-check of `hapke_shade_ortho` against ASP `sfs`. |
| [`wac_isis.ipynb`](../notebooks/wac_isis.ipynb) | Step-by-step walkthrough of ISIS3's EDR-to-`framestitch` pipeline for one real WAC product. |

## Open items

See [`docs/proposed-tasks/open-items.md`](proposed-tasks/open-items.md) for the current list.

## Development history

See [`docs/history.md`](history.md) for the phase-by-phase narrative — what was tried, what broke,
and how each design decision (framelet timing, sensor axis convention, catalog-driven selection, the
perf fixes in the sweep) was actually reached. Background/curiosity reading, not required before
making a change; this file and [`docs/data-sources.md`](data-sources.md)/
[`docs/external-tools.md`](external-tools.md) describe current behavior.
