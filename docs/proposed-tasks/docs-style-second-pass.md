# Docs-style second pass: files and rules the history.md-citation sweep missed

**Status: in progress.** Parts 1-3 done. Part 2: all 4 files' stale `docs/data-sources.md` refs
repointed. Part 1: all 13 never-audited files reworked (`wac_camera_model.py`,
`crater_depth_batch.py`, `control_network.py`, `craters.py`, `crater_depth.py`, `illumination.py`,
`wac.py`, `catalog.py`, `orientation.py`, `session.py`, `__init__.py` -- confirmed already
compliant, no edit needed --, `subprocess_utils.py`, `report.py`); also fixed a downstream stale
pointer `wac_camera_model.py`'s pass surfaced in `isis_wac.py`/`notebooks/pose_alignment_spike.py`.
Part 3: all 17 previously-"done" files re-verified against the stricter docstring bar, 5 needed
fixes (`lunaserv.py`, `pose_alignment.py`, `camera.py`, `maneuver_detection.py`, `tie_points.py`).

**Only part 4 remains** (`docs/*.md` files) -- see its own section below for the file list and
what's already known about each.

Workflow note: the user reviews in batches of up to 3 files via GitHub links before merging to
main -- push each file's commit to the branch as it's done, but hold the `git push origin
<branch>:main` fast-forward until they've reviewed that batch and said to proceed.

**Note for whoever hits the next `git push origin <branch>:main`**: the auto-mode classifier started
blocking direct pushes to `main` partway through this plan (after `control_network.py`, before
`craters.py`) even when the user explicitly asked for "commit, push, merge." `gh` CLI isn't
installed in this worktree either, so there's no PR-based fallback available. Batches after that
point are pushed to the branch only, not merged -- ask the user to merge, or to adjust permissions,
rather than retrying the same push.

## Why this plan exists

`docs/proposed-tasks/docs-style-rollout.md` (now folded into current-state docs, see its own
closing note) selected which `src/trntest/*.py` files to rework by one proxy: whether they cited
`docs/history.md`. That caught the worst offenders, but the proxy has two blind spots this plan
targets:

1. **A file with zero `docs/history.md` citations was never looked at, even if it violates every
   other rule in `docs/docs-style.md`.** 13 of `src/trntest/`'s 31 files fall in this category --
   nobody has read them against the style guide at all.
2. **"No `docs/history.md` citations left" was treated as "done," but the docstring rule (what vs.
   why) turned out to need stricter enforcement than early passes applied.** `trn_dataset.py`
   needed a round 2 specifically because round 1 cut long history paragraphs but left short
   rationale clauses ("-- since X", "-- confirmed Y") embedded in docstrings. Nothing confirms the
   other 17 "done" files don't have the same residue -- they were reworked before that stricter bar
   was established.

Also in scope: `docs/*.md` files, including ones already marked "done" in the prior plan -- the
`docs/data-sources.md` monolith-to-index split happened *during* that rollout, and at least 4
already-reworked `src/trntest/*.py` files (and more `docs/*.md` files) still point at it with
pre-split cross-references that no longer resolve to real content.

## Goal

Apply every rule in `docs/docs-style.md` -- not just "no `docs/history.md` citations" -- to:
- The 13 `src/trntest/*.py` files never audited at all.
- The 4 already-reworked files with confirmed-stale `docs/data-sources.md` references.
- A spot-check of the other 17 "done" `src/trntest/*.py` files against the stricter what/why bar
  `trn_dataset.py` round 2 established.
- The `docs/*.md` files the prior plan's own "Not yet reworked" list left open, plus the
  already-"reworked" `docs/*.md` files that may share the same stale cross-reference problem.

## Scope, part 1: `src/trntest/*.py` files never audited (13 of 31)

