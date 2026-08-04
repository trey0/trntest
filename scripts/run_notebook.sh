#!/bin/sh
# Regenerates a jupytext-paired notebook's .ipynb from its .py source and re-executes it in
# place, so the committed .ipynb's code and outputs both reflect the current .py. Run this before
# committing any notebook change -- the pre-commit hook checks structural sync but can't cheaply
# verify output freshness (that requires actually re-executing, which this does).
#
# A full top-to-bottom `jupyter nbconvert --execute` assigns sequential execution_count values
# (1, 2, 3, ...) with no gaps -- the pre-commit hook's notebook sync check uses that as a proxy
# for "this was produced by a real full run" rather than ad-hoc interactive tinkering, so always
# go through this script rather than committing a notebook edited/re-run cell-by-cell in the
# JupyterLab UI.

set -e

if [ -z "$1" ]; then
    echo "usage: $0 <path/to/notebook.py>" >&2
    exit 1
fi

NOTEBOOK_PY="$1"
REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

NOTEBOOK_IPYNB="${NOTEBOOK_PY%.py}.ipynb"

docker compose -f docker/docker-compose.yml run --rm demo jupytext --to notebook "$NOTEBOOK_PY"
docker compose -f docker/docker-compose.yml run --rm demo jupyter nbconvert --to notebook \
    --execute --inplace "$NOTEBOOK_IPYNB"

echo "run_notebook: regenerated and re-executed $NOTEBOOK_IPYNB from $NOTEBOOK_PY." >&2
echo "run_notebook: review with 'git diff', then stage both files together and commit." >&2
