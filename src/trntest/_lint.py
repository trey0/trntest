"""`trntest-lint` console script: runs ruff format --check, ruff check, and mypy over Python
files, plus a jupytext structural-sync check and a warning/error-output scan over any notebook
files, either the whole repo (--all), an explicit file list, or files changed vs. HEAD (--diff, the
default).
"""

import argparse
import json
import re
import subprocess
from pathlib import Path


def _git_lines(*args: str) -> list[str]:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
    return [line for line in result.stdout.splitlines() if line]


def _tracked_files(suffix: str) -> set[str]:
    return set(_git_lines("ls-files", "--", f"*{suffix}"))


def _untracked_files(suffix: str) -> set[str]:
    return set(_git_lines("ls-files", "--others", "--exclude-standard", "--", f"*{suffix}"))


def _diff_files(suffix: str) -> set[str]:
    """Tracked files with `suffix` changed vs. `HEAD` (added/copied/modified/renamed), plus
    untracked ones -- `git diff` alone misses untracked files entirely."""
    changed = set(_git_lines("diff", "--name-only", "--diff-filter=ACMR", "HEAD", "--", f"*{suffix}"))
    status_lines = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    untracked = {line[3:] for line in status_lines if line.startswith("??") and line.endswith(suffix)}
    return changed | untracked


def resolve_targets(parsed: argparse.Namespace) -> tuple[list[str], list[str]]:
    """Returns (py_files, ipynb_files)."""
    if parsed.all:
        return (
            sorted(_tracked_files(".py") | _untracked_files(".py")),
            sorted(_tracked_files(".ipynb") | _untracked_files(".ipynb")),
        )
    if parsed.files:
        py_files: list[str] = []
        ipynb_files: list[str] = []
        for f in parsed.files:
            if not Path(f).is_file():
                raise SystemExit(f"trntest-lint: not an existing file: {f}")
            if f.endswith(".py"):
                py_files.append(f)
            elif f.endswith(".ipynb"):
                ipynb_files.append(f)
            else:
                raise SystemExit(f"trntest-lint: not a .py or .ipynb file: {f}")
        return py_files, ipynb_files
    return sorted(_diff_files(".py")), sorted(_diff_files(".ipynb"))


def _paired_ipynb(py_file: str) -> str:
    return py_file[: -len(".py")] + ".ipynb"


def _paired_py(ipynb_file: str) -> str:
    return ipynb_file[: -len(".ipynb")] + ".py"


def _unchanged_from_head(path: str) -> bool:
    """Whether `path`'s working-tree content is identical to what's already committed."""
    return subprocess.run(["git", "diff", "--quiet", "HEAD", "--", path], check=False).returncode == 0


