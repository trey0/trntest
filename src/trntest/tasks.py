"""The `huey` (sqlite-backed) task queue `trn_dataset.py`'s `TrnTestDataSet.populate()` drives.
Replaces this project's old filesystem lock/error files with `huey`'s own well-tested queue/result
machinery -- see docs/dataset-plan.md's "Task queue" section for the full design and why.

One `huey` instance per worktree's `output_dir` (not per-dataset-folder): `@huey.task()` binds to a
fixed module-level instance, so `dataset_folder` is a task *argument* rather than part of the
queue's own identity. `output_dir` is already this project's per-worktree isolation boundary (see
docs/environment.md's "Multi-agent worktrees" section), so concurrent agents' queues stay separate
the same way their dataset folders already do.

`immediate=True` (the default here) executes a task synchronously in the calling process the moment
it's enqueued -- no separate `huey_consumer` process needed, matching `populate()`'s existing
single-call, blocks-until-done behavior exactly. `immediate_use_memory=False` is required alongside
it: huey's own default silently switches immediate mode to in-memory storage, which would make a
task's failure invisible to a `status()` call from a *different* process (confirmed empirically --
see docs/dataset-plan.md) -- the real sqlite file must stay authoritative so a fresh
`docker compose run` can still see a prior failure, the same property the old `.error` files had.

Real multi-worker parallel population (several `docker compose run` workers today) is not wired up
here -- would mean `immediate=False` plus a long-running `huey_consumer trntest.tasks.huey -w N -k
process` process (`-k process`, not thread/greenlet, to preserve this project's existing rule that
spiceypy's process-global state is unsafe to share within one process) and changing `populate()` to
enqueue-then-wait instead of enqueue-and-block-per-task. Deferred until real bulk generation is
actually needed; see docs/dataset-plan.md.
"""

from pathlib import Path

from huey import SqliteHuey

from trntest.config import load_config

_config = load_config()
_huey_dir = _config.output_dir / ".huey"
_huey_dir.mkdir(parents=True, exist_ok=True)  # SqliteHuey does not create its own parent dir

huey = SqliteHuey(filename=str(_huey_dir / "tasks.db"), immediate=True, immediate_use_memory=False)


def task_id(dataset_folder: str, product_id: str, product_type: str) -> str:
    """Deterministic (not random) task id, so `trn_dataset.task_state()` can look up a task's
    stored result from a process that never enqueued it -- e.g. `status()` in a fresh
    `docker compose run` after a prior run's failure."""
    return f"{dataset_folder}::{product_id}::{product_type}"


@huey.task()
def generate_product(image) -> Path:
    """Thin wrapper around `TrnTestImage.generate()` -- exists so `populate()` goes through huey's
    queue/result machinery (retries, stored exceptions, huey's own introspection) instead of calling
    `image.generate()` directly. Takes the real `TrnTestImage` object rather than plain, picklable
    `(dataset_folder, product_id, product_type)` args and re-deriving it: the `immediate=True`
    default above never crosses a real process boundary, so there's no serialization requirement to
    design around yet -- reopening the whole dataset from disk here would force every caller
    (including fast, disk-free unit tests that never call `TrnTestDataSet.create()`) to round-trip a
    real `manifest.csv` just to run a fake `generate()`. **If the deferred multi-worker
    `huey_consumer` path above is ever built, this will need picklable primitive args instead** --
    confirm `TrnTestImage`/`TrnTestEntry` instances actually pickle cleanly at that point (untested;
    `functools.cached_property` values already computed on `entry` would need to survive it too).

    Must return a non-`None` value -- confirmed empirically: huey's own `_execute` only calls
    `put_result` for a successful task when `task_value is not None` (or `store_none=True`, not set
    here), so a bare `image.generate(); return None` (huey's implicit default) never gets stored at
    all, and `populate()`'s blocking `result.get()` then polls forever for a result that will never
    arrive. `TrnTestImage.generate()` already returns `raster_path`, so this is a free fix, not a
    workaround."""
    return image.generate()
