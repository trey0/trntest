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

- [x] **Phase 8 — Catalog-driven dataset selection/generation.** Replaced the single hardcoded
  demo EDR (`M1329714703CE`, framelet 440 chosen by hand-inspecting that one product's shadow
  pattern — see Phase 2 above) with a repeatable, programmatic pipeline for building a much bigger
  dataset of real WAC images with favorable illumination:
  1. **`src/trntest/illumination.py`** (new): Sun/orbit geometry via real SPICE functions, not
     hand-rolled vector math — `sun_elevation_deg()` (`spice.ilumin`, method `"ELLIPSOID"`, the real
     PCK reference ellipsoid already loaded, deliberately **not** forced to a sphere — close enough
     that forcing them to match isn't worth a kernel-pool mutation), `sub_solar_lonlat_deg()`
     (`spice.subslr`), `spacecraft_lonlat_deg()` (pure `spkezr`+`reclat`, no shape model needed),
     `terminator_offset_deg()` (pure, our own derived ">=30° off the terminator" criterion),
     `find_sign_change_crossings()`/`find_ascending_node_crossings()` (generic bisection to find
     real ascending-node epochs from actual SPICE trajectory data, no assumed orbital period).
  2. **`src/trntest/catalog.py`** (new): PDS Geosciences Node Orbital Data Explorer (ODE) REST API
     client (`https://oderest.rsl.wustl.edu/live2/`) — confirmed `EDR_PRODUCT_TYPE = "EDRWAC4"`,
     `CDR_PRODUCT_TYPE = "CDRWAC4"` live via the `query=iipt` discovery endpoint. `list_products()`
     returns a `pandas.DataFrame` (paginated), `find_matching_cdr()` looks up each EDR's CDR
     counterpart by orbit number. Reuses `cache.cached_get` for ODE responses, same as everything
     else.
  3. **`src/trntest/dataset.py`** (new): the public API.
     - `select_dataset(config=None, search_start=..., num_orbits=12, min_lan_offset_deg=30.0,
       throttle_minutes=5.0, min_sun_elevation_deg=10.0, max_search_days=30.0,
       min_images_per_orbit=None) -> pd.DataFrame` — finds ascending-node crossings, keeps only
       those >=30° off the terminator (so either the ascending or descending pass is well-lit),
       evaluates every geometry-valid `num_orbits`-orbit window in the search range, and picks the
       one with the **highest minimum per-orbit illuminated-image count** (not just the first window
       clearing a fixed floor — exhaustive search over the whole range, since both the ODE query and
       the per-candidate SPICE check are cheap). Each candidate's illumination is checked at its
       product's own **temporal midpoint** (`nframes/2`, not framelet 0 — a stable, product-relative
       anchor instead of a hand-tuned offset; empirically validated: SPICE-computed midpoint ground
       point matches ODE's own reported `Center_lat`/`Center_lon`/`Incidence_angle` closely).
       Unilluminated products (sun elevation <10° there, `"ELLIPSOID"` model) are excluded outright,
       never searched for a better internal offset. Then throttled to one image per 5 real minutes
       (`throttle_by_time`) and matched to a CDR counterpart.
     - `generate_dataset(images, config=None, limit=None, output_dir=None) -> list[GenerationResult]`
       — runs the *existing, unmodified* single-image pipeline (`camera.build_camera` →
       `lunaserv.fetch_dem_and_ortho` → `render.run_sat_sim`) per selected row, into its own
       `output_dir/<product_id>/` subdirectory (avoids output-filename collisions across images).
       `limit` throttles how many rows are actually processed, for cheap testing (`n=1`, `n=3`)
       before a full run; per-image failures are caught/logged, not fatal to the batch.
     - Both are **plain importable Python functions, not CLI scripts** (explicit preference — no
       argparse, no `[project.scripts]` entries), also exposed as `Session.select_dataset()`/
       `Session.generate_dataset()` one-line delegators.
  4. **Notebook rewire** (`notebooks/lunar_sat_sim_demo.ipynb`): now sources its image via
     `images = session.select_dataset(max_search_days=7)` then
     `results = session.generate_dataset(images, limit=1)` instead of one hardcoded EDR — this
     retires the framelet-440-specific logic from the notebook's main path entirely.
     `camera.py`'s module docstring was corrected to stop framing framelet 440 as a general
     illumination-avoidance strategy (it's specific to one product's history) and points here
     instead.
  5. New dependency: **`pandas`** (explicit, discussed exception to this project's previously
     pandas-free stance — simplifies per-orbit grouping/throttling/manifest I/O and gives free,
     readable table display of `select_dataset()`'s result as a notebook cell's own output).
  6. **Two real bugs found and fixed** while verifying this end-to-end against many real orbit
     passes (not just one product) — see `docs/data-sources.md` for the full write-ups:
     - **SPICE kernel-pool exhaustion** (`SPICE(NOMOREROOM)`/`KERNELPOOLFULL`): `spice.furnsh()`
       does **not** dedupe repeat loads of the same kernel file across separate calls — each call
       consumes a fresh, limited KEEPER slot (~5300 max). `illumination.find_ascending_node_crossings`
       calling `spice_kernels.fetch_and_furnish` once per sampled epoch (thousands of times over a
       multi-day search) exhausted this fast. Fixed in `spice_kernels.py` by tracking every
       currently-furnished local path (`_loaded_kernels`) and skipping `furnsh()` for paths already
       loaded, plus unloading superseded date-ranged (CK/SPK) kernels when the target date moves to
       a different chunk.
     - **Antimeridian longitude-wraparound bug**: `lunaserv.py`'s `footprint_bbox_deg()` took a
       naive `min()`/`max()` of footprint corner longitudes; for a near-polar footprint straddling
       ±180°, this produced a ~360°-wide bbox instead of the true few-degree span on the other side,
       inflated further by padding into a bogus 567°-wide, 57225px-wide WMS request that timed out.
       Fixed by unwrapping corner longitudes onto a common branch (relative to the first corner)
       before taking min/max — confirmed empirically that Lunaserv's WMS handles an out-of-range
       bbox (e.g. `170,...,190`) correctly, returning the same real pixel data as the equivalent
       in-range form (`-190,...,-170`), so no request-splitting is needed.
  7. **Verified**: offline (`ruff format --check`/`ruff check`/`mypy`/`pytest`, 72 tests) and, inside
     Docker: `select_dataset(max_search_days=7)` (twice consecutively, confirming the kernel-pool fix
     — both runs returned 81 images across 12 orbits, 7/orbit), `generate_dataset(limit=1)` and
     `generate_dataset(limit=3)` (including a previously-failing near-polar image, confirming the
     antimeridian fix), and a full `jupyter nbconvert --to notebook --execute` run (all cells pass,
     ~9 min wall time). Committed and pushed as `6e90d82`.

## Phases 1-9 complete

The demo runs end-to-end: real LRO SPICE trajectory → posed synthetic camera → `sat_sim`
render + CSM/ISD sidecar → compared (with explicit SPICE-derived tie points, `tie_points.py`)
against a properly band-separated, correctly-sized, correctly-posed crop of real WAC data, all
reproducible from the checked-in Dockerfile, and packaged as an installable library (`trntest`)
with config, tests, and style tooling, now with a repeatable catalog-driven dataset
selection/generation capability (Phase 8). See `notebooks/lunar_sat_sim_demo.ipynb` for the
walkthrough and `README.md` to run it. Phase 9 (below) found and fixed a real, pass-dependent
mirror bug in the WAC CDR comparison, uncovered by Phase 8's move to catalog-driven, multi-product
selection (the single hand-picked demo product had never exercised this).

## Phase 9 (complete) — WAC CDR was mirrored (not rotated) relative to the synthetic image, on some passes

Found by manually reviewing the notebook after Phase 8: in the Phase 5 comparison figure, the real
WAC CDR panel looked **vertically flipped** relative to the synthetic image on the newly-selected
product `M1327210646CE` — and this was **not** explainable as any 90°-multiple rotation. There was
also a separate-but-related symptom: visible discontinuities in the WAC CDR panel landing on
framelet (14-line VIS block) boundaries. Tie points lined up with the underlying (wrong) image data
the same way across both images, so they didn't reveal anything by themselves (they reuse `wac.py`'s
own row-stacking convention, so they can't catch a bug baked into an assumption they themselves
reuse).

