# Investigation: the `reproject` `TrnTestImage` type, and the synthetic-camera FOV bug found along the way

**Status: implemented and merged to `main`.** The fix lives in `camera.py`'s `solve_corrected_fov`
(see its own docstring/comment for the current algorithm and constants) and
`trn_dataset.TrnTestReprojectImage`. This doc keeps the diagnostic trail and validation data behind
that fix, and the items still open. `old_notebooks/reproject_spike.py`/`.ipynb` (archived) is
superseded exploratory history, not the current implementation.

## What `reproject` is

`crop` (ISIS-processed WAC image) and `hillshade` (synthetic `sat_sim` render, textured from
Lunaserv/Astropedia) are the two implemented `TrnTestImage` types. `reproject` is the third: a
`sat_sim` render through the *same* synthetic camera as `hillshade` (so the two are directly,
pixel-for-pixel comparable), but textured from the WAC crop's own reflectance
(`isis_wac.run_cam2map_for_crop`) instead of the synthetic basemap.

## The bug: synthetic camera FOV didn't fit inside the crop's footprint

First live test (`M1327210646CE`, the demo's default candidate) showed an asymmetric coverage gap:
96.3% valid pixels overall, but the outer 5% edge ring only 79.4% valid, and the corners split
sharply (top 100%, bottom 53-58%).

Root cause, confirmed via direct ray-trace math against the crop's own `campt`-based footprint
corners, had two coupled parts:
1. `camera.build_camera()`'s `fv = fu`: `cross_track_width_km` (what `fu` is built from) is a
   ray-traced ground chord at the pose's actual off-nadir geometry, but the along-track extent
   `fv` should represent came from a flat, non-perspective calculation (`n_frames * km_per_frame`,
   no foreshortening). Using the same angle for both axes calibrates the along-track FOV to a flat
   target but renders it through a perspective (ray-sphere-intersection) model.
2. A second, independent coupling: even after fixing (1) alone, the far corners were *also*
   elongated cross-track, because a corner ray combines both angular offsets at once, and the more
   oblique that combined angle is, the farther out both ground components land. A standard
   4-parameter pinhole (`fu, fv, cu, cv`) can't fully reproduce this — it has no way to make `fu`
   depend on `py`.

**The fix** (see `camera.py`'s `solve_corrected_fov`/`FOV_CROSS_TRACK_SCALE`/
`FOV_ALONG_TRACK_MARGIN` for the current form): shrink the cross-track half-angle by an empirically
tuned factor (0.93), and solve the along-track half-angle/`cv` by ray-tracing the actual corner
(both offsets together, not just the along-track edge midpoint) against the crop's own measured
corner ground truth, with an additional margin shrink (0.93). Both factors are deliberate — per the
user's own call, some fixed shrinkage in exchange for reliable margin against terrain variation is
preferable to chasing an exact geometric fit.

Validated across 4 candidates spanning 38.5°N to -67.5°S, re-using the same constants unchanged:

| product_id | baseline overall | baseline worst corner | fixed overall | fixed worst corner |
|---|---|---|---|---|
| M1327210646CE | 99.2% | 77.1% | 100.0% | 100.0% |
| M1327211014CE | 98.9% | 70.8% | 100.0% | 99.8% |
| M1327211334CE | 97.8% | 58.0% | 100.0% | 100.0% |
| M1327215525CE | 95.5% | 57.8% | 100.0% | 100.0% |

All four reach ~100% with the fixed constants unchanged from image to image — evidence (not proof
at every possible off-nadir angle/latitude) that a single fixed `(FOV_CROSS_TRACK_SCALE,
FOV_ALONG_TRACK_MARGIN)` pair holds up across this manifest/EDR family, not just one image's
overfit tuning.

## The correction lives in `build_camera()`, shared by every product type

Settled by the requirement that `reproject` and `hillshade` stay pixel-grid-identical
(`(fu,fv,cu,cv)` byte-for-byte, for future SSIM/diff-style scoring between them) — only possible if
the correction lives inside `build_camera()` itself, applied once, not a `reproject`-specific
camera variant. This doesn't affect `crop`'s already-validated pose alignment against `hillshade`
(that's about pose, not FOV/size); `crop` stays its own, naturally larger footprint, providing the
margin `reproject`'s render needs.

Three regressions surfaced by re-running the flagship notebook once this correction was wired in,
each already fixed and documented in its own code comment, not repeated here:
`plotting`/`TrnTestHillshadeImage`'s width/height calculation (fixed via
`Camera.render_cross_track_km`/`render_along_track_km`, see `camera.py`), `tie_points.die5_points`'s
bbox-midpoint anchoring (fixed by anchoring on the explicit boresight center instead, see
`tie_points.py`), and `cam_gen`'s CSM conversion silently averaging an asymmetric `fu`/`fv` into one
isotropic field (which motivated reverting to isotropic below, rather than being separately patched
long-term).

## Why the fix ended up isotropic, not anisotropic

An anisotropic (`fu != fv`) version of this fix was built first and validated the same way (100%
coverage across the same 4 candidates, using more of the crop's margin). Converting it to a CSM
Frame model-state JSON (`cam_gen`) cost three bugs along the way: `cam_gen` silently averaging
`fu`/`fv` into the CSM model's single `m_focalLength` field; `tie_points.die5_points`'s anchoring
regression above; and a small, never-fully-explained ~1-8px constant residual between `mapproject
-t csm` and `-t pinhole` reprojections of the same corrected camera, confirmed invariant to how the
anisotropy is encoded across the CSM state's fields (three mathematically-equivalent encodings all
gave the identical residual) — a genuine quirk in compiled `usgscsm`'s handling of an anisotropic
Frame model, not fixable without its source.

The anisotropy was only ever a nice-to-have (more of the crop's margin used, not a correctness
requirement), so given the recurring CSM friction, `camera.solve_corrected_fov` was reverted to
isotropic: solve `fu`/`fv` independently as before, but collapse them to one shared `f = max(fu,
fv)` applied to both axes. Re-validated: 100.0% coverage on all 4 candidates (one improved from
99.83%), at the cost of a ~4-6% smaller cross-track footprint (along-track was already the binding
constraint on all 4, so along-track extent is essentially unaffected); the CSM/Pinhole residual is
now exactly 0px, and `render._correct_csm_focal_length_anisotropy` (the anisotropy-specific
patch) was deleted as dead code.

Reverting to isotropic did shift die5 tie-point placement enough to expose a separate, pre-existing
bug: `campt`'s own ground-to-image solve has a scattered ~38% failure rate for WAC's Pushframe
sensor (a known upstream ISIS bug, unrelated to this FOV change — see
`docs/wac-jigsaw-investigation.md`), which one of the 5 tie points happened to land in after the
footprint shrank. Fixed once `wac_camera_model.find_framelet_and_project` (which sidesteps that bug
entirely) landed and `tie_points.resolve_crop_pixels` switched to it.

## Still open

- **A related but separate architectural point, not acted on**: the existing boresight correction
  (`camera.build_camera()`'s `look_at_rotation` re-aiming) is modeled as a rotation of the whole
  camera frame. This investigation's own FOV fix uses a `cv`/`cu` bias (not a rotation) for a
  different problem (FOV shape) — the same reasoning may apply to the boresight correction itself
  (model it as a `cv` bias, since that's closer to what WAC-VIS's real boresight offset actually
  is), but revisiting that is a separate, bigger change, not started.
- **Dataset-scale validation**: the FOV fix itself has 4-image coverage; `TrnTestReprojectImage`
  end-to-end has only been validated on one entry through the flagship notebook. Validating across
  the rest of the manifest is a separate follow-up.
- **`Camera`'s wider ripple effects were caught reactively** (by re-running the flagship notebook),
  not proactively enumerated — there's no guarantee every consumer of `Camera`'s fields has been
  checked. `grep -rn "cross_track_width_km\|focal_length_px\|principal_point"` across
  `src/trntest/` is a reasonable starting point if something else looks off after this is picked
  back up.

## Key facts for whoever picks this up

- Test entry used throughout: `M1327210646CE` (the demo's default `dataset_manifest.csv` row 0),
  off-nadir 6.24 deg, `n_frames_for_square_crop=70`.
- `entry.camera.camera_along_track_direction_moon_me` (along-track ground direction) and
  `r_cam_to_me[:, 0]` (cross-track ground direction, MOON_ME) are what this investigation used to
  decompose corner ground positions — reusable for future FOV-shape work without re-deriving them.
- `isis_wac.run_cam2map_for_crop`'s `DEFAULTRANGE=camera` auto-sizes its output to the crop's own
  footprint (not padded) — this is what turns an FOV-shape mismatch into `NODATA` pixels in a
  `reproject` render, since `sat_sim` sampling outside that extent gets nothing.
