# Docs rework: applying `docs/docs-style.md` across `docs/` and `src/`

**Status: `docs/*.md` mostly done; `src/trntest/*.py` docstrings/comments barely started.** The
original problem was docstrings, not just standalone docs — `docs/docs-style.md` covers both, but
so far the actual editing has gone almost entirely into `docs/*.md`. The in-code half is the bigger
remaining job.

## Goal

Apply `docs/docs-style.md`'s rules everywhere they're supposed to hold: docs/*.md AND
docstrings/comments in `src/trntest/*.py`. Concretely — no `docs/history.md` citations outside
`docs/history.md` itself, docstrings define the interface not the implementation/history, taciturn
voice (cut "real"/"genuine"/"actual" as bare intensifiers, one idea per sentence), one source of
truth per fact, thin index files, sensible file naming.

## Completed

- `docs/docs-style.md` — written (the rules above).
- `docs/caching.md` — full brevity/voice pass (cut ~25%, `docs/history.md` refs removed).
- `docs/intermediate-product-discipline.md` — brevity pass; `TrnTestDataSet`'s on-disk layout folded
  in as a worked example.
- `docs/data-sources.md` — was 1506 lines, got a full content pass (all `docs/history.md` refs
  removed, most-narrative sections condensed) before being split into a 25-line index.
- `docs/data-sources/*.md` (7 new files: `lunaserv-wms`, `astropedia-gld100`, `wac-emp-pds4`,
  `robbins-craters`, `spice-kernels-naif`, `spice-kernels-isis`, `lroc-wac-edr-cdr`) — inherit the
  already-reworked `data-sources.md` content.
- `docs/external-tools.md`, `docs/crater-grading.md`, `docs/image-pipeline.md`,
  `docs/dataset-selection.md` — new, assembled from the same already-reworked material.
- `docs/proposed-tasks/` convention established (this doc lives under it); `report-plan.md` and
  `corrected-overlay-cam2map-plan.md` moved there.
- `AGENTS.md` — doc index kept current through all of the above.

**Mechanical only, not a content rework**: `docs/plan.md` and ~30 `src/trntest/*.py` files got
cross-references *fixed* (pointers updated to the files things moved to) as a side effect of the
`data-sources.md` split — their own prose wasn't edited for style beyond that.

## Not yet done

**`src/trntest/*.py` docstrings/comments — the original complaint, still mostly untouched.** 18
files still cite `docs/history.md`; `lunaserv.py` is worst by far. Rough priority order (citation
count, then size, as a proxy for how much chatty/historical material likely needs trimming):

| File | `docs/history.md` cites | Lines |
|---|---|---|
| `lunaserv.py` | 41 | 1663 |
| `isis_wac.py` | 12 | 1284 |
| `dataset.py` | 6 | 427 |
| `plotting.py` | 5 | 1257 |
| `sfs_validation.py` | 4 | 324 |
| `config.py` | 4 | 263 |
| `camera.py` | 4 | 608 |
| `cache.py` | 4 | 287 |
| `tasks.py` | 3 | 194 |
| `render.py` | 3 | 204 |
| `pose_alignment.py` | 3 | 515 |
| `trn_dataset.py` | 2 | 682 |
| `tie_points.py` | 2 | 491 |
| `spice_kernels.py` | 2 | 345 |
| `product_registry.py` | 2 | 194 |
| `dataset_selection.py` | 2 | 283 |
| `maneuver_detection.py` | 1 | 269 |
| `_lint.py` | 1 | 230 |

For each: remove `docs/history.md` citations (state the load-bearing fact directly, or cut it),
trim docstrings back to interface-only, move implementation-detail material to an inline comment
near the code it explains (or cut it), fix "real"-as-filler and em-dash sentence-stacking.
`lunaserv.py` is the obvious place to start — worst offender by a wide margin, and the file
`docs/docs-style.md`'s own Phase 85 history entry was written about.

**Docs not yet reworked**:
- `docs/plan.md` (~620 lines) — flagged in conversation as its own future index-pattern candidate,
  not started. Cross-references into it were kept accurate, but its prose/length weren't addressed.
- `docs/environment.md` — its "ephemeral VPS, archive/restore" framing is confirmed stale (the VPS
  data store now persists across sessions); a full rewrite is owed, deferred so far.
- `docs/batch-generation.md`, `README.md`, `docs/reproject-fov-investigation.md`,
  `docs/wac-jigsaw-investigation.md`, `docs/proposed-tasks/*.md` — not reviewed against
  `docs/docs-style.md` at all yet (the two investigation docs only got mechanical link fixes).

## If resuming

1. Re-run `grep -rc "docs/history.md" src/trntest/*.py` to check the table above is still current —
   other sessions may have touched these files since.
2. Work one file at a time; `lunaserv.py` first. Re-verify with `trntest-lint` and, if a docstring
   change touches a function a notebook exercises, `scripts/run_notebook.sh` on the relevant
   notebook before committing.
3. Delete this plan once `src/trntest/*.py` is clean of `docs/history.md` citations and the
   remaining docs above have had their pass, or fold whatever's left into a narrower follow-up.
