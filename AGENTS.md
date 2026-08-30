# trntest — lunar remote sensing demo

Demo: synthetic lunar satellite imagery, posed using the real LRO SPICE trajectory, rendered with
NASA Ames Stereo Pipeline's `sat_sim` from real Lunaserv WMS DEM/imagery. Built as an AI-assisted
coding exercise — the user is an experienced developer but new to Claude Code.

**Read `docs/plan.md` first** for current architecture and status — a fast, scannable map of what
the system does and how `src/trntest/` is organized, kept up to date as work progresses (not just
this file). Then, as needed:

- `docs/data-sources.md` — a thin index (one table) into `docs/data-sources/`'s per-source files:
  endpoint URLs, WMS layer names/formats, NAIF SPICE archive layout, LROC WAC EDR/CDR access, and
  known gotchas for each. Consult before re-deriving any of this from scratch; update the relevant
  file when a concrete choice changes. `docs/external-tools.md` covers the same kind of thing for
  external tools/libraries (ASP, ISIS, `usgscsm`, LightGlue) rather than data. `docs/image-pipeline.md`,
  `docs/dataset-selection.md`, and `docs/crater-grading.md` hold architecture/algorithm detail that
  used to live in `data-sources.md` but isn't about external data at all.
- `docs/generators.md` — the canonical reference for the three TRN test image generators
  (`hillshade`/`crop`/`reproject`): a thin index table (data sources, processing steps, purpose)
  into `docs/generators/`'s per-generator files. `notebooks/image_generation.py` sources its own
  intro table from this; keep them in sync when either changes.
- `docs/caching.md` — why and how external data (SPICE kernels, WMS tiles) is cached locally
  instead of re-fetched; follow this pattern in any new fetch code.
- `docs/intermediate-product-discipline.md` — principles for naming, storing, and sharing generated
  (non-final, non-source) intermediate files across code paths — identity/ownership, storage
  hierarchy, atomicity, and access-mode discipline. Consult before adding a new intermediate artifact
  or a new code path that reads/writes an existing one.
- `docs/batch-generation.md` — the recommended workflow for populating a `TrnTestDataSet` at scale
  via `TrnTestDataSet.populate_via_workers()` (a real multi-worker pool, not `populate()`'s
  sequential default), and the concrete races/gotchas to watch out for when running one. Read this
  before running or advising on a large generator batch job.
