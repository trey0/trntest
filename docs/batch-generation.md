# Batch generation: running a large `TrnTestDataSet` population job

How to actually populate a `TrnTestDataSet` at scale — real network/SPICE/ISIS/ASP work across many
manifest entries — using `TrnTestDataSet.populate_via_workers()`, and the concrete things to watch
out for when you do. See `src/trntest/tasks.py`'s module docstring for the underlying `huey` design
this builds on; this doc is the practical workflow layer on top, not a design doc.

## Two ways to populate a dataset — pick the right one

| | `populate()` | `populate_via_workers()` |
|---|---|---|
| Execution | Sequential, one process (`immediate=True`) | Real parallel, `workers` separate OS processes (a managed `huey_consumer -k process` subprocess) |
| Good for | The flagship demo notebook, small datasets, debugging (failures surface synchronously, no extra process to reason about) | A large batch across many manifest entries |
| Queue | `trntest.tasks.huey` | `trntest.tasks.huey_parallel` (**independent** — see "Two independent queues" below) |
| `product_types`/`retry_failed`/`limit` | Same semantics | Same semantics |

Both take the identical `product_types`/`retry_failed`/`limit` signature — `populate_via_workers()`
is a drop-in replacement for `populate()` from the caller's side, just backed by real parallelism.

## Configuring which generators run (`product_types`)

