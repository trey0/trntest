#!/bin/sh
# Serves one dataset's whole folder (reports/ + logs/) over plain HTTP (python3 -m http.server),
# entirely outside JupyterLab's own server, as its own separate docker compose process on its own
# port.
#
# Prefer the jupyter-server-proxy-backed /output/... route on the *same* already-running JupyterLab
# server instead (config/jupyter_server_config.py, see docs/report-generation.md's "Viewing reports"
# section) unless you specifically don't want JupyterLab running at all -- it needs no separate port
# to coordinate in a multi-agent setup (reuses the worktree's own already-allocated
# TRNTEST_JUPYTER_PORT) and covers every dataset under output/ at once, not just one. Both exist for
# the same underlying reason: Jupyter Server's AuthenticatedFileHandler (the handler behind every
# /files/... response) unconditionally appends "sandbox allow-scripts" plus "frame-ancestors 'self'"
# to every file it serves, so no page reached via /files/... can ever embed another one in an
# iframe/frame -- what reports/index.html's nav bar (a fixed nav strip over a content <iframe>)
# needs to do. Python's http.server sets no CSP at all, so both routes sidestep the problem by using
# it as the actual server -- jupyter-server-proxy just proxies to the same kind of process from
# inside Jupyter's own port instead of running it standalone the way this script does.
#
# Usage: scripts/serve_reports.sh [port] [dataset_folder]
#   port           defaults to 8899 -- this repo's usual multi-agent caveat applies (see
#                  docs/environment.md's "Multi-agent worktrees" section): if another agent might be
#                  serving reports at the same time, ask the user which port to use rather than
#                  trusting this default to be free. (The /output/... route above doesn't have this
#                  problem at all.)
#   dataset_folder defaults to /workspace/output/trn_dataset (the flagship demo's dataset)
#
# Runs in the foreground -- Ctrl-C to stop. Once running, tunnel the port the same way as
# JupyterLab's own (ssh -L <port>:localhost:<port> <this-host>) and open
# http://localhost:<port>/reports/index.html.

set -e

PORT="${1:-8899}"
DATASET_FOLDER="${2:-/workspace/output/trn_dataset}"

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

echo "serve_reports: serving $DATASET_FOLDER on port $PORT -- open http://localhost:$PORT/reports/index.html" >&2

docker compose -f docker/docker-compose.yml run --rm -p "127.0.0.1:$PORT:$PORT" demo \
    python3 -m http.server "$PORT" --directory "$DATASET_FOLDER"
