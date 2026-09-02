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
| `cache.py` | Local-mirror disk caching for all external fetches (NAIF, Lunaserv, LROC) — see `docs/caching.md`. |
| `camera.py` | Poses the synthetic camera from SPICE trajectory/orientation data (`build_camera`) and solves its corrected FOV (`solve_corrected_fov`) — see `docs/reproject-fov-investigation.md`. |
| `catalog.py` | PDS ODE REST API client — lists EDR/CDR products by time range, matches EDR↔CDR pairs (`list_products`, `find_matching_cdr`). |
| `config.py` | `TrntestConfig`/`load_config()` — endpoints, paths, product IDs, tunables. TOML file + `TRNTEST_*` env var overrides. |
| `craters.py` | Robbins craters catalog overlay: fetches/caches the PDS4 CSV, builds a spatially-indexed GeoPackage, and returns ellipse polygons for a raster's footprint (`crater_overlay_layer`) — see `docs/data-sources/robbins-craters.md`. |
| `dataset.py` | Public multi-image API: `images_for_window()` evaluates EDR candidates over a time window (throttled/illumination-filtered); `generate_dataset()` renders the selected ones. |
| `dataset_selection.py` | Orbit-level TRN-OD dataset selection (`notebooks/select_datasets.py`): picks multi-day, maneuver-free orbit spans jointly diverse in solar hour angle, then hands one selected window to `dataset.py`. |
| `illumination.py` | Sun/orbit geometry via SPICE (sun elevation/azimuth, sub-solar point, node-crossing search) plus the angle-wraparound math helpers `dataset_selection.py`/`plotting.py` use. |
| `isis_wac.py` | Steps a WAC EDR through ISIS3's own pipeline (`lrowac2isis`→`spiceinit`→`lrowaccal`→`framestitch`→`crop`→`cam2map`) as this project's real-WAC comparison path — see `docs/external-tools.md`'s ISIS Pushframe pipeline section. |
| `lunaserv.py` | Fetches DEM (Astropedia GLD100) + ortho (WAC_EMP PDS4) imagery for a camera's footprint and preps both for `sat_sim`, including Hapke-relit hillshade blending — see the module docstring and `docs/data-sources/astropedia-gld100.md`/`wac-emp-pds4.md`. |
| `maneuver_detection.py` | Detects likely propulsive maneuvers in LRO's reconstructed-orbit SPK via step changes in angular momentum/orbital energy (`find_maneuver_candidates`) — see the module docstring for the derivation. |
| `orientation.py` | Notebook-display-only north-up rotation (does not touch the sensor model). |
| `plotting.py` | Comparison-figure plotting: raw-pixel/geometry checks (`plot_render_vs_basemap`, `plot_overlay`/`plot_overlay_toggle`/`plot_zoom_blink`), a quantitative brightness diff (`compute_brightness_matched_diff`), and dataset-selection scatter plots. |
| `product_registry.py` | Intermediate-product access-discipline primitives (`writes_product`/`reads_product`/`deletes_product`, `atomic_publish*`) — see `docs/intermediate-product-discipline.md`. |
| `render.py` | Renders the synthetic image via ASP `sat_sim`, then converts the camera to a CSM Frame sidecar via `cam_gen` (`run_sat_sim`). |
| `session.py` | `Session` facade — thin one-line delegators so notebook cells don't repeat `config=...`. |
| `sfs_validation.py` | Cross-checks `lunaserv.hapke_shade_ortho` against ASP `sfs` run as an independent forward renderer, for DEM-aware ground truth on the Hapke shading math. |
| `spice_kernels.py` | Selects/downloads the minimal SPICE kernel set for a date and furnishes it (`fetch_and_furnish`) — see `docs/data-sources/spice-kernels-isis.md`/`spice-kernels-naif.md`. |
| `tasks.py` | Two `huey` (sqlite-backed) task queues driving `trn_dataset.py`'s `populate()`/`populate_via_workers()`, one per execution mode (`immediate=True` in-process vs. `immediate=False` multi-worker). |
| `tie_points.py` | Projects the same 5 ground points (4 corners + center) into both the synthetic render and the WAC crop, for the comparison figure's explicit tie points (`select_tie_points`/`resolve_crop_pixels`). |
| `trn_dataset.py` | `TrnTestDataSet`/`TrnTestEntry`/`TrnTestImage` — a structured, resumable dataset folder; `populate()`/`populate_via_workers()` drive generation sequentially or across worker processes. |
| `wac.py` | Extracts a band-separated VIS mosaic from a WAC CDR product via manual byte offsets (`fetch_vis_mosaic`) — superseded by `isis_wac.py` as the demo's real-WAC comparison method, kept for its own test coverage. |

