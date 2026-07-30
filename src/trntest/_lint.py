"""`trntest-lint` console script: runs ruff format --check, ruff check, and mypy over either the
whole repo (--all), an explicit file list, or files changed vs. HEAD (--diff, the default).
"""

import argparse
import subprocess
from pathlib import Path


def _git_lines(*args: str) -> list[str]:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
    return [line for line in result.stdout.splitlines() if line]


def _tracked_py_files() -> set[str]:
    return set(_git_lines("ls-files", "--", "*.py"))


def _untracked_py_files() -> set[str]:
    return set(_git_lines("ls-files", "--others", "--exclude-standard", "--", "*.py"))


def _diff_py_files() -> set[str]:
    changed = set(_git_lines("diff", "--name-only", "--diff-filter=ACMR", "HEAD", "--", "*.py"))
    status_lines = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    untracked = {line[3:] for line in status_lines if line.startswith("??") and line.endswith(".py")}
    return changed | untracked


def resolve_targets(parsed: argparse.Namespace) -> list[str]:
    if parsed.all:
        return sorted(_tracked_py_files() | _untracked_py_files())
    if parsed.files:
        for f in parsed.files:
            if not f.endswith(".py") or not Path(f).is_file():
                raise SystemExit(f"trntest-lint: not an existing .py file: {f}")
        return list(parsed.files)
    return sorted(_diff_py_files())


def main() -> int:
    parser = argparse.ArgumentParser(prog="trntest-lint")
    parser.add_argument("--all", action="store_true", help="check every .py file in the repo")
    parser.add_argument("--diff", action="store_true", help="check files changed vs. HEAD (default)")
    parser.add_argument("files", nargs="*", help="explicit files to check")
    parsed = parser.parse_args()

    if parsed.files and (parsed.all or parsed.diff):
        parser.error("--all/--diff cannot be combined with explicit file arguments")
    if parsed.all and parsed.diff:
        parser.error("--all and --diff are mutually exclusive")

    files = resolve_targets(parsed)
    if not files:
        print("no Python files to check")
        return 0

    # mypy always runs against the whole package, never scoped to a partial file list --
    # restricting mypy's own inputs is a known footgun when a changed file imports an unmodified
    # sibling module mypy then can't fully check in isolation.
    results = {
        "ruff format --check": subprocess.run(["ruff", "format", "--check", *files], check=False).returncode,
        "ruff check": subprocess.run(["ruff", "check", *files], check=False).returncode,
        "mypy": subprocess.run(["mypy", "src/trntest"], check=False).returncode,
    }
    for name, code in results.items():
        print(f"{name}: {'PASS' if code == 0 else 'FAIL'}")

    return 0 if all(code == 0 for code in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
