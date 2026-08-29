# Docs rework: applying `docs/docs-style.md` across `docs/` and `src/`

**Status: `docs/*.md` mostly done; `src/trntest/*.py` docstrings/comments just started (1 of 18
files).** The original problem was docstrings, not just standalone docs — `docs/docs-style.md`
covers both, but so far the actual editing has gone almost entirely into `docs/*.md`. The in-code
half is the bigger remaining job.

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
- `docs/batch-generation.md` — full brevity/voice pass (`docs/history.md` refs removed; the
  "mixing product types" narrative condensed from a 3-phase investigation trail down to the current
  task-granularity model and why the race it describes is now structurally impossible).
- `AGENTS.md` — doc index kept current through all of the above.
- `src/trntest/lunaserv.py` — full pass, two rounds. Round 1 (1663 -> 1535 lines): all 41
  `docs/history.md` citations removed, "real"/"confirmed" filler cut, module docstring corrected (it
  still described the deprecated Lunaserv-WMS path as the live default; fixed to name
  Astropedia/WAC_EMP). Round 2 (-> 1814 lines, per user feedback on round 1): docstrings cut further,
  to genuinely minimal — what a function does plus RST `:param:`/`:returns:`/`:raises:` fields (used
  where the params/return value benefit from being spelled out, not forced everywhere) — with
  everything else (rationale, caveats, comparisons, open items) moved to a plain comment block as the
  first lines of the function body, so it stays out of `help()`/`.__doc__`. `docs/docs-style.md`
  itself updated with this refined rule first. Verified with `trntest-lint` both rounds (`ruff
  format`/`ruff check`/`mypy` all clean on this file). No code logic touched either round, so no
  notebook re-run was needed.

**Mechanical only, not a content rework**: `docs/plan.md` and ~30 `src/trntest/*.py` files got
cross-references *fixed* (pointers updated to the files things moved to) as a side effect of the
`data-sources.md` split — their own prose wasn't edited for style beyond that.

## Not yet done

**`src/trntest/*.py` docstrings/comments — the original complaint, still mostly untouched.** 17
files still cite `docs/history.md` (`lunaserv.py` done, see Completed above). Rough priority order
(citation count, then size, as a proxy for how much chatty/historical material likely needs trimming):

| File | `docs/history.md` cites | Lines |
|---|---|---|
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

For each: remove `docs/history.md` citations (state the load-bearing fact directly, or cut it), trim
docstrings to genuinely minimal (what the function does, plus RST `:param:`/`:returns:`/`:raises:`
fields where they clarify — see `docs/docs-style.md`'s current wording), move rationale/caveats/open
items to a plain comment block as the first lines of the function body (not above the `def`, so it
stays out of `help()`), fix "real"-as-filler and em-dash sentence-stacking. `isis_wac.py` is next —
worst offender remaining by a wide margin.

**Docs not yet reworked**:
- `docs/plan.md` (~620 lines) — flagged in conversation as its own future index-pattern candidate,
  not started. Cross-references into it were kept accurate, but its prose/length weren't addressed.
- `docs/environment.md` — its "ephemeral VPS, archive/restore" framing is confirmed stale (the VPS
  data store now persists across sessions); a full rewrite is owed, deferred so far.
- `README.md`, `docs/reproject-fov-investigation.md`, `docs/wac-jigsaw-investigation.md`,
  `docs/proposed-tasks/report-plan.md`, `docs/proposed-tasks/corrected-overlay-cam2map-plan.md` —
  not reviewed against `docs/docs-style.md` at all yet (the two investigation docs only got
  mechanical link fixes).

## If resuming

1. Re-run `grep -rc "docs/history.md" src/trntest/*.py` to check the table above is still current —
   other sessions may have touched these files since.
2. Work one file at a time; `isis_wac.py` next. Re-verify with `trntest-lint` and, if a docstring
   change touches a function a notebook exercises, `scripts/run_notebook.sh` on the relevant
   notebook before committing. A pure docstring/comment edit with no code-logic change (confirm via
   `git diff`) doesn't need a notebook re-run — `lunaserv.py`'s pass didn't need one.
3. Delete this plan once `src/trntest/*.py` is clean of `docs/history.md` citations and the
   remaining docs above have had their pass, or fold whatever's left into a narrower follow-up.
