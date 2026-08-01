# trntest — lunar remote sensing demo

Demo: synthetic lunar satellite imagery, posed using the real LRO SPICE trajectory, rendered with
NASA Ames Stereo Pipeline's `sat_sim` from real Lunaserv WMS DEM/imagery. Built as an AI-assisted
coding exercise — the user is an experienced developer but new to Claude Code.

**Read `docs/plan.md` first** for current status and the phase-by-phase approach — it's the
living source of truth for what's done and what's next, kept up to date as work progresses (not
just this file). Then, as needed:

- `docs/data-sources.md` — endpoint URLs, WMS layer names/formats, ASP `sat_sim`/`.tsai` details,
  NAIF SPICE archive layout, LROC WAC EDR access. Consult before re-deriving any of this from
  scratch; update it when a concrete choice is made (exact EDR product, exact kernel files, etc.).
- `docs/caching.md` — why and how external data (SPICE kernels, WMS tiles) is cached locally
  instead of re-fetched; follow this pattern in any new fetch code.

## Working conventions for this repo

- Everything that needs GDAL/ASP/SPICE runs **inside the Docker container** (`docker/`) — the host
  itself has no geospatial tooling installed and should stay that way. `docker compose run --rm demo
  <cmd>` (see `README.md`) for one-off commands; `docker compose up` for the Jupyter Lab server.
- Update `docs/plan.md`'s phase checklist as phases complete, and record newly-learned facts
  (exact product IDs, kernel filenames, gotchas) in `docs/data-sources.md` rather than only in code
  comments or commit messages — this repo's docs are meant to carry context across sessions so a
  fresh Claude Code session doesn't have to re-derive it.
- The demo logic is an installable package, `src/trntest/` (see `pyproject.toml`) — not a flat
  `scripts/` directory. Endpoints/paths/product IDs live in `src/trntest/config.py`
  (`TrntestConfig`/`load_config()`), not hard-coded; `docs/data-sources.md`/`docs/caching.md`
  describe the underlying data-source facts these config values encode. Run `trntest-lint` (see
  README) before committing Python changes — `git config core.hooksPath githooks` wires this up as
  a pre-commit hook automatically. `cache/`/`output/` live outside this repo entirely (siblings of
  the outer workspace's `src/`, not inside this checkout).
- When validating a change with a full notebook render (`jupyter nbconvert --to notebook
  --execute`), write the executed output directly over
  `notebooks/lunar_sat_sim_demo.ipynb` itself — not a scratch/output path — so the results are
  immediately visible by opening that file in the user's already-running `docker compose up`
  Jupyter Lab server (no scp, no separate render step on their end). This matches the user's own
  pre-commit habit of rendering the notebook in place for manual validation. Don't bother
  building/publishing an Artifact for this kind of internal validation check — it's slower and not
  worth the token cost when the user can just view the live render themselves.
- **`nbstripout` is currently NOT actually wired up** despite the README/this-file's docs describing
  it as the convention: there's no `.gitattributes` (which `nbstripout --install` should add, to
  make the filter apply for any clone) and `git config --get filter.nbstripout.clean` is empty in
  this working tree — confirmed by the committed notebook at `HEAD` having baked-in outputs. Don't
  assume outputs get stripped at commit time; check `.gitattributes`/`git config` again before
  relying on this, and flag it to the user rather than silently assuming the documented convention
  is actually active.
