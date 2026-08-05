"""Shared subprocess helper for the ASP wrapper modules (`render.py`, `lunaserv.py`)."""

import subprocess


def run_quiet(cmd: list[str]) -> None:
    """Like `subprocess.run(cmd, check=True)`, but captures stdout/stderr instead of letting them
    flood the notebook cell (ASP binaries are noisy -- progress bars, verbose logs -- and inherit
    the kernel's own stdout/stderr by default). Printed only on failure, so nothing useful is lost
    for debugging."""
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="")
        result.check_returncode()
