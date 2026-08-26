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
`trn_dataset.PRODUCT_TYPES = ("crop", "hillshade")`. To include `reproject` (implemented but
opt-in — see `trn_dataset.py`'s module docstring), pass it explicitly:

```python
PRODUCT_TYPES = ("crop", "hillshade", "reproject")

dataset.populate_via_workers(product_types=PRODUCT_TYPES, workers=4)
dataset.status(product_types=PRODUCT_TYPES, huey_instance=tasks.huey_parallel)
```

**Pass the same `product_types` to every call in a given workflow.** It isn't remembered between
calls — `status()`/`truncate()` after a `populate_via_workers(product_types=(..., "reproject"))` run
will silently only look at `crop`/`hillshade` unless you pass `product_types=PRODUCT_TYPES` there
too, making `reproject`'s real state invisible rather than raising anything.

## Recommended workflow

```python
import trntest
from trntest import tasks, trn_dataset

config = trntest.load_config()
images = trntest.read_manifest("notebooks/dataset_manifest.csv")
dataset = trn_dataset.TrnTestDataSet.create(config.output_dir / "trn_dataset", images, config)

PRODUCT_TYPES = ("crop", "hillshade")  # add "reproject" once you actually want it too

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
`docker compose run` workers safe are gone as of the `huey` migration
(`docs/history.md`'s dated entry) — `populate_via_workers()`'s own worker pool is the supported way
to get real parallelism now, not multiple top-level calls.

**Mixing one entry's product types across concurrent workers is no longer a correctness
requirement (2026-08-23, `docs/history.md`'s Phase 79 entry).** This used to be the
sharpest, most likely-to-actually-happen edge case here: `crop`, `hillshade`, and `reproject` for
the *same* manifest entry all depend on `TrnTestEntry.camera`, which calls `isis_wac.run_pipeline`
against that entry's own ISIS working directory — a shared write path two concurrent workers could
land on at the same time, each independently re-deriving `entry.camera`/`entry.dem_ortho_result`.
The intermediate-product access-discipline work closed this structurally, not just in this one
spot: that shared ISIS state moved from a workspace-level `scratch_dir/isis_wac/<edr_product>/`
into the entry's own `_work/<entry>/isis/` tier, and its real writers
(`isis_wac.crop_for_camera`/`run_framestitch`, plus `lunaserv.fetch_dem`/`fetch_and_shade_ortho`)
now publish atomically (`product_registry.atomic_publish_path`/`atomic_publish` — write to a
uniquely-named temp path, then atomic rename) — two workers racing on the same label now converge
on an equivalent, complete result rather than tearing a partial file or hitting ISIS's own
overwrite-refusal mid-write.

**Since then (2026-08-24), this scenario can no longer happen at all, not just safely — task
granularity moved from `(entry, product_type)` to `entry` (`docs/history.md`'s dated entry).** One
`huey` task now covers every requested, still-pending product type for a given entry, run
sequentially within that single task/process; `populate_via_workers(workers=N)` parallelizes across
*entries* only. Two of the same entry's product types landing on two different workers at the same
moment — the scenario the atomic-publish work above made safe — is now structurally impossible in
addition to being safe, and as a real side benefit `entry.camera`/`entry.dem_ortho_result` (both
`functools.cached_property`) are computed once per entry and reused across its product types
instead of being redundantly rebuilt once per worker that used to land on it.

**Live-validated** (2026-08-23): two never-before-generated manifest rows,
`populate_via_workers(product_types=("crop", "hillshade"), workers=2)`, product types *not*
sequenced. The consumer log confirms real concurrency, not just luck — one entry's own `crop` and
`hillshade` tasks started 27ms apart on two separate worker processes and ran overlapping for the
next 27-40s — and both product types for both entries came out `done`, no errors. See
`docs/history.md`'s dated entry for the full run.

**A follow-up validation the same day found this first run hadn't actually covered
`entry.dem_ortho_result`'s own concurrency** (`crop` never touches it — only `hillshade`/`reproject`
do): `populate_via_workers(product_types=("hillshade", "reproject"), workers=2)` against fresh rows
initially failed, twice, with two different real races (a shared Hapke-shading scratch-cube
collision, then a separate camera-building/CK-kernel-resolution race) — both fixed
(`docs/history.md`'s Phase 80 entry), after which the same call completed cleanly with confirmed
concurrent execution (7ms apart, 50-80s overlap). `reproject` is still opt-in (not in
`PRODUCT_TYPES`), but is now validated safe to mix with `hillshade` under concurrent workers too.

Both validations above describe the pre-2026-08-24 `(entry, product_type)` task granularity —
`crop`/`hillshade`/`reproject` of the *same* entry landing on two different workers, the specific
thing being exercised, can no longer happen at all post-granularity-change (see above). The
atomic-publish fixes these validations confirmed remain valuable regardless — for genuine
cross-entry write collisions and crash/partial-write safety, not just this now-eliminated race.

Sequencing by product type (below) is now a pure throughput choice, not a safety requirement — it
no longer protects against a same-entry cross-worker race (structurally impossible now, see above),
just against many *different* entries' tasks all cold-fetching the same not-yet-cached external
resource around the same time (see "Cold-cache concurrent fetch races" below). A
mixed-`product_types` call like the validated one above is not expected to race either way:

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

**Where to look when something fails.** The consumer subprocess's own stdout/stderr (not the
individual task tracebacks huey stores) go to `<output_dir>/.huey/consumer.log` — check there for
consumer-level problems (a worker crashing, `-k process` health-check restarts) that wouldn't show
up in a per-task `TaskException`. This file is overwritten (not appended) on every
`populate_via_workers()` call, so check it *before* starting another batch if you need to debug a
prior run's failure.

## Verification

This was live-validated against real manifest entries (not just fakes) — two never-before-generated
rows from `notebooks/dataset_manifest.csv`, `populate_via_workers(limit=2, workers=2)`, both crop
cubes and hillshade renders completed correctly via real SPICE/ISIS/ASP calls across two separate
worker processes in 53.4s total. See `docs/history.md`'s dated entry for the full trail.
