# Intermediate-product discipline: implementation plan

Applies `docs/intermediate-product-discipline.md`'s principles to this project's real, current
intermediate-file landscape (`trn_dataset.py`/`tasks.py`'s huey pipeline and the generator functions
it drives). Read the principles doc first; this doc doesn't restate them.

## Where this actually bites today

`docs/batch-generation.md` already documents one live instance of exactly the failure class the
principles doc targets: `crop`/`hillshade`/`reproject` of the *same* entry can land on different
worker processes under `populate_via_workers()`, each independently re-deriving `entry.camera`
(→ `isis_wac.run_pipeline`, writing to `scratch/isis_wac/<edr_product>/`) and racing on that shared
path — currently worked around at the workflow level ("don't mix product types in one batch"), not
fixed structurally. `entry.dem_ortho_result` (→ `lunaserv.fetch_dem_and_ortho`, writing to
`_work/<edr_product>/dem_filled-tile-0.tif` and friends) has the identical exposure, just not yet
separately documented — same shared-per-entry-scratch, same multi-process race, same class of bug as
the WAC_EMP-migration bug that prompted the principles doc, just concurrent rather than sequential.
This plan's actual payoff is turning that documented workaround into a structural guarantee.

Current landscape, briefly:

- `_work/<edr_product>/` (per entry, already exists, under `output_dir`): DEM/ortho fetch outputs,
  the ortho-shading variant family, `hapke_shade_ortho`'s own fixed-name scratch cubes
  (`hapke_from.cub`/`_phase`/`_incidence`/`_emission`/`_out`, reused sequentially by both the
  real-geometry and reference-geometry calls within one shading run), `render.py`'s `sat_sim`
  outputs, each `TrnTestImage` subclass's own `_mapprojected_path()` output, and `sfs_validation.py`'s
  investigation-only outputs.
- `scratch/isis_wac/<edr_product>/` (workspace-level, **not** per-dataset — keyed by `edr_product`
  alone): the stitched/cropped ISIS cubes. **Decision**: move into `_work/<entry>/` (Phase 3) — the
  cross-dataset-reuse case that motivated keeping it separate isn't actually load-bearing, since real
  datasets are non-overlapping in `edr_product` by construction. Kept as its own distinguished
  subtree there, not merged flat, for a different reason: it's the single most expensive thing to
  regenerate (a real multi-subprocess ISIS toolchain run, not just a reproject/reshade), so it needs
  to survive routine pruning that the cheaper entry-scoped files don't — see Phase 3/"Disk
  management" below.

## Phase 1 — Registry primitive, no behavior change

Add `writes_product(label)` / `reads_product(label)` / `deletes_product(label)` decorators (a small
new module, e.g. `product_registry.py`) plus the load-time uniqueness check for `writes_product`
described in our discussion. Pure infrastructure — apply to zero real functions yet, just land the
mechanism and its own tests (duplicate-label registration raises at import time; `reads`/`deletes`
accept multiple registrants). Cheapest, most isolated phase — good place to settle the exact decorator
shape before anything depends on it.

## Phase 2 — Atomic-publish helper

A small reusable "write to a uniquely-named temp path, then atomically rename to the canonical path"
helper, generalizing `cache.cached_get`'s existing pattern from *fetched* files to *generated* ones.
Needs two shapes, since not every writer in this codebase produces its output the same way:

- A context-manager/wrapper for callers that write directly to a path they choose (most of
  `lunaserv.py`, `render.py`).
- A "run this subprocess with `to=<tmp>`, then rename" variant for ISIS/ASP tools invoked via
  `subprocess_utils.run_quiet` (`isis_wac.py`'s cube pipeline).

Retrofit it into the real single-answer writers first (DEM fetch/fill, the ortho fetch, `sat_sim`
render outputs, ISIS cube generation) — the intentional-variant writers (the ortho-shading family)
come along naturally in Phase 4 once they're split out. **Not every fixed-name file here needs full
product-label treatment**: `hapke_shade_ortho`'s `hapke_from.cub`/etc. are pure single-call scratch —
nothing outside that one function invocation ever reads them, and nothing needs to resume/reuse them
across calls the way real products are. The right fix there isn't a *unique persistent* path (that
just trades a collision bug for an accumulation bug) — it's a call-scoped temporary directory
(`tempfile.TemporaryDirectory()`), deleted automatically when the call returns, success or exception.
No registry entry, no lingering file, nothing to prune later.

