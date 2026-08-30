"""Shared subprocess helper for the ASP wrapper modules (`render.py`, `lunaserv.py`)."""

import subprocess


def run_quiet(cmd: list[str]) -> None:
    """Like `subprocess.run(cmd, check=True)`, but captures stdout/stderr instead of letting them
    flood the caller's own output.

    :param cmd: Command and arguments to run.
    :raises subprocess.CalledProcessError: If the command exits non-zero -- stdout/stderr are
        printed first, so nothing useful is lost for debugging.
    """
    # ASP binaries are noisy by default (progress bars, verbose logs) and inherit the calling
    # process's own stdout/stderr, which would otherwise flood a notebook cell.
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="")
        result.check_returncode()
