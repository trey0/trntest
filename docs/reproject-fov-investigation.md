# Investigation: the `reproject` `TrnTestImage` type, and a real synthetic-camera FOV bug found along the way

**Status: implemented and live-validated, still on `feature/reproject`, not yet merged to `main`.**
**The FOV fix is now isotropic (`fu == fv` again) -- see "RESOLVED: reverted to an isotropic FOV,
closing the CSM residual investigation" near the end of this doc for why and how, before reading the
anisotropic derivation below, which is now historical (kept for the rationale, not the current
behavior).**

The FOV fix is folded into `camera.build_camera()` itself (`solve_corrected_fov`) and
`TrnTestReprojectImage(TrnTestHillshadeImage)` is a real class in `src/trntest/trn_dataset.py` --
`notebooks/reproject_spike.py`/`.ipynb` (still on this branch, uncommitted-to-`main` by design, same
pattern as `feature/alignment`'s `pose_alignment_spike.py`) is now superseded exploratory history,
not the current implementation; it computes its own separate `_fovfix` camera/tsai (and the now-
superseded anisotropic derivation, not the isotropic `f = max(fu, fv)` version) rather than using
`build_camera()`'s built-in correction, and will not run against current code without updating (not
worth doing -- see "What's NOT done yet"). What's left before merging to `main`: notebook wiring
(nothing currently generates `reproject` by default -- see `trn_dataset.PRODUCT_TYPES`), validation
at full dataset scale (so far: 4 images for the FOV fix itself, 1 image through the real class
end-to-end), and the boresight-bias tangent (still open, separate, not started). The CSM residual is
resolved -- not by fixing it, but by reverting to an isotropic FOV that makes it moot; see "RESOLVED:
reverted to an isotropic FOV" below.

## What `reproject` is

`crop` (real, ISIS-processed WAC image) and `hillshade` (synthetic `sat_sim` render, textured from
Lunaserv/Astropedia + a synthetic Hapke hillshade) are the two implemented `TrnTestImage` types
(`src/trntest/trn_dataset.py`). `reproject` is the reserved-but-unbuilt third one: still a `sat_sim`
render through the *same* synthetic camera as
`hillshade` (so the two are directly, pixel-for-pixel comparable), but textured from the real WAC
crop's own reflectance (via `isis_wac.run_cam2map_for_crop`, already used for the crop/hillshade
overlay comparison) instead of a synthetic basemap. The user's framing: "use `sat_sim` but for input
data use the RDR of our WAC crop essentially."

## The real bug found: synthetic camera FOV doesn't fit inside the real crop's footprint

