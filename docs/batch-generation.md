# Batch generation: running a large `TrnTestDataSet` population job

How to actually populate a `TrnTestDataSet` at scale — network/SPICE/ISIS/ASP work across many
manifest entries — using `TrnTestDataSet.populate_via_workers()`, and the concrete things to watch
out for when you do. See `src/trntest/tasks.py`'s module docstring for the underlying `huey` design
this builds on; this doc is the practical workflow layer on top, not a design doc.

## Two ways to populate a dataset — pick the right one

| | `populate()` | `populate_via_workers()` |
|---|---|---|
| Execution | Sequential, one process (`immediate=True`) | Parallel, `workers` separate OS processes (a managed `huey_consumer -k process` subprocess) |
| Good for | The flagship demo notebook, small datasets, debugging (failures surface synchronously, no extra process to reason about) | A large batch across many manifest entries |
| Queue | `trntest.tasks.huey` | `trntest.tasks.huey_parallel` (**independent** — see "Two independent queues" below) |
| `product_types`/`retry_failed`/`limit` | Same semantics | Same semantics |

Both take the identical `product_types`/`retry_failed`/`limit` signature — `populate_via_workers()`
is a drop-in replacement for `populate()` from the caller's side, just backed by worker-process
parallelism instead of a sequential loop.

## Configuring which generators run (`product_types`)

