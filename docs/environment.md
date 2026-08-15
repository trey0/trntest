# Dev environment: ephemeral VPS, archive/restore

This repo is developed on an hourly-billed VPS that gets torn down at the end of most work
sessions (along with the Claude Code session itself, via `/clear`). The host provides just git and
Docker — everything else (GDAL/ASP/SPICE, Python deps) runs inside the Docker container described
in `README.md`, so the host stays disposable by design. A fresh session should assume no memory of
prior sessions beyond what's written down in this repo's docs and git history.

## What survives a teardown

`archive.sh` (`/root/trntest_ws/archive.sh` — not tracked in this git repo) tars
`apt-manual-packages.txt`, `.bashrc`, `.claude`, `.claude.json`, `notes.txt`, `.ssh`, and the
*entire* `trntest_ws` directory, then the user scp's the resulting tarball off-box. `restore.sh`
reconstitutes it on the next VPS. `archive.sh`/`restore.sh`/`clean.sh` themselves live one level
above this repo and aren't git-tracked, so they only persist by being swept into the tarball they
describe.

Everything under `trntest_ws` — not just the git repo — gets archived, as long as `archive.sh`
actually gets run before teardown. So "will this survive" isn't really the question day to day;
the question is where things belong.

## Docker images don't survive either

Like everything not under `trntest_ws`, the built Docker image itself isn't archived —
`docker compose build` on a fresh VPS rebuilds it from `docker/Dockerfile` from scratch every
session, regardless of how many times it was built on a prior (now-destroyed) VPS. Since the
ISIS/ALE integration (`docs/history.md` Phases 13–14), this build is meaningfully heavier than it
used to be: a `micromamba create` package solve + install (~1GB download) on top of the existing
ASP tarball fetch. Budget real time for the first `docker compose build` of a new session — it's
not instant. If that layer is ever touched again, note its own `micromamba clean --all --yes`
gotcha (must run in the *same* `RUN` layer as `micromamba create`, or Docker's layered filesystem
won't actually reclaim the space — cost a real 15.8GB→3GB image bloat the first time around; see
the Dockerfile's own comment there).

## Where things belong: source vs. large output

- **`src/trntest/`** is the git repo root — the project's public, clean, committed code. Only
  what's worth keeping in history goes here.
- **`src/scratch/`** is for source that isn't ready to be a real commit yet — spike code,
  exploratory scripts — but is still precious. It's outside the git repo (a different repo root
  entirely, so nothing here needs a `.gitignore` entry to stay untracked), but it's still archived
  along with everything else under `src/`. Nothing under `src/` — committed or not — should ever
  be bulk-deleted.
- **`trntest_ws/scratch/`** (sibling of `src/`, `cache/`, `output/`) is for large, disposable
  spike/experiment output — rendered artifacts, big intermediate data, downloaded blobs. Kept
  distinct from `output/`, whose existing meaning (see `docs/caching.md`) is the demo's own final
  rendered artifacts, not ad hoc spike data.

Everything outside `src/` — `cache/`, `output/`, `scratch/` — is explicitly fair game to be deleted
at any time, to save space before an `archive.sh` run or just to clean up. A fresh session should
never assume prior contents of these dirs are still there.

