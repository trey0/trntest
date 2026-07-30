# trntest — lunar remote sensing demo

Generates a synthetic lunar satellite image, posed using the real LRO SPICE trajectory at the
time of an actual LROC WAC image, rendered with NASA's Ames Stereo Pipeline (`sat_sim`) from real
DEM/imagery pulled from the Lunaserv WMS server. See `docs/plan.md` for the full approach and
status, and `CLAUDE.md` for how the docs in this repo are organized.

The demo logic is the installable `trntest` Python package (`src/trntest/`); the notebook
(`notebooks/lunar_sat_sim_demo.ipynb`) drives it via a small `Session` facade.

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

then open `http://localhost:8888` in a browser. Open `notebooks/lunar_sat_sim_demo.ipynb`.

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
docker compose run --rm demo nbstripout --install
```

This installs `trntest` in editable mode plus `ruff`, `mypy`, `pytest`, `nbstripout`, `jupyterlab`,
and `ipykernel`, and registers the `nbstripout` git filter (see "Viewing the rendered demo" below
for why). Lint/type-check/test-only, without the notebook/ASP/GDAL stack, also works in a plain
host venv with Python 3.11+:

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

`cache/` and `output/` are **not** part of this repo's own directory tree — they live one level
above the outer workspace's `src/` (e.g. `<workspace>/cache`, `<workspace>/output`, siblings of
`<workspace>/src/trntest`), an out-of-source, ROS-workspace-inspired layout. `docker-compose.yml`'s
volume mounts assume this repo is checked out at `<workspace>/src/trntest`; if you've cloned it
somewhere else, either recreate that wrapper directory, edit the two `../../../cache`/
`../../../output` mount lines in `docker/docker-compose.yml`, or just set
`TRNTEST_CACHE_ROOT`/`TRNTEST_OUTPUT_DIR`/a `trntest.toml` instead.

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
binaries, so it runs anywhere the dev dependencies are installed.

## Git pre-commit hook

This repo ships a pre-commit hook (`githooks/pre-commit`) that runs `trntest-lint` against
staged `.py` files before each commit. It uses git's built-in `core.hooksPath` mechanism (no
external `pre-commit` framework, no symlinks to set up). Enable it once per clone:

```sh
git config core.hooksPath githooks
```

If `trntest-lint` is on your `PATH` (host venv with the dev dependencies installed), it runs
directly; otherwise the hook falls back to running it inside Docker automatically, so it works on
any clone with just Docker installed -- no host-side Python setup required.

## Viewing the rendered demo

The git-tracked notebook has its outputs stripped (via the `nbstripout` filter set up above) so
diffs stay clean — source (code/markdown cells) and results (rendered plots) aren't mixed in the
same versioned file. To view a rendered copy, regenerate it inside Docker whenever the demo
changes meaningfully:

```sh
docker compose run --rm demo jupyter nbconvert --to html --execute \
    notebooks/lunar_sat_sim_demo.ipynb -o docs/rendered/lunar_sat_sim_demo.html
```

then publish `docs/rendered/` to a `gh-pages` branch for GitHub Pages hosting (enable Pages from
that branch once, in the repo's Settings):

```sh
git add docs/rendered && git commit -m "Update rendered demo output"
git subtree push --prefix docs/rendered origin gh-pages
```

(ReadTheDocs was considered instead, but rejected: rendering this notebook needs the full Docker
environment — SPICE kernels, live NASA archive fetches, the ASP binaries — which RTD's build
containers can't provide either, so it would only host a pre-executed copy same as this approach,
at the cost of a whole Sphinx project for no practical gain.)
