# `docs/plan.md` cleanup: delete resolved-item narratives, trim the Architecture table

**Status: not started. Scoping done (this doc, split off from `docs-style-second-pass.md` once
that plan's other work finished); deferred at the user's own explicit call (2026-08-31) after a
mid-session scoping discussion, since it's a bigger job than a normal docs-style pass.**

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

For each "**Resolved**" entry in "Known open items": confirm the fact already lives in
`docs/history.md` (it should -- these entries already cite it) and, where it affects current
behavior, in the relevant module's own code comment (many already got this treatment during
`docs-style-second-pass.md`'s parts 1/3, and during that plan's `reproject-fov-investigation.md`/
`wac-jigsaw-investigation.md` passes -- check there first before assuming a fact needs a new home).
Then **delete** the narrative from `plan.md` entirely -- don't trim it, don't summarize it in
place. Keep only:
- Items that are genuinely still open (not yet resolved).
- A scannable current-state summary, if the resolved fact affects how `plan.md`'s own Architecture
  table or Status section should read today.

For the Architecture table: trim each row to what a reader needs to decide whether to go read the
module itself -- one or two sentences of responsibility, not the full design rationale. Move
rationale into the module's own docstring/comments if it isn't already there (spot-check first;
much of it may already be, since Parts 1 and 3 of `docs-style-second-pass.md` covered most of
`src/trntest/`'s docstrings).

Could plausibly shrink the file from 623 lines to somewhere around 150-200.

## Suggested approach

Given the size, this is a multi-batch job, same review workflow as `docs-style-second-pass.md`
used (edit up to ~3 sections at a time, push to a branch, share GitHub links, merge once reviewed).
Reasonable batch boundaries:
1. The stale `docs/data-sources.md` references (5 hits) -- mechanical, quick, do first.
2. The Architecture table (lines 41-65 as originally scoped) -- one pass, module-row by
   module-row, trimming each to scannable.
3. "Known open items" -- likely several batches given its size; work top to bottom, verifying each
   "Resolved" entry's fact is preserved elsewhere before deleting it.
4. A final read-through once trimmed, to confirm the file still reads as a coherent "architecture &
   status" map and not just a pruned list.

Delete this plan doc once done, or fold whatever's left into a narrower follow-up -- same
convention `docs-style-rollout.md`/`docs-style-second-pass.md` both used.
