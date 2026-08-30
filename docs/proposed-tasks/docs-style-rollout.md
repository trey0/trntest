# Docs rework: applying `docs/docs-style.md` across `docs/` and `src/`

**Status: `docs/*.md` mostly done; `src/trntest/*.py` docstrings/comments underway (12 of 18 files).**
The original problem was docstrings, not just standalone docs — `docs/docs-style.md` covers both, but
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
  notebook re-run was needed. User feedback on this file's round 2: "fantastic" — a clear win despite
  the line-count growth.
- `src/trntest/isis_wac.py` — full pass (1284 -> 1404 lines), applying the refined convention
  directly in one pass (no round 2 needed, now that the convention itself is settled): all 12
  `docs/history.md` citations removed, every docstring cut to minimal + RST fields, rationale/caveats
  moved to trailing comment blocks. Verified with `trntest-lint` (`ruff format`/`ruff check`/`mypy`
  all clean on this file). No code logic touched, so no notebook re-run was needed.
- **Round 3 (both files), per user feedback: module docstrings get the same treatment.** A module
  docstring states minimally what the file is for; material that doesn't fit moves to a trailing
  comment below it (mirroring the function pattern) *or*, preferably, to the specific function/class
  it's actually about, if there is one — more discoverable there than as general module framing.
  `docs/docs-style.md` updated with this rule first. Applied to both files: `isis_wac.py`'s module
  docstring trimmed (the CSM/`usgscsm` rationale — genuinely shared context multiple functions already
  point back to — moved to a trailing comment; the VIS/UV-cubes fact relocated to `Lrowac2IsisResult`,
  where the unused `uv_even`/`uv_odd` fields actually live); `lunaserv.py`'s module docstring trimmed
  (a `sat_sim`-illumination rationale that duplicated `shade_ortho`'s own trailing comment, cut per
  "one source of truth"). Verified with `trntest-lint` on both files.
- `src/trntest/dataset.py` — full pass (427 -> 479 lines), including the module docstring (cut a
  "why `select_dataset()` was removed" history paragraph — not load-bearing for using the file today
  — and moved a duplicated `crop_footprint` rationale to `GenerationResult`'s own docstring/comment,
  where the field it explains lives). All 6 `docs/history.md` citations removed. Verified with
  `trntest-lint` (`ruff format`/`ruff check`/`mypy` all clean on this file). No code logic touched,
  so no notebook re-run was needed.
- `src/trntest/plotting.py` — full pass (1257 -> 1427 lines, the densest file yet: `plot_overlay`/
  `plot_overlay_toggle`/`plot_render_toggle` each had 30-60-line docstrings). Module docstring was
  already minimal (2 sentences), no change needed there. All 5 `docs/history.md` citations removed,
  including `plot_overlay_toggle`'s GitHub-sanitizer investigation narrative (kept as a condensed
  trailing comment — genuinely load-bearing rationale for why the `<img>`-GIF mechanism was chosen
  over two rejected alternatives — with the dated Phase 33-35 citations cut). Verified with
  `trntest-lint` (`ruff format`/`ruff check`/`mypy` all clean on this file). No code logic touched,
  so no notebook re-run was needed.
- **Scope addition, per user feedback: functions/classes with no docstring at all now stand out
  against the reworked ones, so add them (not a blanket mandate — a trivial one-liner or a
  nested/local closure can still skip one).** Applied across the 4 files done so far:
  `lunaserv.py` (`pad_bbox`, `union_bbox`), `isis_wac.py` (`_resolved_wac_ck_cache_path`,
  `run_lrowaccal`), `dataset.py` (`write_manifest`, `read_manifest`), `plotting.py`
  (`plot_synthetic_render`, `plot_comparison`, `OverlayLayer.plot`) — all got a docstring in the
  same minimal + RST-fields style. Left two nested closures in `plotting.py`
  (`plot_sfs_comparison`'s `brightness_matched`, `plot_render_toggle`'s `_frame`) undocumented,
  matching the "trivial/local, can skip" carve-out. Apply this same pass to every file as it's
  reworked going forward, not just retroactively to these 4. Verified with `trntest-lint` (all
  four files clean); the 3 non-`plotting.py` diffs are pure additions, no logic touched.
