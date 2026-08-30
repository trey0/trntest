# trntest — lunar remote sensing demo

Demonstrates various approaches for generating synthetic lunar satellite images for new camera
models from real LROC WAC imagery, Lunaserv WMS maps, and LRO SPICE trajectories, rendered
with NASA's Ames Stereo Pipeline tools (like `sat_sim` and `mapproject`). See `docs/plan.md`
for the full approach and status, and `AGENTS.md` for how the docs in this repo are organized.

The demo logic is the installable `trntest` Python package (`src/trntest/`); `notebooks/
image_generation.py`/`.ipynb` drives it via a small `Session` facade, reading the checked-in
`notebooks/dataset_manifest.csv` (a frozen selection of one favorable real LROC EDR image) and
rendering/validating it. Tracked as a jupytext-paired pair: the `.py` (percent format) is the
source of truth for review/diff/lint/IDE work, and the `.ipynb` (fully executed, viewable directly
in GitHub's file browser — no separate publishing step needed) carries the outputs.

`notebooks/select_datasets.py`/`.ipynb` is a separate, early-stage/exploratory notebook for picking
*multiple* maneuver-free multi-orbit TRN-OD test datasets -- see its own module docstring and
`docs/plan.md`.

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

then open `http://localhost:8888` in a browser. Open `notebooks/image_generation.py` — the bundled
`jupyterlab-jupytext` extension renders it as a live, editable notebook (equivalent to opening its
paired `.ipynb` directly). After making changes, run `scripts/run_notebook.sh notebooks/<name>.py`
to regenerate and re-execute the `.ipynb` before committing (see "Viewing the rendered demo"
below).

For one-off commands instead of the notebook server:

```sh
docker compose run --rm demo sat_sim --help
docker compose run --rm demo gdalinfo --version
```

`docker-compose.yml` mounts the repo at `/workspace`, and mounts `cache/`/`output/` (which live
*outside* this repo — see Configuration below) at `/workspace/cache`/`/workspace/output` — fetched
WMS tiles and SPICE kernels persist there across container rebuilds (see `docs/caching.md`).

## Development setup

Inside the Docker container (recommended — has GDAL/ASP/SPICE already):

```sh
docker compose run --rm demo pip install -e '.[dev]'
```

This installs `trntest` in editable mode plus `ruff`, `mypy`, `pytest`, `jupytext`, `jupyterlab`,
and `ipykernel`. jupytext's JupyterLab integration (`jupyterlab-jupytext`) registers automatically
as part of the `jupytext` install — no separate `jupyter labextension install` step. Lint/
type-check/test-only, without the notebook/ASP/GDAL stack, also works in a plain host venv with
Python 3.11+:

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

This repo ships a pre-commit hook (`githooks/pre-commit`) that runs `trntest-lint` against
staged `.py` files (`ruff format --check`, `ruff check`, `mypy`) and staged notebook `.py`/`.ipynb`
pairs (jupytext structural-sync check — see "Viewing the rendered demo" below) before each commit.
It uses git's built-in `core.hooksPath` mechanism (no external `pre-commit` framework, no symlinks
to set up). Enable it once per clone:

```sh
git config core.hooksPath githooks
```

If `trntest-lint` is on your `PATH` (host venv with the dev dependencies installed), it runs
directly; otherwise the hook falls back to running it inside Docker automatically, so it works on
any clone with just Docker installed -- no host-side Python setup required.

## Viewing the rendered demo

The git-tracked `notebooks/image_generation.ipynb` carries fully-executed outputs and renders
natively in GitHub's file browser (markdown, code, and outputs, including images) — just
click the file in the repo. No separate publishing step, HTML build, or GitHub Pages setup.

`notebooks/image_generation.py` (jupytext percent format, paired with its own `.ipynb` via inline
metadata) is the actual source of truth: it's what you edit, what gets `ruff`/`mypy`-checked, and
what stays diffable — the `.ipynb`'s own diff will always be noisy since it carries outputs, which
is expected. After editing the notebook, regenerate and re-execute its `.ipynb` before committing:

```sh
scripts/run_notebook.sh notebooks/image_generation.py
```

The pre-commit hook checks that the `.py`/`.ipynb` pair is staged together, that their code/
markdown content actually matches, and that the `.ipynb`'s `execution_count`s look like a single
clean top-to-bottom run (the shape this script produces) — but it can't cheaply verify that the
outputs are *fresh* relative to the code (that needs a real re-execution, which is slow: SPICE/WMS/
`sat_sim` calls). Always run the script above after a code change rather than relying on the hook
alone.

## About this project

This is a personal project to experiment with using coding agents like Claude Code. Although I
work for NASA, this project was conducted on personal time with personal computing resources
and no NASA internal data or code. (But it makes use of the excellent data that NASA provides
publicly through services like the Planetary Data System!) The project code is licensed under
the very permissive MIT No Attribution (MIT-0) license to make it as convenient as possible
for anyone to reuse.
