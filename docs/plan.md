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

The **live default image comes from a catalog-driven multi-orbit search**, not a single hardcoded
product: `notebooks/dataset_manifest.csv` (checked in, frozen) holds the throttled,
illumination-filtered result of one such search; `generate_dataset()` renders the chosen one(s)
through the pipeline described above. `dataset.images_for_window()` is the still-live version of
that same catalog-query/evaluate logic, now used to resolve an already-selected orbit-sequence
window (`dataset_selection.resolve_orbit_sequence`) rather than to search fresh. There is no
current dependency on any one specific EDR product or framelet index — see
`docs/data-sources/lroc-wac-edr-cdr.md` for the couple of specific products still used as
regression-test fixtures, and `docs/history.md` if you're curious how the demo evolved from a
single hand-picked product to this.

## Architecture (`src/trntest/`)

| Module | Responsibility |
|---|---|
| `config.py` | `TrntestConfig`/`load_config()` — endpoints, paths, product IDs, tunables. TOML file + `TRNTEST_*` env var overrides. |
| `cache.py` | Local-mirror disk caching for all external fetches (NAIF, Lunaserv, LROC) — see `docs/caching.md`. |
| `product_registry.py` | Intermediate-product access-discipline primitives (`writes_product`/`reads_product`/`deletes_product`, `atomic_publish*`) — see `docs/intermediate-product-discipline.md`. |
| `spice_kernels.py` | Selects/downloads the minimal SPICE kernel set for a date and furnishes it (`fetch_and_furnish`) — see `docs/data-sources/spice-kernels-isis.md`/`spice-kernels-naif.md`. |
| `camera.py` | Poses the synthetic camera from SPICE trajectory/orientation data (`build_camera`) and solves its corrected FOV (`solve_corrected_fov`) — see `docs/reproject-fov-investigation.md`. |
| `illumination.py` | Sun/orbit geometry via SPICE (sun elevation/azimuth, sub-solar point, node-crossing search) plus the angle-wraparound math helpers `dataset_selection.py`/`plotting.py` use. |
| `maneuver_detection.py` | Detects likely propulsive maneuvers in LRO's reconstructed-orbit SPK via step changes in angular momentum/orbital energy (`find_maneuver_candidates`) — see the module docstring for the derivation. |
| `catalog.py` | PDS ODE REST API client — lists EDR/CDR products by time range, matches EDR↔CDR pairs (`list_products`, `find_matching_cdr`). |
| `dataset_selection.py` | Orbit-level TRN-OD dataset selection (`notebooks/select_datasets.py`): picks multi-day, maneuver-free orbit spans jointly diverse in solar hour angle, then hands one selected window to `dataset.py`. |
| `dataset.py` | Public multi-image API: `images_for_window()` evaluates EDR candidates over a time window (throttled/illumination-filtered); `generate_dataset()` renders the selected ones. |
| `lunaserv.py` | Fetches DEM (Astropedia GLD100) + ortho (WAC_EMP PDS4) imagery for a camera's footprint and preps both for `sat_sim`, including Hapke-relit hillshade blending — see the module docstring and `docs/data-sources/astropedia-gld100.md`/`wac-emp-pds4.md`. |
| `render.py` | Renders the synthetic image via ASP `sat_sim`, then converts the camera to a CSM Frame sidecar via `cam_gen` (`run_sat_sim`). |
| `wac.py` | Extracts a band-separated VIS mosaic from a WAC CDR product via manual byte offsets (`fetch_vis_mosaic`) — superseded by `isis_wac.py` as the demo's real-WAC comparison method, kept for its own test coverage. |
| `isis_wac.py` | Steps a WAC EDR through ISIS3's own pipeline (`lrowac2isis`→`spiceinit`→`lrowaccal`→`framestitch`→`crop`→`cam2map`) as this project's real-WAC comparison path — see `docs/external-tools.md`'s ISIS Pushframe pipeline section. |
| `sfs_validation.py` | Cross-checks `lunaserv.hapke_shade_ortho` against ASP `sfs` run as an independent forward renderer, for DEM-aware ground truth on the Hapke shading math. |
| `tie_points.py` | Projects the same 5 ground points (4 corners + center) into both the synthetic render and the WAC crop, for the comparison figure's explicit tie points (`select_tie_points`/`resolve_crop_pixels`). |
| `orientation.py` | Notebook-display-only north-up rotation (does not touch the sensor model). |
| `plotting.py` | Comparison-figure plotting: raw-pixel/geometry checks (`plot_render_vs_basemap`, `plot_overlay`/`plot_overlay_toggle`/`plot_zoom_blink`), a quantitative brightness diff (`compute_brightness_matched_diff`), and dataset-selection scatter plots. |
| `tasks.py` | Two `huey` (sqlite-backed) task queues driving `trn_dataset.py`'s `populate()`/`populate_via_workers()`, one per execution mode (`immediate=True` in-process vs. `immediate=False` multi-worker). |
| `trn_dataset.py` | `TrnTestDataSet`/`TrnTestEntry`/`TrnTestImage` — a structured, resumable dataset folder; `populate()`/`populate_via_workers()` drive generation sequentially or across worker processes. |
| `session.py` | `Session` facade — thin one-line delegators so notebook cells don't repeat `config=...`. |
| `craters.py` | Robbins craters catalog overlay: fetches/caches the PDS4 CSV, builds a spatially-indexed GeoPackage, and returns ellipse polygons for a raster's footprint (`crater_overlay_layer`) — see `docs/data-sources/robbins-craters.md`. |

