# Intermediate-product access discipline

A statement of principles for how this project's generated (non-final, non-source) intermediate
files should be named, stored, and shared across code paths — not an implementation plan.

## Principles

1. **Every intermediate artifact has one well-defined identity.** Two categories, not one:
   - *Single-answer artifacts* — exactly one valid value for a given scope (an entry, or an entry +
     generator). Identified by a fixed label alone. No caller-supplied parameter should be able to
     change what such an artifact contains — if it can, two code paths can silently disagree about
     what "the" artifact for that scope actually is.
   - *Intentional-variant artifacts* — multiple valid values are meant to coexist by design (e.g. a
     hillshade rendered under several photometric-parameter combinations, kept side by side for
     comparison). Identified by label plus whatever parameters/variant actually determine content,
     baked into the identity itself.

2. **Exactly one code path writes any given label.** This should be auditable, ideally cheaply —
   discoverable by inspection or a lightweight registry, not re-derived by reading every caller. A
   function that legitimately produces variants of a label still owns the whole family: one *owner*,
   not one *file*.

3. **Storage is hierarchical, per-scope, and prunable without tracking individual files.**
   Intermediate files that aren't part of a dataset's final output live under one disposable subtree.
   The path hierarchy encodes scope directly, so no separate manifest of "which files belong to this
   entry" is needed:
   ```
   <dataset>/_tmp/<entry>/<label>               # entry-scoped (shared across generators)
   <dataset>/_tmp/<entry>/<generator>/<label>   # generator-scoped
   ```
   Pruning is then deletion at the right level: the whole `_tmp/` subtree, or one entry's or one
   entry+generator's own subtree. The directory a label lives under already records whether it's
   shared (principle 1) or generator-specific.

4. **Published artifacts are immutable, and become visible atomically.** A writer produces its
   output at a uniquely-named temporary location and only exposes it at its canonical identity via an
   atomic rename, once complete. Nothing is edited in place after publication; a change in what
   should be produced means a new identity (principle 1), not a mutation. This makes write/read and
   write/write races safe by construction: a reader either sees no artifact yet or a complete one,
   never a partial one, and two writers racing the same label converge on an equivalent result, not a
   torn file.

5. **Each artifact has a declared access mode per task: writer, reader, or (optionally) deleter.**
   At any point it's touched by one writer alone, any number of concurrent readers, or one deleter
   alone — never a mix. Principle 4 makes "writer completes before any reader starts" mostly a
   performance concern, not a correctness one. Deletion doesn't get that protection for free —
   removing a file out from under an active reader is a real hazard — so deletion is the one mode
   that needs "no active readers remain" before it can run. Not implemented yet; this just states
   what its contract would need to be.

6. **No locks.** Stale locks are a known, real source of confusing-to-diagnose failures. Rely on
   atomicity, immutability, and single-writer auditing instead of runtime mutual exclusion to keep
   concurrent access safe.

7. **Write/read/delete relationships are declared, not implicit.** Which labels a given piece of
   code writes, reads, or deletes should be a legible, checkable fact attached to that code — not
   something only recoverable by reading its implementation.

## `TrnTestDataSet` on-disk layout

A concrete instance of the principles above. See `../README.md`'s `trn_dataset.py`/`trn_products.py`/
`tasks.py` rows for the dataset-folder/class-hierarchy/task-queue design.

**Layout**: `<output_dir>/trn_dataset/` (not `<output_dir>/dataset/`, which is
`candidate_window.generate_dataset()`'s own, separate flat per-`product_id` layout — the two don't collide in
meaning or content) holds `manifest.csv` plus `crop/<edr_product>_crop.{cub,json}`,
`hillshade/<edr_product>_hillshade.{tif,json}`, an empty reserved `reproject/`, per-entry
intermediates under `_work/<edr_product>/` (`.tsai`, DEM/ortho tiles, pre-copy render output — kept
out of `crop`/`hillshade` so those two only ever hold the canonical named pair), and each generator
attempt's captured console output under `logs/<edr_product>/<product_type>_log.txt`
(`tasks._capture_generator_log`, see `docs/report-generation.md`'s on-disk layout and
`docs/batch-generation.md`'s "Where to look when something fails" section) — a single-answer
artifact per entry+generator per this doc's own principle 1, written once per real (non-no-op)
`generate()` attempt. Task-queue state
lives outside this folder entirely now, in `<output_dir>/.huey/` — two separate `huey` sqlite
databases (`tasks.db` for `populate()`, `tasks_parallel.db` for `populate_via_workers()`'s real
worker pool), each shared by every dataset under that `output_dir` — see `src/trntest/tasks.py`'s
module docstring. Filenames key on `edr_product` (`M1327210646CE` →
`crop/M1327210646CE_crop.cub`), matching `isis_wac.py`'s own scratch-dir convention; row lookup
(`TrnTestDataSet[key]`) keys on `product_id` instead, matching `candidate_window.generate_dataset()`'s
existing per-image folder convention — the two are always equal in today's real manifest, so this
split is currently low-risk, just future-proofing.

**The real WAC pipeline's own raw-EDR scratch** (`_work/<edr_product>/isis/` — split, calibrated,
and stitched cubes, `isis_wac._spike_dir`) lives inside each `TrnTestDataSet`'s own `_work/`, one
subtree per entry. Was `config.scratch_dir/isis_wac/<edr_product>/`, a workspace-level shared path,
until 2026-08-23: the cross-dataset reuse that separation used to serve isn't load-bearing (real
datasets are non-overlapping in `edr_product` by construction).

As of the crop-preferred refactor below, this subtree is purely disposable, no longer a special
case: `isis_wac.ensure_crop_for_camera` wipes it entirely once the crop is safely published to
`cache/wac_crop/<edr_product>_crop.cub` (`config.delete_isis_intermediates`, default `True`) — same
lifecycle as the rest of `_work/<entry>/`, not exempted from pruning. Cross-dataset reuse moved with
it: `cache/wac_crop/` (keyed by `edr_product`, shared across every `TrnTestDataSet` pointing at the
same `cache_root`, not scoped to one dataset folder) is now where that benefit lives, rather than
this `_work/`-local scratch. `build_camera`/`crop_footprint_corners_for_camera`/`TrnTestEntry.
crop_result` all check `isis_wac.cached_crop_path` before ever touching ISIS, so re-populating an
already-generated entry (from any dataset) is cheap without needing this scratch subtree to still
exist. See `docs/caching.md`'s "WAC crop caching" section for the full rationale.
