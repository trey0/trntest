# trntest — lunar remote sensing demo

Demo: synthetic lunar satellite imagery, posed using the real LRO SPICE trajectory, rendered with
NASA Ames Stereo Pipeline's `sat_sim` from real Lunaserv WMS DEM/imagery. Built as an AI-assisted
coding exercise — the user is an experienced developer but new to Claude Code.

**Read `docs/plan.md` first** for current architecture and status — a fast, scannable map of what
the system does and how `src/trntest/` is organized, kept up to date as work progresses (not just
this file). Then, as needed:

- `docs/data-sources.md` — current, stable facts: endpoint URLs, WMS layer names/formats, ASP
  `sat_sim`/`.tsai` details, NAIF SPICE archive layout, LROC WAC EDR/CDR access, and known gotchas.
  Consult before re-deriving any of this from scratch; update it when a concrete choice changes.
- `docs/caching.md` — why and how external data (SPICE kernels, WMS tiles) is cached locally
  instead of re-fetched; follow this pattern in any new fetch code.
- `docs/history.md` — the phase-by-phase development narrative (what was tried, what broke, how
  each design decision was reached). Background/curiosity reading only — not required before making
  a change, and nothing there should be taken as describing current behavior unless the docs above
  also say so.
- `old_notebooks/` — archived, frozen investigation notebooks (real executed `.ipynb` output kept,
  not re-run or kept in sync going forward — see its own `README.md`). Useful when a `docs/history.md`
  entry references one and you want the actual plots/reasoning trail, not just the narrative summary.
- `docs/environment.md` — the ephemeral VPS/archive-restore workflow this repo is developed under:
  what survives a teardown, and why spike/experimental source (`src/scratch/`) and large file
  output (outside `src/` entirely, e.g. `trntest_ws/scratch/`) must be kept in separate locations.

## Working conventions for this repo

- Everything that needs GDAL/ASP/SPICE runs **inside the Docker container** (`docker/`) — the host
  itself has no geospatial tooling installed and should stay that way. `docker compose run --rm demo
  <cmd>` (see `README.md`) for one-off commands; `docker compose up` for the Jupyter Lab server.
- Keep `docs/plan.md`'s architecture/status current as things change, and record newly-learned facts
  (exact product IDs, kernel filenames, gotchas) in `docs/data-sources.md` rather than only in code
  comments or commit messages — this repo's docs are meant to carry context across sessions so a
  fresh Claude Code session doesn't have to re-derive it. For substantial new work (a new bug found
  and fixed, a new capability), add a dated entry to `docs/history.md` too, the same way past work is
  recorded there — keep `plan.md`/`data-sources.md` themselves scoped to current-state facts, not
  narrative.
