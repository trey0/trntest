# Development history

Archival narrative of how this project reached its current state — phase by phase, including wrong
turns and how they were caught. This is background/curiosity reading, **not required before making
a change**: see `docs/plan.md` for current architecture/status and `docs/data-sources.md` for
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
