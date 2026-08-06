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
