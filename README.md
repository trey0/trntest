# trntest — lunar remote sensing demo

Explores techniques for generating candidate test imagery for terrain-relative navigation (TRN)
testing (hence the repo name) — synthetic lunar satellite images that could stand in for real
spacecraft imagery when testing a TRN algorithm, rendered with NASA's Ames Stereo Pipeline tools
(`sat_sim`, `mapproject`) from real lunar source data. Camera pose for every generated image comes
from the **real LRO spacecraft trajectory** (NAIF SPICE kernels), so each candidate's geometry can
be validated against a real basemap rather than just asserted.

The main product is a populated **dataset**: `notebooks/select_datasets.py` selects a diverse,
maneuver-free multi-orbit window of manifest entries (dozens of images, not one), then
`TrnTestDataSet.populate()`/`populate_via_workers()` runs each entry through three interchangeable
TRN test image generators — see [`docs/generators.md`](docs/generators.md) for what each one is
built from, does, and why. This is a demo/exercise in AI-assisted coding on a real geospatial
engineering task; see [`AGENTS.md`](AGENTS.md) for how this repo's docs are organized.

The demo logic is the installable `trntest` Python package (`src/trntest/`), driven from
jupytext-paired notebooks under `notebooks/` — the `.py` (percent format) is the source of truth
for review/diff/lint/IDE work; the `.ipynb` (fully executed) carries the outputs and renders
natively in GitHub's file browser (see "Notebooks" below for the convention).

## Status

**Untested at dataset scale.** Per-entry report generation (`src/trntest/report.py`/
`notebooks/report_template.py`, via `TrnTestReport`) is wired into `populate()`/
`populate_via_workers()`, but its own content is still a first-pass minimal template. A real
population run across a full selected dataset hasn't happened yet, so there's no dataset-scale
validation.

See the "Primary notebooks" table below for what's demonstrated and validated today, at the
single-entry level.

## Open items

See [`docs/proposed-tasks/open-items.md`](docs/proposed-tasks/open-items.md) for the current list.

## Documentation

See [`docs/docs-index.md`](docs/docs-index.md) for an overview of documentation.

## Build & run

All tooling (GDAL, ASP, SPICE) lives in a Docker container — nothing needs installing on the host
beyond Docker itself.

```sh
cd docker
docker compose build
docker compose up -d
```

Jupyter Lab then listens on the container's port 8888, mapped to `127.0.0.1:8888` on this host
only (no auth token is set, so it's intentionally not exposed on the public interface). From your
own machine:

```sh
ssh -L 8888:localhost:8888 <this-host>
```

then open `http://localhost:8888` in a browser, and from there `notebooks/image_generation.ipynb`
— the bundled `jupyterlab-jupytext` extension keeps it and its paired `.py` in sync, so editing
either one works. See "Notebooks" below for the paired-file workflow.

For one-off commands instead of the notebook server:

```sh
docker compose run --rm demo sat_sim --help
docker compose run --rm demo gdalinfo --version
```

`docker-compose.yml` mounts the repo at `/workspace`, and mounts `cache/`/`output/` at
`/workspace/cache`/`/workspace/output` (these live *outside* this repo — see Configuration below).
Fetched WMS tiles and SPICE kernels persist there across container rebuilds (see `docs/caching.md`).

## Development setup

The Docker image (`docker compose build`, above) already has `trntest` installed in editable mode
plus `ruff`, `mypy`, `pytest`, `jupytext`, `jupyterlab`, and `ipykernel` — nothing further to
install for that path. jupytext's JupyterLab integration (`jupyterlab-jupytext`) registers
automatically as part of the `jupytext` install — no separate `jupyter labextension install` step.

Lint/type-check/test-only, without the notebook/ASP/GDAL stack, also works in a plain host venv
with Python 3.11+:

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

## Configuration

Default settings (cache/output directories, NAIF/Lunaserv/LROC endpoints, which EDR/CDR product
this demo targets, image size, FOV, Moon radius, DEM sampling) live in `src/trntest/config.py` and
match the values this repo has always used. To override any of them, copy `trntest.example.toml`
to `trntest.toml` at the repo root (picked up automatically) or point the `TRNTEST_CONFIG` env var
at a file elsewhere. `TRNTEST_CACHE_ROOT`/`TRNTEST_OUTPUT_DIR` env vars override just those two
paths without needing a config file at all.

`cache/`, `output/`, and `scratch/` are **not** part of this repo's own directory tree — they live
one level above the outer workspace's `src/` (e.g. `<workspace>/cache`, `<workspace>/output`,
siblings of `<workspace>/src/trntest`), an out-of-source, ROS-workspace-inspired layout.
`docker-compose.yml`'s volume mounts assume this repo is checked out at `<workspace>/src/trntest`;
if you've cloned it somewhere else, either recreate that wrapper directory, override
`TRNTEST_HOST_CACHE_DIR`/`TRNTEST_HOST_OUTPUT_DIR`/`TRNTEST_HOST_SCRATCH_DIR` (see `docker/.env`,
below), or just set `TRNTEST_CACHE_ROOT`/`TRNTEST_OUTPUT_DIR`/a `trntest.toml` instead.

