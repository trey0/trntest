# Open items

Genuinely open questions/gaps in `trntest`, pointed to from
[`README.md`](../../README.md)'s "Open items" section. Not a development log — see
[`docs/history.md`](../history.md) for that.

When one of these resolves, delete it — state any fact still needed directly where it's needed,
e.g. a docstring/comment or a `docs/` reference doc, rather than leaving a "Resolved" entry here.

- **`trntest1` entries 201 and 3 (`M1314469291CE` at -60.738°N/145.2327°E, `M1314314993CE` at
  -60.6194°N/168.6734°E) both have a horizontal line artifact in their basemap**
  (`entry.dem_ortho_result.ortho`, the WAC_EMP-derived texture `hillshade` relights — both reported
  by the user browsing `output/trntest1/reports/`; not yet independently confirmed against the
  actual ortho file itself, only the gallery's own separate WMS-backdrop thumbnail, which didn't show
  it at that resolution/data source). Both entries sit right at the ±60° equirect/polar boundary
  `ortho_wac_emp.reproject_wac_emp_reflectance_to_local_grid` mosaics WAC_EMP tiles across (see
  `docs/data-sources/wac-emp-pds4.md`'s "Multi-tile mosaic" section) — and `M1314314993CE`
  specifically is already independently documented (an earlier trial run) as having mosaicked two
  real WAC_EMP tiles there (`WAC_EMP_643NM_P900S0000_304P` + `WAC_EMP_643NM_E300S1350_304P`) and
  "succeeded" only in the sense of not raising — no one looked at the resulting pixels at the time.
  Two independent entries at the same boundary showing the same artifact is stronger than
  coincidence; a stitching seam along a constant-latitude line at that exact boundary is now the
  leading (still unconfirmed) hypothesis for the *horizontal* line specifically.

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
