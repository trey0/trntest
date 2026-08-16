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
