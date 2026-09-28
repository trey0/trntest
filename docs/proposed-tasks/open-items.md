# Open items

Genuinely open questions/gaps in `trntest`, pointed to from
[`README.md`](../../README.md)'s "Open items" section. Not a development log — see
[`docs/history.md`](../history.md) for that.

When one of these resolves, delete it — state any fact still needed directly where it's needed,
e.g. a docstring/comment or a `docs/` reference doc, rather than leaving a "Resolved" entry here.

- **The ±60° WAC_EMP horizontal-line artifact (both hemispheres): the correction is inadequate.**
  Both WAC_EMP tile families (equirectangular, 0-60°; polar-stereographic, 60-90°) carry a real
  edge-brightening defect right at their shared boundary, confirmed directly in the archived `.IMG`
  pixels: the equirect tile's last valid row runs bright, and the polar tile has a damped-oscillation
  overshoot/undershoot in its last few pixels. `wac_emp_edge_correction.py` masks the equirect row
  outright, masks and models the polar side (a damped cosine fit live per window, not one fixed number
  per hemisphere), and closes the small resulting coverage gap (`fill_nearby_gaps`, scoped to pixels
  actually near the masking). Wired into `ortho_wac_emp._reproject_one_wac_emp_tile_to_array`, gated by
  `TrntestConfig.wac_emp_edge_correction_enabled` (default on). See
  `notebooks/wac_emp_seam_correction.py` for the derivation and validation: the seam's peak
  row-to-row jump shrinks 36-61% across the three entries checked (two `trntest1` south entries,
  `M1314469291CE`/`M1314314993CE`; one `trntest2` north entry, `M1309348984CE`).
  Still open:
  1. *Root cause unknown.* Why both archived tile families have this defect (USGS/ASU's own
     production pipeline) hasn't been investigated.
  2. *Tile-precedence flicker, untouched by this correction.* The two tiles' coverage boundary is a
     jagged, locally-diagonal line in the destination grid, not a clean cut, so a single destination
     row can cross it at scattered, non-contiguous columns — whichever tile isn't given merge
     precedence there flickers in and out as a periodic pattern, independent of either tile's own
     radiometry.
  3. *Effectiveness is geometry-dependent, not just hemisphere/longitude-dependent.* For
     `M1309348984CE`, the masked equirect row maps into only a narrow, diagonal swath of that entry's
     own destination grid rather than the full width, so the visible artifact barely changes at
     full-frame-crop scale even though the correction is running and does change the underlying
     pixels. Whether this is common across `trntest2`'s other ~10 affected north entries or a one-off
     for this entry's viewing geometry is unquantified.
  4. *Mask/correction/reference zone widths (2px/8px/13px) are still shared, hemisphere-wide
     constants*, tuned only against the two `trntest1` south entries — not yet revisited for the same
     per-window variation the polar model fit itself now tracks.
  5. A separate, small coverage-gap defect (unrelated to this edge-brightening artifact) already lives
     in many of the same archived tiles, confirmed on most boundary-straddling entries checked. Left
     correctly unfilled by `eligible_gap_fill_mask`'s scoping, not investigated further.
  6. *Inadequate at the three-tile corners, both visually and by metric.* `reflectance_seams.ipynb`'s
     probes show blatant horizontal streaks within ~15-20 km of where a longitude seam meets ±60° at
     7 of the 8 corners (60°N 0°E is the only visually acceptable one; strongest at 60°N 180°E/270°E),
     and a near-seam gradient spike (ratio 1.3-2.2 vs. 1.23 for control lines) even where the streak
     is subtle to the eye. Those 7 fail `seam_probes.wac_emp_thresholds`' ±60° gradient limit (1.4)
     and are strict expected failures in `tests/test_reflectance_seams.py`: remove each marker as a
     better correction fixes that corner.
  `trntest2` is not being regenerated to pick up this correction as part of this change.

- **GLD100 has its own ±60° seam: a nodata row the DEM pipeline never fills.** Separate from the
  WAC_EMP artifact above (same latitude, different source). The cached Astropedia file itself has
  nodata at 60°N (row 5761: 40,383 of 109,165 columns; row 5760: 3,891 more) and a smaller partial
  row at 60°S (3,826 columns). The bilinear warp to the local grid keeps it as a 1-px NaN row, and
  `dem_mosaic --hole-fill-length` doesn't fill it (probably because it touches the raster edge, so it
  isn't an enclosed hole), leaving -3.4e38 nodata in `dem_filled_*.tif`. Shading then draws a
  visible curved line along the parallel: in `trntest2` entry 37 (`M1309363051CE`) the basemap's
  row mean goes 28 → 3 on the gap row and 35-47 just poleward, while the underlying (edge-corrected)
  WAC_EMP reflectance is flat across it (0.1217 → 0.1204). That entry's footprint stops short of
  60°N, so only its basemap shows the line, but any entry whose footprint reaches the gap feeds
  that nodata row to `sat_sim`/`mapproject` too. Possibly also behind some of the "implausibly steep
  pixels" item below. Likely fix: fill DEM NaNs right after the GLD100 warp, before `dem_mosaic`,
  with a fill that also catches edge-touching gaps.
