# Image-pipeline algorithm: square crop, pose epoch, boresight, comparison figure

How `camera.build_camera()` and the surrounding pipeline pose the synthetic camera and size the crop
so it matches a real WAC swath — architecture detail alongside `../README.md`'s higher-level map.

- **Crop sizing**: the synthetic camera's `fu=fv` is derived directly from WAC's real color-mode
  cross-track FOV — **61.4°**, from the SIS (`spice.getfov` on the WAC-VIS IK returns the wrong,
  monochrome-mode ~91.7° FOV, since color mode only reads the center 704 of the ~1024-wide
  detector). `camera.compute_n_frames_for_square_crop()` then ray-traces the real cross-track
  ground width (chord distance between the ±30.7°-ray ground intersections) and the real per-frame
  ground advance (chord distance between consecutive-frame boresight ground points), and picks
  `n_frames = round(cross_track_width_km / km_per_frame)` so the real CDR crop and the synthetic
  image cover the same real ground area — square in real km, not necessarily square in pixels
  (cross-track and along-track have different native GSD). `build_camera()` then applies a further
  correction to that FOV so the synthetic camera's render fully covers the crop's real footprint —
  see [`reproject-fov-investigation.md`](reproject-fov-investigation.md)'s "The correction lives in
  `build_camera()`, shared by every product type" section for why and how.
- **Pose epoch**: the synthetic camera is posed at the crop's own **temporal midpoint**
  (`center_frame_index = start_frame + n_frames/2`), not its start — so both images are centered on
  the same ground point. `camera.build_camera()` derives `n_frames`/`center_frame_index` together.
  **Boresight direction is re-aimed, not trusted from raw SPICE**: `spice.pxform`'s `[0,0,1]` in the
  `LRO_LROCWAC_VIS` frame is confirmed *not* WAC-VIS's real optical boresight (see "WAC-VIS's real
  boresight isn't `spice.pxform`'s `[0,0,1]`" below) — `build_camera()` instead runs the real WAC
  pipeline and queries ISIS's own camera model (`isis_campt.ground_point_at_pixel`) for the real
  ground point at the crop's true center pixel, then points the boresight there directly
  (`camera.look_at_rotation`). Camera *position* is untouched — confirmed exactly correct.
- **Comparison-figure aspect ratio**: both panels are plotted with `extent=` in real km (not raw
  pixel index), since the CDR crop's pixel array isn't square even though the ground area it covers
  is.