**Working in a Claude Code worktree** (`.claude/worktrees/<name>/`, alongside the main checkout,
sharing the same outer `trntest_ws`)? Run `scripts/setup_worktree_docker_env.sh` once before your
first `docker compose` call — it writes a gitignored `docker/.env` so your worktree shares the main
checkout's `cache`/`scratch` but gets its own `output/<name>/` subfolder and its own image
tag/Compose project name, so concurrent agents don't clobber each other's outputs or image builds.
See `docs/environment.md`'s "Multi-agent worktrees" section for why.

## Linting and type-checking

```sh
trntest-lint --all          # everything in the repo
trntest-lint a.py b.py      # explicit files
trntest-lint --diff         # files changed vs. HEAD (also the default with no arguments)
```

Runs `ruff format --check`, `ruff check`, and `mypy`, and reports all three results together.

## Tests

```sh
pytest
```

Covers the pure/deterministic logic (geometry helpers, config resolution, byte-layout unpacking,
`Session`'s delegation) — nothing that needs live SPICE kernels, network access, or the ASP
binaries, so it runs anywhere the dev dependencies are installed. Tests needing any of that (real
SPICE kernel furnishing, live NAIF fetches — e.g. `tests/test_maneuver_detection.py`'s ground-truth
checks against real LRO trajectory data) are marked `@pytest.mark.heavy` and excluded from this
default run (`pyproject.toml`'s `addopts`); run them inside Docker instead:

```sh
scripts/run_heavy_tests.sh              # all heavy tests
scripts/run_heavy_tests.sh -k maneuver  # or narrow with normal pytest args
```

## Git pre-commit hook

This repo ships a pre-commit hook (`githooks/pre-commit`) that runs `trntest-lint` against staged
`.py` files (`ruff format --check`, `ruff check`, `mypy`) and staged notebook `.py`/`.ipynb` pairs
before each commit — checking that the pair is staged together, that their code/markdown content
matches, and that the `.ipynb`'s `execution_count`s look like a single clean top-to-bottom run (the
shape `scripts/run_notebook.sh` produces). It can't cheaply verify that a notebook's outputs are
*fresh* relative to its code (that needs re-execution, which is slow: SPICE/WMS/`sat_sim` calls),
so always run `scripts/run_notebook.sh notebooks/<name>.py` after a code change rather than relying
on the hook alone. It uses git's built-in `core.hooksPath` mechanism (no external `pre-commit`
framework, no symlinks to set up). Enable it once per clone:

```sh
git config core.hooksPath githooks
```

If `trntest-lint` is on your `PATH` (host venv with the dev dependencies installed), it runs
directly; otherwise the hook falls back to running it inside Docker automatically, so it works on
any clone with just Docker installed -- no host-side Python setup required.

## Notebooks (`notebooks/`)

Each notebook's own markdown holds the full tutorial; the tables below just index what each one
covers. Every notebook is a jupytext-paired `.py`/`.ipynb`: the `.py` (percent format) is what
stays diffable and lints cleanly; the `.ipynb` carries fully-executed outputs and renders natively
in GitHub's file browser (markdown, code, and images) — no separate publishing step. Edit either
half — JupyterLab's jupytext extension keeps both in sync on save — or edit the `.py` and
regenerate the `.ipynb` headlessly:

```sh
scripts/run_notebook.sh notebooks/<name>.py
```