There's no per-dataset setting for this — `product_types` is a plain parameter on every call
(`populate()`, `populate_via_workers()`, `status()`, `truncate()`), defaulting to
`trn_dataset.PRODUCT_TYPES = ("crop", "hillshade", "report")`. `report` (the per-entry HTML report)
is on by default; `reproject` is implemented but opt-in
(see `trn_dataset.py`'s module docstring) -- pass it explicitly:

```python
PRODUCT_TYPES = ("crop", "hillshade", "report", "reproject")

dataset.populate_via_workers(product_types=PRODUCT_TYPES, workers=4)
dataset.status(product_types=PRODUCT_TYPES, huey_instance=tasks.huey_parallel)
```

**Pass the same `product_types` to every call in a given workflow.** It isn't remembered between
calls — `status()`/`truncate()` after a `populate_via_workers(product_types=(..., "reproject"))` run
will silently only look at `crop`/`hillshade`/`report` unless you pass `product_types=PRODUCT_TYPES`
there too, making `reproject`'s real state invisible rather than raising anything.

`populate()`/`populate_via_workers()` also take `write_index: bool = True`: after their task-queue
loop, they write `<dataset_folder>/status.csv` and `<dataset_folder>/reports/index.html` (a nav bar
across every entry's own report) via `TrnTestDataSet.write_index()`. `status.csv`/`index.html`
themselves are cheap/pure-Python, but `write_index()` also (re)generates
`<dataset_folder>/reports/overview_map.png` (`overview_map.write_overview_map`) by default, which
is **not** cheap -- it builds a real `Camera` (a SPICE pose rebuild) for *every* entry in the whole
dataset, not just ones the triggering call actually populated. In a `populate(limit=N)` loop over a
large dataset, leaving this on means every single call re-rebuilds cameras for the entire
already-populated portion just to redraw the map -- real, avoidable, roughly-quadratic-in-total-calls
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

PRODUCT_TYPES = ("crop", "hillshade", "report")  # add "reproject" once you actually want it too

# 1. Warm the cache with a small, conservative run first -- see "Cold-cache concurrent fetch
#    races" below for why. workers=1 here is deliberate.
dataset.populate_via_workers(product_types=PRODUCT_TYPES, limit=2, workers=1)

# 2. Scale up once the cache is warm.
dataset.populate_via_workers(product_types=PRODUCT_TYPES, workers=4)

# 3. Check for failures -- huey_instance is required to see populate_via_workers()'s own state.
status = dataset.status(product_types=PRODUCT_TYPES, huey_instance=tasks.huey_parallel)
failed = status[(status[list(PRODUCT_TYPES)] == "failed").any(axis=1)]
print(failed)

# 4. Retry, if anything genuinely transient failed (a network blip, not a real bug).
dataset.populate_via_workers(product_types=PRODUCT_TYPES, workers=4, retry_failed=True)
```

Prefer a large or omitted `limit` for `populate_via_workers()` calls, not a small one repeated many
times — each call starts a fresh consumer subprocess (real process-startup overhead), unlike
`populate()`, where `limit` is cheap to call repeatedly. `limit` is still useful for a first,
deliberately small, cache-warming pass (see below), just not as the default way to chunk a whole run.

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

Live-validated against a real 20-entry, 8-worker run against `orbit_sequence_dataset` (the
`select_datasets.py`-produced dataset, 207 entries total, this run's own `limit=20` scoping it down):
`active` correctly tracked 8/8 → 4/8 → 3/8 → 2/8 → 0/8 as the 20 entries drained across 8 workers,
`eta_min` converged to 0 as the run finished, `disk_free_gb` dropped from 25.3 to 23.7 over the run
(~75MB/entry actually written — the same order of magnitude as `docs/proposed-tasks/
production-run-readiness.md`'s own ~114MB/entry estimate from a single different entry), and
`disk_eta_gb` converged to match `disk_free_gb` exactly once `pending` hit 0. No failures.

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
`populate_via_workers()`'s own worker pool is the supported way to get real parallelism now, not
multiple top-level calls.

**Task granularity is per-entry, not per-`(entry, product_type)`.** One `huey` task covers every
requested, still-pending product type for a given entry, run sequentially within that single
task/process; `populate_via_workers(workers=N)` parallelizes across *entries* only. This makes a
same-entry cross-worker race on shared state (`entry.camera`/`entry.dem_ortho_result`, both
`functools.cached_property`, backed by `isis_wac.run_pipeline`'s shared ISIS working directory)
structurally impossible, not just handled — and as a side benefit, that shared state is computed
once per entry and reused across its product types instead of rebuilt per worker. Real writers
(`isis_wac.crop_for_camera`/`run_framestitch`, `dem_ortho.fetch_dem`/`fetch_and_shade_ortho`) also
publish atomically (`product_io.atomic_publish_path`/`atomic_publish`) — this remains valuable
for genuine cross-entry write collisions and crash/partial-write safety, independent of the
now-eliminated same-entry race.

Sequencing by product type is therefore a pure throughput choice now, not a safety requirement — it
protects against many *different* entries' tasks all cold-fetching the same not-yet-cached external
resource at once (see "Cold-cache concurrent fetch races" below), nothing else:

```python
dataset.populate_via_workers(product_types=("crop",), workers=4)
dataset.populate_via_workers(product_types=("hillshade",), workers=4)
```

This still parallelizes fully across *entries* (today's real manifest has one `edr_product` per
row, so cross-entry write collisions aren't expected in practice) either way.

**Cold-cache concurrent fetch races.** The same class of race `docs/environment.md` documents for
multiple *agents* hitting the same external host/cache path applies here too, self-inflicted by one
batch job's own worker pool: the one-time ~10GB Astropedia GLD100 download
(`cache.fetch_astropedia_gld100`) isn't concurrency-safe, and `cache.py`'s request pacing
(`_REQUEST_PACING_SECONDS`) is calibrated per-*process* — several worker processes each fetching
cold, uncached resources (SPICE kernels, WMS tiles) at once can combine into a burst large enough to
trip a real server-side rate limiter (Lunaserv, NAIF, the PDS ODE API), the same way two independent
agents' bursts can (`docs/environment.md`'s Phase 36 incident). Start a batch's first run small and
at `workers=1` (or check `cache/astropedia/*.tif` already exists) to warm the cache before scaling
up `workers`, rather than pointing a large worker count at an entirely cold cache from the start.

**A killed calling process can orphan the consumer subprocess.** `populate_via_workers()`'s own
`finally` block calls `stop_consumer()` on a normal exception or Ctrl-C, but a hard kill of the
*calling* process (not the consumer) skips that cleanup. Check for a stray `huey_consumer` process
(`docker exec <container> ps aux`) if a batch run was ever killed abruptly, and terminate it by hand
if still present — it does no harm sitting idle, but it does hold the worker processes and the
consumer log file open.

**Where to look when something fails.** Two different logs, at two different scopes:

- **Per-entry, per-generator logs**: `<dataset_folder>/logs/<edr_product>/<product_type>_log.txt` —
  the
  console output (this codebase's own `print()` diagnostics, plus a full traceback on failure) of
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

  **This folder link only works when browsing via a real static file server** — either the
  `jupyter-server-proxy`-backed `/output/...` route on the same JupyterLab server, or the standalone
  `scripts/serve_reports.sh` (see `docs/report-generation.md`'s "Viewing reports" section for both).
  Confirmed live that Jupyter's own `/files/...` route 403s on a bare directory URL (no autoindex
  support at all); it can serve one already-known `<product_type>_log.txt` file directly, just not list
  a folder of them.
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
