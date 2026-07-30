# Plan: SPICE-posed synthetic lunar satellite imagery via ASP `sat_sim`

## Goal

Generate a synthetic lunar satellite image (with a CSM/"ISD" JSON camera sidecar) using NASA's
Ames Stereo Pipeline (ASP) `sat_sim` tool, fed by real DEM + visible imagery pulled live from the
Lunaserv WMS server — with the camera's pose derived from the **real LRO spacecraft trajectory**
(NAIF SPICE kernels) at the time of an actual LROC WAC EDR image, so the synthetic 256x256 frame
approximates the FOV of a real WAC swath and can be compared against it. This is a demo/exercise in
AI-assisted coding on a real geospatial engineering task — see root `CLAUDE.md` for how the repo's
docs are organized.

All heavy tooling/build/test happens inside a Docker container (Ubuntu 24.04), built from the
checked-in `docker/Dockerfile`, so it's reproducible off this host.

See `docs/data-sources.md` for endpoint/format/layer specifics and `docs/caching.md` for the
data-caching approach. Update those files (not just this one) as concrete choices are made.

## Phases (status)

- [x] **Phase 0 — Repo & Docker scaffolding.** `CLAUDE.md`, `docs/*.md`, `docker/Dockerfile`,
  `docker/docker-compose.yml`, `.dockerignore`, `README.md`. Docker Engine installed on host.
- [x] **Phase 1 — Base image.** GDAL 3.12.2 + ASP 3.7.0 (prebuilt Linux x86_64 binary, in
  `/opt/StereoPipeline`) + Python stack installed with `uv` into a venv at `/opt/venv`
  (jupyterlab, numpy, matplotlib, rasterio, requests, spiceypy) — a venv is needed because apt's
  system Python conflicts with installing numpy alongside it. Smoke tested: `sat_sim` and
  `gdalinfo --version` both run inside the container.