None cite `docs/history.md` (confirmed via `grep -c`), which is exactly why the prior plan's
selection criterion skipped them. `real`/`genuine`/`actual` counts below are a rough filler proxy,
not a verdict -- each needs an actual read, since some uses are legitimately contrastive (this
project's own established "real WAC vs. synthetic render" pairing).

| File | Lines | `real`/`genuine`/`actual` count | Stale `docs/data-sources.md` ref |
|---|---|---|---|
| `wac_camera_model.py` | 345 | 43 | yes |
| `crater_depth_batch.py` | 575 | 30 | yes |
| `control_network.py` | 267 | 27 | no |
| `craters.py` | 221 | 18 | yes |
| `crater_depth.py` | 215 | 17 | no |
| `illumination.py` | 196 | 8 | no |
| `wac.py` | 90 | 8 | yes |
| `catalog.py` | 166 | 3 | no |
| `orientation.py` | 110 | 3 | yes |
| `session.py` | 81 | 2 | no |
| `__init__.py` | 68 | 1 | no |
| `subprocess_utils.py` | 15 | 0 | no |
| `report.py` | 39 | 0 | no |

For each: apply the same recipe `docs-style-rollout.md` used (see its "For each" paragraphs, still
readable in git history / `docs/history.md` once this rollout is folded in) -- module docstring
trimmed to what the file is for, function/class docstrings to interface + RST fields with
rationale moved to body comments (the stricter bar: even a short trailing "-- because/since X"
clause counts, not just long paragraphs), missing docstrings added where it "feels wrong" to leave
one out (trivial one-liners and local closures can still skip it), filler words cut, and any
`docs/data-sources.md` reference repointed at the specific post-split file in
`docs/data-sources/*.md` it actually needs (see `docs/data-sources.md`'s own index table for which
one).

`wac_camera_model.py` and `crater_depth_batch.py` are the two biggest and highest-filler-count --
start there, same "size/citation count as urgency proxy" logic the prior plan used.

## Scope, part 2: stale `docs/data-sources.md` references in already-reworked files

The prior plan's `cache.py` and `config.py` entries explicitly repointed their
`docs/data-sources.md` references at the post-split files. These four "done" files weren't caught
the same way -- their docs-style pass happened before or during the split and the cross-reference
fix never got backfilled:

| File | Stale references |
|---|---|
| `lunaserv.py` | 9 (module docstring + 8 comments) |
| `isis_wac.py` | 3 (comments) |
| `spice_kernels.py` | 2 (module docstring + 1 comment) |
| `plotting.py` | 1 (comment) |

For each: `grep -n "docs/data-sources.md"` the file, and for every hit, follow
`docs/data-sources.md`'s own index table to the specific file that now holds the fact being pointed
at (`lunaserv-wms.md`, `astropedia-gld100.md`, `wac-emp-pds4.md`, `robbins-craters.md`,
`spice-kernels-naif.md`, `spice-kernels-isis.md`, or `lroc-wac-edr-cdr.md`), and repoint the
reference there. This is a mechanical fix, not a style rewrite -- doesn't need the full docstring
pass part 1's files get, just a working cross-reference. Quick enough to do all 4 in one sitting.

The same `grep` should be re-run against part 1's 13 files once they're done, since 5 of them
(`wac_camera_model.py`, `crater_depth_batch.py`, `craters.py`, `wac.py`, `orientation.py`) already
show up with the same stale reference -- fix it as part of their own pass rather than as a separate
step.

## Scope, part 3: re-verify the other 17 "done" `src/trntest/*.py` files against the stricter bar

**Done.** Read every docstring (via `ast.get_docstring`, not a `grep` heuristic -- catches a "why"
clause phrased with a comma instead of an em dash too) in `lunaserv.py` and `isis_wac.py` (the two
the plan's own "if resuming" note called out first), then scripted a broader residue-pattern scan
(`since |because |confirmed|deliberately|substantially|worth |the reason|in order to|so that|...`)
across all 16 of the remaining files (`trn_dataset.py`, already round-2'd, was excluded).

Fixed: `lunaserv.py` (`hapke_shade_ortho`'s `along_track_correction` param had an evaluative
"substantially more accurate than X" justification inline), `pose_alignment.py` (module docstring's
entire backstory/validation narrative was in the docstring proper, plus 4 functions had "why"
clauses mixed into otherwise-interface docstrings), `camera.py` (`ground_track_step_km`),
`maneuver_detection.py` (`candidate_utc`), and `tie_points.py` (`resolve_crop_pixels`'s "validated
to exact (0.000px) agreement" empirical claim) -- one or two clauses each.

Checked and confirmed clean or only benign hits (factual cross-references, not embedded rationale):
`isis_wac.py`, `dataset.py`, `_lint.py`, `plotting.py`, `spice_kernels.py`, `cache.py`, `config.py`,
`dataset_selection.py`, `product_registry.py`, `render.py`, `sfs_validation.py`, `tasks.py`.

## Scope, part 4: `docs/*.md`

**Not yet reworked at all** (carried over from the prior plan, still accurate):
- `docs/plan.md` (623 lines) -- flagged as its own future index-pattern candidate (thin index +
  split-out detail files, the same pattern `docs/data-sources.md` and `docs/generators.md` already
  went through), never started. Also has 161 `real`/`genuine`/`actual` hits and its own stale
  `docs/data-sources.md` reference -- whichever pass touches this file should fix that reference
  too, not just the index-split.
- `docs/environment.md` (242 lines) -- confirmed-stale "ephemeral VPS, archive/restore" framing
  (the VPS now persists across sessions); a full rewrite is owed, not just a style pass. Also has a
  stale `docs/data-sources.md` reference.
- `README.md` (172 lines, cites `docs/history.md` once) -- never reviewed against
  `docs/docs-style.md`.
- `docs/reproject-fov-investigation.md` (371 lines) and `docs/wac-jigsaw-investigation.md` (246
  lines, cites `docs/history.md` once) -- only got mechanical link fixes during the split, never a
  content pass. Both have high `real`/`genuine`/`actual` counts (68 each) -- expected to some
  degree for investigation docs describing what turned out to be real vs. spurious findings, but
  worth a read, not an assumption.
- `docs/proposed-tasks/report-plan.md` (148 lines, cites `docs/history.md` once) and
  `docs/proposed-tasks/corrected-overlay-cam2map-plan.md` (153 lines) -- never reviewed.

**Marked "done" by the prior plan, but with a stale `docs/data-sources.md` reference found by this
audit**: `docs/caching.md:22` (a `robbins_craters` fact that now lives in
`docs/data-sources/robbins-craters.md`) and `docs/plan.md` (5 hits -- see its own bullet above).
Checked and confirmed **not** stale, despite mentioning `docs/data-sources.md`: every
`docs/data-sources/*.md` file's own "Index: `docs/data-sources.md`" backlink (correct -- that *is*
the index now), `docs/external-tools.md`'s one mention (explaining how it differs from
`data-sources.md`, not citing a fact from it), `docs/docs-style.md`'s one mention (a hypothetical
example inside the rule text itself, not a real cross-reference), and `docs/environment.md`'s two
mentions (naming the file as a shared-state risk, not citing its content). **Lesson for whoever
does this fix: `grep -l` finds the files, but check each hit's actual sentence before repointing
it -- most "mentions" of `docs/data-sources.md` after the split are fine as-is.**

**Not yet checked by this audit at all**: `AGENTS.md` and the doc index files
(`docs/generators.md`, `docs/data-sources.md` itself) -- worth a quick self-consistency check
(their own "keep index files thin" rule) once the rest of this plan reduces churn in the files they
point to, so the check doesn't have to be redone.

## If resuming

1. ~~Start with part 2~~ Done.
2. ~~Then part 1~~ Done -- all 13 files reworked, one file (up to 3, batched for review) per
   `docs/docs-style.md` recipe, each stale `docs/data-sources.md` reference repointed as part of its
   own file's pass, not separately.
3. ~~Part 3~~ Done -- all 17 files re-read via `ast.get_docstring` + a residue-pattern scan, 5 fixed.
4. Only part 4 (`docs/*.md`) remains -- see its own section for the file list and what's known.
5. Re-verify every `src/trntest/*.py` change with `trntest-lint`; re-run
   `scripts/run_notebook.sh` only if a docstring change happens to touch code logic too (none of
   this plan's work should, if scoped correctly -- confirm via `git diff` before skipping).
6. Delete this plan once part 4 is done, or fold whatever's left into a narrower follow-up, same
   convention `docs-style-rollout.md` used.
