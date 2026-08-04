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
- When validating a change by running the notebook end-to-end, run `scripts/run_notebook.sh
  notebooks/lunar_sat_sim_demo.py` — it regenerates `notebooks/lunar_sat_sim_demo.ipynb` from the
  tracked `.py` source and re-executes it in place, so the results are immediately visible by
  opening that file in the user's already-running `docker compose up` Jupyter Lab server (no scp,
  no separate step on their end). Always go through this script rather than a bare
  `jupyter nbconvert --execute --inplace` when the `.py` may have changed — otherwise the `.ipynb`
  ends up executing stale code. Don't bother building/publishing an Artifact for this kind of
  internal validation check — it's slower and not worth the token cost when the user can just view
  the live notebook themselves.
- **Notebooks are jupytext-paired and both halves are committed.**
  `notebooks/lunar_sat_sim_demo.py` (percent format) is the source of truth for review/diffing/
  lint/IDE work; `notebooks/lunar_sat_sim_demo.ipynb` carries real, fully-executed outputs and is
  committed too — GitHub renders `.ipynb` natively in its file browser (markdown, code, and
  outputs, including images), so no separate HTML/Pages publishing step exists anymore. The two
  are paired via inline jupytext metadata (no `jupytext.toml`). Always edit through one of them
  (JupyterLab renders the `.py` as a live notebook via the bundled `jupyterlab-jupytext`
  extension) and run `scripts/run_notebook.sh` before committing — see the pre-commit hook
  note below for what is and isn't automatically checked.
- **Profiling**: use `cProfile`/`pstats` inside Docker (real SPICE/network) rather than guessing
  which optimization matters — see `docs/history.md` (Phase 10) for an example. When isolating a
  from-cold cost, compare **separate fresh `docker compose run` invocations**, not multiple calls
  within one script/process: SPICE's furnished-kernel tracking (`spice_kernels._loaded_kernels`) and
  `functools.cache` state persist across calls in one process, making a later call look artificially
  fast for reasons unrelated to what's being measured — this produced two wrong root-cause guesses
  in a row in Phase 10 before the mistake was caught. Also: before attributing slowness to a cold
  network/disk cache, check `find <cache-dir> -newermt "-N minutes"` to confirm what was actually
  freshly fetched, rather than assuming.
- `trntest-lint`'s notebook check verifies structural sync (the `.py`/`.ipynb` pair is staged
  together and their code/markdown content matches) and a run-shape heuristic (the `.ipynb`'s
  `execution_count`s look like one clean top-to-bottom execute, i.e. `1, 2, 3, ...` with no gaps —
  the shape `scripts/run_notebook.sh` produces). It does **not** verify true output freshness
  (that the outputs actually reflect the current code) — that would require re-executing the whole
  pipeline, which is slow (SPICE/WMS/`sat_sim` calls). Always run `scripts/run_notebook.sh`
  after editing notebook code, don't just rely on the hook passing.
