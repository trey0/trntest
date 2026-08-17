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

## Part 3: the hand-rolled forward projection -- optics chain done and validated; framelet search not yet built

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

**Remaining work, in order**:
1. **The framelet search** (given an arbitrary 3D ground point with no image coordinates, which
   framelet images it) -- not yet implemented. Agreed design (see the conversation this doc
   summarizes): discrete integer-framelet bisection (monotonic along-track distance/angle as a
   function of framelet index, over this crop's short real timespan) to get a good starting
   candidate, then **explicit 2D pinhole+distortion projection and real containment check
   (`0 <= sample < 704`, `0 <= within_framelet_line < 14`) on the bracketing framelet(s)** --
   deliberately *not* `jigsaw`'s own distance-minimization heuristic, since that's the likely site
   of its bug. If both bracketing framelets validly contain the point (real overlap -- unconfirmed
   whether this actually occurs for this product; `NumLinesOverlap=0` in the label but that field's
   exact meaning wasn't verified), use whichever puts the point closer to that framelet's own center
   line (the user's own proposed tiebreak).
2. **Validation of the search** (not just the optics chain): the *rigorous* round-trip direction is
   forward-then-check-via-the-already-reliable-inverse (ground point -> our forward projector,
   whichever framelet it picks -> feed that resulting pixel through `ground_point_at_pixel` -> must
   recover the same ground point) -- NOT naive forward-vs-original-pixel comparison, which can
   legitimately fail in real overlap zones even for a correct implementation (a subtlety the user
   raised and this project agreed is the correct framing).
3. **The optimizer**: `scipy.optimize.least_squares` over 6 parameters (3 position km, 3 rotation),
   residual = predicted-minus-observed pixel across all resolved control points from
   `resolve_control_points`. Not started.
4. **A real fit** against the actual basemap-derived tie points (not just tautological/synthetic
   validation data), and a corrected overlay comparison via the existing
   `plotting.plot_overlay_toggle`, wired into `notebooks/pose_alignment_spike.py`.

## Repo state / how to resume

This work happened directly in the `a1` worktree, mostly as ad hoc scratch scripts (not committed)
plus two new real modules written at the very end of the session:
- `src/trntest/control_network.py` + `tests/test_control_network.py` -- done, tested, real.
- `src/trntest/isis_wac.py`'s new `cube_serial_number` helper -- done, real (used by
  `control_network.write_control_network`).
- `scripts/isis_write_control_network.py` -- done, real.
- `src/trntest/wac_camera_model.py` + `tests/test_wac_camera_model.py` -- optics chain done,
  validated (see above), framelet search NOT included.
- `notebooks/pose_alignment_spike.py`'s control-network section -- written and notebook-verified
  this session (prints real control-point counts/ranges).

None of this is committed yet as of this doc being written -- see the user's own instruction to
commit on an appropriate branch to preserve it. `feature/alignment` (already merged to `main`
earlier this session) is the natural home if continuing this exact thread, or a new branch (e.g.
`feature/jigsaw-fallback`) if the merged state should stay clean of an admittedly-incomplete
in-progress piece -- the user's call, not decided here.

The `Instructions.trn`/`LroWacSerialNumber.trn` serial-number patch (Part 2, Blocker 1) is **not**
in `docker/Dockerfile` -- it was only ever applied inside ad hoc scratch shell scripts this session
(pattern: `sed -i '/# LRO/a...' ...Instruments.trn` + a heredoc'd new `.trn` file, run at the top of
each scratch script before the real work). If `jigsaw` is ever revisited, that patch needs to be
added to the Dockerfile properly (with real docstring/comment provenance, matching this project's
own convention) rather than re-derived from scratch -- the exact `sed`/heredoc content is preserved
in this session's transcript if needed, and the underlying facts (which keywords, which file, which
values) are fully documented above regardless.