- `src/trntest/sfs_validation.py` — full pass (324 -> 371 lines), including the missing-docstring
  check from the item above (all functions/classes already had one). Cross-checked against
  `docs/plan.md`'s current status first (a peer session flagged a possibly-stale "still-open
  regression" claim in the *notebook* counterpart, `notebooks/sfs_validation.py` — a different
  file); this module's own docstrings didn't have that problem, nothing to correct. All 4
  `docs/history.md` citations removed. Verified with `trntest-lint` (`ruff format`/`ruff
  check`/`mypy` all clean on this file). No code logic touched, so no notebook re-run was needed.
- `src/trntest/config.py` (263 -> 244 lines, all 4 `docs/history.md` citations removed): mostly
  module-level constant comments rather than function docstrings, so the main work was cutting
  duplication against the already-reworked `docs/data-sources/*.md` files (each constant's comment
  now states the essential fact plus a one-hop cross-reference, not the full investigation trail)
  and fixing "real"/"genuine" filler. Added docstrings to the 4 previously-undocumented private
  helpers (`_resolve_config_file_path`, `_validate_keys`, `_coerce_path_fields`,
  `_apply_env_overrides`). Moved the module docstring's `edr_*`/`cdr_*` field-naming rationale to a
  comment above those fields in `TrntestConfig` itself, per the module-docstring rule. **Also
  corrected two facts that had gone stale independent of style**: `DEFAULT_WAC_CK_SOURCE`'s comment
  still claimed `"isis_resolved"` fixes a confirmed ~11-13km pointing discrepancy, but
  `docs/data-sources/spice-kernels-isis.md` (and `spice_kernels.py`) already record that this was
  never reproduced — the default is kept for matching ISIS's own kernel resolution by construction,
  not for fixing a known bug; `DEFAULT_LUNASERV_ORTHO_LAYER`'s comment described the Lunaserv layer
  as if still live, but WAC_EMP's PDS4 archive is now the default ortho source and Lunaserv WMS is
  fallback-only. Verified with `trntest-lint` (`ruff format`/`ruff check`/`mypy` all clean on this
  file). No code logic touched, so no notebook re-run was needed.
- `src/trntest/camera.py` (608 -> 629 lines, all 4 `docs/history.md` citations removed): `docs/
  history.md` citations replaced with the fact stated directly or a cross-reference to
  `docs/data-sources/lroc-wac-edr-cdr.md`/`docs/reproject-fov-investigation.md`.
  `solve_corrected_fov`'s and `build_camera`'s large docstrings (the two most `docs/history.md`
  citations landed in) trimmed to interface + RST fields, with their rationale/investigation trails
  moved to plain comment blocks as the first lines of the function body, per the convention already
  used in `lunaserv.py`/`isis_wac.py`. Added docstrings to 3 previously-undocumented functions
  (`boresight_ground_point_km`, `pixel_ray_cam`, `write_tsai`). Cut "real"/"genuine"/"actual" bare
  intensifiers, but kept the many *legitimately* contrastive uses of "real" — this file's whole
  point is real SPICE/ISIS measurements vs. the synthetic render, so most instances were a real
  contrast, not filler. Verified with `trntest-lint` (`ruff format`/`ruff check`/`mypy` all clean on
  this file). No code logic touched, so no notebook re-run was needed.
- `src/trntest/cache.py` (287 -> 294 lines, all 4 `docs/history.md` citations removed): pacing/429
  rationale (`_REQUEST_PACING_SECONDS`, `FetchError`) now cross-references `docs/caching.md`'s
  "Retry/backoff/pacing policy" section (already carries the full incident narrative) instead of
  restating it with a Phase citation. Updated 4 stale `docs/data-sources.md` references to the
  specific post-split files. Added docstrings to 6 previously-undocumented thin wrapper functions
  for consistency with their already-documented siblings, plus moved `FetchError`'s rationale to a
  trailing class-body comment. **Also fixed a stale reference**: `cached_get`'s docstring named
  `select_dataset()` as the function whose sweep exposed a temp-file race, but that function was
  removed (`dataset_selection.py` already notes this); reworded to describe the sweep generically.
  Verified with `trntest-lint` (`ruff format`/`ruff check`/`mypy` all clean on this file). No code
  logic touched, so no notebook re-run was needed.
