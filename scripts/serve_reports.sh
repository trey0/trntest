#!/bin/sh
# Serves one dataset's reports/ folder over plain HTTP (python3 -m http.server), entirely outside
# JupyterLab's own server.
#
# Necessary, not just convenient: Jupyter Server's AuthenticatedFileHandler (the handler behind
# every /files/... response) unconditionally appends "sandbox allow-scripts" to every file it
# serves, deliberately giving served HTML an opaque origin so it can never impersonate the Jupyter
# server itself. Combined with every file's own "frame-ancestors 'self'", this makes it structurally
# impossible for any page Jupyter serves to embed another page Jupyter serves in an iframe/frame --
# an opaque origin can never satisfy 'self'. That's what reports/index.html's nav bar (a fixed nav
# strip over a content <iframe>) needs to do, so it can never work through JupyterLab's own server,
# no matter what CSP/CORS config is changed there (this is NOT the same issue as the earlier
# report-link 403 that --ServerApp.allow_origin='*' fixed -- that one really was fixable
# server-side; this one isn't). Python's http.server sets no CSP at all, so this sidesteps the
# problem entirely rather than working around it.
#
# Usage: scripts/serve_reports.sh [port] [dataset_folder]
#   port           defaults to 8899 -- this repo's usual multi-agent caveat applies (see
#                  docs/environment.md's "Multi-agent worktrees" section): if another agent might be
#                  serving reports at the same time, ask the user which port to use rather than
#                  trusting this default to be free.
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
