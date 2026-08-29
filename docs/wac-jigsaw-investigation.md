# Projection-aware (3D) camera-pose alignment: `jigsaw` investigation and the hand-rolled fallback

Status snapshot as of 2026-08-17, written to preserve context across a session boundary. This
continues `feature/alignment`'s 2D tie-point work (`src/trntest/pose_alignment.py`, merged to
`main`) toward the real goal: a proper 3D camera-pose bundle adjustment, using real tie points
(SIFT/LightGlue matches against the basemap) instead of a 2D map-space homography. See
`docs/plan.md`'s "camera-pose alignment" open item for the higher-level pointer and
`docs/history.md`'s Phases 52-55 for how the 2D work that precedes this was reached.

## The plan, in one paragraph

Convert matched tie points into real 3D ground control (`src/trntest/control_network.py`, done,
tested, working) and fit a 6-DOF camera pose correction (3 position + 3 attitude, frozen/degree-0)
against them, using each point's real reprojection error as the residual. The natural tool for this
is ISIS's own `jigsaw` bundle adjuster -- but it has a real, confirmed bug for this instrument (see
below), so the fallback is a hand-rolled Python forward-projection + `scipy.optimize.least_squares`
fit, reusing `camera.py`'s already-validated SPICE machinery. That fallback's core piece (the
optics chain) is now built and validated to exact agreement with real ISIS output. What's left is
the framelet *search* (see "Remaining work" below), then the actual optimizer and a real fit against
the basemap-derived tie points.

## Part 1: `control_network.py` -- done, real, merged-worthy

`src/trntest/control_network.py` (new this session, not yet committed -- see "Repo state" below)
converts `pose_alignment`'s matched map-space tie points into real ISIS control points:

