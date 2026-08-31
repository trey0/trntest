# Projection-aware (3D) camera-pose alignment: `jigsaw` investigation and the hand-rolled fallback

Reference for `wac_camera_model.py`'s hand-rolled WAC-VIS forward projection: why ISIS's own
`jigsaw` bundle adjuster can't be used for this camera, and the validation trail behind the
replacement that's used instead. See `docs/plan.md`'s "camera-pose alignment" open item for the
higher-level status; several functions in `wac_camera_model.py`/`control_network.py`/`tie_points.py`
point back to this doc for the ISIS source citations and bug numbers below rather than repeating
them.

## The approach

Convert matched tie points into 3D ground control (`src/trntest/control_network.py`) and fit a 6-DOF
camera pose correction (3 position + 3 attitude, frozen/degree-0) against them, using each point's
reprojection error as the residual. The natural tool is ISIS's own `jigsaw` bundle adjuster, but it
has a confirmed bug for this instrument (see "The `jigsaw` bug" below), so the fallback is a
hand-rolled Python forward-projection + `scipy.optimize.least_squares` fit, reusing `camera.py`'s
already-validated SPICE pose machinery, validated hard against `campt` before being trusted for
anything (see `pose_alignment.py`'s own module comment for why that validation step is
non-negotiable, not optional polish — an earlier hand-rolled `findfeatures` reimplementation in this
project wasn't trustworthy).

## `control_network.py`

Converts `pose_alignment`'s matched map-space tie points into ISIS control points:

- `resolve_control_points`: for each matched tie point, un-warps the WAC-side map pixel back to its
  observed pixel in the *original* (pre-`cam2map`) crop cube (via
  `wac_camera_model.find_framelet_and_project`, deterministic, doesn't depend on trusting the
  current pose), paired with the basemap side's trusted ground lon/lat. Deliberately ellipsoid-only
  (no DEM elevation) — matches this pipeline's `shape=ellipsoid` camera model exactly; feeding
  elevation-aware ground truth into an ellipsoid-only camera model would conflate camera-pose error
  with the ellipsoid-vs-terrain gap, worst at the high-relief features (crater rims) this
  investigation is ultimately about. A DEM-aware shape model is a deliberate follow-up, not done
  here (see "Open item" below).
- On the current default candidate (`M1327210646CE`): 767 LightGlue matches → 477 resolved control
  points (290 dropped — their implied ground point doesn't project into the original crop). All 290
  failures are ISIS's own "no surface intersection" error specifically, matching an
  independently-documented upstream ISIS issue: PushFrame's `GetLocalNormal` can land outside the
  correct framelet during this kind of search (DOI-USGS/ISIS3 GitHub issue #4256). Confirmed not
  edge-of-crop related (drop rate stays ~38-39% regardless of distance from the crop boundary).
- `write_control_network`: writes an ISIS `.net` file via `plio` (bundled with the conda `isis`
  install, deliberately *not* a `pyproject.toml` dependency — see
  `scripts/isis_write_control_network.py`'s own docstring for why, and why it's invoked as a
  subprocess under `$ISISROOT/bin/python` rather than imported). Round-tripped and verified
  byte-correct against ISIS's own reader.

## The `jigsaw` bug

**LRO WAC has no ISIS serial-number support at all** — confirmed via `Instructions.trn` (only
`Nac`/`Minirf` have entries under the `# LRO` section, no `Wac` entry), which is what made
`getsn`/`jigsaw` return `"Unknown"` for any WAC cube regardless of processing stage. Fix: append
`Translation = (Wac, WAC-VIS)`/`Translation = (Wac, WAC-UV)` to the `# LRO` section of
`$ISISROOT/appdata/translations/Instruments.trn`, and add a new
`$ISISROOT/appdata/translations/LroWacSerialNumber.trn` (same 3-key recipe as NAC's own:
`SpacecraftName`+`InstrumentId`+`StartTime`, all present on WAC cubes) — confirmed live, `getsn`
then returns a real SN (`LUNAR RECONNAISSANCE ORBITER/WAC-VIS/2019-11-01T01:22:59.051`), and
`jigsaw` opens the cube and runs. **Not in `docker/Dockerfile`** — this patch turned out not to be
needed, since `jigsaw` itself is unusable regardless (below); worth adding permanently only if
`jigsaw` is revisited.

**Even with the serial number fixed, `jigsaw`'s own bundle solve is fundamentally broken for this
camera.** Isolation trail, cheapest-to-most-decisive:
1. A 477-point fit diverges to nonphysical corrections (position deltas ~1e68 km) when camera pose
   is left fully unconstrained (`CAMSOLVE=ANGLES`/`SPSOLVE=POSITIONS`, no apriori sigma) — the
   classic single-image near-nadir depth/attitude near-singularity, expected and not itself a bug.
2. Adding apriori sigma constraints (`SPACECRAFT_POSITION_SIGMA=50`, `CAMERA_ANGLES_SIGMA=0.02`)
   makes it converge cleanly (`Converged=TRUE`) — but the *residuals* stay huge (~223px sample RMS,
   ~47km at this cube's native GSD) and don't improve between very different regularization
   strengths (1000m/0.5° vs. 50m/0.02° gave nearly identical `Sigma0`) — the tell that something is
   wrong with the fit itself, not the regularization.
3. **Decisive isolation**: built a tautological control network (a pixel → `ground_point_at_pixel`
   → that exact ground point fed back as the "trusted" ground truth for the same pixel —
   mathematically guaranteed zero true error). `jigsaw` still reports huge residuals
   (`Sigma0≈128`) on this mathematically-guaranteed-correct data — proving the bug is in `jigsaw`'s
   own reprojection, not the control network, the tie points, the ellipsoid-vs-DEM question, or the
   camera pose.
4. **Root cause, found via the residual pattern**: for 5 known sample values (35.2, 193.6, 352.0,
   510.4, 668.8, spanning the full 704-wide cube), `jigsaw`'s own computed (predicted) sample barely
   moves at all (317-387, a 70px band) regardless of which well-separated ground point was given —
   not a coordinate-offset bug (which would preserve the slope/correlation, just shift it), but a
   near-total loss of correlation between input and output. Ruled out: wrong band (`campt`/`jigsaw`
   both hard-default to band 1, no override possible, and the per-band value spread is <1%, far too
   small to explain ~350px); a 1024-vs-704 coordinate offset (the pattern is "nearly constant
   regardless of input," not "shifted by a constant," which an offset would produce).
5. **Confirmed against ISIS source**: `PushFrameCameraGroundMap::SetGround` (the ground-to-image
   framelet search) uses a heuristic binary search minimizing spacecraft-to-ground-point distance
   (~30 iterations), not a 2D field-of-view containment check — a plausible bug surface for a
   wide-FOV (61.4°) pushframe sensor, matching the independently-filed upstream issue
   (`GetLocalNormal` landing outside the correct framelet, DOI-USGS/ISIS3 #4256). No CLI-exposed
   workaround exists (`OVEREXISTING`/`OVERHERMITE`, `CONTROL_POINT_COORDINATE_TYPE_BUNDLE=RECTANGULAR`
   — both tried, `Sigma0` essentially unchanged either way). This is compiled C++ inside `jigsaw`,
   not something fixable from this project.

**Decision**: stop pursuing `jigsaw`, pivot to a hand-rolled Python ground-to-image forward
projection reusing `camera.py`'s already-validated SPICE pose machinery, built narrow (WAC-VIS band
1 only, not general) and validated hard against `campt` before trusting it for anything.

## The hand-rolled forward projection

`src/trntest/wac_camera_model.py` implements WAC-VIS band 1's optics chain — camera-frame pinhole
projection → `LroWideAngleCameraDistortionMap`'s radial distortion (iterative undistort-to-distort)
→ `CameraFocalPlaneMap`'s affine focal-plane/detector transform → the sample/line offsets
(`COLOR_SAMPLE_OFFSET=160`, `BAND_START_LINE=703`) — with every constant and formula pulled directly
from ISIS's own C++ source (`LroWideAngleCamera.cpp`, `LroWideAngleCameraDistortionMap.cpp`,
`LroWideAngleCameraFocalPlaneMap.cpp`, `CameraFocalPlaneMap.cpp`, `CameraDetectorMap.cpp`,
`PushFrameCameraDetectorMap.cpp`, all at `github.com/DOI-USGS/ISIS3/blob/dev/isis/src/...`, exact
paths in each function this project wrote), and the per-band coefficients pulled live from the
furnished IK kernel pool (`INS-85631_*` — band 1, confirmed via the exact `filterIKCode =
naifIkCode() - (filterIKBase + fbandid[i])` formula in `LroWideAngleCamera.cpp`'s constructor, not
guessed).

**Validated to exact (0.000px) agreement** with `campt` output, given an already-known-correct
framelet, across a 4x6 grid of pixels spanning the full sample/line range of the default candidate's
crop cube. The ET-per-framelet affine relationship was calibrated from 2 `campt` `EphemerisTime`
values rather than hand-deriving ISIS's own frame-index/crop-offset bookkeeping (fewer places to get
a sign/offset wrong).

**Framelet search** (`wac_camera_model.find_framelet_and_project`, `calibrate_et_per_crop_line`):
discrete integer-framelet bisection (monotonic `within_framelet_line` signal) to a bracketing
framelet, then a 2D containment check (`1 <= sample <= SAMPLES`, `1 <= within_framelet_line <=
FRAMELET_HEIGHT`) — deliberately not `jigsaw`'s own distance-minimization heuristic.
`calibrate_et_per_crop_line` derives the crop's own line-to-ET affine relationship from 2 `campt`
`EphemerisTime` queries at the first/last framelet centers, rather than hand-deriving
`crop_window_for_camera`'s row-offset/flip bookkeeping.

**Overlap confirmed to actually occur**: adjacent framelets' `within_framelet_line` advances by ~9.9
lines per framelet step, not the full `FRAMELET_HEIGHT`=14 — a ~29% ground-coverage overlap,
corroborated by `docs/external-tools.md`'s "`usgscsm`'s `groundToImage` bug for Pushframe sensors"
section, which independently found that adjacent Pushframe exposures overlap. This is not a
correctness problem: a ground point legitimately has more than one valid image-space solution in an
overlap band, and any of them is equally correct — there's no "right" one to recover. The
center-line tiebreak (picking whichever valid framelet puts the point deepest inside its own valid
range) exists for a different reason: keeping the choice *smooth* under small pose perturbations, so
a downstream optimizer doesn't see a discontinuous jump between framelets as it takes steps.

**Validation** (forward-then-check via the already-reliable inverse, not naive
forward-vs-original-pixel comparison, which can legitimately differ in overlap zones for a fully
correct implementation): against the default candidate (`M1327210646CE`, 70-framelet crop) —
forward-projected a 3x3 grid of crop pixels' own ground points (`campt` image-to-ground), fed each
result back through `ground_point_at_pixel`, and got **0.00m ground error on all 9 points**,
spanning the crop's full sample/line range. `et_per_line * FRAMELET_HEIGHT` also reproduced the
interframe delay (1.40625s) exactly, cross-confirming the ET calibration has no sign/scale bug.

**Direction-dependent bisection bug, found and fixed**: the first version hardcoded the assumption
that `within_framelet_line` *decreases* with increasing framelet index — true for the one candidate
validated above, but not universal: LRO's WAC undergoes periodic 180-degree yaw flips (the same
underlying cause as `camera.reverse_crop_along_track`/`boresight_rotation_k`), so a pass with the
opposite yaw state has the opposite sign, and the old hardcoded version would have silently
converged to a completely wrong framelet for it. Caught by a synthetic unit test using a candidate
with the opposite sign, not by the one real candidate exercised so far. Fixed by measuring the
direction live from the two range endpoints before bisecting, instead of assuming it.

**Optimizer** (`wac_camera_model.fit_pose_correction`, `PoseCorrection`, `PoseCorrectionFit`): a
single, frozen (not per-framelet) 6-DOF correction — 3 position (meters, added directly to
`camera_pose_moon_me`'s MOON_ME position) + 3 rotation (a `scipy.spatial.transform.Rotation`
rotation vector, composed on the *camera* side: `R_corrected = R_original @ delta_rotation`,
matching WAC-VIS's boresight offset being frame-constant, not time-varying) — fit via
`scipy.optimize.least_squares`, residual = predicted-minus-observed pixel across all resolved
control points. A control point whose ground point falls outside crop coverage under some candidate
correction gets a fixed, finite penalty residual (`_UNRESOLVED_RESIDUAL_PX`) rather than crashing
the solve.

Validated on synthetic data with a known injected 6-DOF correction: a mocked, straight-line-
translating synthetic camera, ground points placed at chosen `(sample, within_line)` targets across
several framelets (enough geometric diversity to avoid the near-nadir depth/attitude singularity
noted above), observations generated by projecting through the true correction, then fit from
`x0=0`. Converges to near-zero residual (not exact parameter recovery — deliberately not asserted,
since a synthetic near-nadir setup like this can have its own legitimate degeneracies; checking the
residual directly avoids a flaky test for the wrong reason). This synthetic test also surfaced two
bugs worth remembering if this pattern comes up again: (1) a distortion-inversion bug in the test's
own ground-truth construction (solved for the *distorted* focal-plane coordinates but fed them into
the pinhole ray as if undistorted — fixed by applying the closed-form distorted-to-undistorted
relationship first); (2) the bisection-direction bug above, found only because the synthetic
camera's sign happened to differ from the one real candidate tested so far.

**Fit against real control points**: fit against the 477-point control network (from 767 LightGlue
matches): residual mean 4.42px → 3.36px, dominated by a small (~0.18deg) camera-frame rotation. The
corrected pose is baked into a copy of the crop cube via `isis_wac.apply_pose_correction_to_crop`
(patches the cached `InstrumentPointing` Table's `ConstantRotation` via `tabledump`/`csv2table` —
see `docs/external-tools.md`'s "Patching a cube's cached pointing via tabledump/csv2table" section)
so the existing, unmodified `cam2map`/`plotting.plot_overlay_toggle` path reprojects and displays
it, wired into `notebooks/pose_alignment_spike.py`. **Not a full win**: the fit only closes ~24% of
the gap (~813m → ~618m of residual at this crop's own ~184m/px native GSD, vs. the homography
spike's own ~150-165m) — see `docs/plan.md`'s status line for the full writeup and the leading
suspect (ellipsoid-only ground truth, no DEM elevation) motivating the open item below.

Also batched `control_network.resolve_control_points`'s per-point `campt` calls
(`isis_wac.ground_to_image_pixels_batch`, ~230s → ~3s on the 767-point set — see
`docs/external-tools.md`'s "`campt`'s `USECOORDLIST` batch mode" section), found while working the
same notebook, unrelated to the alignment question itself.

## Open item

A DEM-aware shape model (replacing the current ellipsoid-only ground truth) is the leading suspect
for why the fit above only closes ~24% of the gap — the ellipsoid-vs-terrain gap is worst exactly at
the high-relief features (crater rims) this investigation is ultimately about. Not started; see
`docs/plan.md`'s status line.
