#!/bin/sh
# Generates one entry's standalone HTML report by calling trntest.report.generate_report --
# the same jupytext/papermill/nbconvert pipeline TrnTestReport._generate_impl runs when a report is
# generated via dataset.populate(). See docs/proposed-tasks/report-plan.md for the design.
#
# Unlike scripts/run_notebook.sh (which re-executes a notebook in place for review/commit), this
# always writes a fresh copy under <report_dir>/ -- notebooks/report_template.py itself is only
# ever read, never executed or synced to its own .ipynb (it contains literal {{ }} placeholders,
# not valid parameter defaults, so it can't run standalone; deliberately not committed as a paired
# notebook for this reason -- see docs/proposed-tasks/report-plan.md).
#
# Usage: scripts/generate_report.sh <edr_product> [dataset_folder] [report_dir]
#   dataset_folder defaults to /workspace/output/trn_dataset (the flagship demo's dataset)
#   report_dir     defaults to <dataset_folder>/reports/<edr_product>

set -e

if [ -z "$1" ]; then
    echo "usage: $0 <edr_product> [dataset_folder] [report_dir]" >&2
    exit 1
fi

EDR_PRODUCT="$1"
DATASET_FOLDER="${2:-/workspace/output/trn_dataset}"
REPORT_DIR="${3:-$DATASET_FOLDER/reports/$EDR_PRODUCT}"

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

docker compose -f docker/docker-compose.yml run --rm demo python3 -c "
from trntest.report import generate_report
generate_report('$DATASET_FOLDER', '$EDR_PRODUCT', '$REPORT_DIR')
"

echo "generate_report: wrote $REPORT_DIR/report.html" >&2
