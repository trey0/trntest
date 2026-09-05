# Open items

Genuinely open questions/gaps in `trntest`, pointed to from
[`README.md`](../../README.md)'s "Open items" section. Not a development log — see
[`docs/history.md`](../history.md) for that.

When one of these resolves, delete it — state any fact still needed directly where it's needed,
e.g. a docstring/comment or a `docs/` reference doc, rather than leaving a "Resolved" entry here.

- `notebooks/pose_alignment_spike.py` has two stale references left from the `isis_wac.py` split
  (`docs/history.md`'s Phase 94) — `isis_wac.resolve_ground_to_image_model` and
  `isis_wac.image_to_ground_points_batch`, both now on `isis_campt`. Deliberately not fixed yet: the
  source-code-reorganization effort (see this file's own "Source code reorganization" section) is
  running with the usual per-change notebook-re-execution discipline suspended, one full notebook
  pass planned right before that work merges to `main`. Fix this notebook's `.py` and regenerate its
  `.ipynb` as part of that pass, not before.
- Whether `--save-as-csm` state JSON is an acceptable stand-in for a literal ISD file for whatever
  comes after this demo.
- Confirm the lunar frame kernel defining `MOON_ME` loads correctly so SPICE can output that frame
  directly; sanity-check against the known GLD100/LOLA convention.
- **Astropedia's GLD100 only covers ±79° latitude** (`lunaserv.ASTROPEDIA_MAX_ABS_LATITUDE_DEG`) —
  `fetch_dem_and_ortho` raises rather than falling back to the deprecated, artifact-affected
  Lunaserv DTM path for any footprint beyond it, so a catalog-driven selection near either pole
  fails outright. NASA's VIRA project (`github.com/nasa/vira`) points at higher-resolution
  LOLA-derived polar mosaics for this gap. Not implemented — would need its own fetch/caching and a
  coverage-based dispatch in `fetch_dem_and_ortho`.
- The user's requested "error-handling/fallback-consistency" quality audit only got through
  **Chunk A** (`tie_points.py`+`isis_wac.py`) before spiraling into a real fix rather than staying a
  survey. Chunks B-E were never scoped — re-scope from scratch rather than assume a prior chunking
  plan still applies.
- The real-WAC-crop/hillshade brightness match has an unresolved regression and an unresolved
  validation gap. `lunaserv._terrain_photometric_angles`'s surface-normal computation and
  `hapke_shade_ortho`'s Hapke-ratio relighting were both made permanent/unconditional on the user's
  explicit call, despite the Hapke-ratio fix being confirmed to *worsen* the one measured
  brightness-matched diff (8.6853 → 9.2425) — not yet explained. Real `campt` ground truth can't
  validate the DEM-aware case (it stays ellipsoid-normal-based even with a DEM shape model
  attached), so ASP `sfs` was used as an independent forward-render cross-check instead
  (`sfs_validation.py`): its Lambertian mode's own independently-recovered incidence angle now
  matches `lunaserv.real_geometry_photometric_angles` to ~0.0005 deg mean, closing the DEM-aware
  validation gap for incidence — but confirming (not explaining) that the brightness regression and
  three other live visual observations (a real east-brightening gradient the hillshade
  underrepresents; an apparent ~10 deg shadow rotation confirmed *not* a sun-azimuth bug;
  anomalously bright real crater floors) remain genuinely open. `sfs` itself has a structural gap
  for phase/emission cross-checks: its reconstructed CSM camera can't represent
  `along_track_correction`. See [`docs/history.md`](../history.md)'s Phase 70-79 entries for the
  full investigation trail.
- `lunaserv.fetch_real_hapke_params` samples ISIS's real calibration cube once per image, at the
  footprint's own center — real spatial variation exists within one footprint (a few percent of
  `wh`/`b0`/`hg1`'s own full-Moon range, somewhat more for `hg2`/`hh`) but is secondary to the
  placeholder-vs-real gap this already fixed. Per-pixel sampling (reprojecting the calibration cube
  onto the same working grid the DEM/ortho use) would be a real further refinement.
- `lunaserv.fetch_dem`'s DEM output filename carries no suffix tied to `extra_footprint_lonlat_deg`
  (unlike the ortho's own suffix discipline) — two calls against the same output directory with
  different footprints could silently disagree about which DEM is "the" one. All current real call
  sites pass the same footprint derivation, so no live divergence is known, but a future caller that
  forgets to could reintroduce it.
- `real_hapke_params.ipynb`/`pose_alignment_spike.ipynb` both call `plotting.plot_overlay_toggle`
  directly and haven't been regenerated since `margin_frac`'s default changed to `0.3`
  (2026-09-05) — their committed output still shows the old full-basemap-padding view. Regenerate
  via `scripts/run_notebook.sh` next time either is touched for another reason.
- Whether `stretch_reflectance_to_uint8`'s fixed `[0, 0.30]` display stretch saturates is an
  unresolved question. Two distinct sources, neither confirmed absent: (1) `hapke_shade_ortho`'s
  relit reflectance can exceed the max for geometries near opposition (`ratio > 1`); (2)
  `DISPLAY_STRETCH_REFLECTANCE_MAX = 0.30` was confirmed empirically non-saturating for exactly one
  real candidate — not swept across other candidates/geometries (e.g. fresh crater rays,
  near-opposition geometry could plausibly clip). Saturated pixels would bias
  `sfs_validation.true_albedo_map`'s recovered albedo and reduce
  `compute_brightness_matched_diff`'s discriminating power in any clipped region. Resolving this
  needs an actual multi-candidate saturation sweep, not just asserting either combination is fine.

## Source code reorganization

A source-code naming/organization review (2026-09-05) found three oversized modules
(`lunaserv.py`, `isis_wac.py`, `plotting.py`, all past the ~1000-line split guideline), several
confusing module-name groups, and two self-documented circular-import workarounds plus a third,
undocumented one that only avoids crashing by accident. Six tasks below address this.

**Task 1 (splitting `isis_wac.py` into `isis_wac.py`/`isis_campt.py`, and the circular-import fixes
that came with it) is done** — see `docs/history.md`'s Phase 94 entry for what actually shipped
(including two extra circular-import fixes, in `lunaserv.py`/`render.py`/`spice_kernels.py`, that the
original plan hadn't anticipated). One known gap: `notebooks/pose_alignment_spike.py` still has two
stale `isis_wac.resolve_ground_to_image_model`/`isis_wac.image_to_ground_points_batch` references —
deferred to this reorganization's own final notebook-re-execution pass (see the workflow note below),
tracked as its own bullet in this file's main list above. Tasks 2-6 below are intentionally left
light, since their exact boundaries depend on decisions the later ones will make as they're tackled.

**Workflow for the duration of this reorganization (temporary, per the user's 2026-09-05 direction):**
the usual per-change `scripts/run_notebook.sh` re-execution discipline (`AGENTS.md`'s notebook
section) is suspended across tasks 1-6 — rely on `pytest`/`trntest-lint` to catch references broken
by a rename or split, and do one full notebook re-execution pass across every notebook right before
merging `feature/refactor` into `main`, not after each task. Work happens on
`claude/source-code-org-analysis-52dadf` (or whatever branch continues it); commits there don't need
the user's review. The review gate is merging that branch into `feature/refactor` (pushed to
`origin`, reviewable as a GitHub PR) — `feature/refactor` itself only reaches `main` after the final
notebook pass above. Revert to the normal per-change notebook discipline once this reorganization is
done and this note is deleted.

### Target naming

Decided now so later steps don't each re-litigate it. Apply each rename when its task below is
actually done, not before.

| Current | Becomes | Why |
|---|---|---|
| `isis_wac.py` | `isis_wac.py` (unchanged) + new `isis_campt.py` | split by concern — see task 1's own doc |
| `lunaserv.py` | `dem_ortho.py` (orchestration) + new `dem_gld100.py`, `ortho_wac_emp.py`, `lunaserv_wms.py`, `hapke.py`, `geo_utils.py` | file is named for the one data source (Lunaserv WMS) that's now the deprecated fallback, not the live GLD100/WAC_EMP path it mostly contains |
| `wac.py` | deleted; `SAMPLES`/`VIS_BLOCK_HEIGHT` move to new `wac_format.py` | `fetch_vis_mosaic` and its CDR-byte-layout constants (`PDS3_HEADER_BYTES`/`FRAME_BYTES`/`VIS_BLOCK_OFFSET`/`MISSING_CONSTANT`/`LINES_PER_FRAME`) are dead — superseded by `isis_wac.py`, only self-referenced. `SAMPLES`/`VIS_BLOCK_HEIGHT` are real WAC-VIS sensor-geometry constants `isis_wac.py`/`wac_camera_model.py`/`tie_points.py` still import for real code, not just comments — they need the tiny dependency-free home below, not to go down with the rest of the file |
| `dataset.py` | `candidate_window.py` | matches its own `images_for_window()`; frees the word "dataset" from a name collision with `trn_dataset.py` |
| `dataset_selection.py` | `dataset_selection.py` (unchanged) | names the purpose (which datasets to select), not the technical approach (orbit-level) — keep it |
| `trn_dataset.py` | `trn_dataset.py` (unchanged) | only confusing in contrast to `dataset.py`; once that's renamed this one reads fine as "the `TrnTestDataSet` module" |
| `product_registry.py` | `product_io.py` | it's atomic-publish/read/write helpers, not a registry data structure |
| `plotting.py` | `plotting.py` (kept, shrunk) + new `sfs_plotting.py`, `dataset_selection_plots.py` | splits off the two grab-bag pieces (SFS-only plots, dataset-selection-only scatter plots) that don't belong with the generator-comparison figures |
| `trn_dataset.py`'s product classes | new `trn_products.py` | `TrnTestProduct`/`TrnTestImage`/`TrnTestCropImage`/`TrnTestHillshadeImage`/`TrnTestReprojectImage`/`TrnTestReport` move out, leaving `trn_dataset.py` with just `TrnTestEntry`/`TrnTestDataSet` |
| `pose_alignment.py`, `control_network.py`, `wac_camera_model.py` | `pose_alignment/tie_point_matching.py`, `pose_alignment/control_network.py`, `pose_alignment/wac_camera_model.py` | confirmed a real chain, not just three unrelated back-burner files — see task 6 below; `pose_alignment.py` itself is renamed to avoid colliding with its own package name |

Not renamed: `wac_camera_model.py`'s own basename (distinguishable enough once the legacy `wac.py` is
gone entirely rather than just renamed); any notebook. `notebooks/wac_isis.py` has the same
word-order mismatch against `isis_wac.py` that motivated some of the source renames above, but a
notebook rename is a jupytext-pair regeneration plus a README table edit for cosmetic gain only —
not worth doing as part of this reorganization.

### Task 2: split `lunaserv.py` by data source

1830 lines mixing GLD100 DEM fetch, WAC_EMP ortho fetch, the deprecated Lunaserv WMS fallback, ~650
lines of self-contained Hapke photometry math (no fetch/network code, independently exercised by
`hapke_hillshade.ipynb`/`real_hapke_params.ipynb`/`sfs_validation.ipynb`), and generic CRS/bbox math
that isn't data-source-specific at all. Split along the "target naming" table above; `geo_utils.py`
in particular should end up dependency-free (no `trntest.*` imports beyond `config`), since
`isis_wac.py` needs to import it directly to finish resolving the `isis_wac`↔`lunaserv` cycle noted
in task 1's doc. Every one of the six resulting files should land comfortably under 1000 lines.

### Task 3: `wac.py` deletion + rename cleanup

**`wac.py`**: delete `fetch_vis_mosaic` and its CDR-byte-layout-specific constants outright (dead,
per the naming table); move `SAMPLES`/`VIS_BLOCK_HEIGHT` to a new `wac_format.py` (just the two
constants, no imports of its own) first, then delete the file. Update the three real importers
(`isis_wac.py`, `wac_camera_model.py`, `tie_points.py` — note `tie_points.py` currently reaches
these constants both directly as `wac.SAMPLES` and indirectly as `isis_wac.SAMPLES`'s re-export;
route both call sites to `wac_format.py` instead of preserving either). Drop `session.py`'s
`fetch_vis_mosaic` delegator, `__init__.py`'s re-export, and `tests/test_wac_unpacking.py`; drop the
`fetch_vis_mosaic`-specific parts of `tests/test_session.py`.

**Pure renames** (no behavior change beyond the name): `dataset.py` → `candidate_window.py`,
`product_registry.py` → `product_io.py`. Update `README.md`'s source-files table, docstrings, and
every importer for both parts of this task. Independent of tasks 1/2/4/5/6; can be done in any order
relative to them.

### Task 4: split `plotting.py`

1634 lines, docstring-described as "matplotlib display helpers for the notebook" but actually four
distinct audiences: generic raster-display primitives, generator-comparison figures
(`plot_render_vs_basemap`/`plot_overlay*`/`plot_zoom_blink`/`compute_brightness_matched_diff` — what
`image_generation.py`/reports need), SFS-validation-only plots (`plot_sfs_comparison`/
`plot_incidence_validation`), and dataset-selection scatter plots (`plot_sun_elevation_vs_edr_count`/
`plot_illuminated_node_scatter`, the only reason this file depends on `illumination.py` at all). Keep
the first two audiences in `plotting.py`; move the other two to `sfs_plotting.py`/
`dataset_selection_plots.py` per the naming table.

### Task 5: split `trn_dataset.py`'s product classes into `trn_products.py`

Under the 1000-line guideline (855) but has a clean seam already: `TrnTestEntry`/`TrnTestDataSet`
(dataset/filesystem structure) vs. `TrnTestProduct`/`TrnTestImage`/`TrnTestCropImage`/
`TrnTestHillshadeImage`/`TrnTestReprojectImage`/`TrnTestReport` (the per-generator product classes —
`docs/generators.md`'s three generators plus the report). Low urgency; do opportunistically or once
the file grows further.

### Task 6: group the back-burner trio into `pose_alignment/`

Checked before committing to this grouping (2026-09-05), since a `stuff_not_wired_in_yet`-flavored
subpackage with a name that doesn't actually mean anything would be worse than not grouping at all:
`control_network.py`, `pose_alignment.py`, and `wac_camera_model.py` are a real chain, confirmed via
[`docs/pose-alignment.md`](../pose-alignment.md) and their own docstrings, not just three unrelated
modules that happen to share "not wired in" status. `pose_alignment.py` produces 2D matched tie
points; `control_network.py` converts them into 3D ISIS control points and directly imports
`wac_camera_model` for ground-to-image lookups; `wac_camera_model.py` supplies the hand-rolled camera
model both `control_network.py` and the eventual pose-correction fit need, built specifically because
ISIS's `jigsaw` bundle adjuster has a confirmed bug for this camera. All three cite
`docs/pose-alignment.md` as their shared reference doc, which is itself titled around this one
investigation, not a grab-bag. `pose_alignment/` is an accurate name for the whole activity, not a
euphemism for "unfinished."

Moving them into a `pose_alignment/` subpackage (naming table above; `pose_alignment.py` itself
becomes `pose_alignment/tie_point_matching.py` to avoid colliding with the package's own name) makes
the active-vs-experimental boundary visible in the directory tree instead of only in prose. Doesn't
reduce their cross-references to core modules (`camera`, `isis_wac`/`isis_campt`, `tie_points` are
all still needed) — this is purely a discoverability win.