- **Cast shadows speckle on slopes nearly parallel to the sun.** `cast_shadow.sun_sweep`'s lit test
  (`filled >= running_max`) flips pixel-by-pixel where a slope faces away from the sun at close to
  the sun's elevation, since height across the rays is then nearly constant and small DEM variation
  (integer-meter GLD100, spline-upsampled) decides each pixel. Seen as a "screen door" of black dots
  in `trntest2` entry 37's zoomed basemap (sun elevation 15.5°); rerunning
  `cast_shadow.illumination_fraction` with that camera's SPICE sun vector reproduces all 103 dots in
  the crop. Across that DEM, 89% of isolated shadow pixels have an uphill-toward-sun slope of
  0.10-0.35 (median 0.21; tan 15.5° = 0.28), vs. 14% of all pixels. Not the bin-aliasing moiré
  `BIN_SIZE_SAFETY_FACTOR` fixes (99.9% of occupied bins hold more than one sample). Possible fixes: a
  small height tolerance in the lit test, or dropping shadow pixels without shadowed neighbors.

- **`candidate_window.py`'s CDR-matching (`attach_cdr`, `catalog.find_matching_cdr`, the `cdr_volume`/
  `cdr_subdir`/`cdr_doy`/`cdr_product` manifest columns) is now fully vestigial.** Its one real
  consumer, `wac.py`'s manual CDR mosaic extraction, was deleted (superseded by `isis_wac.py`, which
  works from the EDR, not the CDR) as part of the source-code reorganization's task 3 — `TrnTestEntry`/
  `TrnTestImage` never read these columns either. Not removed here: deciding whether to drop the whole
  feature (vs. keeping it for manifest provenance/a future consumer) is a separate call from "delete
  the dead module that used it," out of scope for that task.
- Whether `--save-as-csm` state JSON is an acceptable stand-in for a literal ISD file for whatever
  comes after this demo.
- Confirm the lunar frame kernel defining `MOON_ME` loads correctly so SPICE can output that frame
  directly; sanity-check against the known GLD100/LOLA convention.
- **Astropedia's GLD100 only covers ±79° latitude** (`dem_gld100.ASTROPEDIA_MAX_ABS_LATITUDE_DEG`) —
  `fetch_dem_and_ortho` raises rather than falling back to the deprecated, artifact-affected
  Lunaserv DTM path for any footprint beyond it, so a catalog-driven selection near either pole
  fails outright. NASA's VIRA project (`github.com/nasa/vira`) points at higher-resolution
  LOLA-derived polar mosaics for this gap. Not implemented — would need its own fetch/caching and a
  coverage-based dispatch in `fetch_dem_and_ortho`.
- **Cast shadows (`cast_shadow.py`) have four known gaps.**
  - **Occluders outside the AOI are invisible.** The DEM's sun-facing edge is always lit. At a 13°
    sun a 1 km rim casts a ~4.3 km shadow, so terrain just up-sun of the fetched AOI can matter. A
    fix would pad the DEM up-sun by about `max_relief / tan(elevation)` before sweeping and crop back
    afterward.
  - **Validated on few candidates**: `M1327218454CE` (~13° sun) in depth; `M1314350330CE` (16°) and
    `M1314313691CE` (84°) visually, after the high-sun ordering fix (`docs/history.md` Phase 128).
    Re-check a mid-elevation (40-60°) candidate.
  - **The sweep marks roughly half as much shadow as ISIS `shadow`**, with shadows in the same
    places (see `notebooks/sun_aligned_shadow_sweep.ipynb`). Unexplained; neither is ground truth.
  - **No penumbra.** The Sun's ~0.53° disk softens a shadow edge over `~0.0093 × D` (`D` = occluder
    distance along the ray), so it only spans a 100 m pixel once `D` exceeds ~11 km — likely
    sub-pixel for most candidates. Compute `D` for real low-sun candidates before building it. If
    it's worth it, the sweep can average several sun directions sampled across the disk.
- **Two consumers of the crop's map projection haven't been re-checked since `cam2map` was
  replaced by `wac_resample`:** `sfs_validation.ipynb` (not re-run) and the brightness-matched
  hillshade/crop diff below (`plotting.compute_brightness_matched_diff`, not re-measured).
