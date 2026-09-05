# Documentation index

Every file in `docs/`, one line each. `README.md` and `AGENTS.md` call out the handful worth reading
up front; this is the complete list.

| Doc | Purpose |
|---|---|
| [`data-sources.md`](data-sources.md) | Index into `data-sources/`'s per-source files: endpoints, formats, coverage, gotchas for the DEM/ortho/WMS/SPICE/crater/WAC data this project depends on. |
| [`generators.md`](generators.md) | Index into `generators/`'s per-generator files for the three TRN test image generators (`hillshade`/`crop`/`reproject`). |
| [`external-tools.md`](external-tools.md) | Reference for external tool/library behavior (ASP `sat_sim`, ISIS, `usgscsm`, LightGlue) — flags, formats, sharp edges. |
| [`caching.md`](caching.md) | Why and how external data (SPICE kernels, WMS tiles) is cached locally instead of re-fetched. |
| [`intermediate-product-discipline.md`](intermediate-product-discipline.md) | Principles for naming, storing, and sharing generated intermediate files across code paths. |
| [`batch-generation.md`](batch-generation.md) | Workflow for populating a `TrnTestDataSet` at scale via `populate_via_workers()`, and the races/gotchas to watch for. |
| [`image-pipeline.md`](image-pipeline.md) | Architecture detail: how the synthetic camera is posed and the crop sized to match a real WAC swath. |
| [`dataset-selection.md`](dataset-selection.md) | Architecture detail: maneuver detection and orbit-search/candidate-filtering for TRN-OD dataset selection. |
| [`crater-grading.md`](crater-grading.md) | Crater depth measurement (Breton et al. 2019) and its batch precompute — the input to crater sharpness grading. |
| [`reproject-fov-investigation.md`](reproject-fov-investigation.md) | Investigation (resolved, merged) behind the `reproject` generator and a synthetic-camera FOV bug fix. |
| [`resolution-investigation.md`](resolution-investigation.md) | Investigation (resolved) into why `crop` used to visibly outresolve `hillshade`/`reproject`, and the fix. |
| [`wac-jigsaw-investigation.md`](wac-jigsaw-investigation.md) | Investigation behind the hand-rolled WAC-VIS camera-pose-alignment fallback, and why ISIS `jigsaw` can't be used for this camera. |
| [`docs-style.md`](docs-style.md) | How to write docs and docstrings in this repo — brevity, docstring scope, voice. |
| [`collaboration.md`](collaboration.md) | How a session should collaborate with the user: commit/merge timing, handling ad hoc spikes, presenting findings. |
| [`environment.md`](environment.md) | This repo's VPS dev environment: what persists across sessions, and multi-agent worktree conventions. |
| [`history.md`](history.md) | Phase-by-phase development narrative — what was tried, what broke. Background reading, not required before making a change. |

## `proposed-tasks/`

Forward-looking plans for not-yet-finished work. [`proposed-tasks/open-items.md`](proposed-tasks/open-items.md)
is the one persistent file — a running list of open questions/gaps, refreshed as items resolve.
Every other file in that folder is a single-task plan, deleted once the work it describes is done or
folded into a current-state doc, so this index doesn't link them individually — list the folder's
contents directly if you need to see what's in flight.
