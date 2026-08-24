"""The `huey` (sqlite-backed) task queues `trn_dataset.py`'s `TrnTestDataSet.populate()`/
`populate_via_workers()` drive. Replaces this project's old filesystem lock/error files with
`huey`'s own well-tested queue/result machinery -- see docs/dataset-plan.md's "Task queue" section
for the full design and why.

Two separate `Huey` instances, each with its own sqlite file under `<output_dir>/.huey/` and its
own thin task wrapper around the shared `_generate_entry` helper -- not one, because huey fixes a
`Huey` instance's `immediate` mode at construction time, and the two real use cases need opposite
values. One task per *entry* (covering every requested product type for it, not one task per
`(entry, product_type)`) -- see `_generate_entry`'s own docstring for why:

- `huey` (`tasks.db`, `immediate=True`): what `populate()` drives, unchanged since this module's
  first version. Executes synchronously in the calling process the moment a task is enqueued -- no
  separate consumer process needed, matching `populate()`'s original single-call,
  blocks-until-done behavior exactly. `immediate_use_memory=False` is required alongside it:
  huey's own default silently switches immediate mode to in-memory storage, which would make a
  task's failure invisible to a `status()` call from a *different* process (confirmed empirically
  -- see docs/dataset-plan.md) -- the real sqlite file must stay authoritative so a fresh
  `docker compose run` can still see a prior failure, the same property the old `.error` files had.
- `huey_parallel` (`tasks_parallel.db`, `immediate=False`): what `populate_via_workers()` drives.
  huey's `Consumer` class refuses to start against an `immediate=True` instance (confirmed via its
  own source -- raises `ConfigurationError`), and isn't safely embeddable in a background thread
  either (it registers OS signal handlers, which only works on a process's main thread) -- so real
  worker-pool execution needs a genuinely separate OS process. `populate_via_workers()` enqueues
  into this instance, then spawns/manages a real `huey_consumer trntest.tasks.huey_parallel -w N -k
  process` subprocess (`-k process`, not thread/greenlet, to preserve this project's existing rule
  that spiceypy's process-global state is unsafe to share *within* one process -- each worker
  process still only runs one task at a time, sequentially, same as `populate()`'s own single
  process always has) via `start_consumer`/`stop_consumer` below, and tears it down once the batch
  drains. First use against a cold cache still carries this project's usual documented risk of
  concurrent workers racing on the same not-yet-cached fetch (see docs/environment.md's "Other
  sharp edges" section) -- more likely here than in single-agent use, since multiple worker
  *processes* are now deliberately fetching in parallel; safest against an already-warmed cache.

One instance of each per worktree's `output_dir` (not per-dataset-folder): `@huey.task()`/
`@huey_parallel.task()` bind to fixed module-level instances, so `dataset_folder` is a task
*argument* rather than part of either queue's own identity. `output_dir` is already this project's
per-worktree isolation boundary (see docs/environment.md's "Multi-agent worktrees" section), so
concurrent agents' queues stay separate the same way their dataset folders already do.
"""

import subprocess

from huey import SqliteHuey

from trntest.config import load_config

_config = load_config()
_huey_dir = _config.output_dir / ".huey"
_huey_dir.mkdir(parents=True, exist_ok=True)  # SqliteHuey does not create its own parent dir

huey = SqliteHuey(filename=str(_huey_dir / "tasks.db"), immediate=True, immediate_use_memory=False)
huey_parallel = SqliteHuey(filename=str(_huey_dir / "tasks_parallel.db"), immediate=False)

_CONSUMER_LOG_PATH = _huey_dir / "consumer.log"


def task_id(dataset_folder: str, product_id: str) -> str:
    """Deterministic (not random) task id, so `trn_dataset.task_state()` can look up a task's
    stored result from a process that never enqueued it -- e.g. `status()` in a fresh
    `docker compose run` after a prior run's failure. One id per *entry*, not per
    `(entry, product_type)` -- see `_generate_entry`'s own docstring for why task granularity moved
    to the entry level."""
    return f"{dataset_folder}::{product_id}"


class EntryGenerationError(Exception):
    """Raised by `_generate_entry` when one or more of an entry's requested product types failed to
    generate. Carries every failure (`self.errors`, `{product_type: Exception}`), not just the
    first -- each product type is attempted independently within the task (see `_generate_entry`'s
    own docstring), so one's failure must not hide another's own distinct error."""

    def __init__(self, errors: dict[str, Exception]):
        self.errors = errors
        summary = "; ".join(f"{pt}: {type(exc).__name__}: {exc}" for pt, exc in errors.items())
        super().__init__(f"{len(errors)} of this entry's product type(s) failed: {summary}")


