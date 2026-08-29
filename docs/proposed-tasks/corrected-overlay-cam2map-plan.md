# Corrected blink overlay via a `ConstantRotation` cube patch: implementation plan

Status snapshot as of 2026-08-18, written specifically so this can be resumed from a fresh session
if the current one runs out of budget mid-way -- read this doc plus
`docs/wac-jigsaw-investigation.md` (background/full context on `fit_pose_correction` and everything
that precedes this) before continuing. Check `git log`/`git status` on `feature/alignment` first to
see which chunks below are already committed.

## Goal

`wac_camera_model.fit_pose_correction` (see `docs/wac-jigsaw-investigation.md`) produces a real
6-DOF `PoseCorrection`, live-fit against real control points: baseline residual mean 4.42px -> 3.36px
after correction, dominated by a small camera-frame rotation (~0.18 deg). There is no visual
(blink-overlay) evidence of this correction yet. The plan: reuse ISIS's own already-validated
`cam2map` for the reprojection (no new hand-rolled warp/resampling code) by patching a *copy* of the
real WAC crop cube's cached `InstrumentPointing` SPICE table so `cam2map` picks up the corrected pose
automatically, then feed the result into the existing `plotting.plot_overlay_toggle`.

## Validated so far (this session)

- Real ISIS tools: `tabledump` (export a cube's named Table object to CSV) + `csv2table` (import a
  CSV and attach/overwrite a named Table on a cube, with an optional `label=` flat-PVL file for the
  table's own extra keywords beyond the raw field records -- **required, not optional**, see next
  point).
- `InstrumentPointing`'s real Table object (confirmed via `catlab` on the actual crop cube) carries
  load-bearing metadata beyond the raw quaternion/AV/ET records: `TimeDependentFrames`,
  `ConstantFrames`, `ConstantRotation` (a single fixed 3x3 matrix), `CkTableStartTime`/`EndTime`/
  `OriginalSize`, `FrameTypeCode`, `Description`, `Kernels`. Round-tripping the CSV *without* this
  label metadata (`csv2table` with no `label=`) produces a real, systematic pointing error (~0.08 deg
  latitude, confirmed live) -- not data-precision loss, ISIS silently drops the constant-frame
  offset. Round-tripping *with* the label (extracted via the `pvl` library from real `catlab` output,
  not hand-transcribed -- precision/typo risk) reproduces the original cube's own `campt` output to
  ~1e-8 deg (pure floating-point/CSV-text noise).
- The cached `InstrumentPointing` quaternions (259 rows, ~1.4s apart -- the *full original swath*,
  not just this 70-framelet crop's own range; `crop` doesn't truncate this table, already documented
  elsewhere in `isis_wac.py`) represent spacecraft-bus frame (-85620) to J2000, **not** camera
  (-85621) to J2000 directly -- a separate, single, time-*independent* `ConstantRotation` matrix
  handles -85621->-85620. Since `PoseCorrection.delta_rotation` is defined as a camera-frame
  correction, it can be injected by modifying **only** that single 3x3 `ConstantRotation` matrix --
  the 259-row quaternion table itself never needs to be touched or re-derived.
- **Empirically validated composition formula** (live cross-check: a known synthetic test rotation
  [1.0, -0.6, 0.3] deg, 4 candidate formulas tried, compared against `wac_camera_model`'s own
  already-validated forward projector's prediction for the same ground point):

  ```
  ConstantRotation_new = delta_rotation.T @ ConstantRotation_original
  ```

  matched the forward projector's prediction to ~1e-6 (effectively exact: predicted
  `(363.23274131, 492.99873239)` vs. actual `(363.23274076, 492.99871535)`). The naively-expected
  `ConstantRotation_original @ delta_rotation` was invalid (point fell out of coverage entirely);
  the other 2 of 4 candidates were off by ~0.1px (clearly wrong, not a near-miss). Likely explanation
  (not independently re-derived from ISIS source, just empirically confirmed -- which is enough to
  trust it): ISIS's stored matrix is the transpose of what this project's own `R_A_to_B` convention
  (`v_B = R_A_to_B @ v_A`, confirmed via `camera.py`/`wac_camera_model.py`) would suggest.
