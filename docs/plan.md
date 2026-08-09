# trntest — architecture & status

## What this is

Explores techniques for generating candidate test imagery for terrain-relative navigation (TRN)
testing (hence the repo name) — images that could stand in for real spacecraft imagery when testing
a TRN algorithm. Two candidate pipelines: a synthetic lunar satellite image (with a CSM/"ISD" JSON
camera sidecar) generated via NASA's Ames Stereo Pipeline (ASP) `sat_sim` tool, fed by real DEM +
visible imagery pulled live from the Lunaserv WMS server; and the same real footprint's actual LROC
WAC image, processed through ISIS3's own EDR-to-calibrated-cube pipeline. Both share one thing that
makes them usable as TRN test images at all: the camera pose is derived from the **real LRO
spacecraft trajectory** (NAIF SPICE kernels) at the time of a real LROC WAC image, so each
candidate's own geometry can be validated against a real hillshade-based basemap — two ways (a raw
image-quality check, and a true pixel-for-pixel `mapproject` overlay) — rather than just asserted.
This is a demo/exercise in AI-assisted coding on a real geospatial engineering task.

All heavy tooling/build/test happens inside a Docker container (Ubuntu 24.04), built from the
checked-in `docker/Dockerfile`, so it's reproducible off this host.

## Status

The demo runs end-to-end and is stable: real LRO SPICE trajectory → posed synthetic camera →
`sat_sim` render + CSM/ISD sidecar, and, from the same real footprint's EDR, ISIS3's own calibrated
real-WAC product. Each candidate TRN test image's geometry is validated against a real hillshade
basemap two ways — a raw, north-up-rotated quality check (`plotting.plot_render_vs_basemap`) and a
true pixel-for-pixel `mapproject` overlay (`plotting.plot_overlay`) — and the two candidates are also
compared directly against each other with explicit SPICE-derived tie points. Packaged as an
installable library (`src/trntest/`) with config, tests, and style tooling.

The **live default path is catalog-driven, not a single hardcoded product**: `select_dataset()`
queries the real LROC catalog for a favorable multi-orbit window and returns a list of real,
illuminated WAC images; `generate_dataset()` renders the chosen one(s) through the same pipeline
described above. There is no current dependency on any one specific EDR product or framelet index —
see `docs/data-sources.md` for the couple of specific products still used as regression-test
fixtures, and `docs/history.md` if you're curious how the demo evolved from a single hand-picked
product to this.

## Architecture (`src/trntest/`)