## Phase 3 — Path hierarchy

Introduce the scoped hierarchy the principles doc sketches, under `_work/`, with one addition beyond
what that doc shows — a distinguished subtree for the ISIS pipeline output moving in from `scratch/`,
kept separate specifically so it survives routine pruning that the cheaper stuff doesn't need to:

```
_work/<entry>/isis/<label>           # entry-scoped, expensive to regenerate -- prune deliberately
_work/<entry>/<label>                # entry-scoped, shared across generators
_work/<entry>/<generator>/<label>    # generator-scoped
```

Migrate today's flat `_work/<edr_product>/*` writes into it — most of today's DEM/ortho/render outputs
are genuinely entry-scoped (shared by `crop`/`hillshade`/`reproject`) and land at the middle tier;
each `TrnTestImage` subclass's own `_mapprojected_path()` output is generator-scoped and moves under
its own subdirectory; the stitched/cropped ISIS cubes move from `scratch/isis_wac/<edr_product>/`
into the top tier. "Prune this entry" now has two honest answers — `_work/<entry>/` minus `isis/` (the
routine case) or the whole subtree including `isis/` (the rare, deliberate one) — instead of one
blunt operation silently including the expensive part.

## Phase 4 — Single-writer consolidation

Decorate the real writer functions and fix the actual entanglement the WAC_EMP migration found:
`fetch_dem_and_ortho` currently fuses a single-answer concern (the entry's one DEM) with an
intentional-variant concern (the ortho, legitimately shaded multiple ways) behind one function that
takes a caller-suppliable footprint — which is exactly how two callers were able to silently disagree
about the DEM. Split it: a parameterless, `@writes_product`-decorated per-entry DEM fetch, and a
separate variant-labeled ortho-shading writer that takes the already-fetched DEM as an input, not a
parameter it can re-derive. Apply the same audit to `isis_wac.run_pipeline`/`crop_for_camera`
(currently the *documented* race) and to `render.run_sat_sim`'s outputs.

## Phase 5 — Revisit the documented workaround

Once Phases 1-4 land, re-examine `docs/batch-generation.md`'s "don't mix product types in one batch"
guidance against real concurrent runs. Best case: it's no longer a correctness requirement (atomicity
+ single-writer auditing make the race safe by construction) and the doc changes from "must sequence
product types" to "may still be worth sequencing for efficiency." If it's still required for some
reason not yet identified, that's a real finding to document, not something to assume away.

## Disk management

Kept simple, deliberately not over-built:

- Anything scoped to one call's own lifetime (`hapke_from.cub`/etc.) uses a call-scoped
  `TemporaryDirectory`, auto-deleted when the call returns — Phase 2. Nothing accumulates because
  nothing survives past the call that created it.
- Anything else genuinely disposable-anytime — not a real product per principle 1, just not cleanly
  scoped to a single call — is written consistently under `config.scratch_dir` (not `_work/`, which
  after Phase 3 holds only real, worth-preserving intermediates, `isis/` included). No new tooling:
  periodically clearing `scratch/` by hand stays sufficient, as long as everything landing there is
  genuinely safe to lose.
- Stale orphaned *product* variants (e.g. old `ortho_shaded_*` suffix combinations from a since-changed
  naming scheme) are explicitly out of scope for this plan — not worth building detection tooling for.

## Verification (per phase, not deferred to the end)

- Phase 1: unit tests on the registry alone (no real pipeline involved).
- Phase 2: unit tests with fault injection (kill the process mid-write, confirm no partial file is
  ever visible at the canonical path).
- Phase 3/4: the existing heavy test suite (`test_wac_emp_ortho_source.py`,
  `test_sfs_validation_lambertian_incidence.py`, `test_lunaserv_campt_validation.py`) must keep passing
  unchanged — these are exactly the tests that would catch a re-introduced mismatch, as they already
  did once this session.
- Phase 5: a real `populate_via_workers(product_types=("crop", "hillshade"), workers>1)` run against
  never-before-generated entries, *without* the current sequencing workaround, checked for a clean
  result — the concrete test this whole plan is ultimately in service of.