- **Position correction: deliberately not implemented via this mechanism.** `InstrumentPosition`'s
  cache is a coarser `HermiteSpline` over only 9 nodes (J2000 frame, km); injecting a position
  correction would need each node rotated from MOON_ME (`delta_position_m`'s frame) into J2000 via
  `BodyRotation`'s own quaternion before adding -- not attempted. The real fit found position
  negligible (~9m -> ~0.06px at this slant range, see `docs/wac-jigsaw-investigation.md`), so
  rotation alone should account for essentially all of the fit's real effect. State this explicitly
  as a deliberate scope cut in the final code/docs, not an oversight.
- Scratch validation scripts used to establish all of the above (not committed -- outside git, see
  `docs/environment.md`'s "Where things belong" section; safe to delete or leave, harmless either
  way): `src/scratch/a1_framelet_search_validation.py`, `a1_extract_table_label.py`,
  `a1_constantrotation_test.py`. Contain the exact working commands if anything above needs
  re-confirming.

## Status update (2026-08-19, fresh session resuming this plan)

Chunks 1-2 done and committed (`eb3b670`, `feature/alignment`): `isis_wac.apply_pose_correction_to_crop`
+ `_table_extra_label`, with unit tests (`tests/test_isis_wac_ground_to_image.py`,
`tests/test_isis_wac_parsing.py`) and a live cross-check against the *real* fitted correction from
`fit_pose_correction` (not just the synthetic test rotation validated last night): patched-cube
`campt` output agrees with `wac_camera_model`'s own forward projector to <=0.015px across 3 real
points (scratch script: `src/scratch/a1_real_fit_cross_check.py`, outside git per
`docs/environment.md`).

Chunk 3 (notebook wiring) is done in the working tree (`notebooks/pose_alignment_spike.py`/`.ipynb`,
uncommitted) and has been run end-to-end via `scripts/run_notebook.sh` with no errors -- both the
new `plot_overlay_toggle` cells (uncorrected vs. pose-corrected) rendered real output. **Holding for
the user's own Jupyter Lab review before committing**, per this project's established convention.
Chunk 4 (docs cleanup) not yet started.

## Remaining work, in commit-sized chunks

1. **Real module function.** Add to `src/trntest/isis_wac.py` (the right home -- this is an
   ISIS-cube-manipulation concern, matching that module's existing house style: frozen dataclass
   results, `config = config or load_config()`, subprocess calls, real docstring provenance) a
   function along the lines of:

   ```python
   def apply_pose_correction_to_crop(
       crop: CropResult, correction: "wac_camera_model.PoseCorrection", config=None
   ) -> CropResult
   ```

   that:
   - Copies the crop cube to a new path (e.g. `<stem>.corrected.cub`).
   - `tabledump`s `InstrumentPointing` to a temp CSV (unchanged, reused as-is for `csv2table`).
   - Extracts the table's full label via `catlab` + `pvl` (promote the scratch script's logic, don't
     hand-transcribe).
   - Computes `ConstantRotation_new = correction.delta_rotation.T @ ConstantRotation_original` (the
     validated formula above) and substitutes it into the extracted label.
   - Writes the modified label + unchanged CSV back via `csv2table` onto the cube copy.
   - Returns a `CropResult` (or similar) pointing at the new, corrected cube.

   Note `wac_camera_model.py` currently has no dependency on `isis_wac.py` or vice versa in this
   direction (check for import cycles before wiring the `PoseCorrection` type in -- `isis_wac.py`
   already imports from `camera.py`, so importing `wac_camera_model` from `isis_wac.py` should be
   safe, but confirm).

   Add real unit tests (mock `subprocess.run`/`run_quiet`, matching `tests/test_isis_wac_ground_to_image.py`'s
   existing style) plus a live/heavy validation that reuses this session's cross-check methodology --
   compare `ground_to_image_pixel` on the patched cube against `wac_camera_model.
   find_framelet_and_project`'s own prediction, this time for the *real* fitted correction from
   `fit_pose_correction`, not just the synthetic test rotation used to validate the formula itself.
   Run `trntest-lint` + full test suite before committing.
   **Commit here.**

2. **Wire into `run_cam2map_for_crop`.** Reproject the corrected cube via the *existing*, unmodified
   `run_cam2map_for_crop` (it already reads pose directly from the cube's own cached SPICE data --
   no changes needed there at all) to produce a corrected, map-projected GeoTIFF on the same grid as
   the existing uncorrected `wac_path`. Likely small enough to fold into step 1's commit if it's
   trivial by the time step 1 lands -- use judgement, don't force an artificial split.
   **Commit here** (if separate from step 1).

3. **Notebook wiring.** In `notebooks/pose_alignment_spike.py`, after the existing real-fit cells:
   apply `fit.correction` via step 1/2's function, then
   `plotting.plot_overlay_toggle(basemap_path, corrected_cam2map_path, title=...)` alongside the
   existing uncorrected one, for a direct visual before/after. Run via `scripts/run_notebook.sh` per
   this repo's own convention. **Hold for the user's own Jupyter Lab review before committing** --
   matches this session's established pattern (notebook-output changes wait for explicit
   confirmation, not auto-committed).
   **Commit here** (after review/confirmation).

4. **Docs cleanup.** Update `docs/wac-jigsaw-investigation.md`'s remaining-work list (mark the
   corrected-overlay item done or update its status) and `docs/plan.md`'s status line. Consider a
   `docs/data-sources.md` entry for the `tabledump`/`csv2table` mechanism and the empirically
   validated `ConstantRotation` formula -- a genuinely reusable fact worth preserving there per that
   doc's own stated purpose ("current, stable facts... consult before re-deriving from scratch").
   Delete this plan doc once its content is folded into the other docs, or leave it as a historical
   record -- user's call at that point, not decided here.
   **Commit here.**

## If resuming in a fresh session

1. `git log --oneline -10` and `git status` on `feature/alignment` (this worktree, `a1`) to see
   which chunks above are already committed.
2. Read `docs/wac-jigsaw-investigation.md` for full background if anything here is unclear.
3. Re-run this session's scratch scripts (`src/scratch/a1_constantrotation_test.py` etc., if still
   present) to re-confirm the validated formula still holds before trusting it blindly, especially if
   picking this up much later or after other changes to `camera.py`/`wac_camera_model.py`.
