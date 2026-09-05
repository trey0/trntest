"""Intermediate-product access-discipline primitives -- see `docs/intermediate-product-discipline.md`
for the principles this implements.

Two independent pieces, both pure infrastructure with no behavior change of their own until
writers/readers/deleters are decorated:

- `writes_product`/`reads_product`/`deletes_product`: a lightweight registry making "which code path
  writes/reads/deletes this label" a legible, checkable fact (principle 7) rather than something only
  recoverable by reading every caller. `writes_product` additionally enforces principle 2 (exactly one
  writer per label) at decoration time.
- `atomic_publish`/`atomic_publish_path`/`atomic_publish_prefix`: generalizes `cache.cached_get`'s
  existing temp-file-then-atomic-rename pattern (see that function's own docstring) from fetched
  files to generated ones, so a reader never observes a partially-written artifact (principle 4).
  Three shapes for three different writer conventions: a caller-chosen exact path
  (`atomic_publish`), a tool's own `to=`/`-o` exact-path parameter (`atomic_publish_path`), and a
  tool's own output-*prefix* parameter that gets its own fixed suffix appended
  (`atomic_publish_prefix`).
"""

import contextlib
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path


class ProductRegistryError(Exception):
    """Raised by `writes_product` when a label already has a registered writer -- principle 2 ("exactly
    one code path writes any given label") caught at decoration time (i.e. import time, for
    module-level decorator use) rather than left to be discovered later by a runtime collision."""


_WRITERS: dict[str, Callable] = {}
_READERS: dict[str, list[Callable]] = {}
_DELETERS: dict[str, list[Callable]] = {}


def writes_product(label: str) -> Callable[[Callable], Callable]:
    """Registers the decorated function as `label`'s sole writer. `label` names an artifact *family*
    (e.g. `"dem_filled"`, `"ortho_shaded"`) -- a plain string, not a filename -- so a function that
    legitimately produces multiple variants of one family (principle 1's "intentional-variant
    artifacts") still registers once, as that family's one owner (principle 2's "one owner, not one
    file"). Raises `ProductRegistryError` if `label` already has a writer registered. Returns the
    function unchanged -- pure metadata, no wrapping, no behavior change."""

    def decorator(fn: Callable) -> Callable:
        if label in _WRITERS:
            existing = _WRITERS[label]
            raise ProductRegistryError(
                f"label {label!r} already has a writer ({existing.__module__}.{existing.__qualname__}) "
                f"-- cannot also register {fn.__module__}.{fn.__qualname__}"
            )
        _WRITERS[label] = fn
        return fn

    return decorator


def reads_product(label: str) -> Callable[[Callable], Callable]:
    """Registers the decorated function as one of `label`'s readers. Unlike `writes_product`, any
    number of readers may register for the same label (principle 5: any number of concurrent readers
    is fine). Returns the function unchanged."""

    def decorator(fn: Callable) -> Callable:
        _READERS.setdefault(label, []).append(fn)
        return fn

    return decorator


def deletes_product(label: str) -> Callable[[Callable], Callable]:
    """Registers the decorated function as one of `label`'s deleters. Multiple registrants allowed,
    same as `reads_product` -- principle 5 only requires a *single* deleter be active at any one point
    in time, not that only one function in the codebase is ever capable of deleting. Returns the
    function unchanged."""

    def decorator(fn: Callable) -> Callable:
        _DELETERS.setdefault(label, []).append(fn)
        return fn

    return decorator


def writer_of(label: str) -> Callable | None:
    """The registered writer for `label`, or `None` if nothing has registered one."""
    return _WRITERS.get(label)


def readers_of(label: str) -> list[Callable]:
    """The registered readers for `label`, in registration order. Empty if none registered."""
    return list(_READERS.get(label, []))


def deleters_of(label: str) -> list[Callable]:
    """The registered deleters for `label`, in registration order. Empty if none registered."""
    return list(_DELETERS.get(label, []))


