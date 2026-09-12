# Open items

Genuinely open questions/gaps in `trntest`, pointed to from
[`README.md`](../../README.md)'s "Open items" section. Not a development log — see
[`docs/history.md`](../history.md) for that.

When one of these resolves, delete it — state any fact still needed directly where it's needed,
e.g. a docstring/comment or a `docs/` reference doc, rather than leaving a "Resolved" entry here.

- **`trntest1` entries 201 and 3 (`M1314469291CE` at -60.738°N/145.2327°E, `M1314314993CE` at
  -60.6194°N/168.6734°E) both have a horizontal line artifact in their basemap, traced to a real
  edge-brightening defect in the archived WAC_EMP source tiles themselves, at their shared -60°
  boundary (`WAC_EMP_643NM_E300S1350_304P` equirect, `WAC_EMP_643NM_P900S0000_304P` polar) — not
  nodata or a DEM/elevation issue, and now partially corrected in code, but not fully resolved.**
  `notebooks/wac_emp_seam_investigation.py` confirmed the defect directly in each archived tile's own
  native `.IMG` pixels (no reprojection/mosaicking involved): the equirect tile's own last valid row is
  anomalously bright (+21%/+12% over its interior), and the polar tile has a smaller version of the
  same thing at its own edge (+5.3%, found via full-circle radial binning after a per-longitude probe
  missed it). `notebooks/wac_emp_seam_dem_mosaic.py` cross-checked the mosaicking itself with ASP
  `dem_mosaic` (same seam regardless of precedence) and found a second, separate contributor: the
  polar tile's own coverage boundary is a jagged, locally-diagonal line in the destination grid, not a
  clean cut, so precedence flickers between tiles at scattered columns along the seam row.
  `notebooks/wac_emp_seam_edge_model.py` then profiled each tile's own brightening precisely (mean ±
  SEM vs. signed native-pixel distance from the boundary, ±10px, 0.1px bins): the equirect side is a
  clean single-row spike, but the polar side is a real overshoot-then-undershoot (peak ~10%+ above
  baseline right at the boundary, dipping *below* baseline a couple of pixels in, then a slow
  multi-pixel recovery) — consistent with edge-ringing from the archive's own production pipeline, not
  a simple offset. A 4-parameter damped cosine
  (`baseline + amplitude * exp(-x/tau) * cos(omega*x)`, center pinned to the boundary) fits the
  poleward tail (`x >= 0`) well (chi2/dof ≈ 1.5): `tau` ≈ 1.5px, `omega` ≈ 0.91 rad/px. The very peak
  (`x ≈ -0.5`) falls outside the fit domain — those bins have as few as 10 samples, too sparse to fit.
  **`wac_emp_south_edge_correction.mask_equirect_south_edge_row`/`mask_and_correct_polar_south_edge`
  now apply this correction, called from `ortho_wac_emp._reproject_one_wac_emp_tile_to_array`**
  (native-pixel space, before any resampling; kept in their own module, separate from
  `ortho_wac_emp.py`'s general reprojection machinery, since this is a fix for one specific archived
  defect, not reprojection itself — see that module's own docstring): mask the equirect tile's last row
  outright; on the polar side, mask through `_POLAR_EDGE_MASK_MAX_PX=2` native pixels poleward of the
  boundary (the sub-pixel equatorward overshoot, plus the worst-corrected native pixels the fit domain
  can't see), then subtract the fitted model (leveled to each window's own local 8-13px baseline) over
  the next 8px poleward. Both functions, and the whole correction, are gated behind
  `TrntestConfig.wac_emp_south_edge_correction_enabled` (default `True`) — set `False` to get the raw,
  uncorrected archive data if USGS/ASU ever fix the underlying tiles.
  `notebooks/wac_emp_seam_correction_validation.py` validated this the same way (`dem_mosaic
  --first`/`--count` on the now-corrected per-tile arrays): **the seam's peak row-to-row jump shrinks
  62-65%.** Masking the worst pixels outright (rather than widening or reshaping the model alone) did
  most of the work: an initial 5px-correction-only version shrank the jump just 28-42%, and widening
  the correction zone to 8px barely moved that (29-42%) — but masking through 2px poleward jumped it to
  62-65%, and this time the fraction of the seam row still >2% above local baseline moved with it too
  (42%→33%, 37%→29%, vs. essentially unchanged in the model-only attempts) — real evidence of removed,
  not just dimmed, pixels. Masking a 3rd pixel was tried and found to help one entry only marginally
  and the other not at all, while widening the gap further — 2px is the best trade-off found so far,
  not a point of diminishing-but-still-positive returns. Two known gaps explain what's left: the
  correction zone from `x=2` on still uses a model whose own peak (at `x=0`) undershoots the real,
  unfit peak just before it, and the precedence-flicker effect below is untouched by any radiometric
  correction.
  **The masking opens a small, real coverage gap — now closed.** `merge_local_grid_arrays` itself still
  has no fill step, but `wac_emp_south_edge_correction.fill_nearby_gaps` (a bounded, 6px-radius
  nearest-valid-neighbor fill called right after it in `reproject_wac_emp_reflectance_to_local_grid`,
  only when the correction itself is enabled) closes every gap pixel measured for both entries — up to
  ~0.14% of pixels with the current 2px mask (grew from ~0.03% at 1px, ~0.007% with the original
  correction-only version). The gap blobs can run wide following the seam (up to ~180px) but stay thin
  *across* it (≤6px in every case tested), the dimension that actually matters for a nearest-neighbor
  fill — a genuinely uncovered region would be thick in every direction and is correctly left unfilled;
  worth re-checking this margin if the mask is ever deepened further. Still open: *why* both archived
  products have this edge artifact (USGS/ASU's own tile-production pipeline, out of scope here), and
  the precedence-flicker effect itself (`wac_emp_seam_dem_mosaic.py`'s own finding, untouched by this
  radiometric correction).
  **Generalized to both hemispheres: `wac_emp_edge_correction.py` (renamed from
  `wac_emp_south_edge_correction.py`) now independently fits and corrects the +60° boundary too, not
  just -60°.** `wac_emp_seam_edge_model.py` profiled the matched-lon-zone north tile pair
  (`WAC_EMP_643NM_E300N1350_304P`/`WAC_EMP_643NM_P900N0000_304P`, same 135°E zone as the south tiles)
  the same way: the equirect side shows the identical clean single-row spike (+5.8% vs. south's +7.0%
  by the same immediate-neighbor measure), and the polar side fits the same damped-cosine shape but
  with amplitude ~4x smaller (0.00179 vs. 0.00706) and a slower decay (`tau` 4.05px vs. 1.54px) --
  reusing south's fitted parameters for north would have over-corrected. This wasn't hypothetical:
  `trntest2` (unlike `trntest1`, which has no entry straddling +60° at all) has 23 entries mosaicking
  across +60°, 11 of them showing a >3x elevated row-to-row jump right at that boundary (up to 15.6x
  for `M1309348984CE`) -- confirming the north defect is already live in generated output, not just a
  theoretical concern. `trntest2` is not being regenerated as part of this change.
  **Spot-checking the fix against `M1309348984CE` found a real, previously-unconsidered limitation:
  the correction's visible effectiveness is geometry-dependent, not just hemisphere/longitude-dependent.**
  The archived tile's own defect at that entry's longitude (225°E) is comparable in magnitude to the
  135°E zone the fit came from (+4.2% vs. +5.8%), and the correction *does* measurably change pixel
  values (polar-side max diff 0.0088, similar scale to the fitted amplitude) -- but the masked native
  equirect row maps into only a narrow, diagonal-ish swath of this particular entry's own local-
  orthographic destination grid (1-21 columns of ~2500 per affected row) rather than the full width the
  way it did for south's two validated entries, so the visible artifact barely changes. South's own
  validation (`wac_emp_seam_correction_validation.py`) never surfaced this because it only ever checked
  two entries, both with favorable geometry. Whether this is common or a one-off depends on camera
  viewing geometry relative to the boundary in each entry's own local grid -- unquantified, and a
  natural next step given `trntest2`'s real affected entries are sitting right there for it. Widths
  (`_POLAR_EDGE_MASK_MAX_PX`/`_CORRECTION_MAX_PX`/`_REFERENCE_MAX_PX`) also remain south-tuned only,
  reused as-is for north.

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
  underrepresents; an apparent ~10 deg shadow rotation confirmed *not* a sun-azimuth bug;
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
