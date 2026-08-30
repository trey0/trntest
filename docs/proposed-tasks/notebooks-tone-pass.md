# Notebooks: tone/structure pass

**Status: 6 of 11 notebooks done.**

## Goal

`notebooks/*.py` had drifted from "tutorial showing the high-level entry points, with just enough
rationale to explain the demo's benefit" into development-history narrative (bug root causes,
`docs/history.md` citations, tuning backstory) -- per `docs/docs-style.md`. Fix each notebook's
markdown cells to match; regenerate its `.ipynb` (`scripts/run_notebook.sh`) after editing. One
notebook at a time, user reviews in Jupyter Lab before commit.

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
7. `real_hapke_params.py` (165 lines)
8. `sfs_validation.py` (172 lines) -- **check before editing**: its intro claims a "still-open,
   unexplained regression against the real WAC crop" (Phase 68-72). `docs/plan.md`'s `lunaserv.py`
   row says that regression "now looks resolved as of Phase 78" -- likely another stale-status
   case like `wac_isis.py`'s. Verify against current `docs/plan.md`/`docs/data-sources.md` before
   deciding what this notebook should say.
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
