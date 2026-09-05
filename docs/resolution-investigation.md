# Investigation: `hillshade`/`crop`/`reproject` effective resolution

**Status: resolved.** Written to explain a visually obvious quality ordering in
`notebooks/image_generation.py`'s Phase 5C/6C `plot_zoom_blink_over()` figures: `crop` reads much
sharper than the basemap, which in turn reads sharper than `hillshade`. `config.DEFAULT_IMAGE_SIZE`
(256 → 1316) closes most of that gap — see "The fix" below. All numbers are from the notebook's
current default candidate, `M1327210646CE` — see the Caveat section for how far that generalizes.

## Input source resolution

- DEM (Astropedia GLD100): confirmed 100.0 m/px native pixel size (`docs/data-sources/astropedia-gld100.md`).
- Ortho (WAC_EMP, 304 ppd, 643 nm band): 304 ppd works out to ~99.75 m/px.
- `config.DEFAULT_DEM_TARGET_GSD_M = 100.0` is what `dem_ortho.fetch_dem_and_ortho` samples both onto
  for their shared local working grid — the same grid the notebook shows as "the basemap"
  (`dem_ortho_result.ortho`). It matches the DEM/ortho's own native resolution closely, not
  independently chosen.
- The real WAC crop's native resolution is not fixed — it scales with slant range, since WAC's pixel
  IFOV is fixed in angle (704 samples across a `config.DEFAULT_WAC_VIS_COLOR_FOV_DEG = 61.4°` swath).
  `isis_wac.run_cam2map_for_crop`'s own comment records a directly measured value: without forcing
  `PIXRES=map`, `cam2map` derives the camera's native resolution as **~184 m/px** for these
  candidates — coarser than the "100 m/px" figure `docs/data-sources/lroc-wac-edr-cdr.md` states for
  WAC VIS generally. That figure is presumably a nominal value at one reference altitude; this
  project's manifest is drawn from a broad multi-orbit catalog search, not one fixed altitude, so
  different candidates likely have different native crop GSDs. Not independently confirmed here
  beyond the one measured ~184 m/px data point.

## What each generator renders at

- **`crop`**: nothing in its pipeline resamples the pixel grid — `lrowac2isis` → `spiceinit` →
  `lrowaccal` → `framestitch` only touch radiometry/geometry metadata, and ISIS `crop` only crops
  rows. It's the real sensor's own pixels, at whatever native GSD that candidate's geometry gives
  (~184 m/px measured here). Already at its ceiling; nothing in this codebase throttles it further.
- **basemap**: fetched/reprojected at exactly `dem_target_gsd_m` (100 m/px), matching its own
  DEM/ortho source resolution. For this candidate the local grid is 2387×2440 px over a
  ~238.7×244.0 km AOI — confirmed exactly 100.0 m/px both axes. Also at its ceiling.
- **`hillshade`/`reproject`**: `render.run_sat_sim` passes `sat_sim --image-size <n> <n>` with
  `n = config.image_size` — a **fixed pixel count, independent of the actual footprint size**.
  `camera.solve_corrected_fov` sizes the render's FOV to fit inside the real WAC crop's own
  footprint; for `M1327210646CE` that footprint measures (`Camera.render_cross_track_km`/
  `render_along_track_km`, live-computed) **130.77 × 131.60 km** — a real, fixed physical quantity,
  independent of `image_size` (see "The fix" below for why). At the old `DEFAULT_IMAGE_SIZE = 256`
  this gave an effective GSD of **~511-514 m/px** — 5x coarser than either the 100 m/px DEM/ortho
  inputs or the ~184 m/px real crop.

## The fix

`solve_corrected_fov` solves `cu = image_size / 2.0` and a focal length `f` such that
`(image_size/2)/f` equals a fixed physical half-angle, derived from the real crop's footprint, not
from `image_size`. Scaling `image_size` scales `cu`/`f` together, so **the rendered footprint's own
size is mathematically invariant to `image_size`** — confirmed live: rebuilding the camera at
`image_size=1316` reproduced the exact same 130.77 × 131.60 km footprint `image_size=256` gave.
`image_size` is purely a pixel-density knob on a footprint that's already fixed by the geometry.

That made the fix direct: pick an `image_size` that samples this footprint at ~100 m/px, matching
what the DEM/ortho inputs actually support. `130.77e3/100 ≈ 1308`, `131.60e3/100 ≈ 1316` px per axis
— `DEFAULT_IMAGE_SIZE = 1316` covers both at ≤100 m/px (99.4 m/px cross-track, 100.0 m/px
along-track). Live-tested: `build_camera()` at the new size takes ~1.5s (SPICE/ISIS work is already
cached per-candidate, unaffected by `image_size`) and `run_sat_sim` at 1316×1316 takes ~3.5s —
negligible next to this pipeline's other costs (network fetches, ISIS calibration).

This isn't auto-derived per candidate. The render footprint size varies slightly with each
candidate's own slant range/off-nadir angle, so a truly matched `image_size` would too — deliberately
not chased here; `1316` is a fixed constant picked from one reference candidate, close enough for
every candidate in practice (see Caveat below).

`reproject` shares this same `image_size` with `hillshade` by design — the two are meant to render
through byte-identical camera intrinsics for future pixel-grid comparison — even though `reproject`'s
own real texture source (the WAC crop) caps out coarser, at ~184 m/px (see the ceiling below). That's
a deliberate tradeoff: `reproject` renders finer than its input data actually resolves, in exchange
for staying exactly pixel-aligned with `hillshade`.

Generating an intermediate product at higher resolution and downsampling at the end wasn't the fix
here, and wouldn't have been — there's no separate downsample stage to add. `sat_sim`'s
ray-DEM-intersection render already *is* the resampling from source grid to output grid; asking it
for more output pixels directly was the whole change. The DEM/ortho fetch itself was already sampled
at ~100 m/px — finer than a 256 px render could express — so no upstream product needed to change
resolution.

## The remaining ceiling

`hillshade` still can't exceed the DEM/ortho's own ~100 m/px sampling, and `reproject` still can't
exceed the real crop's own ~184 m/px: its texture is `crop`'s reflectance, reprojected by `cam2map`
(`PIXRES=map`) onto the 100 m/px grid — that upsamples, it doesn't add detail. So expect
`crop` > `reproject` ≈ basemap ≥ `hillshade` to persist, just compressed much closer together than
the old ~5x gap.

Whether GLD100/WAC_EMP's own true per-pixel detail is finer or coarser than their 100 m/px sampling
(stereo-DTM correlation footprint, mosaic compilation from imagery at various altitudes) is
unconfirmed — a genuine data-source-level ceiling this project doesn't control, separate from
`image_size`.

## Caveat

The concrete numbers above (the ~184 m/px crop resolution, the 130.77 × 131.60 km footprint, the
~511-514 m/px old effective GSD) all come from one candidate, `M1327210646CE`. Native crop GSD scales
with orbit altitude/slant range and will differ per candidate in `dataset_manifest.csv` — so will the
render footprint size, meaning `DEFAULT_IMAGE_SIZE = 1316`'s actual achieved GSD on a different
candidate will differ slightly from ~100 m/px, by design (see "The fix" above). Not checked across
the manifest.