- [x] **Phase 2 — WAC EDR selection + SPICE pose.** Chosen product `M1329714703CE` (see
  `docs/data-sources.md`), posed at **framelet index 440** (`TARGET_FRAME_INDEX` in
  `scripts/build_camera_from_spice.py` — originally 0, moved after Phase 5 revealed frames 0-~210
  are in shadow; see that phase's notes below). `scripts/fetch_spice_kernels.py` selects/downloads
  the minimal kernel set (~585 MB, dominated by one 10-day `lrosc` CK chunk) and verifies CK
  coverage for both the SC_BUS (-85000) and WAC (-85620) frames at the target ET.
  `scripts/build_camera_from_spice.py` computes LRO's position + WAC-VIS boresight orientation
  directly in `MOON_ME`, derives focal length so the camera's FOV equals the real documented WAC
  color-mode FOV (61.4°, see "Square-crop sizing" below — originally derived from a fixed ~100
  m/px GSD target instead, revised), writes `output/camera_frame440.tsai`, and computes the
  4-corner+center ground footprint
  (via analytic sphere intersection, not `sincpt`, since our synthetic camera isn't a registered
  SPICE frame). **Checkpoint passed (at frame 0, before the frame-index move):** altitude came out
  ~64 km at ~79°S — squarely consistent with LRO's known Fourth Extended Science Mission frozen
  elliptical orbit (low periapsis over the south pole); boresight vs. nadir off by only ~1.1°, and a
  cross-check using `spice.sincpt` on the named `LRO_LROCWAC_VIS` frame landed within ~0.15° of the
  sphere-intersection center — pose computation is sound; re-run at frame 440 for the final result
  (see Phase 4).
- [x] **Phase 3 — Lunaserv fetch.** `scripts/fetch_lunaserv.py` re-derives the Phase 2 footprint,
  pads it 30%, computes a physically-square pixel grid (accounting for `cos(lat)` near the pole),
  and fetches `luna_wac_global` + `luna_wac_dtm_numeric_meters_absolute` at ~100 m/px via
  `srs=IAU2000:30100` (confirmed native/unprojected — see `docs/data-sources.md`). Converts the DEM
  from planetocentric radius to elevation (subtract 1737400 m — see the "gotcha" in
  `docs/data-sources.md`), then hole-fills with `dem_mosaic` (no actual holes were present for this
  ROI — 100% valid pixels). Output: `output/dem_filled-tile-0.tif` + the cached ortho tif, identical
  size/origin/pixel-size. **Checkpoint passed:** `sat_sim` with its own auto-generated 2-camera
  track rendered two 256x256 `.tif`s successfully against this DEM/ortho pair.
- [x] **Phase 4 — Render.** `scripts/run_sat_sim.sh`: `sat_sim --camera-list` (containing the
  Phase 2 `.tsai`) renders `output/render/run-camera_frame440.tif`. `--save-as-csm` turned out to be
  a no-op in `--camera-list` mode (see `docs/data-sources.md`) — the CSM/"ISD" JSON sidecar is
  instead produced by `cam_gen --input-camera ... --refine-intrinsics none`, which also
  independently re-derived the same sub-spacecraft geodetic position from the `.tsai`'s ECEF pose,
  cross-validating Phase 2's SPICE computation. **Bug found and fixed:** the script originally
  picked the ortho tile via `ls .../luna_wac_global/*.tif | head -1`, which silently grabbed a
  stale tile once the cache held tiles for more than one footprint (see Phase 5) — now
  `fetch_lunaserv.fetch_dem_and_ortho()` writes the exact paths it used to
  `output/lunaserv_result.txt`, which this script sources instead of globbing.
- [x] **Phase 5 — Real-image comparison (revised twice).** Went through three iterations to get a
  comparison that's actually recognizable — see `docs/data-sources.md` ("Real image comparison")
  for the full story:
  1. Raw EDR `.IMG` strip — uncalibrated 8-bit DN, and (unrealized at the time) still multiplexed
     7 filters per 78-line frame. Not recognizable.
  2. Switched to the **CDR** (calibrated I/F, `LRO-L-LROC-3-CDR-V1.0`, product `M1329714703CC` in
     volume `LROLRC_1041C`) — still just a raw multiplexed strip, still not recognizable, because
     calibration alone doesn't separate the 7 interleaved filter bands.
  3. Fetched the actual LROC EDR/CDR SIS PDF and confirmed the 78-line frame layout (2 UV x 4 TDI
     lines + 5 VIS x 14 TDI lines, order flips on yaw maneuvers) — `scripts/fetch_wac_comparison.py`
     now extracts one VIS filter's 14-line block (offset `[22:36)`, yaw-order-invariant) from many
     consecutive frames and stacks them, which is how WAC's push-frame design is meant to build
     continuous coverage. This *also* surfaced that frame 0 (the original Phase 2 pose) is in
     near-total shadow — frames 0-~210 of this product are essentially signal-free, with real
     terrain only appearing from frame ~240 onward. Moved `TARGET_FRAME_INDEX` to **440** (Phase 2)
     as a result. Final result: a clearly recognizable cratered scene that visually matches the
     synthetic render (same bright diagonal feature, same dark crater) — verified by eye.
  4. **Square-crop sizing revision:** the synthetic camera's FOV and the CDR crop's frame count
     were originally sized independently (fixed ~100 m/px GSD target; fixed 19 frames chosen ad
     hoc) and didn't reliably cover the same real ground area. Fixed by grounding both in the real
     WAC color-mode FOV (61.4°, from the SIS — `spice.getfov` on the loaded WAC-VIS IK returns the
     wrong, monochrome-mode ~91.7° FOV instead, so it can't be used directly): the synthetic
     camera's `fu=fv` is now derived from that angle directly, and
     `build_camera_from_spice.compute_n_frames_for_square_crop()` ray-traces the real cross-track
     ground width (≈82.0 km) and per-frame ground advance (≈1.147 km) to pick the frame count
     (71, giving a 994x704 CDR crop) so both images cover the same real square ground area — not
     square in pixels, but square in km, per the user's explicit request. See
     `docs/data-sources.md` ("Square-crop sizing") for the full derivation.
  5. **Pose epoch fix:** the synthetic camera's pose was at the crop's *start* frame (440), not its
     *middle* — so its image center should have lined up with the crop's top edge, not its center.
     Fixed: `build()` now derives `center_frame_index = TARGET_FRAME_INDEX + n_frames/2 = 475.5` and
     poses the camera there instead.
  6. **Comparison-figure aspect ratio:** the CDR crop's non-square pixel array (994x704) was
     displayed by `imshow()` as a tall rectangle despite covering a square ground area. Fixed by
     plotting both panels with `extent=` in real km instead of raw pixel index.
  7. **SPICE-derived tie points** (`scripts/tie_points.py`): 5 points (die's "5"/X pattern) placed
     in the ground area both images share, projected into each image's real pixel coordinates
     (closed-form pinhole for the synthetic image; a frame-index bisection for the real crop, which
     mixes many real poses). Verified via a self-consistency check (the crop's own 4 corners
     project back to exactly `(0,0)`/`(704,0)`/`(0,994)`/`(704,994)`) and via the two checks the
     user asked for (shared bbox non-empty; all 5 points within both images' bounds) — both pass.
     Also found (fixed in the next two revisions, below): the two images are rotated ~90° relative
     to each other, a real consequence of the WAC-VIS X=along-track/Y=cross-track finding combined
     with how the synthetic camera's pixel axes were set up.
  8. **Fixed sensor-model axis convention:** the synthetic camera's pixel axes were an arbitrary
     in-house choice, causing the ~90° mismatch above. Fixed with a **single, fixed** (not
     pass-dependent) 90°-about-boresight rotation of `R` in `build_camera_from_spice.build()`,
     chosen so `px`=cross-track, `py`=along-track — matching both WAC's and NAC's real
     archived-image layout (checked NAC too; not actually a WAC-vs-NAC fork, both agree) — and,
     between the two rotations satisfying that, the one where `py` increases in the same temporal
     sense as the real archived data's row axis. Required re-rendering (`run_sat_sim.sh`, `cam_gen`)
     since it changes actual pixel output. Verified via the crop-corner self-consistency check
     (still exact) and the synthetic-vs-crop corner matching (now direct, not rotated) — see
     `docs/data-sources.md` ("Fixed sensor-model axis convention").
  9. **North-up display rotation** (`scripts/display_orientation.py`): deliberately kept separate
     from the previous fix — this is notebook-display-only (rotates already-rendered/extracted
     arrays and tie-point marker positions via `np.rot90`-equivalent transforms, verified
     numerically against `np.rot90` directly rather than trusted from hand-derived algebra alone),
     and does not touch the sensor model. Picks, per image, the multiple of 90° (no mirroring)
     whose on-screen "up" is closest to true north. This run: both images picked the same 180°
     rotation with the same 26.7° residual deviation from true north (expected, since both share
     the same axis convention after the previous fix; the nonzero residual reflects that this
     pass's along-track direction isn't exactly north-south, the best available result under a
     90°-multiples-only constraint) — see `docs/data-sources.md` for the full derivation.
- [x] **Phase 6 — Notebook.** `notebooks/lunar_sat_sim_demo.ipynb` drives all the `scripts/`
  modules end to end: SPICE pose → Lunaserv DEM/ortho → footprint-over-mosaic plot → `sat_sim`
  render + `cam_gen` CSM/ISD JSON → real WAC CDR band-separated comparison. Verified with
  `jupyter nbconvert --to notebook --execute` inside the container — runs top to bottom with no
  errors, 4 figures render, comparison figure visually confirmed recognizable. Checked in with
  outputs baked in, so it's viewable without re-running. Open via Jupyter Lab (`docker compose up`,
  port-mapped per the user's preference — see README).

- [x] **Phase 7 — Package restructuring.** Converted the flat `scripts/` layout into an installable
  package: `src/trntest/` (src-layout — `cache.py`, `spice_kernels.py`, `camera.py`, `lunaserv.py`,
  `wac.py`, `tie_points.py`, `orientation.py`, `render.py`, `plotting.py`, `session.py`), with
  `pyproject.toml` (`pip install -e '.[dev]'`), a `TrntestConfig`/`load_config()` config module
  (TOML file + `TRNTEST_*` env vars) replacing scattered hard-coded constants (the Moon's radius was
  independently restated 3 times; `TARGET_FRAME_INDEX`/`IMAGE_SIZE`/`WAC_VIS_COLOR_FOV_DEG`/EDR-CDR
  product IDs were defined in whichever module happened to need them first and imported
  transitively elsewhere), and a `Session` facade (`trntest.Session`) so notebook cells read as
  near-one-liners. `run_sat_sim.sh` was retired in favor of direct `subprocess` calls in `render.py`
  (`lunaserv_result.txt` — the fragile Python-writes/bash-sources handoff file — is gone with it).
  `build_camera_from_spice.EdrInfo`/`fetch_edr_label()` renamed to `camera.FrameTiming`/
  `fetch_frame_timing()` (naming only — see `camera.py`'s docstring for why "EDR" was misleading:
  it's used only for frame timing metadata, never pixel data, which comes from the CDR product).
  Fixed a real duplicate-computation bug found while porting: `fetch_lunaserv.fetch_dem_and_ortho()`
  used to call `build_camera_from_spice.build()` internally, so the notebook silently computed the
  camera pose twice per run; `lunaserv.fetch_dem_and_ortho()` now takes the already-built `Camera`
  as a parameter instead. Added a `tests/` suite (pytest) for the pure/deterministic logic, an
  MIT-0 `LICENSE`, and automated style checking (`ruff` format+lint+import-sort, `mypy`, composed by
  the `trntest-lint` console script and a `githooks/pre-commit` hook — see `README.md`). `cache/`
  and `output/` moved fully outside this repo (siblings of the outer workspace's `src/`, ROS-
  workspace-inspired out-of-source layout — see `docker/docker-compose.yml`'s volume mounts). The
  notebook's outputs are now stripped from version control (`nbstripout`); a rendered HTML copy is
  published separately to GitHub Pages instead (see README's "Viewing the rendered demo").

## All phases complete

The demo is done end-to-end: real LRO SPICE trajectory → posed synthetic camera → `sat_sim`
render + CSM/ISD sidecar → compared (with explicit SPICE-derived tie points, `tie_points.py`)
against a properly band-separated, correctly-sized, correctly-posed crop of real WAC data, all
reproducible from the checked-in Dockerfile, and now packaged as an installable library (`trntest`)
with config, tests, and style tooling. See `notebooks/lunar_sat_sim_demo.ipynb` for the walkthrough
and `README.md` to run it.

## Known open items (resolve as encountered, record findings in `docs/data-sources.md`)

- Whether `--save-as-csm` state JSON is an acceptable stand-in for a literal ISD file for whatever
  comes after this demo.
- Exact NAIF kernel filenames/date-range for the chosen EDR timestamp.
- Confirm the lunar frame kernel defining `MOON_ME` loads correctly so SPICE can output that frame
  directly; sanity-check against the known GLD100/LOLA convention.
- Whether Lunaserv's native projection is directly usable by `sat_sim` or a reprojection step is
  actually required after all.