@contextlib.contextmanager
def atomic_publish(dest: Path) -> Iterator[Path]:
    """Yields a uniquely-named temp path in `dest`'s parent directory for the caller to write to
    directly (e.g. `rasterio.open(tmp, "w", ...)`). On clean exit, atomically renames the temp path to
    `dest` (principle 4: nothing is visible at `dest` until it's complete). On any exception, the temp
    path (file or directory) is removed and the exception re-raised -- `dest` is never partially
    written or left in a torn state.
    """
    # Same unique-temp-name mechanism as cache.cached_get (tempfile.mkstemp), generalized from
    # fetched files to generated ones -- see that function's own docstring for why a fixed shared
    # temp path per destination is unsafe under concurrent/rapid sequential use. Unlike
    # cached_get's own <dest.name>.<random>.part, the temp name here preserves dest's suffix
    # (<dest.stem>.tmp.<random><dest.suffix>) instead of a generic .tmp: some callers
    # (format-sniffing tools, or correctness in general) care about the extension, not just this
    # helper's own rasterio/subprocess call sites that happen not to (see atomic_publish_path's own
    # comment for the ISIS to= case where this mattered).
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.stem + ".tmp.", suffix=dest.suffix)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        yield tmp
        tmp.rename(dest)
    except BaseException:
        if tmp.is_dir():
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            tmp.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def atomic_publish_path(dest: Path) -> Iterator[Path]:
    """Same contract as `atomic_publish`, but yields a path that does **not** exist on disk (only
    reserved -- claimed via `tempfile.mkstemp` then immediately unlinked). Required for ISIS/ASP tools
    invoked via `subprocess_utils.run_quiet` that take their own `to=`/`-o` output-path argument and
    refuse to write to an already-existing path.

    Caller builds their own subprocess argv referencing the yielded path and runs it (typically via
    `run_quiet`); on clean exit this renames the tool's output at that path to `dest`, same as
    `atomic_publish`.
    """
    # Confirmed repeatedly for ISIS apps specifically -- see e.g. isis_wac.crop_for_camera/
    # run_lrowac2isis's own docstrings: "ISIS's crop app ... refuses to overwrite an existing to=
    # output". Cleanup is a no-op if the tool never created anything at the yielded path at all.
    #
    # The temp name preserves dest's suffix (<dest.stem>.tmp.<random><dest.suffix>), not a generic
    # .tmp: confirmed to matter for ISIS to= cube outputs (framestitch/crop/cam2map) -- a
    # .tmp-suffixed path silently never got written to (ISIS's own cube-writer declines to create
    # cube content at a path without a .cub-like extension, no error surfaced -- the failure only
    # showed up later, as this context manager's own rename finding nothing there). See
    # atomic_publish's own comment for the same fix applied there too.
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.stem + ".tmp.", suffix=dest.suffix)
    os.close(fd)
    tmp = Path(tmp_name)
    tmp.unlink()
    try:
        yield tmp
        tmp.rename(dest)
    except BaseException:
        if tmp.is_dir():
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            tmp.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def atomic_publish_prefix(dest: Path, tool_suffix: str) -> Iterator[Path]:
    """For ASP/ISIS tools that take an output *prefix* (`-o <prefix>`), not an exact path, and append
    their own fixed suffix to it -- `dem_mosaic`'s `<prefix>-tile-0.tif` (`dem_ortho.hole_fill_dem`),
    `sat_sim`'s `<prefix>-<camera_stem>.tif` (`render.run_sat_sim`) -- neither of which fits
    `atomic_publish_path`'s exact-final-path contract directly.

    Yields a unique temp *prefix* such that `<yielded><tool_suffix>` is a guaranteed-fresh,
    non-existent path for the tool to write its output to. Caller runs the tool against the yielded
    prefix; on clean exit, `<yielded><tool_suffix>` is atomically renamed to `dest`.

    :param tool_suffix: the exact string the tool appends, including any separator (e.g.
        `"-tile-0.tif"`, or `f"-{camera_stem}.tif"`) -- get this wrong and the rename silently finds
        nothing there.
    """
    # Reserved the same way atomic_publish_path reserves its path: a tempfile.mkstemp claim, then
    # immediately unlinked. A wrong tool_suffix is the same failure mode atomic_publish_path's own
    # comment describes for a wrong-extension guess.
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.stem + ".tmp.")
    os.close(fd)
    tmp_prefix = Path(tmp_name)
    tmp_prefix.unlink()
    tmp_output = Path(str(tmp_prefix) + tool_suffix)
    try:
        yield tmp_prefix
        tmp_output.rename(dest)
    except BaseException:
        tmp_output.unlink(missing_ok=True)
        raise
