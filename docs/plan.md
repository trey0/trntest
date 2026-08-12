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
| `spice_kernels.py` | Selects/downloads the minimal SPICE kernel set for a date, furnishes it (`fetch_and_furnish`, `furnish_spk_range`). WAC CK (pointing) kernel selection defaults to asking a real ISIS `spiceinit` run what it resolves (`select_isis_wac_ck_kernels`, via `isis_wac.resolve_wac_ck_kernels`), falling back to a NAIF-metakernel-based heuristic (`select_naif_wac_ck_kernels`, deprecated but numerically equivalent) for dates outside that resolution's own coverage — see `docs/data-sources.md`. |
| `camera.py` | Poses the synthetic camera from real SPICE trajectory/orientation data; `build_camera()`, `FrameTiming`/`fetch_frame_timing()` (EDR label parsing), sensor-axis convention (`boresight_rotation_k`). `build_camera()`'s final boresight is *re-aimed* (`look_at_rotation`) at a real, ISIS-determined target ground point (`isis_wac.run_pipeline`/`ground_point_at_pixel`) rather than trusted directly from `spice.pxform`'s raw `[0,0,1]` -- that raw assumption is confirmed measurably wrong for WAC-VIS (see `docs/history.md`'s dated entry). Camera *position* (`camera_pose_moon_me`) is untouched, already confirmed exactly correct. |
| `illumination.py` | Sun/orbit geometry via real SPICE functions — sun elevation/azimuth, sub-solar point, ascending-node search (`gfposc`). |
| `catalog.py` | PDS ODE REST API client — lists real EDR/CDR products by time range, matches EDR↔CDR pairs. |
| `dataset.py` | Public multi-image API: `select_dataset()` (catalog-driven selection), `generate_dataset()` (renders selected images through the single-image pipeline). Also computes each image's real WAC crop footprint (`tie_points.crop_footprint_corners_for_camera`) and passes it to `lunaserv.fetch_dem_and_ortho` so the DEM/ortho AOI covers both the synthetic camera's footprint and the real WAC crop's — exposed on `GenerationResult.crop_footprint` for reuse (e.g. by the notebook's Phase 6). |
| `lunaserv.py` | Fetches ortho imagery from Lunaserv WMS and the DEM from USGS Astropedia's flat-file GLD100 (`fetch_dem_astropedia`/`reproject_astropedia_elevation_to_local_grid` — Lunaserv's own DTM layer has a real, unfixable-client-side artifact, see `docs/data-sources.md`), both reprojected onto one shared per-camera local Orthographic CRS (real Moon radius) centered on that camera's footprint (optionally unioned with an extra footprint via `union_bbox`, see `dataset.py`) — genuinely isotropic meter pixels either way. `astropedia_coverage_bbox_deg` derives the Astropedia fetch's degree-space AOI directly from the destination Orthographic grid's own (already-padded) meters bbox (`rasterio.warp.transform_bounds`), not by independently padding a second, separate degree-space bbox — the old approach could (and did) undershoot the destination grid's own corners, leaving real nodata triangles there regardless of how generous `dem_padding_fraction` was (see `docs/history.md`'s dated entry). `fetch_dem_native`/`reproject_dem_to_local_grid` (the deprecated Lunaserv-DEM path) are kept for reference/comparison, no longer called by the default pipeline. Despeckles the ortho and blends in a real-sun-lit hillshade (`sat_sim` applies no illumination model of its own). |
| `render.py` | Runs `sat_sim`/`cam_gen` to produce the rendered `.tif` + CSM/ISD JSON sidecar. `run_mapproject` reprojects the render back onto the map through that same CSM sidecar, for geo-aligned overlay display. |
| `wac.py` | Extracts a band-separated, along-track-stacked VIS mosaic from a real WAC CDR product via manual, hand-derived byte offsets. Superseded by `isis_wac.py` as the demo notebook's real-WAC comparison method (see the open items below) but left in place, untouched, with its own unit test coverage. |
| `isis_wac.py` | Steps a real WAC EDR through ISIS3's own pipeline (`lrowac2isis`/`spiceinit`/`lrowaccal`/`framestitch`) -- calibrated, band-separated, and framelet-interleaved through a genuine camera-model-aware toolchain. `crop_for_camera` then crops the stitched cube (ISIS's own `crop` app) down to the real footprint being compared -- a single, real "WAC crop" cube both the notebook's raw-pixel display and `run_cam2map_for_crop` consume, with no special-casing. `run_cam2map_for_crop` reprojects it onto the DEM via ISIS's own *native* Pushframe camera model (`cam2map`, using a PVL map file cloned from `LunaservResult`'s own local Orthographic CRS) rather than ASP's `mapproject`/CSM -- a real bug in `usgscsm`'s `groundToImage` for Pushframe sensors made the CSM route unusable for a crop this size (see `docs/history.md`'s dated entry). `resolve_ground_to_image_model`/`ground_to_image_pixel` generalize that same resolution order (try a CSM ISD sidecar, fall back to the crop's native model only if it resolves to a Pushframe sensor) into a reusable ground-to-image query, now also used for `tie_points.resolve_crop_pixels`. `ground_point_at_pixel` is the reverse (image-to-ground), used by `camera.build_camera()` to re-aim the synthetic camera's boresight at a real target. `run_pipeline`/`crop_for_camera` are both idempotent (take `flip: bool` directly, not a `Camera`, for `run_pipeline` -- avoids a circular data dependency with `build_camera()`; safe to call again from the notebook's own Phase 6 cell or `tie_points.crop_footprint_corners_for_camera` for the same product -- reuses, doesn't redo, the ISIS work). `run_isd_generate`/`run_mapproject` (the old CSM path) are kept for reference/comparison but no longer used. Must run against the stitched (interleaved) cube, not a lone even/odd parity (see the open items below). |
| `tie_points.py` | Ground tie points for the comparison figure: `select_tie_points` picks 5 points (real, campt-derived footprint, used only to choose plausible candidates) and projects them into the synthetic image's exact pixel coordinates; `resolve_crop_pixels` fills in each point's real WAC-crop pixel coordinate via `isis_wac.ground_to_image_pixel` (a genuine ISIS `campt` query against the actual crop, not an approximation -- see `docs/data-sources.md`), dropping (with a warning) any point the real camera doesn't actually see. `crop_footprint_corners_for_camera` queries the real WAC crop's own actual ground footprint directly (`isis_wac.ground_point_at_pixel`, inset `_CROP_EDGE_MARGIN_PX` from the cropped cube's edges -- a real `campt` numerical-stability limit near cube boundaries, not a stylistic choice), for `plotting.plot_render_vs_basemap` and `dataset.generate_dataset`'s DEM/ortho AOI sizing; `_crop_footprint_corners_spice_approx` (deprecated) is the old SPICE ray-trace, kept for reference. |
| `orientation.py` | Notebook-display-only north-up rotation (does not touch the sensor model). |
| `plotting.py` | Comparison-figure plotting. `plot_render_vs_basemap` is the "A"-style geometry check: a render's own raw pixels next to a plain crop of the hillshade basemap covering the same real footprint (no resampling, no rotation on the basemap side -- its local Orthographic CRS is already north-referenced), both optionally marked with the same SPICE-derived tie points. `plot_overlay` is the "B"-style check: two geo-aligned rasters (e.g. a `mapproject` output over `LunaservResult.ortho`) displayed via `rioxarray`, using each file's own real coordinates rather than pixel indices -- expects `overlay_raster_path` to already cover just the real footprint being compared (true for both the synthetic render's own mapproject and `isis_wac.crop_for_camera`'s output), no view-restricting parameter needed. `plot_isis_comparison` is the direct candidate-vs-candidate comparison (brightness-matched, dead-pixel-filled). |
| `session.py` | `Session` facade — thin one-line delegators so notebook cells don't repeat `config=...`. |

`notebooks/data_set_selection.ipynb` (catalog-driven EDR selection, writes the checked-in
`notebooks/dataset_manifest.csv`) and `notebooks/image_generation.ipynb` (reads that manifest,
drives all of the above end to end) together replace what used to be one combined notebook — see
`README.md` to run them, and AGENTS.md's "Working conventions" for how to validate changes against
them.

## Known open items (resolve as encountered, record findings in `docs/data-sources.md`)

- Whether `--save-as-csm` state JSON is an acceptable stand-in for a literal ISD file for whatever
  comes after this demo.
- Confirm the lunar frame kernel defining `MOON_ME` loads correctly so SPICE can output that frame
  directly; sanity-check against the known GLD100/LOLA convention.
- **Resolved, with a corrected premise: the "missing CK kernel" pointing discrepancy doesn't
  actually reproduce.** Originally diagnosed while validating Phase 6B's `cam2map` switch: at the
  exact same instant, this project's own SPICE-based pointing (`camera.camera_pose_moon_me`) and
  ISIS's own camera model (via `spiceinit web=yes`) appeared to disagree by ~11-13km, traced to a
  second CK kernel (`moc42r_*.bc`, spacecraft bus attitude) that ISIS furnishes alongside the usual
  `lrolc_*` one but that `spice_kernels.py`'s `WAC_CK_PREFIXES` never fetched. Built
  `spice_kernels.select_isis_wac_ck_kernels`/`isis_wac.resolve_wac_ck_kernels` to fetch exactly the
  kernel set a real `spiceinit` run resolves (rather than reimplementing USGS's own kernel-selection
  algorithm — see `docs/data-sources.md`). **Direct re-verification then found the original premise
  didn't hold**: comparing our SPICE computation against real `campt` output at several independent
  points across the frame showed **zero** measurable pointing discrepancy, with or without `moc42r`
  furnished — and separately, `moc42r` turns out to have **no effect at all** on
  `camera.camera_pose_moon_me`'s computed pointing, because plain SPICE frame resolution for
  `LRO_LROCWAC_VIS` (-85620) depends entirely on `lrolc`'s own direct CK segments for that frame ID,
  never on the bus-attitude (-85000) kernel `moc42r`/`lrosc` provide (confirmed via a real
  `SPICE(NOFRAMECONNECT)` failure when `lrolc` is omitted even with `moc42r` present). The true cause
  of the originally-observed ~11-13km number was never pinned down — most likely conflated with the
  *other*, since-fixed bug found in the same investigation (`cam2map`'s `WARPALGORITHM=AUTOMATIC`
  striping issue, see the "Resolved" item below). The ISIS-kernel-matching mechanism was kept anyway
  (`TrntestConfig.wac_ck_source`, default `"isis_resolved"`) for its own independent value — it makes
  our furnished kernel set match ISIS's real resolution by construction rather than a hand-picked
  prefix list, which is more principled/future-proof even though it isn't fixing a live bug. The
  deprecated NAIF-metakernel path (`select_naif_wac_ck_kernels`) is confirmed numerically equivalent
  and kept for reference/comparison — and is deliberately still used (forced via
  `wac_ck_source="naif_metakernel"`) in `dataset.evaluate_candidate_image`'s per-candidate sweep,
  since `isis_resolved`'s real-`spiceinit`-per-`edr_product` cost is fine for the handful of
  deliberate final camera-pose calls elsewhere but was a genuine, confirmed O(candidates) performance
  regression there (>100 candidates each triggering their own uncached ISIS pipeline run). See
  `docs/history.md`'s dated entry for the full investigation,
  including the real, decisive empirical tests that overturned the original diagnosis.
- **Resolved**: Phase 6A's real WAC crop appeared systematically misaligned from its own tie points
  (real features consistently south of their matching marker). Root cause: `tie_points.py`'s crop-
  side pixel projection (`project_ground_to_crop_pixel`, now deprecated) was a hand-rolled SPICE
  frame-index bisection, entirely decoupled from whatever pipeline actually produced the crop's real
  pixels -- confirmed live, on this project's real default candidate, to disagree with the crop's
  actual embedded camera model by ~92-96px (out of 994 total lines, ~10%, along-track), and 2 of the
  5 SPICE-chosen die5 points weren't even visible to the real camera at all. Fixed by switching the
  crop-side projection to a genuine ISIS `campt` ground-to-image query (`tie_points.
  resolve_crop_pixels`/`isis_wac.ground_to_image_pixel`) against the real, already-produced crop
  cube. Generalized the camera-model choice into `isis_wac.resolve_ground_to_image_model`, following
  the same resolution order 6B's `cam2map` switch already established: try a CSM ISD sidecar first,
  fall back to the crop's native, SPICE-embedded model only if the ISD resolves to a Pushframe
  sensor (the class `usgscsm`'s `groundToImage` is known unreliable for) -- derived from a real ISD
  each call, not hardcoded, so it stays correct if this pipeline is ever pointed at a non-Pushframe
  instrument. Point *selection* (`select_tie_points`, still SPICE-approximate) is unaffected; a
  selected point the real camera doesn't see is now dropped with a warning rather than crashing the
  notebook. Phase 5's synthetic-image tie points are untouched -- that projection is already exact
  (the same fixed pose that rendered the image), not an approximation of some other camera model, so
  there was no discrepancy to fix there. See `docs/history.md`'s dated entry for the full
  investigation, including the real pixel-offset numbers and why Phase 5 was deliberately left out
  of this pass.
- **Resolved**: the item just above's "Phase 5 was deliberately left out" turned out to have its own
  real bug, found while debugging why the WAC crop's own real map-projected footprint didn't roughly
  match the synthetic render's footprint (as it should, both being TRN candidates for the same real
  ground area) -- they disagreed by ~11-15% in extent, and the exact "center" point by ~6-12km.
  Root cause: `build_camera()`'s synthetic-camera boresight trusted `spice.pxform`'s raw `[0,0,1]`
  directly, but that's confirmed measurably not WAC-VIS's real optical boresight (~5-6 degree real,
  roughly frame-constant angular offset -- not a timing or line-selection artifact, and not
  reproducible from any SPICE-visible kernel data, including the IAK). A first fix attempt (a
  constant correction rotation, empirically fit via a Wahba/Kabsch cross-check) was fully built,
  tested, and then found live-invalid: the correction came out ~identity by construction, since that
  same Wahba fit had already proven the *attitude* (rotation matrix) exactly correct -- the real bug
  was never the rotation, it was that `[0,0,1]` isn't any real pixel's actual look direction in a way
  a rotation can fix without being a no-op. Reverted cleanly. The real fix: `build_camera()` now runs
  the real WAC pipeline (`isis_wac.run_pipeline`, refactored to take `flip: bool` directly instead of
  a `Camera`, to break the resulting circular data dependency, and made idempotent so it's safe to
  call again later, e.g. by Phase 6's own explicit call) and re-aims the boresight (`camera.
  look_at_rotation`) at the real, ISIS-determined ground point at the crop's true center pixel
  (`isis_wac.ground_point_at_pixel`) -- camera *position* is untouched, already proven exact. A real
  ~10-20s cost added to `build_camera()`, traded deliberately for actual accuracy. Live-validated:
  0.000km residual on two different real candidates; Phase 5A's synthetic-vs-basemap alignment
  visibly excellent post-fix. See `docs/history.md`'s dated entry for the full investigation,
  including exactly why the first (reverted) fix attempt couldn't have worked.
- **Resolved**: the stripe/crosshatch artifact reported in the synthetic render (worse in
  darker/shadowed areas, sometimes crosshatched). Root cause turned out to be **Lunaserv's DTM WMS
  layer itself**, not the float32-quantization theory this item originally proposed (tested directly
  and ruled out) -- a real, axis-aligned periodic artifact confirmed baked into Lunaserv's own native
  DTM tile via FFT/periodicity analysis, present regardless of requested resolution, CRS, or
  resampling kernel, and not fixable client-side (the server exposes no resampling control and no
  backing-store metadata). Fixed by switching the live default DEM source to USGS Astropedia's
  flat-file GLD100 distribution instead (confirmed, via the same FFT diagnostic and direct user
  inspection of a real reprojected render, to have none of Lunaserv's artifact). See
  `docs/data-sources.md`'s "Astropedia GLD100 flat file" section and `docs/history.md`'s dated entry
  for the full investigation -- including several dead ends (a notch filter, a native-ppd sweep, a
  GDAL approximate-transformer tolerance check) worth reading before re-deriving them from scratch.
- **Open, future enhancement: Astropedia's GLD100 only covers ±79° latitude** (`lunaserv.
  ASTROPEDIA_MAX_ABS_LATITUDE_DEG`) -- `fetch_dem_and_ortho` raises rather than silently falling back
  to the deprecated, artifact-affected Lunaserv path for any camera footprint that needs data beyond
  it, so a catalog-driven selection landing near either pole currently just fails outright for that
  image. NASA's VIRA project (`github.com/nasa/vira`, `scripts/download_dems.sh`) points at genuinely
  higher-resolution *polar* DEM data for exactly this gap -- real LOLA-derived polar mosaics from
  `pgda.gsfc.nasa.gov`/`imbrium.mit.edu`, down to 5 m/px near 87°S (LOLA ground tracks converge near
  the poles, giving denser altimetry there than equatorial GLD100 -- ironically *better* resolution
  right where Astropedia's coverage ends, not worse). Not implemented -- would need its own flat-file
  fetch/caching (a different host/product per polar region, `curl`-based like
  `cache.fetch_astropedia_gld100`, likely a comparable one-time size) and a coverage-based dispatch
  in `fetch_dem_and_ortho` (Astropedia inside ±79°, a polar LOLA product beyond it) rather than the
  current hard latitude guard.
- **Resolved**: Lunaserv's native geographic projection is fine for `sat_sim`'s forward render, but
  turned out to break the `mapproject --ref-map` round-trip (anisotropic degree-pixels away from the
  equator, not preserved by `--ref-map`) — fixed by requesting a per-camera local Orthographic CRS
  directly from Lunaserv (`IAU2000:30166`, still a single WMS fetch, no separate `gdalwarp` step).
  See `docs/data-sources.md`'s Lunaserv WMS section and `docs/history.md`'s dated entry.
- **Resolved**: Phase 3's displayed GLD100 DEM had small but real nodata gaps (~-3e38 sentinel
  values once passed through `dem_mosaic`'s hole-filling) right at the map's corners — the source
  Astropedia AOI fetch (a degree-space bbox, independently padded around the raw footprint) and the
  destination Orthographic working grid (a separate, also independently padded meters-space bbox)
  were never guaranteed to cover each other: a square's diagonal corners sit ~41% farther from center
  than its edge midpoints, so the degree-space padding — confirmed live, even at the generous default
  30% — undershot the destination grid's own corners by up to ~5km in some directions, regardless of
  how much padding was applied, since the padding was never actually being checked against what it
  needed to cover. Fixed by deriving the Astropedia fetch bbox directly from the destination grid's
  own bbox (`lunaserv.astropedia_coverage_bbox_deg`, via `rasterio.warp.transform_bounds`) instead of
  computing it independently — now structurally impossible for the two to disagree, since there's
  only one padded bbox. Live-validated: zero nodata pixels anywhere in the fetched DEM (was nonzero
  at all four corners). See `docs/history.md`'s dated entry.
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
  `docs/history.md`'s dated entry for the rationale). `notebooks/image_generation.py`'s Phase
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
- **Resolved**: `select_tie_points`'s die5 point-selection footprint's high drop rate under
  `resolve_crop_pixels` (2-3 of 5 points on real candidates, the real camera not seeing them at all).
  Root cause was two-layered: (1) `crop_footprint_corners` was still the deprecated SPICE
  approximation (now `_crop_footprint_corners_spice_approx`), disagreeing with the real crop's
  footprint by ~11-15% in extent — fixed by `crop_footprint_corners_for_camera` querying the real
  crop directly via `campt` image-to-ground (`isis_wac.ground_point_at_pixel`), cheap by construction
  since `camera.build_camera()` already runs the real pipeline (see the item above); (2) even after
  that, points still dropped — traced to a real, confirmed numerical instability in `campt`'s own
  ground-to-image solve within ~5-10px of a *cropped* cube's edge (image-to-ground succeeds there,
  but a ground-to-image query at that exact resulting lon/lat then fails) — fixed with a
  `_CROP_EDGE_MARGIN_PX` inset (20px) when querying the crop's own corners. Live-validated: 5 of 5
  tie points now resolve on the real default candidate (was 2-3 of 5), confirmed visually in Phase
  6A. A separate, more extreme near-polar test candidate (~-81 to -83° latitude) still drops some
  points — the axis-aligned lon/lat bounding-box approximation this module's whole point-selection
  approach relies on breaks down that close to a pole (severe longitude convergence). **Accepted, not
  a bug to fix**: tie points are a debug/QA overlay, not a correctness-critical output —
  `resolve_crop_pixels` already degrades gracefully (drops individual points with a warning, only
  raises if literally none resolve), which is the right behavior for a candidate this display doesn't
  suit well, rather than something to chase with more geometry machinery. See `docs/history.md`'s
  dated entry.

## Development history

See `docs/history.md` for the phase-by-phase narrative — what was tried, what broke, and how each
design decision (framelet timing, sensor axis convention, catalog-driven selection, the perf fixes
in the sweep) was actually reached. Background/curiosity reading, not required before making a
change; this file and `docs/data-sources.md` describe current behavior.
