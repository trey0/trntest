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
TRN test image generators — see `docs/generators.md` for what each one does and why. This is a
demo/exercise in AI-assisted coding on a real geospatial engineering task.

All heavy tooling/build/test happens inside a Docker container (Ubuntu 24.04), built from the
checked-in `docker/Dockerfile`, so it's reproducible off this host.

## Status

**Incomplete and untested at dataset scale.** Per-entry report generation
(`src/trntest/report.py`/`notebooks/report_template.py`) is a first prototype, not the full
multi-entry design calls for — see `docs/proposed-tasks/report-plan.md`. A real population run
across a full selected dataset is deliberately deferred until report completeness improves, so
there's no dataset-scale validation yet.

`notebooks/image_generation.py` validates the full per-entry pipeline in detail, on one manifest
entry (`notebooks/dataset_manifest.csv`, checked in, frozen): all three generators, two independent
geometry checks against a real hillshade basemap, and explicit tie points comparing the candidates
directly against each other. Predates the dataset-population product above and doesn't exercise
`populate()`/`populate_via_workers()` at scale — the reference for what "correct" looks like at the
single-entry level.

Packaged as an installable library (`src/trntest/`) with config, tests, and style tooling.

## Architecture (`src/trntest/`)

| Module | Responsibility |
|---|---|
| `cache.py` | Local-mirror disk caching for all external fetches (NAIF, Lunaserv, LROC) — see `docs/caching.md`. |
| `camera.py` | Poses the synthetic camera from SPICE trajectory/orientation data (`build_camera`) and solves its corrected FOV (`solve_corrected_fov`) — see `docs/reproject-fov-investigation.md`. |
| `catalog.py` | PDS ODE REST API client — lists EDR/CDR products by time range, matches EDR↔CDR pairs (`list_products`, `find_matching_cdr`). |
| `config.py` | `TrntestConfig`/`load_config()` — endpoints, paths, product IDs, tunables. TOML file + `TRNTEST_*` env var overrides. |
| `control_network.py` | Converts `pose_alignment.py`'s 2D tie points into ISIS control points for a `jigsaw` bundle adjustment — see `docs/wac-jigsaw-investigation.md`. On the back burner, not wired into the main pipeline. |
| `crater_depth.py` | Robbins-crater depth measurement off a DEM (Breton et al. 2019) and the Stoffler et al. 2006 fresh-crater reference depth, for a `sharpness_ratio` grade — see `docs/crater-grading.md`. |
| `crater_depth_batch.py` | Whole-database crater-depth precompute, tiled for cache coherence — see `docs/crater-grading.md`. Not yet run across the full database. |
| `craters.py` | Robbins craters catalog overlay: fetches/caches the PDS4 CSV, builds a spatially-indexed GeoPackage, and returns ellipse polygons for a raster's footprint (`crater_overlay_layer`) — see `docs/data-sources/robbins-craters.md`. |
| `dataset.py` | Public multi-image API: `images_for_window()` evaluates EDR candidates over a time window (throttled/illumination-filtered); `generate_dataset()` renders the selected ones. |
| `dataset_selection.py` | Orbit-level TRN-OD dataset selection (`notebooks/select_datasets.py`): picks multi-day, maneuver-free orbit spans jointly diverse in solar hour angle, then hands one selected window to `dataset.py`. |
| `illumination.py` | Sun/orbit geometry via SPICE (sun elevation/azimuth, sub-solar point, node-crossing search) plus the angle-wraparound math helpers `dataset_selection.py`/`plotting.py` use. |
| `isis_wac.py` | Steps a WAC EDR through ISIS3's own pipeline (`lrowac2isis`→`spiceinit`→`lrowaccal`→`framestitch`→`crop`→`cam2map`) as this project's real-WAC comparison path — see `docs/external-tools.md`'s ISIS Pushframe pipeline section. |
| `lunaserv.py` | Fetches DEM (Astropedia GLD100) + ortho (WAC_EMP PDS4) imagery for a camera's footprint and preps both for `sat_sim`, including Hapke-relit hillshade blending — see the module docstring and `docs/data-sources/astropedia-gld100.md`/`wac-emp-pds4.md`. |
| `maneuver_detection.py` | Detects likely propulsive maneuvers in LRO's reconstructed-orbit SPK via step changes in angular momentum/orbital energy (`find_maneuver_candidates`) — see the module docstring for the derivation. |
| `orientation.py` | Notebook-display-only north-up rotation (does not touch the sensor model). |
| `plotting.py` | Comparison-figure plotting: raw-pixel/geometry checks (`plot_render_vs_basemap`, `plot_overlay`/`plot_overlay_toggle`/`plot_zoom_blink`), a quantitative brightness diff (`compute_brightness_matched_diff`), and dataset-selection scatter plots. |
| `pose_alignment.py` | Feature-matches a map-projected WAC crop against the basemap and fits a 2D correction (similarity/affine/homography) — see `docs/wac-jigsaw-investigation.md`. On the back burner, not wired into the main pipeline. |
| `product_registry.py` | Intermediate-product access-discipline primitives (`writes_product`/`reads_product`/`deletes_product`, `atomic_publish*`) — see `docs/intermediate-product-discipline.md`. |
| `render.py` | Renders the synthetic image via ASP `sat_sim`, then converts the camera to a CSM Frame sidecar via `cam_gen` (`run_sat_sim`). |
| `report.py` | Per-entry HTML report helpers (`load_entry`/`summary`/...) for `notebooks/report_template.py` — see `docs/proposed-tasks/report-plan.md`. First prototype, not the full multi-entry design. |
| `session.py` | `Session` facade — thin one-line delegators so notebook cells don't repeat `config=...`. |
| `sfs_validation.py` | Cross-checks `lunaserv.hapke_shade_ortho` against ASP `sfs` run as an independent forward renderer, for DEM-aware ground truth on the Hapke shading math. |
| `spice_kernels.py` | Selects/downloads the minimal SPICE kernel set for a date and furnishes it (`fetch_and_furnish`) — see `docs/data-sources/spice-kernels-isis.md`/`spice-kernels-naif.md`. |
| `subprocess_utils.py` | `run_quiet` — runs ASP/ISIS subprocesses with captured, on-failure-only output. |
| `tasks.py` | Two `huey` (sqlite-backed) task queues driving `trn_dataset.py`'s `populate()`/`populate_via_workers()`, one per execution mode (`immediate=True` in-process vs. `immediate=False` multi-worker). |
| `tie_points.py` | Projects the same 5 ground points (4 corners + center) into both the synthetic render and the WAC crop, for the comparison figure's explicit tie points (`select_tie_points`/`resolve_crop_pixels`). |
| `trn_dataset.py` | `TrnTestDataSet`/`TrnTestEntry`/`TrnTestImage` — a structured, resumable dataset folder; `populate()`/`populate_via_workers()` drive generation sequentially or across worker processes. |
| `wac.py` | Extracts a band-separated VIS mosaic from a WAC CDR product via manual byte offsets (`fetch_vis_mosaic`) — superseded by `isis_wac.py` as the demo's real-WAC comparison method, kept for its own test coverage. |
| `wac_camera_model.py` | Hand-rolled Python forward projector for the WAC Pushframe camera (ground-to-image) — see `docs/wac-jigsaw-investigation.md`. On the back burner, not wired into the main pipeline. |

