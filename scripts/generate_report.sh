#!/bin/sh
# Generates one entry's standalone HTML report from notebooks/report_template.py: substitutes its
# {{ }} placeholders (scripts/render_report_template.py), jupytext-syncs the result to a notebook,
# papermill executes it, then nbconvert renders it to HTML. See docs/report-plan.md for the design
# this implements -- a deliberately minimal first pass.
#
# Unlike scripts/run_notebook.sh (which re-executes a notebook in place for review/commit), this
# always writes a fresh copy under <report_dir>/ -- notebooks/report_template.py itself is only
# ever read, never executed or synced to its own .ipynb (it contains literal {{ }} placeholders,
# not valid parameter defaults, so it can't run standalone; deliberately not committed as a paired
# notebook for this reason -- see docs/report-plan.md).
#
# Usage: scripts/generate_report.sh <edr_product> [dataset_folder] [report_dir]
#   dataset_folder defaults to /workspace/output/trn_dataset (the flagship demo's dataset)
#   report_dir     defaults to /workspace/output/reports/<edr_product>

set -e

if [ -z "$1" ]; then
    echo "usage: $0 <edr_product> [dataset_folder] [report_dir]" >&2
    exit 1
fi

EDR_PRODUCT="$1"
DATASET_FOLDER="${2:-/workspace/output/trn_dataset}"
REPORT_DIR="${3:-/workspace/output/reports/$EDR_PRODUCT}"

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

# --ExtractOutputPreprocessor.enabled=True + --NbConvertApp.output_files_dir=images make nbconvert
# pull each cell's displayed figure out of the executed notebook's own embedded-base64 output and
# write it to a real images/<file>.png next to report.html, rewriting the <img> tag to that
# relative path -- ExtractOutputPreprocessor is nbconvert's own built-in mechanism for this (on by
# default for the markdown/RST/LaTeX exporters, just not HTML's), not something built here. See
# docs/report-plan.md's "Mechanism" section for how this was found and confirmed live.
docker compose -f docker/docker-compose.yml run --rm demo bash -c "
    set -eo pipefail
    mkdir -p '$REPORT_DIR'
    python3 scripts/render_report_template.py notebooks/report_template.py '$REPORT_DIR/report.py' \
        dataset_folder='$DATASET_FOLDER' edr_product='$EDR_PRODUCT'
    jupytext --to notebook '$REPORT_DIR/report.py' --output '$REPORT_DIR/report.ipynb'
    papermill '$REPORT_DIR/report.ipynb' '$REPORT_DIR/report.ipynb' \
        --cwd notebooks --log-output --no-progress-bar
    jupyter nbconvert --to html '$REPORT_DIR/report.ipynb' --output report.html \
        --ExtractOutputPreprocessor.enabled=True --NbConvertApp.output_files_dir=images \
        --TemplateExporter.exclude_input_prompt=True --TemplateExporter.exclude_output_prompt=True
"

echo "generate_report: wrote $REPORT_DIR/report.html" >&2