- `src/trntest/tasks.py` (194 lines, unchanged -- pure reorganization, all 3 `docs/history.md`
  citations removed): moved the module docstring's per-instance (`huey` vs. `huey_parallel`)
  rationale to trailing comments right above each `SqliteHuey()` assignment, since that's what the
  rationale is actually about, per the module-docstring relocation rule. `_generate_entry`'s
  docstring trimmed to interface + RST fields, rationale moved to a body comment block. Also
  consolidated duplicated "why the `TrnTestEntry` object is passed instead of picklable primitives"
  rationale that had drifted into both `_generate_entry`'s and `generate_product_parallel`'s
  docstrings, kept only in the latter (the function that actually crosses the process boundary this
  is about). Verified with `trntest-lint` (`ruff format`/`ruff check`/`mypy` all clean on this
  file). No code logic touched, so no notebook re-run was needed.
- `src/trntest/render.py` (204 -> 207 lines, all 3 `docs/history.md` citations removed):
  `patch_sun_position`'s docstring (the one with 2 of the 3 citations) trimmed to interface +
  params, rationale moved to a body comment block. Also fixed a stale `docs/data-sources.md`
  citation in `run_mapproject`'s docstring -- that content no longer lives there after the
  `data-sources.md` split; restated the fact directly instead. Cut "real"/"actual" filler
  throughout. Verified with `trntest-lint` (`ruff format`/`ruff check`/`mypy` all clean on this
  file). No code logic touched, so no notebook re-run was needed.
- `src/trntest/pose_alignment.py` (515 -> 514 lines, all 3 `docs/history.md` citations removed):
  `native_wac_gsd_m`'s/`downsample_to_gsd`'s/`fit_similarity_correction`'s large docstrings trimmed
  to interface + RST fields, rationale moved to body comment blocks. Fixed a stale
  `docs/data-sources.md` reference in `_lightglue_models`' docstring -- `docs/external-tools.md`'s
  "LightGlue tie-point matching" section already carries the DISK-vs-SuperPoint licensing rationale
  in full, so this now cross-references it instead of restating a compressed duplicate. Cut
  "real"/"genuine" filler, while keeping this project's established "real-WAC" compound term (real
  WAC instrument pipeline vs. the synthetic render pipeline) and other genuinely contrastive uses
  intact. Verified with `trntest-lint` (`ruff format`/`ruff check`/`mypy` all clean on this file).
  No code logic touched, so no notebook re-run was needed.

- `src/trntest/trn_dataset.py` (693 -> 712 lines, both `docs/history.md` citations removed):
  pulled in `origin/main` first (a peer session's label-override changes to
  `plot_vs_basemap`/`plot_overlay`) before starting. Applied the what/why split consistently from
  the start (per the `cache.py` review below) rather than needing a second pass. **Also caught 4
  historical-narrative sentences embedded directly in docstrings with no `docs/history.md` citation
  attached** — the same "used to do X, now does Z" pattern flagged in `render.py`'s docstring (see
  that review below): the module docstring's "Replaces `dataset.generate_dataset()`'s flat...
  layout" and "`populate()` no longer supports... old filesystem lock files... are gone" framing
  (restated as direct current-state facts), `TrnTestHillshadeImage`'s "so this is a pure
  relocation, not new pipeline logic" aside (cut), and `TrnTestReprojectImage`'s "The user's own
  framing: ..." quoted aside (replaced with a plain statement). Added a docstring to
  `plot_vs_basemap`, which had none. Fixed several stale "see X's own docstring" cross-references
  that now point to content relocated to a comment in an earlier-reworked file. Verified with
  `trntest-lint` and `tests/test_trn_dataset.py` (32 passed, non-heavy). No code logic touched.

