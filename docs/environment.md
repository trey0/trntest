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

## Why the separation matters

An in-progress spike (the ISIS/CSM `mapproject` investigation tracked as an open item in
`docs/plan.md`) was lost when its folder was deleted in a hurry to save space — its source code
and its large output files had been sitting in the same folder, so deleting "the big stuff" took
the source with it. The fix isn't "be more careful when deleting" — it's keeping source (under
`src/`, including `src/scratch/`) and large output (outside `src/`, e.g. `trntest_ws/scratch/`) in
physically separate locations from the start, so deleting the disposable stuff can never take the
precious stuff with it.
