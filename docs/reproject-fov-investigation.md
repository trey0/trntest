# Investigation: the `reproject` `TrnTestImage` type, and a real synthetic-camera FOV bug found along the way

**Status: fix validated across 4 real images, still not merged to `main`.** Lives on branch
`feature/reproject` (`notebooks/reproject_spike.py`/`.ipynb`, uncommitted-to-`main` by design, same
pattern as `feature/alignment`'s `pose_alignment_spike.py` -- see `docs/history.md`'s Phase 52
entry). The fixed `(FU_SCALE=0.93, AT_MARGIN=0.93)` constants, originally tuned on one image, were
re-run unchanged against 3 more real candidates spanning a wide latitude/off-nadir range (38.5°N to
-67.5°S) and reached ~100% valid-pixel coverage on all four -- see "Validated: the fix generalizes"
below. What's left before this is ready to wire into a real `TrnTestReprojectImage` class: the
where-should-the-corrected-FOV-live decision and the separate boresight-bias tangent, both still
open (see "What's NOT done yet").

## What `reproject` is

`crop` (real, ISIS-processed WAC image) and `hillshade` (synthetic `sat_sim` render, textured from
Lunaserv/Astropedia + a synthetic Hapke hillshade) are the two implemented `TrnTestImage` types
(`src/trntest/trn_dataset.py`). `reproject` is the reserved-but-unbuilt third one
(`docs/dataset-plan.md`): still a `sat_sim` render through the *same* synthetic camera as
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

- **Not wired into `camera.build_camera()` or a real `TrnTestReprojectImage` class.** Still ad hoc
  notebook code (`notebooks/reproject_spike.py`) computing a second, `_fovfix`-suffixed `.tsai` and
  camera object alongside the normal one, not integrated into the pipeline. Building the real
  `TrnTestReprojectImage(TrnTestImage)` subclass (per `docs/dataset-plan.md`'s original reservation)
  should reuse this fix, but needs a decision first on where the corrected `(fu, fv, cu, cv)` should
  live -- inside `build_camera()` itself (would change `hillshade`'s and `crop`'s FOV too, not just
  `reproject`'s -- probably *not* desired, since `hillshade`/`crop` don't have this coverage problem)
  vs. a `reproject`-specific camera variant that shares pose/attitude with `entry.camera` but not FOV
  (more likely right, but changes the "identical camera across all three product types" property the
  user explicitly wanted -- worth re-confirming with them once this is revisited, since it's now FOV
  that would differ, not pose/attitude).
- **A related but separate architectural point from the user, not acted on**: the existing boresight
  correction (`camera.build_camera()`'s `look_at_rotation` re-aiming, `docs/data-sources.md`'s
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