`notebooks/image_generation.ipynb` reads the checked-in, now-frozen `notebooks/dataset_manifest.csv`
(the notebook that used to regenerate it, `data_set_selection.ipynb`, was removed), populates and
reads from the shared `trn_dataset` folder
via `TrnTestDataSet`/`TrnTestEntry`/`TrnTestImage`, and drives the rest of the pipeline end to end —
see `README.md` to run it, and AGENTS.md's "Working conventions" for how to validate changes
against it.

`notebooks/select_datasets.py` is the entry point for the dataset-population product described
above: picks *orbits*, not single images — a selected dataset here has 100+ entries, a different
scope from `image_generation.py`'s single-EDR demo, so the two don't share a dataset folder or wire
together (`dataset_manifest.csv` is untouched). Its last two cells bridge one selected
orbit-sequence into an images table (`dataset_selection.resolve_orbit_sequence`, resolving just
`selected_datasets.iloc[0]`, not all `N_DATASETS` picks) and create a `TrnTestDataSet` folder from
it (`orbit_sequence_dataset`, separate from `image_generation.ipynb`'s `trn_dataset`) — also writing
`orbit_sequence.csv` alongside `manifest.csv` for debugging/provenance. Stops short of
`populate()` — no rendering from this notebook yet; see `docs/batch-generation.md` for that step.

## Open items

(When one of these resolves, delete it — state any fact still needed directly where it's needed,
e.g. a docstring/comment or a `docs/` reference doc, rather than leaving a "Resolved" entry here.)

- Whether `--save-as-csm` state JSON is an acceptable stand-in for a literal ISD file for whatever
  comes after this demo.
- Confirm the lunar frame kernel defining `MOON_ME` loads correctly so SPICE can output that frame
  directly; sanity-check against the known GLD100/LOLA convention.
- **Astropedia's GLD100 only covers ±79° latitude** (`lunaserv.ASTROPEDIA_MAX_ABS_LATITUDE_DEG`) —
  `fetch_dem_and_ortho` raises rather than falling back to the deprecated, artifact-affected
  Lunaserv DTM path for any footprint beyond it, so a catalog-driven selection near either pole
  fails outright. NASA's VIRA project (`github.com/nasa/vira`) points at higher-resolution
  LOLA-derived polar mosaics for this gap. Not implemented — would need its own fetch/caching and a
  coverage-based dispatch in `fetch_dem_and_ortho`.
- The user's requested "error-handling/fallback-consistency" quality audit only got through
  **Chunk A** (`tie_points.py`+`isis_wac.py`) before spiraling into a real fix rather than staying a
  survey. Chunks B-E were never scoped — re-scope from scratch rather than assume a prior chunking
  plan still applies.
- The real-WAC-crop/hillshade brightness match has an unresolved regression and an unresolved
  validation gap. `lunaserv._terrain_photometric_angles`'s surface-normal computation and
  `hapke_shade_ortho`'s Hapke-ratio relighting were both made permanent/unconditional on the user's
  explicit call, despite the Hapke-ratio fix being confirmed to *worsen* the one measured
  brightness-matched diff (8.6853 → 9.2425) — not yet explained. Real `campt` ground truth can't
  validate the DEM-aware case (it stays ellipsoid-normal-based even with a DEM shape model
  attached), so ASP `sfs` was used as an independent forward-render cross-check instead
  (`sfs_validation.py`): its Lambertian mode's own independently-recovered incidence angle now
  matches `lunaserv.real_geometry_photometric_angles` to ~0.0005 deg mean, closing the DEM-aware
  validation gap for incidence — but confirming (not explaining) that the brightness regression and
  three other live visual observations (a real east-brightening gradient the hillshade
  underrepresents; an apparent ~10 deg shadow rotation confirmed *not* a sun-azimuth bug;
  anomalously bright real crater floors) remain genuinely open. `sfs` itself has a structural gap
  for phase/emission cross-checks: its reconstructed CSM camera can't represent
  `along_track_correction`. See `docs/history.md`'s Phase 70-79 entries for the full investigation
  trail.
- `lunaserv.fetch_real_hapke_params` samples ISIS's real calibration cube once per image, at the
  footprint's own center — real spatial variation exists within one footprint (a few percent of
  `wh`/`b0`/`hg1`'s own full-Moon range, somewhat more for `hg2`/`hh`) but is secondary to the
  placeholder-vs-real gap this already fixed. Per-pixel sampling (reprojecting the calibration cube
  onto the same working grid the DEM/ortho use) would be a real further refinement.
- `lunaserv.fetch_dem`'s DEM output filename carries no suffix tied to `extra_footprint_lonlat_deg`
  (unlike the ortho's own suffix discipline) — two calls against the same output directory with
  different footprints could silently disagree about which DEM is "the" one. All current real call
  sites pass the same footprint derivation, so no live divergence is known, but a future caller that
  forgets to could reintroduce it.
- Whether `stretch_reflectance_to_uint8`'s fixed `[0, 0.30]` display stretch saturates is an
  unresolved question. Two distinct sources, neither confirmed absent: (1) `hapke_shade_ortho`'s
  relit reflectance can exceed the max for geometries near opposition (`ratio > 1`); (2)
  `DISPLAY_STRETCH_REFLECTANCE_MAX = 0.30` was confirmed empirically non-saturating for exactly one
  real candidate — not swept across other candidates/geometries (e.g. fresh crater rays,
  near-opposition geometry could plausibly clip). Saturated pixels would bias
  `sfs_validation.true_albedo_map`'s recovered albedo and reduce
  `compute_brightness_matched_diff`'s discriminating power in any clipped region. Resolving this
  needs an actual multi-candidate saturation sweep, not just asserting either combination is fine.

## Development history

See `docs/history.md` for the phase-by-phase narrative — what was tried, what broke, and how each
design decision (framelet timing, sensor axis convention, catalog-driven selection, the perf fixes
in the sweep) was actually reached. Background/curiosity reading, not required before making a
change; this file and `docs/data-sources.md`/`docs/external-tools.md` describe current behavior.