`notebooks/image_generation.ipynb` reads the checked-in, now-frozen `notebooks/dataset_manifest.csv`
(the notebook that used to regenerate it, `data_set_selection.ipynb`, was removed), populates and
reads from the shared `trn_dataset` folder
via `TrnTestDataSet`/`TrnTestEntry`/`TrnTestImage`, and drives the rest of the pipeline end to end —
see `README.md` to run it, and AGENTS.md's "Working conventions" for how to validate changes
against it.

`notebooks/select_datasets.py` is a separate, early-stage/exploratory notebook
(`dataset_selection.py` + `plotting.py`'s new functions) for picking *multiple* maneuver-free
multi-orbit TRN-OD test datasets, jointly diverse in solar hour angle -- not wired into the
demo pipeline above, and doesn't touch `dataset_manifest.csv`. Does now bridge one selected
orbit-sequence into the older EDR-list world, though: its last two cells call
`dataset_selection.resolve_orbit_sequence` on `selected_datasets.iloc[0]` (one selected window,
not all of them, same iterate-fast-on-one discipline as elsewhere in this project) and then
`TrnTestDataSet.create()` on the result, into its own `orbit_sequence_dataset` folder --
separate from `image_generation.ipynb`'s `trn_dataset`, since this pipeline isn't the demo's
canonical dataset yet. Also writes `orbit_sequence.csv` (the selected window's own row)
alongside `manifest.csv` in that folder, for debugging/provenance. Stops short of `populate()`
-- no rendering from this notebook yet.

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
- **Resolved**: crater sharpness grading — `crater_depth.py` (Breton et al. 2019 depth method off a
  DEM + Robbins crater polygon), `crater_depth.stoffler_fresh_depth_km` (Stoffler et al. 2006's
  fresh-crater depth-diameter relation), `crater_depth_batch.py` (whole-database precompute,
  tile-based for cache coherence), and `crater_depth.sharpness_ratio` (measured depth over
  `stoffler_fresh_depth_km`) are implemented and validated — `notebooks/crater_sharpness_review.py`
  grades one candidate's footprint and shows a sharpness-colored crater overlay plus a depth-vs-
  diameter histogram against the Stoffler curve. See `docs/crater-grading.md` for the full design
  (DEM-source choice, tiling scheme, a rejected third-party pre-graded dataset). **Open**: a
  full-database precompute (all craters, not just one footprint) hasn't been run yet.
- **Resolved**: the real-WAC/basemap overlay's visible small misalignment was root-caused to
  `isis_wac.run_spiceinit` hardcoding `shape=ellipsoid` for every real-WAC cube — switching to
  `shape=user model=<ldem>` (ISIS's own real global lunar shape model, `isis_wac.
  ensure_lunar_shape_model`/`attach_dem_shape_model`) cut the measured feature-matched offset by
  ~85% (849m → 124m), down near the matcher's own noise floor. A projection-aware 3D bundle
  adjustment (`control_network.py`/`wac_camera_model.py`, a from-scratch hand-rolled forward
  projector after ISIS's own `jigsaw` hit an unfixable `usgscsm` Pushframe bug) was tried first and
  only closed ~24% of the gap before the DEM fix was found — a single frozen 6-DOF rigid pose
  correction can't explain spatially-varying error that a missing DEM (ellipsoid-only ground truth)
  would cause. See `docs/wac-jigsaw-investigation.md` for the full trail.
  **On the back burner, not superseded**: this tooling (`pose_alignment.py`/`wac_camera_model.py`/
  `control_network.py` — feature-matched offset/residual measurement, independent of whether a
  correction gets applied) is kept — there's no guarantee a future WAC product's SPICE-derived
  registration will be this accurate, and this is what would catch that.
- **Open**: the user's requested "error-handling/fallback-consistency" quality audit (deliberately
  split into chunks so a single session wouldn't run out of context) only got through **Chunk A**
  (`tie_points.py` + `isis_wac.py`) before spiraling into a real fix (see `docs/history.md`'s Phase
  64 entry) rather than staying a survey. Chunks B-E were never started, and there's no record of
  which files/modules they were meant to cover — re-scope from scratch rather than assume a prior
  chunking plan still applies.
- **In progress**: per-image HTML reports (`src/trntest/report.py`, `notebooks/report_template.py`,
  `scripts/generate_report.sh`/`scripts/render_report_template.py`) — a standalone one-page-per-entry
  report, separate from `image_generation.py`'s long hand-curated demo notebook. Minimal first pass
  validated (one raster + a few manifest fields, images rendered as real files not base64-embedded).
  Growing the report to match the notebook's Phase 5/6/8 comparisons and building a multi-entry index
  page are still open. See `docs/proposed-tasks/report-plan.md` for the full design.
- **Open**: the real-WAC-crop/hillshade brightness match has an unresolved regression and an
  unresolved validation gap. `lunaserv._terrain_photometric_angles`'s surface-normal computation and
  `hapke_shade_ortho`'s Hapke-ratio relighting were both made permanent/unconditional on the user's
  explicit call, despite the Hapke-ratio fix being confirmed to *worsen* the one measured
  brightness-matched diff (8.6853 → 9.2425) — not yet explained. Real `campt` ground truth can't
  validate the DEM-aware case (it stays ellipsoid-normal-based even with a DEM shape model attached),
  so ASP `sfs` was used as an independent forward-render cross-check instead (`sfs_validation.py`):
  its Lambertian mode's own independently-recovered incidence angle now matches
  `lunaserv.real_geometry_photometric_angles` to ~0.0005 deg mean, closing the DEM-aware validation
  gap for incidence — but confirming (not explaining) that the brightness regression and three other
  live visual observations (a real east-brightening gradient the hillshade underrepresents; an
  apparent ~10 deg shadow rotation confirmed *not* a sun-azimuth bug; anomalously bright real crater
  floors) remain genuinely open. `sfs` itself has a structural gap for phase/emission cross-checks:
  its reconstructed CSM camera can't represent `along_track_correction`. See `docs/history.md`'s
  Phase 70-79 entries for the full investigation trail.
- **Open**: `lunaserv.fetch_real_hapke_params` samples ISIS's real calibration cube once per image,
  at the footprint's own center — real spatial variation exists within one footprint (a few percent
  of `wh`/`b0`/`hg1`'s own full-Moon range, somewhat more for `hg2`/`hh`) but is secondary to the
  placeholder-vs-real gap this already fixed. Per-pixel sampling (reprojecting the calibration cube
  onto the same working grid the DEM/ortho use) would be a real further refinement.
- **Resolved (partially)**: `lunaserv.fetch_dem_and_ortho`'s DEM output filename carried no suffix
  tied to `extra_footprint_lonlat_deg`, unlike the ortho's own careful suffix discipline — two calls
  against the same shared output directory with *different* footprints could silently clobber each
  other's DEM file. The DEM fetch was split into its own function (`lunaserv.fetch_dem`, now
  `product_registry`-registered under `"dem_filled"`) so this label has one legible, auditable
  writer, and the three real call sites that had tripped this were fixed to pass
  `extra_footprint_lonlat_deg` explicitly. **Still open**: the filename itself still carries no
  footprint-aware suffix/hash — a future caller that forgets to pass the right footprint can still
  reintroduce this. Eliminating `extra_footprint_lonlat_deg` as a caller-suppliable parameter
  entirely was judged bigger than one session's budget and deliberately deferred.
- **Open, unresolved question**: whether `stretch_reflectance_to_uint8`'s fixed `[0, 0.30]` display
  stretch saturates. Two distinct sources, neither confirmed absent: (1) `hapke_shade_ortho`'s
  relit reflectance can exceed the max for geometries near opposition (`ratio > 1`) — an earlier
  docstring characterized the resulting white-saturated patch as "physically correct, not an
  artifact to avoid," but on review that line was never actually validated or signed off on as a
  real decision; it's improvised reasoning that stood unchallenged, not a settled design stance, and
  shouldn't be cited as one. (2) `DISPLAY_STRETCH_REFLECTANCE_MAX = 0.30` was chosen from general
  knowledge of lunar reflectance ranges and confirmed empirically non-saturating for exactly one real
  candidate (the frozen default) — not swept across other candidates/geometries (e.g. fresh crater
  rays, near-opposition geometry could plausibly clip). Saturated pixels would bias
  `sfs_validation.true_albedo_map`'s recovered albedo and reduce `compute_brightness_matched_diff`'s
  discriminating power in any clipped region. Resolving this needs an actual multi-candidate
  saturation sweep, not just asserting either combination is fine.
- **Resolved**: `hillshade`/`reproject` used to render at a fixed 256×256 (`config.
  DEFAULT_IMAGE_SIZE`), measurably 2-4x coarser than the ~100 m/px DEM/ortho inputs or the real WAC
  crop's own ~184 m/px. `DEFAULT_IMAGE_SIZE` is now `1316`, chosen to land ~100 m/px on the
  notebook's default candidate. `reproject` deliberately shares this size with `hillshade` (for a
  byte-identical pixel grid) even though its own real texture source (the WAC crop) caps out
  coarser. See `docs/resolution-investigation.md` for the full numbers.

## Development history

See `docs/history.md` for the phase-by-phase narrative — what was tried, what broke, and how each
design decision (framelet timing, sensor axis convention, catalog-driven selection, the perf fixes
in the sweep) was actually reached. Background/curiosity reading, not required before making a
change; this file and `docs/data-sources.md`/`docs/external-tools.md` describe current behavior.