def _generate_entry(entry, product_types: tuple[str, ...]) -> dict:
    """Shared body for both `generate_product`/`generate_product_parallel` below -- one task per
    *entry*, covering every product type in `product_types` for it, not one task per
    `(entry, product_type)` as this module originally had. That finer granularity meant two
    product types of the same entry (e.g. `hillshade` and `reproject`, both needing
    `entry.camera`/`entry.dem_ortho_result`) could land on two different `-k process` worker
    processes at once -- `functools.cached_property` only memoizes within one process, so each
    worker independently, redundantly rebuilt the same real SPICE/ISIS/DEM state for that entry
    (confirmed live: a real `("hillshade", "reproject")` batch cost noticeably more wall-clock than
    it needed to, see `docs/history.md`'s dated entry). Bundling every requested product type for
    one entry into a single task restores `populate()`'s own natural single-process sharing (the
    same `entry` object's cached properties, reused across product types) while still parallelizing
    across *entries* under `populate_via_workers()`.

    Takes the real `TrnTestEntry` object rather than plain, picklable `(dataset_folder, product_id)`
    args and re-deriving it via `TrnTestDataSet.open()`, even though `generate_product_parallel`
    (unlike `generate_product`) does cross a real process boundary to a `-k process` worker
    subprocess: re-opening from disk here would force every caller -- including fast, disk-free
    unit tests that never call `TrnTestDataSet.create()` -- to round-trip a real `manifest.csv` just
    to run a fake `generate()`. See `generate_product_parallel`'s own docstring for why passing the
    object directly is confirmed empirically safe across that process boundary, not just assumed.

    Each product type is attempted independently -- one's exception is caught, not left to abort the
    rest, matching this module's existing "one bad task shouldn't abort the whole batch" tolerance
    (see `trn_dataset.populate()`'s own docstring), just applied within one entry's own task instead
    of only across entries. If any failed, raises `EntryGenerationError` (carrying all of them) only
    *after* every product type has been attempted -- so a `crop` success isn't lost just because
    `hillshade` also happened to fail in the same task; `trn_dataset.task_state()`'s own
    `image.exists()`-first check still reports each product type's real state correctly regardless
    of this shared, entry-level failure signal (see that function's own docstring).

    Must return a non-`None` value on success -- confirmed empirically: huey's own `_execute` only
    calls `put_result` for a successful task when `task_value is not None` (or `store_none=True`,
    not set for either instance here), so a bare `None` return never gets stored at all, and a
    blocking `result.get()` then polls forever for a result that will never arrive. Returns
    `{product_type: raster_path}` for every product type that succeeded."""
    results = {}
    errors = {}
    for product_type in product_types:
        image = entry.images_by_type[product_type]
        try:
            results[product_type] = image.generate()
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: any of these means "this
            # product type failed," not KeyboardInterrupt/SystemExit, which must propagate
            # immediately -- and every other product type must still get its own chance to run.
            errors[product_type] = exc
    if errors:
        raise EntryGenerationError(errors)
    return results


@huey.task()
def generate_product(entry, product_types: tuple[str, ...]) -> dict:
    """Thin wrapper so `populate()` goes through huey's queue/result machinery (stored exceptions,
    huey's own introspection) instead of calling `_generate_entry` directly. See that function's own
    docstring for the shared logic, the entry-level task granularity, and the non-`None`-return
    requirement."""
    return _generate_entry(entry, product_types)


@huey_parallel.task()
def generate_product_parallel(entry, product_types: tuple[str, ...]) -> dict:
    """Same as `generate_product`, bound to `huey_parallel` instead -- what `populate_via_workers`
    enqueues. Takes the real `TrnTestEntry` object, not picklable primitive args, same as
    `generate_product`: confirmed empirically (a plain object, not just simple types, round-trips
    fine as a task argument even under a real `-k process` worker subprocess -- huey's own
    serializer handles it) rather than assumed, since unlike `generate_product` this one **does**
    cross a real process boundary. `TrnTestEntry` pickles cleanly as long as no
    `functools.cached_property` already computed on it holds something unpicklable (SPICE objects
    are never cached directly on `entry` today, only derived plain data, so this holds in practice;
    revisit if that ever changes) -- true at enqueue time regardless, since nothing's been computed
    on a freshly-enqueued entry yet."""
    return _generate_entry(entry, product_types)


def start_consumer(workers: int, env: dict[str, str] | None = None) -> subprocess.Popen:
    """Starts a real `huey_consumer` OS process against `huey_parallel`'s queue -- `-k process`
    worker processes, not threads (see this module's own docstring for why). Output is redirected
    to `<output_dir>/.huey/consumer.log` rather than left to flood the caller's own stdout, matching
    this project's `subprocess_utils.run_quiet` convention for noisy tooling -- not built on that
    helper itself, since this is a long-running background process to start/monitor/stop, not a
    one-shot call to run to completion and capture. Caller must call `stop_consumer` once the batch
    drains -- otherwise this keeps polling forever, same as any other `huey_consumer` invocation.

    `env`, if given, replaces (not merges into) the subprocess's environment -- `None` (the default)
    inherits the calling process's own environment unchanged, same as plain `subprocess.Popen`.
    Exists for `tests/test_trn_dataset.py`'s own real-subprocess consumer test, which needs a task
    argument importable from a fresh process without the whole `trntest` package's SPICE/ASP/ISIS
    dependencies -- not expected to matter for real callers."""
    with open(_CONSUMER_LOG_PATH, "w") as log_file:
        return subprocess.Popen(
            ["huey_consumer", "trntest.tasks.huey_parallel", "-w", str(workers), "-k", "process"],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
        )


def stop_consumer(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """Graceful SIGTERM first (huey's consumer handles it as a clean shutdown signal), falling back
    to SIGKILL only if it doesn't exit within `timeout` seconds."""
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
