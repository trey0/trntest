# Pacing `spiceinit web=yes` against multi-worker overload

A plan, not yet implemented — written up after `trntest2`'s first production run (8 workers)
collapsed on this exact failure mode. Nothing below has been coded; it's what to check and build,
plus a recommended sequencing, so a future session doesn't have to re-derive the incident or the
design tradeoffs.

## The incident

`trntest2` (69 entries, `select_datasets.py` pick 2), populated via `populate_via_workers(workers=8)`
immediately after a clean 2-entry/1-worker warm-up pass: **11 of 67 remaining entries succeeded, 56
failed** (`pct_ok` bottomed out at 16.4%). Every single failure is the same class of error, from
ISIS's `spiceinit` command itself:

```
**ERROR** An error occurred when talking to the server. The server is unable to handle the request at this time.
```
(186 occurrences across the run's logs) or

```
**ERROR** An error occurred when talking to the server. An unknown error related to the server occurred.
```
(34 occurrences) — both from `spiceinit web=yes` failing to reach/complete against its remote
NAIF/USGS SPICE pointing-correction web service.

For contrast: `trntest1`'s own real 8-worker production run (207 entries, the night before) hit this
exact same error on only 3 entries (~1.5%) — annoying but tolerable, and treated at the time as
transient network flakiness worth a plain retry (see `docs/history.md`'s Phase 119 entry). `trntest2`
hit it on 84% of attempts. The scale of the difference points at *our own concurrent load* tripping
server-side throttling, not an unrelated external outage — nothing else about the setup changed
between the two runs.

## Root cause

`isis_wac.run_spiceinit` (called twice per entry, once each for the `vis_even`/`vis_odd` cubes
`camera.build_camera` needs — see `run_pipeline`'s own two calls) is a bare, unguarded subprocess
call:

```python
run_quiet(["spiceinit", f"from={cub_path}", "web=yes", "shape=user", f"model={shape_model_path}"])
```

`entry.camera` is a `functools.cached_property`, so this is **2 calls per entry**, not per product
type — but with `workers=8`, that's still up to **16 concurrent, completely uncoordinated** requests
to the same remote service. A second call site, `attach_dem_shape_model` (used by
`control_network.resolve_control_points`'s ground-to-image queries, not the default
`crop`/`hillshade`/`report`/`gallery`/`reproject` pipeline), makes the identical raw call
independently — not routed through `run_spiceinit` at all, so a fix needs to cover both, or
consolidate them into one shared internal helper first.

**This is a real gap, not a regression**: `docs/batch-generation.md`'s "Cold-cache concurrent fetch
races — mostly resolved" section describes a *different* fix (`cache.py`'s `_pacing_gate`,
serializing every `cached_get` call VPS-wide via `fcntl.flock`) that has nothing to do with this
code path. `spiceinit web=yes` is ISIS's own subprocess making its own HTTP request internally —
entirely outside `cache.py`, so none of that existing protection applies here. This has presumably
always been a latent risk; `trntest1`'s low hit rate just didn't expose it clearly.

## Prior art to reuse — and where it doesn't transfer

`cache.py`'s `_pacing_gate()` is the direct template: a cross-process `fcntl.flock` mutex at a fixed
path under `DEFAULT_CACHE_ROOT`, held for the whole guarded operation (not just a quick check), so at
most one instance is ever live VPS-wide — coordinating not just one `populate_via_workers()` call's
own worker pool, but every worktree/agent session sharing this VPS's `cache/` (see that function's
own comment, and `docs/environment.md`'s Phase 36 incident it originally answered). The same
cross-process/cross-agent scope applies here: two agents each running their own `populate_via_workers()`
batch at the same time would combine their `spiceinit` load exactly the way `docs/batch-generation.md`
warns about for external fetches generally.

One existing piece of prior art does *not* transfer directly: `resolve_wac_ck_kernels`'s own comment
explicitly rejects retry/backoff around its own `spiceinit` call ("a failure should surface
immediately, not loop silently; this persisted cache is the resilience mechanism, not automatic
retry"). That reasoning is scoped to *that* call site specifically — it only ever needs to succeed
once per unique EDR product, and a successful resolution is cached forever
(`cache_root/isis_ck_resolution/<edr_product>.json`), so a transient failure there just costs a retry
on the *next* `populate()` call, not a stuck loop. The two call sites actually responsible for this
incident (`run_pipeline`'s per-entry calls, `attach_dem_shape_model`'s copy) have no equivalent
persisted-success cache — every entry needs a fresh, real `spiceinit web=yes` success every time it's
(re)generated, so the same "cache is the resilience mechanism" argument doesn't apply to them.

## Open design questions

- **Full serialization vs. a small concurrency limit.** `cache.py`'s gate allows exactly one live
  fetch VPS-wide. `spiceinit` calls are likely slower than a typical WMS tile fetch (a real ISIS
  subprocess, not just an HTTP GET), so full serialization could meaningfully slow an 8-worker run's
  wall-clock time for this one step. A small semaphore (e.g. 2-3 concurrent) might preserve more
  throughput while still respecting whatever the remote service's real capacity is — but that
  capacity isn't known; starting with full serialization (matching existing precedent, safest
  default) and loosening it later if measured to be overly conservative seems like the right order,
  not the reverse.
- **A separate lock, not `cache.py`'s existing one.** `spiceinit web=yes` talks to a different host
  (NAIF/USGS's SPICE service) than `cached_get`'s callers (Lunaserv, the PDS ODE API, USGS's S3
  kernel bucket). Sharing one lock would over-serialize unrelated traffic for no benefit — a WMS tile
  fetch blocking on an in-flight `spiceinit` call (or vice versa) protects nothing, since they're
  independent rate limits. A dedicated lock path (e.g.
  `DEFAULT_CACHE_ROOT / ".spiceinit_pacing.lock"`) keeps the two concerns separate the way the two
  external hosts already are.
- **Retry-with-backoff on top of pacing, or pacing alone?** `cached_get` can inspect a real HTTP 429
  and a `Retry-After` header; `spiceinit`'s failure surfaces only as a `CalledProcessError` with
  ISIS's own generic error text in captured stdout/stderr — no structured signal to size a backoff
  from. A simpler fixed-delay retry (a small `_MAX_FETCH_ATTEMPTS`-style cap, matching on the known
  error text) layered on top of the pacing gate would add resilience against whatever residual
  contention remains even after serializing our own load — worth doing, but secondary to the gate
  itself, which should remove most of the problem on its own.
- **Where the gate logic should live.** Either a small `isis_wac.py`-local copy of the
  `_pacing_gate` pattern, or generalizing `cache.py`'s existing `_pacing_gate()` into a
  lock-path-parameterized helper both modules call. The latter avoids duplicating the (small but
  fiddly) `fcntl.flock` contextmanager; the former keeps `cache.py` scoped to actual HTTP fetches.
  Not resolved here — a call for whoever implements this.
- **Does this also affect the one-time kernel-resolution call?** `resolve_wac_ck_kernels`'s own
  `spiceinit` call (via `_spiceinit_vis_even_cube`) is a *third*, less-frequent call site. Wrapping
  it in the same pacing gate is presumably harmless (it would just wait its turn like everything
  else) — but its own deliberate no-retry stance (see above) should stay as-is; pacing and retry are
  separable concerns.

## Testing approach

`tests/test_cache.py`'s `test_pacing_gate_serializes_concurrent_callers` is the template: several
threads in one test process each acquire the gate via a stubbed-out guarded operation (not a real
`spiceinit` subprocess call), tracking max concurrent holders and asserting it never exceeds the
intended limit. The same technique works here without needing real ISIS/network access — mock
`subprocess.run`/`run_quiet` to a fast fake, then assert on concurrency and (if retry is added) on
retry-count behavior against a fake that fails N times before succeeding.

## Recommended sequencing

1. Implement the pacing gate (dedicated lock, serialized to start) wrapping both real call sites
   (`run_spiceinit`, `attach_dem_shape_model`'s inline call) — consolidating them into one shared
   helper first if that's the cleaner path.
2. Add the concurrency test above; `trntest-lint`/full suite clean.
3. **Validate against a real batch before trusting it at full scale**: retry `trntest2`'s 56 failed
   entries (`retry_failed=True`) at a deliberately reduced worker count first (e.g. 2-4), not
   immediately back at 8 — confirms the fix actually holds under real network conditions before
   committing to the same concurrency that caused the incident.
4. Once clean at reduced concurrency, retry at the original `workers=8` to confirm the gate (not
   just luck/reduced load) is what's holding.
5. Fold the durable facts (why this exists, the lock path, the serialization-vs-throughput tradeoff
   actually chosen) into `docs/caching.md`'s existing "Retry/backoff/pacing policy" section — it's
   the natural home, already documenting `cached_get`'s equivalent mechanism — and delete this file.