**A wrong first fix, caught by the user.** The first hypothesis (mine) was that this was actually a
*rotation*: `camera.py`'s `SENSOR_MODEL_BORESIGHT_ROTATION_K` comment and
`docs/data-sources.md`'s "Fixed sensor-model axis convention" section had asserted "forward in time
is `-X`" in the raw WAC-VIS frame is a **hardware/data-format property, not pass-dependent** — but
that claim was derived and verified against exactly **one** product and never re-tested until
`generate_dataset()` (Phase 8) started producing new ones. Directly measuring it via real SPICE
trajectory data confirmed the sign really is pass-dependent (dominant `+X` for the new product vs.
`-X` for the original) — but a fix built purely from that (`camera.boresight_rotation_k`, computed
per-pose instead of hardcoded) only ever changes *rotation* choices (`rotation_about_boresight`/
`np.rot90`, always determinant +1 at every layer of this pipeline) and **the user correctly pointed
out this could never produce or repair a genuine mirror** — a real, structural limitation, not
something more testing of the rotation-only fix could have found.

**The decisive test**: comparing the **sign of a 2D signed area** of a 3-point tie-point triangle
in the synthetic image's pixel space vs. the real crop's pixel space — using the existing,
already-independent SPICE-based tie-point projections, with no display-rotation logic involved at
all. Same sign means the two pixel spaces are related by a rotation; opposite sign means a genuine
mirror. Result: the original demo product's signs matched (no mirror, consistent with Phase 5's
verification); the flagged product's signs were **opposite** — a real, definitively-confirmed
mirror.

