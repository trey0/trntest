#!/bin/sh
# Generates one entry's standalone HTML report by calling trntest.report.generate_report --
# the same jupytext/papermill/nbconvert pipeline TrnTestReport._generate_impl runs when a report is
# generated via dataset.populate().
#
# Unlike scripts/run_notebook.sh (which re-executes a notebook in place for review/commit), this
# always writes a fresh copy under <report_dir>/ -- notebooks/report_template.py itself is only
# ever read, never executed or synced to its own .ipynb (it contains literal {{ }} placeholders,
# not valid parameter defaults, so it can't run standalone; deliberately not committed as a paired
# notebook for this reason).
#
# Usage: scripts/generate_report.sh <entry_index> [dataset_folder] [report_dir]
#   dataset_folder defaults to /workspace/output/trn_dataset (the flagship demo's dataset)
#   report_dir     defaults to <dataset_folder>/reports/<product_id at that index>

set -e

if [ -z "$1" ]; then
    echo "usage: $0 <entry_index> [dataset_folder] [report_dir]" >&2
    exit 1
fi

ENTRY_INDEX="$1"
DATASET_FOLDER="${2:-/workspace/output/trn_dataset}"
REPORT_DIR="$3"

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

docker compose -f docker/docker-compose.yml run --rm demo python3 -c "
from trntest.config import load_config
from trntest.report import generate_report
from trntest.trn_dataset import TrnTestDataSet

dataset_folder = '$DATASET_FOLDER'
entry_index = $ENTRY_INDEX
report_dir = '$REPORT_DIR'
if not report_dir:
    entry = TrnTestDataSet.open(dataset_folder, load_config())[entry_index]
    report_dir = str(entry.dataset_folder / 'reports' / entry.product_id)
generate_report(dataset_folder, entry_index, report_dir)
print(f'generate_report: wrote {report_dir}/report.html')
"
