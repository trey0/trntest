"""Shared subprocess helper for the ASP/ISIS wrapper modules (`render.py`, `hapke.py`, `dem_ortho.py`)."""

import shlex
import subprocess

from trntest import trace


def run_quiet(cmd: list[str]) -> None:
    """Like `subprocess.run(cmd, check=True)`, but captures stdout/stderr instead of letting them
    flood the caller's own output.

    :param cmd: Command and arguments to run.
    :raises subprocess.CalledProcessError: If the command exits non-zero -- stdout/stderr are
        printed first, so nothing useful is lost for debugging.
    """
    # ASP binaries are noisy by default (progress bars, verbose logs) and inherit the calling
    # process's own stdout/stderr, which would otherwise flood a notebook cell.
    if trace.enabled():
        # Every call site (isis_wac.py/render.py/hapke.py/dem_ortho.py/report.py) runs through
        # here, so this one line traces every external command this project invokes -- shlex.join,
        # not " ".join, so a path containing spaces is still unambiguous.
        print("+ " + shlex.join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="")
        result.check_returncode()
