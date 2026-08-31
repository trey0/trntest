# `docs/plan.md` cleanup: delete resolved-item narratives, trim the Architecture table

**Status: in progress.** Working in worktree `docs-proposed-tasks-style-0defc6`, branch
`claude/docs-proposed-tasks-style-0defc6`. Batch 1 done and **merged to `main`** (`f93e282`):
Architecture table rewritten to the shorter bar below (also added a missing `craters.py` row, then
alphabetized the whole table), two stray `docs/history.md` citations cut, and two fully-resolved
"Known open items" narratives deleted (the CK-kernel investigation, the crater-overlay/`OverlayLayer`
implementation retelling).

**Remaining**: "Known open items" still has most of its resolved-narrative bulk left, worked from
where batch 1 stopped (right after the crater-overlay deletion, before the `crater_depth.py` entry)
down to the end of the file. The single largest remaining block is the Phase 70-79
photometric-angle/Hapke-shading saga (one very long paragraph, currently a few hundred lines into
"Known open items") -- likely worth its own batch given its size. Many more `docs/history.md`
citations remain throughout this section and should be cut using the same approach as batch 1
(state the load-bearing fact directly if any, otherwise just delete). After that, batch 4 (final
read-through) and the still-open question of whether to add Architecture-table rows for the other 9
modules missing from it (`wac_camera_model.py`, `control_network.py`, `crater_depth.py`,
`crater_depth_batch.py`, `pose_alignment.py`, `subprocess_utils.py`, `report.py`, `__init__.py`,
`_lint.py`) -- deferred, not yet asked about.

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
