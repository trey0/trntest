#!/bin/sh
# Renders the demo notebook to static HTML and publishes it to the `gh-pages` branch for GitHub
# Pages hosting -- packages the exact steps from README's "Viewing the rendered demo" section so
# they don't have to be typed out (and gotten slightly wrong) by hand each time.
#
# `git subtree push --prefix docs/rendered` strips that prefix, so the published file lands at the
# *root* of the gh-pages branch (e.g. https://<user>.github.io/<repo>/lunar_sat_sim_demo.html, not
# nested under /docs/rendered/). Enable Pages once, in the repo's GitHub Settings: source =
# gh-pages branch, folder = / (root).
#
# Requires Docker (for the render step) and a working git/SSH setup for `origin` (for the commit +
# subtree push steps) -- same requirements as running the underlying commands by hand.

set -e

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

RENDERED_DIR="docs/rendered"
RENDERED_NAME="lunar_sat_sim_demo.html"

echo "publish_gh_pages: rendering notebook (re-executes the full pipeline -- SPICE/WMS/LROC" >&2
echo "fetches are cached, but the sat_sim render + WAC extraction still run fresh)..." >&2
# nbconvert's -o/--output is just a base filename, not a path -- it writes next to the input
# notebook unless --output-dir is given explicitly (learned the hard way: a first attempt at this
# script used `-o docs/rendered/lunar_sat_sim_demo.html` and silently wrote to notebooks/ instead).
docker compose -f docker/docker-compose.yml run --rm demo jupyter nbconvert --to html --execute \
    notebooks/lunar_sat_sim_demo.ipynb "--output-dir=$RENDERED_DIR" "--output=$RENDERED_NAME"

git add "$RENDERED_DIR"
if git diff --cached --quiet -- "$RENDERED_DIR"; then
    echo "publish_gh_pages: rendered output unchanged, nothing to commit -- skipping publish." >&2
    exit 0
fi

git commit -m "Update rendered demo output"
git subtree push --prefix docs/rendered origin gh-pages

echo "publish_gh_pages: pushed to origin/gh-pages." >&2
