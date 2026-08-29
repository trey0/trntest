#!/usr/bin/env python3
"""Substitute `{{ name }}` placeholders in a jupytext `.py:percent` template and write the result.
See docs/proposed-tasks/report-plan.md and scripts/generate_report.sh, the only caller.

Usage: render_report_template.py <template.py> <output.py> key=value [key=value ...]
"""

import sys
from pathlib import Path

from trntest.report import render_template


def main() -> int:
    if len(sys.argv) < 3:  # noqa: PLR2004 -- argv length, not a domain magic value
        print(f"usage: {sys.argv[0]} <template.py> <output.py> key=value [key=value ...]", file=sys.stderr)
        return 1

    template_path, output_path, *pairs = sys.argv[1:]
    params = dict(pair.split("=", 1) for pair in pairs)
    Path(output_path).write_text(render_template(Path(template_path).read_text(), params))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
