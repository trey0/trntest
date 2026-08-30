# Notebooks: tone/structure pass

**Status: 8 of 11 notebooks done.**

## Goal

`notebooks/*.py` had drifted from "tutorial showing the high-level entry points, with just enough
rationale to explain the demo's benefit" into development-history narrative (bug root causes,
`docs/history.md` citations, tuning backstory) -- per `docs/docs-style.md`. Fix each notebook's
markdown cells to match; regenerate its `.ipynb` (`scripts/run_notebook.sh`) after editing. One
notebook at a time, user reviews in Jupyter Lab before commit.

## Also check: undocumented cross-notebook dependencies

Added 2026-08-30, per user request. Some notebooks quietly assume another notebook already ran and
produced their input. For each notebook (including ones already marked done above), check every
product path it reads and prefer, in order:

1. **Generate on demand if missing** -- the existing pattern in this codebase:
   `TrnTestEntry.camera`/`.crop_result`/`.dem_ortho_result` (`trn_dataset.py`, all
   `functools.cached_property`, all idempotent resume-from-disk-or-fetch-fresh) and
   `TrnTestImage.generate()` (`if not self.exists(): self._generate_impl()`). Route reads through
   these rather than a raw path.
2. **Fail fast with a clear message** if (1) isn't practical -- `TrnTestImage._require_generated()`
   already does this (`FileNotFoundError` naming the product and pointing at `.generate()`/
   `dataset.populate()`); reuse it rather than inventing a new one.
3. **A markdown warning cell** naming the notebook to run first -- last resort, only when neither
   fits.

**Audit (2026-08-30): the codebase already does (1) almost everywhere.** Every notebook markdown
claim of the form "resumed from disk if `image_generation.ipynb` already generated it, else fetched
fresh here" (`hapke_hillshade.py`, `real_hapke_params.py`, `along_track_correction.py`,
`sfs_validation.py`, `crater_sharpness_review.py`, `pose_alignment_spike.py`) is accurate, not a
hidden dependency -- all go through the self-healing properties above. `wac_isis.py`/
`select_datasets.py` never touch `TrnTestEntry` at all, no risk. **One real gap found**:
`reproject_spike.py` reads `entry.hillshade.raster_path` directly in two plotting cells instead of
through `.generate()`/`_require_generated()` -- a genuine undocumented dependency on
`image_generation.ipynb` having run first. Left as-is: item 10 below already flags this notebook as
likely headed for `old_notebooks/` rather than a rewrite, so fix it only if it survives that
decision -- otherwise moot. Re-check this for `crater_sharpness_review.py`/`reproject_spike.py`/
`pose_alignment_spike.py` specifically as each is reworked below (the others are already confirmed
clean).

## Order

Ascending file size, since that roughly tracks how much history-narrative buildup each one has:
quick wins first, the two large investigation "spikes" last (most rework, and each needs its own
scope decision -- see below).