**User review findings on the first pass (2026-08-30), applied to `cache.py` and then
retroactively to `camera.py`/`tasks.py`/`render.py`/`pose_alignment.py`, and carried forward into
`trn_dataset.py` from the start**:
1. **Removing `docs/history.md` citations and "real"/"genuine" filler is not the whole job.**
   Several large docstrings still mixed "why" (design rationale, investigation-derived numbers,
   comparisons to sibling functions) into the docstring body instead of splitting it into a
   minimal docstring + a plain comment block as the first lines of the function body, per
   `docs/docs-style.md`'s own rule. `lunaserv.py` is the reference example for this pattern —
   consult it, not just the rule text, when in doubt.
2. **Historical narrative can hide in a docstring with no `docs/history.md` citation attached** —
   e.g. "Replaces the old `run_sat_sim.sh`" (`render.py`) or "so this is a pure relocation, not new
   pipeline logic" (`trn_dataset.py`). Read every docstring for "used to do X, but now Y"/"no
   longer"/"before this existed" framing even when `grep -c docs/history.md` comes back clean —
   that grep only catches explicit citations, not the underlying narrative style.
3. A cross-reference ("see X's own docstring for...") can go stale the moment the target's content
   moves to a comment during its own docs-style pass — re-verify each pointer against the file it
   actually points to when doing this style of rewrite, don't just carry old wording forward.

## Not yet done

**`src/trntest/*.py` docstrings/comments — the original complaint, still mostly untouched.** 6
files still cite `docs/history.md` (`lunaserv.py`/`isis_wac.py`/`dataset.py`/`plotting.py`/
`sfs_validation.py`/`config.py`/`camera.py`/`cache.py`/`tasks.py`/`render.py`/`pose_alignment.py`/
`trn_dataset.py` done, see Completed above). Rough priority order (citation count, then size, as a
proxy for how much chatty/historical material likely needs trimming):

| File | `docs/history.md` cites | Lines |
|---|---|---|
| `tie_points.py` | 2 | 491 |
| `spice_kernels.py` | 2 | 345 |
| `product_registry.py` | 2 | 194 |
| `dataset_selection.py` | 2 | 283 |
| `maneuver_detection.py` | 1 | 269 |
| `_lint.py` | 1 | 230 |

For each: remove `docs/history.md` citations (state the load-bearing fact directly, or cut it), trim
docstrings (function/class *and* the module docstring itself) to genuinely minimal (what it does,
plus RST `:param:`/`:returns:`/`:raises:` fields where they clarify — see `docs/docs-style.md`'s
current wording), move rationale/caveats/open items to a plain comment block as the first lines of
the function body (not above the `def`, so it stays out of `help()`) — or, for module-docstring
material about one specific function/class, relocate it there instead of a module-level comment, per
`docs/docs-style.md`'s current wording — fix "real"-as-filler and em-dash sentence-stacking, **and add
a docstring to any function/class in the file that's missing one entirely** (not a blanket mandate — a
trivial one-liner or a nested/local closure can still skip one; use judgment for what "feels wrong" to
leave undocumented, same bar applied retroactively to the first 4 files, see Completed above), and
**read every docstring for historical-narrative framing even where `docs/history.md` isn't
cited** (see the "User review findings" item above — `grep -c docs/history.md` alone won't catch it).
`tie_points.py` is next (tied at 2 citations with `spice_kernels.py`; picked first among the tie,
largest of the two by line count).

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
2. Work one file at a time; `config.py` next. Re-verify with `trntest-lint` and, if a docstring
   change touches a function a notebook exercises, `scripts/run_notebook.sh` on the relevant
   notebook before committing. A pure docstring/comment edit with no code-logic change (confirm via
   `git diff`) doesn't need a notebook re-run — `lunaserv.py`'s pass didn't need one.
3. Delete this plan once `src/trntest/*.py` is clean of `docs/history.md` citations and the
   remaining docs above have had their pass, or fold whatever's left into a narrower follow-up.
