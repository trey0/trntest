# `docs/plan.md` cleanup: delete resolved-item narratives, trim the Architecture table

**Status: done, awaiting review.** Working in worktree `docs-proposed-tasks-style-0defc6`, branch
`claude/docs-proposed-tasks-style-0defc6`. Batch 1 done and **merged to `main`** (`f93e282`):
Architecture table rewritten to the shorter bar below (also added a missing `craters.py` row, then
alphabetized the whole table), two stray `docs/history.md` citations cut, and two fully-resolved
"Known open items" narratives deleted (the CK-kernel investigation, the crater-overlay/`OverlayLayer`
implementation retelling).

Batch 3 done, **pushed to branch**: worked "Known open items" from where batch 1 stopped (the
`crater_depth.py` entry) down to the end of the file, condensing the rest of the resolved-narrative
bulk (crater sharpness grading, camera-pose-alignment, the tie-points die5 bug, the report
prototype, the Phase 70-79 Hapke saga, the DEM filename-collision bug, the saturation open
question, the image-resolution fix) to their load-bearing current-state facts. Lines 89-272 (batch
1's own scope, already user-reviewed and merged) deliberately left untouched at the time.

**Final content pass done and merged to `main`** (`e55292b`): the user reviewed the file and
flagged that it was still fundamentally wrong at a higher level than wording -- "What this is"/
"Status" described a stale premise (a single-image demo notebook as the point of the repo) when the
actual current product is dataset population (`select_datasets.py` selects diverse entries,
`populate()`/`populate_via_workers()` runs the generators over each one, both described in
`docs/generators.md` rather than repeated), and "Known open items" was still misleading since most
of its surviving entries (including the ones batch 1 chose to keep at lines 89-272) were themselves
"Resolved," not open, in violation of `docs/docs-style.md`. Rewrote "What this is"/"Status" to state
the real current product and its real incompleteness (report generation unfinished, no
dataset-scale run yet); renamed "Known open items" to "Open items" and cut it to 8 genuinely-open
entries (everything else deleted, since the load-bearing facts already lived in the relevant
module's own docstring/comment, per `docs/docs-style.md`); resolved the previously-deferred
Architecture-table-additions question by adding rows for the 7 substantive missing modules
(`control_network.py`, `crater_depth.py`, `crater_depth_batch.py`, `pose_alignment.py`, `report.py`,
`subprocess_utils.py`, `wac_camera_model.py` -- `__init__.py`/`_lint.py` skipped as pure infra).

Deleting all those "Resolved" entries orphaned ~20 code/notebook comments across 10 files that cited
"see `docs/plan.md`'s open items" for exactly that content -- fixed each one (inlined the fact
directly, or redirected to the real reference doc, e.g. `docs/wac-jigsaw-investigation.md`) rather
than leave them dangling. Found and fixed two unrelated pre-existing staleness bugs surfaced along
the way: a notebook citing a "Crater depth" section that had moved from `docs/data-sources.md` to
`docs/crater-grading.md`, and `docs/external-tools.md`/`docs/wac-jigsaw-investigation.md` both still
claiming the pipeline's control points are ellipsoid-only when `isis_wac.run_spiceinit`'s default
switched to a real DEM shape model. Flagged (not fixed) a separate finding this surfaced:
`isis_wac.attach_dem_shape_model` has zero production callers and a stale comment describing a
pipeline default that no longer exists -- spun off as its own task.

**Structural pass done, pushed to branch, not yet reviewed/merged**: per further user request, added
a "Notebooks" section (`## Notebooks (\`notebooks/\`)`, two tables -- "Primary notebooks"
(`image_generation.py`/`select_datasets.py`) and "Other notebooks" (the remaining 8) -- replacing
the two prose paragraphs that used to describe the two primary ones only), spun "Open items" out
into its own file (`docs/proposed-tasks/open-items.md`, `plan.md` now just points to it), and
hyperlinked every Architecture-table module name (to its `src/trntest/` source), every Notebooks
entry (to its `.ipynb`, or `.py` for the one unpaired template), and every `docs/*.md` cross-reference
throughout both tables and the surrounding prose. `docs/plan.md` is now 107 lines (was 536 before
batch 1).

**Remaining**: none, once this structural pass is reviewed and merged -- delete this plan doc then,
per its own closing convention below.

## The problem

`docs/plan.md`'s "Known open items" section (lines 86-617 in the version this was scoped
against -- over 500 of the file's 623 lines) is mostly **resolved-item narratives**, not open
items: dozens of "**Resolved**: ... confirmed live ... Fixed by ..." entries, each a full
blow-by-blow investigation retelling, each already duplicated in `docs/history.md` (the entries
themselves cite it, e.g. "see `docs/history.md`'s Phase 78 entry"). This is `docs/history.md`
content living in the wrong file -- a direct "one source of truth per fact" violation, and exactly
the kind of docs/history.md-style narrative `docs/docs-style.md` says shouldn't exist outside
`docs/history.md` itself, just physically relocated instead of actually avoided.

The Architecture table (lines 41-65) has the same problem at the per-module level: each row is a
dense paragraph of Phase numbers and "confirmed live" rationale rather than a scannable one-line
responsibility -- the opposite of what `AGENTS.md`/`docs/plan.md`'s own stated purpose requires
("a fast, scannable map... kept up to date").

Also: 161 `real`/`genuine`/`actual` hits, and a stale `docs/data-sources.md` reference (5 hits) to
repoint at the specific post-split file each one actually needs (see `docs/data-sources.md`'s own
index table).

## What a real fix looks like

**Per the user's own guidance (2026-09-01): be fairly aggressive here.** For each "**Resolved**"
entry in "Known open items," the default is to just **delete** it -- don't trim it, don't summarize
it in place, and don't feel obligated to first confirm it's redundantly noted in `docs/history.md`
if the entry's own enduring lessons-learned value looks low (a narrow bugfix retelling with nothing
a future reader would need to rediscover). Reserve the "check it's preserved elsewhere first"
caution for entries that read as genuinely reusable engineering lessons, not routine bug-and-fix
narration. Keep only:
- Items that are genuinely still open (not yet resolved).
- A scannable current-state summary, if the resolved fact affects how `plan.md`'s own Architecture
  table or Status section should read today.

**For the Architecture table: entries should be very short -- a module's row should generally be
shorter than that module's own docstring, not longer.** One or two sentences of responsibility, not
design rationale (which belongs in the module's own docstring/comments, already mostly moved there
by Parts 1/3 of `docs-style-second-pass.md`). **If a module's own contents resist a succinct
description -- if summarizing it keeps turning into a grab-bag of unrelated topics -- treat that as
a signal the module's own boundaries have drifted, not a reason to write a longer table entry.**
Flag it rather than writing around it; the fix in that case is refactoring the module to align on
one theme, not a better-written summary of a module that's doing too many unrelated things.

Could plausibly shrink the file from 623 lines to somewhere around 150-200, possibly less given how
aggressive the "just delete it" default above is.

## Suggested approach

Given the size, this is a multi-batch job, same review workflow as `docs-style-second-pass.md`
used (edit up to ~3 sections at a time, push to a branch, share GitHub links, merge once reviewed).
Reasonable batch boundaries:
1. The stale `docs/data-sources.md` references (5 hits) -- mechanical, quick, do first.
2. The Architecture table (lines 41-65 as originally scoped) -- one pass, module-row by
   module-row, trimming each to scannable.
3. "Known open items" -- likely several batches given its size; work top to bottom, defaulting to
   delete for routine "Resolved" entries (see "What a real fix looks like" above) and only pausing
   to verify a fact is preserved elsewhere for the rarer entry that reads as a genuine, reusable
   lesson.
4. A final read-through once trimmed, to confirm the file still reads as a coherent "architecture &
   status" map and not just a pruned list.

Delete this plan doc once done, or fold whatever's left into a narrower follow-up -- same
convention `docs-style-rollout.md`/`docs-style-second-pass.md` both used.