def _check_notebook_sync(py_files: list[str], ipynb_files: list[str]) -> int:
    """Checks jupytext-paired notebook files for: both halves of a pair staged together (unless
    the un-staged twin is already unchanged from HEAD -- e.g. re-running a notebook after an
    upstream code fix can refresh only its outputs, leaving the paired `.py` source identical to
    what's already committed, with nothing to stage), code/markdown content matching
    between the two formats, and an execution_count sequence consistent with a single clean
    top-to-bottom execute (the shape `scripts/run_notebook.sh` produces). Read-only -- never writes
    to any file, only reports problems and the fix command.
    """
    notebook_py = [f for f in py_files if f.startswith("notebooks/")]
    py_set = set(notebook_py)
    ipynb_set = set(ipynb_files)
    ok = True

    for py in notebook_py:
        twin = _paired_ipynb(py)
        if Path(twin).is_file() and twin not in ipynb_set and not _unchanged_from_head(twin):
            print(f"trntest-lint: {py} is staged but its paired {twin} is not -- stage both together.")
            ok = False
    for nb in ipynb_files:
        twin = _paired_py(nb)
        if Path(twin).is_file() and twin not in py_set and not _unchanged_from_head(twin):
            print(f"trntest-lint: {nb} is staged but its paired {twin} is not -- stage both together.")
            ok = False

    for nb in ipynb_files:
        twin = _paired_py(nb)
        if not Path(twin).is_file():
            continue
        # `--output -` (stdout) omits the paired-formats header line jupytext otherwise writes, so
        # compare against a real file written into the same directory instead (matches the
        # notebook's own path-pattern pairing config, e.g. `notebooks//py:percent`).
        scratch = Path(nb).with_name(Path(nb).stem + ".sync_check.py")
        try:
            result = subprocess.run(
                ["jupytext", "--to", "py:percent", "--output", str(scratch), nb],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                print(f"trntest-lint: failed to convert {nb} via jupytext for the sync check:\n{result.stderr}")
                ok = False
                continue
            if scratch.read_text() != Path(twin).read_text():
                print(
                    f"trntest-lint: {twin} and {nb} have diverged code/markdown content. "
                    f"Run `jupytext --sync {nb}`, review the result, re-stage both files, and commit again."
                )
                ok = False
        finally:
            scratch.unlink(missing_ok=True)

    for nb in ipynb_files:
        notebook = json.loads(Path(nb).read_text())
        counts = [cell.get("execution_count") for cell in notebook.get("cells", []) if cell.get("cell_type") == "code"]
        expected = list(range(1, len(counts) + 1))
        if counts != expected:
            print(
                f"trntest-lint: {nb}'s execution_count sequence {counts} doesn't look like a full "
                f"clean run (expected {expected}). Re-run `scripts/run_notebook.sh {_paired_py(nb)}` "
                "before committing, rather than committing an interactively-tinkered notebook."
            )
            ok = False

    return 0 if ok else 1


_WARNING_LINE = re.compile(r"Warning\b|\bWARNING\b")


def _check_notebook_warnings(ipynb_files: list[str]) -> int:
    """Scans each notebook's already-recorded cell outputs (reads the committed .ipynb -- doesn't
    execute anything) for raised errors or warning-looking stream text, so a notebook that's noisy
    or actually failing gets caught the same way the sync/execution_count checks already catch
    structural drift. Two independent signals, kept separate since they catch different things:
    any `stream` output with `name == "stderr"` (this is the exact cue Jupyter's own renderer uses
    to show a cell's output in red -- an unhandled exception logged by a background thread/process,
    e.g. a `huey` worker, never becomes an `output_type == "error"` cell, only a stderr stream, so
    checking `output_type` alone misses it), and, on any stream regardless of name, text literally
    containing "Warning"/"WARNING" (Python's own `*Warning:` lines, GDAL/ISIS's "Warning N: ..."
    messages) -- heuristic, not exhaustive, extend the pattern if a library's own warning convention
    slips through."""
    ok = True
    for nb in ipynb_files:
        notebook = json.loads(Path(nb).read_text())
        for i, cell in enumerate(notebook.get("cells", [])):
            if cell.get("cell_type") != "code":
                continue
            for output in cell.get("outputs", []):
                if output.get("output_type") == "error":
                    print(
                        f"trntest-lint: {nb} cell {i} raised {output.get('ename')}: "
                        f"{output.get('evalue')} -- fix and re-run scripts/run_notebook.sh."
                    )
                    ok = False
                elif output.get("output_type") == "stream":
                    text = "".join(output.get("text", []))
                    if output.get("name") == "stderr" and text.strip():
                        first_line = text.strip().splitlines()[0]
                        print(
                            f"trntest-lint: {nb} cell {i} wrote to stderr (renders red in Jupyter): "
                            f"{first_line.strip()[:200]}"
                        )
                        ok = False
                        continue
                    for line in text.splitlines():
                        if _WARNING_LINE.search(line):
                            print(f"trntest-lint: {nb} cell {i} output contains a warning: {line.strip()[:200]}")
                            ok = False
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="trntest-lint")
    parser.add_argument("--all", action="store_true", help="check every .py/.ipynb file in the repo")
    parser.add_argument("--diff", action="store_true", help="check files changed vs. HEAD (default)")
    parser.add_argument("files", nargs="*", help="explicit files to check (.py and/or .ipynb)")
    parsed = parser.parse_args()

    if parsed.files and (parsed.all or parsed.diff):
        parser.error("--all/--diff cannot be combined with explicit file arguments")
    if parsed.all and parsed.diff:
        parser.error("--all and --diff are mutually exclusive")

    py_files, ipynb_files = resolve_targets(parsed)
    if not py_files and not ipynb_files:
        print("no Python or notebook files to check")
        return 0

    results = {}
    if py_files:
        # Notebook .py twins used to suppress IPython's auto-display of a cell's last expression
        # (e.g. to hide a plot call's Figure repr) with a trailing semicolon -- `ruff format` strips
        # those as "redundant" regardless of per-file-ignores (those only affect `ruff check`, not
        # the formatter), which used to force notebook files out of format-checking entirely. Fixed
        # by suppressing display via `_ = expr` instead (an `Assign` node, not the bare `Expr` node
        # IPython's display hook checks for) -- format-safe, so notebook files are now checked the
        # same as everything else.
        # `--force-exclude`: ruff's own `[tool.ruff] exclude` (e.g. `old_notebooks/`, a frozen
        # archive out of scope for lint -- see its own README.md) only applies during
        # ruff's own directory discovery, not when files are passed explicitly on the command line
        # like this -- without this flag, an untracked/changed file under an excluded directory
        # would still get linted.
        results["ruff format --check"] = subprocess.run(
            ["ruff", "format", "--check", "--force-exclude", *py_files], check=False
        ).returncode
        # mypy always runs against the whole package, never scoped to a partial file list --
        # restricting mypy's own inputs is a known footgun when a changed file imports an
        # unmodified sibling module mypy then can't fully check in isolation.
        results["ruff check"] = subprocess.run(["ruff", "check", "--force-exclude", *py_files], check=False).returncode
        results["mypy"] = subprocess.run(["mypy", "src/trntest"], check=False).returncode
    # Scoped to `notebooks/` only, matching `py_files`'s own filtering inside `_check_notebook_sync`
    # (`notebook_py = [f for f in py_files if f.startswith("notebooks/")]`) -- without this,
    # `old_notebooks/` (a frozen archive out of scope for lint, see its own README.md)
    # would still get pairing-checked whenever its .ipynb files are passed explicitly (e.g. the
    # pre-commit hook, which lints exactly the staged file list, not `--diff` mode's own
    # `notebooks/`-agnostic untracked-file detection).
    notebook_ipynb_files = [f for f in ipynb_files if f.startswith("notebooks/")]
    if any(f.startswith("notebooks/") for f in py_files) or notebook_ipynb_files:
        results["notebook sync"] = _check_notebook_sync(py_files, notebook_ipynb_files)
        results["notebook warnings"] = _check_notebook_warnings(notebook_ipynb_files)

    for name, code in results.items():
        print(f"{name}: {'PASS' if code == 0 else 'FAIL'}")

    return 0 if all(code == 0 for code in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