`product_types` is a plain parameter on every call (`populate()`, `populate_via_workers()`,
`status()`, `write_index()`, `truncate()`), but it no longer has one fixed default — leaving it
unset (`None`) resolves to `TrnTestDataSet.default_product_types`, which depends on the dataset's
own `entry_kind`: `trn_dataset.PRODUCT_TYPES = ("crop", "hillshade", "report", "gallery")` for a
normal `entry_kind="edr"` dataset, or `SPICE_PRODUCT_TYPES = ("hillshade",)` for `entry_kind="spice"`
(a `TrnTestEntrySpice` dataset has no EDR, so `crop`/`reproject`/`report`/`gallery` — all of which
need one — aren't applicable). `report`/`gallery` (the per-entry HTML report and the dataset-wide
blink-comparator gallery) are both on by default for an `edr` dataset; `reproject` is implemented
but opt-in (see `trn_dataset.py`'s module docstring) -- pass it explicitly:

```python
PRODUCT_TYPES = ("crop", "hillshade", "report", "gallery", "reproject")

dataset.populate_via_workers(product_types=PRODUCT_TYPES, workers=4)
dataset.status(product_types=PRODUCT_TYPES, huey_instance=tasks.huey_parallel)
```

**Pass the same `product_types` to every call in a given workflow.** It isn't remembered between
calls — `status()`/`truncate()` after a `populate_via_workers(product_types=(..., "reproject"))` run
will silently fall back to `default_product_types` (`crop`/`hillshade`/`report`/`gallery`, no
`reproject`) unless you pass the same explicit `product_types` there too, making `reproject`'s
state invisible rather than raising anything.

`populate()`/`populate_via_workers()` also take `write_index: bool = True`: after their task-queue
loop, they write `<dataset_folder>/status.csv` and `<dataset_folder>/reports/index.html` (a nav bar
across every entry's own report) via `TrnTestDataSet.write_index()`. `status.csv`/`index.html`
themselves are cheap/pure-Python, but `write_index()` also (re)generates
`<dataset_folder>/reports/overview_map.png` (`overview_map.write_overview_map`) by default, which
is **not** cheap -- it builds a `Camera` (a SPICE pose rebuild) for *every* entry in the whole
dataset, not just ones the triggering call actually populated. In a `populate(limit=N)` loop over a
large dataset, leaving this on means every single call re-rebuilds cameras for the entire
already-populated portion just to redraw the map -- avoidable, roughly-quadratic-in-total-calls
cost. Pass `write_index=False` for every call in the loop except (optionally) the last, or
`dataset.write_index(write_overview_map=False)` calls in between with one plain `write_index()` (map
included) once at the end.

## Recommended workflow

```python
import trntest
from trntest import tasks, trn_dataset

config = trntest.load_config()
images = trntest.read_manifest("notebooks/dataset_manifest.csv")
dataset = trn_dataset.TrnTestDataSet.create(config.output_dir / "trn_dataset", images, config)

PRODUCT_TYPES = ("crop", "hillshade", "report", "gallery")  # add "reproject" once you want it too

# 1. Run a small first pass before scaling up workers -- not to protect against a general
#    cold-cache race (that's handled automatically now regardless of worker count, see "Cold-cache
#    concurrent fetch races" below), but to let the one-time ~10GB Astropedia GLD100 download (if
#    this dataset's footprints need it) happen serially, and to catch an early config/environment
#    problem cheaply before committing to a big batch. workers=1 here is still deliberate for the
#    GLD100 case specifically -- see that section's own note.
dataset.populate_via_workers(product_types=PRODUCT_TYPES, limit=2, workers=1)

# 2. Scale up once the cache is warm.
dataset.populate_via_workers(product_types=PRODUCT_TYPES, workers=4)

# 3. Check for failures -- huey_instance is required to see populate_via_workers()'s own state.
status = dataset.status(product_types=PRODUCT_TYPES, huey_instance=tasks.huey_parallel)
failed = status[(status[list(PRODUCT_TYPES)] == "failed").any(axis=1)]
print(failed)

# 4. Retry, if the failure looks transient (a network blip) rather than a reproducible bug.
dataset.populate_via_workers(product_types=PRODUCT_TYPES, workers=4, retry_failed=True)
```

Prefer a large or omitted `limit` for `populate_via_workers()` calls, not a small one repeated many
times — each call starts a fresh consumer subprocess (process-startup overhead), unlike `populate()`,
where `limit` is cheap to call repeatedly. `limit` is still useful for a first, deliberately small,
cache-warming pass (see below), just not as the default way to chunk a whole run.

`populate_via_workers()` also takes `result_timeout: float | None = 1800.0` (30 min): how long to
wait for one entry's stored result before giving up on it and moving to the next, rather than
blocking forever. The default is generous against a slow/cold entry; lower it for a tighter feedback
loop on a smaller exploratory run, or pass `None` to wait forever like before this parameter existed.
See "Don't run the test suite..." below for the gap this is a safety net for.

## Retrying failures

`retry_failed=True` (step 4 above) is **dataset-wide**: it clears every currently-`failed`
entry's stored result and lets the normal pending-scan pick all of them back up. That's the right
tool once you've looked at an entry's own log (see "Where to look when something fails" below) and
concluded the failure was transient -- a network blip, a rate limit, a one-off ISIS/ASP hiccup.

**It's the wrong tool for a failure you've confirmed is reproducible** -- a real bug that will
fail the same entry the same way every time, not chance. Blindly calling `retry_failed=True`
against the whole dataset then just re-attempts it, and burns a worker slot on it, on every future
pass until the bug is actually fixed. This isn't hypothetical: a real `trntest1` run hit exactly
this (`docs/proposed-tasks/open-items.md`'s Hapke `hg2`-out-of-range item, confirmed identical on
two separate runs before the underlying sampling bug was fixed).

Two more targeted primitives for exactly this case:

- **`dataset.skip(entries, reason)`** marks one or more entries (a single `TrnTestEntry` or a
  list) as permanently excluded, persisted to `<dataset_folder>/skip_list.csv`. From then on,
  `task_state()`/`status()` report `"skipped"` for their non-`done` product types instead of
  `"failed"`/`"pending"`, so a plain `populate_via_workers()` call never re-enqueues them and a
  dataset-wide `retry_failed=True` sweep never clears/retries them either -- both apply to
  *everything else* in the dataset as normal. `dataset.unskip(entries)` reverses it once the bug
  is actually fixed. Whenever a call to `populate()`/`populate_via_workers()` finds skip-listed
  entries with otherwise-pending work, it prints `Skipping N entries due to skip list: <ids>` so a
  thinner-than-expected batch doesn't get mistaken for a stalled queue or a fully-populated
  dataset -- if you see that line and didn't expect it, check `skip_list.csv`.
- **`dataset.truncate(entries=[...], product_types=...)` then a normal `populate()`/
  `populate_via_workers()` call** retries one or a few *specific* entries in isolation -- e.g.
  after patching something and wanting to verify just that one, rather than sweeping the whole
  dataset's failures. `truncate()` deletes the entry's product file(s)/log/stored result and
  reverts it to `pending`; pair it with a small `limit` (or scope `product_types` down) if the
  dataset has other unrelated pending work you don't want swept up in the same call.

## Watching a run live: the health monitor

`populate_via_workers()` starts a background thread for the call's own duration (torn down
alongside its consumer subprocess, so a killed calling process takes it with it rather than
leaving it orphaned) that logs one line every 5s to `<dataset_folder>/logs/health_monitor_log.txt`
and prints that path (with a ready-to-run `tail -f` command) at startup. Each line is
self-describing `key=value` pairs, not a CSV — readable via `tail -f` with no header to scroll back
for, and just as easy to parse back into a `DataFrame` for post-run plots
(`re.findall(r"(\w+)=(\S+)", line)` per line):

```
ts=2026-09-09T02:18:38+00:00 done=16 failed=0 pending=4 pct_ok=100.0 pct_done=80.0 eta_min=1.0 disk_free_gb=23.8 disk_eta_gb=23.3 mem_mb=4876.2 cpu_pct=422.2 active=4/8
```

`done`/`failed`/`pending`/`active` count this run's own enqueued tasks (entry-granularity, one
huey task per entry regardless of how many product types it covers), not `status()`'s
per-product-type cells — a live progress view, not a replacement for `status()`/`status.csv` as the
per-product-type record of truth after the run. `eta_min`/`disk_eta_gb` are estimates derived from
this run's own observed throughput/bytes-per-entry so far, not guaranteed — `n/a` until there's
enough signal (no completed entries yet). `mem_mb`/`cpu_pct` sum over the consumer subprocess and
its worker children; `active` (`active_count/workers`) is a sanity check that workers are actually
busy — if it drops toward 0 while entries are still pending, a worker has likely crashed or
stalled (cross-check `.huey/consumer.log`).