- The demo logic is an installable package, `src/trntest/` (see `pyproject.toml`) — not a flat
  `scripts/` directory. Endpoints/paths/product IDs live in `src/trntest/config.py`
  (`TrntestConfig`/`load_config()`), not hard-coded; `docs/data-sources.md`/`docs/caching.md`
  describe the underlying data-source facts these config values encode. Run `trntest-lint` (see
  README) before committing Python changes — `git config core.hooksPath githooks` wires this up as
  a pre-commit hook automatically. `cache/`/`output/` live outside this repo entirely (siblings of
  the outer workspace's `src/`, not inside this checkout).
- When validating a change by running a notebook end-to-end, run `scripts/run_notebook.sh
  notebooks/<name>.py` — it regenerates `notebooks/<name>.ipynb` from the
  tracked `.py` source and re-executes it in place (via `papermill --log-output`, not a bare
  `jupyter nbconvert --execute` — streams live cell-by-cell progress/output and per-cell timing
  instead of buffering everything until the run finishes or hangs; see docs/history.md's Phase 27
  follow-up), so the results are immediately visible by opening that file in the user's
  already-running `docker compose up` Jupyter Lab server (no scp, no separate step on their end).
  Always go through this script rather than invoking a notebook runner directly when the `.py` may
  have changed — otherwise the `.ipynb` ends up executing stale code. A per-cell timing report
  prints at the end and is appended to a kept log (`scratch/notebook_runs/<name>_<timestamp>.log`,
  plus rolling `_latest.log`/`_previous.log`) — check `_previous.log` if a run seems slower than
  expected, or `docker exec`/`docker stats` on the running container if it seems to genuinely hang
  (confirm real CPU/IO activity, not just elapsed time, before assuming something's actually stuck).
  Don't bother building/publishing an Artifact for this kind of
  internal validation check — it's slower and not worth the token cost when the user can just view
  the live notebook themselves.
- **Notebooks are jupytext-paired and both halves are committed.** There are three:
  `notebooks/data_set_selection.py`/`.ipynb` (catalog-driven EDR selection; its last cell writes
  the selected candidate table to the checked-in `notebooks/dataset_manifest.csv`),
  `notebooks/image_generation.py`/`.ipynb` (the flagship demo — reads `dataset_manifest.csv` and
  renders/validates the selected image; no runtime dependency on `data_set_selection.ipynb`
  itself, so rerun that notebook and commit its updated manifest to change which real image gets
  rendered), and `notebooks/wac_isis_spike.py`/`.ipynb` (the narrower ISIS/CSM `framestitch`
  investigation — see `docs/plan.md`'s open items). For each, the `.py` (percent format) is the source of truth for
  review/diffing/lint/IDE work; the `.ipynb` carries real, fully-executed outputs and is committed
  too — GitHub renders `.ipynb` natively in its file browser (markdown, code, and outputs,
  including images), so no separate HTML/Pages publishing step exists anymore. The two halves of a
  pair are linked via inline jupytext metadata (no `jupytext.toml`). Always edit through one of them
  (JupyterLab renders the `.py` as a live notebook via the bundled `jupyterlab-jupytext`
  extension) and run `scripts/run_notebook.sh <path/to/the/one/you/edited.py>` before committing —
  see the pre-commit hook note below for what is and isn't automatically checked.
- **New subprocess calls to ASP/ISIS binaries must use `trntest.subprocess_utils.run_quiet`**, not
  raw `subprocess.run`. These tools are noisy by default (progress bars, library-init messages,
  verbose logs) and inherit the calling process's own stdout/stderr, which floods a notebook cell
  with output that isn't the caller's — `run_quiet` captures it and only surfaces it on failure.
  `render.py`, `lunaserv.py`, and `isis_wac.py` all follow this pattern; a real, painful example of
  what happens without it is in `docs/history.md`'s notebook-warnings-cleanup entries.
- **Profiling**: use `cProfile`/`pstats` inside Docker (real SPICE/network) rather than guessing
  which optimization matters — see `docs/history.md` (Phase 10) for an example. When isolating a
  from-cold cost, compare **separate fresh `docker compose run` invocations**, not multiple calls
  within one script/process: SPICE's furnished-kernel tracking (`spice_kernels._loaded_kernels`) and
  `functools.cache` state persist across calls in one process, making a later call look artificially
  fast for reasons unrelated to what's being measured — this produced two wrong root-cause guesses
  in a row in Phase 10 before the mistake was caught. Also: before attributing slowness to a cold
  network/disk cache, check `find <cache-dir> -newermt "-N minutes"` to confirm what was actually
  freshly fetched, rather than assuming.
- `trntest-lint`'s notebook checks: structural sync (the `.py`/`.ipynb` pair is staged together —
  unless the un-staged twin is already byte-identical to `HEAD`, e.g. a notebook re-run that only
  refreshed outputs — and their code/markdown content matches), a run-shape heuristic (the
  `.ipynb`'s `execution_count`s look like one clean top-to-bottom execute, i.e. `1, 2, 3, ...` with
  no gaps — the shape `scripts/run_notebook.sh` produces), and a warning/error scan (the `.ipynb`'s
  already-recorded cell outputs are checked for raised errors or warning-looking stream text —
  heuristic, matches on literal "Warning"/"WARNING" substrings, not exhaustive). None of these
  verify true output freshness (that the outputs actually reflect the current code) — that would
  require re-executing the whole pipeline, which is slow (SPICE/WMS/`sat_sim`/ISIS calls). Always
  run `scripts/run_notebook.sh` after editing notebook code, don't just rely on the hook passing.