**Actual root cause**: WAC's raw camera frame is body-fixed (no gimbal), and LRO performs periodic
180°-yaw-flip maneuvers (the two products are ~26 days apart, consistent with a flip having occurred
between them). `wac.fetch_vis_mosaic`'s frame-stacking order is always correct *in time* (frames are
read off disk in strict acquisition order, unconditionally) — but a yaw flip rotates the *entire*
raw camera frame together, changing the resulting mosaic's **chirality** relative to the
always-proper synthetic image, not just its rotation. This confirms the plan's original "framelet
order reversed" hypothesis from the user, just precisely conditioned on a real, per-pass SPICE
measurement rather than assumed universally true or false.

**The fix**: `camera.Camera.reverse_crop_along_track` (derived from `boresight_rotation_k`) tells
`wac.fetch_vis_mosaic` to reverse along-track frame-stacking order (`vis[::-1]`) when this pass's
real ground-track direction doesn't match the original reference convention — a genuine mirror,
unlike anything a rotation constant could produce. `tie_points.py` and `orientation.py` were updated
to stay consistent with whichever stacking order `wac.py` actually used for a given pose
(`crop_footprint_corners` needed no change — it's pure ground geometry, independent of pixel
row/reversal).

**Verified**: the same signed-area chirality check, re-run after the fix, now shows matching signs
for *both* products (mirror gone in both cases — confirmed by direct computation, not just visual
impression). `trntest-lint` and the full test suite (73 tests, including a new regression test for
the reversal behavior) pass. A full `jupyter nbconvert --to notebook --execute` run on the flagged
product's default selection shows the comparison figure's two panels now showing genuinely matching
terrain (same large crater, same bright diagonal feature), with all 5 tie-point markers landing on
the correct matching features in both panels.

See `docs/data-sources.md`'s "Fixed: WAC CDR mirror relative to synthetic image (pass-dependent
chirality)" section (right after "North-up display rotation") for the full derivation, including the
exact chirality-check numbers before and after the fix.

## Known open items (resolve as encountered, record findings in `docs/data-sources.md`)

- Whether `--save-as-csm` state JSON is an acceptable stand-in for a literal ISD file for whatever
  comes after this demo.
- Exact NAIF kernel filenames/date-range for the chosen EDR timestamp.
- Confirm the lunar frame kernel defining `MOON_ME` loads correctly so SPICE can output that frame
  directly; sanity-check against the known GLD100/LOLA convention.
- Whether Lunaserv's native projection is directly usable by `sat_sim` or a reprojection step is
  actually required after all.
