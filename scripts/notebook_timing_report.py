#!/usr/bin/env python3
"""Print a per-cell execution-time summary for a notebook already run via papermill/nbconvert.

Both record real per-cell `metadata.execution.iopub.execute_input`/`shell.execute_reply`
timestamps by default -- but that data is invisible in any normal notebook view (JupyterLab,
GitHub, nbviewer) and in papermill/nbconvert's own live output stream, which shows cell
output/progress as it happens but never a clean per-cell duration breakdown afterward. This closes
that gap: called from scripts/run_notebook.sh right after execution, but safe to run standalone
against any already-executed notebook.
"""

import json
import sys
from datetime import datetime
from pathlib import Path


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def cell_durations(notebook: dict) -> list[tuple[int | None, float, str]]:
    """(execution_count, duration_seconds, first-line-of-source) for every code cell carrying real
    execution timing metadata, in notebook (execution) order."""
    rows = []
    for cell in notebook.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        execution = cell.get("metadata", {}).get("execution")
        if not execution:
            continue
        start = execution.get("iopub.execute_input")
        end = execution.get("shell.execute_reply")
        if not start or not end:
            continue
        duration_s = (_parse(end) - _parse(start)).total_seconds()
        source_lines = cell.get("source", [""])
        first_line = (source_lines[0] if source_lines else "").strip()[:70]
        rows.append((cell.get("execution_count"), duration_s, first_line))
    return rows


def main() -> int:
    if len(sys.argv) != 2:  # noqa: PLR2004 -- argv length, not a domain magic value
        print(f"usage: {sys.argv[0]} <notebook.ipynb>", file=sys.stderr)
        return 1

    notebook = json.loads(Path(sys.argv[1]).read_text())
    rows = cell_durations(notebook)
    if not rows:
        print("notebook_timing_report: no cells with execution timing metadata found.")
        return 0

    total_s = sum(duration_s for _, duration_s, _ in rows)
    print(f"{'cell':>4}  {'seconds':>9}  source")
    for count, duration_s, first_line in rows:
        print(f"{count!s:>4}  {duration_s:9.2f}  {first_line}")
    print(f"{'':>4}  {total_s:9.2f}  TOTAL ({len(rows)} cells)")

    slowest = sorted(rows, key=lambda r: -r[1])[:5]
    print("\nslowest cells:")
    for count, duration_s, first_line in slowest:
        print(f"  cell {count}: {duration_s:.2f}s  {first_line}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