- **Decide: adopt a workflow manager, or extend the homegrown product graph.** `truncate()` deletes
  only the generator outputs it's given, so dependents go stale (truncating `reproject` leaves
  `report`/`gallery`, built from it, marked done). It also can't express "this intermediate changed"
  (e.g. the crop's map projection, which feeds `reproject`, `report` and `gallery`), and unused
  files pile up in `_work/` (1.8 GB of `cam2map` outputs in `trntest1`/`trntest2`). The underlying
  structure is a product-level dependency graph; generators are bundles of products, useful as the
  `populate()` interface. Snakemake covers most of this: rules with `input:`/`output:` (the body
  gets its paths from the declarations), per-entry wildcards, `-R` to rerun a rule and everything
  downstream, `-j`/`--retries`/`--keep-going`, and `--rerun-triggers code params` (the rule's own
  code only, not imported `trntest` code). Costs: notebooks use in-process generate-on-demand
  properties (`entry.crop_result` etc.) that don't map onto CLI jobs, per-job process startup
  (imports, SPICE kernels), and replacing `tasks.py`/`populate*`/`truncate`/skip list/`status`.
  Dagster's asset model (partitions per entry, `code_version` staleness) fits the framing best but
  adds a UI/daemon; Bazel's hermetic sandboxing fights network fetches and GB rasters. Next step: a
  Snakemake spike of one entry (crop -> crop map projection -> `reproject` render -> report) calling
  the existing `trntest` functions, checking `-R` semantics, code-change triggers, per-job overhead,
  and notebook fit.
  Fallback if it doesn't fit, in stages that can stop after any one:
  1. Declare reads with the unused `product_io.reads_product` next to `writes_product` (principle 7
     of `docs/intermediate-product-discipline.md`), give each product a `_work/<entry>/<product>/`
     home, and make `truncate(entries, products=...)` delete the downstream closure, never
     upstream. Products regenerated on every use still pass invalidation through; `cache/`
     products need an explicit opt-in; generator names alias their final products.
  2. Per-label path resolvers in `product_io` as the only place product paths are built
     (lint-checked).
  3. A `ProductPath` type carrying its label, checked by the decorators at call time, which also
     catches undeclared reads of paths passed in (e.g. `DemOrthoResult.dem`).
  Either way, deciding which product a library code change invalidates stays a manual call.
- **`docs/external-tools.md` is bloated** (~640 lines, one file for every external tool, heavy on
  investigation narrative). Prune it per `docs/docs-style.md`, or split it into per-tool files behind
  a thin index, as `docs/data-sources.md` and `docs/generators.md` already do.
- **Some renders have nodata blobs at the image's top edge.** After regeneration, 21 of 207
  `trntest1` and 15 of 69 `trntest2` `reproject` renders have them (up to 0.2% of the image), as
  blobs on crater rims, not a strip along the edge. It isn't `reproject`-specific: in 13 of the 21
  `trntest1` entries the `hillshade` render has exactly the same nodata pixels, which points at the
  render's view reaching past the DEM (or failing to intersect it) rather than past the crop; the
  other 8 are `reproject`-only, which fits the view reaching past the crop's along-track end.
  `trntest2`'s two renders mostly differ pixel-for-pixel, though most of its `hillshade` renders
  are older than its `reproject` ones. `sat_sim` writes nodata there (a nodata square in the
  texture becomes exactly nodata in the render), but `reproject`'s nodata is the ISIS sentinel
  -3.4e38, declared only in the GeoTIFF tag (`hillshade` uses NaN), so a consumer that ignores the
  tag reads huge negative values. Start by checking the render footprint against the DEM and crop
  extents for `M1314340826CE` (both renders) and `M1314357076CE` (`reproject` only).
- **`trntest1` DEMs have implausibly steep pixels.** 38 of 207 filled DEMs have pixels steeper
  than 60° at 100 m posting (up to ~300 per DEM, max 76.1° in `M1314424588CE`); real lunar slopes
  at that scale rarely exceed ~40°. Likely seams, hole fills or source-DEM defects. Cast shadows
  render them faithfully, so at low sun they would cast shadows that the real terrain doesn't.
  Not yet looked at; start by imaging `M1314424588CE`'s DEM and slope map.
- The user's requested "error-handling/fallback-consistency" quality audit only got through
  **Chunk A** (`tie_points.py`+`isis_wac.py`) before spiraling into a real fix rather than staying a
  survey. Chunks B-E were never scoped — re-scope from scratch rather than assume a prior chunking
  plan still applies.
- The real-WAC-crop/hillshade brightness match has an unresolved regression and an unresolved
  validation gap. `hapke._terrain_photometric_angles`'s surface-normal computation and
  `hapke_shade_ortho`'s Hapke-ratio relighting were both made permanent/unconditional on the user's
  explicit call, despite the Hapke-ratio fix being confirmed to *worsen* the one measured
  brightness-matched diff (8.6853 → 9.2425) — not yet explained. Real `campt` ground truth can't
  validate the DEM-aware case (it stays ellipsoid-normal-based even with a DEM shape model
  attached), so ASP `sfs` was used as an independent forward-render cross-check instead
  (`sfs_validation.py`): its Lambertian mode's own independently-recovered incidence angle now
  matches `hapke.real_geometry_photometric_angles` to ~0.0005 deg mean, closing the DEM-aware
  validation gap for incidence — but confirming (not explaining) that the brightness regression and
  three other live visual observations (a real east-brightening gradient the hillshade
  underrepresents; an apparent ~10 deg shadow rotation confirmed *not* a sun-azimuth bug, and not
  explained by missing cast shadows either — at the default candidate's 41.8° sun, `cast_shadow`
  darkens only ~20 of its ~5.8 M ortho pixels;
  anomalously bright real crater floors) remain genuinely open. `sfs` itself has a structural gap
  for phase/emission cross-checks: its reconstructed CSM camera can't represent
  `along_track_correction`. See [`docs/history.md`](../history.md)'s Phase 70-79 entries for the
  full investigation trail.

  The user's own characterization of the live regression, stated directly rather than inferred:
  `reproject` (real calibrated WAC imagery, no relighting) reads brighter than `hillshade` (Hapke-relit
  WAC_EMP) specifically **when phase angle is smaller** — a phase-angle-dependent bias, not a flat
  offset. Checked in a later session (2026-09-10), prompted by a hypothesis that a Sato-et-al.-2014
  parameter naming/definition mismatch in `hapke.fetch_real_hapke_params`'s calibration-cube sampling
  could be the root cause: `_HAPKE_CALIBRATION_PARAM_ORDER`'s band assignment
  (`wh, hg1, hg2, bc0, hc, b0, hh, theta, phi`) matches, position for position, the archived band
  order LROC's own SDR documentation states for this product (`w, b, c, Bc0, hc, Bs0, hs, θ, φ`,
  with bands 4/5/8/9 documented as constant — matching this codebase's own independent observation
  that `bc0`/`hc`/`phi` sample as `0`/`1`/`0` for every entry). ISIS's own `photomet` docs define
  `hg2` exactly as the two-term Henyey-Greenstein mixing weight the sampling assumes, valid range
  0.0–1.0. Separately, `docs/generators/reproject.md` states `reproject`'s own pipeline runs no
  relighting step at all (real calibrated WAC imagery reprojected directly). None of this was
  reconciled with the phase-angle-dependent bias above — it's recorded here as evidence gathered
  against one candidate hypothesis (a scrambled/mislabeled Hapke parameter), not as an explanation
  for the regression itself.
