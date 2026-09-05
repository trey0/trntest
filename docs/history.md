# Development history

Archival narrative of how this project reached its current state — phase by phase, including wrong
turns and how they were caught. This is background/curiosity reading, **not required before making
a change**: see `README.md` for current architecture/status and `docs/data-sources.md` for
current reference facts. Nothing here should be treated as describing present-day behavior unless
the current docs also say so.

## Phase-by-phase history

- **Phase 0 — Repo & Docker scaffolding.** `AGENTS.md`, `docs/*.md`, `docker/Dockerfile`,
  `docker/docker-compose.yml`, `.dockerignore`, `README.md`. Docker Engine installed on host.
- **Phase 1 — Base image.** GDAL 3.12.2 + ASP 3.7.0 (prebuilt Linux x86_64 binary, in
  `/opt/StereoPipeline`) + Python stack installed with `uv` into a venv at `/opt/venv`
  (jupyterlab, numpy, matplotlib, rasterio, requests, spiceypy) — a venv is needed because apt's
  system Python conflicts with installing numpy alongside it. Smoke tested: `sat_sim` and
  `gdalinfo --version` both run inside the container.
- **Phase 2 — WAC EDR selection + SPICE pose.** Chosen product `M1329714703CE` (see
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
- **Phase 3 — Lunaserv fetch.** `scripts/fetch_lunaserv.py` re-derives the Phase 2 footprint,
  pads it 30%, computes a physically-square pixel grid (accounting for `cos(lat)` near the pole),
  and fetches `luna_wac_global` + `luna_wac_dtm_numeric_meters_absolute` at ~100 m/px via
  `srs=IAU2000:30100` (confirmed native/unprojected — see `docs/data-sources.md`). Converts the DEM
  from planetocentric radius to elevation (subtract 1737400 m — see the "gotcha" in
  `docs/data-sources.md`), then hole-fills with `dem_mosaic` (no actual holes were present for this
  ROI — 100% valid pixels). Output: `output/dem_filled-tile-0.tif` + the cached ortho tif, identical
  size/origin/pixel-size. **Checkpoint passed:** `sat_sim` with its own auto-generated 2-camera
  track rendered two 256x256 `.tif`s successfully against this DEM/ortho pair.
- **Phase 4 — Render.** `scripts/run_sat_sim.sh`: `sat_sim --camera-list` (containing the
  Phase 2 `.tsai`) renders `output/render/run-camera_frame440.tif`. `--save-as-csm` turned out to be
  a no-op in `--camera-list` mode (see `docs/data-sources.md`) — the CSM/"ISD" JSON sidecar is
  instead produced by `cam_gen --input-camera ... --refine-intrinsics none`, which also
  independently re-derived the same sub-spacecraft geodetic position from the `.tsai`'s ECEF pose,
  cross-validating Phase 2's SPICE computation. **Bug found and fixed:** the script originally
  picked the ortho tile via `ls .../luna_wac_global/*.tif | head -1`, which silently grabbed a
  stale tile once the cache held tiles for more than one footprint (see Phase 5) — now
  `fetch_lunaserv.fetch_dem_and_ortho()` writes the exact paths it used to
  `output/lunaserv_result.txt`, which this script sources instead of globbing.
- **Phase 5 — Real-image comparison (revised many times).** Went through several iterations to get
  a comparison that's actually recognizable:
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
     synthetic render (same bright diagonal feature, same dark crater) — verified by eye. Full
     technical derivation: "Real image comparison" under "Historical derivations," below.
  4. **Square-crop sizing revision:** the synthetic camera's FOV and the CDR crop's frame count
     were originally sized independently (fixed ~100 m/px GSD target; fixed 19 frames chosen ad
     hoc) and didn't reliably cover the same real ground area. Fixed by grounding both in the real
     WAC color-mode FOV (61.4°, from the SIS). Full derivation below ("Square-crop sizing").
  5. **Pose epoch fix:** the synthetic camera's pose was at the crop's *start* frame (440), not its
     *middle*. Fixed: pose now derives `center_frame_index = TARGET_FRAME_INDEX + n_frames/2` and
     poses the camera there instead. Full derivation below ("Pose epoch fix").
  6. **Comparison-figure aspect ratio:** the CDR crop's non-square pixel array (994x704) was
     displayed by `imshow()` as a tall rectangle despite covering a square ground area. Fixed by
     plotting both panels with `extent=` in real km instead of raw pixel index.
  7. **SPICE-derived tie points** (`scripts/tie_points.py`): 5 points (die's "5"/X pattern) placed
     in the ground area both images share, projected into each image's real pixel coordinates.
     Full derivation, including a bisection bug found and fixed, below ("SPICE-derived tie points").
  8. **Fixed sensor-model axis convention:** the synthetic camera's pixel axes were an arbitrary
     in-house choice, causing a ~90° mismatch against the real crop. Fixed with a boresight rotation
     of `R` so `px`=cross-track, `py`=along-track. Full derivation below ("Sensor-model axis
     convention"). **Note**: this section's original framing (`k=1` as a fixed hardware constant)
     turned out to be wrong — see Phase 9 below.
  9. **North-up display rotation** (`scripts/display_orientation.py`): kept separate from the
     previous fix — notebook-display-only, doesn't touch the sensor model. Full derivation below
     ("North-up display rotation").
- **Phase 6 — Notebook.** `notebooks/lunar_sat_sim_demo.ipynb` drives all the `scripts/`
  modules end to end: SPICE pose → Lunaserv DEM/ortho → footprint-over-mosaic plot → `sat_sim`
  render + `cam_gen` CSM/ISD JSON → real WAC CDR band-separated comparison. Verified with
  `jupyter nbconvert --to notebook --execute` inside the container — runs top to bottom with no
  errors, 4 figures render, comparison figure visually confirmed recognizable. Checked in with
  outputs baked in, so it's viewable without re-running. Open via Jupyter Lab (`docker compose up`,
  port-mapped per the user's preference — see README).
- **Phase 7 — Package restructuring.** Converted the flat `scripts/` layout into an installable
  package: `src/trntest/` (src-layout — `cache.py`, `spice_kernels.py`, `camera.py`, `lunaserv.py`,
  `wac.py`, `tie_points.py`, `orientation.py`, `render.py`, `plotting.py`, `session.py`), with
  `pyproject.toml` (`pip install -e '.[dev]'`), a `TrntestConfig`/`load_config()` config module
  (TOML file + `TRNTEST_*` env vars) replacing scattered hard-coded constants, and a `Session`
  facade (`trntest.Session`) so notebook cells read as near-one-liners. `run_sat_sim.sh` was retired
  in favor of direct `subprocess` calls in `render.py`. `build_camera_from_spice.EdrInfo`/
  `fetch_edr_label()` renamed to `camera.FrameTiming`/`fetch_frame_timing()` (naming only — it's used
  only for frame timing metadata, never pixel data, which comes from the CDR product). Fixed a real
  duplicate-computation bug found while porting: `fetch_lunaserv.fetch_dem_and_ortho()` used to call
  `build_camera_from_spice.build()` internally, so the notebook silently computed the camera pose
  twice per run; `lunaserv.fetch_dem_and_ortho()` now takes the already-built `Camera` as a parameter
  instead. Added a `tests/` suite (pytest), an MIT-0 `LICENSE`, and automated style checking (`ruff`
  format+lint+import-sort, `mypy`, composed by the `trntest-lint` console script and a
  `githooks/pre-commit` hook). `cache/`/`output/` moved fully outside this repo (siblings of the
  outer workspace's `src/`, ROS-workspace-inspired out-of-source layout). The notebook's outputs are
  stripped from version control (`nbstripout`); a rendered HTML copy is published separately to
  GitHub Pages instead.
- **Phase 8 — Catalog-driven dataset selection/generation.** Replaced the single hardcoded demo EDR
  (`M1329714703CE`, framelet 440 chosen by hand-inspecting that one product's shadow pattern) with a
  repeatable, programmatic pipeline for building a much bigger dataset of real WAC images with
  favorable illumination:
  1. **`src/trntest/illumination.py`** (new): Sun/orbit geometry via real SPICE functions —
     `sun_elevation_deg()` (`spice.ilumin`), `sub_solar_lonlat_deg()` (`spice.subslr`),
     `spacecraft_lonlat_deg()` (`spkezr`+`reclat`), `terminator_offset_deg()` (our own derived
     ">=30° off the terminator" criterion), and (originally) `find_sign_change_crossings()`/
     `find_ascending_node_crossings()` — a generic bisection to find real ascending-node epochs
     from actual SPICE trajectory data. **Superseded in Phase 10** (below) by a native SPICE
     geometry-finder call; the bisection approach is kept here only as history.
  2. **`src/trntest/catalog.py`** (new): PDS Geosciences Node Orbital Data Explorer (ODE) REST API
     client — confirmed `EDR_PRODUCT_TYPE = "EDRWAC4"`, `CDR_PRODUCT_TYPE = "CDRWAC4"` live via the
     `query=iipt` discovery endpoint. `list_products()` returns a `pandas.DataFrame` (paginated),
     `find_matching_cdr()` looks up each EDR's CDR counterpart by orbit number.
  3. **`src/trntest/dataset.py`** (new): `select_dataset()`/`generate_dataset()`, the public API —
     see `docs/plan.md` for what these do today. New dependency: **`pandas`** (explicit, discussed
     exception to this project's previously pandas-free stance).
  4. **Notebook rewire**: now sources its image via `select_dataset()`/`generate_dataset()` instead
     of one hardcoded EDR, retiring the framelet-440-specific logic from the notebook's main path.
  5. **Two real bugs found and fixed** while verifying this end-to-end against many real orbit
     passes (not just one product):
     - **SPICE kernel-pool exhaustion** (`SPICE(NOMOREROOM)`/`KERNELPOOLFULL`): `spice.furnsh()`
       does **not** dedupe repeat loads of the same kernel file across separate calls — each call
       consumes a fresh, limited KEEPER slot (~5300 max). The original `find_ascending_node_crossings`
       calling `fetch_and_furnish` once per sampled epoch (thousands of times over a multi-day
       search) exhausted this fast. Fixed in `spice_kernels.py` by tracking every currently-furnished
       local path (`_loaded_kernels`) and skipping `furnsh()` for paths already loaded, plus
       unloading superseded date-ranged (CK/SPK) kernels when the target date moves to a different
       chunk — this tracking mechanism is still current (see `docs/data-sources.md`).
     - **Antimeridian longitude-wraparound bug**: `lunaserv.py`'s `footprint_bbox_deg()` took a
       naive `min()`/`max()` of footprint corner longitudes; for a near-polar footprint straddling
       ±180°, this produced a ~360°-wide bbox instead of the true few-degree span on the other side,
       inflated further by padding into a bogus 567°-wide, 57225px-wide WMS request that timed out.
       Fixed by unwrapping corner longitudes onto a common branch before taking min/max.
  6. **Verified**: offline (`ruff format --check`/`ruff check`/`mypy`/`pytest`, 72 tests) and, inside
     Docker: `select_dataset(max_search_days=7)` (twice consecutively, confirming the kernel-pool fix
     — both runs returned 81 images across 12 orbits, 7/orbit), `generate_dataset(limit=1)` and
     `generate_dataset(limit=3)` (including a previously-failing near-polar image, confirming the
     antimeridian fix), and a full `jupyter nbconvert --to notebook --execute` run (all cells pass,
     ~9 min wall time). Committed and pushed as `6e90d82`.

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
*rotation*: `camera.py`'s `SENSOR_MODEL_BORESIGHT_ROTATION_K` comment and `docs/data-sources.md`'s
sensor-model-axis-convention section had asserted "forward in time is `-X`" in the raw WAC-VIS frame
is a **hardware/data-format property, not pass-dependent** — but that claim was derived and verified
against exactly **one** product and never re-tested until `generate_dataset()` (Phase 8) started
producing new ones. Directly measuring it via real SPICE trajectory data confirmed the sign really
is pass-dependent (dominant `+X` for the new product vs. `-X` for the original) — but a fix built
purely from that (`camera.boresight_rotation_k`, computed per-pose instead of hardcoded) only ever
changes *rotation* choices (`rotation_about_boresight`/`np.rot90`, always determinant +1 at every
layer of this pipeline) and **the user correctly pointed out this could never produce or repair a
genuine mirror** — a real, structural limitation, not something more testing of the rotation-only
fix could have found.

**The decisive test**: comparing the **sign of a 2D signed area** of a 3-point tie-point triangle
in the synthetic image's pixel space vs. the real crop's pixel space — using the existing,
already-independent SPICE-based tie-point projections, with no display-rotation logic involved at
all. Same sign means the two pixel spaces are related by a rotation; opposite sign means a genuine
mirror. Result: the original demo product's signs matched (no mirror, consistent with Phase 5's
prior verification); the flagged product's signs were **opposite** — a real, definitively-confirmed
mirror. Exact numbers: original product synthetic area `9630.58` / crop area `102818.91` (same
sign); flagged product synthetic area `15352.84` / crop area `-166560.33` (opposite sign, before the
fix) → `166560.33` (same sign, after the fix).

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

The resulting *current* behavior (`boresight_rotation_k`, `reverse_crop_along_track`) is documented
as a present-tense fact in `docs/data-sources.md`; this section is the "why"/"how it was found."

## Phase 10 (complete) — `select_dataset()` sweep: ~500s → ~6s for a from-cold 7-day search

Motivated by wanting faster iteration on `select_dataset()`'s tuning parameters
(`min_sun_elevation_deg`, `num_orbits`, `min_lan_offset_deg`, `throttle_minutes`) — every call
redid all the sweep's work from scratch, and a from-cold call took ~500s (Docker,
`max_search_days=7`, 1633 EDR candidates evaluated). Rather than guessing which of several
proposed caching layers would matter most, profiled first (`cProfile`/`pstats` around a real
`select_dataset()` call) and let the numbers decide. Found two real, unrelated bugs, in order of
what a first-principles look and then profiling turned up:

1. **`illumination.find_ascending_node_crossings` replaced with SPICE's `gfposc`.** The original
   implementation sampled LRO's `MOON_ME`-frame latitude every 60s across the whole search window
   (~10,000 Python-level `spkezr`+`reclat` calls for a 7-day search) and bisected sign changes with
   a hand-rolled loop (`find_sign_change_crossings`) — despite this module's own stated philosophy
   of using real SPICE geometry functions, not hand-rolled vector math. `spice.gfposc` (geometry
   finder over position coordinates) does the same latitude=0 search natively, as a single call over
   the whole confinement window, evaluated by SPICE's own compiled adaptive search. Needs SPK
   coverage for the whole window furnished at once (new `spice_kernels.furnish_spk_range`) rather
   than the per-epoch just-in-time furnish/unload pattern `fetch_and_furnish` uses elsewhere for CK
   data — safe here since node-crossing search only needs SPK (trajectory), not CK (attitude, the
   kernel type that actually risks exceeding the kernel pool's buffer). Cross-checked against the
   old implementation before removing it: identical crossing counts, epochs agreeing to ~0.5s (well
   under the old implementation's own 1s bisection tolerance), for a real 2019-11-01, 7-day window.
   `find_sign_change_crossings` (now unused in production) and its 3 tests were removed.

2. **`spice_kernels.latest_metakernel_url`'s `functools.cache` was silently defeated.** It's
   memoized on `(year, config)` specifically to avoid re-hitting NAIF's live (uncached-by-design)
   `extras/mk/` directory listing once per date — its own docstring already called this out as
   necessary for exactly this sweep's "hundreds of candidate images in one process" case. But
   `dataset.evaluate_candidate_image` builds a fresh per-candidate config via `dataclasses.replace`
   (varying `edr_volume`/`edr_product`/etc — fields this function never reads), so every one of the
   1633 candidates got a distinct cache key and forced a real HTTP request: ~1600 requests, ~495s of
   the ~500s total, confirmed via `cProfile` (`latest_metakernel_url`/`requests.get` dominating
   cumulative time; `spice.furnsh()` itself only 0.178s across 16 calls, ruling out kernel-file-size
   as the cause). Fixed by keying the cache on `(year, naif_base_url: str)` instead of the whole
   config — `naif_base_url` is the only field the function actually reads, and it never varies
   per-candidate.

A methodological wrong turn along the way, worth remembering: the first profiling comparison (two
`select_dataset()` calls back-to-back in one script) made the *second* call look dramatically faster
than the first, and that difference was initially (wrongly) attributed to network/disk-cache
warmth, then to large CK kernel-file parsing — both ruled out by checking file mtimes and profiling
`spice.furnsh()` directly. The real cause was that both calls ran in the *same process*, so
process-global state (SPICE's furnished-kernel tracking, `functools.cache`) carried over from call 1
to call 2, making call 2 fast for reasons unrelated to disk caching. Isolating a true from-cold cost
requires a fresh process (a fresh `docker compose run`) for each measurement — see the "Profiling"
bullet in `AGENTS.md`.

**Verified**: `trntest-lint`/`pytest` (70 tests, post-removal) pass. Before/after `cProfile` of a
from-cold `select_dataset(max_search_days=7)` (fresh Docker container each time, same real network
+ SPICE): 502.1s → 5.9s, same 81-images-across-12-orbits result both times. Also found and fixed,
same pass: `diff.ipynb.textconv` had the same broken-container-path issue as the `nbstripout` clean
filter (see `AGENTS.md`) — `git diff` on any notebook silently showed no diff at all, even when one
existed. Further caching layers discussed but not implemented (`functools.cache` on
`camera.fetch_frame_timing`, splitting candidate evaluation from threshold filtering into a reusable
in-memory DataFrame) — deferred, since profiling showed ~2s of the ~6s from-cold total is
`fetch_frame_timing`'s XML parse and the rest is spread across many smaller costs; revisit if
repeat-call latency within one process still matters.

## Phase 11 (complete) — Replaced `nbstripout` with jupytext; dropped the GitHub Pages publish flow

Motivated by wanting notebook code under the same `ruff`/`mypy` bar as the rest of the repo (not
possible while the tracked notebook was raw `.ipynb` JSON) and by growing dissatisfaction with the
`gh-pages` branch/`git subtree push` machinery `scripts/publish_gh_pages.sh` used just to make a
rendered HTML copy viewable in a browser.

- **jupytext pairing.** `notebooks/lunar_sat_sim_demo.ipynb` was paired with a `.py:percent`
  twin (`jupytext --set-formats notebooks//ipynb,notebooks//py:percent`, inline metadata, no
  `jupytext.toml`) — verified byte-identical code/markdown content pre- and post-migration (only
  the new pairing-metadata line differed). `notebooks/lunar_sat_sim_demo.py` is now the file that
  gets edited, diffed, and linted; the `.ipynb` is regenerated from it.
- **Dropped `nbstripout` entirely**, including the docker-wrapped local git config it needed (see
  the old "Gotcha" this replaced) and `.gitattributes`.
- **Both notebook files are committed, with the `.ipynb` carrying real outputs** — realized
  partway through this work that GitHub already renders `.ipynb` files natively in its repo
  browser (markdown, code, and images) with no extra infrastructure, which made the entire
  Pages/`docs/rendered`/`gh-pages`-branch/subtree-push stack (introduced back in Phase 7)
  unnecessary. It existed purely to make HTML viewable, and `.ipynb` doesn't have that problem.
  Also realized the "committing outputs bloats history" concern this had originally seemed to
  avoid didn't actually hold: `docs/rendered/*.html` was already being committed straight to
  `main` with embedded output images, paying the same cost under a different filename. Deleted
  `scripts/publish_gh_pages.sh` and `docs/rendered/`.
- **`scripts/run_notebook.sh`** (new) replaces the old publish script: regenerates the `.ipynb`
  from the `.py` (`jupytext --to notebook`) and re-executes it in place
  (`jupyter nbconvert --to notebook --execute --inplace`). Deliberately doesn't auto-commit,
  unlike the old script — review and commit both files normally.
- **`trntest-lint` gained a "notebook sync" check** (`src/trntest/_lint.py`), gated on staged
  `notebooks/*.py`/`*.ipynb` files, read-only (never mutates files, only reports and points at the
  fix — same philosophy as `ruff format --check`): (1) a `.py`/`.ipynb` pair must be staged
  together, (2) their code/markdown content must match (compared via `jupytext --to py:percent`
  written to a real file in the same directory, not `--output -`/stdout — piping to stdout was
  tried first and silently drops the pairing-metadata header line jupytext otherwise writes,
  which produced a false-positive divergence on every run), and (3) the `.ipynb`'s
  `execution_count`s must be exactly `1, 2, 3, ...` with no gaps, a proxy for "produced by a real
  `run_notebook.sh` run" rather than ad-hoc interactive cell tinkering. What this check
  deliberately does **not** attempt: verifying output *freshness* (that outputs reflect the
  current code) — that needs an actual re-execution, too slow for a pre-commit hook given the
  SPICE/WMS/`sat_sim` calls involved, so it stays a documented discipline
  (`scripts/run_notebook.sh` before every notebook commit) rather than a hard guarantee.
  Git filters (what `nbstripout` used) were considered and ruled out for this whole check: a
  clean/smudge filter transforms only the single file being staged and has no way to read or
  write a second, paired file, so cross-file sync checking structurally has to be a hook, not a
  filter.
- Also found, while making `ruff format --check` pass on the migrated `.py`: it strips trailing
  semicolons as "redundant" regardless of `ruff check` per-file-ignores (those don't affect the
  formatter) — but a trailing `;` is exactly how Jupyter/IPython suppresses auto-display of a
  cell's last expression (e.g. hiding a plot call's `Axes` repr), so it's meaningful here, unlike
  in ordinary scripts. Notebook `.py` files are now linted (`ruff check`, with `E501`/`B018`/`E703`
  per-file-ignores for markdown-prose line length, Jupyter's bare-expression display idiom, and
  semicolon suppression respectively) but excluded from `ruff format --check`.

**Verified**: `trntest-lint --all` passes; the paired-staged, structural-sync, and
execution-count checks were each deliberately triggered (unpaired staging, a hand-edited `.py`,
a hand-swapped `execution_count`) and confirmed to block with the documented fix command, then
confirmed to pass again once fixed; `scripts/run_notebook.sh` was run end-to-end against a
warm cache (~24s) and produced a clean, sequentially-numbered notebook; JupyterLab's contents API
was queried directly and confirms it serves `notebooks/lunar_sat_sim_demo.py` with
`"type": "notebook"` (via the bundled `jupyterlab-jupytext` extension), i.e. it opens as a live
notebook, not a text file.

## Phase 12 (spike, inconclusive — no `trntest` code changed) — ISIS/CSM real-WAC DEM reprojection

Motivated by reflecting on how much hand-derived, footgun-prone logic `wac.py`'s framelet
handling required (the Phase 9 mirroring bug) and a bigger goal beyond just fixing that: reproject
a real WAC CDR swath onto the DEM via a genuine camera model (ASP `mapproject`) and re-render it
from a chosen synthetic pose (`sat_sim --ortho`), instead of `wac.py`'s manual pixel-stacking
approach — which needs a real ISD/camera model for the *actual* WAC data, something `wac.py` never
had. Research found this is ASP's own flagship documented example for CSM `Pushframe` sensor
support, written specifically for WAC.

Full technical findings (install recipe, exact commands, measured sizes/timings, both spike runs)
are in `docs/data-sources.md`'s "ISIS3/CSM spike" section — this entry is the narrative pointer to
that. Summary of what was learned, run entirely in a throwaway Docker container against two real
reference products (`M1329714703CE`/440, the old hand-picked product, and `M1327210646CE`/94, the
product the live `select_dataset()` path actually currently selects):

- The full chain (`lrowac2isis` → `spiceinit web=yes` → `lrowaccal` → `isd_generate` → `mapproject
  -t csm` → `sat_sim --ortho`) works end-to-end on real data from this repo's own reference
  products, confirmed twice.
- **The user's original concern (large ISIS data download) is resolved**: `spiceinit web=yes`
  works for WAC (not just NAC, which is all the docs confirm) with **zero local kernel files** —
  confirmed directly, not just assumed from docs.
- **`framestitch`'s `FLIP` parameter was cross-validated against `camera.boresight_rotation_k`
  twice**, on two products with opposite yaw states, and matched both times — real evidence that
  ISIS's manual flag tracks the exact same physical phenomenon this repo's SPICE-derived code
  already computes automatically.
- **The blocker**: real, severe periodic striping at framelet boundaries in the `mapproject`
  output, on both products — confirmed this is a structural/geometric artifact in ASP's own
  (self-described "not fully mature") CSM Pushframe stitching, not an illumination or AOI-sizing
  problem (the second, brighter, correctly-sized-AOI run showed it just as badly, covering ~80% of
  the frame). This is the open blocker for using this pipeline as a `wac.py` replacement.

**Status / next step for whoever picks this up**: not yet a viable `wac.py` replacement as-is.
Unresolved: whether the striping is fixable (tighter `num_lines_overlap` tuning, ASP's own
suggested low-res-DEM mitigation, or some other calibration/geometry correction) or is a hard
limitation of ASP's current Pushframe CSM support. No `trntest` source was changed by this spike —
picking it back up means either continuing the artifact investigation (start from the "ISIS3/CSM
spike" section in `docs/data-sources.md`) or deciding to shelve the idea and keep `wac.py` as-is.

## Phase 13 (2026-08-05, spike, real code now — branch `spike/wac-isis-framestitch`) — ISIS/CSM WAC reprojection: first executable framestitch investigation notebook

Phase 12's throwaway spike container was lost when the VPS hosting it was torn down without being
saved — the findings survived (they'd been written up first), but the environment/code didn't.
This phase rebuilds it as real, reusable, checked-in code (not a throwaway container) — the first
time this spike has touched actual `trntest` source rather than just docs — landed on a branch, not
`main`, since it's still unproven and adds a heavy new toolchain. Scope is deliberately narrower
than Phase 12's full chain: stop after `framestitch` (the step suspected, per the working
hypothesis, of introducing Phase 12's still-unresolved framelet-boundary striping), with a real
inline image displayed after every step — Phase 12's investigation had been badly slowed by not
being able to see intermediate steps at all.

- **ISIS + ASP now coexist in one Docker image** (`docker/Dockerfile`, not a second
  container/service): ISIS/ALE installed via a `micromamba`-managed conda env
  (`/opt/conda/envs/isis`) alongside the existing `uv` venv and ASP's own binary install, all three
  reachable via `PATH` for plain `subprocess.run([...])` calls from one Python process. A second
  container was considered and rejected — the eventual goal is a notebook that walks through *both*
  ISIS's steps and ASP's `mapproject`/`sat_sim` on the result, which two containers would make
  needlessly awkward (cross-container `docker exec` plumbing for no real benefit).
- **Corrected a wrong finding from Phase 12**: the "`--no-kernels` shrinks `base` to near-zero"
  claim was wrong — a real `downloadIsisData base $ISISDATA --no-kernels` run pulled **20 GB**
  (dominated by `base/dems/`, which isn't a "kernel" and isn't touched by that flag at all), and
  `spiceinit web=yes` still failed outright without a handful of tiny local kernels (confirmed:
  "Unable to load leadsecond file" with `base/kernels/lsk` empty) — contradicting Phase 12's "zero
  local kernel files" framing for `base` specifically (its `lro/kernels/` claim was and remains
  correct). Fixed with a narrow `--include` for just the small universal kernel subdirs (lsk/pck/
  sclk/fk/ik/iak) instead of `--no-kernels` — measured **~5 MB**, not 20+ GB. See
  `docs/data-sources.md`'s "ISIS3/CSM spike" section for the full correction.
- **Also found and fixed, mid-notebook**: ISIS's NULL/LRS/LIS/HIS/HRS special pixels are
  huge-magnitude (~±3.4e38) but *finite* float32 sentinels — `np.isfinite()` doesn't catch them, so
  a naive contrast stretch or row-mean reduction silently overflows/washes out to near-blank.
  `plotting.plot_raster()`'s new `valid_pixel_mask()` masks by magnitude threshold instead.
- **Net result**: `notebooks/wac_isis_spike.py` runs end-to-end for real (EDR fetch →
  `lrowac2isis` → `spiceinit web=yes` → `lrowaccal` → `framestitch`, `flip=False` for the default
  product `M1329714703CE`/440) and displays real, recognizable lunar terrain (visible craters) at
  every step, including a data-driven (not hardcoded) zoomed crop near real signal for inspecting
  the framelet seam. Whether the striping is actually visible/attributable to `framestitch`
  specifically is left for interactive follow-up in the notebook itself — not concluded by this
  phase.

**Status / next step for whoever picks this up**: the tooling now exists to actually see each
step, unblocking the investigation Phase 12 got stuck on. Next: use the notebook interactively to
inspect the stitched cube for the striping pattern at multiple locations/zoom levels, and decide
whether it's visible pre-`mapproject` (implicating `framestitch` itself) or only appears once
`mapproject` reprojects it (implicating that step instead, as Phase 12 originally assumed).

## Phase 14 (2026-08-05) — Merged ISIS/CSM WAC pipeline into `main`; added a live comparison to the demo notebook

Phase 13's `spike/wac-isis-framestitch` branch was merged into `main` — the framelet-boundary
striping question is still open (Phase 13's own next step wasn't attempted here), but the decision
was made to stop treating the pipeline as spike-only: `src/trntest/isis_wac.py` is real, working
code, and the best way to keep investigating the striping is to see its output directly in the
flagship demo notebook rather than only in a standalone investigation notebook.

- **Merge conflict**: `main` had independently added near-identical warning-suppression code to
  `src/trntest/plotting.py` (the notebook-warning-cleanup work, done after this branch diverged).
  Resolved by keeping Phase 13's `read_raster_band`/`valid_pixel_mask`/`plot_raster` in full and
  dropping `main`'s smaller, redundant `_open_rendered_tif` in favor of `read_raster_band`
  (`src/trntest/_lint.py`'s equivalent addition merged automatically clean — same content, entirely
  non-overlapping location).
- **Deduplicated `isis_wac.py`'s own `_run_quiet`** against `subprocess_utils.run_quiet` (added on
  `main`, after Phase 13 branched, so `isis_wac.py` couldn't reference it yet) — flagged as
  follow-up work when both were written; this merge is that follow-up.
- **Found and fixed a real image-bloat bug while rebuilding**: the merged image came out at
  **15.8 GB**, not the ~4 GB measured in Phase 13 — `/opt/conda/pkgs` (micromamba's
  download/extraction cache) was left uncleaned inside the image, roughly doubling the ISIS layer's
  real size (measured: ~2 GB env + ~6 GB uncleaned pkgs cache). Fixed by adding `micromamba clean
  --all --yes` to the *same* `RUN` layer as `micromamba create` (cleaning in a later layer doesn't
  shrink an earlier one — Docker's layered filesystem).
- **New reusable pipeline entry points** in `isis_wac.py`, added per code review rather than
  inlining the steps into the notebook cell directly: `run_pipeline(camera, frame_timing, config)`
  runs EDR-fetch-through-`framestitch` in one call, deriving `flip` from
  `camera.reverse_crop_along_track` (not a hardcoded per-product constant like the spike notebook's
  `FLIP = False` — this needs to work for whatever product `select_dataset()` live-selects, not
  just one fixed reference product). `crop_window_for_camera(camera)` picks the same real-footprint
  frame range (`camera.center_frame_index` ± half of `camera.n_frames_for_square_crop`) that
  `wac.fetch_vis_mosaic` already uses for its own Phase 5 crop — both images cover the same real
  ground area by construction (the synthetic camera's footprint is centered there in the first
  place), so unlike Phase 13's spike notebook, no data-driven "guess the signal-rich window"
  heuristic was needed at all.
- **New `plotting.plot_isis_comparison`**: synthetic render next to the ISIS-processed crop, side
  by side (not tie-pointed like `plot_comparison`'s `wac.py` version — the ISIS cube isn't
  reprojected onto the DEM yet, no `mapproject`).
- **Net result**: `notebooks/lunar_sat_sim_demo.py`'s new Phase 6 is two short cells
  (`isis_wac.run_pipeline(...)` then `plotting.plot_isis_comparison(...)`) — real, live-selected-
  product output, not the spike notebook's fixed default product.

**Correction, same day**: the first real run's Phase 6 panel was flagged (by inspection) as far too
short/non-square and showing no visible correspondence with the synthetic panel.
`crop_window_for_camera`'s assumption that the stitched cube is ~one line per original EDR frame
was wrong — checked directly against the cached cubes from that same run: `M1327210646CE` (the
product actually used) measures exactly `258 frames × 14 = 3612` lines, and `M1329714703CE` (the
older reference product, also cached) exactly `538 × 14 = 7532` — `lrowac2isis` preserves **14
lines/frame** (`wac.VIS_BLOCK_HEIGHT`), not 1. Fixed by scaling both `camera.center_frame_index`
and `camera.n_frames_for_square_crop` by that factor before computing the window. See
`docs/data-sources.md`'s "ISIS3/CSM spike" section for the full empirical detail.

**Second correction, same day**: `plot_isis_comparison` wasn't applying the north-up display
rotation or the real-km `extent` scaling `plot_comparison` already uses for its own panels (WAC's
along-track/cross-track pixel GSDs differ) — the synthetic panel showed ~180° rotated and the real
panel was visibly stretched. Fixed by reusing `rotations.k_synthetic`/`k_crop` (from the same
`compute_display_rotations` call Phase 5 already makes) and the same `extent=[0, width_km,
height_km, 0]` pattern. Also shortened the real panel's title (was too long to render cleanly).

**Third addition, same day**: considered generating a CSM/ISD sidecar for the stitched cube (via
ALE's `isd_generate`) to enable a tie-pointed comparison like Phase 5's, but this turned out to be
both unverified (the original spike's `isd_generate` recipe only ran against the *unstitched*
even/odd cubes, never `framestitch`'s merged output) and unnecessary: `tie_points.py`'s crop-pixel
projection was never CSM-based to begin with (pure SPICE frame-index geometry,
`project_ground_to_crop_pixel`), and its row/col origin (`start_frame=config.target_frame_index`)
and scaling (`wac.VIS_BLOCK_HEIGHT`) exactly match what `crop_window_for_camera` already computes.
So Phase 5's already-computed `tie_point_results` are reused as-is on the ISIS panel — no ISD
generation, no new geometry code. `plotting.py` gained a small shared
`_plot_tie_point_marker` helper (de-duplicating the rotate+km-scale+plot logic both comparison
functions now need) and lost the `config` parameter from `plot_comparison`/`plot_isis_comparison`
(both had only used it for `config.image_size`, now passed as a plain `width`/`height` to the
helper instead).

## Phase 15 (2026-08-06) — Fixed `sat_sim` render speckle: ortho source switch + despeckle + real-sun hillshade

The synthetic render showed sparse salt-and-pepper speckle (isolated bright/dark pixels,
concentrated near crater rims). Investigated and rejected several theories before landing on the
real cause and fix:

- **Ruled out the DEM**: `hole_fill_dem` changed 0 pixels for the test product — the DEM was clean.
- **Ruled out `sat_sim`'s ray/DEM-intersection tolerance** (`--dem-height-error-tol`): the artifact
  is salt-and-pepper (both bright and dark single pixels) in otherwise-smooth neighborhoods, not the
  shadow-acne/grazing-ray pattern that tolerance issue would produce.
- **Ruled out WMS nodata mishandling** — tested two theories, both wrong: the server's own
  `GetCapabilities` documents `0 = NoData` for this layer family (not white, as first guessed), and
  more directly, requesting the exact AOI with `transparent=true` (both `image/png` and
  `image/tiff` return a real alpha/mask band, confirmed by testing — not documented in the layer's
  own capabilities entry) showed **alpha=255 everywhere** — zero actual NoData in this AOI. The
  isolated bad pixels are genuinely part of the real `luna_wac_global` mosaic data, not missing data
  filled by any convention.
- **Root cause**: `luna_wac_global` (fetched raw and unfiltered in `lunaserv.py`, unlike the DEM
  which already gets `dem_mosaic --hole-fill-length`) is composited from ~15,000 individual WAC
  images with no evident per-pixel outlier rejection — ~16,000 isolated single-pixel outliers
  (0.235% of pixels, deviation up to ~50 DN, 91% genuinely isolated not blob-edges), most likely
  uncaught single-frame sensor/cosmic-ray noise.
- **Evaluated alternate Lunaserv layers empirically** (fetched real test tiles for the same AOI,
  not just read abstracts): `luna_wac_hapke_643nm` (median of ~140 repeat observations) has fewer
  outliers but is visibly blurrier and introduces its own large saturation blowout on one bright
  crater. `luna_wac_normalized_reflectance` (643nm, >100,000 images) has comparable resolution to
  `luna_wac_global` with ~4x fewer outliers (0.059%, still 91.6% isolated single pixels) — chosen as
  the new default (`config.lunaserv_ortho_layer`).
- **Considered ISIS `lrowaccal`'s `SpecialPixels` correction** (confirmed via its XML docs: a real,
  default-on, temperature/mode-matched known-bad-pixel mask) — confirmed inapplicable to the
  Lunaserv mosaic: it's keyed to raw EDR detector geometry that no longer exists once ASU
  composites/reprojects into the global mosaic. It's already correctly in use, by default, in the
  unrelated `isis_wac.py` EDR pipeline (`run_lrowaccal`, no flags overridden) — consistent with that
  pipeline's own comparison panel showing no similar speckle.
- **Considered ISIS `noisefilter`** (a generic boxcar-tolerance outlier filter, confirmed available
  via `std2isis`/`isis2std` round-trip) as a more "established" alternative to a hand-rolled numpy
  filter — decided against it: proportionate given this is a display/render-texture concern, not
  primary scientific analysis, and a round-trip through ISIS's environment/subprocess adds real
  overhead for no clear accuracy benefit over a filter already validated against this exact data.
- **Key discovery, found by reading `sat_sim`'s own docs before finalizing the fix**: `sat_sim`
  applies **no illumination model of its own** — it "unproject[s] an ortho image into a given
  camera... in the spirit of ISIS `map2cam`" via bicubic interpolation; the DEM is used purely for
  ray/terrain-intersection geometry, not shading. This means all of the *previous* render's apparent
  3-D relief came entirely from real (but arbitrary, uncontrolled, per-source-image) photographic
  shading baked into `luna_wac_global` — not from any physically accurate simulation of the target
  frame's real sun geometry. Switching to a flat/normalized-reflectance ortho would have made the
  synthetic render itself go flat, not just a raw display panel — so a hillshade has to be baked into
  the actual ortho fed to `sat_sim`.
- **Fix**: lit that hillshade with the **real SPICE sun geometry** for the target frame's actual
  acquisition epoch/AOI center, rather than an arbitrary fixed direction — added
  `illumination.sun_azimuth_elevation_deg` (real `spkpos` ephemeris vector projected into an exact
  local East-North-Up frame; SPICE has no single "local azimuth" convenience call). This is a real
  improvement over the old behavior, whose relief direction was an inconsistent patchwork across
  ~15,000 different source images' individual acquisition geometries.
- **Net result (this part)**: `lunaserv.py`'s `fetch_dem_and_ortho` now despeckles the fetched ortho
  (`despeckle`, a MAD-based local-outlier filter — flags a pixel only when it deviates from its own
  *locally smooth* 3x3 neighborhood, so real large features like the blown-out crater are untouched
  by design) and blends in the real-sun hillshade (`shade_ortho`, via
  `matplotlib.colors.LightSource` — pure numpy, no new subprocess/dependency), writing one canonical
  `ortho_shaded.tif` used by both `sat_sim` and every display panel. These are real, validated
  improvements to the ortho's quality on their own merits — but see the correction below: they
  turned out not to be the actual cause of the render speckle.

**Correction, same day**: re-ran the full pipeline end-to-end after the above and the speckle was
still there, essentially unchanged in position/density. Root-caused properly this time, by actually
testing each hypothesis against the real render rather than reasoning from the ortho's own
statistics:

- Despeckling the *already-shaded* ortho a second time (catching anything the hillshade computation
  itself might have introduced) changed real pixels in the ortho but **did not change the render at
  all** when re-rendered from the doubled-despeckled file.
- Added `--blur-sigma` (computed from the actual render/ortho GSD ratio, not guessed) on the theory
  that `sat_sim`'s un-anti-aliased bicubic resampling was aliasing fine real hillshade detail during
  the ~6x downsample from ortho resolution to render resolution. Tested directly: even a deliberately
  huge `--blur-sigma=8` (3x the computed value) visibly softened overall texture but **left the
  speckle dots completely unchanged** — ruling out aliasing as the mechanism.
- Went back to the very first theory from earlier in this investigation (dismissed at the time as "a
  stretch") and actually tested it directly: swept `--dem-height-error-tol` against the *same*
  DEM/ortho/camera. Loosening it (0.1, 0.5) **eliminated the speckle cleanly**; tightening it
  (0.00001) made it **dramatically worse** (many more, denser artifacts) — an unambiguous result in
  both directions, not a subtle one. This is a real DEM-precision issue: Lunaserv's DTM layer serves
  planetocentric radius (~1.7e6 m) as float32, whose ULP at that magnitude is already ~0.125m —
  baked into the source data before `radius_to_elevation` ever runs, not fixable by changing that
  subtraction's own precision. `sat_sim`'s default tolerance (0.001m) is ~100x tighter than the DEM
  can actually resolve, and its ray/DEM-intersection root-finder doesn't degrade gracefully at that
  mismatch.
- **Actual fix**: `render.py`'s `run_sat_sim` now passes `--dem-height-error-tol 0.5` (a 4x margin
  above the ~0.125m float32 floor). `--blur-sigma` was removed entirely — it had no measurable effect
  on the real problem and only added unnecessary softening.
- **Lesson**: the ortho-quality investigation (layer switch, despeckle, real-sun hillshade) was real,
  valuable, well-evidenced work — but none of it addressed the actual bug, because the bug was never
  in the ortho. Should have swept `--dem-height-error-tol` empirically at the very start (it was
  proposed early on) rather than reasoning about why it seemed unlikely.

## Phase 16 (2026-08-06) — Geo-aligned overlay visualization via `mapproject` round-trip

`plot_comparison`'s side-by-side panels are only aligned "in an ad hoc way" (real-km extent + a
north-up display rotation, not true pixel-for-pixel geo-registration). Added a genuine overlay
visualization instead: reproject an image back onto the map with ASP `mapproject`, through the same
CSM/ISD sidecar camera model that produced it, and display it directly over a base map layer with
real shared geographic coordinates.

- **Validated the core idea live before writing any code**: ran `mapproject <dem> <rendered.tif>
  <csm.json> <out.tif> --ref-map <dem> -t csm` by hand against real cached files from a prior
  session — `mapproject` accepted the `cam_gen`-produced CSM JSON directly (`-t csm`, no separate
  ISD-to-`.tsai` conversion needed), and `--ref-map` pointed at the same DEM the render came from
  put the output on that DEM's exact grid, so it overlays `LunaservResult.ortho` with zero extra
  alignment work. Overlaying the result on the hillshade-based ortho showed individual crater rims
  lining up pixel-precisely across the full frame — confirms the "should be equivalent up to
  roundoff" prediction for a round trip through one consistent camera model (`sat_sim` forward:
  DEM+camera→image; `mapproject` inverse: DEM+camera+image→map).
- This is a **different, much simpler case** than the still-unresolved real-WAC `mapproject`
  striping issue (Phase 12/`docs/data-sources.md`'s "ISIS3/CSM spike"): that pipeline mapprojects an
  ISIS-processed *real* WAC cube (real sensor/framelet-stacking artifacts feeding in); this one
  mapprojects a clean synthetic render through its own exact camera model. Worth being clear about
  which case any future `mapproject` finding actually applies to.
- **New API**: `render.run_mapproject(render_result, lunaserv_result, config)` (opt-in, not part of
  `dataset.generate_dataset`'s default pipeline — a real ~4s subprocess call not every run needs);
  `plotting.plot_overlay(base_raster_path, overlay_raster_path, ...)` displays two geo-aligned
  rasters via `rioxarray`, using each file's own real coordinates. `mapproject`'s nodata is real
  `NaN` (not a huge-magnitude sentinel like elsewhere in this codebase) — ordinary NaN-aware
  handling is enough, no new masking logic needed. Both exposed on `Session` per the existing
  one-line-delegator pattern. New notebook Phase 7 demonstrates the synthetic-render-over-hillshade
  case.
- **New dependencies**: `rioxarray` (used immediately by `plot_overlay`) and `geopandas` (added
  ahead of need, per user decision to batch the one Docker rebuild both require, for a planned
  vector-layer overlay — e.g. the Robbins crater database — on top of `plot_overlay`'s raster
  overlay; not yet implemented, see `docs/plan.md`'s open items). Docker image rebuild was clean —
  no GDAL-version friction between `geopandas`'s `pyogrio`/`fiona` backend and the system GDAL
  already required by `rasterio`.

## Phase 17 (2026-08-07) — Fixed `plot_overlay`'s vertical distortion: switched to a local
Orthographic CRS

User noticed the Phase 16 overlay (viewed at `overlay_alpha=1.0` for clarity, up from the default
`0.6`, specifically to make this easier to diagnose) looked like "a tiny portion of the image
blown up to giant size" rather than a clean geo-aligned overlay, and suspected the map projection's
scale was off.

- **Root cause, confirmed empirically**: compared `rioxarray`'s view of the base ortho/DEM against
  `run_mapproject`'s output for the same product. The base's resolution was `(0.0042093,
  -0.0032980)` deg/px (correctly anisotropic — a degree of longitude covers less ground distance
  than a degree of latitude at this product's ~38.4°N) but the `mapproject --ref-map` output's was
  `(0.0042093, -0.0042093)` — **the same value on both axes**. ASP's `mapproject --ref-map` was
  copying the reference grid's x-resolution onto the y-axis instead of preserving its actual
  (different) y-resolution, stretching the reprojected overlay ~27.6% vertically
  (`0.0042093/0.0032980 ≈ 1/cos(38.4°)`) relative to the base it's displayed against. This only
  bites on a CRS whose degree-pixels are anisotropic (i.e. unprojected lon/lat away from the
  equator) — a `--ref-map` reference grid with genuinely square pixels can't expose the bug, since
  copying x onto y is then a no-op.
- **Fix**: switched `lunaserv.fetch_dem_and_ortho` from Lunaserv's native unprojected geographic
  grid (`IAU2000:30100`) to a **per-camera local Orthographic CRS** (`IAU2000:30166`, parametrized
  by that camera footprint's own center lon/lat), still via a single WMS `GetMap` request — no
  separate `gdalwarp` reprojection step added. This CRS has genuinely isotropic meter pixels
  everywhere, so `mapproject --ref-map`'s x→y copying becomes harmless (x already equals the
  correct y). Confirmed via a live GetMap + `gdalinfo` check that `IAU2000:30166` reports the Moon's
  real 1,737,400 m radius — critical, since the generic OGC `AUTO:42003` Orthographic code (also
  present in Lunaserv's `GetCapabilities`, and initially the more obvious choice) is hardcoded to
  **Earth's** WGS84 ellipsoid (6,378,137 m) and would have silently misplaced every ground point by
  the ~3.67x Earth/Moon radius ratio if used directly against lunar lon/lat — a much worse,
  harder-to-notice bug than the one being fixed. `IAU2000:30166`/`30162`(+`scale`, Stereographic)
  were found by diffing Lunaserv's `GetCapabilities` `<SRS>` list around the known-working
  `IAU2000:30100`/`30101` entries — a parametrized `301xx` block (Moon) parallels a `199xx` block
  (Mercury, ellipsoid 2,439,700 m) one digit over, both with placeholder `c_lon`/`c_lat`/`scale`
  tokens for the parametrized entries.
- **Verified end-to-end**: re-ran the full notebook after the fix; `mapproject --ref-map`'s output
  resolution now matches the base to ~0.03% (residual is independent-axis pixel-count rounding, not
  a systematic bug — an entirely different, much smaller-order effect). The overlay figure at
  `overlay_alpha=1.0` now shows properly circular craters (not vertically squashed) and blends
  seamlessly against the base ortho.
- **Code changes**: `lunaserv.py` gained `orthographic_xy_m`/`footprint_bbox_local_m` (forward
  spherical Orthographic projection, matching Lunaserv's own formula/radius so a locally-computed
  bbox lines up with what the WMS server renders) and a simplified `pixel_dims_for_gsd` (no cos(lat)
  correction needed against an already-isotropic metric bbox). `footprint_bbox_deg` is kept
  (still used for a human-readable diagnostic print, still tested) even though it's no longer used
  to size the WMS request. `config.py`'s `lunaserv_srs` field/`DEFAULT_LUNASERV_SRS` constant were
  renamed to `lunaserv_srs_template`/`DEFAULT_LUNASERV_SRS_TEMPLATE` (now a `{c_lon}`/`{c_lat}`
  format string, filled in per camera). `LunaservResult.bbox` is now in meters (that camera's own
  local CRS), not lon/lat degrees — no other code read that field, so this didn't ripple further.
  `plotting.plot_overlay`'s axis labels updated from `"longitude/latitude (deg)"` to `"x/y (m,
  local projected CRS)"` to match.
- **Follow-up**: the fixed overlay renders so seamlessly against the base that it was hard to
  visually confirm it was even there. Added `plot_overlay(show_overlay_outline=True)` (default on):
  traces the overlay raster's real (non-NaN) footprint via `rasterio.features.shapes` on its
  valid-pixel mask, unions the resulting polygons and drops interior holes (isolated nodata
  speckle, not meaningful outline content) with `shapely`, then draws the result as a red vector
  boundary via `geopandas` — the first real use of the `geopandas` dependency added ahead-of-need in
  Phase 16, and a working template for the still-unimplemented vector-layer overlay (e.g. Robbins
  craters) mentioned there. `pyproject.toml`'s mypy overrides gained `geopandas.*`/`shapely.*`
  (no type stubs published for either).

## Phase 18 (2026-08-07) — Fixed `wac_isis_spike`'s `spiceinit` failure: `shape=ellipsoid`

`notebooks/wac_isis_spike.py` (and the newer `isis_wac.py` pipeline it exercises) had never
actually been run against a truly empty `$ISISDATA` cache before — every earlier "confirmed
working" session had a `dems/` directory left over from an earlier, pre-correction full fetch. On a
genuinely fresh cache (this session, a fresh ephemeral VPS with no restored `cache_root`), `spiceinit
web=yes` failed: `USER ERROR NAIF DSK file
[$base/dems/ldem_128ppd_Mar2011_clon180_radius_pad.cub] does not exist`.

- **Root cause**: `spiceinit`'s `SHAPE` parameter defaults to `*SYSTEM`, which resolves to a real
  lunar DSK/DEM cube under `base/dems/` — the ~20 GB directory `ensure_isisdata()` deliberately
  skips (see `docs/data-sources.md`'s ISIS3/CSM spike section; that section's "none of that DEM
  data is needed until `mapproject`" claim was itself wrong, corrected there now). This bites well
  before any terrain-intersection step, contrary to what was previously assumed.
- **Fix**: `isis_wac.run_spiceinit` now passes `shape=ellipsoid` (confirmed via `spiceinit -h`:
  `SHAPE = (ELLIPSOID, RINGPLANE, *SYSTEM, USER)`) — a plain reference ellipsoid, no DSK file
  needed. Tested directly against both real cached `vis.even`/`vis.odd` cubes from a prior run
  before touching the notebook; both succeed (`ShapeModel = Null` in the resulting label, in place
  of a real DSK path). This module's scope stops at `framestitch` (no `isd_generate`/`mapproject`
  precision-terrain step yet), so the simple ellipsoid is sufficient for now.
- **Verified end-to-end**: re-ran `notebooks/wac_isis_spike.py` via `scripts/run_notebook.sh` from
  the same fresh cache that originally hit the failure — full clean run, `trntest-lint` passes
  (notebook sync included).
- **Follow-up**: `notebooks/lunar_sat_sim_demo.py`'s own Phase 6 (`isis_wac.run_pipeline` against
  the live demo product) had separately been commented out, for a different-sounding reason (the
  cell's own comment blamed an external SPICE web service failure, "SPICE server returned
  incompatible SPICE data") — re-enabled it now that `run_spiceinit` no longer needs `base/dems/`,
  and it runs clean end-to-end too (re-ran the full flagship notebook). Either that external-service
  failure was actually this same `dems/` gap manifesting differently, or it was transient/has since
  recovered — not distinguished, since the fix resolves both possibilities' symptom either way.

## Phase 19 (2026-08-07) — Confirmed and cosmetically fixed Phase 6's framelet-boundary striping

User asked whether the dotted/gridded "striping" visible in the newly-re-enabled Phase 6 comparison
(the ISIS-processed real WAC panel, not the `mapproject`-stage striping investigated separately in
Phase 12) might be a no-data issue.

- **Confirmed empirically, directly from the stitched cube's pixel data** (not guessed): read
  `M1327210646CE.vis.cal.stitched.cub` band 1 (3612 x 704) and checked `plotting.valid_pixel_mask`'s
  criterion against it. Overall NULL fraction: 0.96%. Two distinct, fully deterministic components —
  columns 0-1 NULL on every line (fixed detector-edge dead strip), and a fixed set of 56 specific
  columns NULL only on the first line of every 14-line VIS framelet cycle (confirmed identical
  column indices at 6 widely-separated cycles spanning the full cube — a real fixed hardware
  bad-pixel mask, not noise). This is `lrowaccal`'s documented `SpecialPixels` correction (see
  `docs/data-sources.md`'s "LROC WAC EDR/CDR products" section, new bullet) actually firing, exactly
  as predicted there but not previously verified end-to-end.
- Ruled out a display/downsampling artifact: rendered the same crop at native pixel-for-pixel
  resolution (`interpolation='none'`) as well as heavily downsampled (matching the real comparison
  figure's subplot size) — same grid pattern both times. The pattern reads as visually prominent
  despite <1% density purely because it's so *regular* (identical phase/columns every 14-line
  cycle), not because of any rendering/resampling artifact.
- **Fix (display-only, per user's choice)**: added `plotting._fill_dead_columns_for_display` — a
  simple row-wise linear interpolation across each narrow (1-3 column) gap, `np.interp` clamping
  cleanly at the column-0 edge case (no left neighbor). Used only in `plot_isis_comparison`'s
  display array; the contrast stretch (`vmin`/`vmax`) is still computed from the real, unfilled
  valid data first, so the fill can't skew it. Doesn't touch `lrowaccal`'s actual calibrated output
  or anything else that reads the cube. New tests in `tests/test_plotting.py` (interpolation,
  edge-clamping, fully-valid passthrough, fully-invalid-row NaN fallback). Verified end-to-end via a
  full notebook re-run — the grid pattern is gone from the displayed figure with no visible loss of
  real detail.
- **Follow-up**: `plot_isis_comparison`'s own contrast stretch (`vmin`/`vmax` from a 2nd/98th
  percentile of the real panel, each panel auto-normalized independently) had the same problem
  `plot_comparison` was already fixed for (`cc7b369`, prior session): an affine/percentile stretch
  independently renormalizes each panel's own contrast, hiding any real relative-brightness
  difference between them — and here the two panels are on completely different numeric scales to
  begin with (`real`, ISIS-calibrated I/F, ~0.01-0.2; `synthetic`, a rendered-texture brightness
  value, ~0-255), not just different units of the same thing. Ported `plot_comparison`'s exact
  technique: a single multiplicative scale at the median (`median(synthetic_valid) /
  median(real_valid)`, computed over the real *unfilled* valid pixels so the dead-column fill above
  can't skew it), both panels then displayed on the same fixed `vmin=0, vmax=255`. Verified via
  another full notebook re-run — the two panels now read as comparably bright at a glance, not just
  individually "nicely stretched."

## Phase 20 (2026-08-07) — Solved the `mapproject`-stage framelet-boundary striping; new Phase 5B/6A/6B

User asked to look into the *other*, more severe striping issue -- the one from the original ISIS3/
CSM spike (Phase 12), where mapprojecting a real WAC cube onto the DEM produced severe periodic
banding dominating ~80% of the frame, previously concluded to be "a structural/geometric artifact in
the current CSM Pushframe stitching itself... not something fixable" (a genuine ASP/ALE limitation,
it seemed).

- **Reproduced live** (not just re-reading old docs): ran `isd_generate -i` + `mapproject -t csm`
  against this session's own cached `M1327210646CE.vis.even.cal.cub` (a lone parity) and its real
  DEM. Confirmed the striping directly: 31% valid coverage, output dominated by venetian-blind-style
  dark smearing with almost no recognizable terrain -- each framelet's real content survived only as
  a thin sliver, with the rest smeared into a large, mostly-featureless dark region. Checked the
  ISD's own pose tables as a sanity check: `instrument_pointing`'s quaternion table already had 259
  samples (≈ one per framelet, correctly structured, not naively one-per-line) -- so the input pose
  data itself looked fine, pointing suspicion at *which pixel data* was being reprojected, not the
  camera model's geometry.
- **User asked a sharper, decisive question**: could this be related to `isis_wac.py`'s never-fully-
  understood "even"/"odd" cube split, and needing to interleave them before generating the ISD?
  Checked directly: at *every* single frame position in `vis.even.cal.cub`/`vis.odd.cal.cub`, exactly
  one of the two has real (99% valid) data and the other is 100% NULL, in a perfect strict
  alternation confirmed at 20 consecutive frames. So "even"/"odd" are **not** a same-frame split
  (e.g. interlaced TDI rows, as might be guessed from the name) -- WAC genuinely only writes real
  pixel data to alternating nominal frame slots; each parity cube alone is a temporally-sparse,
  ~50%-populated sequence.
- **Fix, confirmed decisively**: generated an ISD from the properly-interleaved `vis.cal.stitched.cub`
  instead (same `isd_generate -i` call -- and its output parameters, e.g. `interframe_delay`, the
  259-sample pointing table, came out byte-for-byte identical to the lone-parity ISD, confirming
  `isd_generate` derives these from label metadata regardless of which pixels are populated) and
  re-ran `mapproject` against it: **81% valid coverage, real craters visible throughout the frame**.
  The dominant smearing artifact is gone; what remains is the same small, already-understood, ~1%
  framelet-boundary dead-pixel speckle from Phase 19. The earlier "fundamental CSM Pushframe
  limitation" conclusion was wrong -- it was mostly a case of mapprojecting a sparse, half-blank
  input and letting ASP's resampling smear across the gaps.
- **Second bug found along the way**: `plotting.plot_overlay`'s first live test of this (overlaying
  the mapprojected real-WAC cube on the hillshade base) rendered as a flat, featureless gray block.
  Root cause: `mapproject`'s nodata convention depends on its *input* format -- a synthetic render
  (plain GeoTIFF source, Phase 16/17) comes out with real IEEE NaN nodata, but an ISIS `.cub` source
  carries ISIS's own huge-magnitude NULL sentinel (~-3.4e38) straight through, with a `nodata` tag
  set to match. `_open_raster_dataarray` was calling `rioxarray.open_rasterio` without `masked=True`,
  so that sentinel dominated `plot.imshow`'s automatic vmin/vmax and washed the real 0.01-0.13 I/F
  signal out to nothing. Fixed by always passing `masked=True` (a no-op for the already-NaN synthetic
  case, so this fixes both without needing to distinguish them).
- **New capability, `isis_wac.py`**: `run_isd_generate` (ALE's `isd_generate -i`, reading pointing/
  timing from the label `run_spiceinit` already embedded) and `run_mapproject` (reprojects the
  stitched cube via its own ISD) -- module docstring updated, no longer "spike"-scoped. `render.py`
  gained `run_mapproject_image`, a generic low-level `mapproject --ref-map` worker factored out of
  `run_mapproject` so both the synthetic-render and real-WAC mapproject paths share one
  implementation.
- **Notebook restructured** per user's plan: Phase 5 → **5A** (unchanged, `wac.py`-based side-by-side
  comparison) plus new **5B** (real, ISIS-processed WAC mapprojected and overlaid on the hillshade
  base -- the new capability above, demonstrated). Phase 6 → **6A** (unchanged ISIS side-by-side
  comparison, now reuses 5B's already-computed `stitched` cube instead of recomputing it). Phase 7 →
  **6B** (unchanged synthetic-render mapproject overlay). 5B and 6B share `plotting.plot_overlay`
  directly. Per the same request, `plot_overlay`'s axis labels changed from raw meters ("x/y (m,
  local projected CRS)") to km with a tick formatter ("Easting (km)"/"Northing (km)"), underlying
  geometry unchanged -- affects both 5B and 6B since they share the function. Also dropped 6B's
  `overlay_alpha=1.0` debug override (from Phase 17's CRS-bug diagnosis, long since resolved and
  verified) back to the default `0.6`, since these cells were already being revisited.
- **Verified end-to-end**: full notebook re-run via `scripts/run_notebook.sh`; `trntest-lint` passes
  (format/check/mypy/notebook sync/notebook warnings).

## Phase 21 (2026-08-07) — Phase 5B's "intense striping": missing nodata fill, AND a real flip bug

User saw much more severe striping in the actual Phase 5B figure than Phase 20's own preview had
suggested, asked to verify hole-filling was applied, and separately suspected individual framelets
might need to be vertically flipped. Both turned out to be real, distinct, compounding problems.

- **First fix -- missing nodata hole-fill**: `plotting.plot_overlay` had no nodata hole-filling at
  all (unlike `plot_isis_comparison`, which gained `_fill_dead_columns_for_display` in Phase 19).
  The underlying no-data density was unchanged from Phase 19's measurement (0.96% overall, same 56
  columns recurring at every 14-line framelet boundary) -- Phase 20's own preview had understated how
  this looks once mapprojected: each of those ~56 recurring dead columns traces its own thin dashed
  streak through map space (following the sensor's ground track), and with 258 framelet cycles in
  the swath, that reads as dense, "intense" striping at real display size despite the low raw
  fraction. Added `plotting._fill_overlay_nodata_for_display` (`rasterio.fill.fillnodata`, GDAL's
  inverse-distance-weighted inpainting -- orientation-agnostic, unlike
  `_fill_dead_columns_for_display`'s row-wise approach, needed since a mapprojected raster's gaps
  run along the ground track, not neatly row-wise anymore). Wired into `plot_overlay` as
  `fill_overlay_nodata=True` (default on); the vector outline is still traced from the *unfilled*
  data, so it reflects the genuine sensor footprint.
- **This didn't fully explain the user's report** -- after the nodata fix, real (non-NaN) pixels
  still showed clear banding at what looked like framelet boundaries. Investigated thoroughly before
  finding the real cause:
  - `framelets_flipped` ISD field: patched to `true`, re-ran `mapproject` on a *fixed* output grid
    for a true pixel-level diff against the unpatched version -- **byte-for-byte identical**. This
    field has no effect on `mapproject`'s geometry.
  - Uniform per-framelet internal line-order flip (reversing all 14 lines of every framelet,
    applied directly to the pixel data, bypassing the ISD entirely): made it worse, introducing a
    new ghosting/doubling artifact.
  - Alternating (even-parity-only, then odd-parity-only) internal line-order flip: no visible
    change either way.
  - Measured a real, smooth cross-track photometric gradient (~-0.008 to +0.016 I/F across the
    704-sample FOV, computed per-column after subtracting each framelet's own median) -- but
    subtracting it before mapprojecting ("destriping") didn't change the banding either.
  - Compared the *same* crop in native image space (pre-`mapproject`): smooth, no periodic banding
    at all -- confirming the banding is introduced by `mapproject`'s reprojection, not present in
    the calibrated pixel values themselves in their original form.
  - Confirmed the banding is real (present in actual valid, non-NaN pixels, highlighted directly
    against nodata) -- not a `fillnodata` smearing artifact.
  - **Root cause, found on user's insistence to keep pursuing the flip angle**: the *other*
    untested ISD field, `framelet_order_reversed` (the framelet *sequence* order, distinct from
    `framelets_flipped`'s within-framelet line order). Patched to `true` and re-ran on a fixed
    output grid: **3.4M of 4.3M pixels differed** from the unpatched version -- a real, substantial
    effect, unlike `framelets_flipped`. Rendered: the severe banding was completely gone, real
    coherent terrain visible throughout. Root cause: `framestitch`'s `DataFlipped` label field (set
    correctly from the `FLIP=TRUE` this mirrored/`k=3` product needs) is not read by `isd_generate`,
    which always emits `framelet_order_reversed: false` regardless -- so `mapproject` was assigning
    each framelet the wrong pose whenever `flip=True` was actually used.
- **Fix**: `isis_wac.FramestitchResult` now carries its own `flip` value forward;
  `run_isd_generate` patches the generated ISD's `framelet_order_reversed` to match it. Verified via
  a full notebook re-run -- Phase 5B is now clean with no visible striping, 6A/6B unaffected (no
  regression). `run_isd_generate`'s docstring records all three tested-and-ruled-out mechanisms
  alongside the real fix, so this doesn't need re-deriving if a future product needs revisiting.

## Phase 22 (2026-08-07) — Retired `wac.py` from the demo notebook; merged Phase 5A into Phase 5

With `isis_wac.py` now producing a real, validated camera model end-to-end (Phase 20/21's fixes),
user asked whether it dominates `wac.py`'s manual CDR framelet-stacking for the demo's purposes.
Agreed it does for the actual goal (proving the render's pose is geometrically correct, not just
visually similar) -- `wac.py`'s only advantage is being lower-dependency/more robust (no ISIS/ALE
toolchain, no live `spiceinit web=yes` call, far fewer moving parts than the multi-stage
`lrowac2isis`→`lrowaccal`→`framestitch`→`isd_generate` chain this session's Phases 18-21 spent
finding and fixing real bugs in) -- not higher fidelity. Asked to rewrite Phase 5 accordingly.

- Since Phase 6A (`plot_isis_comparison`, added in Phase 20) already did the same side-by-side
  comparison + tie points that Phase 5A's `wac.py`-based `plot_comparison` did, just against
  `isis_wac.py`'s data, switching Phase 5A to `isis_wac.py` would have made it a near-duplicate of
  6A. Merged them instead: the old 5A's WAC/tie-point narrative + the old 6A's `isis_wac.run_pipeline`
  call and `plot_isis_comparison` display now form a single **Phase 5**. `stitched` is computed once
  there and reused by the geo-aligned overlay (renumbered **5B → 6A**); **6B** (synthetic render
  overlay) is unchanged. `tie_point_results`/`rotations` needed no changes -- both were already pure
  SPICE frame-index geometry with no dependency on which real-data pipeline produced the pixels
  (confirmed by re-checking `tie_points.compute_tie_points`: it passes `camera.reverse_crop_along_track`
  directly, never touches `wac.fetch_vis_mosaic`'s actual output).
- `wac.py` itself is untouched -- still a real, tested module (`tests/test_wac_unpacking.py`), just
  no longer called by either notebook. Not deleted: it's not dead code (has its own tests
  independent of notebook usage), and there was no request to remove it, only to stop using it here.
- Found and fixed a real, stale docstring while in the area: `plotting.plot_isis_comparison`'s first
  paragraph claimed "not a tie-pointed comparison... since the ISIS cube isn't reprojected onto the
  DEM yet" -- both false by the time this phase started (it's always plotted tie points; `mapproject`
  support was added in Phase 20). Leftover from an early draft, never updated. Corrected.
- Updated `docs/plan.md`'s architecture table and "Known open items" entry to reflect the current
  state (`isis_wac.py` as the demo's sole real-WAC method, current Phase 5/6A/6B numbering) --
  `docs/history.md`'s own Phase 20/21 entries describing the old 5A/5B/6A/6B numbering were left
  untouched, since they're historical narrative describing what was true at the time, not current
  state (see this doc's own header note).
- Verified end-to-end via a full notebook re-run; `trntest-lint` passes (format/check/mypy/notebook
  sync/notebook warnings).

## Phase 23 (2026-08-07) — Reorganized around the demo's actual TRN-testing purpose

User pushed back on Phase 22's renumbering ("5B renamed to 6A" felt like an unexplained mixup) and,
in clarifying, revealed context that had never actually been written down anywhere: this demo's real
purpose is generating **candidate test images for terrain-relative navigation (TRN) testing** (the
repo is literally named `trntest`) -- the synthetic `sat_sim` render and the real, ISIS-processed WAC
crop are two *candidate* TRN test images, and each needs its own geometry validated against a common
reference (the hillshade basemap) in two styles: "A" (raw image quality, up to 90° rotation) and "B"
(map projection, to really scrutinize alignment). `docs/plan.md`'s own "What this is" section never
mentioned TRN at all -- a real documentation gap now fixed.

- **Real inconsistency found in the process**: "A"-style (`plot_comparison`/`plot_isis_comparison`)
  compared the synthetic render directly against the real WAC crop, while "B"-style (`plot_overlay`)
  compared each against the basemap -- not actually the same comparison in two styles, as the "A/B"
  naming implied. User chose to fix this properly: both styles should compare each candidate against
  the *same* baseline (the basemap), keeping the direct candidate-vs-candidate comparison as its own
  separate thing.
- **New capability**: `plotting.plot_render_vs_basemap` -- the real "A"-style function. Takes a
  render's own raw pixels (rotated north-up, no resampling) next to a plain pixel crop of the basemap
  covering the same real footprint. The basemap crop needs *no* rotation (its local Orthographic CRS
  is already north-referenced by construction, +Y = north) -- only the render does (fixed
  sensor-pixel axes). The footprint-to-basemap-window conversion reuses `lunaserv.footprint_bbox_local_m`
  directly (same function `fetch_dem_and_ortho` already uses to size the original WMS fetch) rather
  than re-deriving equivalent logic.
- **New capability**: `tie_points.crop_footprint_corners_for_camera` (+ `Session.crop_footprint_corners`)
  -- the real WAC crop's own ground footprint, independently ray-traced from real SPICE geometry (not
  assumed identical to the synthetic camera's own footprint), needed so `plot_render_vs_basemap` can
  crop the *correct* matching basemap area for Phase 6A specifically.
- **Notebook restructured** to match the TRN-testing framing directly: **Phase 5** = does the
  synthetic render's geometry check out (5A raw-vs-basemap, 5B `mapproject`-vs-basemap overlay,
  unchanged content from Phase 22's "6B"). **Phase 6** = does the real ISIS-processed WAC crop's
  geometry check out (6A raw-vs-basemap [new], 6B `mapproject`-vs-basemap overlay, unchanged content
  from Phase 22's "6A"/original "5B"). **Phase 7** = direct synthetic-vs-real-WAC quality comparison
  with tie points (unchanged `plot_isis_comparison` call, just relocated and reframed as a
  supplementary comparison rather than part of the geometry-check structure).
- **Follow-up, same session**: user asked for 5A/6A to keep the tie-point markers `plot_comparison`/
  `plot_isis_comparison` always had, and pointed out Phase 7 could then be explained simply as 5A's
  and 6A's own render panels put together. Added tie-point support directly to
  `plot_render_vs_basemap` (`tie_point_results`/`render_px_key` params): the render panel reuses the
  existing pixel-coordinate + rotation technique (`_plot_tie_point_marker`); the basemap panel
  (unrotated, already-georeferenced) instead projects each point's real `lonlat` straight into the
  crop's own local-CRS offset via `lunaserv.orthographic_xy_m` -- no pixel coordinates needed there
  at all. This moved `rotations` *and* `tie_point_results` to compute once, right after Phase 4,
  since 5A/6A both now need both; Phase 7's own markdown was rewritten to describe it as literally
  "5A's and 6A's own render panels put together" (plus `plot_isis_comparison`'s brightness-matching
  and dead-pixel-fill, kept as real quality-of-life additions on top, not new geometry content).
  Verified via another full notebook re-run: tie points visibly land on the same real terrain
  features across all four panels (5A/6A's render + basemap sides).
- **Regression caught, same session**: user noticed 6A's real WAC panel had the framelet-boundary
  dead-pixel speckle back (the same pattern `_fill_dead_columns_for_display` fixed in
  `plot_isis_comparison`, Phase 19). Root cause: `plot_render_vs_basemap` only ever masked invalid
  pixels to NaN, never carried over the interpolation fill -- a real gap from building the new
  function without checking `plot_isis_comparison`'s existing render-panel handling for the same
  underlying data. Fixed by calling `_fill_dead_columns_for_display` on `render_array` before
  display, matching `plot_isis_comparison`'s exact pattern -- a no-op for the synthetic render
  (nothing to fill), confirmed via another full re-run that 5A's output was pixel-identical to
  before, only 6A changed (speckle gone).
- **Two more issues caught, same session**: user noticed 6A's real-WAC panel was suspiciously dark,
  and that 6A's square crop didn't correspond to 6B's overlay extent (a long strip vs. a small
  square).
  - **6A darkness**: `plot_render_vs_basemap` displayed both panels with `imshow`'s default naive
    min/max autoscale. Checked the stitched cube's actual pixel distribution directly
    (`min=0.012, max=0.195, p99.9=0.107, p99.99=0.131` vs. a median around `0.048`) -- a handful of
    extreme bright outlier pixels were stretching the autoscale wide enough to compress the real
    terrain into roughly the bottom 20% of the display range. First fix used a 2nd/98th percentile
    affine stretch (matching `plot_raster`'s), but the user flagged that as too aggressive: an affine
    stretch (`vmin` = a data percentile) shifts the black point up, clipping genuinely dark-but-real
    terrain to pure black. Changed to a **linear** stretch through 0 instead -- `vmin=0` always (a
    true black point, not data-derived), `vmax` = the 99.9th percentile (not a naive max, so the same
    handful of outlier pixels still don't wash out the rest, but a less aggressive cutoff than 98th)
    -- applied independently to each panel, same as before.
  - **6A-vs-6B extent mismatch**: `isis_wac.run_mapproject` reprojects `stitched` in full -- all 258
    frames, not just the square crop 6A/Phase 7 use -- so 6B's `plot_overlay` was always showing that
    entire long strip regardless of what 6A cropped. Rather than changing what gets mapprojected
    (would mean cropping the cube before `isd_generate`, risking ISD/cube dimension mismatches), added
    a `zoom_footprint_lonlat_deg` parameter to `plot_overlay` that restricts the *displayed* extent to
    a given footprint's bounding box (`lunaserv.footprint_bbox_local_m`, same technique
    `plot_render_vs_basemap` uses), wired to the already-computed `crop_footprint`.
    - **First attempt was subtly wrong**: computed the bbox using `zoom_footprint_lonlat_deg`'s own
      center as the local-CRS projection origin. `lunaserv.fetch_dem_and_ortho` centers the base/overlay
      rasters' actual CRS on the *camera's* footprint center, not the crop's -- a real latent bug
      (confirmed by checking the ortho tif's own CRS: `+lon_0=169.525773 +lat_0=38.404418`, the camera
      center) that happened not to visibly manifest here because `crop_footprint_corners_for_camera`'s
      ray-traced center turned out numerically identical to the camera's own center for this product
      (verified directly). Fixed to derive the origin from `base.rio.crs.to_dict()`'s `lon_0`/`lat_0`
      instead, so the zoom bbox is always computed in the same frame the rasters are actually plotted
      in, regardless of whether the two centers happen to coincide.
    - After the fix, re-examined 6B and initially misread a chunk of missing coverage on one side of
      the crop (WAC's real swath, mapprojected, doesn't fill the entire axis-aligned crop bbox -- its
      cross-track center drifts along the pass) as a bug. Checked the mapproject tif's actual valid-data
      mask directly: 84% valid within the crop bbox, with the "missing" area a real, legitimate wedge
      where the swath genuinely doesn't reach (confirmed row-by-row) -- not a coordinate-frame error.
- Updated `docs/plan.md`'s "What this is"/"Status" sections and the `tie_points.py`/`plotting.py`
  architecture rows to reflect all of the above.
- Verified end-to-end via several full notebook re-runs (including one to confirm a small refactor
  reusing `lunaserv.footprint_bbox_local_m` was byte-identical to the original inline computation);
  `trntest-lint` passes throughout.

## Phase 24 (2026-08-08) — Fixed 6B for real: a single, correctly-cropped WAC image, not a display-layer zoom

User reported 6B's mapprojected overlay "isn't displaying as desired": judging by the red overlay
outline, the visible area contained only a small part of the overlay, crossing it diagonally rather
than showing a closed shape. Getting to the real fix took three attempts.

**Attempt 1 (real, but insufficient): contrast.** First ruled out an extent bug, since that was the
most recent thing touched (Phase 23's `zoom_footprint_lonlat_deg`). Rebuilt the Docker image and
re-ran the full notebook from cold (cache/output don't survive a VPS teardown -- see
`docs/environment.md`) to get real, current intermediate files rather than trusting the stale
committed notebook or reasoning from the plot alone. Directly measured the real mapproject tif's
valid-data mask against the exact zoom bbox `plot_overlay` computed: 84.4% of the zoomed window was
genuinely valid overlay data -- byte-for-byte the same figure Phase 23 already reported. Root cause
found: `overlay_display.plot.imshow()` had no `vmin`/`vmax`, so `imshow`'s naive min/max autoscale
compressed the real calibrated-I/F overlay (~0.02-0.17) down near black, blending almost invisibly
into the base at `overlay_alpha=0.6` -- the same root cause as Phase 23's "6A darkness" bug, just
never applied to `plot_overlay`. Fixed with the same `vmin=0`/`vmax=`99.9th-percentile stretch
`plot_render_vs_basemap` already used, verified visually before applying it. **Real, worth keeping,
but the user pointed out after seeing it that it didn't address the actual complaint** -- the outline
still only crossed diagonally through the frame, not a closed shape.

**Attempt 2 (also real, still insufficient): a second, explicit crop-footprint outline + view
padding.** Re-examined what the red outline actually traces: `_valid_data_outline(overlay)`
(`plotting.py`) draws the *entire* `overlay` raster's real valid-pixel footprint -- for 6B that's
`isis_wac.run_mapproject`'s output covering the *entire* 258-frame WAC swath (~168km x ~255km
measured), not just the ~149km-square crop being compared. `zoom_footprint_lonlat_deg` only ever
called `ax.set_xlim`/`ax.set_ylim` -- it never restricted what geometry got traced, so no view sizing
could make an unrelated, much-longer boundary render as a closed box. Drafted a plan to draw a
*second* outline from the crop's own idealized footprint corners, with view padding, and got user
sign-off to implement -- but before writing code, the user redirected: "they need to use the same
image, and it really should have an ISD sidecar that is valid for the WAC crop." I.e. fix the
*pipeline*, not the display: produce one real, cropped WAC image up front (mirroring how 5A/5B
already both derive from one synthetic render with no special-casing) rather than patch `plot_overlay`
again.

**Attempt 3 (the real fix): crop the stitched cube, and fix `isd_generate`'s ephemeris-time bug for
cropped input.** Added `isis_wac.crop_for_camera()`, cropping `stitched` (post-`framestitch`, via
ISIS's own `crop` app) to `crop_window_for_camera`'s window -- `lrowaccal` explicitly refuses to run
on a cropped image ("USER ERROR: This application can not be run on any image that has been
geometrically transformed ... or cropped", confirmed empirically), so cropping has to happen after
calibration, on the stitched cube, not earlier in the pipeline. First test of the obvious approach
(`isd_generate -i` directly on the cropped cube, then `mapproject`) produced a *plausible-looking but
wrong* result -- compared pixel-for-pixel against the same real ground region of the known-good
full-cube mapproject (both share the same reference grid, no reprojection needed to compare): only
0.44 correlation, should be ~1.0. Root cause, found by diffing the cropped cube's own generated ISD
against the full cube's: `starting_ephemeris_time`/`ending_ephemeris_time`/`center_ephemeris_time`
and `instrument_pointing.ck_table_start_time`/`ck_table_end_time` all read as if the crop still
started at the *original* uncropped cube's first line -- ISIS's `crop` app (even with the default
`PROPSPICE=true`) does not itself re-anchor a Pushframe cube's per-line pointing cache to the new
starting line (it does correctly update `ck_table_original_size` to the cropped line count, just not
the *start* time). Tried re-running `spiceinit` on the cropped cube in case that would force a fresh,
correctly-scoped recompute -- confirmed empirically it makes no difference (byte-identical wrong
result) -- and tried cropping the calibrated parity cubes before `framestitch` instead of after --
also confirmed empirically identical wrong result, so the bug isn't sensitive to which pipeline stage
the crop happens at. Fix: after `isd_generate`, if the input was `crop_for_camera`'s output (nonzero
`line_offset`), patch just the 5 scalar time fields above by `time_offset_s =
(line_offset / VIS_BLOCK_HEIGHT) * isd["interframe_delay"]` (WAC frames -> real seconds, using ALE's
own reported per-frame timing for this product, not a hardcoded constant) -- the same
patch-the-JSON-after-generation technique `run_isd_generate` already used for `framelet_order_reversed`
(see Phase 20-ish entries above). Deliberately does *not* touch the underlying
`ephemeris_times`/`quaternions`/`angular_velocities` arrays -- confirmed these stay the *entire*
pass's real, absolute-time-tagged samples regardless of crop (identical length before/after), so once
the scalar time fields correctly reflect the crop's real start time, the CSM model interpolates the
*correct* poses out of that same full table for whatever absolute times the cropped lines actually
correspond to. Fixed the correlation to 0.999, confirmed visually as recognizable, correctly-aligned
real terrain (no garbling/duplication).

With `crop_for_camera`'s output usable end-to-end, `plot_overlay`'s `zoom_footprint_lonlat_deg`
parameter (added in Phase 23, patched in Attempt 1 above) became entirely unnecessary and was
**removed** -- 6B now calls `plot_overlay` exactly like 5B, no special-casing, and the existing,
unchanged `_valid_data_outline` mechanism naturally traces a proper closed box because `overlay` is
now genuinely crop-sized.

Also (per user request, to guarantee no no-data gap in 6B's basemap): `dataset.generate_dataset()`
now computes each image's real WAC crop footprint (`tie_points.crop_footprint_corners_for_camera`)
*before* calling `lunaserv.fetch_dem_and_ortho`, and unions it into the DEM/ortho fetch AOI alongside
the synthetic camera's own footprint (`lunaserv.union_bbox`, new). Tried additionally
double-padding the crop side of that union to close a measured ~120m worst-case margin gap --
confirmed empirically this doesn't actually help (the tight edge's margin stayed ~0 regardless of a
261km vs. 410km total fetch, since the underlying drift is directional/asymmetric, not a "not enough
padding" problem) while measurably increasing fetch time (hit a real WMS read-timeout during
testing) -- reverted. The remaining ~120m gap is close to a single ~100m/px DEM pixel, not a
meaningfully visible nodata gap in practice.

Also restructured the notebook's Phase 6 cells per user preference (split compute from plotting
where that enables reuse, rather than a strict one-liner-per-cell rule): `stitched =
isis_wac.run_pipeline(...)` and `wac_crop = isis_wac.crop_for_camera(...)` are their own compute
cell, consumed by separate 6A/6B plotting cells; `crop_footprint` moved to Phase 2 (now
`GenerationResult.crop_footprint`, computed once inside `generate_dataset()`) instead of being
recomputed via a `Session.crop_footprint_corners()` convenience method just before 6A -- that method
had no remaining caller afterward and was removed.

Verified via several full notebook re-runs from a live-rebuilt cache (never trusted the stale
committed notebook or reasoning from a plot image alone at any step); `trntest-lint` and the full
`pytest` suite (88 tests) pass.

## Historical derivations

Detailed technical derivations referenced by the phase history above. All describe *how a current
behavior was reached*; for what that behavior actually is today, see `docs/data-sources.md`.

### Real image comparison (Phase 5): band separation + finding sunlit frames

Two fixes were needed to get a real image that's actually comparable to the synthetic render:

1. **De-interleave one VIS filter across many frames.** WAC's push-frame design is meant to build
   continuous coverage by "repeated imaging such that each of the narrow framelets of each color
   band overlap" (SIS) — i.e. take the *same* filter's TDI-line block from each of many consecutive
   frames and stack them vertically; adjacent frames' blocks tile almost seamlessly (interframe
   ground advance ≈ 1.19 km vs. a ~1.05-1.4 km per-block footprint at this altitude/GSD). Lines
   `[22:36)` within the 78-line frame are used: since UV only ever occupies the first-or-last 8
   lines (depending on the yaw-dependent order), `[22:36)` is guaranteed to fall entirely inside the
   VIS region either way — which exact one of the 5 VIS wavelengths it is depends on the yaw state,
   which wasn't determined (irrelevant for just getting a recognizable picture).
2. **Frame 0 (and up to ~210) is in near-total shadow.** Scanning I/F statistics across the
   de-interleaved VIS block for frames 0, 30, 60, ... 530 showed means and maxima at the noise floor
   (some even negative) through frame ~210, then jumping to real signal (mean ~0.003-0.018, max up
   to ~0.07) from frame ~240 onward, stable through at least frame ~530. Framelet 440 was picked
   from that stable, well-lit stretch — the very product-specific choice later superseded by
   Phase 8's per-product illumination filtering.
3. Verified visually (first with a fixed 19-frame crop, then with the real-geometry-sized crop
   below), contrast-stretched over valid (non-`missing_constant`) pixels: produces a clearly
   recognizable, dramatic cratered lunar scene (a large crater with a bright central peak/rim and
   dark, likely-permanently-shadowed floor) — confirms both the band-separation logic and the frame
   choice are correct.

Product used: `M1329714703CE`, posed at framelet index 440. Computed LRO position in `MOON_ME` at
that instant: sub-spacecraft/output camera center lon/lat/alt ≈ (112.03°, -82.56°, 68.51 km) —
consistent with LRO's low south-polar Fourth Extended Science Mission orbit.

### Square-crop sizing: real ground area, not a fixed pixel/frame count

Originally the synthetic camera's FOV was sized to hit a fixed ~100 m/px GSD at 256x256, and the
real CDR comparison crop used a fixed 19 frames (chosen ad hoc to look roughly 256 px tall) —
neither was grounded in the instrument's actual FOV, so the two images didn't reliably cover the
same real ground area. Fixed by deriving both from the real WAC color-mode field of view:

- Tried reading the real FOV straight out of the loaded WAC-VIS IK via `spice.getfov(-85621, ...)`
  — it returns a symmetric ~91.6°-derived pyramid, which matches the SIS's **monochrome**-mode
  cross-track FOV (91.7°), not the narrower color-mode readout (which only uses the center 704 of
  the full ~1024-wide detector). So the IK's generic FOV entry isn't usable directly for the
  color-mode crop; the SIS's explicit color-mode figure — **61.4°** — is used instead.
- The synthetic camera's `fu=fv` is `(image_size/2) / tan(61.4°/2)` (≈215.6 px at 256x256) — its
  angular FOV literally equals the real WAC color-mode FOV at the same pose, so its footprint
  matches the real swath width by construction.
- The real cross-track ground width at frame 440's exact pose is computed by ray-tracing the
  ±30.7° rays (half of 61.4°) along the camera's cross-track axis to the Moon's sphere and taking
  the chord distance between the two ground points — **≈82.0 km** (implied GSD ≈82.0 km/704 ≈
  116 m/px, a plausible value for WAC at this ~68.5 km altitude).
- The real per-frame ground advance is the chord distance between the boresight ground point at
  frame 440 and frame 450, divided by 10 — **≈1.147 km/frame**.
- `n_frames_for_square_crop = round(cross_track_width_km / km_per_frame)` — **71 frames**, giving
  a `71*14 = 994` line x 704 sample real CDR crop. Not square in *pixels* (cross-track and
  along-track have different native GSD), but square in real km, matching the synthetic camera's
  FOV — this was the user's explicit request and tolerance.

**Gotcha (fixed):** the Lunaserv WMS tile cache is keyed by `(layer, bbox, width, height, format)`,
so after the target frame index moved from 0 to 440 the cache ended up holding tiles for *both*
footprints. `render.py`'s `run_sat_sim()` originally picked the ortho tile via
`ls .../luna_wac_global/*.tif | head -1` — which silently grabbed the *stale* (frame-0) tile,
mismatched against the freshly-regenerated (frame-440) DEM. Fixed by having
`fetch_lunaserv.fetch_dem_and_ortho()` write the exact resolved paths it used to a result file,
which the render step sources — never glob the cache dir for "any" tile of a layer.

### Pose epoch fix: crop's temporal midpoint, not its start

The real CDR crop spans `n_frames` (71) frames *starting at* `target_frame_index` (440) — frames
440 through 510. The synthetic camera's pose was being computed at frame 440's exact timestamp —
the crop's *start*, not its middle — so the synthetic image's center should have lined up with the
real crop's *top edge*, not its center. Fixed in `camera.build_camera()`: compute `crop_info` (and
thus `n_frames_for_square_crop`) first using `target_frame_index`'s geometry as the estimate
(negligible drift over ~71 frames/~49 seconds), then derive
`center_frame_index = target_frame_index + n_frames/2 = 475.5` and use *that* epoch for the actual
pose (`C`/`R`, focal length base, footprint corners, and hence the Lunaserv ROI too). No change was
needed on the real-crop side — it correctly starts at frame 440 regardless.

### Comparison-figure aspect ratio

`imshow()` with no `aspect`/`extent` renders one array cell as one square screen unit regardless of
row/column counts, so the CDR crop's 994x704 array displayed as a tall rectangle even though the
ground area it represents is square. Fixed in the notebook by plotting both panels with
`extent=[0, width_km, height_km, 0]` (real km, not raw pixel index) — the synthetic panel uses
`cross_track_width_km` for both axes (its FOV is symmetric by construction); the CDR panel uses
`cross_track_width_km` for width and `n_frames_for_square_crop * km_per_frame` for height (the
actual achieved along-track distance, which can differ very slightly from `cross_track_width_km`
due to `n_frames` being rounded to an integer).

### SPICE-derived tie points (`src/trntest/tie_points.py`)

Adds 5 explicit tie points (a die's "5"/X pattern: 4 corners + center) to the comparison figure,
computed from the real camera geometry rather than eyeballed: find each image's own ground
footprint, an (approximate, isotropic-shrink) inscribed axis-aligned lon/lat box per image,
intersect the two boxes, place 5 points inside with a 10% margin, and project each into both
images' pixel coordinates.

- Synthetic image: closed-form pinhole inverse (`project_ground_to_synthetic_pixel`) — exact,
  single fixed pose, axis-agnostic (just uses the real `R` directly).
- Real CDR crop: mixes many real poses (one per frame), so `project_ground_to_crop_pixel` bisects
  over frame index for where the along-track camera component crosses zero, then reads the
  cross-track column from that frame's pose. **Bug found and fixed** during implementation: the
  bisection's sign-change precondition (`(f_lo>0)==(f_hi>0)`) fired incorrectly when a target point
  sat almost exactly at one of the search boundaries (e.g. the crop's own corners, which are
  defined *at* frames `start_frame`/`start_frame+n_frames`) — `f_lo`/`f_hi` would be a tiny nonzero
  float of a consistent sign, not exactly 0, so the "already at the root" case wasn't caught before
  the sign-change check ran. Fixed by checking `abs(f_lo) < tol` / `abs(f_hi) < tol` first.
- **Verified via a self-consistency check** (not just "no exception raised"): projected each of the
  real crop's own 4 defining corners back through `project_ground_to_crop_pixel` and got back
  exactly `(0,0)`, `(704,0)`, `(0,994)`, `(704,994)` — confirms the cross-track sign convention and
  frame-to-row mapping are correct.
- **Finding — the two images were rotated ~90° relative to each other, and that was real, not a
  bug.** Cross-projecting each image's own (inset) corners into the *other* image's pixel space
  showed synthetic `top_left` ≈ crop `bottom_left`, synthetic `top_right` ≈ crop `top_left`, and so
  on around — a consistent 90° rotation, confirmed numerically (closest-corner matching, ~0.06-0.4°
  residual). This was a direct consequence of the two images' differing pixel-axis conventions given
  the WAC-VIS **X = along-track, Y = cross-track** finding: the crop's rows were built to be
  along-track (X) and columns cross-track (Y), while the synthetic image's pixel mapping had rows
  and columns swapped relative to that. Fixed by the sensor-model axis convention change below.

### Sensor-model axis convention (original derivation)

The 90° mismatch above came from the synthetic camera's pixel axes being an arbitrary in-house
choice (`px→X, py→Y`) with no relation to any instrument convention. Fixed by rotating the camera's
`R` by 90° about its own boresight before writing the `.tsai` — deliberately **not** influenced by
which way is "north" for this pass (a separate, later concern; see "North-up display rotation"
below).

- Checked NAC's own convention too (LROC SIS): NAC is a simple pushbroom line-scan camera,
  "5064-pixel CCD line-array providing a cross-track field-of-view" — i.e. NAC's samples are
  cross-track too. So this isn't actually a WAC-vs-NAC fork: both instruments' real archived-image
  layouts agree (samples/columns = cross-track, lines/rows = along-track) — one convention to adopt
  and motivate, not a choice between two.
- A pinhole camera's rendered image is fixed only up to rotation about its own boresight (a proper,
  handedness-preserving rotation). Rotating `R` by `rotation_about_boresight(k)` for `k=0,1,2,3`
  cycles which raw camera axis (`X`, `Y`, `-X`, `-Y`) maps to `px` (and correspondingly the other to
  `py`). Two of the four (`k=1`, `k=3`) put `px∥Y` (cross-track) and `py∥X` (along-track) — the
  desired convention; `k=0`/`k=2` keep the original, unmotivated mapping.
- Between `k=1` and `k=3`: picked `k=1` (for *this* product) so that increasing `py` (row) matches
  the same temporal sense as the real archived WAC image's row axis (which increases forward in
  time, by construction of how the frame-stacking works). Consecutive-frame ground-track motion
  measured as dominantly `-X` in the raw WAC-VIS frame for this product — i.e. "forward in time" is
  `-X` here. `k=1` maps `py→-X`, matching that sense.
- **This was originally asserted to be a hardware/data-format property, fixed regardless of orbit
  pass/yaw state — that assertion was wrong** (Phase 9 above): a second, independently-selected
  product measured dominant `+X` instead, and the fix (`boresight_rotation_k`, computed per-pose)
  is now current behavior — see `docs/data-sources.md`.
- This change altered the actual rendered pixels (a real 90° rotation of the output image), so the
  pipeline had to be (and was) re-run: render + `cam_gen`.
- **Verified**: re-ran the crop-corner self-consistency check (still exact) and the
  synthetic-vs-crop closest-corner match — now `top_left↔top_left`, `top_right↔top_right`, etc.
  directly (not the previous 90°-rotated pairing) — and visually, all 5 tie-point markers sat on
  matching terrain in both panels.

### North-up display rotation (`src/trntest/orientation.py`, notebook-only) — derivation

Deliberately kept **separate** from the sensor-model fix above: which way is "north" depends on
this specific pass (ascending vs. descending) and the spacecraft's yaw state, so it must not
influence the camera model, the `.tsai`, or the CSM/ISD JSON — it's purely how the notebook plots
already-rendered/extracted arrays.

- `north_tangent_km(ground_km)`: local north-pointing tangent (`polar - (polar·p̂)p̂`, normalized).
- `best_k_for_north_up(right_orig, up_orig, north, candidates)`: for each candidate `k` (a
  `np.rot90(arr, k)` rotation), the resulting on-screen "up" direction is
  `sin(k·90°)·right_orig + cos(k·90°)·up_orig` — derived from "rotating the displayed array by
  `np.rot90(arr,k)` physically rotates the image `k·90°` counter-clockwise," and **verified
  numerically** against `np.rot90` directly (marked-pixel test) before trusting it, since the
  hand-derived algebra for this kind of thing is easy to get backwards. Picks whichever `k` has the
  highest dot product with true north.
  - Synthetic image: all 4 `k∈{0,1,2,3}` are valid candidates (a free display rotation of an
    already-rendered array; the sensor-model's fixed convention is irrelevant to *this* choice).
  - Real crop: only `k∈{0,2}` are meaningful (its row axis is real along-track data; a 90°/270°
    rotation would put cross-track on the vertical axis, not "north-up").
- `rotate_pixel_coords(col, row, k, height, width)`: maps a pixel coordinate through the same
  `np.rot90(arr, k)` transform, for repositioning tie-point markers on the rotated display. Also
  **verified numerically** rather than trusted from hand-derived array-index algebra alone — an
  off-by-one crept into the first attempt (dropping a `-1` when moving from discrete array indices
  to continuous pixel coordinates) and was caught this way.
- One run's result (product `M1329714703CE`): both images picked `k=2` (180°) with the same
  residual deviation from true north (26.7°) — expected, since after the sensor-model fix, both
  images already shared the same axis convention. The nonzero residual reflects that this pass's
  along-track direction isn't exactly north-south — the best achievable result under the "only
  multiples of 90°, no mirroring" constraint, not a bug.

## Phase 25 (2026-08-09) — Found and worked around a real `usgscsm` bug: Phase 24's "fix" was a false positive

Phase 24's fix (patching a cropped cube's ISD ephemeris-time fields, 0.999 correlation) shipped and
was committed. The user then manually inspected the actual notebook output and found real, still-
present defects Phase 24's own checks had missed: the overlay had **three disconnected regions**
(a genuine gap, not a display artifact) instead of one contiguous shape, and sat measurably left of
where Phase 5B's synthetic-render overlay landed. The user explicitly redirected away from more
self-directed visual interpretation ("your visual perception analyzing images is a bit suspect... prefer
to ask me to check it manually") and toward reading real source rather than guessing — this phase is
the resulting investigation.

**Consulted the paper the user found** (Laura, Mapel & Hare 2020, DOI 10.1029/2019EA000713,
specifically "Table 2 Continued"). Turned out not to cover Pushframe sensors at all — Table 2 lists
only framing/line-scan sensors, and the paper's conclusion lists "push frame sensors" as future
work. It did usefully confirm `center_ephemeris_time`'s Table 1 description (`t0_ephemeris`/
`t0_quaternion` given "relative to center image time") — which turned out to exactly explain an
earlier-session finding that had looked like a contradiction (see below). Fetching the paper itself
was also an early obstacle: Wiley's bot detection blocked every automated attempt (`WebFetch`, `curl`
with browser headers, Unpaywall/Semantic Scholar lookups all confirmed it's genuinely open access,
just inaccessible programmatically) — the user manually placed the PDF in the workspace instead.

**Re-derived the actual mechanism from source, catching a version mismatch first**: this
container's `libusgscsm.so.2.0.1` differs meaningfully from `usgscsm`'s `main` branch on GitHub —
re-fetched the real `2.0.1` tag before trusting anything read from it. Traced
`UsgsAstroPushFrameSensorModel::getImageTime()`: it computes an absolute time from
`m_startingEphemerisTime`/`frameletNumber`/`interframeDelay`, then **subtracts
`m_centerEphemerisTime`** before returning — every downstream lookup works in time-relative-to-center.
Since the position/quaternion tables' own anchor times (`m_t0Ephem`/`m_t0Quat`) are built the same
way, `center_ephemeris_time` **algebraically cancels out** of every interpolation index — this is
why an earlier empirical test (setting it to `0.0`) had shown "zero effect": not a mystery, just
arithmetic, and exactly consistent with the paper's Table 1 description once traced through.
`starting_ephemeris_time` is the one field that doesn't cancel and must be right; confirmed via
`campt` (ISIS's own, unrelated camera model) that the "naive"/physically-correct value was right
all along, to within 0.02s.

**Isolated the real bug with a controlled 2×2 test**: crossing "touch `ck_table_start_time`/
`ck_table_end_time`" against "forward vs. backward `starting_ephemeris_time`" showed the `ck_table_*`
fields have **zero** effect on `mapproject`'s output (byte-identical either way) — the leading
hypothesis, eliminated. Only the timing direction mattered for output *shape*, but neither produced
correct *content*: a direct correlation check against the known-good full-cube reference, at the
crop's own true (no-shift) location, gave only ~0.40 either way, and a ±5km shift search barely
moved it (0.44 peak) — ruling out a simple translation error.

**The decisive test was `cam_test`'s image→ground→image round-trip, iterated**: comparing a camera
model against *itself* should recover the same pixel almost exactly. It didn't (median ~67px error
on the 70-framelet crop vs. ~17px on the full 258-framelet cube) — but critically, chaining the same
transform repeatedly (feed the recovered pixel back in) never converged to a stable fixed point; it
drifted monotonically toward the image boundary. A genuine "found a different but valid answer"
(e.g. an adjacent overlapping framelet) would show up as a stable fixed point within 1-2 iterations;
this didn't, ruling that theory out and pointing at real non-convergence.

**Root cause, confirmed from source**: `UsgsAstroPushFrameSensorModel::groundToImage` does an
**unbracketed secant search over discrete framelet index** — starts from `[0, numFramelets-1]`,
up to 20 iterations of `offset = endDistance*(endFramelet-startFramelet)/(endDistance-startDistance)`,
with no check that a root is actually bracketed and no monotonicity guarantee for the underlying
`calcFrameDistance` (plausible given real ground-coverage overlap between adjacent Pushframe
exposures). A 70-framelet crop gives this a much shorter, more fragile baseline than the full
cube's 258. Confirmed (via ASP's own source, `mapproject_single.cc`'s `demPixToCamPix()` →
`CsmModel::point_to_pixel()` → `m_gm_model->groundToImage()`) that this is exactly what
`mapproject` calls once per output pixel.

**Not just a crop-size problem**: cross-checking `cam2map`'s reprojection of the crop against its
reprojection of the *full* cube (same tool, same projection, only the input differs) gave 0.9999986
correlation over their full overlap — but the old ASP/CSM full-cube reference used throughout this
notebook's earlier validation only agrees with either at ~0.2-0.4. The "known-good" reference this
whole project had trusted for comparison was itself measurably affected by this bug, just less
severely than the crop.

**Fix: bypass `usgscsm`/CSM/ASP `mapproject` entirely for the real WAC crop**, using ISIS's own
native camera model (a completely separate C++ implementation, reads pointing/timing straight from
the cube's cached SPICE data) via `cam2map`. Confirmed clean via `campt` at the crop's center and
all 4 edges (no errors/NaNs anywhere) and `cam2map`'s own output contiguity (smooth row-by-row
valid-fraction profile, no gaps — unlike the old CSM crop's 3-region defect). This is very likely
also why real LROC WAC global mosaics (which predate `usgscsm`) are solid: they almost certainly go
through ISIS's native Pushframe model, not the newer CSM plugin.

Getting `cam2map` onto the same real-world coordinate system as the rest of the pipeline needed one
more verification step: confirmed ISIS's native Orthographic projection agrees with GDAL/PROJ's
`+proj=ortho` to sub-micrometer precision for matching center/radius (a first attempt at this check,
via `mappt`'s `coordsys=map` option, gave wildly wrong numbers — traced to a tool-usage mistake, not
a real discrepancy: that option's reported X/Y reflects the `FROM` cube's own native projection, not
the override, caught by back-computing the reported value against the `FROM` cube's own grid).
`isis_wac._orthographic_map_pvl()` clones `LunaservResult`'s own local Orthographic CRS (center
lat/lon, spherical Moon radius, pixel resolution) into an ISIS PVL map file for `cam2map` to target.
Two gotchas along the way: `cam2map`'s `PIXRES` parameter defaults to `CAMERA` (silently ignores the
map file's own `PixelResolution` unless explicitly set to `PIXRES=map`), and `gdal_translate` on the
resulting cube prints a `PROJ: proj_create_from_name` stderr error (an ISIS/GDAL `PROJ_LIB`
environment mismatch) that's harmless — confirmed the output CRS/transform are correct despite it.
Deliberately **not** pixel-grid-aligned to `LunaservResult`'s own raster (no `UpperLeftCorner`
pinning, no follow-up `gdalwarp`/resampling pass) — `plotting.plot_overlay` composites both rasters
via their own real georeferenced coordinates, not a shared pixel grid, so matching projection is
sufficient, and a resampling pass would have reintroduced exactly the interpolation-quality risk
this whole detour was meant to avoid.

`isis_wac.py` changes: `crop_for_camera` no longer generates an ISD at all (ISIS's native model
needs none — the cropped cube is already fully self-describing) and dropped its `frame_timing`
parameter accordingly. `run_mapproject_for_crop` (the CSM path) was replaced by
`run_cam2map_for_crop`. `run_isd_generate`/`run_mapproject` (the full-cube CSM functions) are kept
in the module for reference/comparison but are no longer called by the notebook, with docstrings
updated to record the newly-found limitation.

**Verified end-to-end after the switch**: re-ran the full notebook from cold. Phase 6B's row-by-row
valid-fraction profile is smooth and contiguous (no gaps, unlike before). Position relative to
Phase 5B's synthetic-render overlay improved substantially (~13km residual, down from ~33-35km).
`docs/plan.md` and `docs/data-sources.md` both got correction entries alongside the original
(now-known-wrong) claims, per this repo's convention of recording wrong turns rather than rewriting
history. **Both remaining points below turned out to need real follow-up** — the "not chased
further" framing originally written here was premature; the user's own manual check caught a real
issue the numeric checks above had missed.

**Follow-up 1 — striping, found by the user's manual check.** The user reported the 6B overlay
showing ~36 alternating stripes of data/nodata at what looked like framelet boundaries. Confirmed
the *source* cube (pre-`cam2map`) is ~99% valid across all 5 bands — no striping there — so this was
being introduced by `cam2map`'s own rasterization, not present in the pixel data. A raw boolean-grid
dump of the output (not a coarse row/column average, which is too coarse to see this) showed real,
wide diagonal bands of missing data matching individual framelets tilted into map space. Root cause:
`cam2map`'s `WARPALGORITHM=AUTOMATIC` (ISIS's own docs recommend this *specifically* for push frame
cameras) locks the patch size to the full framelet height (14px) and silently drops any patch whose
affine-fit isn't within 0.1px of the camera model's own computation — that check was failing for
roughly half the framelets at this map resolution, confirmed present on the *full* cube too (not
crop-specific), which is why the earlier crop-vs-full correlation check hadn't caught it (both were
missing data at the same spots, so wherever both *did* have data, they still agreed almost
perfectly — that check validated content correctness, not coverage completeness). Fix: explicit
`WARPALGORITHM=forwardpatch PATCHSIZE=4` (small patches fit their local affine transform accurately
enough to pass). Verified: coverage went from ~47% to ~71% (no more gaps) with content correlation
still excellent (0.9954, vs. 0.9999986 at the broken default — the small drop is patch-fit noise,
not a regression). Re-verified in the real pipeline output after the fix, not just the scratch test.

**Follow-up 2 — the ~13km position offset was real, not a centroid artifact.** Directly compared
`campt`'s reading at the crop's own designated center pixel against `crop_footprint`'s independently
ray-traced center (which use the *same* frame-index formula) and found they genuinely disagree by
~11km, not just the valid-pixel centroid. Ruled out an off-by-one/reversed-frame-range bug in
`crop_window_for_camera` first (the user's own hypothesis, worth taking seriously): reconstructed
ISIS's own per-line time formula from three exact frame-boundary `campt` queries and confirmed the
crop window selects exactly the intended chronological frame range, correctly accounting for
`framestitch`'s line-order reversal (`flip=True` for this product) — ISIS's per-line time matches
our own `frame_et()` to within 0.016s for the corresponding frames, so the *timing/frame-selection*
side is correct. The actual cause: at that same matched instant, our own SPICE-based pointing
computation (`camera.camera_pose_moon_me`) and ISIS's own camera model disagree by ~11km. Traced to
ISIS's `spiceinit web=yes` pulling in a second CK kernel, `moc42r_2019304_2019335_v01.bc` (name
suggests a mission-ops-reconstructed attitude product), that `spice_kernels.py`'s own
`WAC_CK_PREFIXES = ("lrosc", "lrolc")` never fetches — confirmed this kernel isn't even listed in
the NAIF metakernel our own code already parses, so it's not a simple missing-prefix fix; it needs
a different kernel source entirely. **Left as a known, documented residual per user direction** —
real, diagnosed, but out of scope to chase further right now.

**Also tried, and ruled out empirically**: the user asked whether ASP's `mapproject -t isis` (uses
ISIS's own native sensor model instead of `usgscsm`/CSM, given a plain `.cub` with no separate ISD)
might be a simpler alternative to the hand-written PVL + `cam2map` approach above. Tested directly
against the same crop cube: `mapproject` immediately rejected it — `"ERROR: Unusual input file...
Seems to have Isis camera type 1. Check your data. Maybe it will work with CSM."` — ASP's own ISIS
session wrapper doesn't support this camera type (Pushframe), full stop, not a flag/workaround
issue. `cam2map` remains the only working native-ISIS path found for this sensor.

## Phase 26 (2026-08-09) — The stripe/crosshatch artifact: root-caused as a Lunaserv server problem, fixed by switching DEM source to Astropedia

`docs/plan.md`'s open item (subtle stripe/crosshatch artifacts in the synthetic render, worse in
darker areas) turned into this session's longest single investigation — several real fixes attempted
against the wrong layer of the problem before the actual root cause (and simplest fix) emerged.
Built a reusable FFT/periodicity diagnostic toolkit along the way (`periodicity_report`,
`power_at_freq_and_angle`, `db_above_trend_at_freq`, `annotated_fft_plot` — now archived in
`old_notebooks/stripe_debug.py`, see its own docstrings for the exact methodology) that's worth
reusing for any future artifact-hunting in this pipeline.

**Starting diagnosis**: `fetch_dem_and_ortho` requested the DTM layer
(`luna_wac_dtm_numeric_meters_absolute`) from Lunaserv in a per-camera rotated local Orthographic
CRS at `dem_target_gsd_m` (100 m/px). A live resolution sweep (50/100/200/300/400/500/700/1000 m/px,
same bbox/CRS) showed a strong periodic artifact locked to ~2 pixels of *whatever resolution was
requested* (+14 to +40 dB above the natural terrain power spectrum) at or finer than 300 m/px,
vanishing between 300 and 400 m/px and replaced by a real, resolution-invariant ~18.6 km terrain
feature — the signature of server-side resampling past real detail, not real DEM content. Cross-
checked against the layer's own `GetCapabilities` abstract ("available at 128 ppd") and independent
web research into GLD100's real tiling: Lunaserv's DTM layer serves the coarser of GLD100's two
native tiers (128 ppd/~237 m), not the finer 256 ppd/~118 m tier.

**First fix (a real, validated improvement, later superseded)**: fetch the DTM in Lunaserv's native,
unrotated geographic CRS (`IAU2000:30100`) at 128 ppd — no server-side reprojection at all — then
reproject locally via `rasterio.warp.reproject` onto the same per-camera local Orthographic grid the
ortho already uses. Validated first with a small standalone spike before touching the real pipeline
(confirmed `rasterio.warp.reproject` could open/warp the native CRS via a generic PROJ4 string,
`+proj=longlat +R=<moon radius>`, without needing GDAL to recognize Lunaserv's `IAU2000:*` codes by
name) — this removed the original near-Nyquist checkerboard cleanly (confirmed via the same FFT
diagnostic against the real pipeline output, not just the spike).

**A second, different artifact survived the first fix.** The user visually caught it in the
notebook's zoomed hillshade crop: axis-aligned to the image, straight lines, regular spacing —
unlike the first artifact, which wasn't aligned to the final image's own axes and looked slightly
curved. What followed was a long chase, each step genuinely tested against the real numbers (not
assumed), several of them real but ultimately insufficient improvements:

- **Computing the hillshade near-native-resolution before the final upsample** ("Option A" in the
  investigation): `LightSource.hillshade()` just computes `np.gradient` on elevation for a per-pixel
  normal, then a dot product against the sun direction — so differentiating an already-2.4x-upsampled
  elevation array amplifies any reconstruction ripple, while differentiating near-native resolution
  and only upsampling the smooth, bounded hillshade *scalar* afterward shouldn't. Real, substantial
  effect on one component (~490 m wavelength, ~370x power reduction with a bilinear final-stage
  kernel) but the user still saw visible crosshatch.
- **Resampling kernel choice for the final upsample**: bilinear beat cubic and lanczos for this
  specific case (tested directly, not assumed).
- **GDAL's approximate-transformer `tolerance`** (`rasterio.warp.reproject`'s `tolerance`, maps to
  `gdalwarp -et` — by default GDAL transforms only 3 points per output scanline and linearly
  approximates the rest, a plausible source of geometric ripple for a genuinely curved transform).
  Tested `tolerance=0` (exact per-pixel transform): only a ~20-25% improvement, not the fix.
- **A frequency-targeted notch filter**, once the artifact's precise frequency/direction was known
  (see below): a real, substantial improvement (~12-26x power reduction) via a filter that only
  touches those two specific frequencies rather than broadly suppressing everything above a cutoff
  like a Gaussian blur does. The user flagged two real problems with it anyway: visible crosshatch
  still remained in the most sensitive areas (the darkest part of a large crater), and a legitimate
  overfitting concern — a notch tuned to one image's specific geometry wouldn't generalize to others
  (e.g. the X/Y frequency split depends on the camera's own latitude, since it turned out to come
  from the native DEM grid's real anisotropy — see below).
- **A live native-ppd sweep** (64/80/96/112/128 ppd through the full pipeline): best at ~112 ppd, but
  the user reported the crosshatch "still apparent at all ppd values... modestly improved at 112" —
  "It's maddening that the WMS server is a black box from this perspective."

**Getting the frequency right took two corrections.** The first quantitative read (mis-targeted,
~952 m one axis) turned out to be wrong — a properly-annotated 2D FFT plot (concentric circles at
labeled real-world wavelengths, angle gridlines, built specifically so the user could read off the
real peak directly rather than trusting Claude's own guess at which bin mattered — see the standing
feedback on this) let the user correct it twice: first to ~290-380 m (a real but secondary signal),
then back to the original ~950 m (X) / ~1200 m (Y) — genuinely the dominant one, just harder to
separate from "a mess of junk near the middle" on a first look. The **X vs. Y wavelength difference
turned out to have a precise explanation**: the native DEM fetch is anisotropic in real meters (128
pixels per *degree* in both lon and lat, but a degree of longitude covers less real ground than a
degree of latitude away from the equator) — computed exactly: 236.9 m north-south vs. 185.7 m
east-west at this camera's ~38.4°N latitude, ratio 1.275, matching the observed wavelength ratio
1200/950 = 1.263 almost exactly.

**Root cause, finally confirmed properly** (a corrected, per-axis FFT check — the first attempt used
an *averaged* isotropic native pixel spacing, which doesn't correctly test an anisotropic array on
either axis, and wrongly found nothing): the raw native Lunaserv tile itself shows a real,
consistent **+4.1 dB (X) / +4.0 dB (Y)** anomaly at exactly the observed frequencies, on both axes —
this is baked into Lunaserv's own native 128 ppd tile, not introduced by this project's own
reprojection. A live ppd sweep (32 through 256 ppd, both a "fixed real-world wavelength" and a
"fixed number of native pixels" hypothesis tested directly) then showed the artifact's signature is
weakest around ppd~64-96 and grows above that — consistent with Lunaserv's own server doing *its
own* internal resampling from a true backing resolution coarser than the 128 ppd its layer abstract
claims, the same mistake this project had already fixed on its own end, just one layer further
upstream, and not something reachable from the client side: no response headers or TIFF metadata
reveal Lunaserv's backing store, and 5 different vendor `GetMap` parameter names were all confirmed
ignored (byte-identical responses) — no resampling-method control exists.

**Decision: switch the DEM source to USGS Astropedia's flat-file GLD100 distribution.**
Confirmed live via `gdalinfo` on the real file
(`https://planetarymaps.usgs.gov/mosaic/Lunar_LRO_WAC_GLD100_DTM_79S79N_100m_v1.1.tif`): genuine
100.0 m/pixel (not 128 ppd/~237 m), Int16 real elevation (not planetocentric radius, `Min=-9091
Max=10761`, `NoData=-32768`), 79°N-79°S coverage (`gdalinfo`'s own corner coordinates:
79°0'6.57" both ways), ~10 GB, Equidistant Cylindrical projection (`lon_0=180`, standard parallel 0).
Not a Cloud-Optimized GeoTIFF (`Block=109165x1` — row-strip layout, not 2D-tiled), confirmed via a
real windowed `/vsicurl/` pull: ~64s for one small AOI (pulls full-width row strips, not a small
tile). The same FFT diagnostic run against that real pulled AOI found **none** of Lunaserv's
artifact (X: -5.2 dB, Y: +5.0 dB, both at/below the natural trend) — confirming it's specific to
Lunaserv's serving pipeline, not the underlying GLD100 terrain data. It does have its own, different,
near-Nyquist artifact (~143-149 m, +26-27 dB) almost certainly from the file's own Int16 (1 m step)
elevation quantization — the user confirmed directly (inspecting `notebooks/astropedia_check.py`'s
real reprojected hillshade, now `old_notebooks/astropedia_check.py`) that this isn't visually
apparent and didn't want it addressed. Also checked and ruled out: the finer 256 ppd/~118 m GLD100
tier exists only as 8 quadrangle tiles covering just ±60° latitude (narrower than this 100 m/px
file's ±79°) — not worth the coverage tradeoff.

**Implementation**: `fetch_dem_native`/`reproject_dem_to_local_grid` (the Lunaserv-native path from
earlier in this phase) kept, marked deprecated in their own docstrings — the same "kept for
reference/comparison, no longer used" precedent this project already established for
`isis_wac.py`'s old CSM path. `reproject_dem_to_local_grid`'s warp core was factored into a private
`_reproject_raster_to_local_grid` helper (parametrized by source CRS/transform rather than
hardcoding geographic-degree assumptions) so the new Astropedia path (`fetch_dem_astropedia`,
`reproject_astropedia_elevation_to_local_grid`) shares it without duplicating the
`rasterio.warp.reproject` boilerplate — confirmed the deprecated function's own behavior is
byte-for-byte unchanged (its existing tests pass with zero modifications). New
`lunaserv.astropedia_coverage_bbox_deg`/`ASTROPEDIA_MAX_ABS_LATITUDE_DEG = 79.0` raise a clear
exception for any camera footprint needing data outside Astropedia's real coverage — no silent
fallback to the deprecated, artifact-affected Lunaserv path.

**Caching the whole ~10 GB file, resumably**: per the user's explicit direction ("take the hit and
just grab the whole 10 GB data set, but make sure we grab it only once" + real resume robustness
against an imperfect network). Checked two Python libraries before settling on the approach: `pooch`
(a common scientific-Python data-fetching library) turned out, on reading its actual
`HTTPDownloader` source, to have **no** resume support at all — opens the destination `"w+b"` and
always overwrites from scratch; `pypdl` does support real resume but is a smaller,
less-established `aiohttp`-based multi-segment library, more new-dependency weight than the user
wanted. Landed on shelling out to `curl -fL -C -` (`curl` is already a Docker image dependency, used
for the ASP tarball fetch) — `cache.fetch_astropedia_gld100` is deliberately *not* built on the
existing `cached_get` helper, because two of `cached_get`'s own design choices (a fresh
uniquely-named temp file every call, and deleting it on any failure) are exactly correct for small
WMS tiles but actively defeat resume for one huge file. Uses a stable `<dest>.part` path instead,
and leaves it in place on failure. **Verified for real, not just assumed from `curl`'s own docs**:
started the real ~10 GB download, killed the container mid-transfer at byte 931,119,104, reran, and
`curl` logged `** Resuming transfer from byte position 931119104` — an exact match — then completed
the remaining ~8.87 GB. Confirmed the "only once" contract too (a second call after completion
returned in 0.00s).

**One more thing checked before calling this done, not assumed**: Astropedia's Int16 (1 m step)
elevation encoding is coarser than Lunaserv's float32 (~0.125 m ULP) that `render.py`'s
`DEM_HEIGHT_ERROR_TOL_M = 0.5` was originally tuned for (see Phase 15) — a real question of whether
that tolerance might now be too tight again, reintroducing `sat_sim` ray-intersection speckle with
the new, coarser data. Checked directly: rendered the same real camera/DEM/ortho at
`--dem-height-error-tol` 0.5/1.0/2.0/4.0, measuring each render's isolated-single-pixel-outlier rate
(`lunaserv.despeckle`'s own outlier test, used purely as a measurement here). All four came out
~0.444-0.447% — no meaningful difference, unlike Phase 15's original sweep (order-of-magnitude
swings in both directions) — no change needed.

**Validation**: the full real pipeline (`fetch_dem_and_ortho` against a real camera) ran end to end
using the new path, producing the same `LunaservResult` shape every existing consumer expects, with
the FFT diagnostic confirming the output matches what was validated in the notebook (only the
already-accepted ~350-375 m quantization artifact, no trace of Lunaserv's crosshatch). All 18
existing `test_lunaserv.py` tests pass unchanged (confirming the deprecated path's behavior really
is untouched); 4 new tests cover the latitude guard and the Astropedia reprojection path. The full
demo notebook (`scripts/run_notebook.sh notebooks/lunar_sat_sim_demo.py`) ran clean end to end
(17 cells, sequential execution, zero errors) via the live catalog-driven default. `trntest-lint`
passes (`ruff check`'s `--force-exclude` had to be added to `_lint.py`'s ruff invocations — ruff's
own `exclude` config, added for the new `old_notebooks/` archive directory below, doesn't apply to
explicitly-passed file paths without that flag, which is how `_lint.py` invokes ruff for its
changed/untracked-file diff mode).

**Archived the investigation notebooks** (`stripe_debug.py`/`.ipynb`, `astropedia_check.py`/`.ipynb`)
to a new top-level `old_notebooks/` directory, per the user's explicit request: these are genuinely
useful diagnostic records (the real executed plots, not just code) but won't be kept in sync going
forward, unlike this repo's two real tracked notebook pairs. For each, the **`.ipynb` is the source
of truth** (already-executed output preserved) and the paired `.py` was derived *from* it via
`jupytext --to py:percent` — the reverse of this repo's normal direction. `old_notebooks/README.md`
describes each; `AGENTS.md` points at the folder. `pyproject.toml`'s `[tool.ruff] exclude` now lists
`old_notebooks` to keep it genuinely out of ongoing lint scope, matching how `_lint.py`'s own
notebook-pairing checks already only scan `notebooks/`.

**Left for later, not implemented**: Astropedia's ±79° coverage gap. The user pointed at NASA's VIRA
project (`github.com/nasa/vira`, `scripts/download_dems.sh`) as reference — confirmed real and
concrete: genuine LOLA-derived polar DEMs from `pgda.gsfc.nasa.gov`/`imbrium.mit.edu`, down to
**5 m/px** near 87°S (LOLA ground tracks converge near the poles, giving far denser altimetry there
than equatorial GLD100 — the source of the user's "ironic" observation that higher-res data exists
right where Astropedia's coverage ends). See `docs/plan.md`'s open items.

## Phase 27 (2026-08-09) — Chased the "missing CK" pointing discrepancy; built a real fix, then found the original bug wasn't reproducible

Picked up `docs/plan.md`'s longest-standing open item: Phase 25 had diagnosed a ~11-13km pointing
discrepancy between this project's own SPICE computation (`camera.camera_pose_moon_me`) and ISIS's
own camera model (`spiceinit web=yes`), traced to a CK kernel (`moc42r_*.bc`) ISIS furnishes that
`spice_kernels.py`'s `WAC_CK_PREFIXES = ("lrosc", "lrolc")` never fetched. The user's own framing
going in: ISIS has a mechanism to resolve exactly which kernels an instrument/epoch needs and furnish
them — could this project "ride along" with that mechanism instead of maintaining its own
NAIF-metakernel-based heuristic, and might ISIS's kernel be better because it reflects a
bundle-adjustment refinement based on the imagery?

**Research: where ISIS actually gets `moc42r` from.** `spiceinit web=yes` calls USGS's own ALE-based
SPICE web service (`https://astrogeology.usgs.gov/apis/ale/v0.9.1/spiceserver/`, found via
`spiceinit.xml` inside the Docker image), which — like local, non-web `spiceinit` — resolves kernels
via `kernels.*.db` PVL index files sourced entirely from USGS's own public S3 bucket (`asc-isisdata`).
Confirmed via `/opt/conda/envs/isis/etc/isis/rclone.conf`: LRO's `rclone` remote has **no** `naif:`
union (unlike Dawn/Cassini/TGO, which do) — LRO's entire ISIS kernel tree is NOT proxied from NAIF at
all. `moc42r_*.bc` is real, public, and anonymously fetchable from that bucket (~1.7GB per ~30-day
merge) — confirmed absent from every NAIF-hosted path checked. **Corrected the user's
bundle-adjustment hypothesis**: both NAIF's `lrosc`/`lrolc` and USGS's `moc42r` are tagged
`Type = Reconstructed` in ISIS's own kernel-db vocabulary, which has a distinctly higher `Smithed`
tier for genuinely photogrammetric/bundle-adjusted products — never used for either. The better
explanation (NAIF's own `ckinfo.txt` confirms `lrosc` is itself a merge of daily `moc42_*.bc` files):
both are independently-built ~30-day merges of the *same* underlying raw telemetry, not one being
inherently more accurate.

**Design decision, made with the user via `AskUserQuestion`**: full replacement of the CK-selection
mechanism, not a narrow one-file patch — but *how* to replace it turned out to have a real wrinkle.
`kernels.0001.conf` routes WAC to two `.db` sources (`moc_kernels.????.db`, giving `moc42r`, and
`lroc_kernels.????.db`, presumably the `lrolc`-equivalent role) — but the second route currently has
**zero** matching files in the live bucket. Reimplementing USGS's own kernel-db selection logic in
pure Python would therefore silently drop `lrolc` — a real regression. Rather than guess how ISIS's
live resolution actually fills that gap, asked the user whether "ask ISIS itself" (run a real
`spiceinit`, read its resolved `Kernels` label) was worth the cost — confirmed the WAC EDR `.IMG`
`lrowac2isis` needs is only ~28MB and already fetched by `isis_wac.py`'s existing pipeline for
unrelated reasons (not new download cost), just a new *architectural* coupling (SPICE kernel
selection now depends on running the ISIS toolchain once). User chose this, the most robust option.

**Implementation.** `isis_wac.resolve_wac_ck_kernels`: runs the existing pipeline
(`ensure_isisdata` → `fetch_edr_img` → `run_lrowac2isis` → `run_spiceinit` on `vis_even` — any of the
four parity cubes works, `kernels.0001.conf` routes WAC-VIS/WAC-UV identically) and reads the
resulting cube's `Group = Kernels` label via ISIS's `catlab` app, parsed with the `pvl` library (new
dependency — the format's real nested/duplicate-key structure isn't cleanly regex-able the way the
flat NAIF metakernel manifest is). Confirmed live (`catlab` on a real spiceinit'd
`M1329714703CE.vis.even.cub`): the `InstrumentPointing` field lists
`(Table, $lro/kernels/ck/lrolc_2019334_2020001_v01.bc, $lro/kernels/ck/moc42r_2019334_2020001_v01.bc,
$lro/kernels/fk/lro_frames_2014049_v01.tf)` — **both** kernels together, confirming the original
diagnosis's "second kernel alongside the usual `lrolc_*` one" framing. Also directly falsified the
Phase 25 record's specific filename: USGS's bucket had already re-merged `moc42r` again since that
session (`moc42r_2019334_2020001_v01.bc` now, not `moc42r_2019304_2019335_v01.bc`) — a live,
concrete demonstration of why "ask ISIS every time" beats hardcoding a filename found in a past
session. Result is persisted to `cache/isis_ck_resolution/<edr_product>.json` (per the user's own
resilience ask, addressed below) so the live web service is only ever hit once per distinct
`edr_product`. New `cache.fetch_isis_kernel`/`isis_kernel_rel_path` fetch the actual `.bc` files from
the S3 bucket, cached under `isisdata/lro/kernels/ck/...` — deliberately the same relative layout
`$ISISDATA/lro/...` itself uses (the user's "share whatever cache they have" framing), not a new
independent cache subtree. `spice_kernels.py` gained a `KernelRef(source, path)` dataclass (NAIF and
USGS-S3 kernels now have different remote roots/cache conventions, so a bare path string can no
longer say how to fetch itself), a new `select_isis_wac_ck_kernels` (live default, dispatched via
`TrntestConfig.wac_ck_source`), and the old inline NAIF-metakernel CK-selection logic extracted into
`select_naif_wac_ck_kernels` — kept, marked deprecated, not deleted (this repo's established
precedent: `lunaserv.fetch_dem_native`, `isis_wac.run_isd_generate`/`run_mapproject`).

**Resilience, per explicit user request mid-session**: "check whether we can easily make this
pipeline robust to temporary outages in network or the ISIS services that spiceinit queries." The
user specifically clarified this meant *warm-cache reuse should work fully offline*, not automatic
retry — "I'd rather have a relatively prompt exception and manually retry later." Implemented purely
via the persisted resolution cache (`isis_ck_resolution/<edr_product>.json`, checked before ever
calling `spiceinit`) — deliberately **no** retry/backoff around the `spiceinit` subprocess call
itself. Verified live: a second `resolve_wac_ck_kernels` call after the first hits the cache and never
touches the network at all.

**The trust-but-verify gate found a real bug in the fix's first draft.** Wired `verify_ck_coverage`
(defined since an earlier phase, never actually called) into `fetch_and_furnish` as a hard assertion.
Running the real demo notebook against it immediately caught a genuine design gap: `dataset.
select_dataset()` calls `fetch_and_furnish` for its own wide date-range candidate search (e.g.
2019-11-01), but `select_isis_wac_ck_kernels`'s resolution is tied to one fixed `edr_product`'s own
narrow ~30-day coverage window (2019-11-30 through 2020-01-01) — a real, out-of-range failure, not a
false alarm. Fixed by having `select_isis_wac_ck_kernels` filter the resolved kernel(s) to just those
whose filename-encoded date range actually covers the requested date, and having `select_kernels_for`
fall back to the deprecated NAIF path specifically for that "outside this product's own coverage"
case (not a general silent-failure fallback) — justified by the numerical-equivalence finding below.

**Direct empirical re-verification overturned the original diagnosis.** Before declaring success,
checked whether furnishing `moc42r` actually changes anything: it doesn't. `spice.pxform
('LRO_LROCWAC_VIS', 'MOON_ME', et)` gives byte-identical output whether `lrosc` or `moc42r` is the
co-furnished bus-attitude kernel. Traced why: `spice.ckobj` shows `lrolc` carries **direct** CK
segments for `-85620` (the WAC frame itself, not just an "offset" as the code's old comments assumed)
— confirmed decisively by removing `lrolc` entirely (furnishing only `moc42r`) and getting a hard
`SPICE(NOFRAMECONNECT)` failure, proving plain SPICE frame resolution for this camera never consults
`-85000`/`moc42r` at all. Given that, directly re-ran the actual ISIS-vs-SPICE ground-truth
comparison Phase 25 originally did (`campt`'s reported `SpacecraftPosition`/`LookDirectionCamera` at
a real pixel, transformed via *our own* `pxform`-derived rotation and compared against `campt`'s own
`LookDirectionBodyFixed`) at four independent points spread across the frame (lines 100–7000, various
samples) — **all four agreed to sub-centimeter position and 0.000000° pointing**. No discrepancy of
any kind is currently reproducible, with or without `moc42r`. The original ~11-13km number's true
cause was never pinned down — most plausible explanation: it was conflated with the *other* real bug
found in the same Phase 25 session (`cam2map`'s `WARPALGORITHM=AUTOMATIC` striping issue, fixed in
that phase's own "Follow-up 1"), since both were being chased in the same investigation before either
was isolated.

**Outcome, per explicit user direction**: kept `select_isis_wac_ck_kernels`/
`isis_wac.resolve_wac_ck_kernels` as the live default (`TrntestConfig.wac_ck_source =
"isis_resolved"`) despite it not fixing a live bug — real, independent value in matching ISIS's own
kernel resolution by construction (won't drift if USGS reshuffles its bucket again, as it already did
mid-session) rather than a hand-picked NAIF prefix list. `docs/plan.md`'s open item rewritten to state
this honestly: resolved, with a corrected premise, not "fixed an 11km bug."

**Follow-up — the full notebook re-run caught a real, serious performance regression the earlier
targeted tests missed.** The user's own instinct ("this is hanging") prompted a closer look at what
was genuinely a 30+-minute, still-growing run: `docker exec`'s live process list showed a `spiceinit`
subprocess for a *different* WAC product every time it was checked, and `cache/isis_ck_resolution/`
had accumulated over 100 distinct `<edr_product>.json` files in under 30 minutes. Traced to
`dataset.evaluate_candidate_image` (called once per catalog candidate during `select_dataset()`'s
illumination sweep — potentially hundreds of times per search window) → `anchor_start_frame_for_
centered_crop` → `camera.compute_n_frames_for_square_crop`, which unconditionally calls
`spice_kernels.fetch_and_furnish` with a *per-candidate* config (a different `edr_product` every
call). Since `resolve_wac_ck_kernels`'s persisted cache is keyed on `edr_product`, every single
candidate was a guaranteed cache miss, each paying a full, uncached EDR-fetch + `lrowac2isis` +
live `spiceinit` round-trip (~15-30s) that never existed before this session's change (the old
NAIF-metakernel CK selection was a cheap HTTP GET + regex parse, indifferent to how many distinct
products it was asked about). A static grep for `fetch_and_furnish(` call sites earlier in this same
session had found this exact line (`camera.py:242`) but failed to trace that it's reachable
transitively through `evaluate_candidate_image` — a reminder that grepping for a call site isn't the
same as tracing its actual callers, especially through several layers of indirection.

Fixed by forcing `wac_ck_source="naif_metakernel"` specifically on `evaluate_candidate_image`'s
`per_row_config` — justified by this same phase's own proof that the two sources give numerically
identical pointing, so there's no accuracy cost to using the cheap path for this specific
high-volume, exploratory use, while leaving the `isis_resolved` default in place for the much
smaller number of deliberate, final camera-pose computations (`build_camera`, once per selected
image; `isis_wac.run_pipeline`, likewise). Verified directly: re-ran `select_dataset(max_search_days
=7)` in isolation after the fix (81 candidates) and confirmed via `ls cache/isis_ck_resolution/ |
wc -l`/`ls scratch/isis_wac/ | wc -l` that neither directory grew at all during the sweep — down
from >100 new ISIS pipeline runs to zero. Full notebook re-run after the fix completed cleanly,
`trntest-lint` clean, all 104 tests pass (10 new: `Kernels`-label parsing, `KernelRef` dispatch,
persisted-cache-avoids-network, `cache.py` URL-construction).

**Tooling follow-up, prompted directly by the above** — the runaway sweep only became diagnosable at
all via `docker exec`-level process forensics (`ps aux`, `docker stats`, counting scratch dirs by
hand), because `scripts/run_notebook.sh`'s `jupyter nbconvert --execute --inplace` buffers all
output and only writes the `.ipynb` once, at the very end — a genuinely slow run and a truly hung
one look identical from the outside. Switched to `papermill --log-output --no-progress-bar
--request-save-on-cell-execute --autosave-cell-every 30`: streams live cell-by-cell output as it
happens, and writes the `.ipynb` incrementally (not just once at the end) so even the file itself
shows real progress mid-run. Papermill also writes its own `metadata.papermill` block per cell (in
addition to the standard `metadata.execution` timing nbconvert already wrote) -- found this breaks
`_check_notebook_sync`: jupytext embeds that extra block into the `.py:percent` cell marker line on
round-trip, which the checked-in `.py` source never has, so every run failed the sync check until
`scripts/strip_papermill_metadata.py` (using `nbformat`'s own read/write, not hand-rolled JSON, to
avoid trading one spurious diff for another) strips it right after execution. Added
`scripts/notebook_timing_report.py` to close a related, separate gap: per-cell timing is genuinely
recorded (`metadata.execution`, by both nbconvert and papermill, by default), but invisible in any
normal notebook view or in papermill's own live output — it now prints a per-cell duration table,
slowest-first, both to the terminal and appended to a kept log
(`scratch/notebook_runs/<name>_<timestamp>.log`, plus rolling `_latest.log`/`_previous.log`) so a
slow run can be compared against its own history, not just eyeballed once and lost. Considered and
rejected `jupyterlab-execute-timing` (the user's original ask) — it's a JupyterLab-UI-only
extension, inert for this project's headless/scripted execution path, and redundant with timing data
nbconvert/papermill already record natively.
## Phase 28 (2026-08-10) — Fixed Phase 6A's real tie-point misalignment: switched the WAC-crop side to a genuine ISIS `campt` ground-to-image query

The user noticed Phase 6A's real WAC crop was systematically misaligned from its own tie points:
real features (craters, etc.) consistently sat *south* of their matching marker, while the same
feature in the basemap panel landed right on its marker — a directional, systematic effect, not
marker noise.

**First hypothesis, ruled out by direct code reading**: `usgscsm`'s known `groundToImage` bug
(Phase 25). Doesn't apply here — Phase 6A never reprojects the crop at all (raw pixel display,
fixed north-up rotation only); reprojection only happens in Phase 6B, which already uses ISIS's
native `cam2map`, not `usgscsm`.

**Real cause**: `tie_points.py`'s WAC-crop tie-point pixel locations (`project_ground_to_crop_pixel`)
were computed via a hand-rolled SPICE frame-index bisection — deliberately decoupled, by original
design, from whatever pipeline actually produced the crop's real pixels. Confirmed empirically, live,
on this project's actual default candidate at the time (`M1329714703CE`, a near-polar, ~-80 to -82°
latitude target): comparing the old SPICE-projected `crop_px` against real ISIS `campt` ground-truth
for the same 5 die5 points showed a consistent ~92-96px along-track offset (out of 994 total lines,
~10%) for the 3 points that projected at all — and **2 of the 5 points didn't project into the real
crop at all** ("no surface intersection" under the crop's actual, real camera model).

The user's fix direction, after seeing this: stop approximating — compute the WAC-crop tie points the
same way `isis_wac.run_cam2map_for_crop` already does, via ISIS's own real, cube-embedded camera
model, using `campt`'s `coordtype=GROUND` mode (confirmed live: `campt from=<cub> type=ground
latitude=... longitude=... allowoutside=false` returns a `GroundPoint` PVL group with `Sample`/`Line`
— ISIS's standard 1-based, pixel-center convention — or a clean, distinguishable failure ["not inside
cube" vs. "no surface intersection"] rather than silently extrapolating).

**Broader unification, at the user's direction**: rather than special-case WAC-VIS, generalized the
same resolution order 6B's `cam2map` switch already established into reusable logic
(`isis_wac.resolve_ground_to_image_model`): (1) try building a CSM ISD sidecar for the full stitched
cube (`isd_generate`, same tool 5B's `mapproject` uses) and inspect its real `name_model`; (2) if it
resolves to a Pushframe sensor — confirmed live: `name_model = "USGS_ASTRO_PUSH_FRAME_SENSOR_MODEL"`
— `usgscsm`'s `groundToImage` is known unreliable for that class of camera (Phase 25), so fall back
to the crop's own native, embedded camera model, queried directly; (3) otherwise, attach the CSM
model via ISIS's own `csminit` to a private copy of the crop and query that instead. Not hardcoded to
"WAC is always Pushframe" — deriving it from a real ISD each call keeps the logic correct if this
pipeline is ever pointed at a different, non-Pushframe instrument. For WAC-VIS this always takes the
Pushframe branch today.

Considered, and explicitly declined for this pass (user's call): also unifying Phase 5's synthetic-
render tie points through the identical mechanism. Turned out the premise for why this would be
*hard* was wrong — `render.py`'s `run_sat_sim` already produces a valid CSM/ISD sidecar via `cam_gen`
on every run (this goes back to Phase 4; nothing needed to be built from scratch), it's just never
been *used* for anything. But Phase 5's current closed-form pinhole tie-point projection is already
exact (it's literally the same math/pose that rendered the image, not an approximation of some other
"real" model), so there's no correctness gap to close there — deferred as a separate, lower-priority
follow-up.

`tie_points.compute_tie_points` was split into two stages, since the real crop must exist before its
pixels can be queried, but point *selection* (die5 pattern, still SPICE-approximate — only ever used
to pick plausible candidates, not to place them) doesn't need it and should stay cheap/early:
`select_tie_points` (point selection + exact synthetic-image projection, unchanged timing) and
`resolve_crop_pixels` (real `campt` query against the actual crop, called once `isis_wac.
crop_for_camera`'s output exists). A die5 point the real camera doesn't actually see is dropped with
a printed warning, not a hard failure — confirmed live this happens for real, plausible points near
the poles, where the SPICE-approximate footprint used for *selection* can be off by enough to pick a
point outside the real camera's actual view; `resolve_crop_pixels` only raises if *none* of the 5
points resolve. The deprecated SPICE-only functions (`project_ground_to_crop_pixel`,
`_crop_pixel_at_frame`) are kept for reference/comparison, matching this repo's established
precedent for superseded code.

Verified: live comparison of old-vs-new `crop_px` on the real default candidate (numbers above);
`trntest-lint` clean; full pytest suite green (new coverage: `ground_to_image_pixel`'s PVL-parsing
and failure signaling, `resolve_ground_to_image_model`'s Pushframe/non-Pushframe branching via
mocked `isd_generate`/`csminit`, `resolve_crop_pixels`'s merge/drop/raise logic); full notebook
re-run via `scripts/run_notebook.sh` end to end, on a freshly catalog-selected candidate (this
notebook's default path re-queries the real catalog each run, so it wasn't the same product as the
diagnosis above) — resolved cleanly, no crash, Phase 6A's tie points visibly land on the matching
real craters.

**Honest limitation, worth flagging rather than hiding**: on that same fresh run, only 2 of the 5
die5 points survived (`bottom_left`/`bottom_right`; `top_left`/`top_right`/`center` all failed to
project), a higher drop rate than the diagnosis candidate's 3-of-5. `select_tie_points`'s point-
selection footprint is still the SPICE approximation this whole investigation found isn't reliable
for the real crop — it's only ever been intended to pick plausible *candidates*, but a drop rate
this high across the last two real candidates tried suggests it may be running consistently tight/
marginal, not just occasionally off. `resolve_crop_pixels`'s graceful-degrade design (drop + warn,
raise only if literally none survive) absorbs this without breaking the notebook, but if a future
candidate drops 4 or 5 of 5, the visual check becomes uselessly sparse. A natural, currently
unimplemented follow-up: rebuild `select_tie_points`'s point-selection footprint itself from a real
`campt` image-to-ground query at the crop's own 4 corners (the reverse direction of this fix), so
points are chosen from where the real camera actually looks rather than an approximation of it —
noted in `docs/plan.md` as a candidate future item, not done here (kept in scope to the crop-side
*projection* fix the user actually asked for).
## Phase 29 (2026-08-10) — Traced the WAC-crop misalignment to its real root: not a tilt, not a timing bug, but posing the synthetic camera from the wrong ground point entirely

Follow-up to Phase 28's tie-point fix: the user asked to debug the *underlying* footprint mismatch
directly -- specifically, whether the real, map-projected WAC crop's bounds roughly match the
synthetic render's bounds. They didn't.

**Investigation, step by step:**

1. **Real footprint bounds genuinely disagree.** Comparing `isis_wac.run_cam2map_for_crop`'s real
   output bounds against the SPICE-approximate footprint (`camera.footprint_lonlat_deg`/
   `tie_points.crop_footprint_corners`) on the actual selected candidate: the real WAC footprint was
   ~11-15% *larger* in every direction than the approximation -- not a rounding-scale difference.
2. **The exact center point is off by ~6-12km** (varying by candidate), confirmed via direct `campt`
   query at the same physical pixel/time the SPICE approximation claims is "center."
3. **Position and attitude were both ruled out as the cause.** At the *exact* matching ephemeris
   time (found by bisecting/regressing `campt`'s own reported `EphemerisTime` against ours),
   SPICE's position matched ISIS's real position to 0.6m, and a Wahba/Kabsch rotation fit from real
   `campt` `LookDirectionCamera`/`LookDirectionBodyFixed` correspondences reproduced our own SPICE
   `pxform` attitude to 0.0000 degrees, including on a held-out point. So the full pose (position +
   attitude) is exactly right at any given instant -- ruling out an SPK/CK/frame-kernel bug.
4. **A real, roughly-constant ~5-6 degree angular gap remained anyway.** `LookDirectionCamera` at
   the naively-assumed "center" pixel isn't `[0,0,1]` -- confirmed to hold at a near-constant angle
   across a wide line range (bisecting for where it crosses zero found no crossing at all, just a
   slow drift from ~0.102 to ~0.095 over 200 lines) and similar in magnitude on two very different
   candidates (5.75 vs 5.15 degrees, one flipped/`reverse_crop_along_track`, one not). That
   signature -- frame-relative, not time- or geometry-dependent -- ruled *in* a fixed
   hardware/calibration offset and ruled *out* a line-selection or timing bug as the primary cause
   (an earlier side-investigation into a genuine ~0.3-1.4s `frame_et` timing offset, initially
   suspected as the culprit, turned out to be a real but secondary effect -- correcting for it via
   direct ET-matching left the ~11km residual essentially unchanged).
5. **Checked whether plain SPICE has this number anywhere** before resorting to an empirical fit:
   `spice.getfov(-85621)` reports boresight exactly `[0,0,1]`; the five per-filter frames
   (`LRO_LROCWAC_VIS_FILTER_1..5`, IDs -85631..-85635, found in the real IAK
   `lro_instrumentAddendum_v05.ti`, which this project doesn't otherwise furnish) are confirmed
   *untilted* relative to the generic VIS frame (`pxform` between them is identity to <0.001
   degrees); the IAK's own `-85621` entries are light-time/CK-frame config only, no geometric
   override. Whatever correction ISIS's native Pushframe camera model applies internally isn't
   reproducible from any SPICE-visible kernel data this project furnishes or could furnish.

**First fix attempt -- built, integrated, and *empirically found not to work*.** Given the
"fixed, frame-relative offset" signature, the natural first idea was a constant correction
rotation, derived once via the same Wahba fit and applied to `camera.camera_pose_moon_me`'s raw
SPICE attitude (`isis_wac.resolve_wac_vis_boresight_correction`, threaded through
`camera_pose_moon_me`/`ground_track_step_km`/`km_per_frame`/`compute_n_frames_for_square_crop`/
`tie_points.crop_footprint_corners` via a new `TrntestConfig.apply_wac_vis_boresight_correction`
flag). Fully implemented, tested (mocked unit tests, all passing), linted clean -- and then
*live-validated against real `campt` ground truth before declaring it done* (a deliberate practice
after this exact kind of thing went wrong earlier in the session): the discrepancy was completely
unchanged (11.68km, was ~10-12km; 6.86km, was ~6.75km). The persisted correction matrix itself was
checked directly: 0.47 degrees from identity -- essentially a no-op.

**Why it couldn't have worked, in hindsight**: step 3 above already proved the *attitude* (full
rotation matrix) is exactly correct. A Wahba fit from real correspondences can only ever re-derive
whatever rotation is *actually true* -- so fitting one from data where the true rotation already
equals our own SPICE computation necessarily gives `correction = R_naive⁻¹ @ R_true ≈ identity`, by
construction. The real bug was never in the rotation matrix at all -- it's that pixel `[0,0,1]`
(image cross-track/along-track center) simply isn't *any specific real pixel's* look direction in a
way expressible as a small rotation without that rotation being a no-op. This was a genuine
conceptual error (conflating "attitude is wrong" with "our assumed principal point/target pixel is
wrong") caught only by insisting on live validation rather than trusting the mechanism because it
was clever. Reverted cleanly (confirmed back to 111 passing tests, clean lint, matching the prior
commit's baseline) before building the real fix.

**The actual fix**: stop deriving "the crop's center" from a boresight ray at all. `camera.
build_camera()` now runs the real WAC pipeline (`isis_wac.run_pipeline`) before finalizing the
synthetic camera's attitude, queries ISIS's own real camera model for the true ground point at the
crop's actual center pixel (`isis_wac.ground_point_at_pixel`, `campt`'s image-to-ground direction --
the reverse of `ground_to_image_pixel`, used at `sample=SAMPLES/2`, `line=center_frame_index *
VIS_BLOCK_HEIGHT` -- exactly `crop_window_for_camera`'s own window-center line, so this lines up
with wherever the eventual displayed crop centers regardless of `flip`: window *boundaries* are a
pure translation computed the same way either way; `flip` only reorders *content* within them, so
no separate flip-handling was actually needed here despite earlier suspicion), and re-aims the
boresight directly at that real point (`camera.look_at_rotation`: exact target boresight, roll/other
axes kept close to the original SPICE attitude via Gram-Schmidt). Camera *position* is untouched
(already proven exactly correct); only *what it points at* changes.

**A real architectural cost, paid deliberately**: `isis_wac.run_pipeline`'s signature changed from
taking a full `Camera` to a bare `flip: bool`, specifically to break the circular data dependency
(`build_camera` now needs to call it *before* the `Camera` it used to require exists). Made
idempotent at two levels (final stitched cube exists -> return it; just the `lrowac2isis` split
exists, e.g. as a side effect of `spice_kernels.fetch_and_furnish`'s default CK resolution -> reuse
it, don't re-run `lrowac2isis`), since both `build_camera()` and the notebook's own explicit Phase 6
call now reach it for the same product -- confirmed live that `spiceinit` is safe to re-run
(idempotent, same result) but `lrowac2isis`/`framestitch` are not (refuse to overwrite existing
output), so only the latter needed guarding. Net effect: `build_camera()` now costs a real
~10-20s more (the `lrowaccal`+`framestitch` steps the existing CK-resolution side effect doesn't
already cover) -- a deliberate trade of a real, bounded, one-time cost for actual accuracy, not
introduced casually.

**Deliberately out of scope, still**: `tie_points.crop_footprint_corners` (used for die5
point-*selection* and DEM/ortho AOI sizing) is untouched -- still the SPICE approximation, still
only meant to pick plausible candidates/size a generous-enough fetch area, not to place anything
precisely. See Phase 28's own deferred-item note; unaffected by this fix.

**Validated**: live re-check after the real fix landed -- both test candidates now show *exactly*
0.000000km residual between the synthetic camera's own boresight ground point and independent real
`campt` ground truth at the same target pixel (by construction, since that's literally the target --
a build-correctness check, not fresh independent proof of the underlying finding, which step 3's
Wahba/position work already established rigorously). Rotation matrix confirmed still a valid
orthonormal rotation (`R^T R = I`, `det(R) = 1`). Full `session.generate_dataset()` real flow
(catalog selection through `sat_sim` render) succeeded end-to-end with no errors. `trntest-lint`
clean, full pytest suite green (new coverage: `look_at_rotation`/`off_nadir_and_slant_range`'s pure
geometry, `ground_point_at_pixel`'s PVL parsing, `run_pipeline`'s reuse-existing-cube idempotency
path), full notebook re-run via `scripts/run_notebook.sh`.

**Likely connects back to Phase 25/27's original, never-fully-explained ~11-13km number**: both
used the same kind of comparison (`tie_points.crop_footprint_corners_for_camera`'s SPICE-approximate
"center" vs. real `campt` ground truth) this investigation now shows is expected to disagree by
almost exactly that magnitude, independent of the CK-kernel question Phase 27 chased. Not
re-verified against that exact original scenario, so stated as a plausible connection, not a closed
loop.

## Phase 30 (2026-08-10) — Fixed the die5 tie-point drop rate: real crop footprint corners, and a real campt edge instability

Follow-up to Phase 28's open item: the user noticed Phase 6A's bottom two tie points were "off-map"
(missing from the display) in the live demo notebook — exactly the drop-rate issue Phase 28 flagged
and deliberately left out of scope.

**Root cause, layer 1**: `select_tie_points`'s die5 point-selection footprint (`crop_footprint_corners`)
was still the deprecated SPICE-only ray-trace approximation, never revisited since Phase 28. Given
Phase 29's fix made the real WAC pipeline (`isis_wac.run_pipeline`) run unconditionally inside
`camera.build_camera()`, the "expensive ISIS pipeline" excuse for keeping this approximate no longer
held — by the time `select_tie_points`/`orientation.compute_display_rotations`/`dataset.
generate_dataset` run, the stitched cube already exists. Replaced `crop_footprint_corners` with
`crop_footprint_corners_for_camera` querying the real crop's actual footprint directly via `campt`
image-to-ground (`isis_wac.ground_point_at_pixel`, the reverse of `ground_to_image_pixel`) — the old
function renamed to `_crop_footprint_corners_spice_approx` and kept for reference, matching this
project's established deprecation convention.

**First attempt at the real footprint query used the *stitched* (uncropped) cube's window
boundaries** (`isis_wac.crop_window_for_camera`'s corners) — live-tested, and still dropped the same
2 of 5 points. Diagnosed directly: `campt` extrapolates cheerfully far beyond the stitched cube's own
declared line range (confirmed separately resolving well past line 1000 on a ~3600-line cube), but
the *cropped* cube's own cached SPICE table doesn't support anywhere near that much extrapolation —
querying a die5 point placed with a 10% safety margin inside the resulting "shared footprint" still
hit `**ERROR** ... no surface intersection` against the real crop. The stitched cube was answering a
question ("where would this line project if it existed") the cropped cube couldn't back up.

**Second attempt: query the cropped cube's own exact first/last pixel** (`sample`/`line` = 1 and
`SAMPLES`/height, ISIS's 1-based convention) instead. Still dropped the same 2 points — this time
`**ERROR** ... not inside cube`, a *different* failure than before. A direct round-trip test isolated
why: `campt`'s image-to-ground query at the cropped cube's exact edge pixel (1,1) succeeds and
returns a real lon/lat — but a ground-to-image query at that *exact same* resulting lon/lat then
fails ("not inside cube"). Tested insets of 1/2/5/10/20 pixels from the edge: 1/2/5px all failed the
same way, 10px and 20px succeeded cleanly. This is a real numerical-convergence limitation in
`campt`'s own ground-to-image solver within roughly 5-10px of a cropped cube's edge — not a flaw in
this project's own footprint math, and not something discovered by assumption: found by directly
round-tripping a real query and bisecting the margin empirically, the same "verify, don't assume"
practice that caught the reverted boresight-correction attempt in Phase 29.

**Fix**: `crop_footprint_corners_for_camera` now queries the cropped cube's own corners inset by a
new `_CROP_EDGE_MARGIN_PX = 20` (double the empirically-found safe threshold, for margin). Live
validated on the real default candidate: **5 of 5 die5 tie points now resolve** (was 2-3 of 5),
confirmed both numerically and visually — Phase 6A's real WAC crop and hillshade basemap panels now
show all 5 markers, correctly landing on matching craters in both, including the two that were
previously missing entirely.

**Also found and fixed in passing**: `isis_wac.crop_for_camera` (the ISIS `crop` app call) wasn't
idempotent — `crop_footprint_corners_for_camera` now calls it once before Phase 6's own explicit
call reaches it for the same product, and ISIS's `crop`, like `lrowac2isis`/`framestitch`, refuses to
overwrite an existing output. Added the same existence-check guard `isis_wac.run_pipeline` already
uses.

**A distinct, separate residual limitation, deliberately not fixed here**: re-testing against a
second, far more extreme near-polar candidate (~-81 to -83° latitude — the same one used throughout
Phase 29's validation) still drops points, with a genuinely different error (`no surface
intersection`, not an edge-margin issue). This project's die5 point-selection machinery
(`inscribed_bbox`, `intersect_bbox`, `die5_points`) works entirely in axis-aligned lon/lat rectangles
— a reasonable approximation at mid-latitudes, but one that breaks down this close to a pole, where a
degree of longitude corresponds to a rapidly shrinking real ground distance and a "rectangle" in
lon/lat space is a badly distorted shape on the actual sphere. Out of scope for this pass (the
reported bug was about the live demo's actual mid-latitude candidate, now fully fixed); noted in
`docs/plan.md` as a known, distinct limitation rather than silently left unmentioned.

Verified: 118 passing tests (new: `crop_footprint_corners_for_camera`'s inset-margin logic, mocked),
`trntest-lint` clean, full notebook re-run via `scripts/run_notebook.sh`, and direct visual
confirmation of Phase 6A showing all 5 tie points correctly placed.

## Phase 31 (2026-08-10) — Fixed Phase 3's DEM corner nodata: two independently-padded bboxes were never guaranteed to cover each other

The user spotted nodata sentinel values (~-3e38) right at the corners of Phase 3's displayed GLD100
DEM — visually, like the valid data was "supposed to cover the whole map but just barely missed the
corners because it was warped slightly."

That description was exactly right. `fetch_dem_and_ortho` computes the destination working grid's
bbox (`bbox`, in the per-camera local-Orthographic CRS, meters) by padding the camera footprint's own
meters-space bbox by `dem_padding_fraction` (30% by default). Separately, `fetch_dem_astropedia` (via
the old `astropedia_coverage_bbox_deg`) computed the *source* AOI to read from the local Astropedia
GLD100 file by padding the *same* footprint's degree-space bbox by the *same* fraction — but
independently, in a different coordinate system. Confirmed live, directly, before touching any code:
built a real candidate, computed both bboxes, then inverse-projected the destination grid's own four
corners back to lon/lat and checked them against the fetched degree bbox — **all four fell outside
it**, by up to ~5km in some directions. The root cause: a square's diagonal corners sit `sqrt(2)` (~41%)
farther from center than its edge midpoints, so two bboxes independently padded by the same
*fraction*, in two different coordinate systems, aren't the same shape at all — the degree-space
padding was undershooting the meters-space grid's corners regardless of how generous the fraction
was, since the fraction was never actually being checked against what it needed to cover.

**Fix**: `lunaserv.astropedia_coverage_bbox_deg` no longer pads the footprint's degree bbox
independently. It now takes the destination grid's own bbox (`dst_bbox_m`, already computed by
`fetch_dem_and_ortho`) directly and derives the degree-space AOI from it via
`rasterio.warp.transform_bounds` (which densely samples the whole boundary, not just the 4 corners,
so it's robust to any residual asymmetry between corners and edges), plus a small additional
`DEM_FETCH_SAFETY_MARGIN_FRACTION` (2%) purely for the resampling kernel's own footprint (bilinear
needs real neighbor samples just past the exact destination edge to interpolate cleanly). There's now
only one padded bbox driving both the fetch and the destination grid, so the two can no longer
silently disagree. `fetch_dem_astropedia`'s signature changed to match (`dst_bbox_m, center_lon_deg,
center_lat_deg, config` instead of `camera, config, extra_footprint_lonlat_deg`) -- `extra_footprint_
lonlat_deg` was already being unioned into `dst_bbox_m` by `fetch_dem_and_ortho` before this call, so
passing it separately here was redundant as well as part of the bug.

Live-validated: re-ran the real pipeline and checked every pixel of the resulting DEM for nodata --
zero, anywhere, including 5x5 blocks at all four corners specifically (previously nonzero at all
four). `trntest-lint` clean, full pytest suite green (new/rewritten `test_lunaserv.py` coverage,
including a direct regression test that transforms `dst_bbox_m`'s own corners through the returned
degree bbox and asserts they're inside it -- the exact geometric property the old implementation
didn't have), full notebook re-run via `scripts/run_notebook.sh`.

## Phase 32 (2026-08-12) — Split the flagship notebook into `data_set_selection` + `image_generation`

The single combined notebook (`notebooks/lunar_sat_sim_demo.py`/`.ipynb`) did catalog-driven EDR
selection and full image generation/geometry validation in one run. The user wanted these
separated into `data_set_selection.ipynb` and `image_generation.ipynb`, with `data_set_selection`
conceptually running first but no runtime dependency between them.

The natural split fell exactly where `dataset.py`'s own two functions already divide the work:
`select_dataset()` (catalog query + SPICE-based illumination/geometry filtering) does no DEM/ortho/
render work at all and already returns everything `generate_dataset()` needs per row
(`edr_volume`/`edr_subdir`/`edr_doy`/`edr_product`, `cdr_volume`/`cdr_product`, `start_frame`) --
so no changes to either function were needed.

First design considered was a literal, hand-copied dict of those fields pasted into
`image_generation.py` (with a comment on where it came from) -- rejected by the user in favor of a
real checked-in file, closer to what `write_manifest`/`read_manifest` (`dataset.py`) already exist
for: CSV, human-readable/diffable, and already covered by an existing round-trip unit test
(`tests/test_dataset.py::test_write_read_manifest_round_trip`) that had no real caller until now.
`data_set_selection.ipynb`'s last cell writes the *whole* candidate table (not just the selected
row) to a new checked-in `notebooks/dataset_manifest.csv`, and also prints the selected
`edr_product` id directly. `image_generation.ipynb` reads that file with `read_manifest()` and
calls `generate_dataset(images, limit=1)` on it -- identical to what the combined notebook already
did, just with the `DataFrame` coming from a file instead of a fresh catalog query.
`write_manifest`/`read_manifest` were also added to `trntest/__init__.py`'s top-level exports
(previously only `select_dataset`/`generate_dataset`/`DATASET_COLUMNS`/`GenerationResult` were
re-exported from `dataset.py`).

Mid-implementation, the user pulled 15 commits of upstream work into the checkout (the die5 tie-
point/campt fixes, the Astropedia DEM switch, the boresight re-aim fix, the papermill notebook
runner) that had landed while this split was in progress, stashing the in-flight `__init__.py`
edit first. The stash was left alone (not popped) since the pulled `tie_points`/`session` API had
already changed shape (`compute_tie_points` -> `select_tie_points` + `resolve_crop_pixels`) in a
way that made the stashed diff not directly reapplicable; the `__init__.py` export and both
notebooks were redone by hand against the new code instead. Notebook `Phase` numbers (2-7) were
kept unchanged in `image_generation.py` since `docs/plan.md`/`docs/data-sources.md` cross-reference
"Phase 5A/5B/6A/6B" by those exact labels; `data_set_selection.py` got its own new, unnumbered
section.

Live-validated end to end via `scripts/run_notebook.sh` for both notebooks (real catalog/SPICE/WMS/
`sat_sim`/ISIS calls): `data_set_selection.ipynb` selected `M1327210646CE` from an 81-candidate
window and wrote the manifest; `image_generation.ipynb` read it back, rendered, and all 5 die5 tie
points resolved on both candidates with no dropped points or warnings. Visually inspected the
extracted Phase 5A/6A/7 comparison figures directly (not just "it ran") -- real, sane, geo-aligned
lunar terrain with tie points landing on the same features across the synthetic render, the real
WAC crop, and the hillshade basemap.

## Phase 33 (2026-08-13) — Fixed `plot_overlay_toggle`'s GitHub rendering: from a cosmetic patch to a real mechanism fix

`plot_overlay_toggle` (added the previous session, `plotting.py`) worked correctly in a live
JupyterLab kernel, but the user found afterward that GitHub's static `.ipynb` viewer rendered the
"Toggle Overlay" control as plain text rather than a button -- contrary to the docstring's explicit
claim that the earlier checkbox/`onchange` -> `<details>`/`<summary>` switch had been "confirmed" to
produce "a real, working toggle in both a live kernel and GitHub's static viewer."

Root-caused by reading GitHub's actual open-source HTML sanitizer directly rather than guessing:
`gjtorikian/html-pipeline`'s `SanitizationFilter::DEFAULT_CONFIG` (the allowlist its `.ipynb`
HTML-output rendering is built on) includes `details`/`summary`/`div`/`img` as elements and
`id`/`width`/`height`/`open`/`name` as attributes, but `style` and `class` are not present anywhere
in the config -- not per-element, not in its `all:` global list -- and there's no `<style>`/`<link>`
in the element allowlist either. So the earlier validation (confirming inline `on*` event handlers
are stripped, and that `<details>`/`<summary>` themselves survive) was correct as far as it went, but
never actually tested the one thing both the "button" look and the pixel-stacked overlay mechanism
depended on: `style="..."`. GitHub strips every `style` attribute in the markup while keeping every
tag, degrading the control to a real, still-clickable but completely unstyled `<details>`/`<summary>`
(native disclosure triangle plus label text, no button chrome -- easy to mistake for inert text,
exactly as reported) with the overlay `<img>` (no longer `position:absolute`) falling into normal
document flow *below* the base image on expand, rather than stacking exactly on top of it.

First pass treated this as a cosmetic problem: gave the overlay `<img>` explicit `width=`/`height=`
HTML attributes (not just matching `style`) and wrapped the label in `<strong>`, both of which
survive sanitization, so the control at least reads as clickable instead of inert. That patch shipped,
but left the actual mechanism -- a single `<details>` toggling a *second* image on top of an
always-visible base -- still `style`-dependent for anything beyond "does it look clickable": on
GitHub the overlay image would still land below the base rather than replacing it, and the user's
real ask (confirmed when asked directly how confident the "no way to fix this" conclusion was) turned
out to be about function, not looks -- rapid, repeated flipping between the two full images, not
button styling.

That reframing led to the real fix: `<details>` elements sharing one `name` attribute form a native
"exclusive accordion" group (opening one auto-closes the other, like a two-option radio group) --
shipped in Chrome 120 and Safari 17.2 (both Dec 2023), on by default in Firefox 130 (~mid-2025), so
broadly supported by now, and `name` is a plain global HTML attribute (confirmed present in the same
sanitizer's allowlist), not `style`. `_overlay_toggle_html` now renders two `<details name=...>`
elements -- one per already-fully-rendered PNG (`overlay_alpha=0`/`overlay_alpha=1`, both complete
standalone images, not a transparency layer) -- instead of one `<details>` toggling a second image on
top of an unconditional base. Exclusivity, and therefore "exactly one image visible, real click-driven
flipping," now works identically on both platforms without depending on `style` at all. Cosmetic
concerns (button chrome, and -- live-kernel only, since GitHub strips `style` regardless of any
markup choice -- pixel-exact stacking via `position:absolute`) are layered on top where `style`
happens to survive, and degrade harmlessly (a real but plain toggle, images offset by roughly one
summary row instead of pixel-registered) where it doesn't. Getting the pixel-exact live-kernel
stacking right *and* keeping GitHub's plain fallback from silently reintroducing the earlier
"positioned element paints over in-flow content" stacking-order bug required computing the image's
`top` offset from the summary chrome's own known box model (explicit `height`/`padding`/`border`,
not font-metric guesswork) rather than a fixed `top:0`.

True pixel-registered, exact-same-screen-position overlay stacking remains impossible on GitHub's
viewer specifically -- confirmed structurally, not just empirically: overlapping two same-size
elements at identical coordinates requires *some* CSS positioning mechanism, and GitHub's sanitizer
allows none (no `style`, no `class`, no `<style>`/`<link>`), so no markup can restore it there. That
limit is unrelated to the actual ask, though, which this version now genuinely satisfies on both
platforms.

Confirmed against GitHub's own published sanitizer source rather than trying to scrape GitHub's live
blob viewer (React-rendered, not server-rendered -- a plain HTTP fetch of the notebook's `blob` URL
returns only the app shell, not the rendered notebook HTML); the `<details name>` grouping's own
behavior was checked by parsing the regenerated `.ipynb`'s actual output HTML with `BeautifulSoup`
(two `<details>` per toggle, same `name`, exactly one with `open`), not just by reasoning about it.
`trntest-lint` clean; `scripts/run_notebook.sh notebooks/image_generation.py` re-executed end to end
(real SPICE/WMS/`sat_sim`/ISIS pipeline).

## Phase 34 (2026-08-13) -- `plot_overlay_toggle` still broken on GitHub after Phase 33: wrong
sanitizer investigated, real one found and fixed with CSS `:target`

The user reported that Phase 33's fix, live on `github.com`, still showed "With overlay"/"Base only"
as plain bold text with no way to switch the overlay -- looking exactly like the *original*, pre-Phase-33
bug, not the "plain but still functional" `<details>`/`<summary>` disclosure Phase 33's own root-cause
analysis predicted.

Root cause: Phase 33 investigated the wrong sanitizer entirely. `gjtorikian/html-pipeline`'s
`SanitizationFilter` (what Phase 33 read) backs GitHub's README/markdown rendering, not `.ipynb` blob
rendering. Confirmed by fetching an actual notebook blob page (`curl`, not a browser -- the page is a
React shell, but its embedded JSON payload includes `codeViewBlobLayoutRoute.blob.displayUrl`, an
`https://notebooks.githubusercontent.com/view/ipynb?...` URL) and fetching that URL directly: it
returns a static HTML page whose inline `<script>` sets `window.NOTEBOOK_DATA = {"html": "<the raw,
unsanitized cell-output HTML>"}`, then loads a separate client-side JS bundle
(`/static/dist/bundle-*.js`, fetched directly and read) that runs `DOMPurify.sanitize(content, {
ALLOWED_TAGS })` on it before inserting it into the DOM -- entirely client-side, a completely different
mechanism from html-pipeline's server-side Ruby sanitizer. That bundle's own
`app/static/js/html-sanitizer.ts` hardcodes its `ALLOWED_TAGS`:

    HTML_TAGS = [body, b, blockquote, br, code, dd, del, div, dl, dt, em, h1-h8, hr, i, img,
                 ins, kbd, li, ol, p, pre, q, rp, rt, ruby, s, samp, span, strike, strong,
                 sub, sup, table, tbody, td, tfoot, th, thead, tr, tt, ul, var]
    SVG_TAGS  = [a, animate*, circle, ..., foreignObject, g, ..., style, svg, symbol, ...]

`details`/`summary` (and `input`/`button`/`label`) are absent from both lists. DOMPurify's default
behavior for a disallowed tag is to remove the tag but keep its children in place (`KEEP_CONTENT`) --
so both Phase 33's version and the one before it degraded identically on GitHub: the `<strong>` label
left sitting inert where `<summary>` used to be, the image(s) fallen into plain document flow below it.
"Plain bold text, not clickable" was `<details>`/`<summary>` being unwrapped, not a styling problem --
Phase 33's "GitHub strips `style`" diagnosis was itself downstream of reading the wrong sanitizer's
config: this DOMPurify call passes no `ALLOWED_ATTR` override, so `style` (present in DOMPurify's own
default attribute allowlist, confirmed by grepping the bundle) actually survives fine here.

**Fix**: replaced the `<details name=...>` mechanism with one built only from tags confirmed present in
the real allowlist above -- `div`, `a`, `span`, `img`, and `style` *as a tag* (present via the SVG list;
DOMPurify special-cases `style`/`a`/`font`/`title` to also work outside `<svg>`, straight from that
library's own source comment, specifically so they aren't "erroneously deleted from HTML namespace").
`_overlay_toggle_html` (`plotting.py`) now emits two `<a href="#{id}-with">`/`<a href="#{id}-base">`
links, two empty `<span id="{id}-with">`/`<span id="{id}-base">` anchor targets, and a `<style>` block
of CSS `:target`-conditioned rules using the general-sibling combinator (`~`) to show/hide the matching
`<img>`; clicking a link changes the page's URL fragment, which drives the `:target` match, with a
separate unconditional rule for which image shows before any link has been clicked
(`initial_visible`). Because `style` genuinely survives here (unlike Phase 33's premise), both `<img>`s
stay `position:absolute` at one shared, precomputed offset on *both* platforms -- pixel-registered
stacking is no longer a live-kernel-only concession.

Verified against the real, extracted `ALLOWED_TAGS` array rather than an assumption about which
sanitizer applies (Phase 33's actual mistake) -- a stronger basis than either prior version had -- but
still not exercised through an actual DOMPurify pass before committing: no JS runtime (`node`) was
available in this environment to run the real bundle against candidate markup, so the browser-rendered
result on `github.com` itself was left for the user to confirm live, rather than the docstring claiming
"confirmed" the way both earlier versions did right before failing. `trntest-lint` clean;
`scripts/run_notebook.sh notebooks/image_generation.py` re-executed end to end (real
SPICE/WMS/`sat_sim`/ISIS pipeline); regenerated `.ipynb` output inspected directly (both toggle
instances contain the expected `:target` CSS and matching `<a>`/`<span>`/`<img>` ids).

**Live github.com result for the Phase 34 version (2026-08-13, commit `6336314`)**, as viewed/pasted by
the user directly from the rendered page for the first (5B) toggle instance:

```html
<div style="position:relative; display:inline-block; width:900px; height:934px; text-align:center;">
  
  <a href="//github.com/trey0/trntest/blob/6336314479a08fce6b316743d321ca1e53e650cb/notebooks/#overlay-toggle-8516d1c8-with" style="cursor:pointer; display:inline-block; background:#f0f0f0; color:#000; text-decoration:none; border:1px solid #999; border-radius:4px; padding:4px 12px; font-size:0.9em; height:20px; line-height:20px; margin:0 4px 0 0;"><strong>With overlay</strong></a>
  <a href="[truncated]" width="900" height="900" style="position:absolute; top:34px; left:0; width:900px; height:900px;">
</div>
```

## Phase 35 (2026-08-15) — Root-caused Phase 34's failure for real: a third, server-side
sanitizer strips `<style>` and rewrites fragment links *before* DOMPurify ever runs

Picked back up from Phase 34's unreviewed pasted snippet above. That snippet's second element —
`<a href="[truncated]" width="900" height="900" style="position:absolute; ...">`, an `<a>` tag
carrying `<img>`-only attributes — turned out to be a copy/paste artifact, not a real finding
(confirmed below); the first element's href, `//github.com/trey0/trntest/blob/<sha>/notebooks/#overlay-toggle-8516d1c8-with`,
was the real lead: our source only ever emits a bare `href="#overlay-toggle-8516d1c8-with"`, so
something had rewritten it into an absolute URL — one that drops the filename entirely (path ends
`.../notebooks/#...`, not `.../notebooks/image_generation.ipynb#...`).

Investigated by reproducing Phase 34's own fetch chain (blob page → embedded
`codeViewBlobLayoutRoute.blob.displayUrl` → `notebooks.githubusercontent.com/view/ipynb?...`) with
`curl`, then extracting `window.NOTEBOOK_DATA.html` from that response via a small Python script
(`json.loads` on the `{"html": "..."}` object literal — the whole page is one `<script>` block, not
real JSON, but the object is). Phase 34 had already found and read the client bundle's
`sanitizeNotebook()`, which reads exactly this `window.NOTEBOOK_DATA.html` and passes it straight to
`purify.sanitize(content, {ALLOWED_TAGS, RETURN_DOM_FRAGMENT: true})` — so this string is genuinely
the pre-DOMPurify input, confirmed from the bundle's own code this time, not assumed.

That pre-DOMPurify HTML, for our real committed toggle (`overlay-toggle-8516d1c8`), already has:
- **Zero `<style>` tags** anywhere in the entire ~6.6MB payload (`data.count('<style')` == `0`),
  despite the committed `.ipynb`'s own `text/html` output containing exactly one, verified by loading
  the tracked `notebooks/image_generation.ipynb` JSON directly and grepping that cell's output.
- **Both `<a href="#...">` links already rewritten** to the absolute, filename-dropping
  `//github.com/.../notebooks/#...` form shown above, for both the `-with` and `-base` link (checked
  both, not just the one Phase 34 happened to paste).
- **Both `<img>` tags intact** — `id`, `src="data:image/png;base64,..."`, `width`, `height`, and
  `style="position:absolute; ..."` all present unchanged, confirming `_overlay_toggle_html`'s image
  markup itself was never the problem, and that this pass doesn't touch `data:` image URIs or inline
  `style=` *attributes* generally (only the `<style>` *tag* and fragment `href`s).

This means `<style>` is stripped, and the fragment links are broken, by something upstream of
DOMPurify entirely — a **third sanitizer** (GitHub's own server-side `.ipynb`→HTML rendering step,
distinct from both `html-pipeline` (Phase 33's wrong guess) and the client DOMPurify bundle (Phase
34's fix target)). Its exact implementation isn't visible from the client bundle, but its effect is
directly observed, not inferred. Practical fallout for the `:target` mechanism specifically:

- No `<style>` block survives to reach the browser at all, so no CSS rule -- `:target`-conditioned or
  otherwise -- can ever be defined in the first place. Nothing on DOMPurify's `ALLOWED_TAGS` (which
  does include `style`, per Phase 34) matters, because DOMPurify never sees the tag.
- Even given CSS rules some other way, the trigger is separately broken: rewriting
  `href="#overlay-toggle-8516d1c8-with"` into an absolute URL pointed at the *containing directory*
  (not even the current file) means a click no longer changes the current document's URL fragment at
  all -- it attempts to load a different page (or, since the notebook itself renders inside an
  `<iframe src="https://notebooks.githubusercontent.com/...">`, a same-origin navigation of that
  iframe to a `github.com` URL, a cross-origin target from the iframe's own perspective) instead of
  triggering `:target` in place.

Both failures are independent and either alone is fatal to the CSS `:target` design, so Phase 34's
fix cannot be patched forward -- it needs a different mechanism, not different tags. The one prior
loose end (the "`<a>` tag with `<img>` attributes" in the pasted snippet) was resolved by this same
fetch: the real pre-DOMPurify HTML has two ordinary, well-formed `<img>` tags in that position, not an
`<a>`; Phase 34's paste was very likely a manual copy/paste artifact (e.g. a partial DevTools
selection), not a rendering behavior, since the reproducible fetch shows no mechanism that would
rename an `<img>` element to `<a>`.

**Where this leaves the "blink" goal**: inline `style="..."` *attributes* keep surviving every layer
found so far (this server-side pass, and DOMPurify's defaults per Phase 34), so per-element inline
positioning still works -- it's only block-level `<style>` rules and fragment-link navigation that are
dead on GitHub. A click-driven exclusive toggle therefore has no remaining CSS-only path (no
`<style>`, no working same-page `href="#..."` trigger, and `<details>`/`<summary>` were already ruled
out in Phase 34 for being outright absent from `ALLOWED_TAGS`). Not yet investigated: whether a
single self-contained animated image (GIF/APNG, still just one `<img src="data:...">`, no `<style>`,
no anchor links, nothing for either sanitizer to strip) could deliver the actual underlying want --
automatic, repeated blinking between the base and overlay frames, the classic image-analyst "blink
comparator" technique -- without requiring click-driven interactivity at all. Both `_render_overlay_png`
frames already exist as separate in-memory PNGs before `_overlay_toggle_html` combines them, so this
would reuse that part of `plot_overlay_toggle` unchanged and only replace the HTML-toggle half.

No code changed this phase -- root-cause and options-gathering only, using the real fetched sanitizer
input/output rather than guessing from source reading alone (Phase 33's mistake) or from an unverified
client-side allowlist alone (Phase 34's mistake).

## Phase 36 (2026-08-15) — Replaced `plot_overlay_toggle`'s click-driven toggle with an auto-blinking
animated GIF, per the user's own explicit choice among the Phase 35 options

Given Phase 35's finding that no CSS-only click-toggle mechanism can survive GitHub's real rendering
pipeline (both the trigger and the styling it depends on are broken upstream of the sanitizer Phase 34
targeted), presented the user three ways forward -- an auto-blinking GIF, an unverified SVG-SMIL
declarative-click experiment, or dropping GitHub interactivity entirely -- and they picked the GIF
approach.

`plot_overlay_toggle` (`plotting.py`) keeps its name and signature (plus one addition,
`blink_interval_ms: int = 700`) but now returns a single `<img src="data:image/gif;base64,...">`
instead of the `<div>`/`<a>`/`<span>`/`<style>`/`<img>`x2 structure Phases 33/34 built -- no `<style>`
block, no anchor `href`, nothing left for either GitHub sanitizer layer (the Phase 35 server-side pass
or DOMPurify) to strip, so it now renders identically on a live kernel and GitHub's static viewer by
construction rather than by surviving an allowlist. `_render_overlay_png` (base64 PNG) became
`_render_overlay_frame` (`PIL.Image`, RGB); a new `_blink_gif_b64` replaces `_overlay_toggle_html`,
building the two-frame GIF. One real subtlety: naively saving two independently-quantized RGB frames
as GIF frames would let each frame pick its own 256-color palette, which would very slightly recolor
the *unchanged* base-image pixels differently between frames -- a whole-image flicker on every blink,
masking the actual overlay-region change being compared. Fixed by quantizing both frames onto one
shared palette first (`Image.new` pasting both frames side by side, `.quantize(colors=256)` once on
that combined image, then `.quantize(palette=<that result>)` on each original frame separately) before
handing them to `Image.save(..., format="GIF", save_all=True, ...)`.

`docs/plan.md`'s `plotting.py` row and the two Phase-5B/6B markdown cells in
`notebooks/image_generation.py` that described a "clickable"/"button" toggle were updated to match.
`uuid` (only ever used for the old mechanism's per-element id namespacing, now unneeded) dropped from
`plotting.py`'s imports; `pillow` added to `pyproject.toml`'s direct dependencies since `plotting.py`
now imports `PIL` directly rather than relying on it being pulled in transitively (already true,
confirmed via `docker compose run --rm demo python3 -c "import PIL"`, `12.3.0`). `trntest-lint` clean.

A small, synthetic-array reproduction inside the container (two 100x100 solid-color frames, no real
raster I/O) confirmed the GIF mechanism itself works: valid `GIF89a` header, 2 frames on reload via
`PIL.Image.open(...).n_frames`, correct `duration` on each frame. End-to-end validation through the
real pipeline (`scripts/run_notebook.sh notebooks/image_generation.py`) could not be completed this
session: `session.generate_dataset()`'s fetch of the selected EDR product
(`M1327210646CE`, from `dataset_manifest.csv`) hit a `429` from `pds.mcp.nasa.gov`, and a direct `curl
-I` against the same URL confirmed a CloudFront-level `retry-after: 3600` (a full hour) -- not the
short transient blip a 90s-interval retry loop (tried, stopped once the `Retry-After` header was read)
could clear. `notebooks/image_generation.ipynb` was left at its last real, committed (Phase 34)
executed state rather than committing the broken partial run papermill produced (execution stopped at
cell 2 with the 429 as an unhandled exception) -- `git checkout -- notebooks/image_generation.ipynb`
after confirming via `git diff --stat` what was being discarded. Still outstanding: a real
`scripts/run_notebook.sh` run once the rate limit clears, and the user's own live-github.com
confirmation of the rendered GIF -- this phase's mechanism has not yet been checked against the actual
notebooks.githubusercontent.com pipeline the way Phases 33-35 eventually checked their own guesses.

## Phase 37 (2026-08-15) — Root-caused the Phase 36 PDS 429 (a single unpaced ~1600-request sweep,
not concurrent agents), and made `cache.py`'s fetch path retry, paced, and fail loudly instead of
silently skipping

Prompted by the user asking for a theory on why Phase 36's rate limit happened at all, given several
prior from-cold sessions never hit it, and whether concurrent multi-agent requests might be the cause.

Investigated via the shared `scratch/notebook_runs/` log directory (every worktree's
`scripts/run_notebook.sh` invocation logs there) rather than guessing: exactly one
`data_set_selection_20260815T014907Z.log` exists for today, no overlapping `image_generation` run
from another process during its 01:49:07-01:56:47 window -- no direct evidence of a second
simultaneous agent. That log's own content was more than sufficient on its own, though: its
`select_dataset(max_search_days=7)` cell logged **1,354 `429`s out of 1,633 total candidate fetches**
in that single 459.72s cell, all sequential, from one process. Traced to `dataset.py`'s
`_evaluate_illuminated_candidates`: it calls `evaluate_candidate_image` -> `camera.fetch_frame_timing`
-> a real HTTP GET, once per candidate, in a plain `for` loop, with zero delay anywhere in the fetch
path (confirmed: no `time.sleep` existed anywhere in `src/trntest/` before this phase) -- averaging
~3.5 requests/sec sustained for ~8 minutes against `pds.mcp.nasa.gov`, easily sufficient by itself to
trip a rate limiter (confirmed via a direct `curl -I` against the same URL: CloudFront responded with
`retry-after: 3600`, a flat 1-hour ban, not a short transient one). Why not in prior cold-cache
sessions: `cache/`'s kernel files date back to 2022, so this exact from-scratch 1,633-request sweep
for a *live* search window had likely never actually hit a fully empty cache before (Phase 10,
`docs/history.md`, fixed a different cold-start cost -- a NAIF metakernel cache-key bug -- down to
~6s, but that was measured with these PDS labels already warm); today's cache had almost nothing in
the LROC EDR tree older than today, consistent with `cache/` having been wiped since the last session
(explicitly disposable per `docs/environment.md`).

Separately, and worse: the sweep's own `except Exception: print(...); continue` in
`_evaluate_illuminated_candidates` caught the 429s (`requests.HTTPError`) exactly like any other
per-candidate problem and just kept going -- meaning once the first request got rate-limited, the loop
spent the rest of its ~1600 candidates uselessly hitting an already-refusing server instead of
stopping. `dataset.generate_dataset` has the identical shape (confirmed live during this same
incident: `generate_dataset: FAILED M1327210646CE: 429 ...` from Phase 36's own notebook run,
followed by a confusing `IndexError` on `results[0]` rather than a clear failure, since `limit=1`
meant the "0 succeeded" list was just empty).

**Fix, in `cache.cached_get`** (the shared fetch path behind every source in this project except the
Astropedia flat file):
- **Pacing**: every real fetch attempt (never a cache hit) sleeps `_REQUEST_PACING_SECONDS` (0.2s)
  first -- free in the normal mostly-cached case, meaningfully softens exactly the bulk-fresh-fetch
  case that caused this.
- **Session reuse**: one module-level `requests.Session()` (`_SESSION`) instead of a fresh
  `requests.get()` per call -- connection keep-alive, lighter on the remote server and faster locally.
- **Bounded retry with backoff**: up to `_MAX_FETCH_ATTEMPTS` (3) attempts, short exponential backoff
  between them. A 429 is handled distinctly -- if `Retry-After` is present and <=
  `_MAX_RETRY_AFTER_SECONDS` (30s), sleeps exactly that and retries; otherwise (missing, unparseable,
  or too long -- e.g. this incident's 3600s) fails immediately rather than blocking for however long
  the server asks.
- **New `cache.FetchError`**: raised once attempts are exhausted, chained from the last real
  exception. Deliberately a distinct type (not the raw `requests` exception) so a caller sweeping many
  items can tell "this is a systemic problem" apart from an ordinary per-item failure.

**Fix, in `dataset.py`**: both `_evaluate_illuminated_candidates` and `generate_dataset` now
`except cache.FetchError: raise` before their existing broad `except Exception` (which still
skip-and-continues for genuinely per-item problems, unchanged) -- a systemic fetch failure now
aborts the whole sweep/batch instead of being logged as one more "skipping"/"FAILED" entry among
hundreds. This matches explicit prior direction from the user (`docs/caching.md`'s WAC CK section,
re: the `spiceinit` call: "I'd rather have a relatively prompt exception and manually retry later")
-- extended here to *bounded* retries plus pacing/backoff first, since this failure mode was a
genuinely unpaced burst tripping a rate limiter, not a single flaky call, but the "signal failure
loudly rather than silently swallow it" half of that direction is unchanged and, if anything, this
phase's actual bug (silent skip-and-continue through 1354 more 429s) was a real violation of it that
had gone unnoticed until now.

`docs/caching.md` gained a new section documenting this policy. Existing `tests/test_cache.py` tests
that mocked `requests.get` directly were updated to mock `cache._SESSION.get` instead (an
implementation-detail change, not a behavior one) and to expect `cache.FetchError` rather than a raw
exception; new tests cover retry-then-succeed, exhausting retries, both 429 branches (short
`Retry-After` retried, long/missing one fails fast), and pacing. New `tests/test_dataset.py` tests
cover both loops letting `FetchError` propagate while still skipping ordinary per-item errors.
`trntest-lint` clean; full `pytest` suite (129 tests, up from 118) passes. Not yet re-verified against
the real PDS endpoint end-to-end (`scripts/run_notebook.sh notebooks/data_set_selection.py`) -- the
same `Retry-After: 3600` ban from Phase 36 was still in effect throughout this phase's work.

## Phase 38 (2026-08-15) — Narrowed Phase 37's per-item catch to the two specific, evidenced
exception types it was actually meant for, and made every skip a prominent end-of-run summary

The user pushed back on Phase 37's design: `_evaluate_illuminated_candidates`/`generate_dataset` still
each kept a bare `except Exception: skip` for anything that wasn't `cache.FetchError`, justified only
by each function's own docstring claim that "one bad candidate/image shouldn't abort the whole
search/batch" -- but nothing had actually confirmed that claim against this project's real history, and
the user's stated general preference is to know when something's gone wrong, not silently skip it.

Checked rather than assumed: grepped the one real large-scale run available (Phase 37's own
`data_set_selection` log, 1,633 candidates) for every "skipping" line -- all 1,354 were the 429s Phase
37 already fixed; zero were a genuine non-fetch per-candidate problem. So the bare `except Exception`
had, in this project's actual history, never once caught anything it was nominally there for -- only
things a narrower catch would also let through (real bugs included).

Traced `evaluate_candidate_image`'s and `generate_dataset`'s own call graphs for what a *narrower*,
still-real catch should cover, rather than picking an arbitrary type:
- `evaluate_candidate_image`: `camera.boresight_ground_point_km`'s `assert t is not None, "camera
  boresight does not intersect the Moon"` (a real geometric edge case for some candidate's exact
  timestamp/attitude -- e.g. near the limb) and `spiceypy.utils.exceptions.SpiceyError` (from
  `camera.camera_pose_moon_me`/`illumination.sun_elevation_deg` -- a furnished kernel not actually
  covering this one candidate's exact timestamp, even within the broader search window). Both
  concrete, evidenced conditions in the code, not hypothetical.
- `generate_dataset`: only `ValueError`, from `lunaserv.astropedia_coverage_bbox_deg`'s real
  latitude-coverage check (a candidate's padded AOI falling outside Astropedia's GLD100 flat file's
  +-79-ish deg coverage). Deliberately did *not* extend the same treatment to `lunaserv.py`'s two
  `assert center is not None` checks (they guard state `build_camera` should already have
  guaranteed by construction -- a defensive invariant, not a condition expected to trip for a real
  candidate) or to this pipeline's ISIS `campt` calls (`tie_points.crop_footprint_corners_for_camera`
  -> `isis_wac.ground_point_at_pixel` uses `check=True` specifically *because* no failure is expected
  there, per that function's own docstring -- a failure means something's genuinely wrong, the same
  epistemic status as any other unanticipated exception). `generate_dataset` is, in this project's
  actual usage, always called with `limit=1` on an already-screened candidate (both current notebooks
  and `old_notebooks/stripe_debug.py`), so this rarely matters in practice either way, but the same
  narrow-catch principle now applies if it's ever used as a true multi-image batch.

Both functions now `except (AssertionError, spiceypy.utils.exceptions.SpiceyError)` /
`except ValueError` specifically (still after `except cache.FetchError: raise`, unchanged from Phase
37) -- anything else (a `KeyError`/`TypeError`/`AttributeError` from a real bug, for instance) now
propagates and aborts, same as `FetchError` already did.

Also addressed directly: the user asked for a log of the errors and a prominent end-of-run status,
"at minimum." Both functions now collect skip records (product_id, exception type, message) into a
list during the sweep/batch instead of printing one line at a time inline, then print one clearly
delineated summary block after the loop -- e.g. `select_dataset: 3 of 1633 candidate(s) skipped
(geometry/coverage edge case, not a fetch failure):` followed by one line per skip -- rather than
individual "skipping"/"FAILED" prints scattered through the rest of the sweep's own output. This
mirrors a pattern already established elsewhere in this codebase, found while looking for precedent:
`tie_points.resolve_crop_pixels` already does exactly this (collect drops, one aggregate summary,
only hard-fail if *none* resolve) for its own per-tie-point skip case.

Updated existing tests (`tests/test_dataset.py`) that exercised the old bare-`Exception` catch to use
the two now-actually-caught types instead, and added new tests confirming a genuinely unanticipated
exception (a plain `KeyError`, standing in for a real bug) propagates and aborts both the candidate
sweep and the generation batch rather than being silently skipped. `trntest-lint` clean; full `pytest`
suite (131 tests) passes. Still not re-verified against the real PDS endpoint end-to-end -- the Phase
36 rate limit's ~1hr `Retry-After` window had not yet elapsed by the end of this phase either.

## Phase 39 (2026-08-13, developed concurrently with Phases 33-38 in a separate worktree; merged and
renumbered here after Phase 38) — Implemented `TrnTestDataSet`/`TrnTestEntry`/`TrnTestImage`

Implemented the design `docs/dataset-plan.md` laid out in a prior planning-only session (see that
file's own header): a structured, self-contained dataset folder (`manifest.csv` + `crop`/
`hillshade`/`reproject` subfolders) with a filesystem-based, multi-worker-safe task queue, replacing
`dataset.generate_dataset()`'s flat, all-at-once layout as the notebook-facing generation path.
`dataset.generate_dataset()` itself is untouched -- still used by `select_dataset`'s per-candidate
illumination sweep, just no longer what either notebook calls for its own single-image generation.

New `src/trntest/trn_dataset.py`: `TrnTestDataSet` (`create`/`open`, `__len__`/`__iter__`/
`__getitem__` by index or `product_id`, `populate`/`status`), `TrnTestEntry` (one manifest row's
shared, `functools.cached_property`-cached state -- camera, frame timing, the real WAC pipeline's
stitched/cropped cubes, DEM/ortho), and `TrnTestImage` (abstract; `TrnTestCropImage`/
`TrnTestHillshadeImage` concrete) owning the shared `exists`/`generate`/`plot_vs_basemap`/
`plot_overlay` logic once per product type, matching the design's "real code reuse, not just a
naming convenience" framing. The task queue (`.locks/<product_id>_<product_type>.lock`/`.error`,
claimed via `os.O_CREAT | os.O_EXCL`) is safe across separate OS processes with zero extra
machinery, consistent with this project's existing rule that SPICE state is process-global and
unsafe across concurrent calls *within* one process -- `populate()` itself stays a simple sequential
loop, as designed.

Two new small supporting pieces, both exactly as scoped: `isis_wac.run_isd_generate_for_crop`
(crop-scoped `isd_generate -i` + the same `time_offset_s` ephemeris-time patch formula this
project's history already validated once, re-implemented fresh since the original code was fully
removed after that investigation moved on to the deeper `usgscsm` bug -- see `docs/data-sources.md`'s
"`isd_generate -i` on an ISIS-`crop`ped Pushframe cube" entry and its new follow-up section), and
`lunaserv.result_from_files` (pure-IO reconstruction of a `LunaservResult` from an already-fetched
ortho/DEM pair, reading `bbox`/`width`/`height` back from the ortho's own embedded georeferencing --
this is what lets `TrnTestEntry.lunaserv_result` resume from a prior `generate()` run instead of
re-fetching, the real mechanism behind `populate()`'s designed near-instant second run). A small,
explicitly-optional refactor also landed: `dataset._per_image_config`, extracting the
`dataclasses.replace(...)` construction `generate_dataset()`'s loop and `TrnTestEntry.per_image_config`
both need, behavior-unchanged (confirmed by `test_dataset.py` passing unmodified).

**Two deliberate deviations from `docs/dataset-plan.md`'s literal pseudocode**, found and resolved
during implementation rather than assumed away:
1. The design's `populate(self, product_types=PRODUCT_TYPES, retry_failed=False)` signature has no
   row-count limit -- the task queue is inherently "every manifest row x every implemented product
   type" by construction. But both notebooks' own "call `TrnTestDataSet.create(...)` with `images`"
   wording, taken literally, would make `image_generation.ipynb`'s `populate()` call generate the
   *entire* candidate table (commonly ~80 rows) instead of just the one image that notebook has
   always rendered -- a real, undocumented cost blowup (single-image generation alone measured
   ~19 minutes cold in this session's own validation run) that would have silently turned a routine
   `scripts/run_notebook.sh` verification into an hours-long batch job. Resolved by having
   `image_generation.py` pass `images.head(1)` to `create()`, matching `dataset.generate_dataset(...,
   limit=1)`'s existing scope exactly; `data_set_selection.py`'s own `create()` call (cheap, no
   `populate()`) still gets the full candidate table, as designed.
2. `TrnTestImage.plot_overlay()`'s pseudocode names `plotting.plot_overlay` (the plain, single-state
   overlay). But the notebook this replaces had already switched to `plotting.plot_overlay_toggle`
   (the clickable on/off comparison, added the commit immediately before `docs/dataset-plan.md` was
   written) for exactly this call site -- reverting to the plain version here would have been a real,
   silent UX regression, not a neutral relocation of existing logic. Used `plot_overlay_toggle`
   instead; noted in the method's own docstring so the deviation is visible in the code, not just
   here.

New `tests/test_trn_dataset.py` (19 tests, `tmp_path` + fakes, no real SPICE/ASP/ISIS) covers
`create`/`open`, indexing, exact path naming, the shared `TrnTestImage` base-class logic (via a
minimal fake subclass), and the task queue's four states/atomicity/`populate()` including the
failure-and-continue and `retry_failed` paths -- all passing, alongside the full 137-test suite
(unmodified pre-existing tests included) and a clean `trntest-lint --all`.

**A genuine, unrelated infrastructure bug found and fixed along the way**: `trntest-lint --all`
(which shells out to `git`) failed inside this worktree's Docker container with "not a git
repository" -- a linked git worktree's `.git` is a *file* pointing at the main checkout's real
`.git` by absolute host path, and that path wasn't covered by `docker-compose.yml`'s `..:/workspace`
mount (which only covers the worktree's own directory). Fixed by mounting the main checkout's real
git-common-dir at that same absolute path (read-only), computed by `scripts/setup_worktree_docker_env.sh`
by parsing the worktree's `.git` file directly rather than trusting `git rev-parse`'s own path
resolution -- the latter silently resolves through the `trntest_ws` relocation symlink from earlier
this session, while the literal path git's own worktree admin files expect is the *unresolved* one.
Safe no-op default for the main checkout (which needs no such mount), verified both ways.

**Real-Docker verification** (`scripts/run_notebook.sh` for both notebooks, per `docs/dataset-plan.md`'s
"Verification plan"): both ran clean, no errors. `data_set_selection.ipynb` re-selected the same
`M1327210646CE` product (13.4s, warm cache). `image_generation.ipynb`'s `dataset.populate()` cell --
the real end-to-end exercise of every new code path, including `run_isd_generate_for_crop` -- took
12.08s on a warm cache; **re-running the whole notebook a second time dropped that same cell to
0.87s**, confirming both the task queue's "done" skip and `TrnTestEntry.lunaserv_result`'s
file-based resume path (`lunaserv.result_from_files`) work as designed. `trntest-lint --all` clean
afterward, including the notebook sync/warnings checks.

**Empirical visual check** (extracted and viewed the embedded images directly, not just "it ran"):
Phase 5A/6A (`plot_vs_basemap`) both show real, sensibly-aligned lunar terrain with all 5 tie points
landing on matching features in both panels; Phase 5B/6B (`plot_overlay_toggle`) both show a
well-formed footprint outline over real terrain in both toggle states, not offset or blank.

**Crop ISD accuracy check** -- see `docs/data-sources.md`'s new "`TrnTestDataSet` on-disk layout,
and the crop ISD sidecar's real accuracy" section for the full numbers and investigation. Summary:
dimensions match `gdalinfo` exactly; `starting_ephemeris_time` matches real `campt` output to
0.016s; `ending_ephemeris_time` is off by ~1.39s (~1 `interframe_delay`) -- traced directly (by
re-running the same `campt`-vs-ISD comparison on the pre-existing, unpatched full-cube ISD) to a
pre-existing `isd_generate -i` characteristic for this `flip=true` product, not something this
feature's new offset-shift logic introduced or got wrong; recorded as a known, characterized
non-issue rather than either silently ignored or wrongly "fixed" based on a guess.

**Follow-up, same day**: added `populate(limit=N)` -- stops after the call has done genuinely new
work on `N` distinct entries (an entry already done/in-progress/failed doesn't consume the budget),
so a large dataset's population can be split across multiple separate worker invocations against the
same folder, each resuming from the on-disk task-queue state the last one left. Restructured
`populate()`'s loop from `claim_next_task`'s flat cross-entry scan to an explicit per-entry loop to
make "stop before starting a new entry" straightforward to express correctly; `claim_next_task`
itself is unchanged and still independently tested. 3 new tests; full suite (140) still green.

**Second follow-up, same day**: with `limit` now available, `image_generation.py`'s deviation #1
above is gone -- switched back to passing the *full* manifest to `TrnTestDataSet.create(...)` (as
the design doc originally specified) instead of the `images.head(1)` workaround, so `trn_dataset`'s
own `manifest.csv` now always reflects the full candidate table regardless of which notebook wrote
it last.

**Caught before shipping, not after**: `populate(limit=1)` alone against the full manifest is
*not* equivalent to the old `images.head(1)` behavior once entry 0 is already generated --
`limit` counts distinct entries with genuinely *new* work done, so an already-done entry 0 doesn't
consume the budget and the loop would instead claim and generate a *different*, never-before-seen
manifest row -- a real, undisplayed, multi-minute cost, and a different row again on every
subsequent re-run. Caught by walking through the semantics before wiring the notebook, not by a
failed run. Fixed with a new `TrnTestDataSet.truncate(entries=None, product_types=PRODUCT_TYPES)`:
deletes an entry's (or every entry's, or a list's) already-generated product files and task-queue
lock/error state, reverting `task_state` back to `pending`. `image_generation.py` now calls
`dataset.truncate(dataset[0])` immediately before `dataset.populate(limit=1)` -- since entries
process in manifest order starting from 0, resetting entry 0 to `pending` first guarantees
`populate(limit=1)` always lands on it and stops, never spilling into other rows regardless of
their state. This also means the notebook deliberately gives up the "second run near-instant"
property for its *render* step specifically -- appropriate for a demo notebook actively used while
iterating on `render.py`/`isis_wac.py`, where silently reusing stale prior output would be
misleading; `truncate()` leaves `_work/`'s DEM/ortho intermediates alone, so only the actual
render/crop step re-runs, not the network fetches (confirmed: the truncate+populate cell measured
4.44s on a warm cache in the real re-run below, not the ~12-19 minutes of a true cold generation).
3 new tests (25 total in `test_trn_dataset.py`, 143 across the full suite); real Docker re-run of
`image_generation.ipynb` confirmed clean (no errors, 48.5s total), `trntest-lint --all` clean.

## Phase 40 (2026-08-15) — Two minor post-merge notebook regressions, found in the user's own manual
review: bare `plot_camera_footprint` axes, and a double-rendered plot in two cells

Both found by the user actually looking at the regenerated `image_generation.ipynb` in Jupyter Lab
(per their own standing preference -- see the review-before-commit note added this session -- rather
than trusting `trntest-lint`/`pytest` alone, which can't catch either of these).

**`plot_camera_footprint`'s bare axes**: the user recalled this cell (`In[6]`) once had km-scaled,
labeled axes and asked whether that had regressed. Checked git history first rather than guessing --
`plot_camera_footprint` has never had axis labels in its whole committed history, across every branch
(`git log --all -S`). Traced the actual source of the remembered `"north-south (m)"`/`"east-west (m)"`
phrasing to `old_notebooks/stripe_debug.py`, a frozen, no-longer-synced investigation notebook from
the DEM-artifact debugging phase -- a different file entirely, not a regression in this one. The
underlying complaint was still real, though (raw unlabeled local-CRS meter values on the axes, unlike
the rest of the notebook), so `plot_camera_footprint` gained the same km `FuncFormatter` +
`"Easting (km)"`/`"Northing (km)"` treatment `_render_overlay_figure` already used for this identical
coordinate system.

**Double-rendered plots in cells 11 and 14**: `entry.hillshade.plot_vs_basemap(...)`/
`entry.crop.plot_vs_basemap(...)` each rendered their figure twice. Root cause: two independent
display mechanisms both fire for a bare, un-suppressed expression whose value is a `Figure` still open
at cell-end -- matplotlib's own inline-backend post-execute hook (auto-displays every open figure) and
IPython's own last-expression display hook (auto-displays the cell's final value, since `Figure` has a
`_repr_png_`) -- so leaving the returned `Figure` unsuppressed double-renders it. Four other cells in
the same notebook already suppressed this correctly with a trailing `;` (the IPython convention for
"don't display the last expression"); these two had simply lost theirs at some point (most plausibly
during Phase 39's notebook restructure, since `entry.hillshade.plot_vs_basemap`/
`entry.crop.plot_vs_basemap` didn't exist as call sites before that merge).

Re-adding `;` would have fixed it, but the user raised a real, independent problem with that
convention: `ruff format` strips trailing semicolons as "redundant" (regardless of per-file lint
ignores, which only affect `ruff check`, not the formatter), which is *why* `trntest-lint` had been
excluding `notebooks/*.py` from `ruff format --check` entirely up to now -- a real coverage gap letting
any other formatting drift in those files go unnoticed too. Switched to `_ = expr` instead, verified
directly (not assumed) before committing to it: `ast.parse` confirms `_ = f()` produces an `Assign`
node while `f()`/`f();` produce an `Expr` node, and only a bare `Expr` triggers IPython's
display-hook -- so `_ = expr` suppresses display via the interpreter's own AST semantics, not a
lexical convention formatters treat as noise. Confirmed empirically too: `ruff check`/`ruff format
--check` both pass clean on `_ = make_fig()` with zero special-casing (no `F841` unused-variable
complaint either -- `_` is the standard exempted "discarded value" name). All 6
previously-semicolon-suppressed cells converted; `_lint.py`'s `format_files` carve-out removed, so
`ruff format --check` now genuinely covers `notebooks/*.py` like every other Python file. That
uncovered several pre-existing, previously-invisible formatting issues in `image_generation.py` (long
`print()`/function calls not wrapped, single- vs. double-quote drift) -- fixed by just running `ruff
format` for real over the file rather than by hand.

Verified: real Docker re-run of `image_generation.ipynb` end to end -- both previously-doubled cells
now show exactly one `display_data` output each (checked directly against the regenerated `.ipynb`'s
own cell outputs, not just visually). `trntest-lint` clean (including the newly-real
`ruff format --check` coverage of `notebooks/*.py`); full `pytest` suite (156 tests) passes.

## Phase 41 (2026-08-15) — Merged `plot_dem_ortho`/`plot_camera_footprint` into one figure; renamed
`LunaservResult`/`lunaserv_result` to `DemOrthoResult`/`dem_ortho_result` throughout

Two more small requests from the same manual-review pass as Phase 40.

**Merged Phase 3's two plot cells**: the former `plot_dem_ortho` (ortho + DEM side by side, plain
"sample"/"line" axes) and `plot_camera_footprint` (ortho + footprint overlay, km-labeled axes, added
Phase 40) covered overlapping ground -- the DEM panel is exactly the same AOI as the footprint panel,
just a different band. `plot_dem_ortho` now takes `camera` too and draws the footprint overlay (quad +
center marker) directly on its own left (ortho) panel; the right (DEM) panel gained the same km
`Easting`/`Northing` axis treatment (previously bare "sample"/"line"), rather than left inconsistent
with its new neighbor. `plot_camera_footprint` is gone -- its only caller was the now-removed second
cell (confirmed via `grep`, not assumed, before deleting it), so nothing else needed updating.

**Renamed `LunaservResult`/`lunaserv_result`**: the user flagged this name as misleading -- it bundles
an ortho (still genuinely from Lunaserv WMS) with a DEM that has *not* come from Lunaserv since the
Astropedia GLD100 switch (`docs/history.md`'s own earlier "Fix synthetic render stripe artifact"
entry). Confirmed the mismatch is real, not cosmetic: the function that builds one,
`lunaserv.fetch_dem_and_ortho`, was already accurately named (dem *and* ortho, source-agnostic) --
only the dataclass itself kept the stale single-source name from before that DEM-source switch. Chose
`DemOrthoResult`/`dem_ortho_result` (matching that already-correct function name exactly, over the
alternative `BasemapResult`/`basemap_result`, which reads more naturally in prose but is less precise
about contents) after asking the user directly, given the size of the rename (~10 files: the class in
`lunaserv.py`, every parameter/property/field of that name in `render.py`/`session.py`/
`trn_dataset.py`/`plotting.py`/`dataset.py`/`isis_wac.py`/`__init__.py`, plus `notebooks/
image_generation.py` and the current-state docs). Applied via `sed -i 's/\bLunaservResult\b/.../;
s/\blunaserv_result\b/.../'` per file (word-boundary-anchored, confirmed first via `grep -w` that
nothing else -- e.g. `lunaserv_srs_template`, `fetch_lunaserv_getmap` -- would be caught by accident),
not by hand, given the volume. Deliberately left `docs/history.md`'s own past entries and
`old_notebooks/` untouched -- both are frozen narrative/archive, not current-state references (see
`AGENTS.md`), so they correctly keep the old name describing what was actually true at the time.

Verified: real Docker re-run of `image_generation.ipynb` end to end (merged cell's own output
extracted and visually inspected -- footprint quad + center marker on the left panel, matching
`Easting`/`Northing (km)` axes on both panels); `trntest-lint` clean; full `pytest` suite (156 tests,
unaffected -- nothing test-covered referenced either the old plotting functions or the old name)
passes.

## Phase 42 (2026-08-15) — Closed the `wac_isis_spike.py` gap Phase 40's lint tightening left open;
renamed the notebook to `wac_isis.py`

Phase 40 dropped `trntest-lint`'s `ruff format --check` carve-out for `notebooks/*.py`, but only
converted `image_generation.py`'s own semicolon-suppressed cells -- a follow-up `trntest-lint --all`
run (checking the whole repo, not just files changed vs. `HEAD`, which is what the default `--diff`
mode and the pre-commit hook actually check) surfaced that `notebooks/wac_isis_spike.py` still used
the old trailing-`;` convention on 6 `plotting.plot_raster(...)` cells, so `ruff format --check` now
failed there too in `--all` mode (silently, since normal commits never touched that file and so never
tripped it). Converted all 6 to `_ = plotting.plot_raster(...)`, same as Phase 40.

Also renamed the notebook itself, `wac_isis_spike.py`/`.ipynb` -> `wac_isis.py`/`.ipynb` (`git mv`,
preserving history) -- the "_spike" name stopped being accurate a while ago: `docs/plan.md`'s own open
items section already describes the striping investigation this notebook was for as resolved and
folded into `isis_wac.py`'s real pipeline, with this notebook now serving as "the step-by-step version
for isolating pipeline stages," a reference/debugging tool, not an active spike. Updated the file-path
references in `AGENTS.md`, `docs/plan.md`, and a `docker/Dockerfile` comment to match; deliberately
left this entry's own text and `old_notebooks/` alone (frozen references to what was actually true at
the time, not current-state pointers -- same reasoning as Phase 41's rename). jupytext's pairing
config is filename-pattern-based, not a hardcoded name, so renaming both halves together was enough to
keep the `.py`/`.ipynb` pairing intact with no separate config change.

Verified: real Docker re-run of the renamed notebook end to end (no errors, single-figure output on
every previously-semicolon cell); `trntest-lint --all` now passes clean across the whole repo (40
files, not just the diff-scoped subset Phase 40 verified); full `pytest` suite (159 tests) passes.

## Phase 43 (2026-08-15) — Wired the Robbins crater overlay into the notebook; confirmed ellipse
orientation

`craters.crater_overlay_layer` (query/filter/ellipse construction, added in an earlier session) was
fully built and tested but never actually called from `image_generation.py` -- `docs/plan.md`'s open
items tracked this as "still planned," alongside an unresolved caveat: `DIAM_ELLI_ANGLE_IMG`'s
rotation reference isn't documented in the Robbins PDS4 label, so ellipse *orientation* (unlike
size/position) was unconfirmed pending a real visual check against crater rims in the hillshade
basemap.

Wired it in: both Phase 5B (`entry.hillshade.plot_overlay`) and Phase 6B (`entry.crop.plot_overlay`)
now draw `craters.crater_overlay_layer` as an extra `plotting.OverlayLayer`, computed once
(`crater_layer`/`crater_layers`) since both share the same base raster (`dem_ortho_result.ortho`)
and therefore the same query AOI/CRS -- `crater_overlay_layer` returns `None` when nothing's in
view, so `layers` is left `None` rather than an empty list in that case.

Verified: real Docker re-run of `image_generation.ipynb` end to end (4,633 real craters in view over
this run's real AOI, no errors); extracted and visually inspected both overlay GIFs' rendered
frames -- ellipses land tightly on real crater rims throughout the hillshade basemap, including
visibly elongated (non-circular) craters, whose ellipse's long axis matches the real rim's, not
perpendicular to it -- confirms `_ellipse_polygon`'s current rotation interpretation is correct
as-is, no code change needed. `trntest-lint` clean.

## Phase 44 (2026-08-15) — Crater overlay: size/quality filtering and legible styling, driven by
live user feedback in the running notebook

Phase 43's unfiltered crater overlay (4,633 craters over a real ~250km AOI) turned out too visually
dense once the user actually looked at it in Jupyter Lab -- the ellipses overwhelmed the base image
rather than reading as an annotation. Iterated live against the running notebook (`docker compose
run --rm demo trntest-lint`/`scripts/run_notebook.sh` each round, plus pulling and visually
inspecting the rendered overlay GIF frames) through several rounds of user feedback:

1. Added `crater_overlay_layer(..., min_major_km=...)` (`DIAM_ELLI_MAJOR_IMG`, a full axis
   length/diameter) -- `min_major_km=20.0` cut 4,633 craters down to 40.
2. Tried fading the outline (`alpha=0.3`, `linewidth=0.6`) to declutter further -- this made rim
   alignment *harder* to judge, not easier (a faint line is hard to place precisely), so reverted.
3. Tried a sparse dotted `linestyle` instead (`OverlayLayer` gained this field -- any matplotlib
   linestyle, including custom `(offset, (on, off, ...))` dash tuples, passed straight through to
   `.boundary.plot(...)`) at full width/opacity: keeps each dot high-contrast while leaving most of
   the underlying rim visible between dots. Landed on a dashed (not pure single-pixel-dot) pattern
   after the first attempt (`(0, (1, 10))`) proved too faint to read without zooming in.
4. User wanted the *same 40 craters but better ones*, using crater quality/"grade," not just size.
   Investigated the real Robbins database for this: no dedicated degradation/sharpness field exists
   at all -- confirmed both from the live PDS4 label (all 21 fields are position/size/shape/fit-SD)
   and, more definitively, from the real archive-description PDF shipped in the downloaded bundle
   (`lunar_crater_database_archive_description.pdf`), which states the database's purpose is "a
   uniform, complete census of lunar impact craters" built by manually tracing rims and fitting
   ellipses -- a locations-and-sizes census, not per-crater freshness grading, which is a separate,
   far more labor-intensive research task. `ARC_IMG` (fraction of a crater's own rim circumference
   actually traced/used in its ellipse fit) is the closest real proxy, but empirically confounded
   with size: 41% of *all* craters have `ARC_IMG==1.0` (small bowl craters are trivially fully
   traced) vs. just 2.5% of craters ≥20km major axis (larger craters are more often complex,
   overlapped, or degraded) -- so it's only meaningful as a filter *within* a size band, not
   applied to the whole database. Added `min_arc_img` alongside `min_major_km`; grid-searched the
   real AOI data (not guessed) to find `min_major_km=9.0, min_arc_img=0.86` landing on the same ~40
   count while favoring rim-completeness within that size band.
5. User observed the smaller-crater-biased 40 read as less visually prominent than the original
   size-only 40, despite the same count -- relaxed to `min_arc_img=0.75` (~80 craters, grid-searched
   the same way) to roughly double the count and restore visual presence.
6. User asked what styling was available; after discussion, manually tuned the final notebook cell
   directly in the running Jupyter Lab kernel (color `"#ffddbb"`, `linestyle=(0, (1, 6))`) rather
   than continuing to round-trip every small style tweak through the assistant -- picked up here by
   re-running `trntest-lint`/`scripts/run_notebook.sh` for a clean, reproducible execution and
   fixing one now-stale markdown phrase ("near-white `color`") the manual edit had left behind.

Added `tests/test_craters.py` coverage for both new filter params (`min_major_km`, `min_arc_img`,
including the "filters everything out -> `None`" cases and a combined-filter case matching the
notebook's actual usage).

Verified: real Docker re-run of `image_generation.ipynb` end to end at each step (final state: 78
craters in view, no errors); `trntest-lint` clean; full `pytest` suite (171 tests) passes.

## Phase 45 (2026-08-15) — ISIS `photomet` (Hapke) as an alternate hillshade mode, using the real
camera position for true per-pixel emission/phase

Evaluated replacing the ortho basemap's plain Lambertian hillshade (`shade_ortho`,
`matplotlib.colors.LightSource.hillshade` -- diffuse-only, no opposition surge, no real lunar
reflectance behavior) with ISIS's own `photomet` application using a real Hapke bidirectional
reflectance function (`PHTNAME=HAPKEHEN`, `NORMNAME=SHADE`). Added as a new `hapke=True` mode on
`lunaserv.fetch_dem_and_ortho`/`despeckle_and_shade_ortho` (`hapke_shade_ortho`) alongside the
default, not a replacement -- a feasibility prototype, not lunar-calibrated
(`_HAPKE_PLACEHOLDER_PARAMS`).

**The core obstacle**: `photomet`'s automatic angle sources (`ANGLESOURCE=ELLIPSOID`/`DEM`) need a
real ISIS camera model embedded in the cube (via `spiceinit`) to derive incidence/emission/phase
angles from -- but this ortho is a flat, map-projected mosaic with no ISIS camera model at all,
real or synthetic. Confirmed via `photomet -help`/`photomet.xml` (read directly, not guessed) that
`ANGLESOURCE=BACKPLANE` sidesteps this entirely: it accepts externally-supplied
phase/incidence/emission angle rasters, so `photomet` only ever does the Hapke math, never the
geometry. Wrote those angle rasters as plain (non-georeferenced) ISIS3 cubes directly via GDAL's own
`ISIS3` driver (`rw+v`, confirmed via `gdalinfo --formats`) -- no `gdal2isis`/`isis2std` round-trip
needed. One undocumented `photomet` requirement found only by running it: the `FROM` cube (a pure
size/dtype template in `BACKPLANE` mode -- `NORMNAME=SHADE` overwrites its actual pixel values)
still needs a `BandBin` label group just to open at all, added via `editlab` since GDAL's `ISIS3`
writer doesn't create one from scratch.

**First version (angle rasters) assumed a nadir viewer** -- emission from each pixel's local
surface normal vs. straight up, phase constant across the whole scene -- correct for describing an
already-existing flat WMS mosaic, but not a real camera's actual perspective geometry, so it could
never capture the emission-angle-dependent brightening across a frame that's exactly the kind of
effect a real Hapke BRDF is supposed to add over Lambertian. Replaced with the real, finite camera
position (`Camera.camera_center_moon_me_m`) instead: `_camera_local_enu_m` expresses it as
(East, North, Up) meters relative to the same local tangent point `fetch_dem_and_ortho`'s own local
Orthographic CRS is centered on (a real `+proj=ortho` map projection's (x, y) for a given (lon, lat)
depends only on that (lon, lat), not on any real elevation carried in a separate raster band -- so
the DEM grid's own (x, y) + elevation already effectively live in this same tangent-plane frame, the
same locally-flat approximation `_terrain_photometric_angles`'s surface-normal gradient already
relied on even before this change). `_terrain_photometric_angles` then computes a true per-pixel
view direction (real parallax, from each pixel's own vector to the finite camera position) instead
of an idealized infinitely-distant nadir viewer, so emission and phase now genuinely vary per pixel;
incidence is unaffected (still just needs the Sun's own effectively-parallel-ray direction). This
stays entirely upstream of `sat_sim` (shading the ortho *before* its own geometric reprojection into
the camera's pixel grid, same as the Lambertian path already does) -- no relighting-after-render or
ISIS camera model for the synthetic view was ever needed, just the real position this project's own
SPICE-derived `Camera` already carries.

Added `tests/test_lunaserv.py` coverage for the new pure-geometry helpers (no ISIS/Docker
dependency): `_camera_local_enu_m` against a direct overhead-altitude case and a real consistency
check against `orthographic_xy_m` for an on-sphere point (same tangent-plane projection, two
different derivations); `_terrain_photometric_angles` against a flat-DEM case (incidence constant
`90 - elevation_deg` everywhere; emission/phase ~0 directly below the camera) and an off-nadir pixel
matched against the exact flat-ground `atan(horizontal_distance / altitude)` formula.

`notebooks/hapke_hillshade.py`/`.ipynb` (new) blinks the existing Lambertian basemap against a
freshly Hapke-shaded one for the same footprint, via the same `plot_overlay_toggle` blink-comparator
Phase 5B/6B use -- reuses Phase 1-2's manifest/camera setup but skips `dataset.populate()` (no
`sat_sim` render or ISIS WAC crop needed for this comparison).

Verified: extracted and visually inspected the rendered comparison -- a smooth, physically-expected
difference gradient (Hapke brighter than Lambertian toward the side of the frame the real camera
views more obliquely, consistent with a real emission-angle-dependent BRDF effect Lambertian has no
equivalent of), not noise or a broken render. `trntest-lint` clean; full `pytest` suite (177 tests,
`test_lunaserv.py`'s new geometry cases included) passes; real Docker re-run of
`hapke_hillshade.ipynb` end to end, no errors.

## Phase 46 (2026-08-15) — Made Hapke the default basemap shading, wired into `image_generation.ipynb`

Phase 45's `hapke_shade_ortho` was opt-in (`hapke=False` default); this flips it -- added
`lunaserv.DEFAULT_HAPKE_SHADING = True`, used as `fetch_dem_and_ortho`'s/`despeckle_and_shade_ortho`'s
own parameter default, with the original plain Lambertian `shade_ortho` kept available as an
explicit fallback (`hapke=False`), not removed. `image_generation.ipynb` needed no code changes at
all to pick this up -- `TrnTestEntry.dem_ortho_result` already calls `fetch_dem_and_ortho` with no
explicit `hapke=` argument, so it now gets the Hapke-shaded basemap automatically; only its Phase 3
markdown was updated to say so.

**A real correctness risk found and fixed before it could bite**: `fetch_dem_and_ortho` picks a
mode-specific output filename (`ortho_shaded_hapke.tif` vs. `ortho_shaded.tif`, added in Phase 45 so
both variants could coexist for `hapke_hillshade.ipynb`'s comparison) -- but
`TrnTestEntry.dem_ortho_result`'s own resumption check (skip re-fetching if a prior run's ortho/DEM
already exist on disk) still hardcoded the literal `"ortho_shaded.tif"` name. Left as-is, flipping
the default would have silently resumed a *stale, pre-existing Lambertian file* under the new
"default" filename for any `_work/<edr_product>` folder already populated from before this change
(exactly the state this project's own worktree output folders were already in, from earlier
sessions' runs) -- the notebook would have looked unchanged despite the default flip, with no error
raised anywhere. Fixed by factoring the filename logic into a shared
`lunaserv.ortho_shaded_filename(hapke)` helper, used by both `fetch_dem_and_ortho` and
`dem_ortho_result`'s resumption check against the same `DEFAULT_HAPKE_SHADING` constant, so the two
can never disagree about which cached file counts as "the" default.

Updated `hapke_hillshade.ipynb`'s own framing to match (now a reference/regression comparison
between the current default and the fallback, not an "should we do this" evaluation -- its Fetch
cell now resumes `entry.dem_ortho_result` as the Hapke variant and explicitly fetches
`hapke=False` for the Lambertian one, the reverse of Phase 45's version); light doc updates
(`docs/data-sources.md`'s `sat_sim`-shading note, `lunaserv.py`'s own docstrings) to stop describing
Lambertian as the assumed default.

Verified: full `pytest` suite (177 tests) still passes; `trntest-lint` clean; real Docker re-run of
`hapke_hillshade.ipynb` end to end (confirms mode-aware resumption picks the right file, not a
stale one) and `image_generation.ipynb` end to end (confirms the main pipeline's basemap is now
genuinely Hapke-shaded with no code changes needed there), both with no errors.

## Phase 47 (2026-08-15) — `along_track_correction`: a single-frozen-camera-pose fix, found via a
real user-spotted mismatch and iterated twice against real `campt` ground truth

**Found by the user, not a code review**: looking at Phase 6B's real overlay, the Hapke basemap read
brighter on the north edge while the real WAC crop was brighter in the southeast -- not the kind of
thing a visual sanity check alone would catch as *wrong* (both looked like plausible lunar terrain),
but a real, diagnosable geometry mismatch once flagged.

**Root cause, confirmed via real `campt`, not guessed**: `hapke_shade_ortho`'s per-pixel angles use
one *frozen* camera position (`Camera.camera_center_moon_me_m`, matched to the crop's own
center-frame time) -- but a real WAC crop is a real multi-second pushframe scan. Ran `campt` at the
crop's own corners and center: real `SpacecraftPosition` differed by **~150km** between the crop's
north and south edges (`~97s` apart in real `EphemerisTime`, implying ~1.6 km/s -- matching LRO's
real orbital speed, a good sanity check the numbers are real). The frozen pose is only accurate near
the crop's own center; phase/emission computed from it are increasingly wrong toward the edges --
invisible under the old Lambertian shading (sun-only, no camera-position dependence at all), only
now visible because Hapke's phase/emission terms are the first camera-position-*dependent* ones in
this pipeline.

**First fix -- project out the raw orbital velocity direction.** User's proposal: rather than model
real per-line spacecraft position (this project's existing per-line timing machinery, `isis_wac`'s
own reconstruction, would allow this but is real added complexity), approximate it by discarding the
raw view direction's component along the spacecraft's real orbital velocity (`spkezr`'s own velocity
half of the state vector, MOON_ME) before computing emission/phase -- on the theory that a real
scanning pushframe sensor observes each line close to nadir *in its own along-track direction* at
the instant it's captured, so keeping only the cross-track component of the (wrong-position) raw
view direction approximates that. Validated directly against real `campt` phase/incidence/emission
at the same 5 points (crop corners + center): phase error at the 4 corners dropped from as much as
~30 deg to within ~7 deg (mean absolute error 4.8 deg); emission mostly improved too, less
uniformly. Wired in as `lunaserv._terrain_photometric_angles`'s `along_track_correction` (off by
default); visually confirmed via a new `notebooks/along_track_correction.ipynb` (basemap-vs-real-WAC
diff, brightness-matched at the median) -- mean|diff| against the real crop dropped from 12.1 to
9.2, and critically, the strong north/south bias the user had actually spotted (+8.6 mean diff on
the north half, -10.5 on the south) shrank to near-zero on the north half (-1.4) -- confirms the fix
targets the actual reported symptom, not just an unrelated metric.

**Second fix -- the user correctly suspected raw orbital velocity wasn't quite the right vector,
and was right, though not in the exact way first proposed.** Real spacecraft velocity is a generic
physical fact; it isn't necessarily parallel to the *sensor's own* along-track axis if the real
camera has any off-nadir pointing (which this one does, `off_nadir_deg` is real and nonzero). User's
proposed replacement: `z' x x`, where `z'` is the real (re-aimed) optical boresight direction
(`camera.py`'s "Boresight re-aiming" -- confirmed the same ~6 deg real offset the user recalled) and
`x` is the camera frame's own X axis. Checked this against `camera.py`'s own existing sensor-model
axis convention comment (top of the module) before implementing blind: it identifies the *pre-twist*
X axis as along-track (py) and `cross(z, x)` -- exactly the user's proposed vector -- as **cross-track**
(px), the other one. Tested all three candidates directly against the same 5 real `campt` points
(mean absolute phase error, the cleanest metric here since phase doesn't depend on surface normal at
all, unlike incidence/emission): raw orbital velocity 4.8 deg; the user's `z' x x` (cross-track)
16.9 deg, markedly worse, confirming it's the wrong axis; the pre-twist X axis (true along-track)
**1.3 deg** -- a real, substantial win over the raw-velocity version, confirming the user's core
insight (derive it from the camera's own re-aimed attitude, not generic orbital motion) once pointed
at the right axis.

Replaced `Camera.camera_velocity_moon_me_km_s` (added for the first version, now unused) with
`Camera.camera_along_track_direction_moon_me` -- a unit vector, not a velocity, computed directly in
`build_camera` as `look_at_rotation`'s own pre-twist X-axis output, before `rotation_about_boresight(k)`
is applied (confirmed sign/k-twist doesn't matter here: perpendicular-projection is invariant to
which of +/-along-track the vector points). `camera_pose_moon_me` reverted to its original 4-tuple
return (no longer needs to carry velocity through). Re-ran `along_track_correction.ipynb` with the
new vector: the whole-crop mean|diff| metric came out statistically indistinguishable from the
raw-velocity version (9.2 either way) -- confirmed this isn't a bug (the two candidate vectors are
only ~6 deg apart in this case, and the aggregate metric is dominated by near-center pixels where
neither correction matters much, diluting a corner-concentrated improvement) rather than evidence
the refinement didn't help; the direct `campt` point comparison remains the decisive, uncontaminated
evidence for which vector is actually more accurate.

Added `tests/test_lunaserv.py` coverage: `_local_enu_direction` against a pure-radial-vector case
(no tangent-point subtraction, unlike a position), and `_terrain_photometric_angles`'s
`along_track_local_enu` parameter against an exact synthetic case (an along-track-aligned camera
offset component should be fully removable, leaving only the cross-track-implied angle).

Verified: full `pytest` suite (179 tests) passes; `trntest-lint` clean; real Docker re-run of
`along_track_correction.ipynb` end to end with the new vector, no errors.

## Phase 48 (2026-08-15) — Made `along_track_correction` the default

Same shape of change as Phase 46 (made Hapke the default), and the same stale-cache lesson applied
proactively this time rather than found the hard way: `lunaserv.ortho_shaded_filename` now takes
`along_track_correction` too and gives the corrected default its own real filename
(`ortho_shaded_hapke_atc.tif`), distinct from the uncorrected `ortho_shaded_hapke.tif` -- rather than
have `DEFAULT_ALONG_TRACK_CORRECTION` flip to `True` while any already-cached
`ortho_shaded_hapke.tif` (real files already on disk in this project's own worktree output, from
Phase 47's own testing) silently kept serving uncorrected content under what would otherwise still
look like "the default" filename. `TrnTestEntry.dem_ortho_result`'s resumption check now asks for
`ortho_shaded_filename(DEFAULT_HAPKE_SHADING, DEFAULT_ALONG_TRACK_CORRECTION)` explicitly, matching
`fetch_dem_and_ortho`'s own new defaults.

Added `DEFAULT_ALONG_TRACK_CORRECTION = True`, used as `along_track_correction`'s own parameter
default across `hapke_shade_ortho`/`despeckle_and_shade_ortho`/`fetch_dem_and_ortho`. Neither
`image_generation.ipynb` nor `hapke_hillshade.ipynb` needed code changes to pick this up -- same as
Phase 46, `entry.dem_ortho_result` already calls `fetch_dem_and_ortho` with no explicit
`along_track_correction=` argument. `along_track_correction.ipynb` itself no longer needs its
end-of-notebook "restore the real default" cleanup fetch at all now that each combination has its
own filename (removed); its comparison rows were reordered/relabeled so the corrected default (now
top) reads as "today's default" and the uncorrected variant (now bottom) as the fallback, matching
`hapke_hillshade.ipynb`'s own default-first convention. Light doc updates
(`image_generation.py`'s Phase 3 markdown, `docs/plan.md`'s `lunaserv.py` row) to describe both
defaults together rather than describing `along_track_correction` as still-experimental.

Verified: full `pytest` suite (179 tests) passes; `trntest-lint` clean; real Docker re-run of all
three affected notebooks end to end (`along_track_correction.ipynb`, `hapke_hillshade.ipynb`,
`image_generation.ipynb`), no errors; confirmed directly (not just assumed) that
`image_generation.ipynb`'s own `dem_ortho_result.ortho` now resolves to
`ortho_shaded_hapke_atc.tif`, the new default's real filename.

## Phase 49 (2026-08-16) — Found and substantially reduced a real `cam2map` striping artifact in
6B, found while investigating camera-pose alignment

Started from the user noticing the real-WAC/basemap overlay (6B) wasn't perfectly aligned and asking
whether ASP's bundle-adjustment tools could correct the underlying SPICE-derived pose (see
`docs/plan.md`'s open items for the full alignment-tooling research trail: `bundle_adjust`/
`image_align` conflict with the already-abandoned CSM/`mapproject` Pushframe bug; ISIS's own
`jigsaw`+`findfeatures` space-resection-against-a-basemap route being architecturally sound but
blocked on cross-sensor feature matching, itself explored via a real `findfeatures` spike -- see
that section for the matching investigation's own outcome). While visually checking the WAC image at
native resolution (not the earlier full-frame view, where it wasn't visible), a real, structured
striping artifact turned up in `isis_wac.run_cam2map_for_crop`'s output -- distinct from, and not
fixed by, the already-known dead-column framestitch artifact (confirmed by applying
`plotting._fill_dead_columns_for_display`'s interpolation to a real copy of the source cube via
GDAL's ISIS3 driver `rw+` support and re-running `cam2map`: zero visible change).

Root-caused via a direct `PATCHSIZE` sweep (1/2/4/8/14) re-running `cam2map` on the same crop cube,
same native-resolution zoomed region: the striping gets markedly worse at `PATCHSIZE=8`/`14`, and
`PATCHSIZE=1` looked clean in an initial percentile-stretched comparison against the existing
pipeline default (`PATCHSIZE=4`, chosen in an earlier phase using only an aggregate crop-vs-full
correlation number) -- confirming this is a `cam2map` warp-patch-boundary artifact, not something in
the source data. **Not a complete fix, caught by direct user visual inspection of the real pipeline
output** (not the earlier ad hoc comparison files): a faint residual striping remains visible at
`PATCHSIZE=1` on close inspection. Quantified with a high-pass (Gaussian-blur-subtracted) comparison
against the old `PATCHSIZE=4` output: only a modest ~2.4% reduction in fine-scale energy (std 0.00256
vs. 0.00262) -- real, but far more modest than the initial "clean" read suggested. Judged (by the
user) consistent with genuine, modest photometric discontinuities at framelet transitions, inherent
to any patch-based warp, not the more severe missing/bad-data-looking pattern `PATCHSIZE=4` showed --
and a reasonable stopping point, diminishing returns past here. `PATCHSIZE=1` costs real runtime
(~16s vs. ~10s for one crop, confirmed timed) but no coverage trade-off (71.39% vs. 71.38%,
essentially identical) -- a real, worthwhile improvement, just not full elimination. Changed
`run_cam2map_for_crop`'s `PATCHSIZE=4` to `PATCHSIZE=1`.

Also notable from the alignment side-investigation itself (kept here rather than a separate entry,
since the striping fix was its main actionable outcome): a `findfeatures` spike matching the
mapprojected WAC crop against the basemap found real feature-matching machinery working correctly
(thousands of keypoints, dozens of matches surviving ratio/symmetry/RANSAC/epipolar verification),
but ISIS's control-point-construction step discarding 100% of them regardless of `TARGET=`/
`GEOMTYPE=` settings tried -- traced to the basemap being a plain GDAL-exported GeoTIFF rather than
something ISIS itself map-projected, so it lacks whatever ISIS-native geometry metadata that step
needs, independent of the striping issue. A hand-rolled OpenCV reimplementation of the same matching
pipeline (needed since `findfeatures` doesn't expose raw match coordinates) reproduced the same ~46
match count, but real-world offset statistics between matched pairs (mean 659m, std 344m, individual
distances spanning 88m-1.6km) showed far too much scatter to represent a single clean rigid pose
correction -- likely a mix of some real correspondences buried in false matches, not a usable
control-point set as-is. Camera-pose alignment itself remains unresolved; this phase's concrete
result is the striping fix, which matters independent of the pose question for any real use of the
mapprojected WAC output.

Verified: real `PATCHSIZE` sweep with side-by-side native-resolution visual comparison, a follow-up
high-pass quantitative comparison after the user caught the initial "clean" read overstating it, and
valid-coverage/runtime measurements (all above); full `pytest` suite (179 tests) passes; `trntest-
lint` clean; real Docker re-run of `image_generation.ipynb` end to end, no errors (Phase 6B's
`cam2map` call now taking ~21s vs. ~15s pre-fix, matching the measured `PATCHSIZE=1` cost).

## Phase 50 (2026-08-16) — Maneuver detection for TRN-OD dataset selection, from a data-set
selection discussion

Started from the user wanting to think through dataset selection for TRN-based orbit determination
testing: images used as OD input need to be maneuver-free in between, but there's no known public
source for LRO's flight-dynamics team's own maneuver log ("small forces file"). Worked through this
in stages, each one informing the next:

1. **`lrodv` CK kernel spike (negative result)**: NAIF's yearly LRO metakernels split CK pointing
   into five flavors, one of which (`lrodv`) is documented as "delta-V/maneuver attitude" -- a
   plausible-looking public proxy for maneuver timing, worth checking before building anything more
   elaborate. Checked file coverage for 2010/2012/2018 (`spice_kernels.parse_metakernel` against
   each year's manifest): all three years have exactly 12 `lrodv` files, same ~30-33 day
   contiguous, overlapping cadence as `lrolc` (routine WAC pointing) -- i.e. continuous coverage
   across the whole year, not short files clustered around discrete burns. Ruled out as a
   maneuver-timing signal.
2. **Literature research**: found Mesarch, "Long-Term Orbit Operations for the Lunar Reconnaissance
   Orbiter," AAS-23-234 (2023), NTRS 20230010952 -- see `docs/data-sources.md`'s new "LRO maneuver
   detection" section for the full facts pulled from it. Key finding: LRO's orbit has had **zero
   stationkeeping maneuvers since 2016** (an unmaintained "drift" orbit ever since), and that
   paper's own Eclipse Phasing Maneuver table has a gap covering all of H2 2019 -- so H2 2019
   (which also happens to encompass this repo's fixture EDR, `M1329714703CE`, 2019-11-30) can only
   contain small (~0.05-0.3 m/s) reaction-wheel momentum-unload events, if anything.
3. **Discontinuity-detection experiment**: the user asked whether momentum unloads specifically
   (not just the far larger, already-ruled-out stationkeeping burns) could be detected directly from
   LRO's public reconstructed-orbit SPK, given they'd corrupt a TRN-OD solve just as much as a
   bigger burn would. Sampled the osculating two-body semi-major axis (`spice.oscltx`) at 2-minute
   cadence across H2 2019 and compared it before/after each sample over a one-orbital-period window
   (cancels most of the periodic gravity-driven oscillation, isolates real persistent steps) --
   found 12 clean candidates, 0.07-0.25 m/s each, 11-30 days apart, matching the paper's "every 2-4
   weeks" cadence and 0.05-0.3 m/s magnitude almost exactly. A real, working signal, not noise --
   the plot showed sharp, isolated spikes 5-15x above the noise floor between events.
4. **Consolidation**: turned the spike into `src/trntest/maneuver_detection.py`
   (`detect_discontinuities`/`sample_osculating_semimajor_axis`/`find_maneuver_candidates`/
   `ManeuverCandidate`/`candidate_utc`) with real test coverage, per the user's request to
   consolidate before adding tests. `detect_discontinuities` itself is pure/SPICE-free (plain numpy
   over an already-sampled series), letting most of the test suite (synthetic-input false-positive/
   injected-step/two-separated-steps/too-short-series cases) run fast, matching this repo's existing
   fast-test philosophy. The two real-SPICE tests -- rerunning the H2 2019 check as an actual
   assertion, plus a new positive-control check against a short 2010 window (pre-frozen-orbit,
   correctly finds real >2 m/s stationkeeping-scale events, confirming the detector isn't just
   tuned to the small H2 2019 case) -- need live SPICE kernels and NAIF network access, which this
   repo's existing test suite had never needed before.

That last point motivated a new project-wide capability: **a `@pytest.mark.heavy` split**, since
`pytest`'s existing documented contract ("nothing that needs live SPICE kernels, network access, or
the ASP binaries") would otherwise have been broken by this pair of tests. Added the `heavy` pytest
marker (`pyproject.toml`, with `addopts = "-m 'not heavy'"` so plain `pytest` is unaffected -- a
later `-m heavy` on the command line overrides that default, the standard idiom for this), plus
`scripts/run_heavy_tests.sh` (thin wrapper around `docker compose run --rm demo pytest -m heavy`,
since heavy tests need the Docker image's spiceypy + real network access, unlike the rest of the
suite). Documented in README's Tests section. This is a reusable pattern for any future test that
legitimately needs the real Docker/SPICE/network stack, not just this module's.

One real debugging detour worth recording: the first version of the synthetic-input fast tests used
a perfectly analytic sinusoid with no noise, which made the before/after median diff *exactly* 0.0
at every non-injected-step sample -- degenerating the MAD-based threshold to exactly 0.0 too (via
`detect_discontinuities`'s own `threshold <= 0` guard, added for exactly this "no signal at all"
edge case) and silently suppressing detection of the real injected step as well. Fixed by adding a
tiny (1m std) noise floor to the synthetic series, matching what any real reconstructed SPK actually
looks like (never *exactly* periodic) -- a good reminder that an idealized synthetic test can be
*less* representative than the real data it's meant to stand in for.

`maneuver_detection.py` is not yet wired into `dataset.select_dataset()`'s candidate filtering --
still a standalone tool (`find_maneuver_candidates(start_dt, end_dt, config)`) for vetting a
candidate date range by hand, per `docs/plan.md`'s architecture table entry.

Verified: `scripts/run_heavy_tests.sh tests/test_maneuver_detection.py` (2 heavy tests) and plain
`pytest tests/test_maneuver_detection.py` (4 fast tests) both pass in Docker; full default `pytest`
run (183 tests) still passes with the 2 heavy tests correctly deselected; `trntest-lint` clean
(`ruff format`/`ruff check`/`mypy`) on all new/changed files.

## Phase 51 (2026-08-16) — Maneuver detection: replaced (a, e, i) with (h, eps), fixing a real
literature-confirmed blind spot

Follow-on to Phase 50, same day. The user raised a sharp objection to the single-channel (semi-major
axis only) detector: a reaction-wheel momentum unload's net impulse has 3 DOF of direction, and if
it's orthogonal to the velocity vector (purely radial or purely normal), it does zero work and
wouldn't show up in `a` at all -- proposed generalizing to a weighted Euclidean norm over 3 orbital
parameters, guessing `(a, e, i)` as the right set.

Working through the physics (Gauss's variational equations) confirmed the concern was not just
plausible but *specifically already true in the literature*: Mesarch et al., AAS-23-234, states
outright that early-mission momentum unloads were flown "in the +/- orbit normal direction to
minimize the along-track perturbative effects of firing LRO's ACS thrusters" -- i.e. deliberately
designed to be invisible to exactly the along-track/energy-based check Phase 50 shipped. `(a, e, i)`
was a reasonable first guess but has its own gap: inclination's Gauss-equation sensitivity to a
normal impulse is scaled by `cos(argument of latitude)` and vanishes at node crossings -- a momentum
unload, firing whenever momentum happens to build up, has no reason to avoid that phase, so `i` alone
would just relocate the blind spot rather than close it.

Reconsidered the approach entirely rather than bolting a 3rd channel onto the existing 2 (per the
user's explicit invitation to do so): dropped classical orbital elements altogether in favor of two
quantities with *exact* (not Gauss-equation-linearized) impulse response --

- **Specific angular momentum `h = r x v`**: `Delta h = r x Delta v` exactly, for any impulse size,
  since position is unchanged mid-burn. This is a *linear*, always-exact map with a clean, phase-
  INDEPENDENT null on the radial component only (`r x (anything parallel to r) = 0`, always -- not
  "at certain orbital phases"). Tracking the full 3-vector (not a derived scalar like inclination)
  is what avoids the node-crossing blind spot `i` alone would have.
- **Specific orbital energy `eps = v^2/2 - GM/r`**: `Delta eps = v.Delta v` to an excellent
  approximation (the `|Delta v|^2/2` correction is 3+ orders of magnitude smaller at these burn
  scales) -- captures the radial+tangential sensitivity `h` structurally can't.

Detection: same before/after one-orbital-period median-window diff as Phase 50, generalized to all 4
channels (h_x, h_y, h_z, eps) at once, each normalized by its own robust (MAD-based) noise floor and
combined via quadrature (`sqrt(sum of squared per-channel z-scores)`) -- self-calibrating rather than
needing hand-derived, phase-dependent analytic sensitivity weights (the user's original "weight by
sensitivity" instinct, just derived empirically per-channel instead of analytically per-element).
Bonus this enabled: at each detected peak, the observed `(Delta h, Delta eps)` can be inverted
(weighted least squares) to reconstruct an actual 3D impulse estimate, decomposed into
radial/tangential/normal (RTN) components -- replacing the old tangential-only magnitude estimate,
which would have been systematically wrong (too small) for exactly the normal-dominant events this
redesign now detects.

Rewrote `src/trntest/maneuver_detection.py` around this (dropped `spice.oscltx`/osculating elements
entirely -- just `spkezr` state vectors now, actually simpler than before) and rebuilt
`tests/test_maneuver_detection.py`'s fast tests on a real RK4-integrated two-body propagator with
directly injected impulses (radial/tangential/normal each isolated), rather than the old ad hoc
sinusoid-plus-step synthetic series -- a much stronger fixture, and directly demonstrates the fix:
a purely-normal injected impulse (the literature-documented blind spot) is now correctly detected and
attributed, which the old single-channel version structurally could not do.

Three real bugs found and fixed via this rebuild, each informative in its own right:

1. **Sign flip in the first tangential-impulse test attempt**: the test injected the impulse along
   `v0`'s direction *at periapsis* (t=0), but the injection point (`MID_SAMPLE`) landed near
   apoapsis, where the local velocity direction is reversed -- an exact-magnitude, opposite-sign
   result was the tell. Fixed by computing the injection direction from an unperturbed reference
   propagation *at the actual injection point*, not the initial state. A test bug, not a detector bug.
2. **Spurious near-duplicate candidates from window-edge ripple**: with very clean synthetic noise,
   the sliding before/after window comparison produced two extra candidates a few micro-m/s in
   magnitude (six orders of magnitude below the real 0.2 m/s injected event) a few minutes after the
   real one, as the window transitioned past the step in slightly different ways. Fixed by skipping a
   full window past each detected peak (not just past the immediate contiguous over-threshold run)
   before resuming the scan -- real distinct maneuvers are always far more than one window apart, so
   this can't merge two genuine events.
3. **A real numerical instability, caught by the heavy H2-2019 test, not a synthetic one**: an
   unregularized weighted least-squares solve reported +373 m/s *radial* for the first H2 2019
   candidate (2019-07-02) -- an SVD check confirmed why: near apsis, `v_R -> 0`, so BOTH `[r x]`
   (always) and `v.dv` (there specifically) lose sensitivity to the radial direction at once, giving
   the weighted measurement matrix a singular value ~3.6e-6x its largest -- far too small to trust,
   but not literally machine-precision-zero, so `numpy.linalg.lstsq`'s default `rcond=None` cutoff
   didn't truncate it, and ordinary least-squares amplified whatever noise was in that direction into
   a wildly implausible number. Fixed with an explicit `rcond=1e-2` cutoff, which correctly reports
   ~0 for that direction instead. Following up on this quantitatively (checking the actual singular
   values at the point of *maximum* radial velocity for LRO's real ~0.02 eccentricity, not just right
   at apsis) revealed the gap is broader than "narrow, apsis-adjacent" as first assumed: for an orbit
   this close to circular, radial-impulse recovery is weak (a few percent conditioning) essentially
   everywhere, not just in a brief window near periapsis/apoapsis passage -- updated the module
   docstring and the corresponding fast test (`test_detect_discontinuities_finds_radial_impulse_
   without_blowing_up`, renamed from `_finds_radial_impulse`) to assert detection-without-blowup
   rather than accurate radial recovery, which isn't achievable here.

The rebuilt detector then surfaced a genuinely new, substantive finding on real data, not just a
robustness improvement: several H2 2019 candidates are **normal-direction-dominant**, up to ~2.1 m/s
total -- several times larger than the ~0.07-0.25 m/s the old tangential-only estimate reported for
the *same dates*, since that estimate was structurally blind to exactly the component driving them.
Cross-checked against a short 2010 window (pre-frozen-orbit): real stationkeeping pairs are
unmistakable (`combined_z` in the hundreds, ~5.2-5.6 m/s, tangential-dominant, alternating sign,
~2h38m apart -- matching the paper's "~3 hours" and posigrade/retrograde description almost exactly),
cleanly separated from momentum-unload-scale candidates in the same window (`combined_z` single-to-
low-double-digits). Updated `docs/data-sources.md`'s "LRO maneuver detection" section and
`docs/plan.md`'s architecture table entry with the new method and this finding.

Verified: all 5 fast + 2 heavy tests in `tests/test_maneuver_detection.py` pass (heavy via
`scripts/run_heavy_tests.sh`); full default `pytest` (184 tests) passes with heavy tests correctly
deselected; `trntest-lint` clean (`ruff format`/`ruff check`/`mypy`) on all changed files.

## Phase 52 (2026-08-16, `feature/alignment` branch, not merged to `main`) — Preserved the tie-point
pose-alignment spike as real, checked-in code

Continuation of Phase 49's alignment investigation, done live via ad hoc shell commands and scratch
scripts (not committed) -- the user asked to get it to a real, reproducible, checked-in state before
going further. Consolidated into `src/trntest/pose_alignment.py` (a new module: `to_uint8_for_matching`,
`crop_to_footprint`, `match_features`, `pixel_points_to_map`, `fit_similarity_correction`,
`apply_correction`) plus `notebooks/pose_alignment_spike.py`/`.ipynb`, a real notebook exercising the
whole pipeline against the current default dataset candidate. Added `opencv-python-headless` (for
`cv2`'s SIFT/RANSAC -- not needed anywhere else in this project) and `affine` (already an indirect
`rasterio` dependency, now direct since this module imports it itself) to `pyproject.toml`; `cv2`
needed a `follow_imports = "skip"` mypy override (its bundled stubs are real but incomplete/
inconsistent with the runtime API -- a genuine `SIFT_create` attr-defined error, not a "module not
found" case `ignore_missing_imports` would fix) plus one inline `# type: ignore` the override alone
didn't clear.

Two real design points surfaced turning the spike into real code, both from direct user pushback,
kept in the module's own docstrings so they don't get silently re-litigated later: (1) matching two
already-map-projected rasters directly needs `crop_to_footprint` to bound the basemap down to the
WAC's own real extent first -- confirmed empirically to matter for match quality, not just compute;
and (2) `fit_similarity_correction` uses a similarity transform (translation+rotation+uniform scale)
*not* because that's asserted as the physically correct model -- a real 6-DOF camera pose error on a
pushframe sensor's extended-exposure capture doesn't map cleanly onto any fixed 2D DOF count, so
that would be overclaiming -- but as the simplest starting point for interpretability, with richer
models an explicitly open empirical question for later, contingent on having enough well-distributed
tie points to support them without overfitting.

Real, checked-in-code run against the current default candidate: 106 matched points, 53 inliers
under the similarity fit (145m mean inlier residual vs. 651m mean if the 53 outliers are forced to
fit) -- broadly consistent with the live spike's own numbers (which used a slightly different
basemap footprint/crop), confirming the module correctly reproduces the investigation, not just that
it runs. Deliberately **not merged into `main`** -- pushed to its own `feature/alignment` branch,
since this is still an unvalidated exploratory approach (see `docs/plan.md`'s open items), not a
finished pipeline feature.

Verified: full `pytest` suite (185 tests, 6 new for `pose_alignment.py` covering each function with
deterministic synthetic fixtures, including a real recovered-known-shift check for `match_features`
itself, not just the pure-math functions) passes; `trntest-lint` clean (`ruff format`/`check`, mypy,
notebook sync); real Docker re-run of `notebooks/pose_alignment_spike.ipynb` end to end, no errors;
extracted and visually inspected both blink-overlay GIF outputs from the real executed notebook.

## Phase 53 (2026-08-16, same `feature/alignment` branch) — Matching at the WAC crop's real native
resolution instead of the interpolated 100 m/px working grid, substantially improves match count and
per-pixel residual

Prompted by a direct user question about input quality rather than a further correction-model
change: is Phase 52's map-projected WAC crop grossly oversampled for feature matching? A direct
measurement (`cam2map PIXRES=camera`, no map-file override, on the current default candidate) found
the crop's own real native resolution is **184 m/px** -- confirmed independently via the pipeline's
already-ray-traced cross-track/along-track ground geometry (211 m/px cross-track, 151 m/px
along-track, same order of magnitude, anisotropic as expected for a pushframe sensor). `cam2map`'s
`PIXRES=map` forces the actual mapprojected output onto the basemap's ~100 m/px working grid
regardless -- a real ~1.8x linear (~3.4x area) oversampling by interpolation, specific to the WAC
side: the basemap ortho (`luna_wac_global`) is a genuine ~100 m/px native mosaic, so only the WAC
crop was being upsampled before matching.

Added to `pose_alignment.py`: `native_wac_gsd_m(camera)` estimates the crop's native GSD from
`Camera`'s already-computed `cross_track_width_km`/`km_per_frame` fields (no extra ISIS call),
taking the *coarser* of the two anisotropic axes so the single isotropic downsample target never
claims resolution finer than either direction actually resolves. `downsample_to_gsd` resamples a
raster onto a coarser grid via `Resampling.average` specifically (not bilinear/nearest -- the
correct decimation filter for genuinely shrinking imagery, approximating what a coarser-GSD sensor
would have actually integrated over, per direct user guidance to get this right). Wired into
`pose_alignment_spike.py`: both the WAC crop and the footprint-cropped basemap are downsampled to
the estimated native GSD before `to_uint8_for_matching`/`match_features`; the fit and
`apply_correction` are unaffected (both operate in real map coordinates / the original full-res
grid), so no other plumbing changed.

A real bug surfaced immediately on the first live run: `downsample_to_gsd`'s nodata fallback
originally assumed `apply_correction`'s existing convention (ISIS's float32 `-3.4e38` sentinel)
unconditionally -- crashed on the basemap ortho, which is `uint8` (`lunaserv.
despeckle_and_shade_ortho`'s shaded output) with no real nodata concept and can't represent that
sentinel at all. Fixed: the float sentinel fallback now only applies when the source raster's own
dtype is floating-point; other dtypes fall through to `src.nodata` (`None` if the file has no tag,
which is correct for a dense raster like the basemap). A regression test locks this in
(`test_downsample_to_gsd_handles_uint8_raster_with_no_nodata`).

Live result on the current default candidate, matching at the estimated 211 m/px native resolution
instead of the interpolated 100 m/px grid: matches surviving ratio/symmetry/RANSAC verification more
than doubled (106 -> 259), inliers nearly doubled (53 -> 91), and the fit is meaningfully tighter
once measured in the pixel units that actually matter -- inlier residual **1.45px -> 0.84px**
(145m/100m vs. 177m/211m; the raw meter number looks slightly worse only because each pixel now
covers ~2x more ground). Supports the oversampling hypothesis: real texture at native resolution
gives the matcher more genuine signal and fewer spurious high-frequency matches (plausibly including
some driven by the still-faintly-visible `cam2map` `PATCHSIZE=1` warp-patch seam artifact, Phase 49)
than matching on an interpolated grid did. Still exploratory, still on `feature/alignment`, not
merged to `main` -- this is a matching-quality improvement to the existing spike, not by itself a
decision to adopt the similarity-transform correction as a finished pipeline feature.

Verified: full `pytest` suite (189 tests, 4 new for `pose_alignment.py`) passes; `trntest-lint`
clean; real Docker re-run of `notebooks/pose_alignment_spike.ipynb` end to end, no errors.

## Phase 54 (2026-08-16, same `feature/alignment` branch) — Full affine and homography fits confirm
the tie-point correspondences are real: visually validated, exercise concluded here

With Phase 53's native-resolution downsampling giving 91 inliers (up from 53), the module's own
long-standing "richer model, once there are enough points" deferral (`fit_similarity_correction`'s
docstring) had a real dataset to test against. Added `fit_affine_correction` (6 DOF: independent
x/y scale + shear, via `cv2.estimateAffine2D`) and `fit_homography_correction` (8 DOF: full
projective, via `cv2.findHomography`), plus `apply_homography_correction` -- a homography isn't
representable as an `affine.Affine` (non-trivial bottom row), so it can't reuse `apply_correction`'s
"compose two affines, then `rasterio.warp.reproject`" path; instead it composes `src_transform`
(lifted to a homogeneous 3x3 matrix), the homography, and `src_transform`'s own inverse into a
single pixel-space projective matrix and warps directly via `cv2.warpPerspective`. All three models
are fit from the *same* match set and applied/compared side by side in
`pose_alignment_spike.py` (four blink overlays: uncorrected, similarity, affine, homography), with
residuals reported in native WAC pixels (`target_gsd_m`), not just meters.

Live result on the default candidate: similarity 91/259 inliers (177m/0.84px mean residual), affine
178/259 (143m/0.68px), homography 189/259 (146m/0.69px, 298m/1.41px max -- the only model whose max
residual improved too). The inlier-count jump is expected to be partly mechanical (more DOF lets a
model bend to satisfy the fixed 300m RANSAC threshold for more of the scattered matches, not
necessarily because those points are all genuinely better-explained) -- flagged explicitly before
the user looked, precisely so the numbers alone wouldn't be oversold as proof.

**Direct user visual inspection of the homography blink overlay settled it**: "beautiful... real
benefit to the higher-order model here, not just noise." **User's own conclusion, recorded verbatim
as the actual stopping point for this exercise**: the correspondences this pipeline finds are
validated as real (not just RANSAC accepting noise within a loose threshold) -- confirmed strongly
enough to justify feeding them into a proper projection-informed alignment (a real camera-model
correction, e.g. actually fixing the SPICE-derived pose or pursuing `jigsaw`/`findfeatures`-style
space resection now that there's real evidence a correction is warranted at all -- see
`docs/plan.md`'s open items) rather than continuing to refine this 2D homography spike further. Not
picked up in this session -- a deliberate stop, not an abandoned thread.

Verified: full `pytest` suite (192 tests, 3 new for `pose_alignment.py` covering `fit_affine_correction`/
`fit_homography_correction`/`apply_homography_correction` with the same known-transform-plus-outliers
pattern as the existing similarity/apply_correction tests) passes; `trntest-lint` clean; real Docker
re-run of `notebooks/pose_alignment_spike.ipynb` end to end, no errors; all four blink-overlay GIFs
visually reviewed live by the user in their own running Jupyter Lab.

## Phase 55 (2026-08-16) — LightGlue as a second tie-point matcher, ~3x the match count at
equivalent quality; merged to `main`

User-requested follow-up to Phase 54's stopping point: try LightGlue (a deep-learned local-feature
extractor + learned matcher) instead of/alongside SIFT, hoping to push match count/quality higher --
specifically as insurance for future, more challenging EDRs (shadowed terrain, low texture) that
classical SIFT might not deliver enough tie points on at all, even though the current default
candidate doesn't itself need it.

**A real, consequential choice surfaced during research, resolved before any code was written**:
LightGlue's most common pairing, SuperPoint, ships pretrained weights and inference code carrying a
Magic Leap proprietary-style notice ("does not convey or imply any rights to reproduce, disclose or
distribute... or to manufacture, use, or sell anything that it may describe"), not a standard
permissive OSS license -- confirmed via direct inspection of the pinned commit's actual source file,
not secondhand summary. Flagged to the user rather than assumed; DISK (Apache-2.0) chosen instead --
the other LightGlue-supported extractor with comparable published match quality and no such
restriction.

**No official `lightglue` PyPI package** — pinned to a specific commit
(`git+https://github.com/cvg/LightGlue.git@eb42fee2d71449efb0aa5c10549752b5d75384d8`) for
reproducibility, since upstream's own `pyproject.toml` has no real version (`version = "0.0"`).
Installed `--no-deps`, deliberately *not* also listed in this project's own `pyproject.toml`
dependencies (unlike every other Python dependency here) -- LightGlue's declared `requirements.txt`
would reinstall a conflicting `opencv-python` alongside this project's own `opencv-python-headless`
(both provide `cv2`, colliding on install) and a separately-pinned `kornia`/`torch`/`torchvision`.
Confirmed via direct source inspection (not assumption) that `lightglue/__init__.py` eagerly imports
*every* extractor submodule regardless of which one is actually used, so `torchvision` (via
`aliked.py`'s `torchvision.ops.deform_conv2d`) and `kornia` (via several submodules, including
`disk.py`) are real hard import-time dependencies of the whole package even though only DISK is
used -- both added directly to this project's own `pyproject.toml` instead, alongside `torch`.

**CPU-only torch, a real Docker/`uv` wrinkle**: `uv pip install` (the pip-compatible interface this
project's Dockerfile uses, not the `uv add`/`uv sync` project workflow) doesn't read
`[tool.uv.sources]`/`[[tool.uv.index]]` from `pyproject.toml` at all -- confirmed before attempting
it, avoiding a wasted rebuild cycle. Fixed with the standard Docker pattern instead: a dedicated
`RUN uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu` step before
the main project install, so torch/torchvision are already satisfied (correct CPU wheels, no
`nvidia-*` runtime packages) by the time the main `-e '.[dev]'` install runs. Verified CPU-only is
safe for the whole LightGlue/DISK code path (no custom CUDA extension compiled or loaded anywhere in
it) via direct source inspection, and confirmed via a real published benchmark (~20 FPS at 512
keypoints on an Intel i7 10700K) that this project's one-shot notebook-cell usage doesn't need a GPU
regardless.

Added `pose_alignment.match_features_lightglue` — same `(from_points_px, to_points_px)` contract as
`match_features`, a drop-in alternative. Two deliberate departures from `match_features`, both
explained in the function's own docstring: (1) no `_sobel_edges` preprocessing first -- DISK/
LightGlue are deep features trained on natural imagery, not edge maps, so feeding them
edge-filtered input would fight what they were trained on rather than help; (2) no internal
homography/fundamental-matrix RANSAC verification pass afterward -- every caller already runs its
own RANSAC when fitting a correction, so a second geometric-verification pass would be duplicated
work. Pretrained weights (LightGlue matcher + DISK extractor, ~50MB total) are real network fetches
on first use, cached under `TORCH_HOME` (`docker/Dockerfile` sets this to `/workspace/cache/torch`,
matching this project's existing shared-`cache/` convention -- see `docs/caching.md`'s new section).

Wired into `pose_alignment_spike.py` as a direct, side-by-side comparison against the existing
SIFT-based `match_features`, feeding the same downstream `fit_homography_correction` (Phase 54's
validated model) for an apples-to-apples result. Live result on the current default candidate: SIFT
259 matches / 189 homography inliers / 146m (0.69px) mean inlier residual, vs. LightGlue **767**
matches / **573** inliers / 164m (0.78px) -- roughly **3x** the match/inlier count at a very slightly
*looser* per-point fit, not a strictly better one. **Direct user visual comparison of the new
blink overlay against the existing ones**: "the quality of the alignment seems pretty much
equivalent to the previous approach, but it's heartening to have more matches to work with" --
confirms the honest read of the numbers (not a quality win on this already-easy, well-textured,
unshadowed candidate) while validating the actual motivation (headroom for harder future EDRs this
candidate doesn't itself test). Kept; merged to `main` (this branch's exploratory module and
notebook are now part of `main`, still not wired into `image_generation.py`'s pipeline).

Verified: full `pytest` suite (197 fast + 4 heavy, one new heavy test --
`test_match_features_lightglue_recovers_a_known_pixel_shift`, following the same
`@pytest.mark.heavy` convention `maneuver_detection.py`'s tests established, needing live network
access for the pretrained-weight download) all pass; `trntest-lint` clean (`ruff format`/`check`,
mypy, notebook sync); real Docker image rebuild (torch/torchvision/kornia/lightglue added, ~2.1GB
image growth) and full re-run of `notebooks/pose_alignment_spike.ipynb` end to end, no errors; new
blink-overlay GIF visually reviewed live by the user in their own running Jupyter Lab, matching the
printed statistics.

## Phase 57 (2026-08-16) — `notebooks/select_datasets.py`: illuminated-node orbit statistics and a
greedy multi-dataset selection algorithm

New exploratory notebook (separate from, and not touching, `data_set_selection.ipynb`/
`dataset_manifest.csv`) built incrementally with the user across one long session, working through a
sequence of real design questions rather than a single upfront spec:

1. **The "illuminated node" concept**: every LRO orbit has an ascending and descending node
   (~180 deg apart in longitude); only one is typically sunlit, picked as whichever has the higher
   sun elevation. Per-orbit statistics collected: that node's longitude, the solar hour angle there
   (`illumination.hour_angle_deg`, new -- -90/0/+90 = sunrise/noon/sunset, sign confirmed against
   real SPICE data, not just derived), acceptable-WAC-EDR count (real catalog data, sun-elevation +
   emission-angle filtered -- "typical nadir mapping mode"), and a maneuver flag
   (`maneuver_detection.find_maneuver_candidates`, Phase 50-51's module, its first real consumer).
2. **First plot** (orbit-level scatter, longitude vs. hour angle): iterated live through several real
   rendering problems, not just parameter tuning -- a connecting line colored by time of year was
   completely invisible under a dense marker layer at ~13 orbits/day regardless of alpha/zorder
   (tried both orderings), eventually dropped entirely per the user's own observation that markers
   alone read as a continuous curve at this density anyway; the "Oranges" colormap's white zero-end
   was indistinguishable from the figure background (fixed by switching to `viridis`, which is also
   perceptually uniform and varies in hue as well as lightness -- easier to read a value off a
   marker's color, a plain user ask: "a colormap that makes it easier to figure out values from
   colors").
3. **Second plot**: a straightforward sun-elevation-vs-acceptable-EDR-count 2D histogram, which
   turned up an almost-perfect linear correlation (expected, since sun elevation is one of the two
   acceptance filters) plus a real, separate population at EDR-count=0 even at 60-90 deg sun
   elevation -- flagged, not chased further this session.
4. **Multi-dataset selection**: a dataset is `DATASET_LENGTH_ORBITS` (24, ~2 days) consecutive
   orbits, acceptable if every orbit is acceptable (no maneuver, minimum per-orbit EDR count) and
   contains no illuminated-node flip (needed so the circular-mean "center" longitude/hour-angle
   actually behaves like an average of nearby values). Selection: greedy farthest-point/max-min
   diversity in center hour angle, each pick excluding future candidates within a tunable center-
   longitude separation or sharing orbits with it, seeded (no diversity to compare against yet) by
   the single most robust candidate. Confirmed with the user this counts as "well-posed" only after
   nailing down: circular mean/distance for longitude (wraparound: -170/+160 average to +175, not
   -5; `illumination.circular_mean_deg`/`circular_distance_deg`, new), an explicit diversity
   objective (max-min, not just "diverse"), a first-pick seeding rule, and an explicit non-overlap
   constraint. Real, non-obvious algorithm behavior surfaced and explained when the user asked why
   loosening a threshold produced *fewer* selected datasets, not more or the same: greedy farthest-
   point selection optimizes diversity of the chosen set at each step, not total achievable count --
   a larger candidate pool can change an early pick's winner (traced concretely: pick 1 differed
   between the two runs), cascading into a different, still-locally-optimal but not-necessarily-
   larger final sequence. Per explicit user request, `select_diverse_datasets` now takes a target
   `n_datasets` and raises `RuntimeError` (rather than silently returning fewer) if the exclusion
   constraints exhaust the candidate pool first.
5. **Plot finalized**: axes pegged to their logical ranges (-180/180 longitude, -90/90 hour angle,
   45/30-degree ticks), wide aspect ratio, horizontal colorbar to maximize marker-resolving width, a
   black/medium-grey "underline" per selected dataset (from `underline_offset_deg` below the first
   orbit's own longitude/hour-angle to the same below the last orbit's) split at the +/-180
   wraparound via a new `illumination.unwrap_relative_deg` (draw in an unwrapped coordinate, clip/
   split wherever it crosses +/-180) -- an earlier orange/magenta pairing was hard to distinguish at
   a glance, black/grey reads unambiguously.
6. **Promoted to library code** per explicit user request ("most cells should become one-liner
   calls... key tunable parameters exposed"): the whole pipeline moved to a new
   `src/trntest/dataset_selection.py` (`find_orbits`/`add_maneuver_flags`/`add_acceptable_edr_counts`/
   `enumerate_candidate_datasets`/`select_diverse_datasets`, one function per notebook cell) and two
   new `plotting.py` functions (`plot_illuminated_node_scatter`/`plot_sun_elevation_vs_edr_count`),
   leaving the notebook itself as tunable constants plus one-line calls. While moving `find_orbits`,
   also fixed the initial kernel-furnish call to use the caller's own `period_start` instead of a
   hardcoded, unrelated fixture date left over from copy-paste -- a legitimate cleanup, though it
   incidentally flipped one extremely close greedy-selection tie via a different SPK-segment
   priority in an overlap region (harmless, same candidate/orbit counts either way).

**Two real, independent bugs found and fixed along the way, both outside the notebook's own new
code:**

- **`illumination.find_node_crossings` was needlessly calling `fetch_and_furnish` (full CK
  resolution) per node crossing**, even though the classification it does (`spacecraft_lonlat_deg`)
  is pure position -- no pointing/CK needed at all. Confirmed via profiling (~70% of the function's
  own runtime) and, more seriously, a real crash: at full-year scale, sweeping across many months,
  `fetch_and_furnish`'s default `isis_resolved` CK source (cached per a single fixed
  `config.edr_product`) can have a filename-encoded date range that nominally overlaps a faraway
  query epoch while the file's *actual* `ckcov` coverage doesn't, tripping its own trust-but-verify
  check. Fixed by dropping the per-crossing `fetch_and_furnish` call entirely (SPK is already
  furnished for the whole window; LSK/PCK are the caller's existing responsibility, same convention
  `utc_to_et` already documents) -- also a real, incidental performance win.
- **`catalog.list_products`'s pagination silently truncated large queries.** It decided whether to
  fetch another page from `len(page_df) < _PAGE_SIZE` -- the *parsed* row count -- but a page can
  (and, on a real full-year query, did) have a handful of entries `parse_catalog_entries` drops for
  a missing/malformed field, landing the parsed count just under `_PAGE_SIZE` even though the server
  sent a genuinely full page with more results still to come. This silently truncated a real
  full-year EDR query to its first 5000 raw entries (4996 parsed) out of what should have been
  ~53k -- confirmed live by directly re-querying and counting raw `<Product>` tags in the response.
  Fixed by deciding continuation from the server's own raw entry count instead; added a regression
  test (`test_list_products_keeps_paginating_past_a_page_with_a_parse_failure`) since this path had
  no test coverage at all before (unsurprising: no existing caller had ever queried widely enough to
  trigger it).

**Also added, from a genuine near-incident mid-session**: before running the notebook's first
full-year cold-cache sweep (~50-70 real HTTP requests across NAIF/PDS ODE, estimated and explained
to the user before executing), the user asked to verify it would be "kind to the server" --
confirmed `cache.py`'s existing per-request pacing (`_REQUEST_PACING_SECONDS`, from the Phase 36
rate-limit incident) already covers this generically, then the user asked for a new, durable rule:
message other running agents *before* starting anything request-heavy, not just after, so concurrent
agents can stagger rather than risk their independently-safe bursts combining into a real rate-limit
trip. Added as a new bullet in `docs/environment.md`'s "Agent-to-agent messaging" section, then
immediately followed in-session (messaged `a1-30` before the real sweep).

Verified: full `pytest` suite (211 tests: 5 new for `illumination.py`'s circular-math helpers, 1 new
regression test for `catalog.py`'s pagination fix) passes; `trntest-lint` clean (`ruff format`/
`ruff check`/`mypy`) on all changed/new files; multiple real, live Docker re-runs of
`notebooks/select_datasets.py` end to end across every design iteration (not just the final one),
each visually reviewed by the user in their own running Jupyter Lab (a persistent `docker compose up`
server started mid-session at their request, on this worktree's assigned port 8889).

## Phase 58 (2026-08-16) — Bridging `select_datasets.py`'s orbit-sequence picks into the older
EDR-list `TrnTestDataSet` world, plus a real LROC rate-limit incident along the way

Phase 57's `select_diverse_datasets` picks *orbit windows* (a start/end UTC span), not individual
images -- unusable as-is by `TrnTestDataSet`/`TrnTestEntry`, which expect a `dataset.
DATASET_COLUMNS`-shaped table of individual EDR entries. The user described the target mental model
(paraphrased): the core object is a list of *image entries*, each with camera parameters and pose;
an entry's origin need not be an EDR at all (e.g. future-mission orbit propagation, with different
pluggable generators per origin type); a selected orbit window is best thought of as its own type
(a plain pandas table is fine, no literal class needed) that becomes the primary argument to a
constructor turning it into a table of acceptable EDRs. Confirmed the concrete plan with the user,
who explicitly scoped out generalizing `TrnTestEntry` for non-EDR origins as future work, and gave
an explicit constraint to carry forward: resolve **one** selected window at a time, not all of them
-- the same fast-iteration-on-one-item discipline this project has followed throughout (e.g.
`TrnTestDataSet.populate(limit=1)`).

**Implementation** (`src/trntest/dataset.py`, `dataset_selection.py`):

- `dataset.images_for_window(start_dt, end_dt, config, ...)` -- `select_dataset()`'s own catalog-
  query/evaluate/finalize tail, generalized from "search fresh over N days" to "evaluate this exact
  window," sharing `_evaluate_illuminated_candidates`/`_finalize_images` (the latter newly extracted
  from `select_dataset()`, behavior-preserving) rather than forking the logic.
- `dataset._prefilter_by_catalog_metadata` -- a cheap pre-filter (sun-elevation from
  `incidence_angle_deg`, optionally emission angle) computed straight off catalog fields, before any
  per-candidate network fetch, with a deliberate false-positive-over-false-negative margin
  (`prefilter_margin_deg`, default 5 deg).
- `attach_cdr=False` as `images_for_window`'s default: confirmed via grep that the `cdr_*` columns'
  only real consumer anywhere in the codebase is `wac.py`, itself already superseded by
  `isis_wac.py` -- a legacy artifact from before ISIS's own functions were found to work directly
  from EDRs, per the user's own read of it, which the investigation confirmed. Skips an unneeded
  per-candidate network round-trip for callers (like `resolve_orbit_sequence`) that don't need it.
- `dataset_selection.resolve_orbit_sequence(orbit_sequence: pd.Series, config, ...)` -- the actual
  bridge, a thin wrapper around `images_for_window` taking exactly one row (not the whole
  `select_diverse_datasets` table). Named after an initial user suggestion of "bless" (called "a
  colorful metaphor" by the user themself, who asked for something more descriptive); renamed to
  `resolve_orbit_sequence` to match this codebase's existing `resolve_*` naming precedent
  (`resolve_ground_to_image_model`, `resolve_wac_ck_kernels`, `resolve_crop_pixels`).

**Real incident**: the first live end-to-end test (one real 24-orbit window, 295 raw candidates,
before the pre-filter existed) tripped a genuine HTTP 429 on `pds.lroc.im-ldi.com` (the LROC EDR
label host), `Retry-After=3600s`. Reported immediately; messaged peer agent `a1-30` to rule out
combined load (confirmed uninvolved). Root cause inconclusive (an earlier small smoke test had just
succeeded on the same host moments before). Mitigated three ways at the user's direction: the
catalog-metadata pre-filter above (295->207 candidates for this specific window -- a real but
modest ~30% cut, since a pre-selected "good" window naturally has a high true-positive rate
already); `attach_cdr=False`; and a general (not host-specific) pacing increase in `cache.py`'s
`_REQUEST_PACING_SECONDS`, 0.2s -> 0.5s, on explicit instruction ("Let's dial down to 0.5s spacing
wherever we had spacing before... Hoping that we'll mostly have warm caches in practice"). Verified
the ban cleared via one targeted re-fetch of the failed product, then re-ran the real test
successfully: 207 images resolved in 32.7s, zero errors. Also caught, mid-incident, having skipped
the "ping other agents before request-heavy work" rule (Phase 57) on the retry -- the user asked
directly ("Did you ping a1 by the way?"); acknowledged the miss and sent a belated heads-up.

**Notebook wiring**: `notebooks/select_datasets.py` gained two cells --
`dataset_selection.resolve_orbit_sequence(selected_datasets.iloc[0], ...)`, then
`TrnTestDataSet.create()` on the result into a new `orbit_sequence_dataset` folder (kept separate
from `data_set_selection.py`'s canonical `trn_dataset`, since this pipeline is still exploratory),
also writing `orbit_sequence.csv` (the one selected window's own row) alongside `manifest.csv` for
debugging/provenance, per the user's original suggestion. Stops short of `populate()` -- no
rendering wired in yet. Executed for real via `scripts/run_notebook.sh` (a host-side script -- it
itself shells out to `docker compose`, so it must run outside the container, not inside it as first
attempted) after pinging `a1-30` beforehand this time; the window was already cache-warm from the
incident retry above, so the resolve cell finished in under a second with zero fresh LROC requests.

Verified: full `pytest` suite (209 fast tests, 2 new for `_prefilter_by_catalog_metadata`) passes;
`trntest-lint` clean on all changed files; a real, live end-to-end notebook run, its output
(`manifest.csv`/`orbit_sequence.csv`) inspected directly in the dataset folder.

## Phase 59 (2026-08-17) — Removed `notebooks/data_set_selection.py`/`.ipynb` and the now-dead
`select_dataset()` code path

With Phase 58's bridge in place, the user was ready to retire the original catalog-driven selection
notebook. Investigated first rather than assuming scope: `dataset_manifest.csv` (the checked-in
selection result) turned out to be read by five other notebooks (`image_generation.py`,
`hapke_hillshade.py`, `pose_alignment_spike.py`, `along_track_correction.py`, mentioned by
`select_datasets.py`), all with no runtime dependency on `data_set_selection.ipynb` *itself* — only
on the CSV file it last wrote. Two explicit scope decisions confirmed with the user before touching
anything:

1. **Freeze `dataset_manifest.csv`, delete the notebook** (over rewiring the manifest-reading
   notebooks onto `select_datasets.py`'s new pipeline, or leaving the manifest/notebook alone
   entirely) — the CSV stays exactly as it is, just no longer regenerable via that notebook; the
   demo pipeline is otherwise unaffected.
2. **Also delete `dataset.select_dataset()`** (+ `session.select_dataset()` + its `__init__.py`
   export) once confirmed it had zero remaining callers anywhere in the codebase, not even tests —
   genuinely dead code once the notebook was gone, not just an unused convenience wrapper. Its
   shared internals (`_evaluate_illuminated_candidates`, `_finalize_images`) stay, since
   `dataset.images_for_window()` (Phase 58) still uses them.

Deleting `select_dataset()` cascaded one level further: its own private helpers
(`_candidate_geometry_windows`, `_pick_best_window`, `DEFAULT_SEARCH_START`) had no other caller and
were removed with it, which in turn left `illumination.node_terminator_offset_deg` and
`illumination.find_ascending_node_crossings` with zero callers (neither had test coverage either) —
removed as the same dead-code cleanup, not a separate decision. `illumination.find_node_crossings`
(the more general function `find_ascending_node_crossings` wrapped) stays — still live, called by
`dataset_selection.find_orbits`.

Reworded every docstring/comment across `dataset.py`, `dataset_selection.py`, `session.py`,
`__init__.py`, `camera.py`, `config.py`, `spice_kernels.py`, `cache.py`, `notebooks/wac_isis.py`,
`notebooks/image_generation.py`, `notebooks/select_datasets.py`, `AGENTS.md`, `README.md`,
`docs/environment.md`, `docs/plan.md`, `docs/dataset-plan.md`, and `docs/data-sources.md` that
described `select_dataset()`/`data_set_selection.ipynb` as the *current* live behavior — pointing
each at its real current equivalent (`images_for_window()`, the frozen `dataset_manifest.csv`,
`dataset_selection.add_maneuver_flags`) instead. Left alone, deliberately, every mention that's
already a historical citation of a specific past incident or run (this file's own past entries,
`docs/caching.md`'s Phase 36 citation, `docs/data-sources.md`'s "the product the live demo
notebook's `select_dataset()` path actually chose" passage, `old_notebooks/` — an explicitly frozen,
unmaintained archive per its own README) — those are accurate statements about what happened at the
time, not claims about the code as it stands today, same distinction this project's history entries
have always been trusted to preserve.

Notebook re-sync: `image_generation.py`/`select_datasets.py`/`wac_isis.py` only had markdown/comment
cells edited (no code cells touched), so `jupytext --sync` regenerated their `.ipynb` twins without
needing a real re-execution — confirmed via diff that no cell outputs or `execution_count`s changed,
only source text.

Verified: full `pytest` suite (209 tests) passes; `trntest-lint --all` clean (`ruff format`/
`ruff check`/`mypy`/notebook sync/notebook warnings) across every file, not just the changed ones.

## Phase 60 (2026-08-17, `feature/reproject` branch, not merged) — Building `reproject`, the third
`TrnTestImage` type, found and fixed a real synthetic-camera FOV bug along the way

Started on the third, reserved-but-unbuilt `TrnTestImage` type (`docs/dataset-plan.md`): `sat_sim`
fed by the real WAC crop's own reflectance (`isis_wac.run_cam2map_for_crop`) instead of the Lunaserv
basemap, through the *same* synthetic camera as `hillshade` (byte-identical pose/FOV/intrinsics,
so the two are directly comparable) -- per the user's own framing, "use `sat_sim` but for input data
use the RDR of our WAC crop essentially." User flagged upfront that the synthetic FOV might not
reliably stay inside the real WAC swath and asked to test on one real image first, rather than
assume.

That instinct was right. First live test (`M1327210646CE`) found a real, asymmetric `NODATA` gap:
96.3% overall valid, but the outer edge ring only 79.4%, bottom two corners 53-58%, and (per direct
user visual inspection of the render) the entire bottom row empty, "thicker" at the top. Root-caused
to two coupled effects (both confirmed by decomposing real ground positions into cross-track/
along-track components, not just eyeballing): (1) `build_camera()`'s `fv = fu` calibrates the
along-track FOV to a flat, non-perspective target (`n_frames_for_square_crop * km_per_frame`) but
renders it through the same real ray-traced perspective projection `fu`'s own cross-track target
uses -- a real, confirmed ~4.2km/2.8% overshoot; (2) even after fixing that alone, the far corners
stayed elongated *cross-track* too (~81-82km vs. the crop's own near-constant ~70km), because a
corner ray combines both angular offsets at once and lands farther out in *both* components the more
oblique it is -- a coupling a standard 4-parameter pinhole (`fu,fv,cu,cv`) can't fully separate,
since `fu` can't depend on `py`. Two earlier, partial fix attempts (symmetric `fv` shrink; then an
asymmetric `fv`/`cv` solve against the along-track edge *midpoint*) each helped some but plateaued
around 75-82% at the worst corner -- diagnosed and moved past each, not just tuned further.

**Fix that reached 100%**: shrink `fu` by a tuned `FU_SCALE` (0.93), then solve `fv`/`cv`
independently by ray-tracing the actual *corner* (both offsets together, not just one axis) against
the real crop's own measured near/far corner ground truth (`entry.crop_footprint`, real ISIS
`campt`), with an additional `AT_MARGIN` (0.93) shrink -- both deliberately conservative per the
user's own explicit call mid-investigation: "we can accept a bit of arbitrary shrinkage on the frame
sensor FOV if that's what it takes to solve the problem reliably... there is some variation due to
terrain and we would want to build in a bit of margin in any case." Result on the one tested image:
valid pixels 96.3% -> 100.0%, worst corner 53.6% -> 100.0%.

**Two other things found along the way**: (1) a real process bug -- the spike notebook's early cells
called `dataset.populate(limit=1)` before grabbing `entry = dataset[0]`; since entry 0 already had
`crop`+`hillshade` from a prior `image_generation.ipynb` run, `populate(limit=1)` silently advanced
to the next *undone* entry instead and did real, unintended Lunaserv/Astropedia fetches + ISIS
generation on 3 unrelated manifest rows (confirmed via `dataset.status()`) -- fixed by dropping the
`populate()` call, since entry 0 never needed it. General trap worth remembering: `populate(limit=N)`
on an already-populated entry advances the queue, it doesn't no-op. (2) A user-prompted architectural
observation, not acted on: the *existing* boresight correction (`build_camera()`'s `look_at_rotation`
re-aiming, `docs/data-sources.md`'s "WAC-VIS's real boresight isn't `spice.pxform`'s `[0,0,1]`") was
modeled as a frame *rotation* -- the user's own words, "it was always going to be more correct to
model it as a bias in `cv`, since that's what it is in the real WAC VIS," which this investigation's
own `cv`-bias fix (for a different problem) ended up validating the shape of. Revisiting the original
boresight correction that way is a separate, bigger change, not started.

**Deliberately not merged to `main`** -- pushed to its own `feature/reproject` branch, same pattern
as `feature/alignment`: unvalidated past one image, and not yet wired into a real
`TrnTestReprojectImage` class (still ad hoc notebook code producing a second, `_fovfix`-suffixed
`.tsai`/camera alongside the normal one). Session ended here for token-budget reasons -- full status,
open questions (does a single `(FU_SCALE, AT_MARGIN)` generalize across images or does the solve need
to run fresh per-image; where the corrected FOV should live without changing `hillshade`/`crop`'s own
FOV; the boresight-bias-vs-rotation question above) captured in
`docs/reproject-fov-investigation.md`, referenced from `docs/plan.md`'s open items, for whoever picks
this up next.

Verified: no test/lint changes this phase (notebook-only work); real, live Docker re-runs of
`notebooks/reproject_spike.py` at every stage of the investigation (not just the final one), each
inspected via its own printed coverage numbers and rendered output.

## Phase 61 (2026-08-18, `feature/reproject` branch, still not merged) — Validated Phase 60's FOV
fix generalizes across 4 real images

Picked back up per Phase 60's own stated next step: does the tuned `(FU_SCALE=0.93, AT_MARGIN=0.93)`
pair hold up on other real candidates, or does the solve need retuning per image? Added a reusable
`evaluate_reproject_coverage()` to `notebooks/reproject_spike.py` (the same crop→reproject→render→
coverage pipeline as Phase 60's investigation, refactored into a function so it could run repeatedly)
and re-ran the *same*, unchanged constants against 3 more real candidates already available in the
`trn_dataset` folder (crop+hillshade already generated from Phase 60's own accidental
`populate(limit=1)` advance — reused rather than wasted, and avoided repeating that same mistake by
never calling `populate()` again in this pass), deliberately spanning a wide latitude/off-nadir
range: `M1327211014CE` (55.4°N), `M1327211334CE` (70.7°N), `M1327215525CE` (-67.5°S), against the
original `M1327210646CE` (38.5°N).

**Result: all four reach ~100% valid-pixel coverage with the unmodified constants** (worst case
99.8%, negligible) — up from a 95.5-99.2% "solve-only" baseline (the corner-ray `fv`/`cv` solve alone,
no `FU_SCALE`/`AT_MARGIN` shrink) whose own worst corner ranged 57.8-77.1%. This resolves Phase 60's
open "per-image solve or fixed constant?" question: a single fixed constant pair holds up across this
range, at least for candidates from the same manifest/EDR family this demo already uses — no evidence
yet that per-image retuning is needed. Not proof it holds at every conceivable off-nadir angle/
latitude (all 4 tested are still non-polar WAC-VIS with similar `n_frames_for_square_crop`), but a
real, meaningful result: the original fix wasn't overfit to one image.

Full first-run timing (before a lint-driven reformat, re-run to confirm results were unchanged, see
below) showed the added validation pass costs ~155s of real Docker time (`cam2map` + `sat_sim` run
per candidate per baseline/fixed pair, 8 renders total) — consistent with Phase 60's own per-run cost,
not surprising or a new performance concern.

Still not wired into a real `TrnTestReprojectImage` class — the open items from Phase 60 (where the
corrected FOV should live; the boresight-bias-vs-rotation tangent) are unchanged and still the actual
blockers, not this validation gap. See `docs/reproject-fov-investigation.md`'s "Validated: the fix
generalizes across 4 real images" section for the full table and discussion.

Verified: no test/lint changes needed to `src/trntest/` itself; `trntest-lint --all` flagged one
`ruff format` issue in the new notebook code (a too-long line), fixed and the notebook re-run to
confirm identical results post-format; full `trntest-lint --all` clean after.

## Phase 62 (2026-08-18, `feature/reproject` branch, still not merged) — Wired the reproject FOV fix
into `camera.build_camera()`, built the real `TrnTestReprojectImage` class, and caught two live
regressions along the way

Picked back up per Phase 61's own validated conclusion and a direct question to the user: where
should the corrected FOV live? The investigation doc had flagged this as an open architectural
question -- a `reproject`-specific camera variant (simpler, no risk to `hillshade`/`crop`) vs. inside
`build_camera()` itself (would also shrink `hillshade`'s FOV). The user's answer resolved it: a
future goal is SSIM/LPIPS/diff-style scoring between `hillshade` and `reproject`, which needs them
pixel-grid-identical -- only possible if the correction lives inside `build_camera()`, applied once.
The user also pointed out the earlier "would degrade `crop`'s alignment" worry didn't actually apply:
`crop` naturally needs to stay a bit larger, since it's real source data providing margin, not
something that needs FOV parity with the other two.

**`camera.py` changes**: `solve_corrected_fov` (the spike notebook's `FU_SCALE`/`AT_MARGIN` solve,
generalized into a reusable function) is now called from inside `build_camera()` itself, right after
the existing boresight re-aim -- reuses the same real WAC crop (`tie_points.
crop_footprint_corners_for_camera`) `build_camera()` already produces internally for that re-aim, so
this costs no new ISIS work. `Camera` gained 4 new fields: `focal_length_u_px`/`focal_length_v_px`
(replacing the old single `focal_length_px`, which implicitly assumed `fu=fv`) and
`principal_point_u_px`/`principal_point_v_px` (replacing an implicit, project-wide `cu=cv=image_size/2`
assumption). `footprint_lonlat`'s `"center"` entry changed from a hardcoded `(size/2, size/2)` to the
real boresight ray `(cu, cv)` -- the two coincided everywhere before this fix (by construction), so
this was a latent bug waiting for exactly this kind of change to expose it; every consumer of
`footprint_lonlat_deg["center"]` (AOI centering, sun-angle lookups, display rotation) wants the real
pose target, not literal image-center. Live-validated directly: on the demo's own default candidate,
the fix reproduces last session's exact tuned numbers (`fu=235.25, fv=249.40, cu=128.00, cv=133.26`),
`footprint_lonlat_deg["center"]` now matches the real crop's own `campt`-derived center to 0.000 deg,
and both a hillshade-style and reproject-style render through the corrected camera hit 100% valid
coverage (hillshade was never broken, just untested at the corrected FOV before now).

**`trn_dataset.py` changes**: `TrnTestReprojectImage(TrnTestHillshadeImage)` -- subclasses
`TrnTestHillshadeImage`, not `TrnTestImage` directly, since (confirmed by this session's own
implementation, not just docs/dataset-plan.md's original guess) only 4 members need overriding
(`raster_path`/`sidecar_json_path`/`render_label`/`_generate_impl`, the last just feeding a
WAC-crop-textured `DemOrthoResult` into the same `render.run_sat_sim` call `hillshade` already uses)
-- everything else, including `_mapprojected_path`, is inherited unchanged and picks up the right
`raster_path`/`sidecar_json_path` via ordinary dynamic dispatch. Deliberately kept out of
`PRODUCT_TYPES` (`populate()`'s default product-type set) -- opt-in only via an explicit
`product_types=` argument, since it isn't wired into any notebook yet and has real dataset-scale
validation still to do. Live-validated end to end against a private scratch dataset folder (not the
shared demo one): populated `crop`+`hillshade`+`reproject` for the demo's default candidate,
confirmed `hillshade.width_km`/`height_km` exactly equal `reproject`'s (byte-identical camera,
confirmed not just asserted), both renders 100% valid, and both `plot_vs_basemap`/`plot_overlay`
produce correct, well-aligned figures (visually inspected, not just "didn't crash").

**Two real regressions found by re-running the flagship `image_generation.ipynb` end to end** (not
by reasoning alone -- both were live, measured failures):

1. `plotting.plot_isis_comparison` (the live Phase-comparison figure `image_generation.py` calls) and
   `TrnTestHillshadeImage.width_km`/`height_km` both reused `Camera.cross_track_width_km`
   (crop-window-derived) as a stand-in for the synthetic render's own real width/height, and assumed
   the render was exactly square -- both true before this session (`fu=fv`, derived from the same
   half-angle the crop window used), false after. Fixed by adding `Camera.render_cross_track_km`/
   `render_along_track_km` (`camera.footprint_width_height_km`, a real ground-chord measurement of
   the corrected footprint's own 4 corners) and switching both consumers to them;
   `cross_track_width_km` itself is untouched, still correctly describing the real crop's own extent
   (`TrnTestCropImage.width_km`, `pose_alignment.py`'s crop GSD calc) -- those were never affected by
   a synthetic-camera-only fix and don't need to be.
2. `tie_points.select_tie_points`'s 5 QA-overlay tie points dropped from 5-of-5 resolving (the demo's
   own documented default-candidate result, from an earlier phase's investigation) to 1-of-5 once the
   FOV fix was wired in -- caught directly in the re-run notebook's own printed warning, not
   anticipated. Root cause: `tie_points.die5_points` anchored its 5 points on the shared bounding
   box's own naive `(lon_min+lon_max)/2, (lat_min+lat_max)/2` midpoint, not the true shared boresight
   center -- harmless while the synthetic footprint was symmetric around its own center (the naive
   midpoint and the true center were the same point by construction), wrong once
   `solve_corrected_fov` made the footprint asymmetric (near corners ~91k m from center, far corners
   ~100k m) enough to shift the naive midpoint measurably away from the true center. Confirmed via
   direct `campt` queries: even the "center" test point itself failed with "no surface intersection"
   against the real crop's own pushframe camera model, despite being geometrically inside the
   (axis-aligned-box-approximated) intersection of both footprints' inscribed boxes -- the real
   containment guarantee that reasoning relies on assumes an accurately-centered box, which the naive
   midpoint no longer provided. Fixed by giving `die5_points` an explicit `center` argument
   (`select_tie_points` already computes `synthetic_center`, the real shared boresight point) and
   anchoring all 5 points on it -- each of the 4 corner points now scaled by its own reach from
   `center` to its own side of the bbox, not a single shared box half-width. Live-validated: 5 of 5
   tie points resolve again on the default candidate, and the "center" tie point's real crop pixel
   lands within ~2px of the crop's own true center pixel (previously exact by construction, now
   exact again).

The user's own framing for why re-running the real notebook mattered here, not just the isolated FOV
validation: these two regressions were both real, silent behavior changes in code paths the FOV fix
never touched directly (`plotting.py`, `tie_points.py`) -- neither would have been caught by
`solve_corrected_fov`'s own direct tests, since both are about *downstream consumers'* implicit
assumptions about `Camera`'s fields, not the FOV solve's own correctness.

**A third regression, found only by the user's own direct visual inspection in Jupyter Lab** (not by
anything automated in this session, including this session's own visual check of
`TrnTestReprojectImage`'s overlay output): Phase 5B's `mapproject`-based blink overlay, previously
"always very accurately aligned" per the user, came out visibly misaligned. Root cause: `cam_gen`'s
conversion of our `.tsai` to a CSM Frame model-state JSON has only one, isotropic `m_focalLength`
field -- confirmed live it silently averages an asymmetric `fu`/`fv` into one value
(`(235.25+249.40)/2 = 242.32`, matching the JSON exactly), harmless while `fu=fv` always held, a real
~5% one-axis distortion once it no longer did. Quantified directly: the CSM-reprojected footprint's
own bounding box came out nearly square (143.1x142.6 km) instead of the correct,
`render_cross_track_km`/`render_along_track_km`-matching non-square shape (146.0x139.1 km).

First fix attempt bypassed the CSM sidecar entirely (`mapproject -t pinhole` against the `.tsai`
directly) -- worked, but the user asked whether this was really a CSM model limitation or just
`cam_gen`'s own conversion being lossy, hoping to keep a correct CSM sidecar available too (relevant
to `docs/plan.md`'s still-open question about the CSM JSON standing in for a literal ISD file later).
Investigated rather than assumed: `ale`'s own real-instrument CSM formatters (`ale/drivers/
lro_drivers.py`, installed in this image) populate the model's `m_iTransL`/`m_iTransS`/`m_transX`/
`m_transY` fields directly from NAIF's real, genuinely anisotropic instrument-kernel keywords for
actual flight cameras -- proving the CSM Frame model itself fully supports per-axis anisotropy, `cam_gen`
just doesn't populate it for a synthetic Pinhole conversion. Confirmed by hand-patching a `cam_gen`
sidecar (pivoting `m_focalLength` to `fu`, rescaling `m_iTransL`/`m_transY` by `fv/fu`/`fu/fv`) and
re-running `mapproject -t csm`: reprojected footprint came out 146.3x139.2 km, matching `-t pinhole`
to ~0.2%.

**Final fix**: `render._correct_csm_focal_length_anisotropy`, called right after `cam_gen` in
`run_sat_sim`, restores the sidecar itself (pivots `m_focalLength` to `fu`, rescales whichever of
`m_iTransL`'s two coefficients `cam_gen` set nonzero by `fv/fu`, `m_transY`'s matching coefficient by
the reciprocal, preserving sign rather than assuming a fixed index) -- a more foundational fix than
the first attempt, since the sidecar copied into the dataset folder (`hillshade/*.json`, `reproject/
*.json`) is now genuinely correct for any future consumer, not just the one call site that happened
to need it this session. `TrnTestHillshadeImage._mapprojected_path` (inherited by
`TrnTestReprojectImage`) reverted back to the CSM sidecar (`camera_type="csm"`, the default) now that
it's correct at the source; `render.run_mapproject_image` stayed generalized (`camera_path`/
`camera_type`) as good hygiene even though its one live caller no longer needs `"pinhole"`.
`TrnTestCropImage`'s own `_mapprojected_path` (ISIS `cam2map`, not ASP `mapproject`) was never
affected by any of this. See docs/reproject-fov-investigation.md for the full trail.

Live-validated across all 4 candidates used throughout this investigation (not just the one
hand-patched case): each candidate's freshly-rendered, auto-corrected CSM sidecar's own
`mapproject -t csm` footprint matches its `-t pinhole` ground truth to within 0.00-0.27% (vs. the
original bug's ~2-4%), confirming the fix generalizes across different `fu`/`fv` ratios and sensor
rotations (`boresight_rotation_k`), not just the one candidate it was derived on.

Verified: full `pytest` suite (213 tests -- +1 `die5_points` center-anchoring test, +3 new
`test_render.py` tests for `_correct_csm_focal_length_anisotropy`: restores per-axis scale,
preserves sign on a flipped-axis convention, no-ops when `fu==fv`) and `trntest-lint --all`
(format/check/mypy/notebook sync/notebook warnings) clean; `image_generation.ipynb` regenerated
end-to-end via `scripts/run_notebook.sh` against the corrected camera (forced via
`TrnTestDataSet.truncate()` on the demo's own default candidate's `hillshade`, since it was already
cached from a prior run and wouldn't otherwise regenerate), three times across this phase -- Phase
5A's tie-point overlay and Phase 5B's blink overlay both visually confirmed correct in the final run,
now via the restored CSM path. Held for the user's own review in Jupyter Lab before committing, per
this repo's standing practice for notebook-output changes.

**Session ended here, mid-investigation, for token-budget reasons -- one more real finding, not yet
resolved.** The user pushed back on the "~0.27%" CSM-vs-pinhole agreement number above ("that sounds
high"), rightly: a deterministic-control test (re-running `-t pinhole` against itself: bit-identical,
zero noise floor) proved any CSM-vs-pinhole disagreement is real; a precise ground-point-placement
check (not texture correlation, which gave noisy/inconsistent results) found a small but genuine
**constant** positional offset (~1-8px, not growing with distance -- ruling out a residual
scale/anisotropy error) between the corrected-CSM and pinhole reprojections; a symmetric-camera
(`fu=fv`) control confirmed this offset is specific to the asymmetric-FOV correction, not a
pre-existing CSM-vs-Pinhole quirk. The exact mechanism remains unexplained -- the derived correction
model predicts zero residual on the untouched sample/column axis, but empirically there's still ~8px
there. Root-causing further would need `usgscsm`'s own source (only the compiled `.so` is present in
this Docker image). Left the code in its current state (CSM path, `_correct_csm_focal_length_anisotropy`
live in `render.run_sat_sim`) rather than reverting to the proven-exact `-t pinhole` workaround,
since it's a real, substantial, live-validated improvement over the original bug regardless, and
reverting without being asked would have been a unilateral call on a question the user was actively
weighing. See `docs/reproject-fov-investigation.md`'s "OPEN: an unexplained small residual" section
(added this session) for exact repro numbers and the full diagnostic trail, so whoever picks this up
doesn't have to re-derive any of it.

## Phase 63 (2026-08-19, `feature/reproject` branch, merged to `main`) — Closed the CSM residual by reverting to an isotropic FOV; fixed a real, unrelated tie-point bug found along the way

Picked up Phase 62's open CSM-vs-pinhole residual. First did one more diagnostic round before
deciding anything: reconstructed `cam_gen`'s pristine, pre-correction sidecar (inverting
`_correct_csm_focal_length_anisotropy`'s own known operations) and tried three different, but
mathematically equivalent, ways of splitting the `fu`/`fv` anisotropy across the CSM state's fields
(pivot `m_focalLength` to `fu`, the shipped correction; pivot to `fv` instead; leave `m_focalLength`
at the original average and scale both `iTrans` fields). All three gave the *identical* residual,
`(row -1, col +8)`, at every point -- ruling out an encoding bug on this project's side definitively,
and confirming the residual is a genuine `usgscsm` quirk with anisotropic Frame models, not
fixable without its source.

The user reconsidered the anisotropic FOV correction itself in light of this: it was only ever a
nice-to-have (more of the real crop's margin used, not a correctness requirement), and it had now
cost three real bugs (this residual, the `cam_gen` `m_focalLength` collapse from Phase 62, and the
`die5_points` anchoring regression from Phase 61) -- with a real risk that other downstream CSM/ISIS
consumers of this data would hit the same kind of friction. Decision: revert `camera.
solve_corrected_fov` to isotropic -- solve `fu`/`fv` exactly as before (same two independent
half-angle solves), but collapse them to one shared `f = max(fu, fv)` applied to both axes, rather
than keeping them separate; `cv` re-derived against this shared `f` to keep the near edge exactly on
target. Checked empirically before committing to it: across the same 4 real candidates the
anisotropic fix was validated on, the isotropic version reaches **100.0% coverage on every one**
(actually improving `M1327211014CE`'s 99.83% worst-corner to 100%), at the cost of a ~4-6% smaller
cross-track footprint (along-track was already the binding constraint -- `fv > fu` -- on all 4, so
along-track extent is essentially untouched). `render._correct_csm_focal_length_anisotropy` deleted
outright as dead code (a no-op once `fu == fv` always), along with its dedicated tests. Re-running
the flagship notebook end to end confirmed `mapproject -t csm` and `-t pinhole` now agree exactly
(0px at all 5 points) and the real WAC-crop reproject coverage check still hits 100%. 210 tests pass,
lint clean. See `docs/reproject-fov-investigation.md`'s "RESOLVED: reverted to an isotropic FOV"
section for the full trail.

**A second, unrelated bug found and fixed along the way.** The isotropic revert's smaller footprint
moved the demo's default candidate's die5 tie points enough that one (`top_right`) started dropping
during `tie_points.resolve_crop_pixels` -- initially misdiagnosed (via pattern-matching onto this
project's own already-documented `_CROP_EDGE_MARGIN_PX` crop-edge numerical instability) as an
edge-of-crop effect from the smaller footprint. Checked the actual ISIS error text rather than
trusting that assumption, prompted by a cross-agent conversation with `feature/alignment`'s own
session (`a1`): the real error was "no surface intersection", not "not inside cube" -- the signature
of a completely different, pre-existing bug `a1` had independently found and root-caused
(`docs/wac-jigsaw-investigation.md`): `campt`'s own ground-to-image solve has a real, *scattered*
(~38% on this same default candidate, no edge concentration -- `a1` measured resolved-vs-dropped
edge-distance directly and found no significant difference) failure rate for WAC's Pushframe sensor,
a known upstream ISIS bug (`PushFrameCameraGroundMap::GetLocalNormal`, DOI-USGS/ISIS3#4256) entirely
unrelated to the FOV revert -- which just moved `top_right`'s die5 position enough to land in that
pre-existing failure mode where no point had before. Fixed once `a1`'s `wac_camera_model.
find_framelet_and_project` (`feature/alignment`, merged to `main` this session) landed: a from-scratch
reimplementation of ISIS's own WAC-VIS camera model, validated to exact (0.000px) agreement with real
`campt` output, whose own containment check sidesteps the bug entirely rather than working around it.
`tie_points.resolve_crop_pixels` now calls it instead of `isis_wac.ground_to_image_pixel`/
`resolve_ground_to_image_model` (both kept, still used by `a1`'s own pose-correction work). Live-
validated: all 5 die5 points resolve again on the default candidate. 233 tests pass, lint clean.

Also: two `feature/alignment` merges landed on `main` this session (`a1`'s pose-correction/
`wac_camera_model`/`control_network.py` work), each pulled into this worktree at a clean stopping
point; a real, harmless (confirmed live: byte-identical downstream fit numbers) concurrency race on
`isis_wac.run_isd_generate`'s non-atomic `scratch/isis_wac/` write was found and documented in
`docs/environment.md`'s "Other sharp edges" section, the same class of issue as the already-documented
GLD100 fetch race. `reproject` itself remains not wired into any notebook and not dataset-scale
validated -- still the real remaining work before this branch is done; see
`docs/reproject-fov-investigation.md`'s intro for the current punch list.

## Phase 64 (2026-08-20) — Fixed the die5 near-polar limitation: point selection now works in local meters, not raw lon/lat degrees; `resolve_crop_pixels` raises instead of tolerating drops

Phase 30 had left one residual limitation "accepted, not a bug": `select_tie_points`'s die5
point-selection geometry (`inscribed_bbox`/`intersect_bbox`/`die5_points`) worked entirely in raw
lon/lat degrees, which breaks down near the poles (a degree of longitude covers a rapidly shrinking
real distance there), so `resolve_crop_pixels` tolerated dropped points as an expected edge case.
Revisited at the user's request: "There's no reason why the tie points ever need to fall outside the
intersected FOV of the two images being compared... I think the right fix is to make that work, not
design around the weakness of sometimes messing that up" -- i.e. fix the root cause, not the symptom.

**Fix**: `inscribed_bbox`/`intersect_bbox`/`die5_points` are pure planar-geometry functions with no
lon/lat-specific logic, so `select_tie_points` now projects both footprints into a shared local
Orthographic frame (meters, centered on the synthetic camera's own boresight ground point) before
running them, then projects the resulting 5 points back to lon/lat -- via `rasterio.warp.transform`
(the same real PROJ-backed tool `control_network.map_points_to_lonlat` already used for point-wise
transforms), not a hand-rolled projection formula, per the user's explicit preference for validated
code: "I generally prefer to rely on validated code vs. write new."

With point selection now trustworthy near the poles too, `resolve_crop_pixels` no longer tolerates a
resolution failure -- it raises immediately, naming the failing point, instead of dropping it with a
printed warning. `control_network.resolve_control_points` deliberately keeps its own tolerant-drop
behavior (a real, different case: `cam2map` resampling can genuinely push a many-point matched
control-network pixel just past the original crop's real edge), so only its docstring's now-stale
cross-reference to `resolve_crop_pixels`'s old convention needed updating.

**Also deduplicated**: the `"+proj=longlat +R=... +no_defs"` / `"+proj=ortho +lon_0=... +lat_0=...
+R=... +units=m +no_defs"` PROJ4 string patterns, independently built inline in `lunaserv.py` (4
sites), `craters.py`, `control_network.py`, and `plotting.py`, into two shared functions
(`lunaserv.geographic_crs`/`lunaserv.local_orthographic_crs`) -- per the user's explicit preference:
"I prefer not to redefine the same PROJ frame in multiple places. The issue is not just brevity but
consistency." Every site above, plus `tie_points.py`'s new local-meters helpers, now calls these
instead of building the string itself.

**Found in passing**: the just-landed radius-configurability cleanup (making `MOON_RADIUS_KM`/
`MOON_RADIUS_M` fixed constants, no longer `TrntestConfig` fields) had missed one call site --
`tie_points.resolve_crop_pixels` still read `config.moon_radius_km`, a field that no longer existed,
a live `AttributeError` waiting to happen. Fixed as part of this same pass (dropped the now-redundant
explicit argument; `lonlat_to_ground_km` already defaults to the fixed constant).

Verified: `pytest -q -m "not heavy"` and `trntest-lint` clean inside Docker; new tests cover the
local-meters round trip, a synthetic near-polar regression case (proving die5 points stay inside the
true lon/lat polygon where raw-degree `inscribed_bbox` would not), `resolve_crop_pixels`'s new
raise-immediately behavior, and the two new `lunaserv` CRS-string helpers.

## Phase 65 (2026-08-21) — Wired `reproject` into `image_generation.ipynb` (Phase 8) and added a same-grid render blink comparator

`reproject` (`TrnTestReprojectImage`) had been implemented and FOV-validated for a while (Phase
60-63) but was still explicitly not wired into any notebook -- the last open item on
`docs/reproject-fov-investigation.md`'s own punch list. Picked up this session at the user's
request ("let's work on the reproject generator").

**First pass**: added `reproject` to Phase 2's `dataset.truncate`/`populate` `product_types=`
(still opt-in -- `trn_dataset.PRODUCT_TYPES` itself unchanged), then a new Phase 8 mirroring Phase
5/6's own A/B-style raw-quality-vs-basemap + `mapproject`-overlay-vs-basemap checks. The user
paused this before running it: "make sure to structure it logically so the comparability of the
different generators is clear" -- the as-written Phase 8 just repeated the basemap check a third
time, burying the actual point of `reproject`: it shares `hillshade`'s exact `Camera` (pose, FOV,
pixel grid), so it's the one candidate comparable to `hillshade` **without any reprojection at
all** -- a clean texture-source-only ablation (Lunaserv/Hapke basemap vs. real WAC reflectance,
identical geometry).

**Redesigned**: Phase 8 now has just two things -- a valid-pixel coverage print (the real,
permanent answer to the FOV investigation's own "does the real crop's footprint actually cover the
FOV corners" question: 100.0% on the default candidate), and a direct blink comparison against
`hillshade`. No repeated basemap check -- `reproject`'s geometry is already guaranteed identical to
`hillshade`'s, already validated in Phase 5, so there's nothing new to re-verify there.

New `plotting.plot_render_toggle`: a blink-GIF comparator for two renders that already share one
pixel grid by construction. Deliberately *not* `plot_overlay_toggle` with different inputs --
that function's whole shape (`rioxarray`, geo-registration, footprint outline tracing) exists to
align two rasters that don't share a grid; `hillshade`/`reproject` already do, so all of that is
unneeded machinery. Reuses `_blink_gif_b64` directly (the actual hard-won GIF-encoding mechanism --
shared 256-color palette, `<img src="data:image/gif;...">`, nothing for GitHub's sanitizer to
strip) with a much lighter frame-renderer: plain `read_raster_band` + `np.rot90` + the same
single-multiplicative brightness match `plot_isis_comparison`/`plot_overlay` already use (still
needed despite both sides being `sat_sim` renders -- `reproject`'s real ISIS-calibrated I/F input,
~0.01-0.2, and `hillshade`'s synthetic basemap texture land on very different absolute DN scales).

**Two more rounds of user feedback, both applied**: (1) titles -- shorter, using the
already-established `hillshade`/`reproject` names rather than the long `render_label` strings, with
a `plot_overlay_toggle`-style `☑`/`☐` checkbox glyph marking which one is currently showing
(`"☑ hillshade / ☐ reproject"`, flipped on the other frame) instead of the title swapping wholesale
-- same stable-width convention `plot_overlay_toggle` established, generalized from an on/off
binary to naming which of two candidates is on screen. (2) tie-point markers didn't actually help
read a blink and were dropped from `plot_render_toggle` entirely -- confirmed live, a blink already
shows misalignment directly, unlike the static side-by-side panels the markers help elsewhere in
this notebook. Also renamed the section itself from "8B" (with no surviving "8A" -- an interim
"Phase 8: comparing three candidates, 8A/8B" structure was proposed and rejected before ever being
written to the file) to a plain, unlettered header.

One real self-caught error along the way, worth recording since it easily could have gone
unnoticed: an early edit meant for the new reproject entry landed in the *wrong* section of
`docs/plan.md` (the unrelated camera-pose-alignment/LightGlue bullet) due to a same-named-section
mixup while both were open in the same file; caught by re-reading the diff before moving on,
reverted, and re-applied to the correct location.

Live-validated end-to-end via `scripts/run_notebook.sh notebooks/image_generation.py`, twice (once
per round of feedback) -- both full 36-cell top-to-bottom runs, no errors, exit 0; `trntest-lint`
(ruff format/check, mypy, notebook sync/warnings) clean both times. Still only validated on this one
entry (`M1327210646CE`) through the real `TrnTestReprojectImage` class and the flagship notebook --
dataset-scale validation across the rest of the manifest remains open, not done in this pass.

## Phase 66 (2026-08-21) — Replaced `trn_dataset.py`'s filesystem task queue with `huey` (sqlite)

The user's request: cut the custom concurrency-sensitive bookkeeping in `trn_dataset.py`'s task
queue (`.locks/<product_id>_<product_type>.lock`/`.error`, atomic `os.O_CREAT|O_EXCL` claims --
`docs/dataset-plan.md`'s original "Task queue" design) in favor of `huey`, a small, well-known
Python task-queue library with a sqlite backend, on the reasoning that it needs no extra
infrastructure (no Redis/broker, just a file -- the same "self-contained folder, no extra services"
ethos this project already follows) and hands back better-tested retry/error-storage/consumer
tooling for free. Confirmed with the user up front: this was scoped as a code-quality/robustness
upgrade, not a push for real parallel bulk generation (which this queue was designed to eventually
support but has never actually been exercised for -- every real use so far is
`image_generation.py`'s single-image `dataset.populate(limit=1)`), so `populate()` needed to keep
behaving exactly as before -- one blocking call, no new process to start -- rather than switching to
a persistent multi-worker consumer model.

**New `src/trntest/tasks.py`**: one module-level `huey = SqliteHuey(...)` instance per worktree's
`output_dir` (`output_dir/.huey/tasks.db`, not per-dataset-folder -- `@huey.task()` binds to a fixed
instance at import time, so `output_dir` being this project's existing per-worktree isolation
boundary, per `docs/environment.md`, was the natural queue-identity boundary too, not the dataset
folder). `immediate=True` executes a task synchronously in the calling process the moment it's
enqueued, matching `populate()`'s original behavior with zero new operational steps.
`immediate_use_memory=False` is required alongside it and easy to miss -- huey's own default
silently switches immediate mode to in-memory storage, which would make a stored failure invisible
to a `status()` call from a different process (confirmed empirically with a real subprocess probe
before writing any of the integration code); the real sqlite file has to stay authoritative so a
fresh `docker compose run` can still see a prior run's failure, the same property the old `.error`
files had. `generate_product(image)` is a thin wrapper calling the existing, unchanged
`TrnTestImage.generate()` directly on the real object (not re-opened from disk via
`(dataset_folder, product_id, product_type)` args) -- deliberately not designed for the deferred,
not-yet-built multi-worker `huey_consumer` path's cross-process picklability requirement, since nothing
today actually crosses a process boundary and an earlier draft that *did* reopen from disk broke
every fast, disk-free unit test that constructs a `TrnTestDataSet` directly without calling
`.create()` first.

**`trn_dataset.py`**: deleted `_lock_path`/`_error_path`/`claim_task`/`mark_done`/`mark_failed`/
`claim_next_task`/`clear_lock` outright. `task_state()` collapsed from four states to three (`done`
still wins first, via `image.exists()`; `failed`/`pending` now come from querying
`tasks.huey.result()` for a deterministic task id -- `f"{dataset_folder}::{product_id}::{product_type}"`
-- instead of checking lock/error files; `in_progress` no longer has a meaningful, file-observable
equivalent under the synchronous default and was dropped). `populate()` now enqueues
`tasks.generate_product` per pending task and blocks on its `Result`, catching `TaskException`
instead of a bare `Exception`. **A genuine, real crash-recovery win, not just a lock-file
relocation**: since there's no lock file to leak anymore, a worker killed mid-task leaves nothing
behind to clean up -- the next `populate()` call just re-enqueues based on disk state alone, no
`clear_lock()`-equivalent manual step needed at all.

**One real tradeoff, made explicit rather than silently accepted**: the old design's atomic
`claim_task` was what made running several separate `docker compose run` invocations against the
same dataset folder safe as a way to parallelize -- that's gone. `populate()` is no longer safe to
run from more than one process concurrently against the same folder; both `trn_dataset.py`'s own
module docstring and `docs/dataset-plan.md` now say so explicitly. The deferred, documented (not
built) replacement for real parallel population is a `huey_consumer trntest.tasks.huey -w N -k
process` long-running worker pool -- `-k process`, not thread/greenlet, to preserve this project's
existing rule that spiceypy's process-global state is unsafe to share within one process.

**Two real bugs found and fixed empirically before this could be trusted, both worth recording**:
(1) `populate()`'s first draft called `result.get(blocking=True)` (no `preserve=True`) to read a
just-enqueued task's outcome -- since a plain `.get()` pops the stored result on read, this erased a
failure's own record before `task_state()` ever got a chance to see it, so `status()` right after a
failing `populate()` call incorrectly reported `pending`, not `failed`. Fixed by reading with
`preserve=True` throughout (successes stay preserved too now, harmless -- `task_state()` never
queries huey for the "done" case, disk existence wins first). (2) After that fix, `populate()`
still hung *forever*, but only on a **retried** task (`retry_failed=True`, the same deterministic
task id reused for a second attempt) -- root-caused via a long bisection (isolating pytest vs. a
plain script, trimming the test file down repeatedly, then reading huey's own `_execute` source)
to `generate_product` not returning a value: huey's `_execute` only calls `put_result` for a
successful task when `task_value is not None` (or `store_none=True`, not set here), so a bare
`image.generate()` with no `return` never got a result stored at all, and the second attempt's
blocking `.get()` polled forever for a result that would never arrive. The first (failing) attempt
never surfaced this, since a stored error is unconditional regardless of return value. Fixed by
having `generate_product` `return image.generate()` (already returns `raster_path` for free, not a
workaround). Neither bug was caught by casual single-call testing -- both needed the exact
retry/failure-then-recheck sequence real usage (and the test suite) exercises.

`huey` added to `pyproject.toml`'s `dependencies` (plus a `huey.*` mypy `ignore_missing_imports`
override, no stubs published). `tests/test_trn_dataset.py`: removed the tests that were purely
about the deleted lock-file primitives; rewrote the task-state tests to exercise `pending`/`failed`/
`done` through the real `populate()`/`task_state()` path instead of poking file state directly; kept
`populate()`/`truncate()`/`limit`/`retry_failed` tests' original intent, now exercising the
huey-backed path; added `test_failed_task_state_survives_a_fresh_process`, a real subprocess-based
regression test for the `immediate_use_memory=False` requirement. Full suite (241 passed, 3 heavy
deselected) and `trntest-lint --all` both clean. Live-validated via
`scripts/run_notebook.sh notebooks/image_generation.py`.

## Phase 67 (2026-08-21) — Added `populate_via_workers()`, a real multi-worker equivalent of `populate()`

Direct follow-on to Phase 66's huey migration, same day: the user wanted to think through actually
running a big batch job with a worker pool, framed as "an option to switch out the `populate()` call
with an equivalent call (including the `limit`) that is routed through huey." Two things had to be
worked out before writing any code, both confirmed via huey's own source/docs rather than assumed:
(1) huey's `Consumer.start()` explicitly raises `ConfigurationError` against an `immediate=True`
instance, and isn't safely embeddable in a background thread either (it registers OS signal
handlers, which only works on a process's main thread) -- so real worker-pool execution needs a
genuinely separate OS process, not just "the same call but async under the hood". (2) `immediate` is
fixed on a `Huey` instance at construction time, and `populate()`'s existing `tasks.huey` needed to
stay `immediate=True` (untouched, no workflow change for the existing single-image demo notebook) --
so real parallelism needed a **second** `Huey` instance, not a mode switch on the first. Presented
this as a recommendation with the tradeoff (the new call spawning/managing its own `huey_consumer`
subprocess vs. requiring the user to start one externally first) via `AskUserQuestion`; the user
picked the auto-managed, one-call option.

**`src/trntest/tasks.py`**: added `huey_parallel` (`tasks_parallel.db`, `immediate=False`) alongside
the existing `huey`; both now share one `_generate(image)` helper (still must return a non-`None`
value, same reasoning as Phase 66) behind two thin `@task()` wrappers, `generate_product`/
`generate_product_parallel`. `generate_product_parallel` takes the real `TrnTestImage` object
directly, same as `generate_product` -- confirmed empirically (not just assumed) that a real
`TrnTestCropImage` instance pickles cleanly and round-trips correctly through an actual `-k process`
worker subprocess, since unlike `generate_product` this one genuinely crosses a process boundary.
New `start_consumer(workers, env=None)`/`stop_consumer(proc, timeout=10.0)`: spawn/manage a real
`huey_consumer trntest.tasks.huey_parallel -w N -k process` subprocess (`-k process`, not
thread/greenlet, preserving this project's existing rule that spiceypy's process-global state is
unsafe to share *within* one process -- each worker process still only runs one task at a time,
sequentially, same as `populate()`'s own single process always has), output redirected to
`<output_dir>/.huey/consumer.log`, `stop_consumer` SIGTERM-then-SIGKILL. `env` exists so
`tests/test_trn_dataset.py`'s own real-subprocess test can point the consumer's `PYTHONPATH` at a
SPICE/ASP/ISIS-free picklable test task, not needed by real callers.

**`src/trntest/trn_dataset.py`**: new `TrnTestDataSet.populate_via_workers(product_types,
retry_failed, limit, workers=4)` -- same signature and `limit`/`retry_failed` semantics as
`populate()`, but enqueues into `huey_parallel` and blocks on results only after starting the
consumer subprocess (torn down in a `finally`, even on an interrupted/failed batch -- though any
tasks the consumer had already claimed keep running to completion in their own worker processes
regardless, huey's own `SIGTERM` handling, not this method's). Extracted the shared "enqueue every
still-pending task up to `limit`, return the `Result` handles" loop into a new module-level
`_enqueue_pending()` (also fixed a ruff `PLR0912` too-many-branches complaint on the first draft) and
the "block on one `Result`, catch `TaskException`" bit into `_await_result()` -- both now shared by
`populate()` and `populate_via_workers()`, a net simplification, not just new surface area.
`task_state()`/`status()`/`_clear_stored_result()` gained a `huey_instance` parameter (default
`tasks.huey`, unchanged behavior for every existing caller) so the two queues' state can be
inspected/cleared independently -- `truncate()` now clears a stored result from *both* queues
unconditionally, since a task's most recent attempt could have gone through either method.

**A real, deliberate asymmetry, not an oversight**: `populate_via_workers()`'s own failures are
invisible to a plain `status()` call (which only checks `tasks.huey`) unless the caller passes
`huey_instance=tasks.huey_parallel` explicitly -- the two queues are genuinely independent by
design, not merged into one unified view. Documented in both methods' docstrings and `tasks.py`'s
module docstring rather than silently left as a surprise.

**Testing**: the class of bug this project's huey work keeps finding lives at real process/thread
boundaries, so testing leaned into that rather than mocking around it. Fast, in-process tests for
`populate_via_workers()`'s own control flow (done/failed/retry/limit/queue-separation, ~10 new
tests) flip `tasks.huey_parallel.immediate` to `True` (huey's own documented pattern for testing
without a consumer) and no-op `start_consumer`/`stop_consumer`, avoiding a real subprocess for logic
that doesn't need one. Separately, a genuinely real `-k process` subprocess test
(`test_generate_product_parallel_runs_in_a_real_worker_subprocess`,
`..._failure_visible_via_huey_parallel_result`, `test_start_stop_consumer_lifecycle`) exercises the
actual cross-process path, using a new `tests/_fake_worker_task.py` (a plain, picklable, SPICE-free
`FakeWorkerTask`/`FailingWorkerTask` pair, deliberately its own top-level-importable module -- a
class defined inside `test_trn_dataset.py` itself would need `tests/` *and* `trntest`'s own heavy
dependency stack importable from the fresh worker subprocess, defeating the point). Full suite: 251
passed (up from 241), 3 heavy deselected, ~18s; `trntest-lint --all` clean.

**Live-validated against real manifest entries, not just fakes**: a throwaway `TrnTestDataSet`
against the real `notebooks/dataset_manifest.csv` (81 entries, only entry 0 previously generated by
the flagship notebook), `populate_via_workers(limit=2, workers=2)` against two never-before-generated
entries -- both real crop cubes (`isis_wac`/ISIS pipeline) and hillshade renders (`sat_sim`, real
Lunaserv/Astropedia DEM fetches) completed successfully in 53.4s total for both entries together,
each through its own separate OS process, real SPICE/network calls included. Validation dataset
folder deleted afterward -- scratch, not part of the committed demo.

## Phase 68 (2026-08-22) — Fixed a real curvature (sagitta) omission in the Hapke shading's emission/phase geometry

User-requested audit of Phase 45-47's Hapke emission/phase angle math (`lunaserv._terrain_photometric_angles`), prompted by the hillshade and real WAC crop looking photometrically different despite aligning geometrically -- specifically checking whether an idealized-body assumption was sneaking in somewhere instead of real DEM terrain.

**Found**: `_terrain_photometric_angles` builds each pixel's 3D ground position as `[x_grid, y_grid, dem]`, where `x_grid`/`y_grid` come from the local orthographic projection (`orthographic_xy_m`'s own exact East/North tangent-plane coordinates of the on-sphere point) and `dem` is elevation above the reference sphere. An orthographic projection drops the vertical component by construction, so reusing `dem` directly as the tangent-plane "Up" coordinate silently omitted the sphere's own curvature drop-off away from the tangent point (the sagitta term, `R - sqrt(R^2 - x^2 - y^2)`). `tests/test_lunaserv.py`'s own on-sphere-point test for `_camera_local_enu_m` already computed this exact term and called it negligible -- but only at the ~6.7km offset that test used (~13m sag); real footprints are much bigger. Not an "ellipsoid instead of DEM" bug in the literal sense the user's question proposed -- the DEM's real relief genuinely drives the surface normal (and therefore incidence, unaffected by any of this) -- but a related, unintentional flat-*plane* assumption baked into the emission/phase view-vector math specifically.

Incidence angle doesn't depend on ground position at all (only the DEM-derived normal and the parallel-ray sun direction), so it's exact/unaffected either way. Emission and phase do, via the camera-to-ground view vector.

**Quantified two ways before touching any code**: (1) analytically, using this project's own real, live-validated 143.1x142.6km footprint (`docs/reproject-fov-investigation.md`) and a real ~68.5km altitude value (this doc, Phase-scale reference elsewhere) -- ~0.6 deg bias at the frame's mid-edges, ~1.1 deg at the corners; (2) empirically, in a throwaway notebook (`notebooks/curvature_sag_investigation.py`/`.ipynb`, deleted once the fix landed -- git history has it if needed) against the real default candidate `M1327210646CE`'s own DEM/footprint: incidence bias exactly 0.0 deg as predicted, emission bias averaging 0.58 deg (max 2.40 deg), phase bias averaging 0.33 deg (max 2.08 deg). The notebook also ran the real ISIS `photomet` Hapke model on both angle sets and diffed the blended basemap against the real WAC crop (`isis_wac.run_cam2map_for_crop`, same brightness-matched-diff methodology `along_track_correction.ipynb` uses): mean|diff| improved slightly with the correction (6.97 -> 6.82) -- modest, confirming the right direction without being the dominant source of the photometric mismatch the user originally noticed (the still-uncalibrated `_HAPKE_PLACEHOLDER_PARAMS`, investigated separately the same session, is likely the bigger contributor there).

**Fix**: `_terrain_photometric_angles` gained a required `radius_m` parameter; `ground`'s "Up" coordinate is now `dem + sphere_sag`, where `sphere_sag = sqrt(radius_m**2 - x_grid**2 - y_grid**2) - radius_m` -- the exact closed form, not a small-angle approximation, so it stays correct at any real footprint size. `hapke_shade_ortho` passes `MOON_RADIUS_M`. Two existing tests (`..._emission_grows_with_offset_from_nadir`, `..._along_track_correction_removes_along_track_component`) needed their tolerance loosened from `rel=1e-6` to `abs=1e-4` -- their small (~100m) test grids now have a real, if tiny (~1.2e-5 deg), curvature-induced deviation from the old exact-flat-ground formula they compare against. New test `test_terrain_photometric_angles_curvature_correction_reduces_emission_at_large_offset` validates the correction at realistic (~100km) scale against an independently-written closed-form expected value (not a call into the fixed code itself).

**A real, separate, still-open gap found while writing this fix, deliberately not folded in**: `normal` (the surface-normal-from-DEM-gradient calculation, feeding both incidence and emission) is still built entirely in the tangent point's own fixed (East, North, Up) frame -- the same flat-DEM convention `LightSource.hillshade` uses, reasonable for a small terrestrial DEM tile but not necessarily for a whole lunar image. A ground point at real angular offset theta from the tangent point has a true local vertical tilted by theta from the tangent point's own -- comparable in magnitude to the sagitta effect above. So incidence (previously described as "exact, unaffected by any of this") is only unaffected *by this specific fix*; it likely still carries its own separate, unaddressed, similarly-sized bias from the normal never rotating with position. Flagged in `_terrain_photometric_angles`'s own docstring as a known remaining gap, not investigated or validated -- a candidate for a future pass if the user wants to pursue it, would need a per-pixel rotated local frame rather than a single Z-coordinate correction.

Verified: full test suite (252 passed, 3 heavy deselected) and `trntest-lint` clean; real Docker re-run of `image_generation.ipynb` end to end, no errors, reproject valid-pixel-fraction unchanged at 100.0% (confirms geometry/alignment, which never depended on this fix, is genuinely untouched -- only the Hapke-shaded basemap's own pixel values change).

## Phase 69 (2026-08-22) — Made real, ISIS-calibration-sourced Hapke parameters the default, replacing the illustrative placeholder

Direct follow-on to Phase 68's angle-geometry audit, same day: asked to also research whether `_HAPKE_PLACEHOLDER_PARAMS` (explicitly documented as "illustrative... not calibrated against real lunar photometry") could be replaced with something real.

**Research, not guessed**: Sato et al. (2014), *"Resolved Hapke parameter maps of the Moon,"* JGR Planets 119, derived spatially-resolved Hapke parameters from ~21 months of real LROC WAC observations -- the modern reference for lunar Hapke photometry, at the same WAC 7-band wavelengths (321-689nm) this project already fetches other layers from. Better than re-deriving numbers from the paper: ISIS itself ships that exact calibration, already converted into its own native `Wh`/`Hg1`/`Hg2`/`Bc0`/`hc`/`B0`/`Hh`/`Theta`/`phi` parameterization, as part of the `lro` ISIS data package `isis_wac.ensure_isisdata` already fetches for `lrowaccal`/`spiceinit` -- confirmed present in this project's own Docker image, not a new dependency: `$ISISDATA/lro/calibration/WAC_global_7bands_1x1_wbhs70NS_const_each_pole.cub`, a real, spatially-resolved 1deg/px (~30km) global cube, 7 wavelength bands x 9 parameters each. Sampled directly against the real default candidate's own footprint center (169.58E, 38.77N, 643nm to match `config.lunaserv_ortho_layer`'s own wavelength) and compared to the placeholder:

| param | placeholder | real | global range |
|---|---|---|---|
| `wh` | 0.52 | 0.393 | 0.17-0.65 |
| `hg1` | 0.213 | 0.224 | 0.16-0.30 |
| `hg2` | 1.0 | 0.470 | -0.25-1.20 |
| `hh` | 0.17 | 0.045 | 0.0-0.2 |
| `b0` | 0.025 | 1.650 | 1.47-2.21 |
| `theta` (deg) | 0.0 | 23.66 | constant globally |

`b0` (opposition surge amplitude) was ~66x too small in the placeholder -- the Moon is one of the strongest-opposition-surge bodies in the solar system, and the placeholder modeled almost none of it. `theta` (macroscopic roughness) was 0 -- a perfectly smooth surface -- vs. a real, well-established ~23.4-23.7 deg global constant (matches Sato's own reported 23.4 deg closely). `hg2` was off by roughly 2x in the wrong direction. `hg1`/`wh` were already in a reasonable ballpark.

**A real, non-obvious mapping had to be verified, not assumed**: ISIS's `hg1`/`hg2` aren't literally McEwen's `(b, c)` double-Henyey-Greenstein parameters Sato et al. report. Confirmed against ISIS's own documented `hg1`/`hg2` value ranges and a documented equivalence note (`(hg1, hg2=1.0)` == `(-hg1, hg2=0.0)`) that the mapping is `hg1 = b`, `hg2 = (1+c)/2` -- getting this wrong (e.g. plugging Sato's raw `c` straight into `hg2`) would have silently produced a wrong phase function shape.

**Implementation** (`src/trntest/lunaserv.py`): new `fetch_real_hapke_params(lon, lat, config, wavelength_nm=643)` -- resolves the calibration cube's path (`_hapke_calibration_cube_path`, `isis_wac.ensure_isisdata` + a version-glob, highest-numbered match, the same "don't assume a specific version" discipline this project already applies elsewhere; a local import to avoid a real circular dependency with `isis_wac.py`, `# noqa: PLC0415`, the same pattern `camera.py`/`spice_kernels.py` already use), then samples all 9 parameters at one real ground point (`_sample_hapke_calibration`, split out specifically so it's unit-testable against a small synthetic fixture without a real `$ISISDATA`/network). New `real_hapke_params: bool` parameter threaded through `hapke_shade_ortho`/`despeckle_and_shade_ortho`/`fetch_dem_and_ortho`/`ortho_shaded_filename`, mirroring exactly how `along_track_correction` was added in Phase 47 -- its own filename suffix (`_realparams`) so `TrnTestEntry.dem_ortho_result`'s resumption check can never confuse a placeholder-shaded cached file for a real-calibration one. One real bug caught while writing this: the boolean parameter name `real_hapke_params` initially collided with the module-level function of the same name, which would have silently called a bool instead of the lookup function -- caught immediately (the parameter shadows the function inside `hapke_shade_ortho`'s own body), fixed by renaming the function to `fetch_real_hapke_params`.

A single value per image (the footprint's own center), not per-pixel -- checked directly against the real cube: within one real ~143km candidate footprint, `wh`/`b0`/`hg1` vary only a few percent of their own full-Moon range; `hg2`/`hh` vary somewhat more but still secondary next to the placeholder-vs-real gap above. Per-pixel sampling (reprojecting the calibration cube the way `reproject_astropedia_elevation_to_local_grid` does for the DEM) would be a real further refinement, not implemented here -- recorded as an open item.

**Validated in a notebook before deciding, then made default on request**: `notebooks/real_hapke_params.ipynb` blinks the placeholder against the real-calibration basemap (same `plot_overlay_toggle` pattern as `hapke_hillshade.ipynb`) and diffs both against the real WAC crop (`along_track_correction.ipynb`'s brightness-matched-diff methodology). First pass showed mean|diff| improving 6.95 -> 6.89 vs. the real crop, but that comparison turned out to be contaminated by a stale, differently-gridded cached file on one side (a real, if minor, bug in the notebook's own comparison helper -- fixed by re-fetching both variants with matching `extra_footprint_lonlat_deg` and reading the crop window via `out_shape`-resampling instead of assuming pixel-identical grids). Recomputed on two genuinely matching grids: mean|diff| 6.8942 (placeholder) vs. 6.8920 (real) -- a real but much smaller improvement than first reported, well within the noise of this coarse whole-frame metric; the more informative signal is the blink comparison's visible emission-angle-dependent shading pattern difference, not this single number. Given the improvement's direction was still consistently favorable (matching Phase 68's own sagitta-fix finding) and the placeholder's own gap from real calibrated values is large and well-documented, the user asked to wire it in as the default rather than leave it opt-in.

`DEFAULT_REAL_HAPKE_PARAMS` flipped `True`; `real_hapke_params=False` kept as an explicit fallback (never deleted, matches `hapke=False`/`along_track_correction=False`'s own precedent). `notebooks/real_hapke_params.ipynb` kept as a permanent reference/regression comparison (not deleted like Phase 68's throwaway investigation notebook -- unlike that pure geometry bug fix, there's a legitimate reason to keep comparing against the placeholder, since Hapke calibration accuracy is inherently more open-ended than a closed-form geometry correction), reframed from "should we do this" to "reference comparison against the current default," the same edit Phase 46 made to `hapke_hillshade.ipynb` when Hapke shading itself became the default.

**Testing**: 8 new tests, all pure-Python/synthetic-fixture (no ISIS/network) -- `ortho_shaded_filename`'s new suffix behavior (including an explicit backward-compat check that pre-existing defaults' filenames are unchanged) and `_sample_hapke_calibration`'s band/pixel-selection logic against a small synthetic multi-band GeoTIFF fixture in the calibration cube's own CRS/band-layout convention (each band's value encodes `wavelength_index * 100 + param_index`, so a wrong band/pixel choice is easy to catch). `fetch_real_hapke_params`/`_hapke_calibration_cube_path` themselves are integration-validated live (real `$ISISDATA`, real candidate) rather than unit tested, the same "geometry math gets a fast unit test, the ISIS-subprocess/data-area-dependent parts get a real Docker validation" split this file already follows for `hapke_shade_ortho` itself.

Verified: full test suite (265 passed, 3 heavy deselected, one pre-existing unrelated flake in a real-subprocess huey consumer test confirmed to fail identically against the pre-Phase-69 commit too, not caused by this change) and `trntest-lint` clean; real Docker re-run of `image_generation.ipynb` end to end with the new default, no errors.

## Phase 70 (2026-08-22) — Investigated (and shelved) the normal-tilt fix and an ISIS `phocube`-based replacement for `_terrain_photometric_angles`

Direct follow-on to Phase 68's own flagged "known remaining gap" (the DEM-gradient surface normal never rotates for a pixel's true local vertical away from the tangent point), same day.

**The normal-tilt fix itself: derived, verified correct, but not adopted.** A one-line generalization of Phase 68's own sagitta idea: use `dem + sphere_sag` (not raw `dem`) as the gradient input for `normal`, not just for `ground`'s position -- algebraically exact (the cross product of `ground`'s own two numerically-differentiated tangent vectors expands to precisely this formula), and verified directly against a synthetic flat sphere (computed normal converges to the true tilt angle as grid resolution increases, residual ~0.0017 deg at production ~100m/px resolution vs. a ~3.3 deg true tilt at the test offset). On the real default candidate: incidence bias mean 1.84 deg / max 5.67 deg (first-ever nonzero incidence bias -- previously exact), emission bias mean 2.16 deg, *systematically* signed -- confirmed via a linear-plane fit that 98.3% of the emission bias field is explained by a single gradient whose azimuth (228.1 deg) matches the scene's real sun azimuth (227.8 deg) to within 0.3 deg, the expected large-scale photometric-gradient signature of a curved body under directional lighting, not an artifact.

Despite this, it made the brightness-matched diff against the real WAC crop measurably *worse* (6.89 -> 7.58 mean|diff|, and still worse, just less so, with the old placeholder Hapke params: 6.89 -> 7.23 -- ruling out "real params' strong opposition surge amplifies noise" as the whole explanation, though it is part of it). No pixels crossed a 90 deg incidence/emission threshold (ruled out a discontinuity artifact). Leading hypothesis, not verified: `config.lunaserv_ortho_layer` (`luna_wac_normalized_reflectance`) is itself a pre-existing "Normalized Reflectance" composite -- our from-scratch Hapke re-shading was never validated against whatever geometric assumptions that prior processing already baked in, so a more physically correct angle *input* on our side doesn't guarantee a more correct combined *output*.

**A real methodology lesson, not just a null result**: the user initially read the notebook's blink comparison as "a huge improvement, much closer visually to the real WAC image" -- prompting a plan to wire the fix in. On mentioning a visible SW-NE brightness gradient, that gradient was confirmed (via the same linear-plane-fit technique above) to align with the real sun azimuth to within 0.3 deg -- seemingly further confirming the fix. The user then realized they had actually been comparing the *corrected-vs-uncorrected* blink toggle (both synthetic, no real WAC image in either panel), not a corrected-render-vs-real-WAC-image comparison as intended, and retracted the "huge improvement" read. This is the reason the notebook (deleted once this phase's findings were folded in, alongside `docs/history.md`'s own record here) added an explicit 2x2 controlled comparison (placeholder x real-Hapke-params, flat-frame x normal-tilt-corrected) with individually-labeled blink toggles for each isolated pairwise comparison, specifically so a visual read can't accidentally conflate two different changes -- a real, reusable lesson for any future comparison notebook in this project, not just this one.

**A cleaner alternative was investigated next: does ISIS's own `phocube` already do this correctly, sidestepping hand-rolled tangent-plane math entirely?** User's own research question, not assumed. Confirmed via ISIS documentation and, more importantly, live testing (not just docs) that `phocube` computes real per-pixel photometric backplanes from a cube with a real camera model (+ optionally a real shape model) attached -- exactly this project's own goal. The catch matches Phase 45's own original finding: it needs a real camera model attached, which the flat WMS-mosaic ortho basemap this function shades doesn't have -- but the *rendered* image does, via the CSM state `render.run_sat_sim`'s `cam_gen` step already produces for every render (not a new artifact).

Live-tested end to end against the real default candidate's own render: `csminit` (needed `usgscsm` installed into the `isis` conda env via `micromamba install -n isis -c conda-forge usgscsm` -- not present by default in this project's Docker image, not added to the Dockerfile since this path wasn't adopted, so this was a transient, container-local change only) successfully attached the render's own real CSM state to a `gdal_translate -of ISIS3`-converted cube. `phocube`'s ellipsoid-based angles (`incidence`/`emission`/`phase`/`latitude`/`longitude`) then came back fully sane and geometrically correct (lat/lon matched the real candidate's known footprint; emission 0.1-43 deg, a plausible near-nadir range) -- **after** one real fix: `cam_gen`'s CSM conversion doesn't populate `m_sunPosition` (ASP's own tools never need real sun geometry), so incidence/phase were initially degenerate (constant 180 deg / near-180 deg) until the real sun position (`spice.spkpos("SUN", et, "MOON_ME", "NONE", "MOON")`, the same call `illumination.py` already makes) was patched into the CSM state JSON before `csminit`.

**DEM-aware ("local incidence/emission") did not work.** `csminit shapemodel=<this project's already-cached global 128ppd lunar shape model>` attached successfully (a real ISIS-native map-projected shape cube is required -- confirmed empirically that a plain GDAL-ISIS3-converted GeoTIFF with an Orthographic PROJ4 CRS is rejected, "not map-projected," since GDAL's ISIS3 writer doesn't populate a real ISIS `Mapping` label group from an arbitrary PROJ4 string). `phocube localincidence=true` then returned a degenerate ~145-180 deg local incidence almost everywhere on a real, decently-illuminated candidate scene -- implausible, and inconsistent with `localemission` (same local normal, same call) looking correct, and with ellipsoid incidence (same sun position, no DEM) also looking correct, ruling out a sun-position or flipped-normal-everywhere explanation. A real, independently-found ISIS issue (DOI-USGS/ISIS3#3645, "Phocube missing essential output options: local phase angle, slope, slope azimuth") suggests `phocube`'s DEM-aware path is generally less mature than its ellipsoid path -- consistent with, though not proof of, what was found here. Not root-caused further (didn't try this project's own higher-resolution local DEM, reformatted as a real ISIS shape cube, as the shape model instead of the coarse global one -- a real, not-yet-tried variable). Shelved rather than pursued further, on the reasoning that the ellipsoid-only case alone wouldn't actually close the gap this investigation exists to close.

**A real, separate architectural implication, whether or not `phocube` itself is ever adopted**: using ISIS's own camera-model-aware geometry this way would require inverting this project's current shade-before-render pipeline order (`hapke_shade_ortho` shades the ortho basemap *before* `sat_sim` geometrically reprojects it, since `sat_sim` applies no illumination model of its own) to render-then-shade (attach a real camera model to the already-rendered geometry, then compute angles/shade directly on it) -- a real, separate redesign, not a drop-in replacement for `_terrain_photometric_angles`, even if the DEM-aware gap gets resolved later.

**A real, unrelated operational incident surfaced mid-investigation**: the shared VPS disk filled to 100% (98G/98G) partway through this work, causing a real `sqlite3.OperationalError: disk I/O error` when trying to generate the render needed for the `phocube` test. Root cause: this investigation's own accumulated throwaway diagnostic `.cub`/`.tif` scratch files (~1.2GB, from ad hoc one-off Python probes, not the notebooks' own committed outputs) plus ~15GB of reclaimable Docker build-layer cache (from this session's own repeated container rebuilds) plus (found and fixed by the user directly, not this session) a large `scratch/isis_wac/` directory of regenerable ISIS WAC-pipeline intermediates. An initial wrong guess (that `TrnTestDataSet.truncate()` was somehow responsible for clearing per-entry DEM/ortho cache and forcing an expensive ~10GB Astropedia GLD100 re-fetch) was made without checking the code first and had to be corrected once actually read -- `truncate()`'s own docstring explicitly preserves `_work/<edr_product>/` intermediates; the real cause of that particular candidate's missing per-entry cache was never conclusively identified. No code changes resulted from this incident; recorded here only because it consumed real investigation time and is a reminder to verify a specific code-behavior claim before stating it, even one that sounds plausible.

**Outcome**: neither the normal-tilt fix nor the `phocube` replacement is wired in. `_terrain_photometric_angles`'s own docstring (`src/trntest/lunaserv.py`) carries the full rationale for both (the derivation, the empirical non-improvement, and the `phocube` DEM-aware failure) directly, so a future session doesn't have to re-derive or re-attempt either without knowing they were already tried. No notebook kept for this phase (unlike Phase 69's `real_hapke_params.ipynb`) -- neither result is a live, ongoing comparison mode worth a permanent reference notebook, matching Phase 68's own "delete once folded into docs" precedent for a non-adopted finding.

## Phase 71 (2026-08-22) — Wired in Phase 70's normal-tilt fix as the new default, opt-out

Direct follow-on to Phase 70, same day, different session (`a1`). User's own call, after re-reading the actual code (not just the docstring) to check a specific worry -- double-counting the curvature correction between `ground`'s position and `normal`'s gradient input -- and not finding one: `ground`'s z is already `dem + sphere_sag` (exact, since an orthographic projection's `(x, y)` *is* the sphere point's East/North component by construction), and the fix just makes `normal`'s gradient use that same height function instead of a different, incomplete one (previously omitting `sphere_sag` entirely). It fixes a real inconsistency between the two, not a double application of the same correction.

**`normal_tilt_correction` is now a real parameter on `_terrain_photometric_angles`/`hapke_shade_ortho`/`despeckle_and_shade_ortho`/`fetch_dem_and_ortho` (plus a new `DEFAULT_NORMAL_TILT_CORRECTION` module constant and `ortho_shaded_filename` suffix, `_normaltilt`), defaulting `True` -- opt-out, not opt-in.** This is a deliberate departure from this project's own established convention (every other shading toggle -- `along_track_correction`, `real_hapke_params`, `hapke` itself -- defaults on only once it's shown to improve the real-WAC-crop match): the geometry here is believed correct on its own terms, and the old, uncorrected formula isn't something new code should have to opt out of just because its interaction with the real image is still unexplained (Phase 70's 6.89 -> 7.58 mean|diff| regression, still unresolved). `normal_tilt_correction=False` remains only as a temporary comparison/fallback mode, expected to be deleted (parameter and all) once that regression is separately understood or the fix is fully retired from doubt -- not a permanent third option like the other toggles. `hapke_shade_ortho`'s actual default rendered output changes starting now, including for the real default candidate, in exactly the way Phase 70 already characterized.

**Testing**: `_terrain_photometric_angles`'s three pre-existing flat-terrain tests (`..._flat_dem_directly_below_camera`, `..._emission_grows_with_offset_from_nadir`, `..._curvature_correction_reduces_emission_at_large_offset`, plus the along-track-correction test) all silently broke when `normal_tilt_correction` defaulted to `True` -- each assumed a constant, untilted `[0,0,1]` normal as an unstated premise for isolating some *other* effect (nadir alignment, parallax, the sagitta/ground-position correction, along-track removal). Fixed by passing `normal_tilt_correction=False` explicitly in each, rather than updating their expected values -- keeps each test isolating only the one thing it exists to test. Two new tests added: a closed-form check (independently derived, not a second call into `lunaserv`) that incidence at a real angular offset matches `(90 - elevation_deg) - theta_true_deg` under a sun-aligned-with-offset geometry chosen to keep the vector algebra coplanar (`abs=0.02` tolerance -- observed residual ~0.016 deg at this test's ~1km/px grid, `np.gradient`'s central-difference discretization error, consistent with though not identical to the docstring's own ~0.0017 deg figure at a finer ~100m/px resolution used during Phase 70's own validation); and an explicit check that `normal_tilt_correction=False` still reproduces the old flat-normal behavior exactly. Six new `ortho_shaded_filename` tests cover the new `_normaltilt` suffix and its backward-compat filename (`normal_tilt_correction=False` must still resolve to the exact pre-Phase-71 filename real cached files may already exist under). Full suite: 262 passed, 3 heavy deselected, the same one pre-existing unrelated worker-subprocess flake from Phase 69 (confirmed again, live, to fail identically against the pre-this-change commit) -- and `trntest-lint` clean.

**Independent validation against real ISIS ground truth (the user's own ask, to build confidence beyond re-reading the code) -- done, and passes.** `tests/test_lunaserv_campt_validation.py` (new, permanent `@pytest.mark.heavy` test -- this project's first to actually shell out through real ISIS binaries rather than mock them, needing `usgscsm` added to `docker/Dockerfile`'s `isis` conda env, permanently this time, not the transient container-local install Phase 70 used) compares `_terrain_photometric_angles(dem=zeros, ...)` (the ellipsoid limit) against real `campt` output at 5 sparse sample points (die's-5 pattern, inscribed in the real candidate's own camera footprint -- `tie_points.die5_points`/`inscribed_bbox`, the same pattern `select_tie_points` already uses), using that candidate's own real CSM camera (`render.run_sat_sim`'s `cam_gen` output, `csminit`-attached, real sun position patched into the CSM state via a new reusable `render.patch_sun_position`). `campt` superseded `phocube` for this specifically: it reports phase/incidence/emission in one point query (no raster/pixel-grid-alignment needed against a render, and no structural phase gap the way `phocube` has, per DOI-USGS/ISIS3#3645), and is already extensively validated elsewhere in this project.

Two real, live-discovered mechanics gaps had to be fixed before this worked, beyond Phase 70's own `m_sunPosition` patch: (1) **`csminit`'s `isd=` parameter is the wrong one** -- it wants a from-scratch ISD (ALE's own format, `isis_wac.run_isd_generate`'s), and fails ("Could not parse the sensor model name") on a `cam_gen`-style pre-built CSM model *state* string even once it's valid JSON; `csminit`'s separate, documented `state=` parameter is the one that wants exactly `cam_gen`/`read_csm_state`'s own "bare model-name line + JSON" format, unmodified. (2) **The real `campt` PVL field names are `Phase`/`Incidence`/`Emission`, not `Phase Angle`/`Incidence Angle`/`Emission Angle`** as ISIS's own prose documentation (fetched during planning) suggested -- confirmed by a live dump of real `campt` output, not assumed. Also found live: die5 sample points must be inscribed in the *camera's own real footprint*, not the DEM/ortho fetch's own deliberately-larger padded AOI -- an initial attempt using the padded bbox picked points genuinely outside the render's own FOV, which real `campt` correctly refused to project (`allowoutside=false`).

Result: **max |diff| ~0.018 deg across 15 angle comparisons** (5 points x phase/incidence/emission) -- well within the expected residual budget (the synthetic-sphere numerical-gradient error already characterized above, plus treating the sun as one scene-wide direction rather than a true per-point vector), confirming the normal-tilt fix's geometry is correct via a completely independent tool, and specifically ruling out double-counting the curvature correction (the concern that prompted this validation in the first place) as an explanation for the still-unresolved real-WAC-image regression.

**Follow-up: does `campt` extend to the real DEM-aware case, the way `phocube`'s broken `localincidence` flag was supposed to? No -- clean negative result.** Reused the exact same fixture (same CSM-attached cube, same 5 sample points) with the cached global 128ppd lunar shape model attached via `csminit shapemodel=...` (the same one Phase 70's `phocube localincidence` test used, and `isis_wac.ensure_lunar_shape_model` fetched fresh here, ~2GB one-time), and compared `campt`'s `Incidence`/`Emission` there against the ellipsoid-mode values above at the same 5 points, as a one-off diagnostic script (not a permanent test -- no known-correct DEM-aware value to assert against yet, matching this plan's own "exploratory, not fixed-assertion" framing; deleted once this result was recorded, matching Phase 68/70's own precedent for non-adopted findings).

Result: **`Incidence` is byte-identical (0.0000 deg diff) at all 5 points, with or without the shape model attached; `Emission` shifts by a small but real 0.15-0.57 deg.** This is a clean, informative pattern, not noise: `Incidence` depends only on the surface normal and sun direction, with no view-vector/ground-position term at all, so its exact-zero difference means the *normal* `campt` uses is unaffected by the attached shape model -- still the ellipsoid's own normal, not a real local terrain-tilted one. `Emission` depends on the normal *and* the sensor-to-ground view vector, and *that* vector does shift slightly once ray-DEM intersection uses the shape model's real local elevation instead of the ideal ellipsoid radius -- explaining `Emission`'s small nonzero shift without needing any change in the normal itself. So `campt`'s plain angle output stays firmly ellipsoid-normal-based even with a real DEM shape model attached -- it has no working equivalent to `phocube`'s (broken) `localincidence`/`localemission`, confirming the suspicion `photomet`'s own `ANGLESOURCE=ELLIPSOID`/`DEM` documentation raised during planning. **Idea 2 from the original 3-idea discussion this whole investigation started from (validating our DEM-aware channels against phocube's own DEM-mode output) still has no working ISIS ground-truth tool to lean on** -- that gap remains open, unlike idea 1 (ellipsoid validation), which this phase closed.

**Outcome**: `normal_tilt_correction` is wired in as the default (opt-out `False` fallback kept temporarily), independently validated against real ISIS ground truth at the ellipsoid limit, and confirmed to close the double-counting worry that motivated the validation. The real-WAC-image regression it causes (Phase 70) remains open and unexplained -- a distinct question from "is the geometry correct," which this phase answers. The DEM-aware case (this project's actual production use, and the harder question Phase 70 originally set out to answer) still has no independent ISIS ground-truth check available -- `phocube`'s own `localincidence` stays broken (Phase 70), and `campt`'s plain angles now confirmed not to reflect real local terrain tilt either. `render.patch_sun_position`/`isis_wac.campt_photometric_angles` are real, reusable, permanent additions (not one-off notebook code) for any future ISIS-ground-truth validation work against a synthetic render's own CSM camera.

## Phase 72 (2026-08-22) — Hapke-ratio relighting fix; both photometric corrections made unconditional; reusable brightness-matched-diff tool

Same day, same session (`a1`), direct continuation of Phase 71's still-open real-WAC-image regression.

**New finding: `config.lunaserv_ortho_layer` (`luna_wac_normalized_reflectance`) isn't raw albedo.** The user identified it as ASU/LROC's WAC_EMP product (PDS4 `LROLRC_2001/DATA/MDR/WAC_EMP`, README: https://pds.mcp.nasa.gov/data/store/img/lunar_reconnaissance_orbiter/pds4/lroc/lro-l-lroc-5-rdr/LROLRC_2001/DATA/MDR/WAC_EMP/WAC_EMP_README.TXT) -- confirmed live (not assumed): WAC_EMP's own product page lists the identical 7 wavelengths *and* specifically calls out 643nm as the one band offered at 304 ppd (~100 m/px), matching this project's documented layer on wavelength *and* that unusual higher-resolution special case simultaneously, too specific to be coincidence. The README states every pixel is "photometrically normalized to a standard geometry of 30 degrees incidence angle, 0 degrees emission angle, and 30 degrees phase angle" using "an empirically derived photometric function similar to that of Boyd et al. (2012)" -- Boyd & Robinson, LPSC 2012 abstract #2795, a purely empirical 3rd-order multivariate polynomial fit to WAC's own photometric-library observations, *not* a Hapke-family model (no opposition surge, no Chandrasekhar H-functions, no roughness shadowing). This means `hapke_shade_ortho`'s existing blend (`reflectance / percentile99(reflectance)`, multiplying the already-normalized texture by an arbitrarily-rescaled fresh Hapke evaluation with no relationship to the reference geometry) was never a relighting operation at all -- a real, previously-unidentified bug, distinct from Phase 70/71's normal-tilt question, and a leading unifying explanation for why a more geometrically-correct fix (Phase 71) made the real-image match *worse*: a more accurate H(i,e,g) numerator fed into an already-wrong combination formula diverges further from truth, not closer.

**Fix**: relight by the ratio H(i,e,g)/H(reference) instead -- the standard photometric-correction technique (valid whenever the source was itself produced by an equivalent ratio-based normalization), physically meaningful and centered near 1 for geometries close to the reference, so no separate display-range rescale is needed beyond `[0, 255]` clipping. `_hapke_reflectance` (new, in `src/trntest/lunaserv.py`) factors the shared `photomet` subprocess-orchestration out of `hapke_shade_ortho` so it can be called twice -- once for the real per-pixel geometry, once for a tiny constant-valued `REFERENCE_INCIDENCE_DEG`/`REFERENCE_EMISSION_DEG`/`REFERENCE_PHASE_DEG` (30/0/30) backplane, cheap since H(reference) has no per-pixel variation. Not a bit-exact inverse of Boyd's specific polynomial (a genuinely different functional form than Hapke), but the Sato et al. (2014) Hapke parameters this project already samples were themselves derived from/cross-validated against this same WAC photometric dataset, so it's a principled approximation, not an arbitrary stand-in -- independently corroborated by an external ChatGPT consultation the user ran in parallel, reaching the same conclusion.

**Empirical result: makes the brightness-matched diff against the real WAC crop worse, not better, on the one candidate tested.** Measured with a new reusable tool (see below): 8.6853 (old percentile blend) -> 9.2425 (new ratio blend). Live diagnostics ruled out an obvious implementation bug -- the ratio values themselves are physically sane (median 0.77, range [0.24, 1.18], matching the real geometry's own wide 28-80 deg phase-angle span for this candidate) -- but the *size* of the regression (a ~6.4% relative increase) is larger than a back-of-envelope expectation that brightness-matching's own single-scalar median rescale should absorb most of a pure denominator-constant change, an open discrepancy not fully explained. Not investigated further this phase.

**User's call: both corrections (normal-tilt and Hapke-ratio) made fully unconditional -- no opt-out parameter for either any more.** Explicit user decision, given despite the unresolved/negative real-image performance: "the need for the correction seems very clear." `normal_tilt_correction` (the parameter itself, `DEFAULT_NORMAL_TILT_CORRECTION`, and its `False` path) removed entirely from `_terrain_photometric_angles`/`hapke_shade_ortho`/`despeckle_and_shade_ortho`/`fetch_dem_and_ortho`/`ortho_shaded_filename` -- the corrected normal-tilt formula is now simply what the code does, unconditionally. The Hapke-ratio fix never had a toggle to begin with (nothing to remove there). `ortho_shaded_filename`'s `_normaltilt` suffix is kept, but made permanent/unconditional rather than deleted outright -- a real cache-safety requirement, not just following the letter of Phase 68's own "unconditional fixes don't need a suffix" precedent: pre-Phase-71 cached files already exist on disk under the *un-suffixed* name (`ortho_shaded_hapke_atc_realparams.tif`), across potentially many previously-populated dataset entries, not just this session's own test candidate -- dropping the suffix now would silently resume that stale, pre-correction content under today's default's own name, exactly the risk this filename scheme exists to prevent.

**New reusable tool: `plotting.compute_brightness_matched_diff`.** Every prior brightness-matched-diff number in this project's history (Phase 68/70/71, this phase's own first measurement) was hand-recomputed ad hoc, in a throwaway script, with no committed code and no guarantee any two attempts used the same methodology -- confirmed live: this phase's own reconstructed baseline (8.6853) doesn't match Phase 70's originally-cited figures (6.89/7.58) at all, most likely a methodology difference, not a contradiction, but impossible to confirm either way without the original (deleted) code. Factored out as a real, tested, reusable function (reusing `_prep_overlay_rasters`'s existing brightness-matching technique) so future comparisons are reproducible and mutually comparable going forward. Also fixes a real gotcha discovered live while building it: the base and overlay rasters share a map grid but not necessarily the same window/extent (e.g. a padded DEM/ortho AOI vs. a real crop's own smaller footprint) -- naively diffing the underlying arrays by raw position raises a shape-mismatch error (or would silently misalign them if the shapes happened to match by coincidence); both must be aligned by real coordinate (`reindex_like`) first. 3 new synthetic-fixture tests in `tests/test_plotting.py`; verified live against the real candidate to reproduce the hand-rolled number closely (9.228 vs. 9.2425 -- the tiny residual is almost certainly a minor valid-pixel-count difference between the two ad hoc scripts, not a bug).

**Three further visual observations from the user, while comparing old-vs-new blends in `image_generation.ipynb`'s Phase 6B, in order:**

1. **The WAC crop shows a real east-brightening gradient our hillshade reproduces only weakly, in both blends.** Since present in both, independent of today's specific normalization fix. Leading hypothesis, not verified: since `ortho` (WAC_EMP) is itself already normalized to remove geometry-driven brightness gradients, essentially all of any such gradient in our final output should come from our own per-pixel Hapke relighting factor -- if real, this points at our modeled phase-brightness falloff being shallower than reality (single, footprint-center-sampled Hapke params, or the Hapke model's own phase curve vs. Boyd's), not a normalization-formula bug. Not investigated further.
2. **Apparent ~10 deg clockwise shadow "rotation" vs. the real WAC crop, in both blends -- checked live, and it is *not* a sun-azimuth bug.** Queried real `campt` against the *native* ISIS WAC crop cube (real SPICE geometry, not the synthetic CSM-attached one) at the candidate's footprint center: `SubSolarGroundAzimuth` = 227.807 deg vs. this project's own `illumination.sun_azimuth_elevation_deg` = 227.801 deg -- 0.006 deg agreement, decisively ruling out a systematic azimuth error. (Incidentally found ISIS reports two different "sun azimuth" quantities ~93.5 deg apart -- `SubSolarAzimuth` (local-horizon-based) vs. `SubSolarGroundAzimuth` (matches this project's own ENU convention) -- not a bug, just two legitimate definitions.) Leading hypothesis, not verified: `hapke_shade_ortho` has no true ray-traced shadow casting, only local-normal-driven shading -- a real crater's cast shadow (blocked by a distant rim, elongated across the floor) vs. our foreshortened local-slope-only darkening could read as "rotated" to the eye even with correct illumination geometry. Not investigated further.
3. **Crater floors specifically read much brighter in the real WAC than in our hillshade, while the rest of each crater matches reasonably well, in both blends.** Not investigated. Two candidate explanations discussed, not distinguished: this project's ~100m/px DEM (Astropedia GLD100) may be too coarse to resolve a small crater floor's true flatness distinctly from its walls (`np.gradient`'s local-slope estimate smears the two together, most pronounced exactly at small sharply-distinct features, consistent with "floor off, walls fine"); real regolith-albedo anomalies (fresh/immature material) crater floors are commonly known for, which the single whole-image Hapke parameter set has no way to represent. A proposed cheap discriminator (check whether `ortho` itself already shows the floor/wall contrast before any of our relighting) was not pursued -- deprioritized by the user in favor of stepping back to plan next priorities.

**Priority discussion, user's own ask ("are there other open issues we should prioritize, or low-hanging fruit for improved validation?"):** recommended (and then built) the reusable diff-metric tool above as the top priority, on the reasoning that it de-risks every other open thread (none of the open photometric questions can be judged "improved" or "worse" without a trustworthy, reproducible number) -- ranked ahead of continuing to chase the specific visual observations above, ahead of the DEM-aware validation gap (important, but no known tool exists to attack it, not "low-hanging"), and ahead of other pre-existing, unrelated open items (`feature/alignment`'s own DEM-aware camera-pose fit, GLD100 polar coverage, the error-handling audit's unstarted chunks B-E, per-pixel Hapke calibration sampling).

**Outcome**: both corrections are now permanent, unconditional parts of the default pipeline -- no toggle exists for either any more, a deliberate closing-off of the "keep it optional until proven" posture Phase 71 still held. The real-WAC-image regression remains open and, with the Hapke-ratio fix now also implicated as making it *worse*, less understood than before this phase, not more -- both corrections are believed correct on independent physical/geometric grounds, but their *combined* interaction with the real WAC texture and pipeline is an unresolved question for a future session. Three new, specific, still-unexplained visual leads are recorded above rather than lost to a scrollback. `plotting.compute_brightness_matched_diff` is the concrete infrastructure improvement carried forward.

## Phase 73 (2026-08-22) — `phocube`'s DEM-mode ("local") backplanes are a dead end for validation, confirmed with a real DEM patch, not just the coarse global shape model

Same day, same session (`a1`). Revisits idea 2 from the very first plan this session started from (validate our DEM-aware backplane channels against `phocube`'s own `LOCALINCIDENCE`/`LOCALEMISSION`/`LOCALNORMAL`), which Phase 70 had already found looked broken using ISIS's coarse global 128ppd shape model, but left one real, untried variable on the table: whether `phocube` fares better against *this project's own* higher-resolution local DEM (the actual DEM `_terrain_photometric_angles` validates against), reformatted as a real ISIS shape cube, instead of the coarse global one.

**New permanent helper: `isis_wac.sample_local_dem_patch(center_lon_deg, center_lat_deg, cellsize_m, config) -> np.ndarray`.** Builds a 3x3 elevation patch (meters, `MOON_RADIUS_M`-subtracted) around a real ground point by converting the 8 neighbor offsets from the point's own local Orthographic tangent frame to lon/lat (`rasterio.warp.transform` between `lunaserv.local_orthographic_crs`/`geographic_crs`) and batch-sampling real radii via the already-existing `sample_lunar_dem_radii_batch` -- row 0 is north, matching `np.gradient`'s own row-is-north convention elsewhere in this project. Live-verified (2 new tests, `tests/test_isis_wac_dem.py`) against real lat values passed through the mocked batch call. Kept as permanent, tested infrastructure despite this phase's own negative result below -- a generically useful "sample a small real elevation patch around a point" utility, the same kind of investigation-born-but-reusable addition Phase 71's `render.patch_sun_position`/`isis_wac.campt_photometric_angles` turned out to be.

**Result: still broken, and this time genuinely dead-ended, not just "not yet tried with the right DEM."** Attached a fresh CSM-rendered cube's own DEM (via `csminit shapemodel=...`, requires a real ISIS-native map-projected shape cube -- a plain GDAL-ISIS3-converted GeoTIFF with an Orthographic PROJ4 CRS is rejected as "not map-projected," so this used the project's own already-cached global cube for the shape-model attachment machinery itself, sampling *this project's own* local DEM only through `sample_local_dem_patch`'s independent path for the comparison side). At the same 5 real `die5` sample points (top/bottom-left/right, center) used throughout this session's other validations: `phocube`'s raw `Local Incidence Angle`/`Local Emission Angle` values came back tiny (<1, implausible as degrees) -- tested a radians-not-degrees reinterpretation, which raised them into a more plausible range but still left large, systematic disagreement against `_terrain_photometric_angles`'s own DEM-aware computation: incidence off by ~11-14 deg, emission off by ~13-35 deg, at all 5 points, no sign of converging as the guess improved. `Local Normal X/Y/Z` (e.g. one point's raw values ~(34.2, 178.1, -0.75)) is not even close to a unit vector, ruling out a simple scale/unit misinterpretation of that channel too. Not root-caused further -- the pattern (wrong regardless of unit reinterpretation, a non-unit "normal" vector) points at `phocube`'s DEM-mode machinery itself being unreliable for a CSM-attached synthetic cube like this project's own, not a one-off setup mistake on this end, consistent with the real ISIS issue Phase 70 already found (DOI-USGS/ISIS3#3645).

**Outcome**: idea 2 from the original 3-idea plan is now closed out as a genuine dead end, not merely deprioritized -- confirmed with the actual local DEM this project cares about, not just the coarse global shape model Phase 70 tried. `phocube`'s DEM-aware ("local") backplanes are not a usable ground-truth source for this project's real production case (a synthetic, CSM-attached render), on either shape model tried. The DEM-aware validation gap (no known ISIS tool gives trustworthy ground truth for terrain-tilted incidence/emission) remains genuinely open; the user's own next move was to look outside ISIS entirely (see Phase 74).

## Phase 74 (2026-08-22) — Independent cross-check via Ames Stereo Pipeline `sfs`, run as a pure forward renderer

Same day, same session (`a1`), direct continuation of Phase 73's dead end. User's proposal: since ISIS itself has no trustworthy DEM-aware ground-truth tool (Phase 70/73), and every variation tried so far has stayed inside this project's own hand-rolled `lunaserv.py` pipeline, use a genuinely different, independently-coded implementation instead -- ASP's `sfs` (normally an iterative shape-from-shading DEM refiner), run with `--save-sim-intensity-only` to skip the refinement entirely and use it as a pure forward renderer: given a DEM, a camera, a Hapke reflectance model, and a per-pixel albedo map, render what the scene *should* look like, and compare directly to the real WAC crop.

**New module `src/trntest/sfs_validation.py`** (+ `lunaserv.reference_hapke_reflectance`/`hapkehen_params_from_source`, factored out of `hapke_shade_ortho` so both share the exact same reference-geometry-denormalization logic instead of two independently-written copies):

- **`true_albedo_map`**: `ortho / H(reference)` -- literally the same denominator `hapke_shade_ortho` itself divides by when computing its H(i,e,g)/H(reference) relighting ratio (Phase 72), just not multiplied back through by H(real geometry) this time. Consistent with `sfs`'s own `image = exposure * albedo * reflectance(geometry)` formalization: `sfs` supplies its own independent reflectance(geometry) via its own ray-DEM intersection and Hapke evaluation, so handing it this "undo the WAC_EMP reference-geometry normalization" map as `--input-albedo` is the natural analogue of `hapke_shade_ortho`'s own approach, not a new modeling assumption.
- **`hapke_params_to_asp_model_coeffs`**: maps `fetch_real_hapke_params`'s real, ISIS-calibration-sourced `HAPKEHEN` parameters to ASP `sfs --model-coeffs`'s own Hapke order. Confirmed via ISIS's own `photomet.xml` parameter descriptions that 5 of 6 correspond directly (`wh`=omega, `hg1`=b, `hg2`=c, `b0`=B0, `hh`=h) -- but ISIS's 6th parameter, `theta` (macroscopic roughness, ~24 deg for this project's current candidate, not small), has **no equivalent anywhere in `sfs`** (confirmed live: `sfs --help | grep -iE 'roughness|theta'` finds nothing beyond `--model-coeffs` itself) -- silently dropped, a real, permanent approximation gap in this cross-check.
- **`run_sfs_forward_render`**: builds the albedo map, runs `sfs -i <dem> --reflectance-type 2 --model-coeffs "..." --input-albedo ... --save-sim-intensity-only <camera cube>`. **Cannot use the real WAC crop's own native ISIS camera model** -- confirmed live, `sfs` refuses it outright ("Seems to have Isis camera type 1... Maybe it will work with CSM"), ASP's ISIS session support apparently doesn't cover WAC-VIS's native Pushframe camera type at all (a different failure than the known `usgscsm`/Pushframe `groundToImage` reliability issue `isis_wac.py`'s module docstring already describes). Uses this project's own reconstructed CSM Frame camera instead (`render.run_sat_sim`/`patch_sun_position`, attached via `csminit state=`) -- the same camera the Hapke pipeline itself renders from, independently validated against real `campt` to ~0.018 deg (Phase 71's heavy test) -- a real, if different, source of camera-pose truth, not a fallback of unknown quality.
- **`mask_sfs_uncovered`**: `sfs` writes literal `0.0` (not a tagged nodata value) for DEM pixels outside the camera's real coverage -- confirmed live, ~68-72% of the padded DEM/ortho AOI for this candidate, since that AOI is deliberately padded beyond the camera's real footprint. Masking these to real NaN before any brightness comparison is required, not optional -- without it, the first live attempt's brightness-matched diff came out >100x too large (median landing exactly on the uncovered region's own 0.0, corrupting the single-multiplicative-scale brightness match `compute_brightness_matched_diff` relies on).

**A real bug caught and fixed mid-investigation, not just a modeling gap**: the very first live attempt silently wrote a misaligned albedo map -- `dem_ortho_result.dem`/`.ortho` on disk had genuinely drifted to different extents (two independently-cached files from different points in this session, ~54 minutes apart by file mtime), and `rasterio.open(path, "w", **profile).write(array)` does **not** raise on an array-shape/profile-shape mismatch, it silently crops to the destination's own window -- so the albedo map was quietly built from the wrong georeferenced window with no error at all. Fixed by forcing a fresh, single, consistent `fetch_dem_and_ortho` call (deleting the stale pair so `TrnTestEntry.dem_ortho_result`'s disk-resume path can't reuse them). **Open follow-up, not fixed here**: that disk-resume path itself has no consistency check between the two cached files it resumes -- a real, if narrow, gap worth a future guard (e.g. asserting matching shape/transform before returning `DemOrthoResult.result_from_files`'s result), noted in `docs/plan.md`'s open items rather than fixed under this phase's own scope.

**Result: real, own-order-of-magnitude agreement, not a clean win but a genuine independent cross-check.** Brightness-matched diff (`plotting.compute_brightness_matched_diff`, restricted to `sfs`'s real coverage region for a fair comparison) against the real WAC crop: our own existing hillshade = 0.00317-0.00433 (varies slightly with exactly how the comparison region is restricted); `sfs`'s fully independent forward-render = 0.00645 -- about 2x higher, but the same order of magnitude, not wildly divergent. Visually (the new `notebooks/sfs_validation.ipynb`), all three panels (real WAC, our hillshade, `sfs`'s render) show the same craters with closely matching overall shading; the `sfs` panel has a visible brightening artifact near its coverage edge, not yet investigated. This doesn't resolve the Phase 70/72 real-image regression, and if anything shows ASP's own independent Hapke/shading code agreeing with the real image somewhat *less* well than this project's own hand-rolled pipeline does -- but it's a real, differently-coded second opinion landing in the same ballpark as the real image, which is itself useful independent evidence that neither implementation is grossly broken, distinct from every other check this investigation has run so far (all of which stayed inside `lunaserv.py`'s own pipeline or hit a dead ISIS tool).

**New reusable infrastructure, consolidated (not left as throwaway scripts) per this project's own "preserve valuable spikes" convention**: `src/trntest/sfs_validation.py` (4 functions, `tests/test_sfs_validation.py`, 6 new mocked/fast tests -- no live `sfs`/ISIS call in the fast suite, matching how `hapke_shade_ortho`'s own `photomet` dependency is kept out of `tests/test_lunaserv.py`), `plotting.plot_sfs_comparison` (+`plotting._cellsize_m`, a small shared refactor out of `compute_brightness_matched_diff`), and `notebooks/sfs_validation.ipynb`/`.py` -- a real, runnable notebook, not a one-off diagnostic, so this specific cross-check can be rerun against a future candidate or after a future pipeline change without rebuilding it from scratch.

**Not yet done, left for a future session or explicit user direction**: the `sfs` panel's own edge-brightening artifact is unexplained; no `@pytest.mark.heavy` test wraps a real, full `sfs` invocation the way `tests/test_lunaserv_campt_validation.py` does for `campt` (a real, ~10-40s-per-candidate cost, deliberately not added this phase so the notebook -- the user's own stated priority, "getting a result I can see" -- landed first); and Phase 73's `sample_local_dem_patch` addition, while committed as permanent infrastructure, currently has no other caller in the codebase beyond its own tests.

**Follow-up, same session: the "edge artifact" is real geometry, not noise -- and diagnosing it surfaced a real double-counting bug in `true_albedo_map`.** The user, looking at the rendered notebook, flagged the `sfs` panel's brightening as looking "contrast stretched" with "a strong gradient growing toward the northeast corner that might be saturating." Pulling `_terrain_photometric_angles`'s own real per-pixel fields for this candidate confirmed it: phase angle runs from ~80 deg (west) to ~28 deg (northeast), a nearly pure east-west gradient dominating over local terrain -- real geometry (fixed sun direction, camera-to-ground view vector shifting substantially across a ~140km footprint from ~50km altitude), not a DEM blunder or coverage-edge artifact (confirmed separately: none of the 20 brightest `sfs` pixels were near the coverage boundary, and the intensity histogram was a smooth continuous tail, not a discrete spike). Since Hapke reflectance rises steeply as phase drops, and this project's real, ISIS-calibration-sourced `b0=1.65` opposition-surge amplitude is large, the northeast corner being systematically brighter is expected behavior given the real geometry -- also directly relevant to Phase 72's still-open "our hillshade under-represents the WAC's real east-brightening gradient" observation: our own pipeline computes the same large phase-driven brightening internally, but its final `[0,255]` clip (plus the ortho texture already sitting near-saturated at bright terrain) suppresses most of it in the displayed output, while `sfs`'s raw, unclipped `sim-intensity` shows the same underlying effect starkly.

The user's own next observation, verbatim: *"The along-track correction is physically valid for the real WAC but not modeled in the sfs render. If we wanted to match the sfs render, we would remove it. Of course, in the end we want to match the WAC. The sfs render, meanwhile, probably can't be corrected for along-track, so may be less useful than I was thinking."* Checked directly: `_terrain_photometric_angles` with `along_track_correction=True` (the default, what `hapke_shade_ortho` actually uses) gives phase range 27.87-80.45 deg (52.58 deg span) in a nearly pure column-only (east-west) pattern; with it off (`along_track_correction=False`, the closest analogue to what `sfs`'s own reconstructed CSM Frame camera -- a single frozen pose, no per-row along-track modeling possible at all -- implicitly computes), phase range widens to 0.01-98.65 deg (98.65 deg span) in a diagonal (NE/SW) pattern, confirming the correction's own axis (E=0.08, N=0.99, U=0.11, essentially due north, matching LRO's near-polar orbit) is exactly what collapses the diagonal baseline pattern into the clean cross-track-only one. So the correction reduces, not causes, the swing -- but `sfs`, using a camera model structurally unable to represent it, implicitly computes something much closer to the *uncorrected*, wider-swing case.

Thinking through why that would matter turned up something more fundamental than a geometry mismatch: **`true_albedo_map` had a real double-counting bug.** It divided `dem_ortho_result.ortho` by the constant `reference_hapke_reflectance` (H(reference)) -- but `dem_ortho_result.ortho` is `hapke_shade_ortho`'s **already-shaded** output (`raw_ortho * H(real,ATC)/H(reference)`, clipped), not the raw pre-shading WAC_EMP texture. Dividing that by the constant H(reference) again left a full, uncanceled `H(real,ATC)` factor sitting in "albedo," which `sfs` then multiplied by its *own* independently-computed `H(real, no ATC)` a second time -- silently squaring the geometry-dependent brightening factor, worst exactly where it's largest (the low-phase northeast corner). **Fix** (`lunaserv.real_geometry_hapke_reflectance`, factored out of `hapke_shade_ortho`'s own setup so both share it; `sfs_validation.true_albedo_map`'s new signature takes `real_reflectance` directly): divide by `H(real,ATC)` -- the *same* factor `hapke_shade_ortho` itself multiplied in -- instead of the constant H(reference). Algebraically: `shaded_norm/H(real) = (raw_norm * H(real)/H(reference))/H(real) = raw_norm/H(reference)`, the actual quantity wanted, recovered using only values already computed, no need to keep the raw pre-shading texture around.

**Measured effect of the fix: real, but modest** -- brightness-matched diff against the real WAC crop, 0.006452 -> 0.006068 (~6% better); `sfs`'s own raw max sim-intensity, 301.96 -> 269.90 (~11% lower); p99, 182.28 -> 157.42. Not the dramatic correction a clean "squared bug" might suggest, because `dem_ortho_result.ortho`'s own `[0,255]` clip already discards much of the per-pixel variation the bug would otherwise have doubled. **The double-counting bug was a real, separate, worth-fixing correctness issue, but it was never the dominant driver of the northeast brightening -- the along-track camera-model gap the user identified is.** Back-of-envelope confirmation via Hapke's own opposition term `B(g) ~ B0/(1+tan(g/2)/h)`: at `g=27.89 deg` (this candidate's real, ATC-corrected northeast-corner phase) `B(g)~0.25`; at `g=7.95 deg` (the same corner's phase *without* the along-track correction, i.e. closer to what `sfs`'s own reconstructed camera implicitly sees) `B(g)~0.65` -- a real ~2.5x jump in just the opposition-surge contribution alone, from losing a correction `sfs` has no way to represent.

**Outcome, confirming the user's own read**: `sfs`'s residual disagreement with our own pipeline in exactly this region is well-explained, not mysterious -- primarily the structural along-track-correction gap (large, unfixable without a different camera model for `sfs` -- CSM Frame cameras have no per-row time-varying pose), secondarily the now-fixed double-counting bug (small, real, worth having fixed regardless). This meaningfully narrows what `sfs` is actually useful for as an ongoing cross-check: most informative near the frame's own along-track center (where the along-track correction's own magnitude is smallest, so the camera-model gap matters least), and structurally unable to give a clean apples-to-apples comparison anywhere the real along-track correction matters a lot -- exactly the user's own conclusion, now with a quantified mechanism behind it rather than just a visual impression. Whether to keep pursuing `sfs` (e.g. restricted to the along-track-center strip) or treat this as this particular tool's own natural stopping point is an open decision for the user, not resolved this phase.

## Phase 75 (2026-08-23) — `sfs`'s Lambertian-mode trick gives this project its first real DEM-aware ground-truth check

Same session (`a1`), direct continuation of Phase 74. The user's own question: *"Now I'm curious how well we could match the sfs render if we tried. I guess it's not easy because it has a different Hapke model. ChatGPT at one point suggested we could put sfs in Lambert mode and infer what it calculated for the incidence angle."*

**The trick**: Lambert's law is `image = exposure * albedo * cos(incidence)` -- no emission or phase term at all, unlike Hapke. Running `sfs --reflectance-type 0` with a uniform `--input-albedo` of `1.0` therefore makes its raw `sim-intensity` output exactly `exposure * cos(incidence)`. `exposure` isn't `1.0` -- confirmed live `sfs` applies some non-unit internal default scaling even without `--estimate-exposure-haze-albedo` (139.66 for this candidate) -- but it's written to `<prefix>-exposures.txt`, so dividing it back out and taking `arccos` recovers `sfs`'s own fully independent, ray-DEM-intersection-derived incidence angle, with zero Hapke-parameterization dependence to confound the comparison the way Phase 74's own cross-check was.

**Result: near-exact agreement across the whole real-coverage region, not just a handful of sample points.** Compared against `lunaserv._terrain_photometric_angles`'s own incidence field (via the new `real_geometry_photometric_angles`) over all 1,618,386 real-coverage pixels for this candidate: mean|diff| = 0.0237 deg, max|diff| = 0.5138 deg, with the small residual visibly concentrated at crater rims (discretization noise between `sfs`'s own ray-DEM-intersection normal and `_terrain_photometric_angles`'s `np.gradient`-based one, not a systematic bug). **This is this project's first genuine DEM-aware ground-truth check** -- Phase 70/73 found no ISIS tool that gives one at all (`phocube`'s `LOCAL*` backplanes confirmed broken/implausible; real `campt`'s plain angles confirmed to stay ellipsoid-normal-based even with a DEM shape model attached). It independently confirms `_terrain_photometric_angles`'s incidence computation -- including the normal-tilt/sphere_sag correction from Phase 70/71 -- is geometrically correct on real terrain, at full resolution, not just the synthetic-sphere/ellipsoid cases Phase 71's `campt` heavy test already covered.

**Incidental clarification, checked directly**: incidence came out *identical* whether `along_track_correction` is on or off. Makes sense once stated -- incidence depends only on the surface normal and sun direction, never the view vector, so the along-track correction (which only ever adjusts the effective camera position) can only affect emission/phase, never incidence. This check is therefore clean and complete for incidence, but says nothing about emission/phase or the along-track question Phase 74 raised -- Lambert's law has no way to isolate either of those the same way.

**Formalized** (user: "Yes, formalize it -- real module, test, and notebook section"):
- `lunaserv.real_geometry_photometric_angles` -- `(incidence_deg, emission_deg, phase_deg)` at `camera`'s own real per-pixel geometry, factored out of `real_geometry_hapke_reflectance`'s own setup (no behavior change to the latter) so a caller can get just the angles without a Hapke evaluation.
- `sfs_validation.run_sfs_lambertian_incidence`/`incidence_deg_from_lambertian_sim_intensity` -- runs the Lambertian `sfs` invocation and inverts its output; `sfs_validation._camera_cub_for_sfs` factors the shared camera-attachment steps (`run_sat_sim`/`patch_sun_position`/`gdal_translate`/`csminit`) out of both this and `run_sfs_forward_render`, which previously duplicated them.
- `plotting.plot_incidence_validation` -- 3-panel sfs/ours/diff comparison, new section in `notebooks/sfs_validation.ipynb`.
- **`tests/test_sfs_validation_lambertian_incidence.py`, a new `@pytest.mark.heavy` test** -- this project's first heavy test that validates the DEM-aware case specifically (`test_lunaserv_campt_validation.py`'s own heavy test is ellipsoid-only), asserting `mean|diff| < 0.1 deg` and `max|diff| < 1.0 deg` over the *entire* real-coverage region (not a sparse 5-point sample the way the `campt` test is limited to) -- a real margin above the observed 0.024/0.51 deg without masking a genuine regression. Runs in ~16s (fast relative to Phase 74's own full-Hapke `sfs` run, since Lambertian mode has no `--model-coeffs`/albedo-derivation step to speak of). 6 new fast/mocked tests for the pure inversion math and the plotting function, no live `sfs`/ISIS call in the fast suite.

**Follow-up, same session: the residual isn't pure noise -- it has a real, small directional structure.** The user, looking at the notebook's diff panel, noticed the small (mostly-white) residual still shows a visible trend: blue (sfs lower) in the southwest, red (sfs higher) in the northeast. Fit a plane to `diff_deg` over real (x_east, y_north) coordinates: gradient points along compass bearing 35.17 deg -- close to the sun's own azimuth's *opposite* direction (227.80 deg sun azimuth, 47.80 deg opposite), a ~12.6 deg gap plausible given the fit only explains 17% of the residual's total variance (most of it is still the localized crater-rim noise already described above). A competing "simple radial sagitta-approximation error" hypothesis was checked and ruled out directly: correlation between `diff_deg` and plain distance from the tangent point is ~0.046, essentially zero. This makes physical sense once framed correctly: incidence-angle error is a `normal . sun_direction` dot-product error, so it's most sensitive to normal-vector errors that project *along* the sun direction, and near-insensitive to ones perpendicular to it -- exactly the directional signature a first-order tangent-plane approximation's own remaining (uncorrected) higher-order curvature error would leave, distinct from the already-corrected radial/sagitta term. Magnitude is small (~0.056 deg per 100km, well under a tenth of a degree across the whole real coverage region from this linear component alone) and doesn't change the heavy test's own pass/fail margin -- recorded here as a real, now-understood residual characteristic, not an open mystery for a future session to rediscover.

**Outcome**: closes a real, long-standing gap (DEM-aware validation, open since Phase 70) with a clean, independent, whole-frame-resolution result, using a tool this project already had a reason to reach for. Phase 74's own Hapke-model cross-check remains limited by the along-track/theta gaps described there; this Lambertian check is a narrower but much cleaner win -- incidence only, but validated thoroughly (including its own small residual's real directional structure, not just its magnitude) and now a permanent regression test.

## Phase 76 (2026-08-23) — Relief displacement: the real source of Phase 75's residual, fixed and independently confirmed to ~0.0005 deg

Same session (`a1`), direct continuation of Phase 75. User's question, on hearing "sagitta" called an approximation for the first time: *"Is there a convenient way to incorporate the higher-order curvature?"* -- then, mid-investigation, refined the visual read: *"The difference between the error patches in the southwest to the northeast is about 1 degree."*

**The actual gap, derived and confirmed**: `sphere_sag` (`sqrt(radius_m**2-x**2-y**2)-radius_m`) is exact -- the *reference sphere itself* has no approximation. The gap is one step later: real planetary elevation (`dem`) is physically defined along *each point's own* true local radial direction, but `_terrain_photometric_angles` was still adding it along the tangent point's *fixed* Up axis (`dem + sphere_sag`) -- correct only at the tangent point itself, increasingly wrong away from it. This is the same "relief displacement" effect well known in orthophoto/DEM photogrammetry: real terrain relief shifts a point's own apparent horizontal position under an orthographic-style projection, not just its height. Worked out the exact closed form (both exact, not small-angle approximations, derived directly from `sphere_sag`'s own definition): a point's true (East, North) position is displaced outward by `relief_scale = 1 + dem/radius_m`, and its true "Up" contribution is `dem*cos(theta)` (`cos_theta = (sphere_sag+radius_m)/radius_m`), not the plain `dem` the code was using.

**Why the user's two observations were both right, and how they fit together**: since the effect scales with `dem` itself (not just distance from the tangent point), it's concentrated exactly at high-relief terrain (crater rims) rather than smooth across the frame -- a first linear-plane fit to the residual (Phase 75) underestimated its real peak-to-peak size (R^2 only 0.17, a poor model for a patchy, terrain-correlated effect) even though it correctly identified the directional signature (gradient azimuth matching the sun's own azimuth, since incidence error is a `normal . sun` dot product, most sensitive to normal error that projects along the sun direction). The user's ~1 deg peak-to-peak visual read (specific SW/NE patches, not an averaged trend) was the more honest number.

**Fix, implemented in `lunaserv._terrain_photometric_angles`**: `ground` now uses the exact embedding above (`(x_grid*relief_scale, y_grid*relief_scale, sphere_sag + dem*cos_theta)`) instead of `(x_grid, y_grid, dem+sphere_sag)`. Since `ground`'s (East, North) components are no longer exactly the flat grid coordinates, `normal` can no longer use the `normalize(-df/dx, -df/dy, 1)` shortcut (only valid when the embedding's horizontal components are the differentiated coordinates themselves) -- generalized to the standard parametric-surface-normal construction instead: `np.gradient` on each of `ground`'s 3 coordinate channels (row/col index space, via the same `dy`/`cellsize_m` spacing as before), cross product of the two resulting tangent vectors. **A strict generalization, not a rewrite**: at `dem=0` everywhere, `relief_scale=1` and `dem*cos_theta=0`, reducing exactly to the previous formula -- confirmed live, all 38 existing `tests/test_lunaserv.py` tests (all flat-`dem` synthetic scenarios) pass byte-for-byte unchanged, no updates needed.

**Empirical result: essentially closes the residual, not just shrinks it.** Re-ran Phase 75's own heavy test (`tests/test_sfs_validation_lambertian_incidence.py`, `sfs`'s independent DEM-aware incidence vs. `real_geometry_photometric_angles`, whole real-coverage region): mean|diff| 0.0237 -> 0.0005 deg, max|diff| 0.5138 -> 0.0005 deg -- a ~47x/~1000x reduction, down to floating-point/interpolation noise level, not real geometric disagreement. The notebook's own diff panel (`notebooks/sfs_validation.ipynb`) went from visibly showing crater-rim structure and a SW/NE gradient to uniformly flat white. This means what looked like "ordinary `np.gradient` discretization noise" in Phase 75 was actually mostly this same real, systematic error, just locally amplified at exactly the crater-rim locations where it's largest -- a stronger, more surprising result than the original back-of-envelope estimate suggested, and now independently confirmed (a second, differently-coded implementation, ASP `sfs`, agrees with this project's own from-scratch derivation to sub-thousandth-of-a-degree precision).

**What this does and doesn't change**: `real_geometry_photometric_angles`'s `emission_deg`/`phase_deg` (and therefore `hapke_shade_ortho`'s own default shaded output, since `ground`'s position also feeds `view_vec`) use the same corrected embedding for consistency, on the same physical reasoning -- but that consequence hasn't been independently re-validated the way incidence has (Lambert's law has no emission/phase term to extract the same way). Checked directly whether this closes the still-open Phase 70/72 real-WAC-crop brightness-matched-diff regression: **no, essentially unchanged** (0.004330 vs. the pre-fix run's 0.004332, well within noise) -- this fix is real and now independently proven correct, but it was never a plausible explanation for that separate, much larger, still-unexplained regression. `sfs_validation.run_sfs_forward_render`'s own Hapke-model comparison is similarly close to unchanged (0.006068 -> 0.006077, noise-level) -- expected, since that comparison's own dominant residual (Phase 74) is the along-track camera-model gap, a completely different mechanism this fix doesn't touch.

**Test/heavy-test thresholds tightened**: `tests/test_sfs_validation_lambertian_incidence.py`'s assertions moved from `mean < 0.1 deg / max < 1.0 deg` (Phase 75's own margin above its 0.024/0.51 deg observation) to `mean < 0.005 deg / max < 0.01 deg` (a real margin above this phase's 0.0005/0.0005 deg observation, tight enough to catch a real regression back toward the old, larger residual).

**Outcome**: a real, previously-undocumented second approximation gap, found by directly investigating a small residual the user noticed and refused to write off as noise, derived to an exact closed form, implemented as a genuine generalization (not a special case), and independently confirmed via a second, differently-coded tool to a precision an order of magnitude tighter than this project's previous best DEM-aware validation. `real_geometry_photometric_angles`'s incidence output can now be treated as essentially exact for practical purposes, not just "validated to within known caveats."

## Phase 77 (2026-08-23) — `_terrain_photometric_angles` made fully MOON_ME-native, via `rasterio.warp.transform` to a real geocentric CRS

Same session (`a1`), direct continuation of Phase 76. The user, reflecting on three successive hand-derived terrain-embedding fixes in a row (sagitta, normal-tilt, relief-displacement): *"It's frustrating to keep finding errors in our custom software... might it not be simpler to explicitly transform the DEM samples to MOON_ME x/y/z coordinates, and likewise transform spacecraft and sun positions, and do all the calculations that way? I'm assuming we have validated tools for that transform."* A first, smaller proposal (rewrite the existing tangent-plane closed form as one self-evidently-correct formula, no signature changes) was explicitly rejected: *"No, I meant switching to MOON_ME-native, explicitly trying to reduce complexity by removing the need for any ENU calculations... Maybe there is some more vectorized path involving GDAL or rasterio somehow?"*

**Confirmed live, exactly the tool the user was hoping existed**: `rasterio.warp.transform` (already imported in `lunaserv.py`) accepts an optional `zs` array and, given a destination CRS built from a new `+proj=geocent` PROJ4 string (`moon_geocentric_crs`, siblings `geographic_crs`/`local_orthographic_crs`), converts `local_orthographic_crs`'s own projected `(x, y)` *plus* a real elevation `z` directly into true MOON_ME X/Y/Z in one vectorized call -- verified three independent ways (direct ortho->geocentric, the old two-step ortho->geographic->geocentric path, and the original hand-derived spherical formula) to agree to full float64 precision. This is a real, previously-unused corner of a library already trusted elsewhere in this file, not new machinery.

**As with the rejected smaller proposal, this doesn't change any number** -- it's proven algebraically equivalent to the Phase 76 closed form (confirmed live: the heavy `campt` test still reports max|diff| 0.018214 deg, and the heavy `sfs` Lambertian test still reports mean/max 0.0005 deg, both identical to Phase 76's own recorded figures; `notebooks/sfs_validation.ipynb`'s brightness-matched diffs are also byte-for-byte unchanged). The value is entirely in eliminating the pattern that produced three successive bugs -- a hand-derived tangent-plane approximation nobody could fully trust was complete -- in favor of one call to well-tested library code, directly answering the user's stated frustration with their own proposed mechanism.

**What changed in `_terrain_photometric_angles`**: `ground` (each DEM pixel's true 3D position) now comes directly from the `rasterio.warp.transform` call above, replacing `sphere_sag`/`cos_theta`/`relief_scale` entirely. `camera_center_moon_me_m`/`along_track_direction_moon_me` are `Camera.camera_center_moon_me_m`/`camera_along_track_direction_moon_me` (already real MOON_ME) used directly, with zero rotation -- `_camera_local_enu_m` and `_local_enu_direction`, which used to do that rotation, are deleted as dead code (confirmed via grep: no other callers). `normal`'s construction (the generic parametric-surface-normal `np.gradient`+cross-product) is otherwise unchanged, now just operating on real MOON_ME coordinates.

**One rotation deliberately kept, not an oversight**: `real_geometry_photometric_angles` still takes `azimuth_deg`/`elevation_deg` (the sun relative to the tangent point's own local horizon) -- the existing, human-readable convention every caller/test/notebook uses, and one `shade_ortho`'s Lambertian fallback genuinely needs regardless for matplotlib's `LightSource` API. It's converted to a MOON_ME vector once, via a new `_moon_me_direction_from_local_enu` (the exact inverse rotation of the deleted `_local_enu_direction`, built on the kept `_local_enu_basis`). Rotating a free *direction* between orthonormal frames is exact and lossless -- no elevation-embedding step to get subtly wrong, unlike a *position* -- so it was never part of the bug class Phases 70/72/76/77 otherwise eliminated, and threading raw MOON_ME sun vectors through every caller instead would have added real churn (a new `illumination.sun_direction_moon_me`, signature changes cascading through `hapke_shade_ortho`/`despeckle_and_shade_ortho`/`fetch_dem_and_ortho`) for no numeric benefit.

**Test migration**: `_camera_local_enu_m`'s and `_local_enu_direction`'s own dedicated tests are deleted with the functions; a new test covers `_moon_me_direction_from_local_enu`. The 5 direct `_terrain_photometric_angles` synthetic tests in `tests/test_lunaserv.py` -- which check relative geometry, not tied to any real Moon location -- now fix an arbitrary tangent point `(lon, lat) = (0, 0)`, where MOON_ME's own axes happen to be a simple permutation of the old local (East, North, Up) frame (`up=(1,0,0)`, `east=(0,1,0)`, `north=(0,0,1)`), via two tiny test-local helpers; every test's own closed-form *expected value* math is unchanged. `tests/test_lunaserv_campt_validation.py` now calls the *public* `real_geometry_photometric_angles` instead of hand-building a local-frame camera position via the now-deleted `_camera_local_enu_m`, exercising the real end-to-end path. All 38 `test_lunaserv.py` tests, the full 276-test default suite, and both heavy tests (`campt`, `sfs` Lambertian) pass unchanged.

**Outcome**: replaces three successive hand-derived closed-form terrain-embedding corrections with one call to a real, validated coordinate-transform library already used elsewhere in this file, at the user's own explicit direction and using their own proposed mechanism (confirmed to exist, where an initial smaller in-house-math alternative was correctly rejected as not actually addressing the underlying complaint). Provably and empirically numerically identical to Phase 76's output -- this closes the "is our hand-rolled geometry trustworthy" question by removing the hand-rolled part, not by finding a further bug.

## Phase 78 (2026-08-23) — Replaced the Lunaserv-WMS ortho source with WAC_EMP's own PDS4 archive tiles, fixing the confirmed affine display stretch

New session, picking up an approved plan a prior session (`a1`) wrote but ran out of budget to implement (see that session's own memory note and `docs/data-sources.md`'s "WAC_EMP PDS4 archive" section for the confirmed affine-stretch numbers that motivated this: `luna_wac_normalized_reflectance`'s WMS-served DN was found to carry `DN/255 = a*reflectance + b`, `a≈5.94-5.98`, `b≈-0.213..-0.214`, not raw reflectance). Implemented the full plan: `cache.fetch_wac_emp_tile`, `config.wac_emp_base_url`, and three new `lunaserv.py` functions (`wac_emp_tile_id_for_bbox`, `fetch_wac_emp_reflectance`, `reproject_wac_emp_reflectance_to_local_grid`).

**Tile-naming scheme confirmed live, not guessed from one example filename**: the plan's own throwaway diagnostic script had hardcoded one tile's constants (`WAC_EMP_643NM_E300N1350_304P.IMG`) without ever deriving the general pattern. Queried the real archive's own S3-backed host directly (`?list-type=2&prefix=...&delimiter=/` against `pds.mcp.nasa.gov/data/store/img/...`, a real `ListObjectsV2` call, not a browser directory listing) and got the full 159-key listing back. This revealed the real pattern: `WAC_EMP_<wavelength>NM_E300<N|S><lon_center_deg*10:04d>_<ppd:03d>P.IMG` -- one 60-deg latitude band per hemisphere (`E300` = the fixed 30.0-deg band-center code), 4 lon zones 90 deg wide (centered 45/135/225/315), every band offered at 64 ppd with 643nm additionally offered at 304 ppd, and a separate, unverified-format polar tile pair (`P900N`/`P900S`, 643nm only) confirming the plan's own decision to scope polar coverage out via a `WAC_EMP_MAX_ABS_LATITUDE_DEG = 60.0` guard (mirroring `ASTROPEDIA_MAX_ABS_LATITUDE_DEG`'s precedent) rather than guess at that format.

**Numeric pipeline redesign, as planned**: WAC_EMP's tile is real IEEE754 float32 physical reflectance with no embedded display stretch (confirmed live via `gdalinfo`/`rasterio` on the real 304ppd 643nm tile -- a genuine PDS3-attached-label file GDAL's own driver reads natively, real Equirectangular CRS/transform, no hand-rolled PROJ4 or manual byte-offset math needed in the permanent code, unlike the investigation's own throwaway scripts). `hapke_shade_ortho`'s old `ortho.astype(np.float64) / 255.0` un-scaling step -- which was always implicitly assuming a linearly-scaled DN input -- is gone; it now computes `relit_reflectance = ortho * ratio` directly in real physical units and returns that (float64), not a `uint8` image. A new, explicit `stretch_reflectance_to_uint8` (a fixed linear range, `DISPLAY_STRETCH_REFLECTANCE_MIN/MAX = 0.0/0.30`, not adaptive) is the one place that cosmetic display step happens now, moved to the very end of `despeckle_and_shade_ortho`, decoupled from the physics. Live-validated against the real default candidate: `0.30` keeps the whole frame comfortably inside `[0, 255]` with zero saturation either direction (observed `min=32, max=227, mean≈79`).

**A real bug caught live by actually running the notebooks, not just by reasoning about the design**: the first regeneration of `notebooks/hapke_hillshade.ipynb` produced a **fully black, 100%-zero-pixel** Lambertian (`hapke=False`) ortho. Root cause: `shade_ortho` (the plain-Lambertian fallback) is deliberately unchanged, per the plan's own explicit scope, still assuming its input is `[0, 255]` DN -- but `despeckle_and_shade_ortho`'s `hapke=False` branch was still handing it the raw WAC_EMP array directly, now real reflectance (~0.05-0.3). `shade_ortho`'s own internal `/255.0`-then-`*255.0` round-trip algebraically cancels for values this small, so the result truncated to 0 under `.astype(np.uint8)` every time. Fixed by giving `despeckle_and_shade_ortho` its own `ortho_source` parameter and applying `stretch_reflectance_to_uint8` to the cleaned array *before* handing it to `shade_ortho` whenever `ortho_source="wac_emp_pds"` -- `shade_ortho` itself stays completely untouched, exactly as scoped; only the orchestrating function's choice of what to feed it changed. A new heavy regression test (`test_fetch_dem_and_ortho_wac_emp_pds_lambertian_fallback_is_not_all_black`) locks this in. The mirror-image combination, `ortho_source="lunaserv_wms"` (deprecated fallback, real DN) with `hapke=True` (now real-reflectance-only), is the one combination left genuinely incoherent after this migration -- documented directly in `fetch_dem_and_ortho`'s own docstring rather than guarded in code, since no caller anywhere in this codebase actually requests it.

**A second, genuinely pre-existing bug found and fixed at its two known call sites, though the general naming gap that caused it remains** (flagged in `docs/plan.md`'s open items): `dem_filled-tile-0.tif`'s filename carries no suffix tied to `extra_footprint_lonlat_deg`, unlike `ortho_shaded_filename`'s own careful suffix discipline -- so any two `fetch_dem_and_ortho` calls against the same shared per-candidate output directory with *different* footprints silently clobber each other's DEM file, leaving a mismatched ortho/DEM pair on disk for anything that resumes it later. This was already a known, unguarded risk per `sfs_validation.run_sfs_forward_render`'s own docstring ("not re-guarded against here"); regenerating `sfs_validation.ipynb` right after `hapke_hillshade.ipynb` in the same session triggered its real shape-mismatch `ValueError` live, and a follow-up heavy-suite ordering check reproduced the same corruption as a *geometry* regression (`test_real_geometry_incidence_matches_sfs_lambertian_inversion_across_the_whole_frame`'s residual jumping from 0.0005 deg to 0.379 deg -- alarming on its face, since the plan's own explicit expectation was that geometry stays untouched by this migration; root-caused to the same stale-DEM pairing, not an actual geometry bug).

Root cause, once traced fully: `hapke_hillshade.py`'s own docstring already *claimed* its two `fetch_dem_and_ortho` calls (Hapke via `entry.dem_ortho_result`, Lambertian via a direct call) fetch "the same DEM/ortho pair again" -- but the direct call never actually passed `extra_footprint_lonlat_deg=entry.crop_footprint`, so it silently used a smaller, camera-only-footprint AOI instead. **Fixed properly, not just worked around**: added the missing `extra_footprint_lonlat_deg=entry.crop_footprint` to that call, making the notebook's code finally match its own documented intent -- confirmed live (`ROI size 2387x2440 px` printed identically for both calls, where it previously printed two different sizes). The exact same bug, independently reintroduced by this session's own new `tests/test_wac_emp_ortho_source.py::test_fetch_dem_and_ortho_wac_emp_pds_lambertian_fallback_is_not_all_black` (which also called `fetch_dem_and_ortho(..., hapke=False)` directly without the footprint union), got the identical fix. With both fixed, the full heavy suite (`test_wac_emp_ortho_source.py` + `test_sfs_validation_lambertian_incidence.py` + `test_lunaserv_campt_validation.py`) passes together in either run order, and the geometry residual is back to its correct 0.0005 deg. **The underlying design gap is still open, not eliminated**: `dem_filled-tile-0.tif`'s filename still carries no footprint-aware suffix, so any *future* caller that fetches a differently-scoped footprint against an existing candidate's output directory without also unioning in `entry.crop_footprint` can reintroduce this same corruption -- see `docs/plan.md`'s open items for what a real fix would need.

**Real brightness-matched-diff result, reported honestly per the plan's own requirement**: `sfs_validation.ipynb`'s own official measurement (the codebase's established `compute_brightness_matched_diff(real_wac_crop, our_ortho)` call order -- diff expressed in the real WAC crop's own native small-magnitude units) now reads **mean_abs_diff = 0.00382** for "real WAC vs. our hillshade" -- squarely inside Phase 74's own recorded pre-regression "healthy" range (~0.0032-0.0043), not the elevated values the Lunaserv-WMS-affine-stretch-era regression showed. (An earlier throwaway diagnostic script's own ad hoc measurement, ~6.53, used the same function with its arguments in the *opposite* order -- base=our own 0-255-scale ortho instead of the real WAC crop -- giving a technically real but non-comparable number in different units; not the regression this entry is reporting against.) This is consistent with, though not definitive proof of, the affine-stretch bug having been a real contributor to that regression -- the geometry corrections (Phases 70-77) are untouched by this migration and their own residuals (`campt` ~0.018 deg, `sfs` Lambertian ~0.0005 deg) are confirmed unchanged.

**Tests**: 8 new fast tests (`tests/test_lunaserv.py`, `tests/test_cache.py`) for `wac_emp_tile_id_for_bbox`'s tile resolution/error cases and `reproject_wac_emp_reflectance_to_local_grid`'s shape/constant-field preservation, mirroring the existing Astropedia test shapes. 3 new `@pytest.mark.heavy` tests (`tests/test_wac_emp_ortho_source.py`) against the real default candidate: the fetched ortho is non-saturating, the Lambertian-fallback-black-image regression stays fixed, and the real candidate resolves to the exact confirmed tile. Full 290-test default suite and the pre-existing `campt`/`sfs`-Lambertian heavy tests all pass unchanged (one unrelated, pre-existing flaky huey-subprocess test failure confirmed present on unmodified `HEAD` too, not caused by this change). `trntest-lint` clean (source, tests, and the 3 regenerated notebooks).

**Docs**: `docs/data-sources.md` gained a full "WAC_EMP PDS4 archive" section (URL, confirmed tile-naming scheme/grid, format, ±60-deg limit, size) and a note on the existing Lunaserv section documenting the confirmed affine stretch. Both throwaway diagnostic scripts (`_boyd_hapke_mismatch_tmp.py`, `_wac_emp_pds_scale_check_tmp.py`) deleted once their findings were folded in here, per this project's standing convention.

**Not done in this pass** (see `docs/plan.md`'s open items): `notebooks/along_track_correction.ipynb`/`real_hapke_params.ipynb`/`reproject_spike.ipynb`/`pose_alignment_spike.ipynb` also call `fetch_dem_and_ortho`/`dem_ortho_result` and so are equally affected by the new default, but weren't regenerated this pass (the approved plan scoped exactly `hapke_hillshade`/`image_generation`/`sfs_validation`) -- their committed `.ipynb` outputs are now stale relative to current code. The `dem_filled-tile-0.tif` naming collision above is also unfixed. Regenerated notebooks are held, per this project's standing convention, for the user's own Jupyter Lab review before any commit.

## Phase 79 (2026-08-23) — Implemented `docs/intermediate-product-plan.md` (Phases 1-5): a real write/read registry, atomic publishing, the `_work/` path hierarchy, and a validated fix for the documented concurrent-worker race

New session, working directly in the main checkout (not a worktree -- `git rev-parse --show-toplevel` confirmed it; the checkout was on a stale `dummy` branch, 15 commits behind `main` with zero unique commits of its own, so switched to `main` first, then a new `feature/intermediate-product-discipline` branch off it) at the user's own request to implement the plan doc `af2a22a`/`c94509f` had already landed on `main` from an earlier session.

**Phase 1-2 (`src/trntest/product_registry.py`, new module)**: `writes_product`/`reads_product`/`deletes_product` decorators (module-level dicts, `writes_product` raises `ProductRegistryError` on a duplicate label at decoration time) plus `atomic_publish`/`atomic_publish_path` context managers, generalizing `cache.cached_get`'s existing temp-then-atomic-rename pattern from fetched files to generated ones. Applied to zero real functions in this first commit, per the plan's own phasing. 17 new unit tests.

**A real bug found only by the heavy suite, not unit tests**: `atomic_publish_path`'s first version named its temp file `<dest.name>.<random>.tmp` -- real ISIS `to=` calls (`framestitch`/`crop`/`cam2map`) silently never wrote anything there at all (no error at write time), only surfacing later as a `FileNotFoundError` on the rename-to-`dest` step. ISIS's own cube writer appears to require/expect a real `.cub`-like extension on its output path. Fixed by preserving `dest`'s own suffix at the end of the temp name (`<dest.stem>.tmp.<random><dest.suffix>`) instead of a generic `.tmp`, applied to both context managers for consistency; added a regression test and confirmed the pre-existing unit tests (which mock `run_quiet` and never exercise this) hadn't and couldn't have caught it.

**Phase 3**: moved `_work/` onto the entry-scoped/generator-scoped path hierarchy the plan describes. `isis_wac._spike_dir` now returns `_work/<entry>/isis/` (`config.output_dir`) instead of the old workspace-level `scratch_dir/isis_wac/<edr_product>/` -- the cross-dataset-reuse case that separation used to serve isn't load-bearing (real datasets are non-overlapping in `edr_product` by construction), and this keeps the single most expensive-to-regenerate subtree distinguished so it survives routine `_work/<entry>/` pruning that excludes `isis/`. `isis_wac.run_cam2map_for_crop`'s outputs move to `_work/<entry>/crop/` (generator-scoped, even though `TrnTestReprojectImage` also reads them back). `TrnTestHillshadeImage`/`TrnTestReprojectImage._mapprojected_path`'s mapproject output moves to `_work/<entry>/<hillshade|reproject>/`, reusing `raster_path.parent.name` rather than a second per-subclass constant. `sfs_validation.py`'s investigation-only outputs move to `_work/<entry>/sfs_validation/`. `dataset.generate_dataset()`'s separate flat `output_dir/<product_id>` layout is untouched -- out of scope, a different pipeline entirely.

**Phase 4**: registered the real writers (`lunaserv.fetch_dem`/`fetch_and_shade_ortho`, `isis_wac.run_framestitch`/`crop_for_camera`/`run_cam2map_for_crop`, `render.run_sat_sim`) and retrofit `atomic_publish`/`atomic_publish_path` onto the direct-write cases (`lunaserv._reproject_raster_to_local_grid`, `despeckle_and_shade_ortho`, and the three `isis_wac` `to=`/positional outputs above). The `run_cam2map_for_crop` retrofit fixed a real latent gap as a side effect: that function had no existence guard at all, so a second call for the same crop used to hit ISIS's own "`to=` already exists" refusal on `mapproj_cub`; it now always writes to a fresh temp path and lets the final POSIX rename replace any prior output atomically. Split `lunaserv.fetch_dem_and_ortho` into `fetch_dem` (the DEM fetch) + `fetch_and_shade_ortho` (the ortho-shading step, taking `fetch_dem`'s own `DemFetchResult` -- `bbox`/`width`/`height` -- as input rather than re-deriving it), with `fetch_dem_and_ortho` itself kept as an unchanged-signature composing wrapper.

**Deviation from the plan's original phrasing, flagged rather than silently made**: the plan said `fetch_dem` should take "no footprint parameter -- its value is fully determined by the entry," eliminating `extra_footprint_lonlat_deg` entirely as the structural fix for the Phase 78 DEM-filename-clobbering bug (`docs/plan.md`'s own open item). Investigating the real blast radius found this would require either a public-API signature change (`session.py`'s `fetch_dem_and_ortho(camera)` convenience wrapper has no `frame_timing` to derive the footprint from) plus re-executing three notebooks that call the function directly, or a resume-check redesign (a bbox-derived filename hash, requiring `TrnTestEntry.dem_ortho_result`'s resume check to recompute the same bbox) -- both real, but bigger than this pass's budget given a user-flagged token-budget concern mid-session. Kept `fetch_dem_and_ortho`'s exact prior signature/behavior; the split still delivers real value (registry legibility, atomicity) but the DEM filename-collision gap itself remains open, documented explicitly in `fetch_dem`'s own docstring rather than claimed fixed.

Not retrofit with atomic publishing: `hole_fill_dem` (`dem_mosaic`'s own `-o <prefix>` convention appends `-tile-0.tif` to whatever prefix it's given, not the exact literal path) and `render.run_sat_sim` (`sat_sim`/`cam_gen`'s `-o` has the same prefix-suffix issue) -- both documented in place as known, deliberate exceptions, not silently skipped.

**Phase 5, live-validated**: a real `populate_via_workers(product_types=("crop", "hillshade"), workers=2)` run against two never-before-generated manifest rows (`M1327216343CE`/`M1327216889CE`, picked for moderate latitude after an earlier attempt with polar/marginal rows hit unrelated pre-existing data-coverage failures -- a real `spiceinit` error on one row and a real "beyond WAC_EMP's 60 deg coverage" `ValueError` on another, neither a concurrency bug, just bad row choices for this test), *without* the `docs/batch-generation.md` "don't mix product types in one batch" sequencing workaround. Consumer log confirms genuine concurrency, not just luck: one entry's own `crop` and `hillshade` tasks started 27ms apart on two separate worker processes and ran overlapping for the next 27-40s. Both product types for both entries completed `done`, no errors. `docs/batch-generation.md` updated: the mitigation section now reads "no longer a correctness requirement," with the sequencing snippet kept only as a throughput suggestion for large batches.

**Verified, per-phase as it landed, not deferred to the end**: fast suite (17 new registry/atomic-publish tests, 4 new path-shape tests across `test_isis_wac_dem.py`/`test_trn_dataset.py`/`test_sfs_validation.py`, full 321-test suite green except the same pre-existing unrelated worker-subprocess flake reconfirmed present on the unmodified Phase 1-2 baseline via `git stash`) after every phase; the real heavy suite (`test_wac_emp_ortho_source.py` x3, `test_sfs_validation_lambertian_incidence.py`, `test_lunaserv_campt_validation.py`) after Phase 4, which is what caught the `atomic_publish_path` extension bug above; the real `populate_via_workers()` run for Phase 5. `trntest-lint` clean throughout, one commit per phase (4 commits total on `feature/intermediate-product-discipline`, not yet merged to `main`).

**Not done in this pass**: the DEM filename-collision gap (above). Extending `@writes_product`/`@reads_product` and `atomic_publish` to the dataset's *published* final outputs (`TrnTestImage.raster_path`/`sidecar_json_path` -- `crop/`/`hillshade`/`reproject/`) -- flagged by the user during planning as a real gap in the discipline doc's own "non-final intermediates only" scope, audited directly (no current code reads a sibling's *published* copy rather than its private scratch state, so no live bug today, but the invariant is unenforced) and deliberately left as a distinct, not-yet-scheduled follow-up rather than folded in unannounced. The four notebooks Phase 78 already left stale (`along_track_correction`/`real_hapke_params`/`reproject_spike`/`pose_alignment_spike`) are still stale. `hole_fill_dem`/`run_sat_sim`'s atomic-publish gaps (above).

## Phase 80 (2026-08-23) — Closed the remaining Phase 4 atomic-publish gaps; found and fixed two more real concurrent-worker races the same day's Phase 79 validation hadn't actually exercised

Same session, continuing directly from Phase 79. The user asked which loose end to pick up next; picking between the DEM-filename-collision gap and finishing the DEM/ortho concurrency validation, this session flagged a real observation: Phase 79's own `populate_via_workers()` validation used `product_types=("crop", "hillshade")`, but `crop` never touches `entry.dem_ortho_result` (only `hillshade`/`reproject` do) -- so it never actually had two workers concurrently calling `lunaserv.fetch_dem`/`fetch_and_shade_ortho` for the same entry, despite `entry.dem_ortho_result`'s exposure being the *other* motivating example in the plan's own opening paragraph, alongside the ISIS-scratch race Phase 79 did fix and validate. The user agreed to close that gap.

**New `product_registry.atomic_publish_prefix`**: for ASP/ISIS tools that take an output *prefix*, not an exact path, and append their own fixed suffix to it (`dem_mosaic`'s `<prefix>-tile-0.tif`, `sat_sim`'s `<prefix>-<camera_stem>.tif`) -- neither fit `atomic_publish_path`'s exact-final-path contract, the reason both were left un-retrofit in Phase 79. Applied to `lunaserv.hole_fill_dem` and `render.run_sat_sim` (whose `cam_gen` step, an exact-path `-o`, uses plain `atomic_publish_path` instead).

**Real bug #1, found by the first `product_types=("hillshade", "reproject"), workers=2` validation run**: `lunaserv._hapke_reflectance` wrote fixed-name scratch cubes (`hapke_from.cub` etc.) directly to `config.output_dir` -- shared across the whole entry, not scoped to one call. Two workers computing the same entry's `hillshade` and `reproject` concurrently both reach this function and raced on those cubes, confirmed live (`**I/O ERROR** Failed to write blob`). Fixed with a call-scoped `tempfile.TemporaryDirectory()` -- exactly what Phase 2's own plan specified for this function but never actually implemented. Also dropped the now-fully-unused `config` parameter from `_hapke_reflectance`/`reference_hapke_reflectance`.

**Real bug #2, found by the same validation run after fixing bug #1**: a *different*, unrelated race in `camera.build_camera`'s own CK-kernel-resolution path (`isis_wac.resolve_wac_ck_kernels`/`_spiceinit_vis_even_cube`), hit because `crop`/`hillshade`/`reproject` all build a camera, not just the DEM/ortho path. `_spiceinit_vis_even_cube`'s idempotency check (`vis_even_path.exists()`) treated a file's mere existence as "fully processed," but `run_lrowac2isis`'s raw output becomes visible the moment `lrowac2isis` finishes, before `spiceinit` has run on it -- a concurrent worker landing in that window got back a not-yet-spiceinit'd cube and crashed reading its absent `InstrumentPointing` label (`KeyError`, confirmed live, twice, with two different fresh manifest rows before the fix). A second, related exposure: `run_lrowac2isis` itself writes 4 real output files from one subprocess call, not atomically, so `run_pipeline`'s own "`vis_even` and `vis_odd` both exist" reuse check could also observe a partially-written set. Fixed both: `run_lrowac2isis` now builds under a call-scoped temp subdirectory of `_spike_dir` (same filesystem, required for atomic rename) and publishes all 4 outputs via individual atomic renames; `_spiceinit_vis_even_cube`'s reuse check now verifies actual completion (`Kernels.InstrumentPointing` present, a new `_is_spiceinit_complete` helper) instead of bare existence. **Not fixed, documented instead**: two workers both calling `run_spiceinit` on the exact same physical file at the same moment remains a narrower, unconfirmed theoretical risk -- an existing, deliberate design tradeoff already in this codebase's history (`run_pipeline`'s own docstring: `spiceinit` is idempotent to rerun serially, "never specially guarded"), not something this fix set out to redesign.

**Verified**: fast suite green throughout (22 new tests across `test_product_registry.py`/`test_isis_wac_dem.py`, only the same pre-existing unrelated worker-subprocess flake), the same 5 heavy tests pass against real ISIS/network after each fix, and the `("hillshade", "reproject"), workers=2` validation -- which failed with two different real bugs, twice, against three different pairs of fresh manifest rows -- finally completes cleanly on genuinely fresh entries, with the consumer log confirming real overlap (same-entry tasks starting 7ms apart, running 50-80s concurrently). Two commits on a new branch, `feature/dem-ortho-atomic-race-fix`, off `main` (Phase 79's branch was already merged and pushed).

**Not done in this pass**: the DEM filename-collision gap is still open (unchanged from Phase 79). The narrower two-workers-spiceinit-the-same-file risk noted above is left open, not fixed. Phase 6 (published-output registry coverage) remains not-yet-scheduled.

## Phase 81 (2026-08-24) — Moved task granularity from `(entry, product_type)` to `entry`

Follow-on to Phases 79-80's atomic-publish work, same week: those phases made two of the same
entry's product types landing on two different `-k process` workers *safe* (atomic publishing
converges on an equivalent result instead of tearing a file), but didn't address the other real cost
of that granularity -- `functools.cached_property` only memoizes within one process, so `hillshade`
and `reproject` of the same entry, landing on two different workers, each independently,
redundantly rebuilt the same real SPICE/ISIS/DEM state for that entry (`entry.camera`/
`entry.dem_ortho_result`). Confirmed live as a real, measurable wall-clock cost, not just a
theoretical inefficiency, in a `("hillshade", "reproject")` batch.

**Fix**: `tasks.py`'s `_generate` (took one `TrnTestImage`) replaced by `_generate_entry(entry,
product_types)` -- one huey task per *entry*, covering every requested-and-still-pending product
type for it, each attempted independently within the task (one's failure doesn't block the others).
A new `EntryGenerationError` carries every product type's exception (`{product_type: Exception}`),
raised only after all types in the task were attempted, so a task with one real failure and one real
success doesn't lose or misreport the success. `tasks.task_id(dataset_folder, product_id)` drops its
`product_type` parameter -- one deterministic id per entry, not per `(entry, product_type)`.

`trn_dataset.py` updated to match: `task_state()`'s stored-huey-result fallback and
`_clear_stored_result()` are now keyed per entry; `_enqueue_pending()` builds each entry's own
pending-product-type subset and enqueues one task covering it (`task_fn.s(entry, pending_types)`,
not `task_fn.s(image)` per type); `populate()`/`populate_via_workers()`'s `retry_failed` clearing and
`truncate()`'s stored-result clearing collapsed from a per-product-type loop to one per-entry call.
`task_state()`'s own `image.exists()`-first precedence means per-product-type `done`/`pending`
reporting stays exactly correct despite the underlying stored result now being entry-level -- the
real, accepted tradeoff is coarser `failed`-attribution when more than one product type in the same
task didn't complete (can't tell from the stored result alone which of the task's several types
failed, only that at least one did).

**Real side effect, not just a performance win**: this also structurally eliminates the specific
same-entry-cross-worker race Phases 79-80's atomic-publish fixes made *safe* -- two of one entry's
product types can no longer land on two different workers at all now, since they're always in the
same task. `docs/batch-generation.md`'s "Mixing product types across concurrent workers" and
`docs/environment.md`'s `run_isd_generate` same-dataset-folder-race note both updated to reflect
this. The atomic-publish work itself stays valuable regardless -- for genuine cross-entry write
collisions and crash/partial-write safety, not just this now-eliminated race.

**Verified**: fast suite green (only the same pre-existing unrelated worker-subprocess flake), plus
a direct manual repro confirming a `crop` failure doesn't block `hillshade`'s own success within one
entry's task. `docs/plan.md` (`tasks.py`/`trn_dataset.py` rows) and `docs/batch-generation.md`
updated to describe the new granularity. (`docs/dataset-plan.md`/`docs/intermediate-product-plan.md`
themselves were removed in a same-day follow-up -- see Phase 82.)

## Phase 82 (2026-08-24) — Removed the superseded `dataset-plan.md`/`intermediate-product-plan.md` design docs and swept their references

Same-day follow-up to Phase 81. Both docs were fully-implemented, historical planning documents --
`dataset-plan.md`'s design (`TrnTestDataSet`/`TrnTestEntry`/`TrnTestImage`, the original filesystem
task queue) had long since been superseded in practice by `docs/plan.md`'s own architecture table
and the `huey` migration (Phase 66); `intermediate-product-plan.md`'s Phases 1-5 were fully
implemented and validated by Phase 79. Neither was in `AGENTS.md`'s list of docs kept current, and
both were carrying stale internal claims of their own (`dataset-plan.md`'s "Status: designed, not
yet implemented" header, in particular, hadn't been true for many phases). `git rm` both.

**Swept the ~30 references to them** across `src/trntest/` (`trn_dataset.py`, `tasks.py`,
`product_registry.py`, `sfs_validation.py`, `isis_wac.py`, `lunaserv.py`), `tests/`
(`test_product_registry.py`, `test_isis_wac_dem.py`, `test_sfs_validation.py`,
`test_trn_dataset.py`), `docs/` (`plan.md`, `data-sources.md`, `batch-generation.md`,
`environment.md`, `reproject-fov-investigation.md`, `report-plan.md`), and the notebook markdown
cells in `image_generation.py`/`select_datasets.py`/`reproject_spike.py` -- each redirected to
whichever current doc actually carries that fact now (mostly `docs/plan.md`'s architecture table,
`docs/data-sources.md`'s on-disk-layout section, or a specific `docs/history.md` phase entry in
place of the removed doc's own internal phase numbering, since that numbering no longer resolves to
anything). `docs/history.md`'s own narrative mentions of both files (there are many, e.g. Phase
66/79's own entries) were deliberately left as-is -- they're accurate historical statements about
what existed at the time, consistent with this file's own framing as a narrative log, not a
currently-synced reference.

**Notebooks**: `image_generation.py`/`select_datasets.py` had their markdown-cell prose updated the
same way, then each was re-run via `scripts/run_notebook.sh` to keep its `.ipynb` pair in sync
(structural-sync is part of `trntest-lint`) -- real re-execution, not just a text edit, since these
are jupytext-paired notebooks with committed, executed output. Both completed cleanly.
`reproject_spike.py`'s own re-run hit a real, already-documented, pre-existing gap instead --
`docs/plan.md`'s own open items already flag this notebook (along with
`along_track_correction`/`real_hapke_params`/`pose_alignment_spike`) as not regenerated since Phase
78's `ortho_source="wac_emp_pds"` default change, and its candidate row's footprint (latitude
51-60 deg) exceeds WAC_EMP's 60 deg equirect coverage under that default -- unrelated to this
phase's doc sweep. Rather than force a real pipeline fix or leave a half-executed notebook
committed (papermill's incremental `--request-save-on-cell-execute` had already written a
partial run with the raised exception baked in as a cell output before failing), the partial
`.ipynb` was discarded (`git checkout --`) and `jupytext --sync` used instead to update just the
one changed markdown cell's text in the `.ipynb`, leaving its existing (already-stale, already-known)
outputs/execution counts untouched -- keeps structural sync (what `trntest-lint` actually checks)
without pretending the notebook's own real output freshness gap is resolved.

**Also found and fixed a real, separate bug in Phase 81 itself, unrelated to the doc sweep**:
`tests/test_trn_dataset.py::test_generate_product_parallel_runs_in_a_real_worker_subprocess`
asserted `str(value) == str(marker_path)`, but `_generate_entry` (Phase 81) always returns
`{product_type: raster_path}`, not a bare path -- confirmed via `git show` that this assertion was
never updated when Phase 81's own commit changed the call to pass `("fake",)`, so it's been failing
deterministically since that commit, not something this phase's edits caused. Fixed to
`str(value["fake"]) == str(marker_path)`.

**A second, real but separate finding, not fixed here**: verifying the above turned up that this
project's fast suite can fail on a *second* consecutive `docker compose run --rm demo pytest`
invocation against the same worktree -- `tasks.py`'s module-level `huey`/`huey_parallel` instances
are keyed to `load_config().output_dir`, which is bind-mounted to this worktree's real,
host-persistent `output/` directory (survives across ephemeral container runs), while pytest's
`tmp_path` naming is deterministic per test function in a fresh container. A test that intentionally
records an entry-level "failed" result (`test_populate_marks_failed_and_continues`) leaves that
record behind under a task id a later invocation's `tmp_path` reproduces exactly, and since Phase
81 moved to one shared stored result per *entry*, that stale record now taints every product type
sharing that entry's task id, not just the one that actually failed -- `_enqueue_pending()` sees
nothing "pending" and never re-enqueues, so a product type that would otherwise succeed fresh never
gets attempted. Confirmed directly: `rm -rf output/<worktree>/.huey` before a run makes the whole
fast suite pass cleanly every time; without it, a second consecutive run reliably fails two tests.
Flagged as a follow-up task (not fixed in this phase -- a real test-isolation fix, out of scope for
a docs-focused change) rather than silently worked around.

**Verified**: `trntest-lint` clean (source/tests/notebooks); fast suite green (323 passed) with
`.huey` cleared immediately beforehand, per the finding above.

## Phase 83 (2026-08-26) — Crater sharpness grading: `stoffler_fresh_depth_km`, the tiled whole-database `crater_depth_batch.py` precompute, and its real multi-worker path

Continuation of the `crater_depth.py` work from two sessions ago (see that entry's own dated addition in `docs/plan.md`) -- picked up in a fresh session that first had to work out *where* the prior work actually was. The session opened confused: `docs/plan.md` had no reference to `crater_depth.py` on the branch it started on, because that branch (`claude/crater-sharpness-grading-de0582`) turned out to be a disconnected sibling worktree with unrelated commits -- the real, uncommitted work was sitting in a different sibling worktree (`-dad2c9`) that a separate Claude Desktop session had left mid-task. Found via `EnterWorktree` once the mismatch was diagnosed (checking every branch's committed history first, which came up empty, before realizing the work was *uncommitted* and thus invisible to any commit-history search); the first real step was just running the existing tests to confirm the prior session's own recorded pass rate still held, then committing that work as its own clean commit before starting anything new.

**`crater_depth.stoffler_fresh_depth_km`**: the reference-depth half of an actual sharpness grade, per Stoffler et al. 2006's classic two-regime lunar depth-diameter relation (simple craters `0.196 * D^1.010`, complex craters `1.044 * D^0.301`, crossing at ~10.58 km). Implemented as `min()` of the two raw formulas rather than an explicit branch on the crossover, after working out live that this is provably exact, not an approximation: since the simple-crater exponent is the larger one, the complex-crater curve decays more slowly and dominates as `D -> 0`, the simple-crater curve overtakes it exactly once at the crossover and stays larger beyond it -- so the two curves cross exactly once for `D > 0`, and their elementwise minimum reproduces the textbook piecewise form on both sides with no branch, continuous by construction. A derived (not independently hardcoded) `STOFFLER_CROSSOVER_DIAMETER_KM` constant keeps the documentation value from drifting from the two curves it describes.

**Design discussion, then `src/trntest/crater_depth_batch.py`**: the user wanted to precompute depth for the *whole* database (not just per-camera-footprint, `crater_depth.py`'s existing scope) and specifically asked how to handle craters spanning tile boundaries when working tile-by-tile for cache coherence. The resolved design deliberately separates two concerns that get conflated if handled as one: *ownership* (which tile computes a given crater -- purely the crater's own center point falling in a tile's nominal, unpadded bounds, matching `craters.py`'s own center-point spatial index) from *raster extent* (how much DEM a tile actually reads -- an independently-tunable, larger *padded* bbox). A crater whose real ellipse doesn't fit even the padded raster gets `depth_m=None`, kept as a row not dropped -- the user's own explicit call ("This is fine for now... the really big ones are quite rare anyway"), with both tile sizes left as fixed global constants (2 deg nominal / 3 deg padded defaults) rather than sized per-tile from the data. Output is one small CSV file per tile (not one growing table, and not Parquet -- see below), atomically published under a directory whose own name encodes the tuning parameters, `load_graded_database` concatenating them back for querying.

**A real correctness bug caught before it could ship**: `lunaserv._reproject_raster_to_local_grid`'s own raw output writes real gaps as literal `NaN` but never sets the file's own `nodata` tag. Invisible in the existing per-camera pipeline because `lunaserv.fetch_dem` always runs the reprojected DEM through `hole_fill_dem` first (which is also what sets a proper `nodata` tag) -- the first version of the tiler skipped that step, which would have silently leaked `NaN` into `crater_depth_m`'s percentile computation as if it were real elevation. Caught by reasoning through the existing pipeline's own call graph before writing a test that could have missed it, not by a failing test surfacing it after the fact. A second, much smaller bug -- the test fixture's own synthetic rim annulus was positioned *inside* the crater's true radius instead of straddling it, so the first real end-to-end test failed with `depth_m=None` -- was root-caused by directly reproducing `crater_depth_m`'s own masking logic step by step in a scratch script rather than guessing at fixes.

**A dependency choice made, found wrong, and corrected rather than left standing**: added `pyarrow` for Parquet output, then a full-suite run failed an unrelated real-subprocess test (`test_generate_product_parallel_runs_in_a_real_worker_subprocess`) that had been passing. Reproduced the failure against a *pristine* checkout with only `pyarrow` added via a temporary `git worktree add` isolation test, concluded `pyarrow` was the cause, and switched the output format to plain CSV to avoid it (also updating the module's own docstring to state this as fact). Further investigation for the *parallel-worker* task below then found the real cause: `tasks.huey_parallel`'s sqlite queue file lives under `<output_dir>/.huey/`, bind-mounted to a *persistent* host directory for this worktree, and running that one test repeatedly across this session's own debugging had accumulated stale queue state there -- deleting `.huey/` fixed it immediately, with `pyarrow` still absent, and a repeat of the earlier isolation test showed the *first* run against a fresh output dir always passed regardless of `pyarrow`, only a *second* run in the same dir failed. The `pyarrow`-caused-it claim was corrected in the module's own docstring rather than left standing once disproven (`docs/history.md`'s own Phase 79 entry, read only afterward, already noted this same flake as pre-existing and unrelated to that phase's own changes -- this session re-discovered, not discovered, it). CSV was kept anyway on its own real merits (no new dependency for a small, simple-schema table), not because the original reason turned out to hold.

**Real measured timing, not a guess**: 10 tiles sampled pole-to-pole against real GLD100/Robbins data fit a cost model of ~2.2s/tile fixed overhead (mostly the `dem_mosaic` hole-fill subprocess) plus ~0.014s/crater. Scaled against the real tile-grid size (14,220 tiles) and the real in-coverage crater count (1,250,659, queried directly, not estimated): **~13.6 hours single-threaded for the whole grid** -- slower than the earlier session's naive ~6.9-hour per-crater extrapolation, since that number never accounted for real per-tile reprojection overhead at all, only crater count (the batched approach's *marginal* per-crater cost, ~0.014s, is genuinely cheaper than the naive loop's 19.4ms, but the fixed overhead dominates and more than offsets that win at this tile size).

**`grade_database_via_workers`**: the real multi-worker path, asked for once the 13.6-hour single-threaded number was in hand. Mirrors `trn_dataset.TrnTestDataSet.populate_via_workers`'s own established pattern exactly rather than inventing a new one -- generalized `tasks.start_consumer` to take a `huey_module` argument (was hardcoded to `trntest.tasks.huey_parallel`) so this reuses the existing subprocess-management machinery instead of duplicating it for a second task domain, and added a dedicated `huey_crater_depth` instance (own sqlite file, own task) rather than sharing `tasks.huey_parallel`, which is bound to a different task's argument shape. The real grade-and-publish body is factored into a plain `_grade_and_publish_tile` helper shared by the sequential and parallel paths and directly unit-testable without a live consumer -- the same shared-helper-plus-thin-decorated-wrapper idiom `tasks._generate`/`generate_product`/`generate_product_parallel` already established. Live-validated end to end (not just via mocks): 6 real tiles graded in 6.5s wall-clock with 3 workers vs. ~11.3s sequential for the same 6, and real resumability confirmed across the sequential/parallel boundary (a `via_workers` call correctly skipped tiles an earlier sequential call had already written).

**Investigated and rejected as a shortcut**: a third-party HuggingFace dataset (`huggingface.co/datasets/juliensimon/lunar-craters-robbins`) claiming to be Robbins craters plus a pre-computed `depth_km` column, surfaced by the user mid-session as a possible alternative to the whole precompute above. Direct inspection found real red flags: its `crater_id` values don't exist in this project's own cached Robbins GeoPackage at all (no usable join key), position/diameter matching found no real correspondence for several "giant" (400-1,100 km) craters against this project's own verified data, one row has an outright impossible value (latitude outside +-90 deg, a diameter bigger than the Moon itself), and the dataset card doesn't explain how `depth_km` was actually derived. Recorded in `docs/data-sources.md` so a future session doesn't re-spend time on the same dataset.

**Tests**: 5 new tests for `stoffler_fresh_depth_km` (both regimes, exact crossover continuity, vectorization); `crater_depth_batch.py` grew from 10 to 19 tests covering the tile grid/bounds math, the empty-tile short-circuit, a real end-to-end DEM-reprojection-through-`crater_depth_m` case (both a fitting and an oversized crater in one tile), sequential resumability/`limit`, and the parallel path's own orchestration (enqueue count, resumability, `limit`, no-consumer-started-when-nothing-pending) via mocks, plus a direct real end-to-end smoke test of the actual worker-subprocess path. `trntest-lint` clean throughout; full suite green (336 tests) once the pre-existing `.huey/` state issue above was understood and cleared.

**Flagged, not fixed in this pass**: the pre-existing worker-subprocess test flake (a `spawn_task` chip was raised for it, redundant with Phase 79's own prior note but still unresolved) -- root cause still not nailed down beyond "stale/leftover state across repeated runs against the same persistent `.huey/` directory," which could be the ordering issue between `test_start_stop_consumer_lifecycle` and the failing test, the cross-run persistence issue, or both.

## Phase 84 (2026-08-26) — The actual sharpness score, `consolidate_graded_geopackage`'s join, and a real review notebook

Direct continuation of Phase 80, same session. Two design conversations with the user first, each landing on a concrete choice: (1) whether to build a final post-processing step that joins measured depth onto the main Robbins database, once its likely scale was measured (full depth-only table ~180MB as CSV, ~10-20MB as Parquet; the joined "fat" table well under the source GeoPackage's 374MB) -- yes, and (2) GeoPackage over Parquet for that joined artifact specifically, since the user confirmed per-footprint queries are the more common case and GeoPackage lets every existing bbox-query function (`craters.query_craters_in_bbox` and everything built on it) work against it unchanged, reusing the real spatial index rather than a lean-but-index-less Parquet file. `crater_depth_batch.consolidate_graded_geopackage` implements this: left-joins `load_graded_database`'s combined depth table onto the full Robbins `GeoDataFrame` by `CRATER_ID`, atomically publishes it as its own GeoPackage. A snapshot, not auto-synced.

**The user then gave the actual sharpness formula directly**: `depth / stoffler_fresh_depth(diameter)`, units matched. `crater_depth.sharpness_ratio` implements it (meters converted to km before dividing); `NaN`/`None` `depth_m` propagates to `NaN` with no special-casing (confirmed live: `np.asarray(None, dtype=float)` is already `nan`). `consolidate_graded_geopackage` computes and stores it as a `sharpness` column -- cheap, so recomputing it on every consolidation costs nothing, unlike the depth measurement itself.

**A real notebook to review it, per the user's own request** (`notebooks/crater_sharpness_review.py`/`.ipynb`) -- reusing `image_generation.ipynb`'s Phase 1-2 but skipping `dataset.populate()` entirely, the same minimal-setup pattern `hapke_hillshade.ipynb` already established (`entry.dem_ortho_result` is enough). Needed one new precompute entry point first: `crater_depth_batch.grade_footprint` grades just the tiles touching one candidate's real footprint rather than the whole database, via a new `tiles_covering_bbox` (snapped to the same grid `iter_tile_origins` defines, so tiles it grades are indistinguishable from ones a full `grade_database` run would reach -- same resumability, same filenames). `craters.query_craters_for_raster`'s own bbox-deriving logic was factored out as `craters.raster_bbox_deg` (unpadded) specifically so `grade_footprint` didn't have to duplicate it -- a second real caller, not just a hypothetical one.

**Two real, live iterations on the notebook's own plots, not a one-shot success**:
- The depth-vs-diameter 2D histogram's first (linear-binned) version was tried, run for real, and found genuinely unreadable -- one bin near the smallest diameters/depths held the vast majority of the 3,565 real graded craters, visually swamping the rest. Switched to log-log axes and log-spaced bins (matching the standard way this kind of power-law crater data is presented in the literature, not just a fix for this plot's own range) -- the re-run result is a real, informative validation signal: the bulk of the population clusters at or below the Stoffler reference curve at small diameters, which is physically sensible (most real craters in a random sample are somewhat degraded, not freshly formed), not just "the code runs."
- The sharpness-colored crater overlay (same hillshade base/sparse-dashed style as Phase 5B/6B, colored by `sharpness` instead of one fixed color -- `GeoDataFrame.boundary` returns a bare `GeoSeries` with no attribute columns, so the boundary geometry has to be rebuilt into its own `GeoDataFrame` with `sharpness` reattached before `.plot(column=...)` has anything to color by) was, at first, too dense to read as individual crater shapes -- the unfiltered population at this one footprint's scale (thousands of small craters) collapsed the sparse dashes into a fuzzy cloud. Sent both real plots to the user for a look (not just described in prose) before iterating further. Follow-up user feedback, each applied and re-verified against a real re-run: axes in km via a tick formatter (not by rescaling the underlying geometry, to avoid a raster/vector unit mismatch), the query's own 5%-padding-beyond-the-frame craters suppressed from *display* via `ax.set_xlim`/`set_ylim` (not re-filtering the query, which still wants that padding for craters whose center sits just outside but which still overlap the frame), a `min_major_km` filter (mirroring Phase 5B/6B's own name/convention) raised from 3.0 to 6.0 km, and the histogram's "(log scale)" axis-label text removed (redundant with the visibly log-spaced ticks) along with fixing real tick-label collision on the diameter axis (the default log-axis locator/formatter packs in enough scientific-notation minor-tick labels to overlap at this data's < 2-decade range -- fixed with an explicit 1/2/5-per-decade `LogLocator` + plain-number `ScalarFormatter` + minor labels off).

**Tests**: 4 new `sharpness_ratio` tests (matches the reference depth exactly at ratio 1.0, scales correctly, `None` propagation, vectorization), a real join test for `consolidate_graded_geopackage` (one graded + one ungraded crater, confirms no duplicate/suffixed columns and correct `NaN` handling), `tiles_covering_bbox`/`grade_footprint` tests (grid-snapping correctness, real touching-tiles selection with resumability). `trntest-lint` clean throughout; full 343-test suite green (`.huey/` cleared first, per Phase 80's own finding).

**Not done in this pass**: the notebook/`consolidate_graded_geopackage`/`grade_footprint` are not yet committed as of this entry -- held for the user's own review in Jupyter Lab first, per this repo's standing convention for notebook-output changes.

## Phase 85 (2026-08-29) — `docs/docs-style.md`: a style guide for docs and docstrings

The user flagged two long-standing problems: docstrings across the codebase (`lunaserv.py` worst of
all) had grown into implementation walkthroughs and dated justification trails rather than interface
definitions, and nearly every doc/docstring in the repo cites `docs/history.md`'s "dated entries" --
a scavenger hunt through a 4000+ line narrative log that `AGENTS.md` already says shouldn't be
required reading. `grep`ping the repo confirmed the scale: 50+ `docs/history.md` references in
`lunaserv.py` alone, plus `dataset.py`, `camera.py`, `plotting.py`, `data-sources.md`, and others.

`docs/docs-style.md` (touchstone: [Google's docguide best
practices](https://google.github.io/styleguide/docguide/best_practices.html)) now states: a
docstring defines the interface (summary, args, returns, exceptions), not the implementation, not
the development history, and not a sharp edge that should just be fixed instead; overflow material
belongs in a comment near the code it explains (inside the function body for implementation detail,
above it for whole-function rationale), an overview/tutorial doc, or nowhere; and nothing outside
`docs/history.md` itself should cite it. A "Voice" section, prompted by the user's own framing, names
the root cause directly: write like a taciturn developer, not a chatty one -- the existing verbosity
is a voice problem, not just an absence of pruning. Indexed in `AGENTS.md` alongside the other
`docs/*.md` files. This phase only adds the guide; applying it across the existing codebase is
follow-up work.

## Phase 86 (2026-08-29) — First two docs-style edit passes, and three more style rules from doing them

Same session as Phase 85, continued. Applied the new style guide for real, on two candidate files,
rather than leaving it untested:

- `docs/intermediate-product-discipline.md` first -- chosen as a stable, unlikely-to-collide file,
  but turned out to already be fairly disciplined prose; the edit mostly split long em-dash-stacked
  sentences into shorter ones, with only a modest net line reduction. The user found this
  underwhelming and asked for a second candidate more likely to have real bloat to cut.
- `docs/caching.md` next -- a much better hit: a full incident narrative (trimmed to one clause with
  the concrete number), a `docs/history.md` citation (removed, the underlying fact stated directly
  instead, per Phase 85's new rule), "confirmed empirically"/"deliberate, explicit resilience"
  flourishes, and stale before/after historical framing ("used to... now persisted instead") that
  doesn't matter for current behavior. Cut from 177 to well under 150 lines with no facts lost.

Reviewing both passes, the user proposed three more rules, added to `docs/docs-style.md`: **one
source of truth per fact** (prune duplicated facts to wherever the reader who needs them is most
likely to look, not just wherever they were first written; a cross-reference, if one's needed, must
resolve in one hop, not "see this 1000-line file"); **keep index files thin** (`AGENTS.md`/
`docs/plan.md` should hold only enough to tell a reader whether they need to go read the real thing,
since nearly every session pays their read cost -- real content that accumulates there should move
to the file it points to); and **file naming** (a filename is often the only thing a reader sees
before opening it -- name for current content, not the task/phase that produced it, and rename on
drift).

**A real stale-fact catch during review, unrelated to the style pass itself**: reviewing
`docs/caching.md`'s edit, the user flagged its "Archive/restore cost" paragraph (Astropedia GLD100's
~10GB archive-tarball impact) as obsolete -- the VPS's main `trntest_ws` data store is no longer torn
down and archived/restored between sessions the way `docs/environment.md` describes; `archive.sh`/
`restore.sh` are no longer used. Removed that paragraph outright. `docs/environment.md` (built
entirely around that now-stale "ephemeral VPS, archive/restore" framing) and at least one reference in
`docs/data-sources.md` are also affected -- the user chose to defer that fuller rewrite rather than
do it in this pass, so only `AGENTS.md`'s own doc-index blurb for `docs/environment.md` was patched
with a note flagging the staleness, not a rewrite. `docs/environment.md`'s real rewrite is still owed.

## Phase 87 (2026-08-29) — Split `docs/data-sources.md` (1506 lines) into an index-pattern doc family

Same session, continuing the docs-style effort. `docs/data-sources.md` had grown into a single file
mixing three genuinely different kinds of content: external *data* facts (endpoints, formats,
coverage), external *tool/library* behavior (ASP, ISIS, `usgscsm`, LightGlue -- not data, but the same
kind of "don't re-derive this" reference), and pure internal architecture/algorithm notes that had
nothing to do with external dependencies at all. Discussed with the user across several turns before
touching anything: agreed the file was a real index-pattern candidate, then worked out where each of
its 19 sections actually belonged (a few turned out to be split-worthy themselves, mixing two of the
three kinds in one section) before executing.

**Result**: `docs/data-sources.md` itself shrank from 1506 lines to a 25-line index -- one table
(data type / source / example uses / rationale, the shape the user proposed) linking out to 7 new
per-source files under `docs/data-sources/` (`lunaserv-wms.md`, `astropedia-gld100.md`,
`wac-emp-pds4.md`, `robbins-craters.md`, `spice-kernels-naif.md`, `spice-kernels-isis.md`,
`lroc-wac-edr-cdr.md`). A new `docs/external-tools.md` (529 lines) collects the tool-behavior half:
ASP `sat_sim`/`mapproject`, the whole ISIS Pushframe `cam2map`/`mapproject` pipeline (install
gotchas through the even/odd-parity-is-temporal-not-spatial root cause), the `usgscsm`
`groundToImage` bug, the crop ISD sidecar's accuracy investigation, LightGlue, and both `campt`
gotcha sections. Three new plan.md-family docs took the pure-architecture material: `docs/crater-
grading.md` (Breton et al. depth method + the whole-database batch precompute -- kept out of
`docs/batch-generation.md` since it's a genuinely different worker-pool subsystem, a different
dataset), `docs/image-pipeline.md` (crop sizing/pose epoch/tie points + the WAC-VIS boresight
finding, which turned out to literally be that section's own appendix already), and `docs/dataset-
selection.md` (the LRO maneuver-detection algorithm). `TrnTestDataSet`'s on-disk layout facts were
folded into `docs/intermediate-product-discipline.md` instead of getting a new file, as a concrete
worked example of that doc's own principles.

**Cross-reference cleanup, the unglamorous but necessary part**: every doc/code comment that named a
specific `docs/data-sources.md` section by heading (not just a bare "see docs/data-sources.md")
pointed at a heading that no longer existed there once this landed. Fixed ~12 in `docs/plan.md` and
~30 in `src/trntest/*.py` (`isis_wac.py`, `lunaserv.py`, `camera.py`, `craters.py`, `config.py`, and
others) plus a few in `docs/caching.md`, all mechanically redirected to the correct new file --
verified after the fact via a script confirming every markdown link in `docs/` resolves to a real
file, and that every touched `.py` file still parses. Bare, unnamed `docs/data-sources.md` pointers
were left alone (still resolve fine through the new index) rather than over-editing `plan.md`, which
the user has separately flagged as its own future index-pattern candidate. `docs/history.md`'s own
existing citations into the old section names were deliberately left untouched too -- they describe
the past accurately as of when they were written and were never meant to track current doc
structure.

## Phase 88 (2026-08-29) — `docs/proposed-tasks/` convention, and a status doc for the docs-rework effort itself

Same session, continuing directly. The user wanted a new convention: forward-looking plan docs
(as opposed to the current-state reference docs everywhere else in `docs/`) live under a dedicated
`docs/proposed-tasks/` folder, not loose in `docs/`. Asked to check for existing candidates first
rather than just being told which to move.

**Survey, then two judgment calls confirmed with the user**: of the four `-plan`/`-investigation`
named docs, `docs/report-plan.md` was a clean match (an explicit, still-incomplete design doc,
still cited from `docs/plan.md`'s own open items). `docs/corrected-overlay-cam2map-plan.md` was
ambiguous -- self-describes as a resumable implementation plan, but no longer referenced from
`docs/plan.md`'s current status text (that area was resolved a different way, a DEM shape-model
fix rather than the `ConstantRotation` cube patch this plan describes) -- still referenced from
`docs/wac-jigsaw-investigation.md`/`isis_wac.py`/`notebooks/pose_alignment_spike.py` though, so not
orphaned. User's call: move it anyway, as-is, and let a future reader judge whether to pick it up.
`docs/reproject-fov-investigation.md`/`docs/wac-jigsaw-investigation.md` were judged *not* matches
despite their names -- both are investigation records of already-merged work, now cited from
`docs/plan.md`'s architecture rows as background, not forward plans -- left where they are.

Moved both files (`git mv`), fixed every incoming reference (`docs/plan.md`,
`docs/wac-jigsaw-investigation.md`, `isis_wac.py`, `src/trntest/report.py`,
`scripts/render_report_template.py`, `notebooks/pose_alignment_spike.py` -- including its paired
`.ipynb`, via `jupytext --sync` rather than a full re-run, since it was a one-line text change with
no output to regenerate). Documented the convention itself as a new `AGENTS.md` doc-index bullet,
per the user's own preference for where.

**Then asked to document the docs-rework effort itself** as a proposed-task plan
(`docs/proposed-tasks/docs-style-rollout.md`), explicitly required to list which files already have
the rework done. Surveying honestly turned up a real gap: nearly all of the actual editing so far
went into `docs/*.md` (the style guide itself, `caching.md`, `intermediate-product-discipline.md`,
the `data-sources.md` split and its 4 new spinoffs) -- the *original* complaint, verbose/chatty
docstrings in `src/trntest/*.py`, has barely been touched. `grep -rc "docs/history.md"
src/trntest/*.py` found 18 files still citing it, `lunaserv.py` worst by far (41 citations, 1663
lines) -- recorded as a priority table in the new plan doc, with `docs/plan.md`/`docs/environment.md`
/`docs/batch-generation.md`/the two investigation docs also flagged as not yet given a style pass.

## Phase 89 (2026-08-29) — `docs/batch-generation.md`'s style pass, working straight down the priority list

Same session, continuing directly from the Phase 88 status doc's own "if resuming" list -- asked to
just continue to the next file. `docs/batch-generation.md` had the same two problems as the docs
already fixed: 6 `docs/history.md` citations, and one section ("mixing product types across
concurrent workers") that had grown into a full 3-phase investigation trail (a race found, fixed,
then made structurally impossible by a later task-granularity change) when the *current* fact is
simple -- task granularity is per-entry now, so that specific race can't happen at all, and atomic
publishing covers what's left (genuine cross-entry collisions, crash safety). Condensed that section
down to the current model instead of the history of getting there; kept every other gotcha
(cold-cache concurrent fetch races, orphaned consumer processes on a hard kill, where the consumer
log lives) as-is, since those were already tight single-paragraph callouts, not narrative. 179 -> 142
lines, zero `docs/history.md` citations. Updated `docs/proposed-tasks/docs-style-rollout.md` to
reflect it.

## Phase 90 (2026-08-30) — Notebooks tone/structure pass complete: all 11 notebooks, real bugs found along the way

Multi-session effort (individual per-notebook commits in git log) applying `docs/docs-style.md`'s
tutorial-tone rules to every `notebooks/*.py`/`.ipynb` pair, tracked in
`docs/proposed-tasks/notebooks-tone-pass.md` (now deleted, folded into `AGENTS.md`'s notebook
bullet per its own closing instructions). Same underlying problem as the parallel `src/trntest/*.py`
docstring pass: notebooks had drifted from tutorial markdown into development-history narrative
(`docs/history.md` citations, "Phase N" citations, tuning backstory, bare "real"/"genuine" filler)
-- cut throughout, one notebook at a time, each regenerated (`scripts/run_notebook.sh`) and held for
the user's own Jupyter Lab review before commit.

Two of the 11 needed more than a tone pass. `report_template.py` (a Jinja template, not paired with
an `.ipynb`) needed nothing at all -- already exactly what a template should be.
`reproject_spike.py` was archived to `old_notebooks/` instead of rewritten (its premise question,
"should we build `TrnTestReprojectImage`?", is stale now that `reproject` is fully implemented and
wired into `image_generation.py`'s Phase 8) -- see `old_notebooks/README.md`'s own new section for
the investigation summary.

Regenerating repeatedly surfaced real, live bugs unrelated to the tone edits, each found and fixed
in place: `along_track_correction.py`'s `basemap_and_diff` assumed two independently-fetched
rasters always shared a pixel grid (false; switched nearest+tolerance to linear interpolation) and,
separately, its `fetch_dem_and_ortho` call was missing `extra_footprint_lonlat_deg`, silently
clobbering the shared, footprint-suffix-less `dem_filled-tile-0.tif` -- a live hit of an
already-documented `docs/plan.md` open item, fixed to match its sibling call sites.
`sfs_validation.py`'s `sim_masked_path` pointed at the wrong directory after an earlier refactor
moved where `run_sfs_forward_render` actually writes. `isis_wac.apply_pose_correction_to_crop`'s
`csv2table` call passed a `coltypes=` argument the installed ISIS 9.0 no longer accepts -- that
version converts every CSV column to floating point unconditionally now, confirmed via the app's
own XML docs; dropped the argument and the now-dead `_INSTRUMENT_POINTING_COLTYPES` constant.

Also found, three separate times, a notebook's markdown asserting a "still open" investigation
status that had actually been resolved (or, in `pose_alignment_spike.py`'s case, stopped
reproducing, for reasons not investigated) elsewhere or later: `wac_isis.py`'s "unresolved
blocker," `sfs_validation.py`'s "still-open, unexplained regression" against the real WAC crop, and
`pose_alignment_spike.py`'s SIFT 6-DOF-pose regression (documented as a ~10x ground-space blowup;
the fresh run shows a modest improvement instead). Cut or corrected in place rather than left stale.

**A real bug the user caught by eye, not by the linter**: `pose_alignment_spike.ipynb`'s first
executed cell rendered in Jupyter's red error styling, even though `scripts/run_notebook.sh` had
exited 0 and `trntest-lint` passed clean. Root cause: the same class of bug
`old_notebooks/reproject_spike.py`'s own docstring already documents -- `dataset.populate(limit=1)`
on an already-populated entry 0 silently advances the queue to a *different*, not-yet-done manifest
entry instead of no-op'ing, and that entry (`M1327215525CE`) hit a real `WAC_EMP` polar-coverage
limit, logging an unhandled-exception traceback to stderr from a background `huey` worker thread --
never a raised exception in the main execution path, so papermill saw nothing wrong. Fixed by
removing the (unnecessary -- this notebook only ever touches `entry.crop_result`/
`entry.dem_ortho_result`/`entry.camera`, all self-healing) `populate()` call, matching
`reproject_spike.py`'s own precedent exactly.

The linter gap itself was real too: `_check_notebook_warnings` only flagged `output_type == "error"`
cells (a raised exception in the main thread) or stream text literally containing "Warning" --
neither matches a background-thread traceback logged to stderr. Fixed to flag any `stream` output
with `name == "stderr"` directly, the exact field Jupyter's own renderer keys its red styling off
of, independent of content. Considered the weaker alternative (grep stderr text for "error") and
rejected it after checking empirically: zero false positives from the strict version across every
committed notebook, including the one LightGlue/torch-heavy one that could plausibly emit
progress-bar noise, and no `tqdm`/progress-bar library anywhere in `src/trntest/*.py` to begin
with -- consistent with this project's existing `run_quiet` philosophy (quiet by default, surface
only on failure) rather than a new constraint.

**Cross-notebook dependency check** (added mid-pass, per user request): audited whether any
notebook reads another notebook's output without checking for it first. The codebase already does
the right thing almost everywhere -- `TrnTestEntry.camera`/`.crop_result`/`.dem_ortho_result` and
`TrnTestImage.generate()` are all idempotent generate-if-missing, and
`TrnTestImage._require_generated()` is the fail-fast fallback. One real gap found
(`reproject_spike.py` reading `entry.hillshade.raster_path` directly, bypassing both) -- moot once
that notebook was archived rather than fixed forward.

## Phase 91 (2026-08-30) — `plot_zoom_blink`: a full-resolution blink comparator; new Phase 5C/6C

New `plotting.plot_zoom_blink`: `plot_render_toggle`'s blink mechanism (two full frames, auto-looping
GIF), applied to two geo-aligned map-projected rasters instead of two same-grid renders, restricted to
a full-resolution square crop (`crop_px`, default 200px) so per-pixel detail isn't compressed away the
way `plot_overlay_toggle`'s whole-footprint figure does. `TrnTestImage.plot_zoom_blink_over` is its
notebook-facing wrapper, live in `image_generation.ipynb` as Phase 5C/6C.

Went through two real corrections mid-session, both from direct user feedback after seeing the first
version live:

1. **Crop anchor.** The first version anchored the crop window on whichever raster was passed first.
   Checked directly against this project's own default candidate: the basemap's own array center sits
   up to ~10km off a given candidate's actual footprint center (it's a padded/unioned AOI, not
   footprint-centered) -- anchoring there instead of on the candidate's own render/crop risked cropping
   mostly nodata. Fixed by always anchoring on the caller's own raster (`TrnTestImage.
   plot_zoom_blink_over` always passes `self`'s own map-projected raster as the anchor), independent of
   which raster ends up as the left-hand label.
2. **Argument order.** First version made `self` the blink's left-hand ("☑ label_a / ☐ label_b")
   entry. Per user request, switched so the *other* raster (an explicit `TrnTestImage`, or `None` for
   the implicit basemap) is left-hand instead, matching `plot_overlay`'s own `(base_raster_path,
   overlay_raster_path)` argument order -- `other` stands in for `plot_overlay`'s always-implicit
   basemap by default. `plot_zoom_blink_over`'s own default (self plays first in the loop) still
   matches `plot_overlay_toggle`'s overlay-first default.

Also **found and fixed a real, independent frame-order bug** in `_blink_gif_b64` while building this:
`show_a_first=True` was playing the *second* raster first, not the first, for both `plot_render_toggle`
and the new `plot_zoom_blink` -- `_blink_gif_b64(base_frame, overlay_frame, initial_visible)` plays
`overlay_frame` first when `initial_visible`, so passing `(frame_a, frame_b, show_a_first)` positionally
had it backwards. Fixed by swapping which frame is passed first; confirmed live via extracted GIF
frames, both before and after. `plot_overlay_toggle` itself was already correct (its own `initial_visible`
already meant "overlay first," matching this same convention).

Phase 5C/6C are `entry.hillshade.plot_zoom_blink_over()` / `entry.crop.plot_zoom_blink_over()` -- each
one call, no separate markdown commentary, mirroring how 5A/5B and 6A/6B are each one call per
candidate against the same phase-level markdown intro.

## Phase 92 (2026-08-31) — Brightness "matching" redesigned as symmetric median-normalization; `compute_brightness_matched_diff`'s numbers are no longer comparable to pre-Phase-92 values

**The problem, from the user:** every brightness comparison in `plotting.py` picked one side (`A`) as
the reference and scaled the other (`B`) to match it, then derived the display's `vmax` from `A` alone.
If `B` was much darker than `A`, it got scaled up a lot to match -- and nothing capped its now-inflated
highlights from clipping past a `vmax` sized only for `A`. Not robust to a genuinely dark side.

**First proposal (wrong, caught by the user): scale both sides toward a shared "target" (e.g. the
geometric mean of their medians).** Worked through the algebra live: once `vmax` is *re-derived from the
post-scale data* (as it already was), the absolute choice of target cancels out completely --
`scale_a/scale_b = median_b/median_a` for *any* target, so the final displayed image is identical
regardless of which target is picked. The real, non-moot fix for *display* is narrower: derive `vmax`
from the max of both sides' own post-match percentile, not from `A` alone. Which side gets held fixed
during the match is irrelevant to what's shown, as long as `vmax` accounts for both.

**Second proposal (the one that shipped, from the user): normalize *both* sides independently to their
own median = 1.0, rather than one matched to the other's absolute level.** For *display*, this is
provably equivalent to the disproven "common target" idea (same reasoning: any per-side target is moot
once `vmax` is re-derived) -- but for `compute_brightness_matched_diff`, a *terminal* quantity with no
downstream re-normalization step, it's a real, coherent change: `|A/median_a - B/median_b|` is
algebraically `|A - (median_a/median_b)*B| / median_a` -- the *old* diff, rescaled by a fixed, meaningful
constant (dividing by `A`'s own median), not an arbitrary cancel-out. The result is a dimensionless
fraction of each raster's own median brightness (`0.05` == "5% of typical brightness"), comparable
across candidates at very different absolute brightness levels -- not a raw diff in one raster's own
arbitrary units.

**Implementation:** two new shared helpers, `_robust_median`/`_normalize_to_median` (guard a zero/
non-finite/empty median the same way every prior scale computation already did, just factored out).
Applied to `_prep_overlay_rasters` (and therefore `plot_overlay`/`plot_overlay_toggle`/
`compute_brightness_matched_diff`, which all build on it), `plot_isis_comparison`, `plot_sfs_comparison`,
`plot_render_toggle`, and `plot_zoom_blink`. Every one of these now derives its display `vmax` from the
*largest* of all its panels' own post-normalization percentile, not one panel's alone.

**Deliberately left untouched: `plot_comparison`.** Confirmed dead code (retired from the demo notebook
at Phase 22, referenced nowhere but its own docstring and other functions' comments) -- not worth
updating a function nothing calls, and its stale cross-references in other functions' comments were
cleaned up instead of preserved.

**Breaking change, not retroactively fixed:** every `compute_brightness_matched_diff` number cited
anywhere in this file or `docs/plan.md` before this phase (e.g. Phase 72/74/78's `mean_abs_diff`
values, in raw calibrated-reflectance units) is no longer reproducible or comparable against a fresh
run -- the metric's own definition changed, not just its inputs. Freshly regenerated for calibration
(`sfs_validation.ipynb`, this candidate): real WAC vs. our hillshade `mean_abs_diff=0.0848`, vs. `sfs`
forward-render `mean_abs_diff=0.1208` -- both dimensionless fractions of median, not directly comparable
to the old ~0.0032-0.0061 absolute-reflectance-unit range.

Six notebooks touched: `image_generation.ipynb`/`hapke_hillshade.ipynb`/`real_hapke_params.ipynb`/
`sfs_validation.ipynb`/`pose_alignment_spike.ipynb` all call an affected function directly and were
regenerated end to end (all clean). `along_track_correction.ipynb`/`real_hapke_params.ipynb`'s own
printed `mean|diff|` numbers are untouched -- both implement their own separate inline brightness-match
(not this module's shared functions) -- only `real_hapke_params.ipynb`'s `plot_overlay_toggle` blink
figure changed visually. `tests/test_plotting.py` updated to match: two tests' expected numbers changed
(one from `10.0` to `0.1`, reflecting the exact rescaling derived above; one from checking `100.0` to
checking `1.0`), one renamed for accuracy, one gained an assertion that `base` (not just `overlay`) now
normalizes too.

## Phase 93 (2026-08-31) — Deleted `plot_comparison`

Phase 92 confirmed `plot_comparison` dead (retired from the demo notebook at Phase 22, referenced
nowhere but its own docstring and other functions' comments) and deliberately left it on the old
brightness-matching technique rather than update code nothing calls. Per user request, deleted it
outright instead of leaving it to keep drifting further from every other comparison function's own
technique. Also removed the now-unused `wac` import (`wac.MISSING_CONSTANT` was `plot_comparison`'s
only live reference to it) and fixed three stale cross-references in still-live code/docstrings
(`_plot_tie_point_marker`'s "shared by" comment, `plot_overlay`'s docstring, `_render_overlay_figure`'s
km-tick-formatter comment) that named it as a still-existing sibling. No notebook called it, so
nothing to regenerate; `trntest-lint`/`pytest`/`mypy` all clean.

## Phase 94 (2026-09-05) — Split `isis_wac.py` into `isis_wac.py`/`isis_campt.py`, removed the camera/isis_wac/wac_camera_model/lunaserv/render/spice_kernels circular-import cluster

First step of a broader source-code organization pass (naming/module-boundary review, full plan
originally in `docs/proposed-tasks/isis-wac-module-split.md`, folded in here now that it's done; the
remaining steps are tracked in `docs/proposed-tasks/open-items.md`'s "Source code reorganization"
section). Done on a separate `feature/refactor` integration branch per the user's request, with the
usual per-change notebook re-execution discipline (`AGENTS.md`) suspended for the duration —
`pytest`/`trntest-lint` catch reference breakage instead, and one full notebook pass happens right
before `feature/refactor` merges to `main`.

**The split**: `isis_wac.py` (1402 lines) mixed running the ISIS pipeline
(`lrowac2isis`→`spiceinit`→`lrowaccal`→`framestitch`→`crop`→`cam2map`, plus CSM ISD generation) with
answering ground-truth ground↔image queries against an already-processed cube via `campt` — a seam
the test suite already reflected (`tests/test_isis_wac_ground_to_image.py` covered only the second
concern). New `isis_campt.py` (538 lines) takes the `campt`-based queries (`GroundToImageModel`,
`resolve_ground_to_image_model`, `ground_to_image_pixel(s_batch)`, `image_to_ground_points_batch`,
`campt_photometric_angles`, `ground_point_at_pixel`, `ephemeris_time_at_pixel`, `cube_serial_number`)
and the CSM ISD family they depend on (`IsdGenerateResult`, `run_isd_generate(_for_crop)`,
`run_mapproject`); `isis_wac.py` (909 lines) keeps the pipeline itself. `run_mapproject`'s docstring
was rewritten from a bare "**Deprecated**" to name the actual blocker (`usgscsm`'s Pushframe
`groundToImage` has an unreliable secant-search bug, docs/external-tools.md) and frame it as
preferable to `run_cam2map_for_crop` once that's fixed upstream, not abandoned — the user's own
framing, since the CSM approach is architecturally the more direct one.

**The circular imports**: two were already self-documented `# noqa: PLC0415` lazy-import workarounds
(`camera.py` needing `isis_wac.run_pipeline`/`ground_point_at_pixel` for a real boresight correction;
`lunaserv.py` needing `isis_wac.ensure_isisdata`); a third (`isis_wac.py`↔`wac_camera_model.py`) had no
workaround at all and only avoided crashing because neither side did a name-specific import of the
other. Tracing actual runtime need (not just type annotations) showed `isis_wac.py`'s imports of
`Camera`/`FrameTiming`/`PoseCorrection`/`DemOrthoResult` were annotation-only, never constructed —
fixed via `from __future__ import annotations` + `if TYPE_CHECKING:` guards in `isis_wac.py` and
`isis_campt.py`, letting `camera.py` and `wac_camera_model.py` import them normally at module scope.

That surfaced two more real cycles the plan hadn't anticipated, both only reachable once `camera.py`
imports `isis_wac`/`isis_campt` for real: `lunaserv.py` and `render.py` also imported `Camera` (and
`render.py` also `DemOrthoResult`) at module scope, annotation-only in both — same
`TYPE_CHECKING`-guard fix applied to both files, confirmed by an actual Docker import test
(`docker compose run --rm demo python3 -c "import trntest.<mod>"` for every module, and again with
each module as the sole first import in a fresh process) after hand-tracing the chain wrongly twice.
A fourth, separate lazy-import workaround turned up in `spice_kernels.py` (`from trntest import
isis_wac  # noqa: PLC0415 -- avoids a circular import`, for `resolve_wac_ck_kernels`) — its own root
cause (`spice_kernels`→`isis_wac`→`camera`→`spice_kernels`) was already fixed by the `camera.py`
change above, so it converts to a normal top-level import too, as a bonus beyond the original plan's
scope.

**Verification**: fresh Docker image build, the import tests above, `trntest-lint --all`, and the fast
`pytest` suite (362 passed) — all clean after fixing one real breakage
(`test_tie_points_geometry.py` patched `isis_wac.ground_point_at_pixel`, which moved) and updating
every test/doc/comment cross-reference to a moved name across `src/`, `tests/`, `scripts/`, and the
current-state docs (`docs/external-tools.md`, `docs/pose-alignment.md`, `docs/image-pipeline.md`,
`docs/environment.md`). `notebooks/pose_alignment_spike.py` still has two stale
`isis_wac.resolve_ground_to_image_model`/`isis_wac.image_to_ground_points_batch` references —
deliberately left for the final notebook pass rather than fixed now, per the suspended-discipline
workflow above.

## Phase 95 (2026-09-05) — `.gitignore`: exclude `cache/`/`output/`/`scratch/`

Found while re-running `trntest-lint --all` repeatedly during Phase 94: `docker-compose.yml` bind-mounts
`cache/`/`output/`/`scratch/` (which live outside this repo entirely, see `docs/environment.md`) at
`/workspace/cache`/`/workspace/output`/`/workspace/scratch` inside the container — nested under this
repo's own working directory from git's point of view. Without a `.gitignore` entry, `git ls-files
--others --exclude-standard` (and therefore `trntest-lint --all`, which uses it to find untracked
files) walks into the mounted `scratch/` content and reports spike scripts there as lint findings, on
every `--all` run, on every worktree. Added `/cache/`, `/output/`, `/scratch/` to `.gitignore` — no
host-side layout change, just stops git-based tooling inside the container from treating a bind mount
as part of the repo.

## Phase 96 (2026-09-05) — Split `lunaserv.py` by data source into six modules

Task 2 of the source-code reorganization (`docs/proposed-tasks/open-items.md`'s "Source code
reorganization" section; task 1 was Phase 94). Same `feature/refactor` branch, same suspended
per-change notebook discipline as Phase 94.

`lunaserv.py` (1836 lines) mixed GLD100 DEM fetch, WAC_EMP ortho fetch, the deprecated Lunaserv WMS
fallback, ~650 lines of self-contained Hapke photometry math, and generic CRS/bbox math with no
data-source dependency of its own. Split into six files, matching `docs/data-sources.md`'s own
per-source organization:

- **`geo_utils.py`** (230 lines) — dependency-free CRS/bbox/reprojection math
  (`geographic_crs`/`local_orthographic_crs`/`pad_bbox`/`reproject_raster_to_local_grid`, the last
  renamed from the private `_reproject_raster_to_local_grid` since it's now called across modules)
  plus `DEM_FETCH_SAFETY_MARGIN_FRACTION`, shared by both DEM/ortho fetch paths.
- **`dem_gld100.py`** (158 lines) — the live default DEM source (Astropedia GLD100).
- **`ortho_wac_emp.py`** (203 lines) — the live default ortho source (WAC_EMP PDS4), importing two
  wavelength constants from `hapke.py` (`HAPKE_CALIBRATION_WAVELENGTHS_NM`, also renamed public since
  it's now cross-module) since the Hapke calibration cube and WAC_EMP's own archive share one
  wavelength band set by design.
- **`lunaserv_wms.py`** (157 lines) — the deprecated Lunaserv WMS fallback (`fetch_dem_native`,
  `reproject_dem_to_local_grid`, `radius_to_elevation`) — real code with real test/notebook coverage,
  not dead, just superseded as the live default.
- **`hapke.py`** (814 lines) — despeckling, the default Hapke relighting pipeline, the plain
  Lambertian fallback, and the photometric-angle geometry both need.
- **`dem_ortho.py`** (384 lines) — the orchestration layer (`fetch_dem`/`fetch_and_shade_ortho`/
  `fetch_dem_and_ortho`), including `hole_fill_dem` (previously misfiled in this doc's own task-2
  scoping note as living in `product_registry.py` — it was always defined in the old `lunaserv.py`
  itself).

**Fully resolved the `isis_wac`↔`lunaserv` circular-import cycle** flagged as unfinished business in
Phase 94: `isis_wac.sample_local_dem_patch`'s real `local_orthographic_crs`/`geographic_crs` calls
now go to the dependency-free `geo_utils.py` instead, so `isis_wac.py` no longer needs `lunaserv`
(real or type-only) at all — its `DemOrthoResult` import (now from `dem_ortho.py`) is a plain
top-level import, no `TYPE_CHECKING` guard needed, since nothing cyclic remains on that edge.

**Deviations from the original plan** (`docs/proposed-tasks/open-items.md`'s pre-Phase-96 naming
table): `product_registry.py` → `product_io.py` and the `wac.py` deletion are unchanged, still
task 3, not touched here.

**Verification**: same method as Phase 94 — fresh Docker build, every module imported both together
and as the sole first import in a fresh process (no failures either way), `trntest-lint --all` clean
on the first real attempt (one unused-import fixup, one auto-formatting fixup), full `pytest` clean
(362 passed, matching the pre-Phase-96 count exactly since this only moved/renamed existing tests).
`test_lunaserv.py` (842 lines) split into `test_geo_utils.py`/`test_dem_gld100.py`/
`test_ortho_wac_emp.py`/`test_lunaserv_wms.py`/`test_hapke.py`/`test_dem_ortho.py` along the same
seams as the source. Every real call site across `src/`, `tests/`, and current-state docs updated;
five notebooks (`along_track_correction.py`, `real_hapke_params.py`, `crater_sharpness_review.py`,
`sfs_validation.py`, `hapke_hillshade.py`) still import the old `lunaserv` module and are deliberately
left for the reorganization's final notebook pass, per the same suspended-discipline policy as
Phase 94's one deferred notebook.

## Phase 97 (2026-09-05) — Deleted `wac.py`; renamed `dataset.py` → `candidate_window.py` and `product_registry.py` → `product_io.py`

Task 3 of the source-code reorganization (`docs/proposed-tasks/open-items.md`'s "Source code
reorganization" section; tasks 1/2 were Phases 94/96). Same `feature/refactor` branch, same suspended
per-change notebook discipline.

**Deleted `wac.py`**: `fetch_vis_mosaic` (manual byte-offset VIS mosaic extraction from a WAC CDR
product) and its CDR-byte-layout constants (`PDS3_HEADER_BYTES`, `FRAME_BYTES`, `VIS_BLOCK_OFFSET`,
`MISSING_CONSTANT`, `LINES_PER_FRAME`) were dead — superseded by `isis_wac.py`'s ISIS pipeline, only
self-referenced. The two real WAC-VIS sensor-geometry constants it also held, `SAMPLES` and
`VIS_BLOCK_HEIGHT` (still imported for real by `isis_wac.py`/`wac_camera_model.py`/`tie_points.py`),
moved to a new, dependency-free `wac_format.py` first. `tie_points.py` had been reaching these two
constants two different ways — directly as `wac.SAMPLES` and indirectly via `isis_wac.py`'s own
re-export of `wac.SAMPLES`/`wac.VIS_BLOCK_HEIGHT` — both call sites now route to `wac_format.py`
directly, per the user's explicit instruction not to preserve either indirection. Also dropped:
`session.py`'s `fetch_vis_mosaic` delegator, `__init__.py`'s re-export, `tests/test_wac_unpacking.py`
(3 tests), and the `fetch_vis_mosaic`-specific test in `tests/test_session.py`.

Deleting `wac.py` also retired the `TrntestConfig` fields only it consumed
(`lroc_cdr_dataset`/`cdr_volume`/`cdr_product` and their `DEFAULT_*` constants). Removing them
surfaced a real, active-path bug: `dataset.py`'s `_per_image_config` (the per-entry config builder
used by both `generate_dataset()` and `TrnTestEntry.per_image_config` — i.e. the whole pipeline's own
config derivation) was still passing `cdr_volume=`/`cdr_product=` into
`dataclasses.replace(config, ...)`, which would have raised at runtime for every entry. Caught by
`trntest-lint --all`'s mypy pass (`Unexpected keyword argument "cdr_volume"`), not by grep (which only
checked attribute-access patterns, missing the keyword-argument form) — fixed by dropping both kwargs
and updating the docstring. This is the strongest evidence yet in this reorganization for running the
full lint+test pass rather than trusting a grep sweep alone.

Left deliberately unremoved, flagged instead as a new deferred open item: `dataset.py`'s
`attach_cdr`/`catalog.find_matching_cdr`/`cdr_*`-manifest-column feature is now fully vestigial with
`wac.py` gone (nothing else ever read those columns), but deciding whether to drop the whole feature
(vs. keeping it for manifest provenance) is a separate call from "delete the dead module that used
it" — out of scope for this task, per the user's own instruction to keep the CSM dead code (a related,
not identical, "keep dead-but-meaningful code" judgment call raised in the same review round) rather
than deleting things reflexively.

**Renamed `dataset.py` → `candidate_window.py`** (matches its own `images_for_window()`; frees the
word "dataset" from a name collision with `trn_dataset.py`) and **`product_registry.py` →
`product_io.py`** (it's atomic-publish/read/write helpers, not a registry data structure) — pure
renames, no behavior change. Every real importer updated (`session.py`, `dataset_selection.py`,
`trn_dataset.py`, `illumination.py`, `sfs_validation.py`, `camera.py`, `catalog.py`, `config.py`,
`__init__.py` for the first; `geo_utils.py`, `render.py`, `isis_wac.py`, `dem_ortho.py`, `hapke.py`,
`crater_depth_batch.py` for the second), along with every dangling comment/docstring reference across
`src/`, two docs (`docs/data-sources/lroc-wac-edr-cdr.md`, `docs/external-tools.md`,
`docs/data-sources/spice-kernels-naif.md`, `docs/batch-generation.md`), and `README.md`'s source-files
table (added a `wac_format.py` row, removed the `wac.py` row, renamed the `dataset.py`/
`product_registry.py` rows and re-alphabetized). `tests/test_dataset.py` → `test_candidate_window.py`,
`tests/test_product_registry.py` → `test_product_io.py`, both with internal references updated to
match.

Also fixed, while sweeping for `wac.py` references: `camera.py`'s comment claiming pixel data for the
real-WAC comparison "comes from the CDR counterpart" — stale even before this phase, since
`isis_wac.py` has worked from the EDR directly since task 1. Rewritten to state the current fact.

**Verification**: same method as Phases 94/96 — fresh Docker build, all 36 modules import cleanly both
together and as the sole first import in a fresh process, `trntest-lint --all` clean (after the mypy
fix above), full `pytest` clean (358 passed, down from 362 by exactly the 4 tests removed with
`wac.py` — 1 from `test_session.py`, 3 from the deleted `test_wac_unpacking.py` — and unchanged again
after the pure renames, as expected).

## Phase 98 (2026-09-05) — Split `plotting.py` into three: `plotting.py`, `sfs_plotting.py`, `dataset_selection_plots.py`

Task 4 of the source-code reorganization (`docs/proposed-tasks/open-items.md`'s "Source code
reorganization" section; tasks 1/2/3 were Phases 94/96/97). Same `feature/refactor` branch, same
suspended per-change notebook discipline.

`plotting.py` (1633 lines) mixed four audiences: generic raster-display primitives
(`plot_raster`/`read_raster_band`/`valid_pixel_mask`/...), the generator-comparison figures
`image_generation.py`/reports actually need (`plot_render_vs_basemap`, `plot_overlay*`,
`plot_zoom_blink`, `compute_brightness_matched_diff`), two SFS-validation-only plots
(`plot_sfs_comparison`, `plot_incidence_validation`), and two dataset-selection scatter plots
(`plot_sun_elevation_vs_edr_count`, `plot_illuminated_node_scatter` + its private
`_underline_segments` helper) — the only reason this file depended on `illumination.py` at all. Kept
the first two audiences in `plotting.py` (1392 lines); moved the SFS pair to new `sfs_plotting.py`
(93 lines) and the dataset-selection pair to new `dataset_selection_plots.py` (175 lines).

**The one real complication**: `plot_sfs_comparison` needs four of `plotting.py`'s own raster-display
helpers (`_open_raster_dataarray`, `_cellsize_m`, `_normalize_to_median`, `_robust_median`), all four
still genuinely shared with functions staying in `plotting.py` (`_prep_overlay_rasters`,
`compute_brightness_matched_diff`, `plot_render_toggle`, `plot_zoom_blink`) — a real cross-module
dependency, not something to duplicate. This codebase's own convention (established at Phase 96,
`geo_utils.reproject_raster_to_local_grid`) is that a leading underscore means module-private, so a
helper crossing a module boundary for real loses it: all four renamed public
(`open_raster_dataarray`/`cellsize_m`/`normalize_to_median`/`robust_median`), every internal call
site in `plotting.py` updated to match, and `sfs_plotting.py` imports them from `trntest.plotting`
normally. `plot_incidence_validation` and both dataset-selection plots needed no such treatment —
fully self-contained apart from `illumination.unwrap_relative_deg` (already a real, public,
cross-module call).

Also dropped from `plotting.py`, now unused once their only two callers moved out: `import pandas as
pd`, `from datetime import datetime`, and `from trntest import illumination`.

**Deferred, per the same suspended-notebook-discipline policy as Phases 94/96**:
`notebooks/select_datasets.py` still calls `plotting.plot_sun_elevation_vs_edr_count`/
`plotting.plot_illuminated_node_scatter`, and `notebooks/sfs_validation.py` still calls
`plotting.plot_sfs_comparison`/`plotting.plot_incidence_validation` — both notebooks' stale
references tracked in `docs/proposed-tasks/open-items.md`'s deferred-notebooks list, fixed only in
this reorganization's final notebook-re-execution pass.

**Verification**: same method as Phases 94/96/97 — fresh Docker build, all 38 modules import cleanly
both together and as the sole first import in a fresh process, `trntest-lint --all` clean on the
first attempt (no fixups needed this time), full `pytest` clean (358 passed, unchanged -- pure code
movement, no tests added or removed). `tests/test_plotting.py`'s two `plot_sfs_comparison`/
`plot_incidence_validation` tests moved to new `tests/test_sfs_plotting.py`; no prior test coverage
existed for either dataset-selection scatter plot, so no test file was needed for
`dataset_selection_plots.py`.

## Phase 99 (2026-09-05) — Split `trn_dataset.py`'s product classes into `trn_products.py`

Task 5 of the source-code reorganization (`docs/proposed-tasks/open-items.md`'s "Source code
reorganization" section; tasks 1-4 were Phases 94/96/97/98). Same `feature/refactor` branch, same
suspended per-change notebook discipline.

`trn_dataset.py` (868 lines) held two concerns along an already-clean seam: `TrnTestEntry`/
`TrnTestDataSet` (dataset-folder structure, task-queue orchestration) and `TrnTestProduct`/
`TrnTestImage`/`TrnTestCropImage`/`TrnTestHillshadeImage`/`TrnTestReprojectImage`/`TrnTestReport`
(the per-generator product classes). Moved the six product classes to new `trn_products.py` (413
lines); `trn_dataset.py` (476 lines) keeps `TrnTestEntry`/`TrnTestDataSet` plus the module-level
task-queue helpers (`task_state`, `_enqueue_pending`, etc.).

**The circular import**: `TrnTestEntry.crop`/`hillshade`/`reproject`/`report` properties construct
`trn_products.TrnTestCropImage(self)` etc. for real, so `trn_dataset.py` needs a normal top-level
import of `trn_products`. In the other direction, `TrnTestProduct.__init__(self, entry:
TrnTestEntry)` only ever reads `self.entry`'s attributes -- never constructs or isinstance-checks a
`TrnTestEntry` -- so `trn_products.py` uses `from __future__ import annotations` +
`TYPE_CHECKING` for that one import, the same pattern established at Phase 94. Confirmed via the
usual Docker import test (39 modules, both bulk and sole-first-import) -- no manual hand-tracing
needed this time, the pattern is now familiar enough to get right on the first attempt.

`TrnTestReport._generate_impl`'s existing lazy `from trntest import report` (`noqa: PLC0415`)
stays lazy, but for a different, more indirect reason than its own comment used to state: `report.py`
imports `TrnTestDataSet`/`TrnTestEntry` from `trn_dataset.py` (unchanged), which now imports
`trn_products.py` for real (to construct product instances) -- so a top-level `report` import inside
`trn_products.py` would close that loop. Comment rewritten to state the actual chain.

**Also fixed while sweeping for real `trn_dataset.TrnTestX` references** (all now `trn_products.`):
`render.py`'s `run_mapproject_image` doc comment, `isis_campt.py`'s ISD-sidecar comment, `plotting.py`'s
`mathtt` docstring, README's source table (new `trn_products.py` row, `trn_dataset.py`'s row narrowed
to just `TrnTestDataSet`/`TrnTestEntry`), and four `docs/generators*.md`/`docs/reproject-fov-
investigation.md` cross-references. `tests/test_trn_dataset.py`'s `_FakeImage` class and its own
"Exact path naming"/"TrnTestImage shared base-class logic" test sections (5 tests total) moved to new
`tests/test_trn_products.py`, mirroring the source split; the task-queue/populate/truncate tests that
merely monkeypatch product classes' `_generate_impl` as a fast stand-in stayed in
`tests/test_trn_dataset.py`, since they're really testing `TrnTestDataSet`'s own orchestration, not
the product classes.

**Also found and fixed, unrelated to this task**: task 3's own reference sweep (Phase 97, `dataset.py`
→ `candidate_window.py`) had checked `src`/`tests`/current-state docs but missed several real
`dataset.X` comment references in files it hadn't touched for other reasons --
`tie_points.py`/`render.py`/`report.py`/`spice_kernels.py` (x2)/`cache.py` (x2)/`config.py`, plus
`docs/intermediate-product-discipline.md` (x2)/`docs/dataset-selection.md` (x2)/`docs/caching.md`
(x2). Found by accident while grepping this task's own file list for stale references, not by a
deliberate audit -- all fixed here since they were real, present-tense inaccuracies, not merely
adjacent to this task's scope. Two more, in `notebooks/select_datasets.py`'s markdown cells, are
notebook content and so deferred to the final notebook pass like every other notebook staleness this
reorganization has found -- new bullet added to `open-items.md`'s deferred list.

**Verification**: same method as Phases 94/96/97/98 -- fresh Docker build, all 39 modules import
cleanly both together and as the sole first import in a fresh process, `trntest-lint --all` clean
(one `ruff format` fixup for trailing blank lines from the file split, one `UP037` auto-fix for a
type annotation that no longer needed quotes once `from __future__ import annotations` was in
scope), full `pytest` clean (358 passed, unchanged -- pure code movement, no tests added or removed
net of the test-file split above).