| Module | Responsibility |
|---|---|
| `config.py` | `TrntestConfig`/`load_config()` — endpoints, paths, product IDs, tunables. TOML file + `TRNTEST_*` env var overrides. |
| `cache.py` | Local-mirror disk caching for all external fetches (NAIF, Lunaserv, LROC) — see `docs/caching.md`. |
| `spice_kernels.py` | Selects/downloads the minimal SPICE kernel set for a date, furnishes it (`fetch_and_furnish`, `furnish_spk_range`). |
| `camera.py` | Poses the synthetic camera from real SPICE trajectory/orientation data; `build_camera()`, `FrameTiming`/`fetch_frame_timing()` (EDR label parsing), sensor-axis convention (`boresight_rotation_k`). |
| `illumination.py` | Sun/orbit geometry via real SPICE functions — sun elevation/azimuth, sub-solar point, ascending-node search (`gfposc`). |
| `catalog.py` | PDS ODE REST API client — lists real EDR/CDR products by time range, matches EDR↔CDR pairs. |
| `dataset.py` | Public multi-image API: `select_dataset()` (catalog-driven selection), `generate_dataset()` (renders selected images through the single-image pipeline). Also computes each image's real WAC crop footprint (`tie_points.crop_footprint_corners_for_camera`) and passes it to `lunaserv.fetch_dem_and_ortho` so the DEM/ortho AOI covers both the synthetic camera's footprint and the real WAC crop's — exposed on `GenerationResult.crop_footprint` for reuse (e.g. by the notebook's Phase 6). |
| `lunaserv.py` | Fetches DEM + ortho imagery from Lunaserv WMS for a camera's footprint (optionally unioned with an extra footprint via `union_bbox`, see `dataset.py`), in a per-camera local Orthographic CRS (`IAU2000:30166`, real Moon radius) centered on that footprint — genuinely isotropic meter pixels, unlike Lunaserv's native geographic grid. Despeckles the ortho and blends in a real-sun-lit hillshade (`sat_sim` applies no illumination model of its own). |
| `render.py` | Runs `sat_sim`/`cam_gen` to produce the rendered `.tif` + CSM/ISD JSON sidecar. `run_mapproject` reprojects the render back onto the map through that same CSM sidecar, for geo-aligned overlay display. |
| `wac.py` | Extracts a band-separated, along-track-stacked VIS mosaic from a real WAC CDR product via manual, hand-derived byte offsets. Superseded by `isis_wac.py` as the demo notebook's real-WAC comparison method (see the open items below) but left in place, untouched, with its own unit test coverage. |
| `isis_wac.py` | Steps a real WAC EDR through ISIS3's own pipeline (`lrowac2isis`/`spiceinit`/`lrowaccal`/`framestitch`) -- calibrated, band-separated, and framelet-interleaved through a genuine camera-model-aware toolchain. `crop_for_camera` then crops the stitched cube (ISIS's own `crop` app) down to the real footprint being compared -- a single, real "WAC crop" cube both the notebook's raw-pixel display and `run_cam2map_for_crop` consume, with no special-casing. `run_cam2map_for_crop` reprojects it onto the DEM via ISIS's own *native* Pushframe camera model (`cam2map`, using a PVL map file cloned from `LunaservResult`'s own local Orthographic CRS) rather than ASP's `mapproject`/CSM -- a real bug in `usgscsm`'s `groundToImage` for Pushframe sensors made the CSM route unusable for a crop this size (see `docs/history.md`'s dated entry). `run_isd_generate`/`run_mapproject` (the old CSM path) are kept for reference/comparison but no longer used. Must run against the stitched (interleaved) cube, not a lone even/odd parity (see the open items below). |
| `tie_points.py` | SPICE-derived ground tie points, projected into both images' pixel coordinates, for the comparison figure. `crop_footprint_corners`/`crop_footprint_corners_for_camera` independently ray-trace the real WAC crop's own ground footprint (not assumed identical to the synthetic camera's), for `plotting.plot_render_vs_basemap` and `dataset.generate_dataset`'s DEM/ortho AOI sizing. |
| `orientation.py` | Notebook-display-only north-up rotation (does not touch the sensor model). |
| `plotting.py` | Comparison-figure plotting. `plot_render_vs_basemap` is the "A"-style geometry check: a render's own raw pixels next to a plain crop of the hillshade basemap covering the same real footprint (no resampling, no rotation on the basemap side -- its local Orthographic CRS is already north-referenced), both optionally marked with the same SPICE-derived tie points. `plot_overlay` is the "B"-style check: two geo-aligned rasters (e.g. a `mapproject` output over `LunaservResult.ortho`) displayed via `rioxarray`, using each file's own real coordinates rather than pixel indices -- expects `overlay_raster_path` to already cover just the real footprint being compared (true for both the synthetic render's own mapproject and `isis_wac.crop_for_camera`'s output), no view-restricting parameter needed. `plot_isis_comparison` is the direct candidate-vs-candidate comparison (brightness-matched, dead-pixel-filled). |
| `session.py` | `Session` facade — thin one-line delegators so notebook cells don't repeat `config=...`. |

`notebooks/lunar_sat_sim_demo.ipynb` drives all of the above end to end — see `README.md` to run it,
and AGENTS.md's "Working conventions" for how to validate changes against it.

## Known open items (resolve as encountered, record findings in `docs/data-sources.md`)

- Whether `--save-as-csm` state JSON is an acceptable stand-in for a literal ISD file for whatever
  comes after this demo.
- Confirm the lunar frame kernel defining `MOON_ME` loads correctly so SPICE can output that frame
  directly; sanity-check against the known GLD100/LOLA convention.
- **Open, investigate soon: `spice_kernels.py` is missing a CK kernel ISIS uses, causing a real
  ~11-13km pointing discrepancy.** Diagnosed (not fixed) while validating Phase 6B's `cam2map`
  switch: at the exact same instant, this project's own SPICE-based pointing
  (`camera.camera_pose_moon_me`) and ISIS's own camera model (via `spiceinit web=yes`) disagree by
  ~11km — confirmed not a `crop_for_camera` frame-selection bug (ISIS's own per-line time matches
  `camera.frame_et()` to within 0.016s for the corresponding frames; it's the *pointing* that
  differs, not the *timing*). Traced to `moc42r_2019304_2019335_v01.bc`, a second CK kernel ISIS's
  `spiceinit web=yes` furnishes alongside the usual `lrolc_*` one (name suggests a mission-ops
  reconstructed/refined attitude product) — `spice_kernels.py`'s `WAC_CK_PREFIXES = ("lrosc",
  "lrolc")` never fetches it, and it isn't even present in the NAIF metakernel this project's own
  code already parses, so it needs a different kernel source, not just an added prefix. Affects
  every use of `camera.camera_pose_moon_me`/`build_camera`, not just Phase 6B — likely the same
  root cause behind other small, previously-unexplained position residuals throughout this
  notebook's geometry checks. See `docs/history.md`'s dated entry (Phase 25) for the full diagnosis.
- **Resolved**: Lunaserv's native geographic projection is fine for `sat_sim`'s forward render, but
  turned out to break the `mapproject --ref-map` round-trip (anisotropic degree-pixels away from the
  equator, not preserved by `--ref-map`) — fixed by requesting a per-camera local Orthographic CRS
  directly from Lunaserv (`IAU2000:30166`, still a single WMS fetch, no separate `gdalwarp` step).
  See `docs/data-sources.md`'s Lunaserv WMS section and `docs/history.md`'s dated entry.
- **Resolved**: whether a real WAC swath can be reprojected onto the DEM via a genuine ISIS/CSM
  camera model (`mapproject`). The previously-reported "severe framelet-boundary striping"
  (`docs/history.md` Phase 12, `docs/data-sources.md`'s "ISIS3/CSM spike" section) turned out to be
  mostly a methodological artifact, not a fundamental CSM Pushframe limitation: mapprojecting a lone
  even/odd parity cube (each only ~50% populated — WAC alternates which nominal frame slot gets real
  data) leaves `mapproject` to resample across that sparsity, producing the smearing. Mapprojecting
  the properly interleaved *stitched* cube instead resolves the vast majority of it (31% valid
  coverage/no recognizable terrain → 81% valid coverage/real craters throughout, same real product).
  `isis_wac.py` now implements the full chain: EDR fetch → `lrowac2isis` → `spiceinit web=yes` →
  `lrowaccal` → `framestitch` → `crop_for_camera` → `isd_generate` → `mapproject` (via
  `render.run_mapproject_image`, shared with the synthetic render's own mapproject step). Given a
  genuine, validated camera model now exists end-to-end, `isis_wac.py` replaced `wac.py`'s manual
  framelet-stacking as the demo notebook's real-WAC comparison method entirely (`wac.py` itself is
  untouched, still covered by its own unit tests, just no longer used by either notebook — see
  `docs/history.md`'s dated entry for the rationale). `notebooks/lunar_sat_sim_demo.py`'s Phase
  5A/5B/6A/6B demonstrate the result: Phase 5 is the synthetic render's own geometry check (5A raw
  quality, 5B `mapproject` overlay), Phase 6 is the real WAC crop's (6A raw quality, 6B `mapproject`
  overlay) — 5B/6B share `plotting.plot_overlay` with no special-casing, since both overlays are
  already cropped to just the real footprint being compared by the time they reach it. See
  `docs/data-sources.md`'s "ISIS3/CSM spike" section and `docs/history.md`'s dated entries for the
  full investigation, including the ISD ephemeris-time bug found and fixed when reprojecting a
  cropped (not full-swath) cube; `notebooks/wac_isis_spike.py` remains the step-by-step version for
  isolating pipeline stages.
  **Correction: the ISD ephemeris-time fix above was a false positive** (0.999 correlation on a
  too-small sample), caught by the user's manual visual inspection of the actual notebook output,
  not by this project's own automated checks. Deeper investigation traced the real cause to a bug
  in `usgscsm`'s `UsgsAstroPushFrameSensorModel::groundToImage` itself (an unbracketed secant search
  over framelet index, unreliable for Pushframe images generally and badly so for a short crop) --
  not fixable via ISD authoring at all. Phase 6B now uses ISIS's own native camera model
  (`cam2map`) instead of ASP `mapproject`/CSM for the real WAC crop; `run_isd_generate`/
  `run_mapproject` are kept in `isis_wac.py` for reference but no longer used by the notebook. See
  `docs/history.md`'s dated entry for the full investigation.
- `geopandas` (added alongside `rioxarray` for `plotting.plot_overlay`) now has a concrete caller:
  `plot_overlay(show_overlay_outline=True)` traces the overlay raster's real (non-NaN) footprint and
  draws it as a vector boundary. A vector *data* layer (e.g. the Robbins crater database) on top of
  this raster overlay is still a possible future extension, not yet implemented.

## Development history

See `docs/history.md` for the phase-by-phase narrative — what was tried, what broke, and how each
design decision (framelet timing, sensor axis convention, catalog-driven selection, the perf fixes
in the sweep) was actually reached. Background/curiosity reading, not required before making a
change; this file and `docs/data-sources.md` describe current behavior.