- `hapke.fetch_real_hapke_params` samples ISIS's real calibration cube once per image, at the
  footprint's own center — real spatial variation exists within one footprint (a few percent of
  `wh`/`b0`/`hg1`'s own full-Moon range, somewhat more for `hg2`/`hh`) but is secondary to the
  placeholder-vs-real gap this already fixed. Per-pixel sampling (reprojecting the calibration cube
  onto the same working grid the DEM/ortho use) would be a real further refinement.
- Whether `stretch_reflectance_to_uint8`'s fixed `[0, 0.30]` display stretch saturates is an
  unresolved question. Two distinct sources, neither confirmed absent: (1) `hapke_shade_ortho`'s
  relit reflectance can exceed the max for geometries near opposition (`ratio > 1`); (2)
  `DISPLAY_STRETCH_REFLECTANCE_MAX = 0.30` was confirmed empirically non-saturating for exactly one
  real candidate — not swept across other candidates/geometries (e.g. fresh crater rays,
  near-opposition geometry could plausibly clip). Saturated pixels would bias
  `sfs_validation.true_albedo_map`'s recovered albedo and reduce
  `compute_brightness_matched_diff`'s discriminating power in any clipped region. Resolving this
  needs an actual multi-candidate saturation sweep, not just asserting either combination is fine.
- **Richer report problem flags**: `report.problem_flags` is deliberately narrow today (just low sun
  elevation, from manifest fields alone). Three real checks are still missing, each blocked on the
  same thing — a cheap-enough way to compute them (a persisted value or a lightweight query, not a
  fresh SPICE/camera rebuild or GLD100 fetch per entry per `write_index()` call, which would make
  `problem_flags` no longer cheap/pure-Python): crater-sharpness grading (`crater_depth.py`), a real
  tie-point pixel residual (not computed anywhere today — `tie_points.py` only produces ground-truth
  pixel *locations* for overlay plotting, no image-based comparison), and a footprint-geometry
  outlier check (needs `entry.camera`, not persisted anywhere cheap to re-read — the overview map's
  own FOV polygons no longer pay this cost, see the item just below, but a footprint-outlier check
  specifically would still need real per-entry accuracy, not the approximation now used for the map).