Live-validated against a real 20-entry, 8-worker run against `trntest1` (the
`select_datasets.py`-produced dataset, 207 entries total, this run's own `limit=20` scoping it down;
named `orbit_sequence_dataset` at the time of this specific run, later renamed to `trntest1`):
`active` correctly tracked 8/8 → 4/8 → 3/8 → 2/8 → 0/8 as the 20 entries drained across 8 workers,
`eta_min` converged to 0 as the run finished, `disk_free_gb` dropped from 25.3 to 23.7 over the run
(~75MB/entry actually written — the same order of magnitude as a separate, real single-entry
measurement of ~114MB/entry for a different entry), and `disk_eta_gb` converged to match
`disk_free_gb` exactly once `pending` hit 0. No failures.

The startup announcement (`Health monitor: tail -f ...`) needs its `print(..., flush=True)` --
`docker compose run`'s stdout is a pipe, not a tty, so a bare `print()` there is block-buffered by
default and can sit unflushed until the whole run exits, silently defeating the point of
announcing the path *at startup*. Caught live: the first validation run above only showed the line
after the process had already finished; a follow-up 2-entry run with `flush=True` confirmed the
line now streams immediately, well before that run's own completion.

## Issues to watch out for

**Two independent queues.** `populate_via_workers()`'s failures live in `tasks.huey_parallel`, not
`tasks.huey` — a plain `status()` call (default `huey_instance=tasks.huey`) will show `pending` or
`done`, never a `populate_via_workers()`-recorded `failed`. Always pass
`huey_instance=tasks.huey_parallel` when checking on a worker-pool run. This is deliberate, not a
bug — see `tasks.py`'s module docstring for why the two queues can't be merged.

**Not safe to run concurrently with itself.** Only one `populate()` *or* `populate_via_workers()`
call should run against a given dataset folder at a time (running one of each simultaneously is
fine — separate queues — but two `populate_via_workers()` calls, or two `populate()` calls, against
the *same* folder at once are not). The old filesystem lock files that made concurrent
`docker compose run` workers safe are gone as of the `huey` migration —
`populate_via_workers()`'s own worker pool is the supported way to get parallelism now, not multiple
top-level calls.