1. ~~`image_generation.py`~~ -- done (also produced `docs/generators.md` +
   `docs/generators/{hillshade,crop,reproject}.md`, the canonical generator reference the
   notebook's own intro table now links to).
2. ~~`wac_isis.py`~~ -- done. Kept (not archived) per explicit user direction, but minimally: cut
   its "unresolved blocker" investigation-status framing rather than update it, since restating
   current status here would just duplicate `docs/data-sources/lroc-wac-edr-cdr.md` and risk going
   stale again.
3. ~~`select_datasets.py`~~ -- done, light touch (already close to tutorial tone).
4. ~~`report_template.py`~~ -- reviewed, no changes needed. Confirmed genuinely different in kind:
   a Jinja template (`scripts/render_report_template.py` substitutes `{{ dataset_folder }}`/
   `{{ edr_product }}`), not paired with an `.ipynb`, deliberately kept to bare one-liner cells
   (`trntest/report.py`'s own docstring). No narrative to cut, no stale claims, no
   `docs/history.md` citations -- already exactly what a template should be.
5. ~~`hapke_hillshade.py`~~ -- done.
6. ~~`along_track_correction.py`~~ -- done, tone pass **plus a real bug fix**. Found live:
   `basemap_and_diff` used `rasterio.windows.from_bounds` + manual pixel-index slicing to crop a
   freshly-fetched basemap down to the real WAC crop's own window, assuming both always come out the
   same array shape -- confirmed false (`ValueError: operands could not be broadcast together with
   shapes (1827,1688) (1828,1688)`), most likely since the WAC_EMP PDS4 ortho-source migration
   changed how the DEM/ortho fetch rounds its pixel grid. First fix attempt
   (`reindex_like(..., method="nearest", tolerance=cellsize/2)`, matching
   `plotting.compute_brightness_matched_diff`'s own technique) executed clean but left a visible
   artifact the user caught by eye: an all-NaN row (white line) in both panels, around row 653 --
   the two grids' pixel pitch drifts by a fraction of a cell across the image, and right at the row
   where that drift crosses the half-cell tolerance boundary, nearest-neighbor matching fails
   entirely. Switched to `interp_like(method="linear")` instead -- confirmed zero NaN pixels in the
   aligned array, and the regenerated figure has no artifact. Verified both numerically and by
   visually inspecting the regenerated `.ipynb`'s own output image. Root cause (a structural
   `cam2map`-vs-GDAL/rasterio grid-rounding disagreement, not the WAC_EMP migration -- confirmed
   directly by testing the old `lunaserv_wms` ortho path too, not guessed) is now documented in
   `docs/external-tools.md`'s ISIS Pushframe pipeline section, so it doesn't have to be
   re-diagnosed if it resurfaces elsewhere.
7. ~~`real_hapke_params.py`~~ -- done, light touch. Was one of 4 notebooks `docs/plan.md` flagged
   as stale since the WAC_EMP ortho-source migration (never regenerated under it) -- regenerating
   surfaced a real finding, now noted directly in the notebook: on this one candidate, placeholder
   Hapke params slightly beat the real-calibration default (5.62 vs. 6.25 mean|diff|), opposite of
   what motivated the default. Kept the default as-is (physical grounds, single-candidate result).
   Also updated `docs/plan.md`'s stale-notebook tracking to match (2 of 4 no longer stale;
   `reproject_spike.py`/`pose_alignment_spike.py` still are).
8. ~~`sfs_validation.py`~~ -- done. Confirmed stale as expected: cut the "still-open, unexplained
   regression" framing entirely (not updated -- restating the nuanced current status here would
   just duplicate `docs/plan.md`'s own entries and risk going stale again, same call as
   `wac_isis.py`'s). Regenerating surfaced two real, live bugs, neither related to the tone edits:
   (1) `entry.dem_ortho_result`'s DEM/ortho pair was already mismatched on disk (shapes
   `(2267,2258)` vs `(2440,2387)`) -- a live hit of `docs/plan.md`'s already-documented
   footprint-suffix-less `dem_filled-tile-0.tif` clobbering gap, traced to
   `along_track_correction.py`'s own `fetch_dem_and_ortho` call missing
   `extra_footprint_lonlat_deg=entry.crop_footprint` (unlike its `hapke_hillshade.py`/
   `real_hapke_params.py` siblings, already fixed this way per `docs/plan.md`). Fixed that call
   site the same way and regenerated `along_track_correction.ipynb` too (confirmed both
   `along_track_correction` variants now fetch the same 2387x2440 ROI). (2) The notebook's own
   `sim_masked_path` was hand-built from `config.output_dir / "sfs_run"`, but
   `run_sfs_forward_render` actually writes under `config.output_dir / "sfs_validation" / "sfs_run"`
   -- fixed by deriving the path from `sfs_result.sim_intensity_tif.parent` instead of
   reconstructing it. Regenerated numbers corroborate the "resolved" framing: brightness-matched
   diff 0.00382 (matches `docs/plan.md`'s own recorded Phase 78 value exactly), incidence diff
   0.0005 deg mean/max (matches its Phase 77 value). Verified with `trntest-lint` on both notebook
   pairs.
9. `crater_sharpness_review.py` (255 lines)
10. `reproject_spike.py` (507 lines) -- **likely obsolete, decide before rewriting**: its premise
    is "should we build `TrnTestReprojectImage`?", but `reproject` is now fully implemented and
    wired into `image_generation.py`'s Phase 8. May be a better fit for `old_notebooks/` (archived,
    frozen) than a tone rewrite -- ask the user, same as the `wac_isis.py` discussion.
11. `pose_alignment_spike.py` (666 lines, largest) -- its own "on the back burner, not superseded"
    framing already matches current status (per `docs/plan.md`), so likely just needs the
    tone/citation pass, not a status correction.

## If resuming

Re-check each notebook's status claims against current `docs/plan.md` before editing -- the
pattern found twice already (`wac_isis.py`, likely `sfs_validation.py`) is a notebook asserting an
"open question" that was actually resolved elsewhere later. Once all 11 are done, fold this list's
learnings (if any are still load-bearing) into `AGENTS.md`'s notebook bullet and delete this file.
