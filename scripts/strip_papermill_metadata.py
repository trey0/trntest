#!/usr/bin/env python3
"""Strip papermill's own `metadata.papermill` block (notebook-level and per-cell) from an already-
executed notebook, in place.

Papermill (unlike plain nbconvert) writes this block *in addition to* the standard `metadata.
execution` timing nbconvert/nbclient already records -- but jupytext embeds `metadata.papermill`'s
full JSON directly into the `.py:percent` cell marker line (`# %% papermill={...}`) on round-trip,
which the checked-in `.py` source (generated before execution, so it never has this key) doesn't
have -- a spurious sync failure `_lint.py`'s notebook-sync check would otherwise flag on every run.
`metadata.execution` isn't affected (jupytext already knows to leave it out of the marker line), so
`notebook_timing_report.py`'s own timing report keeps working unchanged after this runs.

Uses `nbformat`'s own read/write (not hand-rolled `json.load`/`json.dump`) so the file's
serialization stays byte-compatible with what Jupyter's own tools produce -- a manual re-dump risks
a different indent/key-order/newline convention, which would just trade one spurious diff for
another.
"""

import sys

import nbformat


def main() -> int:
    if len(sys.argv) != 2:  # noqa: PLR2004 -- argv length, not a domain magic value
        print(f"usage: {sys.argv[0]} <notebook.ipynb>", file=sys.stderr)
        return 1

    path = sys.argv[1]
    notebook = nbformat.read(path, as_version=4)
    notebook.metadata.pop("papermill", None)
    for cell in notebook.cells:
        cell.metadata.pop("papermill", None)
    nbformat.write(notebook, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