**Don't run the test suite against the same worktree while a batch is active.** `tasks.huey`/
`tasks.huey_parallel` are process-wide singletons keyed on `config.output_dir` alone (see `tasks.py`'s
own module docstring: "One instance of each per worktree's `output_dir`, not per-dataset-folder") —
every `docker compose run` container in a given worktree shares the same bind-mounted `output_dir`,
so they all resolve to the same `<output_dir>/.huey/*.db` files regardless of which dataset each
container is working on. Several test files' autouse `_flush_huey_before_test` fixture calls
`tasks.huey.flush()`/`tasks.huey_parallel.flush()` to keep each test isolated. Run `pytest` in a
second `docker compose run` container while `populate_via_workers()` is active in a first one against
the same worktree, and that flush wipes the live batch's queue and result store out from under it:
`huey.storage.flush_all()` clears `flush_queue()` (any task not yet dequeued — the batch processes
fewer entries than requested) and `flush_results()` (every stored result, including ones
`_await_result()` hasn't collected yet). The calling process's `_await_result()` then blocks forever
on a result that will never arrive, even though the worker pool's own remaining tasks keep completing
in the background. `result_timeout` (30 min default) bounds the damage from this or any other cause
of a missing result, but avoiding the collision is simpler: don't run tests against a worktree with a
batch in flight.

**Task granularity is per-entry, not per-`(entry, product_type)`.** One `huey` task covers every
requested, still-pending product type for a given entry, run sequentially within that single
task/process; `populate_via_workers(workers=N)` parallelizes across *entries* only. This makes a
same-entry cross-worker race on shared state (`entry.camera`/`entry.dem_ortho_result`, both
`functools.cached_property`, backed by `isis_wac.run_pipeline`'s shared ISIS working directory)
structurally impossible, not just handled — and as a side benefit, that shared state is computed
once per entry and reused across its product types instead of rebuilt per worker. Writers
(`isis_wac.crop_for_camera`/`run_framestitch`, `dem_ortho.fetch_dem`/`fetch_and_shade_ortho`) also
publish atomically (`product_io.atomic_publish_path`/`atomic_publish`) — this remains valuable for
cross-entry write collisions and crash/partial-write safety, independent of the now-eliminated
same-entry race.

Sequencing by product type is therefore a pure throughput choice now, not a safety requirement — it
protects against many *different* entries' tasks all cold-fetching the same not-yet-cached external
resource at once (see "Cold-cache concurrent fetch races" below), nothing else:

```python
dataset.populate_via_workers(product_types=("crop",), workers=4)
dataset.populate_via_workers(product_types=("hillshade",), workers=4)
```

This still parallelizes fully across *entries* (today's manifest has one `edr_product` per row, so
cross-entry write collisions aren't expected in practice) either way.

**Cold-cache concurrent fetch races -- mostly resolved.** `cache.py`'s request pacing
(`_REQUEST_PACING_SECONDS`) used to be calibrated per-*process* only: several worker processes each
fetching cold, uncached resources (SPICE kernels, WMS tiles) at once could combine into a burst
large enough to trip a server-side rate limiter (Lunaserv, NAIF, the PDS ODE API), the same way
two independent agents' bursts can (`docs/environment.md`'s Phase 36 incident). **Fixed**: every
`cached_get` call -- pacing sleep, request, response streaming, and any retry/backoff -- now runs
under a single `fcntl.flock` mutex at a fixed path under `DEFAULT_CACHE_ROOT`, so at most one fetch
is ever live VPS-wide regardless of worker count, and regardless of which worktree/agent issued it.
A large `workers` count no longer scales aggregate request rate the way it used to; there's no need
to hold a batch's first run to `workers=1` just to avoid that specific failure mode.

One gap this fix does *not* cover: the one-time ~10GB Astropedia GLD100 download
(`cache.fetch_astropedia_gld100`) is deliberately not built on `cached_get` (it needs a stable,
resumable `.part` path across retries -- see that function's own comment), so it isn't
serialized by the same lock and remains not concurrency-safe. Check
`cache/astropedia/*.tif` already exists before pointing a fresh worker pool (or a fresh agent) at a
dataset whose footprints might trigger this fetch, rather than relying on request-pacing to protect
it the way it now does for everything else.

**`spiceinit web=yes` overload -- fixed.** A different external host than any of the above, hit
through a different mechanism: ISIS's `spiceinit` subprocess (`isis_wac.run_spiceinit`,
`attach_dem_shape_model`, both called once per entry per pipeline stage) makes its own HTTP request
to NAIF/USGS's SPICE pointing-correction web service, entirely outside `cache.py`, so none of the
`cached_get` pacing above ever applied to it. A real `trntest2` run (69 entries,
`populate_via_workers(workers=8)`, right after a clean 2-entry warm-up) hit this: up to 16
concurrent, uncoordinated `spiceinit` calls tripped server-side throttling hard enough to fail 56 of
67 remaining entries, all with the same "server is unable to handle the request" error -- a same-scale
run the night before, without the extra worker contention, hit it on only ~1.5% of entries. Fixed the
same way as above -- `isis_wac._run_spiceinit_web` now runs under `cache.pacing_gate()`, given its own
dedicated lock path (`DEFAULT_CACHE_ROOT / ".spiceinit_pacing.lock"`, not `cached_get`'s) since it's a
different host and sharing one lock would over-serialize both for no benefit. Live-validated by
retrying all 56 originally-failed entries: 0 `spiceinit`-related failures at `workers=8`, the exact
concurrency that caused the incident.

**A killed calling process can orphan the consumer subprocess.** `populate_via_workers()`'s own
`finally` block calls `stop_consumer()` on a normal exception or Ctrl-C, but a hard kill of the
*calling* process (not the consumer) skips that cleanup. Check for a stray `huey_consumer` process
(`docker exec <container> ps aux`) if a batch run was ever killed abruptly, and terminate it by hand
if still present — it does no harm sitting idle, but it does hold the worker processes and the
consumer log file open.

**Where to look when something fails.** Two different logs, at two different scopes:

- **Per-entry, per-generator logs**: `<dataset_folder>/logs/<edr_product>/<product_type>_log.txt` —
  the console output (this codebase's own `print()` diagnostics, plus a full traceback on failure) of
  that one `generate()` call, captured by `tasks._capture_generator_log` regardless of which process
  ran it (a `populate()` notebook cell, or one of `populate_via_workers()`'s worker processes). This
  is almost always the right first stop for "why did entry X's product type Y fail" — both
  `reports/overview_table.html` (a `logs` column, most useful exactly when a row shows `failed`) and
  each per-entry report page's own summary line link to the entry's whole `logs/<edr_product>/`
  folder (`report._logs_link_html`), not one link per generator file, so you don't need to construct
  any path by hand; pick the specific `<product_type>_log.txt` you want from that folder's own
  listing.
  Only written when that product type is actually (re)generated — an already-`done` type's prior log
  is left alone, never silently cleared by a later no-op `generate()` call.

  **This folder link only works when browsing via a static file server that can list a directory**
  — either the `jupyter-server-proxy`-backed `/output/...` route on the same JupyterLab server, or the
  standalone `scripts/serve_reports.sh` (see `docs/report-generation.md`'s "Viewing reports" section
  for both). Jupyter's own `/files/...` route 403s on a bare directory URL (no autoindex support);
  it can serve one already-known `<product_type>_log.txt` file directly, just not list a folder of
  them.
- **The consumer subprocess's own stdout/stderr** (not per-task output, which lives in the
  per-generator logs above) go to `<output_dir>/.huey/consumer.log` — check there for
  consumer-level problems (a worker crashing, `-k process` health-check restarts) that wouldn't show
  up in a per-task `TaskException`. This file is overwritten (not appended) on every
  `populate_via_workers()` call, so check it *before* starting another batch if you need to debug a
  prior run's failure.

## Verification

This was live-validated against real manifest entries (not just fakes) — two never-before-generated
rows from `notebooks/dataset_manifest.csv`, `populate_via_workers(limit=2, workers=2)`, both crop
cubes and hillshade renders completed correctly via real SPICE/ISIS/ASP calls across two separate
worker processes in 53.4s total.

A later, much larger run (100+ entries against `trntest1`) surfaced two bugs this small-scale
validation didn't exercise, both fixed and regression-tested: the health-monitor race described
above, and `write_index()`'s `overview_map.write_overview_map()` crashing on `pd.to_datetime` when
`start_time`/`stop_time` values mix sub-second precision across rows (fixed with an explicit
`format="ISO8601"`). Worth re-validating this doc's advice at a large, diverse scale before trusting
an example that was only ever run small.