- `docs/docs-style.md` — how to write docs and docstrings in this repo (brevity, what a docstring is
  and isn't for, why nothing should cite `docs/history.md`). Follow this when writing or editing any
  doc or docstring.
- `docs/proposed-tasks/` — forward-looking plans for not-yet-finished work (e.g.
  `report-plan.md`), as opposed to the reference docs elsewhere in `docs/` that describe current
  state. Put a new plan doc here instead of loose in `docs/`; when the work finishes, fold its
  content into the relevant current-state doc (or `docs/history.md`) and delete the plan, per its
  own usual "once resumed/done, delete or fold in" closing note.
- `docs/history.md` — the phase-by-phase development narrative (what was tried, what broke, how
  each design decision was reached). Background/curiosity reading only — not required before making
  a change, and nothing there should be taken as describing current behavior unless the docs above
  also say so.
- `old_notebooks/` — archived, frozen investigation notebooks (real executed `.ipynb` output kept,
  not re-run or kept in sync going forward — see its own `README.md`). Useful when a `docs/history.md`
  entry references one and you want the actual plots/reasoning trail, not just the narrative summary.
- `docs/environment.md` — this repo's VPS dev environment: why spike/experimental source
  (`src/scratch/`) and large file output (outside `src/` entirely, e.g. `trntest_ws/scratch/`) must
  be kept in separate locations, and (see its "Multi-agent worktrees" section) how concurrent Claude
  Code worktree agents share the outer `trntest_ws` workspace safely, merge into `origin/main`, and
  message each other directly to stay in sync. **Note:** the file's own "ephemeral VPS,
  archive/restore" framing is stale as of 2026-08-29 — the main data store now persists across
  sessions, `archive.sh`/`restore.sh` are no longer used — pending a fuller rewrite.

## Working conventions for this repo

- Everything that needs GDAL/ASP/SPICE runs **inside the Docker container** (`docker/`) — the host
  itself has no geospatial tooling installed and should stay that way. `docker compose run --rm demo
  <cmd>` (see `README.md`) for one-off commands; `docker compose up` for the Jupyter Lab server.
- **If you're running in a Claude Code worktree** (this session's checkout is
  `.claude/worktrees/<name>/`, not the main checkout — check `git rev-parse --show-toplevel`), run
  `scripts/setup_worktree_docker_env.sh` once before your first `docker compose` call in this
  session, and use your worktree's own `output/<name>/` subfolder for anything else you write under
  the shared `trntest_ws` (e.g. one-off files outside `output/`, if you ever need them) — the outer
  `trntest_ws` workspace (`cache/`, `output/`, `scratch/`) is shared with the main checkout and any
  other concurrent worktree agents, and `output/` in particular isn't safe to write to un-namespaced
  since two agents' runs would clobber each other there. `cache/`/`scratch/` are meant to stay
  shared (no per-agent copy needed). See `docs/environment.md`'s "Multi-agent worktrees" section for
  the full rationale and what the setup script does — including its "Other sharp edges" subsection
  (shared narrative docs/`dataset_manifest.csv` as merge-conflict risks, resolving `.ipynb` conflicts
  by merging the `.py` and regenerating rather than reading the JSON diff, why `clean.sh`/
  `archive.sh`/`restore.sh` are the user's own session-teardown tools and not something an agent
  should run, a real concurrency race in the one-time Astropedia GLD100 fetch, and per-worktree
  Docker image cleanup) if another agent might be active at the same time. Verify your own worktree
  name yourself (`git rev-parse --show-toplevel`) rather than trusting a
  name you're told — it can be stale in a multi-agent conversation.
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
- **Notebooks are jupytext-paired and both halves are committed.** Notably:
  `notebooks/image_generation.py`/`.ipynb` (the flagship demo — reads the checked-in, now-frozen
  `notebooks/dataset_manifest.csv` and renders/validates the selected image; see `docs/history.md`'s
  dated entry for why the notebook that used to regenerate that CSV was removed), and
  `notebooks/wac_isis.py`/`.ipynb` (the narrower ISIS/CSM `framestitch` investigation — see
  `docs/plan.md`'s open items). For each, the `.py` (percent format) is the source of truth for
  review/diffing/lint/IDE work; the `.ipynb` carries real, fully-executed outputs and is committed
  too — GitHub renders `.ipynb` natively in its file browser (markdown, code, and outputs,
  including images), so no separate HTML/Pages publishing step exists anymore. The two halves of a
  pair are linked via inline jupytext metadata (no `jupytext.toml`). Always edit through one of them
  (JupyterLab renders the `.py` as a live notebook via the bundled `jupyterlab-jupytext`
  extension) and run `scripts/run_notebook.sh <path/to/the/one/you/edited.py>` before committing —
  see the pre-commit hook note below for what is and isn't automatically checked. Notebook markdown
  should read as tutorial prose per `docs/docs-style.md`, not development-history narrative — when a
  notebook needs a product another notebook can generate, read it through this codebase's own
  generate-on-demand primitives (`TrnTestEntry.camera`/`.crop_result`/`.dem_ortho_result`,
  `TrnTestImage.generate()` — all idempotent resume-from-disk-or-fetch-fresh) rather than a raw path
  assuming a prior run, falling back to `TrnTestImage._require_generated()`'s fail-fast error only
  when that's not practical. A notebook's own markdown can go stale about what's still "open" as the
  underlying code changes elsewhere — re-verify a specific claim against `docs/plan.md` (or by
  re-running the notebook) before trusting it.
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