First live test (one real image, `M1327210646CE`, the demo's own default candidate) showed a real,
asymmetric coverage gap: overall 96.3% valid pixels, but the outer 5% edge ring only 79.4% valid,
and the four corners split sharply -- top corners 100% valid, bottom corners 53-58% valid. Per-row
profile: row 0 (top) 99.2% valid, tapering to 0% by row 251 of 256.

**Root cause, in two coupled parts** (both confirmed via direct ray-trace math against the real
crop's own ISIS `campt`-based footprint corners, `entry.crop_footprint`):

1. **`camera.build_camera()`'s `fv = fu`** (`src/trntest/camera.py`, `footprint_lonlat` call):
   `cross_track_width_km` (what `fu` is built from) is a real ray-traced ground chord at the pose's
   actual off-nadir geometry (6.24 deg for this candidate). But `n_frames_for_square_crop` (what the
   real crop's along-track extent -- and so what `fv` *should* represent) comes from a flat,
   non-perspective calculation: `n_frames * km_per_frame`, a simple accumulated ground-track step
   with no foreshortening in it at all. Using the same angle for both axes calibrates the along-track
   FOV to a flat-distance target but renders it through a real perspective (ray-sphere-intersection)
   model -- confirmed live: target 147.94 km, actual (with `fv=fu`) 152.14 km.
2. **A second, independent coupling effect**, found only after fixing (1) alone barely moved the
   corners: decomposing each corner into real cross-track/along-track ground components (via
   `entry.camera.camera_along_track_direction_moon_me` for along-track, `r_cam_to_me[:, 0]` for
   cross-track) shows the far corners are *also* elongated cross-track (~81-82 km vs. the crop's own,
   remarkably constant ~70 km on both near and far ends) -- even though `fu`/`cu` never change. A
   corner ray combines both angular offsets at once, and the more oblique that combined angle is, the
   farther out *both* ground components land, not just the axis being actively solved for. **A
   standard 4-parameter pinhole (`fu, fv, cu, cv`) cannot fully reproduce this** -- it has no way to
   make `fu` itself depend on `py`.

## The fix that worked (on this one image)

`notebooks/reproject_spike.py`'s final cells (search for `FU_SCALE`/`AT_MARGIN`):

1. Shrink `fu` by `FU_SCALE` (0.93 in the tested run) -- closes the far-corner cross-track excess.
2. Solve `fv`/`cv` independently by ray-tracing the actual *corner* (both cross-track and
   along-track offsets together, matching `camera.pixel_ray_cam`'s exact ray formula -- not just the
   along-track edge midpoint at `px=cu`, which was tried first and barely helped, since a corner's
   ground position isn't a simple function of its along-track offset alone) against the real crop's
   own measured near/far corner ground truth (`entry.crop_footprint`, not the flat
   `n_frames * km_per_frame` approximation), with an additional `AT_MARGIN` (0.93) shrink.
3. Both shrink factors are deliberate, per the user's own explicit call: "we can accept a bit of
   arbitrary shrinkage on the frame sensor FOV if that's what it takes to solve the problem
   reliably... there is some variation due to terrain and we would want to build in a bit of margin
   in any case." Not something to solve away with a more exact model.

Result on `M1327210646CE`: valid pixels 96.3% -> **100.0%**, worst corner 53.6% -> **100.0%**.
`fu: 215.58 -> 235.25` (`cu` unchanged, 128.0), `fv: 215.58 -> 249.40`, `cv: 128.0 -> 133.26`.

## Validated: the fix generalizes across 4 real images

`notebooks/reproject_spike.py`'s later cells (`evaluate_reproject_coverage`, search for
"Validating the fix across more images") re-ran the *same* `(FU_SCALE=0.93, AT_MARGIN=0.93)`
constants -- unchanged, not retuned -- against 3 more real candidates already available in the
`trn_dataset` folder (crop+hillshade already generated from the accidental `populate(limit=1)`
advance documented below), spanning a wide latitude/off-nadir range: `M1327211014CE` (55.4°N),
`M1327211334CE` (70.7°N), `M1327215525CE` (-67.5°S), against the original `M1327210646CE`
(38.5°N). "Baseline" here means the corner-ray `fv`/`cv` solve *without* the `FU_SCALE`/`AT_MARGIN`
shrink (i.e. `fu_scale=1.0, at_margin=1.0` -- already better than the fully-uncorrected `fv=fu`
starting point earlier in this doc, so these baseline numbers are higher than the 96.3%/53.6% above):

| product_id | baseline overall | baseline worst corner | fixed overall | fixed worst corner |
|---|---|---|---|---|
| M1327210646CE | 99.2% | 77.1% | 100.0% | 100.0% |
| M1327211014CE | 98.9% | 70.8% | 100.0% | 99.8% |
| M1327211334CE | 97.8% | 58.0% | 100.0% | 100.0% |
| M1327215525CE | 95.5% | 57.8% | 100.0% | 100.0% |

All four reach ~100% (worst case 99.8%, on the corner-ray solve alone -- negligible) with the fixed
constants, unchanged from image to image. This resolves the "per-image solve or fixed constant?"
question below: **a single fixed `(FU_SCALE, AT_MARGIN)` pair holds up** across this range, at least
for candidates from the same manifest/EDR family this demo already uses -- no evidence yet that it
needs to vary per-image. Not proof it holds at every possible off-nadir angle/latitude in general
(all 4 are still non-polar, WAC-VIS, similar `n_frames_for_square_crop`), but a real, meaningful
result: this isn't one image's overfit tuning.

## What's NOT done yet

- **Resolved: wired into `camera.build_camera()` and a real `TrnTestReprojectImage` class.** The
  where-should-the-corrected-FOV-live question was settled by the user's own requirement: `reproject`
  and `hillshade` should stay pixel-grid-identical (`(fu,fv,cu,cv)` byte-for-byte), since a future
  goal is SSIM/LPIPS/diff-style scoring between them, which needs a shared pixel grid -- only
  possible if the correction lives inside `build_camera()` itself, applied once, not a
  `reproject`-specific camera variant. The earlier worry that this would degrade `crop`'s already-
  validated alignment against `hillshade` turned out not to apply: that alignment is about *pose*
  (`look_at_rotation`'s target, "0.000km residual"), not FOV/size -- `crop` and `hillshade` were
  never required to be the same size, only correctly co-located, so shrinking `hillshade`'s FOV
  doesn't touch it. `crop` (real source data) stays its own, naturally larger real footprint,
  providing the margin `reproject`'s render needs -- exactly the shape the user described: "crop
  needs to stay a bit larger since it's serving as source data and there needs to be some margin."
  `TrnTestReprojectImage(TrnTestHillshadeImage)` needed only 4 overrides (`raster_path`/
  `sidecar_json_path`/`render_label`/`_generate_impl` -- feeding a WAC-crop-textured `DemOrthoResult`
  into the same `render.run_sat_sim` call `hillshade` uses); `_mapprojected_path` and the
  `width_km`/`height_km`/`footprint_lonlat_deg`/`rotation_k`/`tie_point_px_key` properties are all
  inherited unchanged. Live-validated end to end (private scratch dataset, not the shared demo
  folder): `hillshade`/`reproject` confirmed byte-identical `width_km`/`height_km`, both 100% valid
  coverage, `plot_vs_basemap`/`plot_overlay` both produce correct, well-aligned figures.
- **Resolved along the way: `footprint_lonlat`'s `"center"` entry was hardcoded to the geometric
  image-center pixel** (`size/2, size/2`), not the boresight ray `(cu, cv)` -- harmless before this
  fix (the two were always equal, `cu=cv=size/2` everywhere), but wrong once the principal point can
  be offset. Every consumer of `footprint_lonlat_deg["center"]` (AOI centering in `lunaserv.py`, sun-
  angle lookups, `orientation.py`'s display rotation) wants the real pose target, not the literal
  image-center pixel. Fixed; confirmed live the corrected `"center"` now matches the real crop's own
  `campt`-derived center exactly (0.0 deg delta, both lon and lat).
- **Resolved along the way, a real regression caught by re-running the flagship demo notebook**:
  `plotting.plot_isis_comparison` and `TrnTestHillshadeImage.width_km`/`height_km` both reused
  `Camera.cross_track_width_km` (crop-window-derived) as a stand-in for the *synthetic render's own*
  real width/height and assumed the render was exactly square -- both true before this fix (`fu=fv`,
  derived from the same half-angle the crop window used), false after. Added
  `Camera.render_cross_track_km`/`render_along_track_km` (`camera.footprint_width_height_km`, a real
  ground-chord measurement of the actual corrected footprint) and switched both consumers to use
  them; `cross_track_width_km` itself is untouched, still correctly describing the real crop's own
  extent (`TrnTestCropImage.width_km`, `pose_alignment.py`'s crop GSD calc) -- unaffected by a
  synthetic-camera-only fix.
- **Resolved along the way, a second real regression found by re-running the flagship notebook**:
  `tie_points.select_tie_points`'s 5 QA-overlay tie points dropped from 5-of-5 resolving (the demo's
  own documented default-candidate result) to 1-of-5 once the FOV fix was wired in. Root cause:
  `tie_points.die5_points` anchored its points on the shared bbox's own naive
  `(lon_min+lon_max)/2, (lat_min+lat_max)/2` midpoint, not the true shared boresight center -- fine
  while the synthetic footprint was symmetric around its own center (midpoint == true center by
  construction), wrong once `solve_corrected_fov` made it asymmetric (near corners ~91k m from
  center, far corners ~100k m) enough to shift the naive midpoint measurably off the true center --
  confirmed live: even the "center" test point itself landed outside the real crop's pushframe FOV
  ("no surface intersection"). Fixed by anchoring `die5_points` on an explicit `center` argument
  (`select_tie_points`'s already-computed `synthetic_center`) instead, with each of the 4 corner
  points scaled by its own reach from `center` to its own side of the bbox. Live-validated: 5 of 5
  tie points resolve again on `M1327210646CE`, and the "center" tie point's real crop pixel now lands
  within ~2px of the crop's own true center pixel.
- **Resolved along the way, a third real regression -- this one caught only by the user's own direct
  visual inspection in Jupyter Lab, not by anything automated**: Phase 5B's blink overlay (`entry.
  hillshade.plot_overlay()`, the true pixel-for-pixel `mapproject`-based geometry check, previously
  "always very accurately aligned" per the user) came out visibly misaligned once the FOV fix was
  wired in. Root cause: `TrnTestHillshadeImage._mapprojected_path()` reprojects via ASP `mapproject
  -t csm`, using a CSM Frame model-state JSON `cam_gen` converts from our own `.tsai` -- but the CSM
  Frame sensor model (`USGS_ASTRO_FRAME_SENSOR_MODEL`) has only one, *isotropic* `m_focalLength`
  field, no separate fu/fv. Confirmed live: `cam_gen` silently averages an asymmetric `fu`/`fv` into
  that one field (`(235.25+249.40)/2 = 242.32`, matching the JSON's actual value to 10 significant
  figures) -- harmless while `fu=fv` always held (lossless average of two equal numbers), a real,
  measurable ~5% one-axis distortion once `solve_corrected_fov` made them differ. Quantified directly:
  the CSM-reprojected footprint's own bounding box came out nearly square (143.1x142.6 km, ratio
  1.0035) while the correct one (computed via `mapproject -t pinhole`, using the `.tsai` directly --
  ASP's Pinhole model has no such isotropy limitation) is properly non-square (146.0x139.1 km, ratio
  1.049), matching `Camera.render_cross_track_km`/`render_along_track_km`'s own real ~1.05 ratio.
  First fix attempt: generalized `render.run_mapproject_image` (`camera_path`/`camera_type` instead
  of a hardcoded CSM sidecar) and switched `TrnTestHillshadeImage._mapprojected_path` to
  `camera_type="pinhole"` against `entry.camera.tsai_path` directly, bypassing the lossy CSM sidecar
  entirely -- worked, but the user asked a sharper question: is this a fundamental CSM limitation, or
  just `cam_gen`'s own conversion being lossy (with a hopeful eye toward keeping CSM, since a correct
  standalone ISD sidecar matters for `docs/plan.md`'s still-open "acceptable stand-in for a literal
  ISD file" question)? Investigated properly rather than assuming: `cam_gen --help` only exposes a
  single `--focal-length`/`--pixel-pitch` (no per-axis flags) -- but `ale`'s own real-instrument CSM
  formatters (`ale/drivers/lro_drivers.py`, installed in this image) populate the model's
  `m_iTransL`/`m_iTransS`/`m_transX`/`m_transY` fields directly from NAIF's real, genuinely
  anisotropic `INS<id>_ITRANSL`/`ITRANSS`/`TRANSX`/`TRANSY` instrument-kernel keywords for actual
  flight cameras -- proving the CSM Frame model itself fully supports per-axis anisotropy via those
  fields, entirely independent of the single `m_focalLength`. Confirmed empirically by hand-patching
  a `cam_gen`-produced sidecar (pivoting `m_focalLength` to `fu`, rescaling `m_iTransL`/`m_transY` by
  `fv/fu`/`fu/fv`) and re-running `mapproject -t csm`: the reprojected footprint came out 146.3x139.2
  km, matching the correct `-t pinhole` result (146.0x139.1 km) to ~0.2% -- vs. the original broken
  CSM output's 143.1x142.6 km. So `cam_gen`'s conversion is the actual bug, not the model.

  **Final fix**: `render._correct_csm_focal_length_anisotropy` restores the sidecar itself, called
  right after `cam_gen` in `run_sat_sim` -- pivots `m_focalLength` to `fu`, rescales whichever of
  `m_iTransL`'s two coefficients `cam_gen` set nonzero by `fv/fu` (and `m_transY`'s matching
  coefficient by the reciprocal), preserving sign rather than assuming a fixed index/sign convention.
  `TrnTestHillshadeImage._mapprojected_path` (and `TrnTestReprojectImage`, inherited) reverted back to
  the CSM sidecar (`camera_type="csm"`, the default) now that it's correct at the source --
  `run_mapproject_image` stayed generalized (`camera_path`/`camera_type`) as good hygiene, even
  though its one live caller no longer needs `"pinhole"`. Live-validated across all 4 candidates used
  throughout this investigation: the auto-corrected CSM sidecar's own `mapproject -t csm` footprint
  matches the from-the-`.tsai` `-t pinhole` ground truth to within 0.00-0.27% on each, confirming the
  fix generalizes, not just for one hand-tuned case. `TrnTestCropImage`'s own `_mapprojected_path`
  (ISIS `cam2map`, not ASP `mapproject`) was never affected by any of this.

  **Notable**: none of this session's own automated checks (pytest, the FOV coverage validation, even
  a direct visual check of `TrnTestReprojectImage`'s own overlay output) caught the original
  regression -- only the user's own side-by-side comparison against their memory of the *previous*
  (always-correct) Phase 5B alignment did. A real reminder that "didn't crash and looked plausible in
  isolation" isn't the same bar as "matches a previously-established, known-good baseline." And the
  user's own follow-up question -- "is this really a CSM limitation, or just cam_gen?" -- led to a
  better, more foundational fix (the sidecar itself is now correct for any future consumer, not just
  the one call site that happened to need it) than the first, narrower workaround would have been.

  **OPEN: an unexplained small residual remains in the CSM mapproject path -- picked up here next.**
  The user pushed further on the "matched to within ~0.27%" claim above ("that sounds high") -- right
  to, since it turned out to be masking something. Chased it live, in order:
  1. **Control test**: re-ran the identical `mapproject -t pinhole` command twice on the same camera
     and diffed the outputs -- bit-for-bit identical (`std=0.0000`), confirming `mapproject` itself is
     fully deterministic with zero inherent noise floor. Any disagreement between `-t csm` and
     `-t pinhole` is therefore real, not implementation jitter.
  2. **Precise point-placement check** (exact known ground points -- the camera's own footprint
     center + 4 corners, real `campt`/ray-sphere ground truth -- converted to pixel row/col via each
     output raster's own `rasterio` transform, not texture cross-correlation, which turned out to be
     too noisy/ambiguous to trust -- an earlier FFT-correlation attempt gave inconsistent, seemingly
     patch-location-dependent offsets that didn't survive this more direct check): on
     `M1327210646CE`'s real corrected camera (`fu=235.25, fv=249.40`), CSM and pinhole disagree by a
     **constant** offset at all 5 points -- (row -1, col +8) px, not growing toward the edges. A
     constant (not distance-scaling) offset rules out a residual anisotropy/scale error -- it's a
     positional bias instead.
  3. **Symmetric-camera control** (`fu=fv=235.25`, `cu=cv=128.0` -- reconstructed via
     `camera.footprint_lonlat`/`write_tsai` directly, matching how this project's camera always
     worked before this session): CSM and pinhole agree **exactly**, 0px at all 5 points. This proves
     the residual is specific to the asymmetric-FOV correction, not a pre-existing CSM-vs-Pinhole
     implementation quirk that was always there and just never noticed.
  4. **Raw (uncorrected) vs. corrected CSM, both vs. pinhole**, same 5 points: raw `cam_gen` output
     disagreed with pinhole by (row -16, col +9); the corrected version disagrees by (row +1, col -8).
     The row axis (the one `_correct_csm_focal_length_anisotropy` actually rescales, via `m_iTransL`/
     `m_transY`) improved by ~16x. **The confusing part**: the column axis (`m_iTransS`/`m_transX`,
     never touched by the correction) also *changed* between raw and corrected -- expected, since
     changing `m_focalLength` (which the correction does pivot, from the averaged value to `fu`)
     feeds into `xf` for both axes before the per-axis transform applies -- but per the model derived
     for this fix, pivoting to `fu` with `m_iTransS`'s coefficient left at `1` *should* make the
     column axis land exactly on `fu * x/z`, matching pinhole exactly (0 residual) -- it doesn't (-8px
     residual instead). So there's a real gap between the derived model and the CSM Frame model's
     actual internal math that this session did not close, without `usgscsm`'s own source (only the
     compiled `.so` was available in this image -- `find / -iname '*usgscsm*'` for what's on disk).

  **Numbers to reproduce this immediately, without redoing the diagnostic work**: entry 0
  (`M1327210646CE`), `fu=235.24707571465046, fv=249.40000663655198`. Compare `mapproject -t csm`
  (using `render_result.csm_json`, i.e. after `_correct_csm_focal_length_anisotropy` already ran)
  against `mapproject -t pinhole` (using `camera.tsai_path` directly) for the same rendered image;
  convert `camera.footprint_lonlat_deg`'s 5 points to each output's own local Orthographic meters
  (`lunaserv.orthographic_xy_m`) and look up `rasterio` `.index()` in each raster -- should reproduce
  (row -1, col +8) (pinhole minus csm, or thereabouts -- sign convention wasn't triple-checked, verify
  fresh). **Net assessment**: not fully closed, but the residual (~1-8px out of a ~1400-1460px image,
  <0.6% of a ~140-160km footprint, constant not growing) is far smaller than the original bug (~5%,
  growing toward the edges) -- the user was mid-decision between accepting this, reverting the live
  `mapproject` call back to `camera_type="pinhole"` (proven exact throughout this whole
  investigation, zero residual, but leaves the exported CSM sidecar itself imperfect for other future
  consumers), or continuing to chase the exact cause, when the session ended for token-budget reasons.
  **Left as-is (CSM path, `camera_type="csm"` default) rather than reverted** -- it's a real,
  substantial, live-validated improvement over the original bug either way; reverting without being
  asked would have been a unilateral call on an open question. Whoever picks this up should re-read
  this section, decide, and either accept/document or continue the debugging trail above.

  **RESOLVED: reverted to an isotropic FOV, closing this investigation instead of continuing to chase
  the residual.** Picked back up in a later session, docker rebuilt, and the residual reproduced
  exactly as documented above (constant `(row -1, col +8)` at all 5 points on `M1327210646CE`). Before
  deciding accept-vs-revert, ran one more diagnostic round: three ways of encoding the *same* `fu`/`fv`
  anisotropy into the CSM state (pivot `m_focalLength` to `fu` -- the shipped correction; pivot to `fv`
  instead, rescaling the sample axis; leave `m_focalLength` at `cam_gen`'s original average and scale
  *both* `iTrans` fields relative to it) -- reconstructing `cam_gen`'s pristine pre-correction output
  each time rather than chaining corrections on top of an already-corrected sidecar (an early version
  of this check had that bug, giving misleading "it changes by encoding" results that didn't survive a
  clean re-run). All three gave the **identical** residual, `(row -1, col +8)`, at every point --
  ruling out "we're encoding the correction wrong" definitively (a mathematically-equivalent
  restatement of the same `fu`/`fv` can't itself be a bug), and confirming this is a genuine quirk in
  compiled `usgscsm`'s handling of an anisotropic Frame model, not fixable from our side without its
  source.

  That, combined with the user's own reassessment of the anisotropy's value -- it was only ever a
  nice-to-have (more of the real crop's margin used, not a correctness requirement), and the pain of
  chasing three real bugs from it (this residual, the `m_focalLength` collapse, and the `die5_points`
  anchoring regression) raised the concern that other downstream CSM/ISIS consumers might hit the same
  kind of friction -- led to reverting `camera.solve_corrected_fov` to isotropic instead: solve `fu`
  (cross-track) and `fv`/`cv` (along-track) exactly as before, but collapse them to one shared
  `f = max(fu, fv)` applied to both axes (re-deriving `cv` against this shared `f`), rather than
  keeping them separate. Checked empirically before committing to it (see `docs/plan.md`'s "reproject"
  entry): across the same 4 real candidates the anisotropic fix was validated on, the isotropic version
  reaches **100.0% coverage on every one** (actually improving `M1327211014CE`'s 99.83% worst-corner
  under the anisotropic fix to 100%), at the cost of a ~4-6% smaller cross-track footprint (along-track
  was already the binding constraint -- `fv > fu` -- on all 4, so along-track extent is essentially
  untouched). `render._correct_csm_focal_length_anisotropy` is deleted outright (dead code once
  `fu == fv` always -- it was already a no-op in that case), and re-running the flagship
  `image_generation.ipynb` end to end confirms `mapproject -t csm` and `-t pinhole` now agree exactly
  (0px at all 5 points, re-verified live) and the real WAC-crop reproject coverage check still hits
  100%. One real, initially-accepted side effect, since fixed (see below): `M1327210646CE`'s smaller
  footprint dropped 1 of 5 QA tie points (`top_right`) that resolved under the anisotropic fix's
  larger footprint. All 210 tests pass, lint clean.

  **The `top_right` drop above turned out to be a different, pre-existing bug, not a footprint-size
  regression -- now fixed.** Investigated live rather than assumed: the actual ISIS error for that
  point was "no surface intersection" (via `campt`, called by `isis_wac.ground_to_image_pixel`), not
  "not inside cube" -- ruling out the crop-edge numerical instability this doc's `_CROP_EDGE_MARGIN_PX`
  section describes (that mechanism gives the latter error, not the former). Traced to a1's
  `docs/wac-jigsaw-investigation.md` finding on `feature/alignment`: `campt`'s own ground-to-image
  solve for WAC's Pushframe sensor has a real, *scattered* (~38% on this same default candidate, no
  edge concentration -- a1 measured resolved-vs-dropped edge-distance directly and found no
  significant difference) failure rate, a known upstream ISIS bug
  (`PushFrameCameraGroundMap::GetLocalNormal`, DOI-USGS/ISIS3#4256) unrelated to the isotropic FOV
  change entirely -- the revert just moved `top_right`'s die5 position enough to land in that
  pre-existing scattered failure mode where no point had before. Fixed once a1's
  `wac_camera_model.find_framelet_and_project` (a from-scratch WAC-VIS camera model reimplementation,
  validated to exact agreement with real `campt`, whose own containment check sidesteps the bug
  entirely) landed on `main` -- `tie_points.resolve_crop_pixels` now calls it instead of
  `isis_wac.ground_to_image_pixel`. Live-validated: all 5 die5 points resolve again on
  `M1327210646CE`. See `docs/wac-jigsaw-investigation.md` for the full bug investigation.
- **A related but separate architectural point from the user, not acted on**: the existing boresight
  correction (`camera.build_camera()`'s `look_at_rotation` re-aiming, `docs/image-pipeline.md`'s
  "WAC-VIS's real boresight isn't `spice.pxform`'s `[0,0,1]`") was modeled as a *rotation* of the
  whole camera frame. The user's own observation, prompted by this investigation: "viewing the
  boresight correction as a rotation was not quite right. It was always going to be more correct to
  model it as a bias in `cv`, since that's what it is in the real WAC VIS." This investigation's own
  fix uses a `cv`/`cu` bias (not a rotation) for a *different* problem (FOV shape), which the user
  confirmed validates that framing. Revisiting the *original* boresight correction to use a `cv` bias
  instead of `look_at_rotation` is a separate, bigger change -- not started, worth its own session.
- **A real process bug found and fixed along the way, already committed on this branch**: the
  notebook's early cells called `dataset.populate(limit=1)` before grabbing `entry = dataset[0]`.
  Entry 0 already had `crop`+`hillshade` generated (from an earlier `image_generation.ipynb` run), so
  `populate(limit=1)` silently moved on and did real, unintended work on 3 *other* manifest entries
  instead (confirmed via `dataset.status()`; each triggered a real fresh Lunaserv/Astropedia DEM/
  ortho fetch and crop/hillshade generation for an unrelated product). Fixed by removing the
  `populate()` call entirely, since entry 0 never needed it -- worth remembering as a general trap:
  `populate(limit=N)` on an already-populated entry advances the queue, it doesn't no-op.
- **Resolved: `reproject` is now wired into `image_generation.ipynb`** (Phase 8, mirroring Phase 5's
  A/B geometry checks -- raw quality vs. the basemap, then a `mapproject` overlay -- plus a
  valid-pixel-fraction print as the direct answer to this investigation's own coverage question).
  Phase 2's `dataset.truncate`/`populate` calls now pass `product_types=("crop", "hillshade",
  "reproject")` explicitly to include it -- still opt-in, `trn_dataset.PRODUCT_TYPES` itself is
  unchanged (just `crop`+`hillshade`). **Still open: only validated on this one entry through the
  real `TrnTestReprojectImage` class and the flagship notebook** (the FOV fix itself has 4-image
  coverage; the class/notebook wrapping it doesn't yet) -- dataset-scale validation across the rest
  of the manifest is a separate follow-up, not done here. `notebooks/reproject_spike.py` itself is
  now stale relative to `build_camera()`'s built-in correction (still computes its own separate
  `_fovfix` camera/tsai) -- not worth updating, since it already did its job (finding and validating
  the fix); the real implementation lives in `src/trntest/camera.py`/`trn_dataset.py` now, this
  notebook is exploratory history.
- **Still open: the FOV-corrected `Camera`'s wider ripple effects were caught reactively (via
  re-running `image_generation.ipynb`), not proactively enumerated** -- two real regressions were
  found and fixed this way (`plot_isis_comparison`/`width_km`/`height_km`, `die5_points`'s
  anchoring), but there's no guarantee every consumer of `Camera`'s fields has been checked;
  `grep -rn "cross_track_width_km\|focal_length_px\|principal_point"` across `src/trntest/` is a
  reasonable starting point if something else looks off after this branch is picked back up again.

## Key facts for whoever picks this up

- Test entry used throughout: `M1327210646CE` (the demo's own default `dataset_manifest.csv` row 0),
  off-nadir 6.24 deg, `n_frames_for_square_crop=70`, `boresight_rotation_k=3`,
  `reverse_crop_along_track=True`.
- `entry.camera.camera_along_track_direction_moon_me` is the real, SPICE-measured along-track ground
  direction (already computed, already on `Camera` -- see its own docstring); `r_cam_to_me[:, 0]` is
  the real cross-track ground direction (the camera's own x-axis, mapped into `MOON_ME`). Both were
  used directly for decomposing corner ground positions in this investigation -- reusable for future
  FOV-shape work without re-deriving them.
- `entry.crop_footprint` (`tie_points.crop_footprint_corners_for_camera`, real ISIS `campt`) is the
  ground-truth source for the real crop's own footprint -- confirmed exactly centered on the same
  point as `entry.camera.footprint_lonlat_deg["center"]` (the boresight re-aiming fix already
  guarantees this), so the only remaining mismatch is shape, not position.
- `isis_wac.run_cam2map_for_crop`'s `DEFAULTRANGE=camera` auto-sizes its output to the crop's own
  real footprint (not padded) -- this is what turns an FOV-shape mismatch into actual `NODATA`
  pixels in a `reproject` render, since `sat_sim` samples outside that extent gets nothing.