Either way, commit only once the notebook has executed cleanly top-to-bottom (checked by the
lint's notebook checks).

### Primary notebooks

| Notebook | Purpose |
|---|---|
| [`image_generation.ipynb`][image_generation.ipynb] | Demonstrates and validates the three TRN test image generators (see [`docs/generators.md`](docs/generators.md)) on a single, real LRO WAC EDR — two independent geometry checks against a real basemap, plus explicit tie points comparing the candidates against each other. |
| [`select_datasets.ipynb`][select_datasets.ipynb] | Demonstrates selecting multiple TRN testing datasets — each covers 24 consecutive, maneuver-free orbits, and the selected datasets are jointly diverse in lunar longitude and solar hour angle. See [`docs/batch-generation.md`](docs/batch-generation.md) for the next step (`populate()`). |

### Other notebooks

| Notebook | Purpose |
|---|---|
| [`along_track_correction.ipynb`][along_track_correction.ipynb] | Validates `lunaserv.hapke_shade_ortho`'s along-track motion correction against the frozen-camera-position fallback. |
| [`crater_sharpness_review.ipynb`][crater_sharpness_review.ipynb] | Visual review of crater sharpness grading for one candidate's footprint — see [`docs/crater-grading.md`](docs/crater-grading.md). |
| [`hapke_hillshade.ipynb`][hapke_hillshade.ipynb] | Compares ISIS `photomet` Hapke hillshading against the plain Lambertian fallback. |
| [`pose_alignment_spike.ipynb`][pose_alignment_spike.ipynb] | Exercises the camera-pose-alignment tooling (`pose_alignment.py` row below) — see [`docs/wac-jigsaw-investigation.md`](docs/wac-jigsaw-investigation.md). |
| [`real_hapke_params.ipynb`][real_hapke_params.ipynb] | Compares real, ISIS-calibration-sourced Hapke parameters against the illustrative placeholder defaults. |
| [`report_template.py`][report_template.py] | The `{{ }}`-templated source for per-entry HTML reports (not paired/executable itself, so linked as `.py` — there's no `.ipynb`) — see `report.py` row below. |
| [`sfs_validation.ipynb`][sfs_validation.ipynb] | Independent forward-render cross-check of `hapke_shade_ortho` against ASP `sfs`. |
| [`wac_isis.ipynb`][wac_isis.ipynb] | Step-by-step walkthrough of ISIS3's EDR-to-`framestitch` pipeline for one real WAC product. |

[image_generation.ipynb]: notebooks/image_generation.ipynb
[select_datasets.ipynb]: notebooks/select_datasets.ipynb
[along_track_correction.ipynb]: notebooks/along_track_correction.ipynb
[crater_sharpness_review.ipynb]: notebooks/crater_sharpness_review.ipynb
[hapke_hillshade.ipynb]: notebooks/hapke_hillshade.ipynb
[pose_alignment_spike.ipynb]: notebooks/pose_alignment_spike.ipynb
[real_hapke_params.ipynb]: notebooks/real_hapke_params.ipynb
[report_template.py]: notebooks/report_template.py
[sfs_validation.ipynb]: notebooks/sfs_validation.ipynb
[wac_isis.ipynb]: notebooks/wac_isis.ipynb

## Source files (`src/trntest/`)

| Module | Responsibility |
|---|---|
| [`cache.py`][cache.py] | Local-mirror disk caching for all external fetches (NAIF, Lunaserv, LROC) — see [`docs/caching.md`](docs/caching.md). |
| [`camera.py`][camera.py] | Poses the synthetic camera from SPICE trajectory/orientation data (`build_camera`) and solves its corrected FOV (`solve_corrected_fov`) — see [`docs/reproject-fov-investigation.md`](docs/reproject-fov-investigation.md). |
| [`catalog.py`][catalog.py] | PDS ODE REST API client — lists EDR/CDR products by time range, matches EDR↔CDR pairs (`list_products`, `find_matching_cdr`). |
| [`config.py`][config.py] | `TrntestConfig`/`load_config()` — endpoints, paths, product IDs, tunables. TOML file + `TRNTEST_*` env var overrides. |
| [`control_network.py`][control_network.py] | Converts `pose_alignment.py`'s 2D tie points into ISIS control points for a `jigsaw` bundle adjustment — see [`docs/wac-jigsaw-investigation.md`](docs/wac-jigsaw-investigation.md). On the back burner, not wired into the main pipeline. |
| [`crater_depth.py`][crater_depth.py] | Robbins-crater depth measurement off a DEM (Breton et al. 2019) and the Stoffler et al. 2006 fresh-crater reference depth, for a `sharpness_ratio` grade — see [`docs/crater-grading.md`](docs/crater-grading.md). |
| [`crater_depth_batch.py`][crater_depth_batch.py] | Whole-database crater-depth precompute, tiled for cache coherence — see [`docs/crater-grading.md`](docs/crater-grading.md). Not yet run across the full database. |
| [`craters.py`][craters.py] | Robbins craters catalog overlay: fetches/caches the PDS4 CSV, builds a spatially-indexed GeoPackage, and returns ellipse polygons for a raster's footprint (`crater_overlay_layer`) — see [`docs/data-sources/robbins-craters.md`](docs/data-sources/robbins-craters.md). |
| [`dataset.py`][dataset.py] | Public multi-image API: `images_for_window()` evaluates EDR candidates over a time window (throttled/illumination-filtered); `generate_dataset()` renders the selected ones. |
| [`dataset_selection.py`][dataset_selection.py] | Orbit-level TRN-OD dataset selection (`notebooks/select_datasets.py`): picks multi-day, maneuver-free orbit spans jointly diverse in solar hour angle, then hands one selected window to `dataset.py`. |
| [`illumination.py`][illumination.py] | Sun/orbit geometry via SPICE (sun elevation/azimuth, sub-solar point, node-crossing search) plus the angle-wraparound math helpers `dataset_selection.py`/`plotting.py` use. |
| [`isis_wac.py`][isis_wac.py] | Steps a WAC EDR through ISIS3's own pipeline (`lrowac2isis`→`spiceinit`→`lrowaccal`→`framestitch`→`crop`→`cam2map`) as this project's real-WAC comparison path — see [`docs/external-tools.md`](docs/external-tools.md)'s ISIS Pushframe pipeline section. |
| [`lunaserv.py`][lunaserv.py] | Fetches DEM (Astropedia GLD100) + ortho (WAC_EMP PDS4) imagery for a camera's footprint and preps both for `sat_sim`, including Hapke-relit hillshade blending — see the module docstring and [`docs/data-sources/astropedia-gld100.md`](docs/data-sources/astropedia-gld100.md)/[`wac-emp-pds4.md`](docs/data-sources/wac-emp-pds4.md). |
| [`maneuver_detection.py`][maneuver_detection.py] | Detects likely propulsive maneuvers in LRO's reconstructed-orbit SPK via step changes in angular momentum/orbital energy (`find_maneuver_candidates`) — see the module docstring for the derivation. |
| [`orientation.py`][orientation.py] | Notebook-display-only north-up rotation (does not touch the sensor model). |
| [`plotting.py`][plotting.py] | Comparison-figure plotting: raw-pixel/geometry checks (`plot_render_vs_basemap`, `plot_overlay`/`plot_overlay_toggle`/`plot_zoom_blink`), a quantitative brightness diff (`compute_brightness_matched_diff`), and dataset-selection scatter plots. |
| [`pose_alignment.py`][pose_alignment.py] | Feature-matches a map-projected WAC crop against the basemap and fits a 2D correction (similarity/affine/homography) — see [`docs/wac-jigsaw-investigation.md`](docs/wac-jigsaw-investigation.md). On the back burner, not wired into the main pipeline. |
| [`product_registry.py`][product_registry.py] | Intermediate-product access-discipline primitives (`writes_product`/`reads_product`/`deletes_product`, `atomic_publish*`) — see [`docs/intermediate-product-discipline.md`](docs/intermediate-product-discipline.md). |
| [`render.py`][render.py] | Renders the synthetic image via ASP `sat_sim`, then converts the camera to a CSM Frame sidecar via `cam_gen` (`run_sat_sim`). |
| [`report.py`][report.py] | Per-entry HTML report helpers/pipeline (`generate_report`, `problem_flags`, ...) for `notebooks/report_template.py`, used by `TrnTestReport` below. Report content itself is still a first-pass minimal template. |
| [`session.py`][session.py] | `Session` facade — thin one-line delegators so notebook cells don't repeat `config=...`. |
| [`sfs_validation.py`][sfs_validation.py] | Cross-checks `lunaserv.hapke_shade_ortho` against ASP `sfs` run as an independent forward renderer, for DEM-aware ground truth on the Hapke shading math. |
| [`spice_kernels.py`][spice_kernels.py] | Selects/downloads the minimal SPICE kernel set for a date and furnishes it (`fetch_and_furnish`) — see [`docs/data-sources/spice-kernels-isis.md`](docs/data-sources/spice-kernels-isis.md)/[`spice-kernels-naif.md`](docs/data-sources/spice-kernels-naif.md). |
| [`subprocess_utils.py`][subprocess_utils.py] | `run_quiet` — runs ASP/ISIS subprocesses with captured, on-failure-only output. |
| [`tasks.py`][tasks.py] | Two `huey` (sqlite-backed) task queues driving `trn_dataset.py`'s `populate()`/`populate_via_workers()`, one per execution mode (`immediate=True` in-process vs. `immediate=False` multi-worker). |
| [`tie_points.py`][tie_points.py] | Projects the same 5 ground points (4 corners + center) into both the synthetic render and the WAC crop, for the comparison figure's explicit tie points (`select_tie_points`/`resolve_crop_pixels`). |
| [`trn_dataset.py`][trn_dataset.py] | `TrnTestDataSet`/`TrnTestEntry`/`TrnTestProduct` — a structured, resumable dataset folder; `populate()`/`populate_via_workers()` drive generation sequentially or across worker processes. `TrnTestProduct` covers all four product types (`TrnTestImage` subclasses `crop`/`hillshade`/`reproject`; `TrnTestReport` is the per-entry HTML report, default-on in `PRODUCT_TYPES`, self-ensuring its `hillshade` dependency). `write_index()` writes a dataset-wide `status.csv`/`reports/index.html` nav bar after each `populate*()` call. |
| [`wac.py`][wac.py] | Extracts a band-separated VIS mosaic from a WAC CDR product via manual byte offsets (`fetch_vis_mosaic`) — superseded by `isis_wac.py` as the demo's real-WAC comparison method, kept for its own test coverage. |
| [`wac_camera_model.py`][wac_camera_model.py] | Hand-rolled Python forward projector for the WAC Pushframe camera (ground-to-image) — see [`docs/wac-jigsaw-investigation.md`](docs/wac-jigsaw-investigation.md). On the back burner, not wired into the main pipeline. |

[cache.py]: src/trntest/cache.py
[camera.py]: src/trntest/camera.py
[catalog.py]: src/trntest/catalog.py
[config.py]: src/trntest/config.py
[control_network.py]: src/trntest/control_network.py
[crater_depth.py]: src/trntest/crater_depth.py
[crater_depth_batch.py]: src/trntest/crater_depth_batch.py
[craters.py]: src/trntest/craters.py
[dataset.py]: src/trntest/dataset.py
[dataset_selection.py]: src/trntest/dataset_selection.py
[illumination.py]: src/trntest/illumination.py
[isis_wac.py]: src/trntest/isis_wac.py
[lunaserv.py]: src/trntest/lunaserv.py
[maneuver_detection.py]: src/trntest/maneuver_detection.py
[orientation.py]: src/trntest/orientation.py
[plotting.py]: src/trntest/plotting.py
[pose_alignment.py]: src/trntest/pose_alignment.py
[product_registry.py]: src/trntest/product_registry.py
[render.py]: src/trntest/render.py
[report.py]: src/trntest/report.py
[session.py]: src/trntest/session.py
[sfs_validation.py]: src/trntest/sfs_validation.py
[spice_kernels.py]: src/trntest/spice_kernels.py
[subprocess_utils.py]: src/trntest/subprocess_utils.py
[tasks.py]: src/trntest/tasks.py
[tie_points.py]: src/trntest/tie_points.py
[trn_dataset.py]: src/trntest/trn_dataset.py
[wac.py]: src/trntest/wac.py
[wac_camera_model.py]: src/trntest/wac_camera_model.py

## Development history

See [`docs/history.md`](docs/history.md) for the phase-by-phase narrative — what was tried, what
broke, and how each design decision (framelet timing, sensor axis convention, catalog-driven
selection, the perf fixes in the sweep) was actually reached. Background/curiosity reading, not
required before making a change; this README and [`docs/data-sources.md`](docs/data-sources.md)/
[`docs/external-tools.md`](docs/external-tools.md) describe current behavior.

## About this project

This is a personal project to experiment with using coding agents like Claude Code. Although I
work for NASA, this project was conducted on personal time with personal computing resources
and no NASA internal data or code. (But it makes use of the excellent data that NASA provides
publicly through services like the Planetary Data System!) The project code is licensed under
the very permissive MIT No Attribution (MIT-0) license to make it as convenient as possible
for anyone to reuse.
