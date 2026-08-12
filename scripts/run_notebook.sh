#!/bin/sh
# Regenerates a jupytext-paired notebook's .ipynb from its .py source and re-executes it in
# place, so the committed .ipynb's code and outputs both reflect the current .py. Run this before
# committing any notebook change -- the pre-commit hook checks structural sync but can't cheaply
# verify output freshness (that requires actually re-executing, which this does).
#
# A full top-to-bottom execution assigns sequential execution_count values (1, 2, 3, ...) with no
# gaps -- the pre-commit hook's notebook sync check uses that as a proxy for "this was produced by
# a real full run" rather than ad-hoc interactive tinkering, so always go through this script
# rather than committing a notebook edited/re-run cell-by-cell in the JupyterLab UI.
#
# Uses `papermill --log-output` (not `jupyter nbconvert --execute`) specifically so a long run's
# progress is visible live, not just after the fact -- nbconvert buffers everything and writes the
# .ipynb only once, at the very end, which made a real stuck-vs-just-slow run genuinely
# undiagnosable without resorting to `docker exec`-level process forensics (see docs/history.md's
# Phase 27 follow-up). `--request-save-on-cell-execute`/`--autosave-cell-every 30` make papermill
# write the .ipynb incrementally (after each cell, and periodically during a single long-running
# one) instead of only once at the end, so even the committed file itself shows real progress
# mid-run, not just the log. The run's live output is also `tee`'d to a kept log
# (scratch/notebook_runs/<name>_<timestamp>.log, plus rolling `_latest.log`/`_previous.log`
# convenience copies) so a slow run can be compared against its own last run's per-cell timings,
# not just eyeballed once and lost. Both nbconvert and papermill already record per-cell start/end
# timestamps in the output .ipynb's own cell metadata by default (`ExecutePreprocessor.
# record_timing`/papermill's own `papermill.duration`) -- but that's invisible in any normal
# notebook view (JupyterLab, GitHub, nbviewer) and in papermill's own live output, which shows cell
# output as it happens but not a duration breakdown -- notebook_timing_report.py closes that gap,
# printed to the terminal and appended to the same kept log right after execution.
#
# strip_papermill_metadata.py runs first: papermill (unlike plain nbconvert) writes its own
# `metadata.papermill` block per cell *in addition to* the standard `metadata.execution` timing --
# jupytext embeds that extra block into the `.py:percent` cell marker line on round-trip, which the
# checked-in `.py` (generated before execution) never has, so left alone it's a spurious notebook-
# sync failure on every single run. `metadata.execution` isn't touched, so the timing report above
# still works after stripping it.
#
# --cwd (the notebook's own directory, e.g. notebooks/) matches a live JupyterLab kernel's default
# working directory -- without it, papermill's kernel inherits this whole invocation's cwd (the
# Docker service's own `working_dir: /workspace`, i.e. the repo root), silently breaking any
# notebook that reads/writes a file via a plain relative path (e.g. image_generation.ipynb's
# `dataset_manifest.csv`, which works fine opened live in JupyterLab but raised a real
# FileNotFoundError here before this flag was added).

set -e

if [ -z "$1" ]; then
    echo "usage: $0 <path/to/notebook.py>" >&2
    exit 1
fi

NOTEBOOK_PY="$1"
REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

NOTEBOOK_IPYNB="${NOTEBOOK_PY%.py}.ipynb"
NOTEBOOK_NAME=$(basename "$NOTEBOOK_PY" .py)
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG_DIR="/workspace/scratch/notebook_runs"
LOG_FILE="$LOG_DIR/${NOTEBOOK_NAME}_${TIMESTAMP}.log"
LATEST_LOG="$LOG_DIR/${NOTEBOOK_NAME}_latest.log"
PREVIOUS_LOG="$LOG_DIR/${NOTEBOOK_NAME}_previous.log"

docker compose -f docker/docker-compose.yml run --rm demo jupytext --to notebook "$NOTEBOOK_PY"
docker compose -f docker/docker-compose.yml run --rm demo bash -c "
    set -eo pipefail
    mkdir -p '$LOG_DIR'
    [ -f '$LATEST_LOG' ] && cp '$LATEST_LOG' '$PREVIOUS_LOG'
    papermill '$NOTEBOOK_IPYNB' '$NOTEBOOK_IPYNB' --log-output --no-progress-bar \
        --cwd '$(dirname "$NOTEBOOK_IPYNB")' \
        --request-save-on-cell-execute --autosave-cell-every 30 2>&1 | tee '$LOG_FILE'
    python3 scripts/strip_papermill_metadata.py '$NOTEBOOK_IPYNB'
    python3 scripts/notebook_timing_report.py '$NOTEBOOK_IPYNB' 2>&1 | tee -a '$LOG_FILE'
    cp '$LOG_FILE' '$LATEST_LOG'
"

echo "run_notebook: regenerated and re-executed $NOTEBOOK_IPYNB from $NOTEBOOK_PY." >&2
echo "run_notebook: log saved to scratch/notebook_runs/$(basename "$LOG_FILE") (also _latest.log/_previous.log for quick diffing)." >&2
echo "run_notebook: review with 'git diff', then stage both files together and commit." >&2