**One real exception worth knowing before deleting on autopilot**: `cache/astropedia/` holds a
single ~10GB DEM file (see `docs/caching.md`'s "Astropedia GLD100 caching" section) — still safe to
delete (it's fully re-fetchable), but re-fetching it is a genuine, non-trivial one-time cost, unlike
the rest of `cache/`'s small, fast-to-refetch WMS tiles/kernels. Worth deciding deliberately whether
to keep it archived (bigger tarball/scp) or delete-and-re-download-next-session, not just deleting it
reflexively along with everything else in a space-saving pass.

## Multi-agent worktrees

Multiple Claude Code sessions can now work this repo concurrently as **Claude Code worktrees**:
the main checkout stays at `<workspace>/src/trntest`, and each additional agent gets its own git
worktree checked out under `<workspace>/src/trntest/.claude/worktrees/<name>/` (its own working
tree and branch, e.g. `worktree-a1`, but sharing the same `.git` history/objects as the main
checkout). All of this — main checkout and every worktree — sits under the same `trntest_ws`
(`<workspace>`), so `cache/`, `output/`, and `scratch/` are physically shared, not per-agent copies.

- **`cache/` and `scratch/` should stay shared.** Re-fetching SPICE kernels/WMS tiles per agent
  would be wasteful, and `scratch/`'s contents are already self-namespaced (e.g.
  `scratch/notebook_runs/<name>_<timestamp>.log`), so concurrent agents writing there don't
  collide in practice.
- **`output/` must not be shared as-is** — it holds final rendered demo artifacts, and two agents
  writing to the same `output/` at once means one agent's run silently clobbers another's. Each
  worktree agent should write to its own `output/<worktree-name>/` subfolder instead (e.g.
  `trntest_ws/output/a1/`) so concurrent agents can't step on each other.
- **The Docker image tag/Compose project name must also be per-worktree.** `docker compose run
  --rm` rebuilds the image from whatever's currently on disk in that checkout; if two worktrees
  share one image tag, whichever agent's build finishes last silently becomes the image the
  *other* agent's `run --rm` picks up next, mid-task.

`docker-compose.yml` handles all three via env vars (`TRNTEST_HOST_CACHE_DIR`,
`TRNTEST_HOST_OUTPUT_DIR`, `TRNTEST_HOST_SCRATCH_DIR`, `TRNTEST_IMAGE_TAG`,
`COMPOSE_PROJECT_NAME`), with defaults that already suit the main checkout unchanged. Run
`scripts/setup_worktree_docker_env.sh` once in a new worktree, before the first `docker compose`
call there — it detects the worktree name from the checkout path (no manual path arithmetic) and
writes a gitignored `docker/.env` pointing cache/scratch at the shared roots and output/image
tag/project name at agent-specific ones. Re-run it any time; it's idempotent. Only the main
checkout is expected to run the long-lived `docker compose up` Jupyter Lab server; worktree agents
use `docker compose run --rm demo <cmd>` for one-off commands (same as the existing non-worktree
workflow), which doesn't publish ports, so there's no port contention to manage per worktree.

The pre-commit hook (`git config core.hooksPath githooks`) doesn't need re-running per worktree —
`core.hooksPath` lives in the shared `.git/config` (worktrees don't get their own by default, since
`extensions.worktreeConfig` isn't enabled here), so it's already active in every worktree
automatically.

### Other sharp edges when more than one agent is active

- **`docs/plan.md`/`docs/history.md`/`docs/data-sources.md` are shared narrative state.** AGENTS.md
  asks every agent to update these as it works; two agents doing so concurrently on separate
  worktree branches *will* produce merge conflicts when both branches land on `main`. Keep edits to
  these additive/localized (a new dated `history.md` entry, a small targeted edit elsewhere) rather
  than restructuring, so conflicts stay small and mergeable — resolving them is expected, not a sign
  something went wrong.
- **`notebooks/dataset_manifest.csv` is shared demo-selection state, not per-agent.** Re-running
  `data_set_selection.py` and committing its output changes which real image *every* subsequent
  `image_generation.py` run renders, for every agent and the user, not just yours. Fine to run
  read-only (e.g. while warming cache) without committing; don't commit a changed manifest unless
  you specifically mean to change the demo's target image.
- **If a merge conflict lands in a `.ipynb`, don't try to resolve it in the `.ipynb` itself** —
  its JSON diff isn't worth reading. Resolve the conflict in the paired `.py` (the real source of
  truth), then regenerate the `.ipynb` from scratch with `scripts/run_notebook.sh
  notebooks/<name>.py`. This is also why concurrent edits to the same notebook across agents are
  low-stakes as long as both sides touch the `.py`: whoever merges second just re-derives the
  `.ipynb`, no manual JSON surgery required.
- **`clean.sh`/`archive.sh`/`restore.sh` (`/root/trntest_ws/*.sh`, host-level, not git-tracked) are
  the user's own session-teardown/restore tools, run by hand after shutting down every agent** —
  not something an agent should ever invoke. `clean.sh` in particular is nuclear and not
  worktree-aware: it stops *every* running container, `docker system prune -a`s *every* image
  (including every other worktree's per-agent tag), and `rm -rf`s the entire shared `cache/`,
  `output/`, and `scratch/` — not just yours. If you think cleanup is needed, ask the user rather
  than running these (or equivalent manual `docker system prune`/`rm -rf` on those shared dirs)
  yourself.
- **`cache.fetch_astropedia_gld100`'s one-time ~10GB GLD100 download is not concurrency-safe** —
  unlike every other fetch path in `cache.py` (which downloads to a uniquely-named temp file before
  an atomic rename, so concurrent cold fetches of the same small file are safe, if slightly
  wasteful), this one deliberately resumes into a *stable* `<dest>.part` path so a `curl -C -` can
  continue an interrupted multi-GB transfer (see `docs/caching.md`). Two agents both triggering a
  cold fetch of this file at the same time will race on that same partial file. In practice this
  only matters once (it's cached forever after) — check `cache/astropedia/*.tif` already exists
  before kicking off a full pipeline run if you're unsure whether another agent got there first.
- **Docker images accumulate per worktree** (`trntest-lunar-demo-<name>`, several GB each once
  ISIS/ASP are installed — see "Docker images don't survive either" above). When a worktree's work
  is done and the worktree itself is removed, also `docker rmi trntest-lunar-demo-<name>` so stale
  per-agent images don't pile up; `docker system df` shows current usage.
- **Merging your own worktree branch into `origin/main` without a PR is normal here — but only
  when the user asks for it in that turn, and only your own branch.** This is a small team of
  agents working closely with the user, not a large/anonymous one, so the informal "just merge it
  in" workflow that implies is intentional — it doesn't need a PR. It's still the same kind of
  action as any other push/merge needing explicit confirmation (see the general safety guidance you
  already follow): never merge/push on your own initiative, and never merge, push, delete, or
  force-touch *another* worktree's branch yourself — that stays the user's call. Right after
  merging, message every other running agent that `origin/main` moved (see "Agent-to-agent
  messaging" below) so they know to pull at their next good stopping point, not mid-edit.
- **If you're told your worktree/agent name, verify it** with `git rev-parse --show-toplevel`
  (look for the `.claude/worktrees/<name>/` segment) rather than trusting it blindly — in a
  multi-agent conversation that name can be stale or simply wrong.

### Agent-to-agent messaging

This repo is worked by a small number of Claude Code agents at a time (the user plus a couple of
worktree agents), working closely enough that direct messages between agents — via the `ListAgents`
and `SendMessage` tools — are part of the normal workflow here, not just a break-glass fallback:

- **On startup, announce yourself.** Once you've verified your own worktree name (`git rev-parse
  --show-toplevel`), call `ListAgents` to see who else is currently running, then `SendMessage`
  each one a short note with your worktree/branch name and what you're about to work on. This is
  how agents learn they're not alone and avoid duplicate or conflicting work (e.g. two agents both
  editing `docs/plan.md`, or both about to trigger the same cold cache fetch — see the GLD100 race
  above).
- **After merging into `origin/main`, tell the others.** Message every other agent `ListAgents`
  shows: that you merged, a one-line summary of what changed, and that they should `git pull
  origin main` next time they hit a good stopping point (not mid-edit).
- **Message ad hoc whenever something you learn affects another agent's in-flight work** — those
  two triggers aren't the only ones. Examples: you found a bug in code another agent is likely
  about to run ("don't run `render.py` right now, it's producing corrupt output, fix incoming"),
  you're about to touch shared narrative state (`docs/plan.md`/`docs/history.md`/
  `docs/data-sources.md`, `notebooks/dataset_manifest.csv`) and want to flag it to avoid a
  collision, or you're about to do something slow/disruptive to the shared `trntest_ws` (a long
  cold fetch, anything touching `cache/`). When in doubt, send the message — it costs little;
  staying silent risks another agent burning time on stale state or a known-bad code path.
- This is deliberately informal: no ticket system, no required message format. Keep messages short,
  and skip them for anything purely local to your own worktree that doesn't touch shared state or
  another agent's branch.

## Why the separation matters

An in-progress spike (the ISIS/CSM `mapproject` investigation tracked as an open item in
`docs/plan.md`) was lost when its folder was deleted in a hurry to save space — its source code
and its large output files had been sitting in the same folder, so deleting "the big stuff" took
the source with it. The fix isn't "be more careful when deleting" — it's keeping source (under
`src/`, including `src/scratch/`) and large output (outside `src/`, e.g. `trntest_ws/scratch/`) in
physically separate locations from the start, so deleting the disposable stuff can never take the
precious stuff with it.