- **Tie points** (`tie_points.py`): 5 points (a die's "5"/X pattern: 4 corners + center) placed in
  the ground area both images *approximately* share (SPICE-only estimate, used only to pick
  plausible candidate points), projected into each image's real pixel coordinates. Synthetic side:
  closed-form pinhole inverse (exact, single fixed pose — `select_tie_points`). Real WAC crop side:
  a genuine ISIS `campt` ground-to-image query (`resolve_crop_pixels`, via
  `isis_campt.resolve_ground_to_image_model`/`ground_to_image_pixel`) against the crop's own real,
  embedded camera model — not the deprecated frame-index-bisection SPICE approximation
  (`project_ground_to_crop_pixel`/`_crop_pixel_at_frame`, kept for reference). Switched after
  confirming live, on this project's real default candidate, that the SPICE approximation disagreed
  with the real camera model by ~92-96px (out of 994 total lines, ~10%, along-track).
  `resolve_ground_to_image_model` tries a CSM ISD sidecar first (`isd_generate`, same tool ASP's
  `mapproject` uses — see `docs/external-tools.md`) and only falls back to the crop's native model
  if the ISD's own `name_model` resolves to a Pushframe sensor — the class `usgscsm`'s
  `groundToImage` is known unreliable for (`docs/external-tools.md`'s `usgscsm` bug section); for
  WAC-VIS this always takes that branch, but the check is real, not hardcoded. A die5 point the real
  camera doesn't actually see (confirmed live: happens for real near-polar candidates, since the
  SPICE-approximate footprint used for point *selection* can be off by enough to pick a point
  outside the real camera's view) is dropped with a warning, not raised — `resolve_crop_pixels` only
  raises if *none* of the 5 points resolve.
- **North-up display rotation** (`orientation.py`, notebook-display-only — never touches the
  sensor model, `.tsai`, or CSM/ISD JSON): picks, per image, the multiple of 90° (no mirroring)
  whose on-screen "up" is closest to true north, via `best_k_for_north_up()` (verified numerically
  against `np.rot90` rather than trusted from hand-derived algebra alone). The synthetic image
  allows all 4 `k∈{0,1,2,3}`; the real crop only `k∈{0,2}` (its row axis is real along-track data —
  a 90°/270° rotation would put cross-track on the vertical axis). The crop's `up_orig` depends on
  `camera.reverse_crop_along_track`, since which end of the mosaic is "forward in time" is
  pass-dependent (see `docs/data-sources/lroc-wac-edr-cdr.md`'s "Pass-dependent sensor axis
  convention").

## WAC-VIS's real boresight isn't `spice.pxform`'s `[0,0,1]`

**The finding**: `camera.camera_pose_moon_me()`'s attitude (`spice.pxform("LRO_LROCWAC_VIS",
"MOON_ME", et)`) is exactly correct — confirmed via a Wahba/Kabsch rotation fit from real `campt`
`LookDirectionCamera`/`LookDirectionBodyFixed` correspondences reproducing it to 0.0000° (including
on a held-out point), and via SPICE position matching ISIS's own real position to 0.6m at the
matching instant. But treating `[0,0,1]` in that frame as "the boresight" is measurably wrong for
WAC-VIS specifically: `LookDirectionCamera` at the naively-assumed center pixel (image cross-track
center, mid-framelet) isn't `[0,0,1]` — off by a roughly constant ~5-6° (5.75° and 5.15° on two very
different real candidates), confirmed to hold across a wide line range with no zero-crossing nearby
(so not a line/timing-selection artifact — bisecting for where the angle crosses zero over a
200-line span just found it drifting slowly from ~0.102 to ~0.095 rad-equivalent, never reaching 0).

**Checked and ruled out as the source, so future work doesn't re-check these**:
- `spice.getfov(-85621)` ("LRO_LROCWAC_VIS") reports boresight exactly `[0, 0, 1]` — the IK itself
  doesn't encode a different nominal boresight.
- WAC-VIS has 5 separate per-filter NAIF frame IDs (`LRO_LROCWAC_VIS_FILTER_1..5`, IDs
  -85631..-85635 — found in `lro_instrumentAddendum_v05.ti`, the real IAK, which this project
  doesn't otherwise furnish; there's also `LRO_LROCWAC_UV_FILTER_1/2`, -85641/-85642). `spice.pxform`
  between any of these and the generic `LRO_LROCWAC_VIS` frame is identity to <0.001° — the
  per-filter frames exist (presumably for cross-track FOV-boundary bookkeeping, given
  `INS-85631_FOV_BOUNDARY_CORNERS`-style keywords) but carry no boresight tilt relative to each
  other or the generic frame.
- The IAK's own `INS-85621_*` entries are `SWAP_OBSERVER_TARGET`/`LIGHTTIME_CORRECTION`/
  `LT_SURFACE_CORRECT`/`CK_FRAME_ID`/`CK_REFERENCE_ID` — processing/frame-chain config, no
  geometric (boresight/distortion) override for -85621 anywhere in it.
- The real ISD's `detector_center` field (`{line: 775.76, sample: 509.54}` for this product) is
  **not** directly usable as an image sample/line coordinate — substituting `sample=509.54` for a
  ground-to-image query made the discrepancy *worse* (32km vs. ~10km), confirming it's expressed in
  some other (raw multi-band detector, pre-windowing) coordinate system this project never fully
  decoded, not the calibrated 704-sample VIS image's own sample axis.

**Why a constant correction rotation doesn't work, despite the "frame-relative constant offset"
signature initially suggesting one would**: a Wahba fit from real correspondences can only ever
recover the rotation that's *actually true* — and that's already proven to equal
`camera_pose_moon_me`'s own SPICE computation. So `correction = R_naive⁻¹ @ R_true` is
`≈ identity` (confirmed live: 0.47° from identity) by mathematical construction, not by bug — no
rotation exists that both matches the proven-correct attitude and changes where `[0,0,1]` points
without being a no-op. The ~5-6° gap is a statement about which pixel is the true optical center
(a principal-point fact), not about the camera's orientation.

**The fix in use** (`camera.build_camera()`): don't derive "where the crop centers" from a boresight
ray at all — run the real ISIS pipeline, query the real ground point at the crop's actual center
pixel via `campt`'s image-to-ground direction (`isis_campt.ground_point_at_pixel`), and re-aim the
synthetic camera's boresight directly at that real point (`camera.look_at_rotation`, Gram-Schmidt
against the original SPICE X axis for roll).