`notebooks/image_generation.ipynb` reads the checked-in, now-frozen `notebooks/dataset_manifest.csv`
(the notebook that used to regenerate it, `data_set_selection.ipynb`, was removed), populates and
reads from the shared `trn_dataset` folder
via `TrnTestDataSet`/`TrnTestEntry`/`TrnTestImage`, and drives the rest of the pipeline end to end —
see `README.md` to run it, and AGENTS.md's "Working conventions" for how to validate changes
against it.

`notebooks/select_datasets.py` is a separate, early-stage/exploratory notebook (`dataset_selection.py`
+ `plotting.py`'s new functions) for picking *multiple* maneuver-free multi-orbit TRN-OD test
datasets, jointly diverse in solar hour angle -- not wired into the demo pipeline above, and doesn't
touch `dataset_manifest.csv`. Does now bridge one selected orbit-sequence into the older EDR-list
world, though: its last two cells call `dataset_selection.resolve_orbit_sequence` on
`selected_datasets.iloc[0]` (one selected window, not all of them, same iterate-fast-on-one
discipline as elsewhere in this project) and then `TrnTestDataSet.create()` on the result, into its
own `orbit_sequence_dataset` folder -- separate from `image_generation.ipynb`'s `trn_dataset`, since
this pipeline isn't the demo's canonical dataset yet. Also writes `orbit_sequence.csv` (the
selected window's own row) alongside `manifest.csv` in that folder, for debugging/provenance. Stops
short of `populate()` -- no rendering from this notebook yet.

## Known open items (resolve as encountered, record findings in `docs/data-sources.md`)

- **Resolved: `TrnTestDataSet`/`TrnTestEntry`/`TrnTestImage` implemented** — see `trn_dataset.py`'s
  row in the Architecture table above for the design.
  `image_generation.ipynb` rewired onto it; `dataset.generate_dataset()` itself is untouched, just
  no longer the notebook-facing generation path. `TrnTestDataSet.populate()` grew a `limit` parameter (not in the original planning session, added
  once a real need for it came up): stops after genuinely new work on `limit` distinct entries, for
  splitting a large dataset's population across multiple separate worker invocations against the
  same folder. `image_generation.ipynb` uses `populate(limit=1)` against the *full* manifest
  (`TrnTestDataSet.create(..., images, ...)`) to keep its own single-image scope — this was
  originally worked around by passing `images.head(1)` instead (before `limit` existed); that
  workaround is gone now. One remaining deliberate deviation from the original planning session's
  literal pseudocode: `TrnTestImage.plot_overlay()` calls `plotting.plot_overlay_toggle` (the auto-blinking
  overlay/base GIF), not the plain `plotting.plot_overlay` that pseudocode named, since the notebook
  had already adopted the toggle version and reverting it would be a real UX regression, not a
  neutral relocation. `reproject` remains unimplemented (reserved folder only), as designed.
- **Resolved (on `feature/reproject` branch, not yet merged to `main`): `reproject` implemented.**
  `sat_sim` fed by the real WAC crop instead of the Lunaserv basemap, through the same camera as
  `hillshade`. The synthetic-camera FOV bug found along the way (the naive symmetric FOV didn't
  fully fit inside the real crop's own footprint) is now folded directly into `camera.build_camera()`
  itself (`solve_corrected_fov`), not a separate `reproject`-only camera variant -- decided after
  the user's own requirement that `hillshade`/`reproject` stay pixel-grid-identical (for future
  SSIM/diff-style comparison between them), which only a shared, corrected `Camera` can guarantee;
  `crop` (real source data) is unaffected and naturally larger, providing the margin `reproject`
  needs. Validated unchanged across 4 real candidates spanning 38.5°N to -67.5°S (~100% valid-pixel
  coverage), then live end-to-end through the real `TrnTestReprojectImage(TrnTestHillshadeImage)`
  class and the flagship `image_generation.ipynb` notebook -- which also surfaced and fixed a real,
  separate regression in `tie_points.die5_points`'s point-placement anchoring (see `tie_points.py`'s
  row above). A separate boresight-bias tangent raised along the way (modeling the *existing*
  boresight correction as a `cv` bias instead of a rotation) is still open, not started -- see
  `docs/reproject-fov-investigation.md` for the full trail, including what's left before merging to
  `main` (notebook wiring, more validation at dataset scale).

  **Resolved (2026-08-21): `reproject` is now permanently wired into `notebooks/image_generation.py`**
  as Phase 8 -- mirrors Phase 5's own A/B geometry checks (raw quality vs. basemap, then a
  `mapproject` overlay) against `entry.reproject`, reusing Phase 4's `tie_point_results` and Phase
  5's crater layer as-is (both already type-agnostic across `TrnTestImage` subclasses), plus a
  valid-pixel-fraction print as the direct, permanent answer to this investigation's own coverage
  question. Phase 2's `dataset.truncate`/`populate` calls now pass `product_types=("crop",
  "hillshade", "reproject")` explicitly to include it -- still opt-in, `trn_dataset.PRODUCT_TYPES`
  itself is unchanged (just `crop`+`hillshade`). **Still open**: only validated end-to-end on this
  one entry (`M1327210646CE`) through the real class and notebook -- dataset-scale validation across
  the rest of the manifest remains a separate follow-up, not done in this pass.

  **The FOV correction was briefly anisotropic (`fu != fv`), then reverted back to isotropic.** The
  anisotropic version (kept a bit more of the real crop's margin) surfaced a small, real, never-fully-
  explained residual (~1-8px, <0.6% of the footprint, constant not growing with distance) between the
  CSM and Pinhole `mapproject` reprojections of the same corrected camera -- confirmed specific to the
  asymmetric case (a symmetric `fu=fv` camera matches exactly, 0px) and, on a later pass, confirmed
  *invariant* to how the anisotropy is encoded across the CSM state's fields (three different but
  mathematically-equivalent encodings all gave the identical residual), ruling out an encoding bug on
  this project's side -- root cause is some deeper compiled-`usgscsm` quirk with anisotropic Frame
  models, not fixable without its source. Combined with two *other* real bugs the anisotropy caused
  along the way (`cam_gen` silently averaging `fu`/`fv` into one isotropic `m_focalLength`, and the
  `die5_points` anchoring regression above) and the concern that other downstream CSM/ISIS consumers
  might hit the same kind of friction, the user's own call was to revert: `solve_corrected_fov` now
  solves `fu`/`fv` exactly as before but collapses them to one shared, isotropic
  `f = max(fu, fv)` rather than keeping them separate. Live-validated: 100.0% coverage on all 4
  candidates (actually improving one from 99.83%), at the cost of a ~4-6% smaller cross-track
  footprint (along-track was already the tighter constraint on every candidate, so along-track extent
  is essentially unaffected); the CSM/Pinhole residual is now exactly 0px again, confirmed live.
  `render._correct_csm_focal_length_anisotropy` (and its dedicated tests) are deleted -- dead code
  once `fu == fv` always. See `docs/reproject-fov-investigation.md`'s "RESOLVED: reverted to an
  isotropic FOV" section for the full trail, including the anisotropic derivation kept for rationale.
- Whether `--save-as-csm` state JSON is an acceptable stand-in for a literal ISD file for whatever
  comes after this demo.
- Confirm the lunar frame kernel defining `MOON_ME` loads correctly so SPICE can output that frame
  directly; sanity-check against the known GLD100/LOLA convention.
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
  `docs/data-sources/astropedia-gld100.md` and `docs/history.md`'s dated entry
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
  See `docs/data-sources/lunaserv-wms.md` and `docs/history.md`'s dated entry.
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
  (`docs/history.md` Phase 12, `docs/external-tools.md`'s "ISIS Pushframe pipeline" section) turned out to be
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
  `docs/external-tools.md`'s "ISIS Pushframe pipeline" section and `docs/history.md`'s dated entries for the
  full investigation, including the ISD ephemeris-time bug found and fixed when reprojecting a
  cropped (not full-swath) cube; `notebooks/wac_isis.py` remains the step-by-step version for
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
- **Resolved**: `plotting.OverlayLayer` (a `geoseries` + style, drawn via `.boundary.plot(...)` or
  `.plot(...)`) replaced per-layer-type named parameters on `plot_overlay`/`plot_overlay_toggle`, so
  adding the crater-ellipse overlay below cost no new parameters. The footprint outline
  (`outline_geoseries`/`overlay_outline_color`) stayed a separate, always-present parameter since
  it's the geometry-validation reference the Phase 5/6 comparison exists to show, not an optional
  annotation.
- **New (2026-08-23): `src/trntest/crater_depth.py`** — first step toward grading crater sharpness
  (`ARC_IMG`, per the entry just above, isn't a real freshness proxy; this database has no
  degradation field at all). A simplified reimplementation of Breton et al. 2019's depth method
  (60th-percentile rim-ring elevation minus 3rd-percentile interior elevation, off a DEM + crater
  polygon) against this project's own local GLD100 DEM and Robbins ellipse polygons — the original
  authors' own reference script's per-pixel area-weighting collapses to a plain `np.percentile` on
  this project's isotropic-meters grid, where every "inside" pixel already covers the same real
  area. `crater_depths_for_footprint` stores a `None` depth (kept as a row, not dropped) for any
  crater too close to GLD100's own ±79 deg coverage limit, rather than switching to a coarser,
  interpolation-artifact-prone global DEM just for polar coverage — see
  `docs/crater-grading.md` for the full
  DEM-source comparison. Module + synthetic-fixture tests (`tests/test_crater_depth.py`) plus one
  real-data throughput test against a live GLD100 tile (`tests/test_crater_depth_gld100_tile.py`,
  `@pytest.mark.heavy`) — 129/129 real, fully-contained craters in a real equatorial 512x512px tile
  got a valid depth, ~19.4ms mean per crater, extrapolating to a rough ~6.9 hour single-threaded
  estimate for the whole non-polar database (see `docs/crater-grading.md` for the full caveats on
  that number). Not yet wired into `image_generation.ipynb` or related to an actual sharpness grade; both
  are real next steps.

  **Done (2026-08-26): `crater_depth.stoffler_fresh_depth_km`** — the reference-depth half of an
  actual sharpness grade (Stoffler et al. 2006, *Reviews in Mineralogy and Geochemistry* 60(1),
  519-596, DOI `10.2138/rmg.2006.60.05`): a fresh crater's expected depth for a given diameter, the
  classic two-regime lunar depth-diameter relation (simple craters steeper, complex craters
  shallower, crossing at ~10.58 km). Implemented as `min(simple_regime, complex_regime)` rather than
  an explicit branch on the crossover -- provably identical to the textbook piecewise form for any
  `diameter_km > 0` (the simple-crater curve is the smaller of the two below the crossover, the
  complex-crater curve is smaller above it, and they cross exactly once), so the elementwise minimum
  picks the right regime on both sides with no branch and is exactly continuous at the crossover by
  construction. 4 new tests confirm both regimes, exact continuity, and vectorization.

  **Done (2026-08-26): `src/trntest/crater_depth_batch.py`** — the whole-database precompute
  `crater_depth.py`'s own docstring already called out as a real next step, tiled for cache
  coherence rather than a naive independent-per-crater loop. See `docs/crater-grading.md` for the
  full design (ownership-by-center vs.
  padded-raster-extent, why a direct read off GLD100's own raw grid would have been wrong, a real
  `nodata`-tag bug caught and fixed along the way) and real measured timing (~13.6 hours for the
  whole grid single-threaded -- slower than the earlier naive ~6.9-hour estimate, since that number
  never accounted for real per-tile reprojection overhead). `grade_database_via_workers` is the real
  multi-worker path, mirroring `trn_dataset.TrnTestDataSet.populate_via_workers`'s own established
  pattern (`tasks.start_consumer`/`stop_consumer`, generalized to take a `huey_module` argument
  rather than duplicate that subprocess-management code for a second task domain) -- live-validated
  end to end against real data. A third-party dataset that looked like it might shortcut this whole
  precompute (`huggingface.co/datasets/juliensimon/lunar-craters-robbins`, Robbins craters plus a
  pre-computed depth column) was investigated and rejected -- see `docs/crater-grading.md` for the
  concrete red flags found on direct inspection. Not yet combined into an actual sharpness score
  (deliberately: `stoffler_fresh_depth_km` and the measured-depth precompute are kept independent,
  so a future change to the grade formula itself doesn't require re-running the multi-hour DEM pass)
  or wired into any notebook.

  **Done (2026-08-26): the actual sharpness score, a real review notebook, and the pieces needed to
  build it.** `crater_depth.sharpness_ratio(depth_m, diameter_km)` -- the user's own chosen formula,
  measured depth over `stoffler_fresh_depth_km` (units matched, ~1.0 for a crater as deep as a fresh
  one of its size "should" be). `crater_depth_batch.consolidate_graded_geopackage` now computes and
  stores it as a `sharpness` column (cheap, safe to recompute on every consolidation, unlike the
  depth measurement itself). `craters.query_craters_for_raster`'s bbox-deriving logic is factored out
  as `craters.raster_bbox_deg` (unpadded), shared with a new `crater_depth_batch.grade_footprint` --
  grades just the tiles touching one candidate's real footprint (`tiles_covering_bbox`, snapped to
  the same grid `iter_tile_origins` defines) rather than the whole database, for reviewing/validating
  grading against a single image without the multi-hour full-database cost; writes into the same
  per-tile CSVs `grade_database`/`grade_database_via_workers` use, so it's fully compatible with a
  later full run. `notebooks/crater_sharpness_review.py`/`.ipynb` (new, minimal setup matching
  `hapke_hillshade.ipynb`'s pattern -- reuses `image_generation.ipynb`'s Phase 1-2 but skips
  `dataset.populate()` entirely, since only `entry.dem_ortho_result` is needed) grades + consolidates
  the default candidate's footprint, then shows two real results: a sharpness-colored crater overlay
  on the same hillshade basemap/sparse-dashed style Phase 5B/6B use, and a log-log depth-vs-diameter
  2D histogram with the Stoffler curve overlaid. **Real, live result**: the bulk of the measured
  population clusters at or below the reference curve at small diameters -- physically sensible (most
  real craters in a random sample are somewhat degraded, not freshly formed), a real positive signal
  for both the depth measurement and the reference formula, not just a working plot. The histogram
  needed log-log axes/bins after a first linear attempt was genuinely unreadable (one bin near the
  smallest diameters held the vast majority of craters) -- also the standard way this kind of
  power-law crater data is presented in the literature, not just a fix for this one plot. The overlay
  needed `min_major_km` filtering (mirroring Phase 5B/6B's own convention) after the unfiltered
  population (thousands of small craters over one footprint) made the sparse-dashed ellipses collapse
  into an unreadable cloud.
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
  6A. A separate, more extreme near-polar test candidate (~-81 to -83° latitude) still dropped some
  points — the axis-aligned lon/lat bounding-box approximation this module's whole point-selection
  approach relied on broke down that close to a pole (severe longitude convergence). Originally
  accepted as a known limitation rather than fixed (tie points being a debug/QA overlay, not a
  correctness-critical output); **since fixed** (see `docs/history.md`'s later dated entry):
  `select_tie_points` now does the box-inscribing/intersection/placement geometry in a shared local
  Orthographic frame (meters), not raw lon/lat degrees — `inscribed_bbox`/`intersect_bbox`/
  `die5_points` are pure planar-geometry functions, so this only changes what coordinates they're
  fed, via `rasterio.warp.transform` (not a hand-rolled projection). With point *selection* now
  trustworthy near the poles too, `resolve_crop_pixels` no longer degrades gracefully — it raises
  immediately on any unresolved point instead of dropping it with a warning, since a failure now
  means something is fundamentally wrong rather than an expected edge case. See `docs/history.md`'s
  dated entries.
- **Resolved (partially — see "Open" below)**: 6B's real-WAC/basemap overlay is visibly not
  perfectly aligned (small, "not huge" per direct user observation). Investigated whether an ASP
  bundle-adjustment tool could correct the SPICE-derived pose via feature-matching against the
  basemap. **Found a real, unrelated bug, substantially (not fully) improved**:
  `isis_wac.run_cam2map_for_crop`'s own `PATCHSIZE=4` (chosen in an earlier phase using only an
  aggregate crop-vs-full correlation number) introduces a real, visible striping artifact —
  confirmed via a direct `PATCHSIZE` sweep (1/2/4/8/14) at native resolution; switched to
  `PATCHSIZE=1` (no coverage trade-off, ~6s/crop runtime cost). A high-pass quantitative check found
  only a modest ~2.4% reduction in fine-scale energy versus the old default, and a faint residual
  remains visible on close inspection — judged consistent with genuine, modest photometric
  discontinuities at framelet transitions (inherent to any patch-based warp), not the more severe
  missing/bad-data-looking pattern `PATCHSIZE=4` showed, and a reasonable stopping point (diminishing
  returns past here). Distinct from, and not fixed by, the already-known framestitch dead-column
  artifact — confirmed by patching that artifact out of a real cube copy (via GDAL's ISIS3 `rw+`
  write support) and re-running `cam2map`: zero visible change. See `docs/history.md`'s dated entry.
  **Open**: the actual camera-pose-correction question itself is unresolved. Research trail: ASP's
  standard `bundle_adjust`/`pc_align`/`image_align` recipe applies corrections via ASP's own
  `mapproject`, which is the CSM/Pushframe route already abandoned elsewhere in this pipeline for a
  confirmed severe bug — a dead end. ISIS's own `jigsaw` + `findfeatures` (space resection against a
  basemap, staying camera-model-native) is architecturally sound and USGS-documented practice for
  single images, but a real `findfeatures` spike found its control-point-construction step discards
  every match regardless of `TARGET=`/`GEOMTYPE=` settings — likely because the basemap is a plain
  GDAL-exported GeoTIFF, not something ISIS itself map-projected, so it lacks whatever ISIS-native
  geometry metadata that step needs (not yet confirmed or fixed).
  **Implemented, not yet wired into the main pipeline**: a from-scratch 2D approach sidestepping
  both blockers — `src/trntest/pose_alignment.py` feature-matches the already
  map-projected WAC crop directly against the basemap (no camera model, no ISIS control network),
  fits a similarity transform (translation+rotation+uniform scale, deliberately the simplest
  plausible model, not asserted as physically correct — see the module's own docstring) via RANSAC,
  and applies it to the WAC raster's own georeferencing so it drops into the existing
  `plotting.plot_overlay_toggle` unmodified. `notebooks/pose_alignment_spike.py` exercises the whole
  pipeline against the current default candidate — a real signal, not yet validated enough to wire
  into the main pipeline. **Follow-up**: the map-projected WAC crop being matched against turned out
  to be substantially oversampled — `cam2map`'s `PIXRES=map` forces its output onto the basemap's
  ~100 m/px working grid, but a direct measurement (`cam2map PIXRES=camera`) found the crop's own
  real native resolution is ~184 m/px (~1.8x coarser), specific to the WAC side (the basemap ortho is
  genuinely ~100 m/px native). Added `pose_alignment.native_wac_gsd_m`/`downsample_to_gsd`
  (area-averaging) to match at native resolution instead of the interpolated grid; live result:
  matches more than doubled (106 → 259), inliers nearly doubled (53 → 91), and inlier residual
  tightened from 1.45px to 0.84px once measured in native pixels.
  **Concluded (for this exercise): the correspondences are real, not RANSAC noise.** With enough
  inliers to support it, added `fit_affine_correction`/`fit_homography_correction`
  (`apply_homography_correction` for the latter, since a homography isn't an `affine.Affine`) and
  compared all three models' blink overlays side by side. Direct user visual inspection of the
  homography overlay: "beautiful... real benefit to the higher-order model here, not just noise."
  **User's own conclusion, the deliberate stopping point for this branch of investigation**: this
  validates that real, usable correspondences exist here — the next real step is a proper
  projection-informed alignment (an actual camera-model correction — fixing the SPICE-derived pose
  directly, or the `jigsaw`/`findfeatures` space-resection route above, now with real evidence a
  correction is warranted at all) rather than continuing to refine this 2D homography spike further.
  Not picked up yet. **LightGlue (DISK extractor) added as a second matcher**
  (`pose_alignment.match_features_lightglue`, `~3x` SIFT's match/inlier count on the default
  candidate, 767 vs. 259, at a very slightly looser per-point fit — not a quality win on this
  already-easy candidate, but real headroom for future shadowed/low-texture EDRs SIFT might not
  find enough points on at all; direct user visual confirmation the alignment quality holds).
  See `docs/history.md`'s dated entries (Phases 52–55) for the full trail.
  **Done, with a real, honest negative-ish result (as of 2026-08-19): a projection-aware 3D bundle
  adjustment, now fit against real control points and visually compared.** `src/trntest/
  control_network.py` (done, tested) converts tie points into real ISIS control points. ISIS's own
  `jigsaw` was tried and hit a real, root-caused, unfixable bug in its PushFrame framelet search
  (confirmed via a tautological, mathematically-guaranteed-zero-error control network that still
  produced huge `jigsaw` residuals) -- pivoted to a hand-rolled Python ground-to-image forward
  projection instead (`src/trntest/wac_camera_model.py`), complete end-to-end: the optics chain
  (validated to exact 0.000px agreement with real `campt`), the framelet search
  (`find_framelet_and_project`/`calibrate_et_per_crop_line`, live-validated to 0.00m ground error
  round-tripped through `campt`'s trusted inverse), and the optimizer (`fit_pose_correction`, a
  single frozen 6-DOF correction via `scipy.optimize.least_squares`). Fit against the real 477-point
  control network (from 767 LightGlue matches): residual mean 4.42px -> 3.36px, dominated by a small
  (~0.18deg) camera-frame rotation. `isis_wac.apply_pose_correction_to_crop` bakes the fitted
  correction into a copy of the crop cube's cached pointing (patches the `InstrumentPointing`
  Table's `ConstantRotation` via `tabledump`/`csv2table`, live cross-validated to <=0.015px against
  the forward projector's own prediction) so the existing, unmodified `cam2map`/
  `plotting.plot_overlay_toggle` path reprojects and displays it with no new warp code -- wired into
  `notebooks/pose_alignment_spike.py`, reviewed live by the user. **The real finding**: this only
  closes ~24% of the real gap (residual ~813m -> ~618m at this crop's own ~184m/px native GSD, both
  well above the homography spike's own ~150-165m) -- a single frozen 6-DOF rigid pose correction
  doesn't explain most of the true misalignment. Leading suspect, not yet tested: this pipeline's
  ground truth is still ellipsoid-only (no DEM elevation, see `control_network.py`'s own docstring),
  which would show up as spatially-varying error a rigid pose bias can't capture but a flexible 2D
  homography can silently absorb -- a DEM-aware shape model (`spiceinit shape=user`) is the
  planned next step. **See `docs/wac-jigsaw-investigation.md` for the full technical detail** (exact
  ISIS source citations, every constant's provenance, the serial-number bug and its fix) and
  `docs/external-tools.md`'s `campt`/`tabledump`/`csv2table` entries for the reusable mechanism facts.

  **The leading suspect was it (2026-08-20): the ellipsoid was the real bug, not the camera pose.**
  `isis_wac.run_spiceinit` hardcoded `shape=ellipsoid` for every real-WAC cube in the pipeline --
  confirmed to be the actual root cause of the parallax-like crater-rim misalignment that motivated
  this whole investigation in the first place, not a simplification safe to defer. Switched to
  `shape=user model=<ldem>` against ISIS's own real global lunar shape model
  (`isis_wac.ensure_lunar_shape_model`/`attach_dem_shape_model` -- `sample_lunar_dem_radii_batch` for
  camera-independent elevation sampling), one function changed, everything downstream (the 6B
  `cam2map` panel, `run_isd_generate*`, `control_network.resolve_control_points`'s jigsaw ground
  truth) inherits it with no further code changes. Live result on the flagship demo candidate,
  measured with `pose_alignment.py`'s own feature-matching machinery, **zero camera-pose correction
  applied**: mean raw offset 849m (ellipsoid) -> 124m (DEM), an 85% reduction, down near the
  matcher's own noise floor at this basemap's ~100m/px resolution. Everything on the synthetic/
  basemap side (`render.run_sat_sim`'s raytrace, Lunaserv's own orthorectified imagery) was already
  DEM-correct -- the asymmetry was entirely on the real-WAC side.

  **Camera-pose alignment (`pose_alignment.py`/`wac_camera_model.py`/`control_network.py`) is on the
  back burner, not superseded** -- explicitly the user's own framing, not a downgrade to "no longer
  needed." The DEM fix closes the specific gap that was visible on this candidate, but the capability
  to measure real alignment statistics (feature-matched offset, residual pixel error) independent of
  whether a correction gets applied is worth keeping regardless: there's no guarantee a future WAC
  product's initial SPICE-derived registration will be as accurate as this one turned out to be once
  the shape model was fixed, and this tooling is exactly what would catch that.

- **Open**: the user's requested "error-handling/fallback-consistency" quality audit (deliberately
  split into chunks so a single session wouldn't run out of context) only got through **Chunk A**
  (`tie_points.py` + `isis_wac.py`) before spiraling into a real fix (this file's die5/local-meters
  entry above and `docs/history.md`'s Phase 64) rather than staying a survey. Chunks B-E were never
  started, and there's no record elsewhere of which files/modules they were meant to cover -- next
  session should ask the user to re-scope chunks B-E from scratch (or just re-run the audit idea
  fresh) rather than assume a prior chunking plan still applies.
- **New, first minimal prototype built (2026-08-21): per-image Jupyter/HTML reports.** A
  standalone, one-HTML-page-per-entry report, separate from `image_generation.py`'s long
  hand-curated demo notebook -- `notebooks/report_template.py` (real `{{ name }}` text
  substitution via `trntest.report.render_template`, not papermill's `parameters`-tag mechanism,
  after that turned out unable to support a true one-line `load_entry("<path>", "<id>")` call with
  literal values -- see `docs/proposed-tasks/report-plan.md`'s "Mechanism" section; deliberately compact,
  one-liner cells calling `src/trntest/report.py`, no per-cell markdown headers; not committed as
  a paired notebook, since its `{{ }}` source can't execute standalone) +
  `scripts/generate_report.sh`/`scripts/render_report_template.py` (substitute -> jupytext sync ->
  papermill execute -> nbconvert to HTML, `ExtractOutputPreprocessor` on so figures land as real
  files under `images/` rather than base64-embedded, `In[N]:`/`Out[N]:` prompt gutters suppressed
  but code cells left visible). Deliberately minimal first pass (one raster + a couple of manifest
  fields), hand-run repeatedly against multiple entries, confirmed both the images-as-separate-
  files property and that each entry's own data renders (not a stale default). Growing the
  report's content to match `image_generation.py`'s Phase 5/6/8 comparisons and
  building the multi-entry index page (`<iframe>`-based navigation across entries) are both real,
  not-yet-started follow-ups. See `docs/proposed-tasks/report-plan.md` for the full design.

- **Open (2026-08-22, Phase 71/72) — both the normal-tilt fix and a new Hapke-ratio relighting fix are now permanent and unconditional (no opt-out parameter for either), but their combined effect on the real-WAC-crop match is still unresolved, and empirically worse, not better.** `lunaserv._terrain_photometric_angles`'s DEM-gradient surface normal uses `dem + sphere_sag` (not raw `dem`) as its gradient input, closing the tangent-point-fixed-frame gap Phase 70 found, independently validated against real ISIS `campt` ground truth at the ellipsoid limit (max |diff| ~0.018 deg, `tests/test_lunaserv_campt_validation.py`, a new permanent `@pytest.mark.heavy` test, `usgscsm` now in `docker/Dockerfile`). Separately, `hapke_shade_ortho` relights the ortho texture by H(i,e,g)/H(reference-geometry) instead of a bare rescaled H(i,e,g) (Phase 72) — `config.lunaserv_ortho_layer` was confirmed (via its own PDS4 README) to be ASU's WAC_EMP product, itself photometrically normalized to a fixed i=30/e=0/g=30 geometry via an empirical (Boyd et al. 2012), non-Hapke function, so the old blend wasn't relighting at all. Both were made unconditional on the user's own explicit call ("the need for the correction seems very clear") despite neither being confirmed to improve, and the Hapke-ratio fix being confirmed to *worsen*, the brightness-matched diff for the one candidate tested (8.6853 → 9.2425, measured with the new `plotting.compute_brightness_matched_diff`, a reusable tool built specifically because this project's own historical diff numbers turned out to be irreproducible ad hoc scripts). A follow-up diagnostic (not kept) confirmed real `campt`'s own plain angles stay ellipsoid-normal-based even with a DEM shape model attached, so it can't validate the real DEM-aware case the way `phocube`'s own broken `localincidence` flag (Phase 70) was supposed to — that gap remains genuinely open, and is judged the harder, non-"low-hanging" priority relative to further chasing this regression. Three further live visual observations (a real east-brightening gradient our hillshade underrepresents; an apparent ~10 deg shadow "rotation" confirmed *not* to be a sun-azimuth bug via real `campt` cross-check, 0.006 deg agreement; crater floors reading anomalously bright in the real WAC vs. our hillshade) are recorded but not investigated further. Full rationale in `_terrain_photometric_angles`/`hapke_shade_ortho`'s own docstrings and `docs/history.md`'s Phase 70/71/72 entries. **Phase 73 confirmed `phocube`'s DEM-mode backplanes are a dead end even against this project's own local DEM** (not just the coarse global shape model Phase 70 tried) -- the DEM-aware validation gap has no ISIS-side tool left to try. **Phase 74 ran a genuinely independent cross-check instead: ASP `sfs`, used as a pure forward renderer** (`sfs_validation.py`, `notebooks/sfs_validation.ipynb`) — lands in the same ballpark as the real WAC crop (brightness-matched diff ~0.0061 vs. our own pipeline's ~0.0032-0.0043) but doesn't resolve the regression above. The `sfs` panel's own brightening toward one corner is real, explained geometry (a genuine ~50 deg real phase-angle swing across the frame, mostly cross-track parallax) compounded by a real double-counting bug in `true_albedo_map` (found and fixed same session) and, more significantly, by a structural gap: `sfs`'s own reconstructed CSM camera has no way to represent `along_track_correction`, so its implicit geometry runs measurably hotter (lower phase, stronger opposition surge) than `hapke_shade_ortho`'s own default output in exactly the region that differs most — narrowing what this tool is actually useful for as an ongoing cross-check (see `docs/history.md`'s Phase 74 follow-up for the full mechanism). `TrnTestEntry.dem_ortho_result`'s disk-resume path still has no consistency check between its two independently-cached files (a real, if narrow, bug Phase 74 hit and worked around, not fixed at the source). **Phase 75 found a much cleaner win from the same tool: `sfs`'s Lambertian mode (`--reflectance-type 0`, no emission/phase term at all) can be inverted for `sfs`'s own independent incidence angle with zero Hapke-model dependence — confirmed to match `lunaserv.real_geometry_photometric_angles` to ~0.02 deg mean/~0.5 deg max across a whole candidate's real coverage region, closing the DEM-aware ground-truth gap Phase 70/73 left open.** Now a permanent `@pytest.mark.heavy` test (`tests/test_sfs_validation_lambertian_incidence.py`) validating the full-resolution DEM-aware case, not just campt's ellipsoid-only sparse-point check. Validates incidence only (emission/phase depend on the view vector, which Lambert's law has no term for) — the along-track-correction question from Phase 74 remains open for those. **Phase 76 found and fixed the actual source of that ~0.02/~0.5 deg residual: a second, real approximation gap (`dem` embedded along the tangent point's fixed Up axis instead of each point's own true local radial direction — "relief displacement," exact closed form now used) beyond the sagitta/normal-tilt fixes above.** Reduces to the previous formula exactly at `dem=0` (no regression risk for the ellipsoid-limit `campt` test above, confirmed unchanged at 0.018 deg). Post-fix: mean/max diff against `sfs`'s own incidence dropped to ~0.0005 deg both — essentially exact agreement, not just "within known caveats." Checked directly whether this closes the real-WAC-crop regression from Phase 70/72 or the Hapke-model comparison gap from Phase 74: **no, both essentially unchanged** — this fix is real and independently proven correct, but neither of those separate, larger, still-open issues was ever explained by it. **Phase 78 follow-up (2026-08-23, user's own direct visual review of the regenerated notebooks)**: the WAC_EMP-PDS migration improved the aggregate brightness-matched diff (see this row's own opening paragraph) but did **not** resolve the most visually obvious of the three observations above — the real west-to-east brightness gradient our hillshade still underrepresents is still directly visible post-migration. Confirms this gradient is a real, still-open issue independent of the Lunaserv-affine-stretch bug Phase 78 fixed, not explained or subsumed by it — still unexplained and not investigated further.
- **Open, found during the Phase 69 Hapke-calibration work, not implemented**: `lunaserv.fetch_real_hapke_params` samples ISIS's real calibration cube once per image, at the footprint's own center — real spatial variation exists within one footprint (checked directly: `wh`/`b0`/`hg1` a few percent of their own full-Moon range, `hg2`/`hh` somewhat more) but is secondary to the placeholder-vs-real gap this fixed. Per-pixel sampling (reprojecting the calibration cube onto the same working grid `reproject_astropedia_elevation_to_local_grid` builds the DEM/ortho on) would be a real further refinement.
- **Open (2026-08-23, Phase 78), pre-existing, not caused by the WAC_EMP migration but found and partially fixed while validating it**: `lunaserv.fetch_dem_and_ortho`'s DEM output filename (`dem_filled-tile-0.tif`) carries no suffix tied to `extra_footprint_lonlat_deg`, unlike `ortho_shaded_filename`'s own careful suffix discipline — any two calls against the same shared per-candidate output directory with *different* footprints silently clobber each other's DEM file, leaving a mismatched ortho/DEM pair on disk for anything that resumes it later (already a known, unguarded risk per `sfs_validation.run_sfs_forward_render`'s own docstring). Confirmed to trip a real `ValueError` there live, and separately to masquerade as a false *geometry* regression in `test_sfs_validation_lambertian_incidence.py` (0.0005 deg → 0.379 deg) — both traced to the same stale DEM pairing, not an actual geometry bug. **Three concrete call sites that triggered it are now fixed**: `notebooks/hapke_hillshade.py`'s direct Lambertian `fetch_dem_and_ortho` call, `tests/test_wac_emp_ortho_source.py`'s equivalent, and (found live 2026-08-30, via a real `entry.dem_ortho_result` shape mismatch while regenerating `notebooks/sfs_validation.ipynb`) `notebooks/along_track_correction.py`'s own `fetch_dem_and_ortho` call — all now pass `extra_footprint_lonlat_deg=entry.crop_footprint`, matching `entry.dem_ortho_result`'s own internal call. See `docs/history.md`'s Phase 78 entry for the full mechanism. **The underlying design gap remains open**: `dem_filled-tile-0.tif`'s own name still carries no `extra_footprint_lonlat_deg`-aware suffix (or hash), the same class of fix `ortho_shaded_filename` already applies for its own parameters — a future caller that forgets to union in the right footprint can still reintroduce this. **Update (2026-08-23, `docs/history.md`'s Phase 79 entry)**: the DEM fetch was split out into its own function (`lunaserv.fetch_dem`, now `product_registry`-registered under `"dem_filled"`) specifically so this label has one legible, auditable writer — but eliminating `extra_footprint_lonlat_deg` as a caller-suppliable parameter entirely (the plan's original, fuller fix) turned out to need either a `session.py` public-API signature change plus three notebook re-executions, or a resume-check redesign around a bbox-derived filename hash, both bigger than that session's budget — deliberately deferred rather than done partway. Real callers today all still pass the identical footprint derivation, so no live divergence is known, same as before.
- **Resolved (2026-08-29/30)**: `notebooks/along_track_correction.ipynb`/`real_hapke_params.ipynb`/`reproject_spike.ipynb`/`pose_alignment_spike.ipynb` all call `fetch_dem_and_ortho`/`dem_ortho_result`, so were equally affected by the `ortho_source="wac_emp_pds"` default but weren't regenerated in Phase 78's own pass. `along_track_correction.ipynb`/`real_hapke_params.ipynb`/`pose_alignment_spike.ipynb` are now all regenerated under current code; `reproject_spike.ipynb` no longer applies (archived to `old_notebooks/`, frozen, not kept in sync going forward). `real_hapke_params.ipynb`'s regenerated numbers: placeholder Hapke params (`mean|diff|=5.62`) now slightly beat the real-calibration default (`6.25`) on its one candidate — a modest, single-candidate result, not investigated further; the real-calibration default isn't reconsidered on this alone.
- **Open (2026-08-23, Phase 78) — saturation in `stretch_reflectance_to_uint8`'s fixed `[0, 0.30]` display stretch is neither confirmed absent nor a validated, deliberate acceptance; it's an unresolved question.** Two distinct sources of potential saturation, both unresolved:
  1. `hapke_shade_ortho`'s own `relit_reflectance = ortho * ratio` can exceed `DISPLAY_STRETCH_REFLECTANCE_MAX` for geometries near opposition (`ratio > 1`). Phase 72's original docstring characterized a resulting white-saturated patch as "the physically correct behavior... not an artifact to avoid" — on review (2026-08-23), that line was never actually validated or deliberately signed off on as a real decision; it's improvised reasoning that was allowed to stand unchallenged, not a settled design stance, and shouldn't be cited as one.
  2. `DISPLAY_STRETCH_REFLECTANCE_MAX = 0.30` (Phase 78, this migration) was chosen from general knowledge of typical lunar reflectance ranges and confirmed empirically non-saturating (`min=32, max=227`) for exactly **one** real candidate (the frozen default, `M1327210646CE`) — not swept across other candidates/geometries. A brighter real feature (e.g. fresh crater rays, a near-opposition geometry) could plausibly clip under this same fixed constant.

  Saturated pixels bias `sfs_validation.true_albedo_map`'s recovered albedo (an already-documented residual limitation of using the quantized `uint8` output, now triggered at a different absolute reflectance threshold than before this migration) and reduce `compute_brightness_matched_diff`'s discriminating power in any clipped region. Resolving this needs an actual decision — e.g. a real multi-candidate saturation sweep to see how often/how badly it happens, then either accepting a quantified risk, widening the range, or reconsidering the Hapke-ratio clipping behavior itself — not just asserting either combination is fine.

## Development history

See `docs/history.md` for the phase-by-phase narrative — what was tried, what broke, and how each
design decision (framelet timing, sensor axis convention, catalog-driven selection, the perf fixes
in the sweep) was actually reached. Background/curiosity reading, not required before making a
change; this file and `docs/data-sources.md`/`docs/external-tools.md` describe current behavior.