- `resolve_control_points`: for each matched tie point, un-warps the WAC-side map pixel back to its
  real observed pixel in the *original* (pre-`cam2map`) crop cube (via `isis_wac.
  ground_to_image_pixel`, deterministic, doesn't depend on trusting the current pose), paired with
  the basemap side's trusted ground lon/lat. **Deliberately ellipsoid-only** (no DEM elevation) --
  matches this pipeline's existing `shape=ellipsoid` camera model exactly; feeding elevation-aware
  ground truth into an ellipsoid-only camera model would conflate real camera-pose error with the
  ellipsoid-vs-terrain gap, worst exactly at the high-relief features (crater rims) motivating this
  whole investigation (the user's own visual parallax observation). A DEM-aware shape model is a
  real, deliberate follow-up, not done here.
- Real result on the current default candidate (`M1327210646CE`): 767 LightGlue matches -> 477
  resolved control points (290 dropped -- their implied ground point doesn't project into the
  original crop; confirmed via a real diagnostic that this is **not** edge-of-crop related (Phase
  30's old `_CROP_EDGE_MARGIN_PX` precedent doesn't apply -- drop rate stays ~38-39% regardless of
  distance from the crop boundary) -- instead, all 290 failures are ISIS's own "no surface
  intersection" error specifically, matching a real, independently-documented upstream ISIS issue:
  PushFrame's `GetLocalNormal` can land outside the correct framelet during exactly this kind of
  search (DOI-USGS/ISIS3 GitHub issue #4256). Not a bug in this project's own code.
- `write_control_network`: writes a real ISIS `.net` file via `plio` (bundled with the conda `isis`
  install, deliberately *not* a `pyproject.toml` dependency of this project -- see
  `scripts/isis_write_control_network.py`'s own docstring for why, and why it's invoked as a
  subprocess under `$ISISROOT/bin/python` rather than imported). Round-tripped and verified
  byte-correct against ISIS's own reader.

## Part 2: the real `jigsaw` bug -- confirmed, root-caused, not fixable from here

**Blocker 1 (found and fixed)**: stock ISIS has *zero* serial-number support for LRO WAC at all --
confirmed via `Instruments.trn` (only `Nac`/`Minirf` have entries under the `# LRO` section, no
`Wac` entry), which is what made `getsn`/`jigsaw` return `"Unknown"` for any WAC cube regardless of
processing stage (confirmed even on the earliest post-`spiceinit` cube, before any of this
project's own stitching/cropping). Real, working fix: append
`Translation = (Wac, WAC-VIS)`/`Translation = (Wac, WAC-UV)` to the `# LRO` section of
`$ISISROOT/appdata/translations/Instruments.trn`, and add a new
`$ISISROOT/appdata/translations/LroWacSerialNumber.trn` (same 3-key recipe as NAC's own:
`SpacecraftName`+`InstrumentId`+`StartTime`, all present on WAC cubes) -- confirmed live, `getsn`
then returns a real SN (`LUNAR RECONNAISSANCE ORBITER/WAC-VIS/2019-11-01T01:22:59.051`), and
`jigsaw` opens the cube and runs. **Not yet added to `docker/Dockerfile`** -- only applied ad hoc in
scratch shell scripts this session, since `jigsaw` itself turned out not to be usable regardless
(Blocker 2). Worth adding permanently only if `jigsaw` is revisited.

**Blocker 2 (found, root-caused, not fixable)**: even with the SN fixed, `jigsaw`'s own bundle
solve is fundamentally broken for this camera. Full trail, cheapest-to-most-decisive:
1. A real 477-point fit diverges to nonphysical corrections (position deltas ~1e68 km) when camera
   pose is left fully unconstrained (`CAMSOLVE=ANGLES`/`SPSOLVE=POSITIONS`, no apriori sigma) --
   the classic single-image near-nadir depth/attitude near-singularity, expected and not itself a
   bug.
2. Adding real apriori sigma constraints (`SPACECRAFT_POSITION_SIGMA=50`, `CAMERA_ANGLES_SIGMA=0.02`)
   makes it converge cleanly (`Converged=TRUE`) -- but the *residuals* stay huge (~223px sample RMS,
   ~47km at this cube's native GSD) and **don't improve** between very different regularization
   strengths (1000m/0.5° vs. 50m/0.02° gave nearly identical `Sigma0`), which is the real tell:
   something is wrong with the fit itself, not the regularization.
3. **Decisive isolation**: built a tautological control network (real pixel -> `ground_point_at_pixel`
   -> that exact ground point fed back as the "trusted" ground truth for the *same* pixel --
   mathematically guaranteed zero true error). `jigsaw` *still* reports huge residuals
   (`Sigma0≈128`) on this mathematically-guaranteed-correct data -- proving the bug is in `jigsaw`'s
   own reprojection, not our control network, the real tie points, the ellipsoid-vs-DEM question, or
   the actual camera pose.
4. **Root cause, found via the actual residual pattern**: for 5 known sample values (35.2, 193.6,
   352.0, 510.4, 668.8, spanning the full 704-wide cube), `jigsaw`'s own *computed* (predicted)
   sample barely moves at all (317-387, a 70px band) regardless of which real, well-separated ground
   point was given -- not a coordinate-offset bug (which would preserve the slope/correlation, just
   shift it), but a near-total loss of correlation between input and output. Ruled out: wrong band
   (checked -- `campt`/`jigsaw` both hard-default to band 1, no override possible, and the real
   per-band value spread is <1%, far too small to explain ~350px); a 1024-vs-704 coordinate offset
   (checked -- the pattern is "nearly constant regardless of input," not "shifted by a constant,"
   which the offset hypothesis would produce and this data rules out).
5. **Confirmed against real ISIS source** (not guessed): `PushFrameCameraGroundMap::SetGround` (the
   actual ground-to-image framelet search) uses a **heuristic binary search minimizing
   spacecraft-to-ground-point distance** (~30 iterations), not a real 2D field-of-view containment
   check -- a plausible, real bug surface for a wide-FOV (61.4°) pushframe sensor, and matches a
   real, independently-filed upstream issue (`GetLocalNormal` landing outside the correct framelet,
   DOI-USGS/ISIS3 #4256). No CLI-exposed workaround exists (`OVEREXISTING`/`OVERHERMITE`,
   `CONTROL_POINT_COORDINATE_TYPE_BUNDLE=RECTANGULAR` -- both tried, `Sigma0` essentially unchanged
   either way). This is compiled C++ inside `jigsaw`; not something fixable from this project.

**Decision** (explicit, from the user): stop pursuing `jigsaw`, pivot to a hand-rolled Python
ground-to-image forward projection, reusing `camera.py`'s already-validated SPICE pose machinery,
built narrow (WAC-VIS band 1 only, not general) and validated hard against `campt` before trusting
it for anything -- see `pose_alignment.py`'s own docstring for a precedent this project already
learned the hard way (an earlier hand-rolled `findfeatures` reimplementation that wasn't
trustworthy) for why that validation step is non-negotiable, not optional polish.

## Part 3: the hand-rolled forward projection -- optics chain and framelet search done and validated

`src/trntest/wac_camera_model.py` (new this session, not yet committed) implements WAC-VIS band 1's
real optics chain -- camera-frame pinhole projection -> `LroWideAngleCameraDistortionMap`'s real
radial distortion (iterative undistort-to-distort) -> `CameraFocalPlaneMap`'s real affine
focal-plane/detector transform -> the real sample/line offsets (`COLOR_SAMPLE_OFFSET=160`,
`BAND_START_LINE=703`) -- with every constant and formula pulled directly from the real ISIS C++
source (`LroWideAngleCamera.cpp`, `LroWideAngleCameraDistortionMap.cpp`,
`LroWideAngleCameraFocalPlaneMap.cpp`, `CameraFocalPlaneMap.cpp`, `CameraDetectorMap.cpp`,
`PushFrameCameraDetectorMap.cpp` -- all at `github.com/DOI-USGS/ISIS3/blob/dev/isis/src/...`, exact
paths in each function this project wrote), and the real per-band coefficients pulled live from the
furnished IK kernel pool (`INS-85631_*` -- band 1, confirmed via the exact `filterIKCode =
naifIkCode() - (filterIKBase + fbandid[i])` formula in `LroWideAngleCamera.cpp`'s constructor, not
guessed).

**Validated to exact (0.000px) agreement** with real `campt` output, given an already-known-correct
framelet, across a 4x6 grid of real pixels spanning the full sample/line range of the current
default candidate's crop cube. This was a real, live Docker run this session (not a claim without a
run behind it) -- the ET-per-framelet affine relationship was calibrated from 2 real `campt`
`EphemerisTime` values rather than hand-deriving ISIS's own frame-index/crop-offset bookkeeping
(deliberately -- fewer places to get a sign/offset wrong).

**Resolved this session: the framelet search** (`wac_camera_model.find_framelet_and_project`,
`calibrate_et_per_crop_line`). Implemented per the agreed design: discrete integer-framelet
bisection (monotonic `within_framelet_line` signal) to a bracketing framelet, then a real 2D
containment check (`1 <= sample <= SAMPLES`, `1 <= within_framelet_line <= FRAMELET_HEIGHT`) --
deliberately not `jigsaw`'s own distance-minimization heuristic. `calibrate_et_per_crop_line`
derives the crop's own line-to-ET affine relationship from 2 real `campt` `EphemerisTime` queries at
the first/last framelet centers, rather than hand-deriving `crop_window_for_camera`'s row-offset/
flip bookkeeping.

**Real overlap confirmed to actually occur** (previously flagged as unconfirmed): adjacent
framelets' `within_framelet_line` advances by ~9.9 lines per framelet step, not the full
`FRAMELET_HEIGHT`=14 -- a real ~29% ground-coverage overlap, independently corroborated by this
project's own earlier `docs/data-sources.md` note (from the `usgscsm`/`jigsaw` bug investigation)
that adjacent Pushframe exposures have real overlapping coverage. **This is not a correctness
problem**: a ground point legitimately has more than one valid image-space solution in an overlap
band, and any of them is equally correct -- there is no need to recover whichever specific pixel a
prior observation happened to be measured at (a framing this session tried and the user corrected).
The center-line tiebreak (picking whichever valid framelet puts the point deepest inside its own
valid range) exists for a different reason: keeping the choice *smooth* under small pose
perturbations, so a downstream optimizer doesn't see a discontinuous jump between framelets as it
takes steps -- not "recovering the right answer" among several equally valid ones.

**Validation, following the doc's own agreed rigorous direction** (forward-then-check via the
already-reliable inverse, not naive forward-vs-original-pixel comparison, which can legitimately
differ in overlap zones for a fully correct implementation): live Docker run against the current
default candidate (`M1327210646CE`, 70-framelet crop) -- forward-projected a 3x3 grid of real crop
pixels' own ground points (`campt` image-to-ground), fed each result back through
`ground_point_at_pixel`, and got **0.00m ground error on all 9 points**, spanning the crop's full
sample/line range. `et_per_line * FRAMELET_HEIGHT` also reproduced the real interframe delay
(1.40625s) exactly, cross-confirming the ET calibration has no sign/scale bug.

**Resolved this session: a real, direction-dependent bug in the bisection itself.** The first
version hardcoded the assumption that `within_framelet_line` *decreases* with increasing framelet
index -- true for the one real candidate validated above (its `et_per_line` came out negative), but
not universal: LRO's WAC undergoes periodic 180-degree yaw flips (the same underlying cause as
`camera.reverse_crop_along_track`/`boresight_rotation_k`), so a pass with the opposite yaw state
would have the opposite sign, and the old hardcoded version would have silently converged to a
completely wrong framelet for it. Caught by a synthetic unit test (`test_fit_pose_correction_...`)
using a candidate with the opposite sign, not by the one real candidate exercised so far. Fixed by
measuring the direction live from the two range endpoints before bisecting, instead of assuming it
-- re-confirmed the real candidate still gives 0.00m ground error after the fix (no regression).

**Resolved this session: the optimizer** (`wac_camera_model.fit_pose_correction`, `PoseCorrection`,
`PoseCorrectionFit`). A single, frozen (not per-framelet) 6-DOF correction -- 3 position (meters,
added directly to `camera_pose_moon_me`'s MOON_ME position) + 3 rotation (a `scipy.spatial.
transform.Rotation` rotation vector, composed on the *camera* side: `R_corrected = R_original @
delta_rotation`, matching this project's own precedent that WAC-VIS's real boresight offset is
frame-constant, not time-varying) -- fit via `scipy.optimize.least_squares`, residual =
predicted-minus-observed pixel across all resolved control points. A control point whose ground
point falls outside crop coverage under some candidate correction gets a fixed, finite penalty
residual (`_UNRESOLVED_RESIDUAL_PX`) rather than crashing the solve.

Validated on synthetic data with a known injected 6-DOF correction (not real control points yet --
see "Remaining work" below): a mocked, straight-line-translating synthetic camera, ground points
placed at chosen `(sample, within_line)` targets across several framelets (enough geometric
diversity to avoid the classic near-nadir depth/attitude singularity noted in Part 2's Blocker 2),
observations generated by projecting through the true correction, then fit from `x0=0`. Converges to
near-zero residual (not exact parameter recovery -- deliberately not asserted, since a synthetic
near-nadir setup like this one can have its own legitimate degeneracies; checking the residual
directly avoids a flaky test for the wrong reason). Getting this synthetic test working also
surfaced two real bugs along the way (worth remembering if this pattern comes up again): (1) a
distortion-inversion bug in the test's own ground-truth construction (solved for the *distorted*
focal-plane coordinates but fed them into the pinhole ray as if undistorted -- fixed by applying the
real closed-form distorted-to-undistorted relationship first); (2) the bisection-direction bug
above, found only because the synthetic camera's sign happened to differ from the one real candidate
tested so far.

**Remaining work, in order**:
1. ~~The framelet search~~ -- done, see above.
2. ~~Validation of the search~~ -- done, see above.
3. ~~The optimizer~~ -- done, see above.
4. ~~A real fit against the actual basemap-derived tie points, and a corrected-overlay visual
   comparison~~ -- done (2026-08-19). Fit against the real 477-point control network (from 767
   LightGlue matches): residual mean 4.42px -> 3.36px, dominated by a small (~0.18deg) camera-frame
   rotation. The corrected pose is baked into a copy of the crop cube via
   `isis_wac.apply_pose_correction_to_crop` (patches the cached `InstrumentPointing` Table's
   `ConstantRotation` via `tabledump`/`csv2table` -- see `docs/data-sources.md`'s own entry on this
   mechanism and its real gotchas, and `docs/proposed-tasks/corrected-overlay-cam2map-plan.md` for the full
   implementation trail) so the existing, unmodified `cam2map`/`plotting.plot_overlay_toggle` path
   reprojects and displays it, wired into `notebooks/pose_alignment_spike.py` and reviewed live by
   the user. **Real result, not a full win**: the fit only closes ~24% of the real gap (~813m ->
   ~618m of residual at this crop's own ~184m/px native GSD, vs. the homography spike's own
   ~150-165m) -- see `docs/plan.md`'s status line for the full writeup and the leading suspect
   (ellipsoid-only ground truth, no DEM elevation) motivating the next step: a DEM-aware shape model.

## Repo state / how to resume

All of Part 1 and 2's work is committed on `feature/alignment` (WIP commit `504b9ff`):
- `src/trntest/control_network.py` + `tests/test_control_network.py` -- done, tested, real.
- `src/trntest/isis_wac.py`'s `cube_serial_number` helper -- done, real (used by
  `control_network.write_control_network`).
- `scripts/isis_write_control_network.py` -- done, real.
- `src/trntest/wac_camera_model.py` + `tests/test_wac_camera_model.py` -- optics chain, validated.
- `notebooks/pose_alignment_spike.py`'s control-network section.

A later session added Part 3 in full: the framelet search (`find_framelet_and_project`,
`calibrate_et_per_crop_line`, plus `isis_wac.ephemeris_time_at_pixel`), the direction-agnostic
bisection fix, and the optimizer (`fit_pose_correction`, `PoseCorrection`), each with real unit
tests (synthetic/mocked, no live ISIS needed to test the algorithms themselves) plus the live Docker
validation described above. `scipy` is now a real `pyproject.toml` dependency (added this session).

A still-later session (2026-08-19) completed item 4: the real fit, `isis_wac.
apply_pose_correction_to_crop`, and the corrected-overlay notebook wiring -- see that item's own
entry above for the real result. Also batched `control_network.resolve_control_points`'s per-point
`campt` calls (`isis_wac.ground_to_image_pixels_batch`, ~230s -> ~3s on the real 767-point set, see
`docs/data-sources.md`), unrelated to the alignment question itself but found while working the same
notebook. What's left for this investigation as a whole: the DEM-aware ground truth follow-up noted
in item 4 and `docs/plan.md`'s status line.

The `Instructions.trn`/`LroWacSerialNumber.trn` serial-number patch (Part 2, Blocker 1) is **not**
in `docker/Dockerfile` -- it was only ever applied inside ad hoc scratch shell scripts this session
(pattern: `sed -i '/# LRO/a...' ...Instruments.trn` + a heredoc'd new `.trn` file, run at the top of
each scratch script before the real work). If `jigsaw` is ever revisited, that patch needs to be
added to the Dockerfile properly (with real docstring/comment provenance, matching this project's
own convention) rather than re-derived from scratch -- the exact `sed`/heredoc content is preserved
in this session's transcript if needed, and the underlying facts (which keywords, which file, which
values) are fully documented above regardless.
