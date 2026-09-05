# Docs rework: applying `docs/docs-style.md` across `docs/` and `src/`

**Status: `docs/*.md` mostly done; `src/trntest/*.py` docstrings/comments done (18 of 18 files) -- every `docs/history.md` citation is out of `src/trntest/*.py`.**
The original problem was docstrings, not just standalone docs — `docs/docs-style.md` covers both, and
both halves are now largely done. One doc (`docs/environment.md`) remains unreviewed and open to
pick up; one more (`docs/proposed-tasks/report-plan.md`) is blocked on another agent's in-flight
work — see "Not yet done" below.

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
- `docs/proposed-tasks/` convention established (this doc lives under it); `report-plan.md` moved
  there (`corrected-overlay-cam2map-plan.md`, moved there at the same time, has since been resolved
  and deleted independently of this rollout).
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
  `docs/architecture.md`'s current status first (a peer session flagged a possibly-stale "still-open
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

- **`src/trntest/trn_dataset.py`, round 2 — the first pass (693 -> 712 lines) removed both
  `docs/history.md` citations and caught 4 historical-narrative sentences, but user review said it
  still wasn't far enough into "docstring = minimal statement of intent + non-obvious params" —
  too many docstrings still read as commentary on *how*/*why* the implementation works, even after
  the citations were gone, rather than *what* the thing does.** Concretely: a short rationale
  clause tacked onto the end of an otherwise-minimal sentence ("-- since X", "-- confirmed Y",
  "-- matching Z's approach") is exactly as out-of-scope for a docstring as a whole paragraph of
  it, per `docs/docs-style.md`'s existing rule — the round-1 pass had been treating that rule as
  "cut long history/investigation paragraphs" rather than "cut *any* implementation rationale,
  however short." Round 2 (712 -> 706 lines) re-swept every docstring in the file with that
  stricter bar: `TrnTestEntry.stitched`'s/`TrnTestDataSet`'s/`TrnTestImage`'s/
  `TrnTestCropImage`'s/`TrnTestHillshadeImage`'s/`TrnTestReprojectImage`'s class-or-property
  docstrings each had a trailing "-- because/since/matching ..." clause evicted to a class-body or
  property-body comment; the module docstring's `populate_via_workers`/`reproject` asides moved to
  a trailing module comment; `populate()`'s/`populate_via_workers()`'s/`_enqueue_pending()`'s
  `:param:`/`:returns:` fields were trimmed to state the semantics, not also justify the design
  choice inline. Left the load-bearing exceptions where a "why" clause **is** the correctness-
  relevant fact a caller needs (`dem_ortho_result`'s resume-from-files behavior, `truncate()`'s not
  clearing `_work/`, `create()`'s not touching already-generated files, `plot_overlay()`'s no-
  trailing-`;` requirement) — those already lived in the docstring proper in round 1 and stayed
  there, since they're usage contract, not implementation commentary. Verified with `trntest-lint`
  (`ruff format`/`ruff check`/`mypy` all clean) and `tests/test_trn_dataset.py` (32 passed,
  non-heavy). No code logic touched, so no notebook re-run was needed.
- `src/trntest/tie_points.py` (491 -> 450 lines, both `docs/history.md` citations removed): the
  densest file yet — a ~59-line module docstring (full 5-step selection/projection procedure plus a
  deprecated-camera-model investigation narrative) and several 30+-line function docstrings in the
  same investigation-trail style. Module docstring trimmed to what/why in ~7 lines; the
  local-meters-not-lonlat-degrees rationale and the WAC-VIS axis convention (both genuinely shared
  across `inscribed_bbox`/`select_tie_points`/`camera.py`) kept as a trailing module comment, per the
  relocation rule — everything else (the deprecated-path switch history, exact measured
  discrepancies) cut as either duplicated elsewhere (each deprecated function's own docstring already
  says "superseded by X") or as pure investigation narrative not load-bearing today.
  `crop_footprint_corners_for_camera`'s and `resolve_crop_pixels`'s docstrings (the two heaviest)
  trimmed to interface + RST fields, rationale moved to body comment blocks. Added docstrings to the
  2 previously-undocumented functions (`lonlat_to_ground_km`, `intersect_bbox`). Kept this project's
  established "real WAC crop" vs. synthetic-render contrast intact rather than treating it as filler.
  Verified with `trntest-lint` (`ruff format`/`ruff check`/`mypy` all clean on this file) and
  `tests/test_tie_points_geometry.py` (11 passed). No code logic touched, so no notebook re-run was
  needed.
- `src/trntest/spice_kernels.py` (345 -> 356 lines, both `docs/history.md` citations removed):
  `latest_metakernel_url`'s/`select_isis_wac_ck_kernels`'s/`fetch_and_furnish`'s/
  `furnish_spk_range`'s docstrings trimmed to interface + RST fields, rationale moved to body
  comment blocks. `fetch_and_furnish`'s docstring dropped a duplicate restatement of the
  SPICE(KERNELPOOLFULL)/SPICE(NOMOREROOM) rationale — that's already the `_loaded_date_ranged_kernels`/
  `_loaded_kernels` module-level comments' own job (per "one source of truth"); the
  `_loaded_date_ranged_kernels` comment, which previously just said "see fetch_and_furnish's
  docstring for why this matters," now states the SPICE(KERNELPOOLFULL) rationale directly instead of
  forward-referencing. Added docstrings to 3 previously-undocumented functions/classes (`KernelRef`,
  `select_kernels_for`, `_fetch_kernel_ref`). **Also fixed a stale fact carried over from before the
  `config.py` pass's correction**: `select_naif_wac_ck_kernels`'s docstring still claimed a
  "confirmed ~11-13km disagreement with ISIS's own camera model" as the reason it's deprecated, but
  `select_isis_wac_ck_kernels`'s own docstring already recorded that this discrepancy was never
  reproduced (direct verification found both `wac_ck_source` options give numerically identical WAC
  pointing) — reworded to state the actual reason it's kept only for comparison (doesn't fetch the
  second CK ISIS furnishes) without repeating the stale ~11-13km claim. Verified with `trntest-lint`
  (`ruff format`/`ruff check`/`mypy` all clean on this file) and `tests/test_spice_kernels_parsing.py`
  (12 passed). No code logic touched, so no notebook re-run was needed.
- `src/trntest/dataset_selection.py` (283 -> 298 lines, both `docs/history.md` citations removed):
  `add_maneuver_flags`'s and `resolve_orbit_sequence`'s docstrings (the two `docs/history.md`
  citations) trimmed to interface + RST fields, rationale moved to body comment blocks.
  `resolve_orbit_sequence`'s docstring was the densest in the file — one paragraph covering 5
  separate design decisions (one-row-at-a-time iteration, the pre-filter, `attach_cdr=False`,
  `throttle_minutes=None`'s history vs. the removed `select_dataset()`) — each decision's rationale
  now lives in its own comment paragraph instead of one run-on block. Cut 3 "real"-as-filler
  instances (`add_acceptable_edr_counts`'s "real WAC EDRs", `resolve_orbit_sequence`'s "a real
  one-window resolve attempt"/"tripped a real rate limit" — none were contrastive against a
  synthetic counterpart, unlike this project's usual real-vs-synthetic-render pairing). All 6
  functions already had docstrings; the 3 nested closures in `select_diverse_datasets`
  (`circular_distance_arr`/`exclude`/`require_available`) left undocumented, matching the
  trivial/local carve-out already applied in `plotting.py`. No dedicated test file exists for this
  module. Verified with `trntest-lint` (`ruff format`/`ruff check`/`mypy` all clean on this file).
  No code logic touched, so no notebook re-run was needed.
- `src/trntest/product_registry.py` (194 -> 197 lines, both `docs/history.md` citations removed):
  the module docstring's Phase 79/80 rollout citation and `add_maneuver_flags`-style "real
  writers/readers/deleters"/"real writer conventions" filler cut. `atomic_publish`'s/
  `atomic_publish_path`'s/`atomic_publish_prefix`'s docstrings (each mixing the ISIS-`to=`-output
  investigation trail into the interface description) trimmed to interface + RST fields, rationale
  moved to body comment blocks -- including the "confirmed live" `.tmp`-suffix-silently-fails-ISIS
  finding, kept as load-bearing rationale for why the temp name preserves `dest`'s real extension,
  not dropped outright. All functions/classes already had docstrings. Verified with `trntest-lint`
  (`ruff format`/`ruff check`/`mypy` all clean on this file) and `tests/test_product_registry.py`
  (23 passed). No code logic touched, so no notebook re-run was needed.
- `src/trntest/maneuver_detection.py` (269 -> 262 lines, the one `docs/history.md` citation
  removed): the module docstring's ~65-line math derivation ("why h/eps, not the classical
  elements") is genuinely shared context multiple functions depend on, not history -- kept, moved
  to a trailing module comment per the relocation rule, docs/history.md citation cut and the
  Mesarch-et-al. validation claim restated directly. Two paragraphs that were module-docstring-level
  but actually describe one specific function's algorithm got relocated there instead of the
  trailing comment, per the "or, for module-docstring material about one specific function/class,
  relocate it there" branch of the rule: the "detection method" paragraph into
  `detect_discontinuities`'s own docstring, the "peak reconstruction" paragraph into
  `_reconstruct_candidate`'s. Also deduped the "+373 m/s radial" incident, which had been stated
  twice (once in the module docstring, once in `_reconstruct_candidate`'s own body comment) --
  kept only in the latter, the module comment now just points to it. Cut "real"-as-filler
  throughout (this module has no synthetic-vs-real distinction to be genuinely contrastive
  against, unlike `camera.py`/`pose_alignment.py`'s established real-WAC-vs-synthetic-render
  pairing). All functions/classes already had docstrings. Verified with `trntest-lint` (`ruff
  format`/`ruff check`/`mypy` all clean on this file) and the fast (non-`@pytest.mark.heavy`)
  subset of `tests/test_maneuver_detection.py` (5 passed, 2 heavy deselected -- need live
  SPICE/network). No code logic touched, so no notebook re-run was needed.
- `src/trntest/_lint.py` (230 -> 232 lines, the one `docs/history.md` citation removed): already
  the cleanest file in the pass -- module docstring was already minimal, and every function already
  had a docstring except two truly trivial private one-liners (`_git_lines`, `_paired_ipynb`, etc.,
  left undocumented per the trivial-one-liner carve-out) and two that weren't quite trivial enough to
  skip (`_diff_files` combines two git queries for a non-obvious reason -- `git diff` alone misses
  untracked files; `_unchanged_from_head`'s returncode-based boolean isn't self-evident from its
  name), which got short docstrings added. Cut 3 "genuinely"-as-filler instances (none were
  contrastive against a specific alternative) and kept one legitimate "real" contrast (`--output -`
  (stdout) vs. "a real file written into the same directory instead"). This closes out
  `src/trntest/*.py`'s `docs/history.md` citations entirely -- `grep -rc "docs/history.md"
  src/trntest/*.py` now returns nothing. Verified with `trntest-lint` (`ruff format`/`ruff
  check`/`mypy` all clean on this file, and the run itself -- `_lint.py` is the tool doing the
  checking -- exercised the changed code end-to-end with no dedicated test file needed). No code
  logic touched.

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

**`src/trntest/*.py` docstrings/comments — done, all 18 files** (`grep -rc "docs/history.md"
src/trntest/*.py` returns nothing). If a future review finds the same "docstring reads as
implementation commentary" issue `trn_dataset.py` round 2 fixed (see its Completed entry above) in
another file, the fix is the same: evict any "-- because/since/matching X" rationale clause from
the docstring to a body/class comment, even a short one — not just long history paragraphs.

**`docs/architecture.md` — now done**, via a separate rename+dedupe pass (`docs/plan.md` →
`docs/architecture.md`, commit `665d15c`) that happened to satisfy this rollout's original
index-pattern ask: 620 lines down to ~103, already a thin index (one table row per module, no
rationale). Spot-checked against `docs/docs-style.md` here: every `real`/`actual` occurrence is a
legitimate real-vs-synthetic contrast (this project's established convention), its one
`docs/history.md` reference is the explicitly-allowed "if you're curious" pointer, and the Architecture
table rows are one sentence each except `trn_dataset.py`'s (three sentences, covering four product
types) — long for an index row, but the module itself is the dataset's central orchestration point;
not treated as a violation worth splitting out. No edits made.

- `README.md` — no `docs/history.md` citations to begin with. Cut 3 bare-intensifier
  `real`/`actual`/`actually` instances (kept the ones contrasting real SPICE/NAIF data/tests against
  the deterministic default test run — this project's established convention); the rest were plain
  filler. Also fixed one "one source of truth" duplication: "Viewing the rendered demo" restated the
  `.py`-is-source-of-truth fact the intro section already establishes — trimmed to just the new
  information (ruff/mypy-checked, diffable, `.ipynb` diff noisy) instead of repeating the claim. Split
  one sentence carrying two em-dash asides (the `docker-compose.yml` mounts paragraph) into two. No
  code/behavior described changed.
- `docs/reproject-fov-investigation.md` — no `docs/history.md` citations to begin with. Cut 3
  bare-intensifier `actual`/`genuine`/`actually` instances (kept one legitimately-contrastive
  `actual corner`, explicitly contrasted against "just the along-track edge midpoint" in the same
  sentence). Otherwise already close to the target voice — investigation docs like this one are the
  designated place for the diagnostic-trail content `docs/docs-style.md` says to evict from
  docstrings, so the narrative itself stayed; this pass only touched filler wording, not content or
  the "Still open"/status claims.

- `docs/wac-jigsaw-investigation.md` — no `docs/history.md` citations to begin with. Cut the one
  bare-intensifier `actually` ("confirmed to actually occur" → "confirmed to occur"); every other
  `real`/`actual` occurrence was a legitimate contrast (real SN vs. the broken `"Unknown"` return,
  real vs. synthetic-test candidates, "real control points" vs. the earlier tautological/synthetic
  validation, and the established "real-WAC" compound term) — left as is. Same scope as the other
  investigation docs: filler wording only, not the diagnostic-trail content or the "Open item"
  status, which is this doc's designated purpose per `docs/docs-style.md`'s own carve-out for
  material that belongs in an overview/tutorial doc rather than a docstring.

**Docs not yet reworked**:
- `docs/environment.md` — its content was rewritten separately for the VPS-persistence change (no
  longer "ephemeral VPS, archive/restore"), but that rewrite wasn't done as a `docs/docs-style.md`
  pass — still needs one. At 239 lines it's the longest doc in the repo other than `docs/history.md`
  itself, with a "Multi-agent worktrees" section full of incident narrative ("confirmed live", dated
  fixes) that may be legitimate given the doc's own operational-gotchas purpose, but hasn't been
  checked line-by-line against the voice/filler rules the way the `src/trntest/*.py` files were.
- `docs/proposed-tasks/report-plan.md` — not reviewed against `docs/docs-style.md` at all yet.
  **Hands off for now (2026-09-05): a separate agent is working this file's "Future work" section —
  don't touch it until that lands**, to avoid a collision on the same file.

## If resuming

1. `src/trntest/*.py`, `docs/architecture.md`, `README.md`, `docs/reproject-fov-investigation.md`,
   and `docs/wac-jigsaw-investigation.md` are done; `docs/environment.md` is the only doc left open
   to pick up solo (per the user's request, two at a time unless one's too big on its own — at 239
   lines this one's a likely single-doc batch). `docs/proposed-tasks/report-plan.md` is blocked on
   another agent's in-flight work (see above) — check whether that's landed before touching it.
   Re-verify any `src/trntest/*.py` docstring change with `trntest-lint` and, if it touches a
   function a notebook exercises, `scripts/run_notebook.sh` on the relevant notebook before
   committing. A pure docstring/comment edit with no code-logic change (confirm via `git diff`)
   doesn't need a notebook re-run — `lunaserv.py`'s pass didn't need one.
2. Delete this plan once the remaining docs above have had their pass, or fold whatever's left into
   a narrower follow-up.
