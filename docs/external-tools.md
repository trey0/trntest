# External tool/library reference

Current, stable facts about how the external tools and libraries this project depends on actually
behave — flags, formats, and known sharp edges, as distinct from [`docs/data-sources.md`](data-sources.md)'s
external *data*. Consult before writing new code against any of these tools; update this file (not
just code comments) when a concrete choice changes.

## ASP `sat_sim`

- Docs: https://stereopipeline.readthedocs.io/en/latest/tools/sat_sim.html
- Takes `--dem` + `--ortho` (a georeferenced/mapprojected image aligned to the DEM), and either
  auto-generates cameras (`--first/--last/--num/...`) or reads existing ones via `--camera-list`
  (one `.tsai`/CSM path per line).
- `--sensor-type pinhole` (default) writes `.tsai` Pinhole camera files; `--save-as-csm` also/instead
  emits a CSM model-state JSON sidecar. This CSM state JSON is what this project calls the "ISD
  sidecar" — technically a CSM *state* file, not a from-scratch ISD; double check at implementation
  time whether this distinction matters for whatever downstream tooling consumes it.
- Input DEM should have no holes (use `dem_mosaic --hole-fill-length`), extend well beyond the AOI.
  Fed in the per-camera local Orthographic projection (the same grid the ortho shares — see
  `fetch_dem_and_ortho`), not either DEM source's own native projection — both the deprecated
  Lunaserv-native path and the live Astropedia path reproject locally before `sat_sim` ever sees the
  DEM; no evidence `sat_sim` demands a local stereographic projection instead.
- **`sat_sim` applies no illumination/shading model of its own.** Per its own docs, it
  "unproject[s] an ortho image into a given camera... in the spirit of ISIS `map2cam`," generating
  output pixels via bicubic interpolation of the `--ortho` input. The DEM is used purely for
  ray/terrain-intersection *geometry* (which ground point a given camera ray hits) — the output
  pixel value is a direct geometric resample of whatever's already in the ortho, with no per-ray
  reflectance/sun-angle computation applied. Any relief/shading visible in a render is therefore
  whatever was already baked into the ortho texture, not something `sat_sim` computes — this
  project supplies that shading itself (`lunaserv.despeckle_and_shade_ortho` — a real Hapke BRDF via
  ISIS `photomet` by default, `hapke_shade_ortho`, with a plain Lambertian `shade_ortho` fallback;
  both lit with real SPICE sun geometry, not relying on any shading baked into the source imagery,
  which was never guaranteed to match the simulated frame's real sun angle in the first place).
- **`--dem-height-error-tol`'s default (0.001m) is too tight for this project's DEM and causes
  visible salt-and-pepper speckle** in the render (`sat_sim`'s ray/DEM-intersection root-finder
  misbehaves at scattered pixels). Root cause: Lunaserv's DTM layer serves planetocentric radius
  (~1.7e6 m) as float32, whose ULP (smallest representable step) at that magnitude is already
  ~0.125m — baked into the source data itself, not something fixable in
  `lunaserv.radius_to_elevation`'s own subtraction. **Confirmed empirically**: tightening the
  tolerance further makes the speckle dramatically worse (more, denser artifacts), loosening it to
  comfortably clear that ~0.125m floor eliminates it cleanly — neither outcome is subtle.
  `src/trntest/render.py`'s `DEM_HEIGHT_ERROR_TOL_M = 0.5` (a 4x margin above the float32 floor) is
  what `run_sat_sim` actually passes. Derived against Lunaserv's float32 data, but re-checked (not
  just assumed still valid) after switching the live default DEM source to Astropedia's coarser
  Int16 encoding — see [`data-sources/astropedia-gld100.md`](data-sources/astropedia-gld100.md)'s
  own precision note; no change needed. Two other theories were tested and ruled out first:
  ortho-side noise/aliasing (despeckling the ortho, and even a large `--blur-sigma`, left the speckle
  pattern essentially unchanged) and the ortho source layer's own quality (switching layers changed
  the *baseline* noise level but not this specific artifact).
- **`--save-as-csm` only applies when `sat_sim` generates its own cameras** — when using
  `--camera-list` with pre-existing `.tsai` files (this project's case), it's silently a no-op (only
  the rendered `.tif` is written, no camera file at all). To get the CSM/"ISD" JSON sidecar for a
  specific `.tsai`, use ASP's `cam_gen` instead:
  `cam_gen <rendered.tif> --input-camera <cam.tsai> --camera-type pinhole --refine-intrinsics none
  -o <cam.json>` — `--refine-intrinsics none` keeps the pose/intrinsics exactly as given (pure
  format conversion, not a re-solve).
- The resulting file (`USGS_ASTRO_FRAME_SENSOR_MODEL` state) is **not plain JSON** — the first
  line is a bare model-name string, and the rest is the JSON blob (standard CSM "state string"
  convention: `f"{model_name}\n{json_body}"`). Parsers must skip line 1 before `json.loads`.
- `.tsai` Pinhole format (https://stereopipeline.readthedocs.io/en/latest/pinholemodels.html):
  `P = R*Q + C` — camera center `C` and rotation `R` (camera-to-world) are in a body-fixed Cartesian
  frame (ECEF-equivalent), independent of the DEM/ortho's map projection. For the Moon this is the
  **Mean Earth (MOON_ME)** frame, matching USGS lunar cartographic conventions (GLD100/LOLA).

## ASP `mapproject`

- Docs: https://stereopipeline.readthedocs.io/en/latest/tools/mapproject.html
- Syntax: `mapproject <dem> <camera-image> <camera-model> <output-image> [options]` — the geometric
  inverse of `sat_sim`: instead of rendering an image from a DEM+camera, it reprojects an existing
  image *back* onto the map using a DEM+camera. Accepts the `cam_gen`-produced CSM/ISD JSON sidecar
  directly as `<camera-model>` with `-t csm` (confirmed working — no separate ISD-to-`.tsai`
  conversion needed).
- **`--ref-map <path>`**: reads the output projection *and* grid size from an existing mapprojected
  image, rather than deriving them from `--t_srs`/`--tr`/`--mpp`/`--ppd`. Pointing this at the same
  DEM used to produce the input image guarantees the output lands on that DEM's exact pixel
  grid/projection — i.e. the same grid as any other raster derived from that DEM (this project's
  `lunaserv.py` outputs, e.g. `DemOrthoResult.ortho`), with no separate reprojection/alignment step
  needed to overlay them. This is what `render.run_mapproject` uses.
- Output nodata is real `NaN` (confirmed empirically, `Float32` output by default) — not a
  huge-magnitude sentinel like `wac.MISSING_CONSTANT` elsewhere in this codebase, and not something
  `plotting.valid_pixel_mask`'s threshold check is needed for; ordinary NaN-aware handling
  (`rioxarray`/matplotlib already treat NaN as transparent/masked) is sufficient. **This depends on
  the input format**, though — see "ISIS Pushframe pipeline" below: an ISIS `.cub` source instead
  carries ISIS's own huge-magnitude NULL sentinel straight through.
- **Round-trip alignment validated**: mapprojecting `sat_sim`'s own synthetic render back through its
  own CSM sidecar (same DEM, same camera model, forward-then-inverse) overlays real terrain features
  pixel-precisely against the hillshade-based ortho — confirmed visually (individual crater rims line
  up across the full frame), consistent with going forward and back through one self-consistent
  camera model. This is a different, much simpler case than the real-WAC `mapproject` striping issue
  below — that pipeline mapprojects an ISIS-processed *real* WAC cube (with real sensor/framelet-
  stacking artifacts feeding in), not a clean synthetic render through its own exact camera model.

## ISIS Pushframe pipeline: install, `lrowac2isis`/`framestitch`, `cam2map`

An alternative/complement to `wac.py`'s manual framelet-stacking approach: reproject a real WAC CDR
swath onto the DEM via ISIS/CSM (`mapproject`) and re-render it from a synthetic pose (`sat_sim
--ortho`). The earlier part of the chain (EDR fetch through `framestitch`) is implemented for real as
`src/trntest/isis_wac.py`; the `mapproject`/`sat_sim --ortho` half was only ever run in a throwaway
container and isn't part of `trntest` (see `docs/plan.md`'s open items for current status) — these
are the durable tool facts either path needs, recorded so they don't have to be re-derived.

- **Install**: `mamba create -n isis --override-channels -c usgs-astrogeology -c conda-forge
  --channel-priority flexible isis ale` — the plain `-c usgs-astro` channel name from older docs
  is wrong/404; the current channel is **`usgs-astrogeology`**. This single command pulled ISIS
  10.0.0 and ALE 1.2.0 (`isd_generate`) together with no dependency conflicts once the channel name
  was fixed and priority set to `flexible` (`strict` fails to solve — ISIS's own build pulls
  `embree`/`qt`/`bullet` pins that strict priority can't reconcile against conda-forge). Needs
  `ISISROOT=<conda env prefix>` set explicitly (e.g. `/opt/conda/envs/isis`) — without it,
  `IsisPreferences` isn't found and every app aborts immediately.
- **`$ISISDATA` size — bulk download is avoidable**: `spiceinit web=yes` (USGS's SPICE Web Service)
  **works for WAC**, not just NAC as the docs imply — after running it, `$ISISDATA/lro/kernels/`
  doesn't exist locally at all, yet the resulting label correctly lists the real per-date CK/SPK files
  used remotely (~14s/call). **Gotcha**: `downloadIsisData`'s `--dry-run` flag does not skip the
  transfer in this version (10.0.0) — real files are written despite it.
  **What's actually needed locally, found after two wrong turns**: `--no-kernels` does *not* shrink
  `base` to near-zero as its name suggests — `base`'s ~26 GB is dominated by `base/dems/` (global
  shape models), untouched by that flag. And `spiceinit`'s default `SHAPE=*SYSTEM` needs a real lunar
  DSK/DEM cube (`$base/dems/ldem_128ppd_Mar2011_clon180_radius_pad.cub`) even for plain
  pointing/calibration (`lrowaccal`/`framestitch`), not just for terrain-intersection as its name
  might suggest — so skipping `dems/` entirely breaks it ("USER ERROR NAIF DSK file [...] does not
  exist"). The actual minimal, correct fetch: `spiceinit ... shape=ellipsoid` (a plain reference
  ellipsoid, no DSK needed — sufficient while this module stops at `framestitch`) plus
  `downloadIsisData base $ISISDATA --include
  "{kernels/lsk/**,kernels/pck/**,kernels/sclk/**,kernels/fk/**,kernels/ik/**,kernels/iak/**}"`
  (~5 MB, skips `dems/`/`examples/`/`kernelTesting/`) plus `downloadIsisData lro $ISISDATA
  --no-kernels` for `lrowaccal`'s dark/flat calibration cubes (~5 GB, NAC+WAC together, no narrower
  filter found). Real one-time cost for this module's scope: **~5 GB total**.
- **`lrowac2isis`** (EDR `.IMG` only, confirmed CDR is not accepted) splits into 4 cubes
  (`*.uv.even.cub`, `*.vis.even.cub`, `*.uv.odd.cub`, `*.vis.odd.cub`). Confirmed via `catlab`:
  `vis.even.cub` is **704 samples × 7532 lines × 5 bands** — the 5 VIS filters come out as 5
  distinct ISIS cube bands already correctly separated, no manual byte-offset extraction needed
  (unlike `wac.py`'s current hand-picked `VIS_BLOCK_OFFSET`).
  **Line count, confirmed empirically (`isis_wac.crop_window_for_camera`)**: the cube preserves
  **14 lines per original EDR frame** — exactly `wac.VIS_BLOCK_HEIGHT`, *not* 1 line/frame (an
  earlier, wrong assumption briefly shipped in `crop_window_for_camera` before this was checked
  against real data). Confirmed on two real products: `M1327210646CE` measures exactly `258 frames
  × 14 = 3612` lines (its EDR label's own `nframes`, cross-checked directly), and
  `M1329714703CE` measures exactly `538 × 14 = 7532` lines. `lrowac2isis` does *not* TDI-sum each
  frame down to a single output line — it keeps the same per-frame line structure `wac.py`'s own
  raw-CDR byte-layout code already assumes, it just separates the 5 VIS bands automatically instead
  of needing a manual byte offset. `even`/`odd` individually already carry the *full* frame count's
  worth of lines each (not half) — `framestitch` deinterlaces/merges them into correct along-track
  order, it doesn't concatenate two half-height inputs into a full-height one (confirmed: `stitched`
  measures the same height as `even`/`odd` individually).
- **`framestitch`'s `FLIP` is a real, per-pass manual decision, not automatic** — directly tested
  both values on `M1329714703CE` (this repo's documented non-mirrored/`k=1` reference product):
  `flip=false` produced a coherent, recognizable lunar surface; `flip=true` produced a scrambled,
  heavily-banded image. `flip=false` being correct here is consistent with this product's existing
  "non-mirrored" characterization from `camera.boresight_rotation_k`'s convention (see
  `docs/data-sources/lroc-wac-edr-cdr.md`) — a useful cross-check, but ISIS did not derive this
  automatically; it had to be determined the same way `boresight_rotation_k` already does
  (real-SPICE-geometry-informed, here just visual A/B).
- **`isd_generate`** (ALE) run against the *unstitched*, calibrated even/odd cubes produces a CSM
  `USGS_ASTRO_PUSHFRAME_SENSOR_MODEL` state JSON per parity. Unlike ASP's own `cam_gen` output
  (bare model-name string on line 1, then JSON — see the `sat_sim`/ISD note above), ALE's
  `isd_generate` output is **plain, direct JSON** with `name_model` as a top-level key — no line to
  skip.
- **`isd_generate -i` on an ISIS-`crop`ped Pushframe cube produces a wrong-but-plausible-looking
  ISD** — confirmed empirically: the generated `starting_ephemeris_time`/`ending_ephemeris_time`/
  `center_ephemeris_time` and `instrument_pointing.ck_table_start_time`/`ck_table_end_time` all read
  as if the crop still started at the *original*, pre-`crop` cube's first line — ISIS's `crop` app
  (even with its default `PROPSPICE=true`) does not itself re-anchor a Pushframe cube's per-line
  pointing cache to the new starting line (it does correctly update `ck_table_original_size` to the
  cropped line count, just not the *start* time). Left unpatched, `mapproject`'s output lands on
  completely wrong ground geometry (measured 0.44 pixel correlation against the known-good, same
  real ground area from the full-cube mapproject; should be ~1.0) despite running without any error
  and looking like plausible terrain. Neither re-running `spiceinit` on the cropped cube, nor
  cropping earlier in the pipeline (the calibrated parity cubes, before `framestitch`, rather than
  the stitched cube after) changes this — both produce the exact same wrong result, so the bug isn't
  about pipeline ordering. **Fix** (`isis_wac.run_isd_generate`): patch the 5 scalar time fields
  above by `time_offset_s = (line_offset / VIS_BLOCK_HEIGHT) * isd["interframe_delay"]` after
  generation — the underlying `instrument_pointing.ephemeris_times`/`quaternions`/
  `angular_velocities` arrays are untouched by `crop` (confirmed identical length before/after crop)
  and still hold the entire pass's real, absolute-time-tagged samples, so correcting just the scalar
  time fields is sufficient for the CSM model to interpolate the correct poses; fixed the
  correlation to 0.999. **Also confirmed**: `lrowaccal` explicitly refuses to run on an
  already-`crop`ped cube ("USER ERROR: This application can not be run on any image that has been
  geometrically transformed ... or cropped") — cropping must happen after calibration/`framestitch`,
  not before.
  **Correction: the 0.999 correlation above was a false positive**, measured on too small a sample.
  A user's manual visual inspection of the actual notebook output later caught real remaining
  defects (a 3-region gap in the valid-data mask, a real position offset) that this project's own
  automated checks had missed. Deeper investigation (see "`usgscsm`'s `groundToImage` bug" below)
  traced the true root cause to a bug in `usgscsm`'s own `groundToImage` implementation, not fixable
  by any ISD field patch — this whole ISD-authoring approach was abandoned in favor of ISIS's native
  `cam2map` (see below).
- **`mapproject -t csm <dem> <cub> <json> <out>`** works directly against the unstitched per-parity
  cube + its own ISD (no separate stitched-cube pairing step needed for this) — confirmed on real
  data: ASP's bundled GDAL reads `.cub` natively (no `isis2gdal`/conversion needed for the DEM
  input either — `isis2gdal` doesn't exist in ISIS 10; use GDAL's native ISIS3 driver directly). A
  4337×5367 output at 100 m/px took **~23s**. Confirms ASP's own caveat concretely: the resulting
  orthophoto has **real, significant periodic striping at framelet boundaries** (visually severe in
  a 1:1 crop, not a display artifact) — matches ASP's documented "not fully mature... artifacts at
  framelet borders" warning exactly, for this project's actual reference product.
- **Even/odd parity cubes are temporal alternation, not a spatial split** — a real, easy-to-repeat
  mistake: "even"/"odd" sounds like a same-frame split (e.g. interlaced TDI rows), but at *every*
  nominal frame slot, exactly one of `vis.even.cal.cub`/`vis.odd.cal.cub` has real (99% valid) pixel
  data and the other is 100% NULL. Mapprojecting one parity alone reprojects a sparse,
  ~50%-populated sequence, and `mapproject`'s resampling smears framelet content across the gaps —
  producing severe venetian-blind banding. Mapprojecting the properly **interleaved, stitched** cube
  instead resolves the vast majority of it: on `M1327210646CE`, 31% valid coverage with no
  recognizable terrain → **81% valid coverage with real craters visible throughout the frame**, same
  product, same DEM. What remains at that point is the small, already-understood, ~1%
  framelet-boundary dead-pixel pattern (`docs/data-sources/lroc-wac-edr-cdr.md`), not this banding.
  `isis_wac.py`'s `run_isd_generate`/`run_mapproject` implement this correctly (against `stitched`,
  never a lone parity).
- **`FLIP` cross-check confirmed exactly as predicted** on a second product
  (`M1327210646CE`/frame 94): `boresight_rotation_k=3` (a mirrored pass, opposite yaw state from
  `M1329714703CE`'s `k=1`) needed `flip=true` — directly validating that ISIS's manual `FLIP` tracks
  the same real per-pass yaw-flip geometry `camera.boresight_rotation_k` already computes from
  SPICE, just as a hand-set flag instead of a derived one.
- **One parity alone leaves large coverage gaps** — mapprojecting only `vis.even` and feeding it to
  `sat_sim --ortho` with this repo's existing `camera_frame440.tsai` pose left most of the 256×256
  render as nodata; mosaicking `vis.even` + `vis.odd` orthophotos (`dem_mosaic`) filled in more but
  the render was still mostly nodata — the DEM/ortho AOI used for this spike (a generous south-polar
  cap, lat -75 to -90) evidently didn't fully contain frame 440's real footprint. Not investigated
  further (spike scope) — a real integration would need a properly sized/centered AOI, not a
  generic polar cap.
- **`mapproject`'s output nodata convention depends on its *input* format**: a synthetic render
  (plain GeoTIFF source) comes out with real IEEE NaN nodata (see ASP `mapproject` above) — but an
  ISIS `.cub` source carries ISIS's own huge-magnitude NULL sentinel (~-3.4e38) straight through into
  the output, with a GDAL `nodata` tag set to match (confirmed via `gdalinfo`/`rasterio`).
  `plotting._open_raster_dataarray` now always passes `rioxarray.open_rasterio(path, masked=True)`
  to handle both cases uniformly — without it, the sentinel dominates `plot.imshow`'s automatic
  vmin/vmax and washes the real signal out to a flat gray.
- **`isd_generate` always emits `framelet_order_reversed: false`, regardless of the cube's actual
  content** — it does not read `framestitch`'s own `DataFlipped` label field, which *does* correctly
  record whether `FLIP=TRUE`/`FALSE` was used. Left at the wrong (always-`false`) default,
  `mapproject` assigns each framelet the wrong pose whenever `flip=True` was actually used (any
  mirrored/`k=3` pass) — confirmed empirically: severe venetian-blind-style banding at every
  framelet boundary with the wrong value, completely gone with the correct one, on the same real
  product/DEM. A separate, similarly-named field, `framelets_flipped` (within-framelet *line* order,
  not framelet *sequence* order), was also tested and rigorously ruled out as unrelated — patching it
  produced a byte-for-byte identical `mapproject` output on a fixed grid; ASP's implementation
  doesn't appear to consume that field at all. `isis_wac.run_isd_generate` now patches
  `framelet_order_reversed` to match the same `flip` value `framestitch` was run with (threaded
  through via `FramestitchResult.flip`).
- **End-to-end wall time** for one product, from a cold `.IMG` fetch through a rendered `sat_sim`
  frame: a few minutes total (dominated by the ~1min `mamba create`, `EDR` fetch, and two ~23s
  `mapproject` calls) — fast enough that per-product cost isn't a practical concern.
- **Net verdict**: the chain is technically real and works end-to-end on this project's actual
  reference product, but isn't yet a clean drop-in replacement for `wac.py` — the framelet-boundary
  striping is a genuine, visible quality problem (not just ASP being cautious in its docs), and a
  usable comparison needs both parities mosaicked plus a correctly-sized AOI, neither of which is
  spelled out in ASP's own WAC example. See `docs/plan.md`'s open items for whether/how to integrate
  this into `trntest` itself.

## `usgscsm`'s `groundToImage` bug for Pushframe sensors, and the ISIS `cam2map` fix

The 0.999-correlation "fix" documented above (patching the cropped cube's ISD ephemeris-time
fields) was a false positive, caught by a user's manual visual inspection of the actual notebook
output (a 3-region gap in the valid-data mask, plus a real ~33-35km position offset from the
synthetic render's own overlay) — this project's own automated checks (a too-small correlation
sample, and `mapproject_single --query-pixel`, which turned out to be unreliable for this sensor
model even on the known-good full cube) had missed both. Full investigation:

- **Consulted Laura, Mapel & Hare 2020** ("Planetary Sensor Models Interoperability Using the
  Community Sensor Model Specification", DOI 10.1029/2019EA000713) at the user's suggestion — doesn't
  actually cover Pushframe sensors (its own conclusion lists them under future work), but its Table 1
  did usefully confirm `center_ephemeris_time` shouldn't matter independently of
  `starting_ephemeris_time`/`ending_ephemeris_time`, later confirmed exactly right and *why* from the
  C++ source directly (see below).
- **Version mismatch caught early**: this container ships `libusgscsm.so.2.0.1`, which differs
  meaningfully from the `main` branch on GitHub. Re-fetched the actual `2.0.1` tag before drawing
  any conclusions from source.
- **Traced the real mechanism**: `UsgsAstroPushFrameSensorModel::getImageTime()` computes
  `m_startingEphemerisTime + 0.5*exposureDuration + frameletNumber*interframeDelay`, then
  subtracts `m_centerEphemerisTime` before returning — i.e. all downstream position/quaternion
  lookups work in *time relative to center*, and since `m_t0Ephem`/`m_t0Quat` (the position/
  quaternion tables' own anchor times) are built the same way (`table's own absolute start time -
  center_ephemeris_time`), `center_ephemeris_time` algebraically cancels out of every interpolation
  index. This is why patching it alone had "zero effect" in earlier testing — not a mystery, just
  arithmetic. `starting_ephemeris_time` is the one scalar field that actually matters (it doesn't
  cancel), and it needs to be the crop's real absolute start time — confirmed independently via
  `campt` (ISIS's own native camera model) reporting the crop's own line-1/line-980
  `EphemerisTime` matching the naive/"forward" expectation to within 0.02s.
- **Isolated the real bug**: a controlled 2x2 test (patching `ck_table_start_time`/`ck_table_end_time`
  crossed with "forward"/physically-correct vs. "backward"/empirically-hacked
  `starting_ephemeris_time`) showed the `ck_table_*` fields have **zero** effect on `mapproject`'s
  output (byte-identical either way), ruling out the leading ISD-field hypothesis. Neither timing
  direction produced correct content either — a correlation check against the known-good full-cube
  reference gave only ~0.40 either way, and a ±5km shift search barely moved it (0.44 peak), ruling
  out a pure translation error. **`cam_test`** (an image→ground→image round-trip self-consistency
  check) showed a real, non-random defect: median ~67px error on the 70-framelet crop vs. ~17px on
  the full 258-framelet cube, and iterating the same transform never converged to a stable fixed
  point — genuine non-convergence, not a different-but-valid answer.
- **Root cause, confirmed from source**: `UsgsAstroPushFrameSensorModel::groundToImage` does *not*
  do a continuous per-pixel solve. It does an **unbracketed secant search over discrete framelet
  index** (`startFramelet=0`, `endFramelet=numFramelets-1`, up to 20 iterations,
  `offset = endDistance*(endFramelet-startFramelet)/(endDistance-startDistance)`) to find which
  framelet's along-track center is closest to a target ground point — with no check that a root is
  actually bracketed, and no monotonicity guarantee for `calcFrameDistance` (plausible given real
  ground-coverage overlap between adjacent Pushframe exposures). A 70-framelet crop gives this
  search a much shorter, more numerically fragile baseline than the full cube's 258 framelets.
  Confirmed this is exactly what ASP's `mapproject` calls, once per output pixel: `mapproject_single.cc`'s
  `demPixToCamPix()` calls `camera_model->point_to_pixel(xyz)`, and ASP's `CsmModel::point_to_pixel()`
  (`CsmModel.cc`) calls `m_gm_model->groundToImage(...)` directly.
  **Not just a crop-size issue**: cross-checking `cam2map`'s own reprojection of the crop against
  its reprojection of the *full* cube (same tool, same projection, only the input line range
  differs) gave 0.9999986 correlation over their full overlap — but the old ASP/CSM full-cube
  reference (`M1327210646CE.vis.cal.stitched-mapproj.tif`) only agrees with either at ~0.2-0.4,
  meaning the long-trusted "known-good" full-cube reference used throughout this notebook's earlier
  validation was itself measurably affected by this bug, just less severely.
- **Fix: bypass `usgscsm`/CSM/ASP `mapproject` entirely for the real WAC crop.** ISIS's own native
  camera model (reads pointing/timing directly from the cube's cached SPICE data, completely
  separate C++ implementation from `usgscsm`) has no such issue — confirmed via `campt` at the
  crop's center and all 4 edges (all resolve cleanly, no errors/NaNs) and `cam2map`'s own output
  contiguity (a clean row-by-row valid-fraction profile, no gaps, unlike the old CSM crop's
  3-region defect). This is very likely also why real LROC WAC global mosaics (which predate
  `usgscsm`, ~2018+) are solid: they almost certainly go through ISIS's native Pushframe model and
  `cam2map`, not the newer generic CSM plugin.
- **Getting `cam2map` onto the same coordinate system as the rest of the pipeline**: ISIS supports
  Orthographic projection natively (`$ISISROOT/appdata/templates/maps/orthographic.map`). Verified
  ISIS's implementation agrees with GDAL/PROJ's `+proj=ortho` to sub-micrometer precision for
  matching center lat/lon and spherical radius (cross-checked `cam2map`+`campt` against `pyproj`'s
  own forward projection at a real test pixel — first attempt at this check used `mappt`'s
  `coordsys=map` option and got wildly wrong numbers, traced to a tool-usage mistake: that option's
  reported X/Y reflects the `FROM` cube's own native projection, not the override, confirmed by
  back-computing the reported value against the `FROM` cube's own grid parameters). Two real
  gotchas found getting `cam2map` to actually use the custom map file: `PIXRES` defaults to
  `CAMERA` (auto-derives resolution from the image), silently ignoring the map file's own
  `PixelResolution` unless explicitly set to `PIXRES=map`; and `gdal_translate` on the resulting
  cube prints a `PROJ: proj_create_from_name` error to stderr (an ISIS/GDAL `PROJ_LIB` environment
  mismatch) that's harmless — confirmed the output CRS/transform are correct despite it, and the
  process still exits 0.
  **Deliberately not pixel-grid-aligned** to `DemOrthoResult`'s own raster (no `UpperLeftCornerX`/
  `UpperLeftCornerY` pinning, no post-hoc `gdalwarp`/resampling pass) — `plotting.plot_overlay`
  composites both rasters via their own real georeferenced coordinates (`rioxarray`/`xarray`), not
  a shared pixel grid, so matching *projection* (verified above) is sufficient; a separate
  resampling pass would have reintroduced exactly the kind of interpolation-quality/subtle-
  misalignment risk this whole detour was meant to avoid.
- **Composite via real coordinates, not raw array indexing — and reindex the smaller raster onto
  the larger one's grid, never the reverse.** `cam2map`'s own pixel dimensions and a
  `DemOrthoResult`'s (GDAL/rasterio-computed) never exactly agree, even at the "same" resolution and
  projection — each tool rounds physical bounds to pixel counts independently, so their grids are
  offset from each other by a fraction of a pixel that drifts smoothly across the image. Any code
  that windows/crops the *larger* raster down to the *smaller* one's shape by raw array index
  (`rasterio.windows.from_bounds` + manual `row_off`/`col_off`/height/width slicing) can silently
  end up off by one row/column once that drift shifts far enough — confirmed live: this broke
  `notebooks/along_track_correction.py`'s comparison outright (`ValueError: operands could not be
  broadcast together with shapes (1827,1688) (1828,1688)`) after some change (not the WAC_EMP
  ortho-source migration — tested directly, `ortho_source="lunaserv_wms"` hits the identical
  mismatch) shifted which side of a rounding boundary the two grids' shapes land on. Coordinate-based
  alignment (`xarray.DataArray.reindex_like`/`.interp_like`, `plotting.compute_brightness_matched_diff`'s
  approach) sidesteps the raw-shape assumption, but **reindexing in the "shrink big onto small"
  direction is still not safe**: `method="nearest"` with a half-cell `tolerance` can leave an
  all-NaN row right where the sub-pixel drift crosses the tolerance boundary (confirmed: the same
  crop/basemap pairing drops the identical row under this method, regardless of ortho source).
  `compute_brightness_matched_diff` avoids this by always reindexing the smaller raster onto the
  larger one's full grid (gaps then land only outside the smaller raster's real footprint, never
  inside it) — the safe direction. Where the "shrink" direction is unavoidable (e.g. cropping a
  basemap down to a small comparison window, as `along_track_correction.py` does), `interp_like`
  (linear interpolation, no tolerance cliff) is the more robust choice.
- **`cam2map`'s own `WARPALGORITHM=AUTOMATIC` default introduces real striping for this sensor**,
  found via manual visual check the automated correlation checks above had missed (a correlation
  check only sees pixels valid in *both* rasters — it can't detect matching coverage gaps). ISIS
  recommends `AUTOMATIC` for push frame cameras (locks `PATCHSIZE` to the full 14px framelet height),
  but a patch is silently dropped if its 4-corner affine fit isn't within 0.1px of the camera model's
  own computation — failing for roughly half the framelets here.
  **Fix**: explicit `WARPALGORITHM=forwardpatch PATCHSIZE=1` — coverage went from ~47% to ~71% with
  no more gaps, same as any `PATCHSIZE` 1-4, at a real but small runtime cost (~16s vs. ~10s, no
  coverage tradeoff). An earlier `PATCHSIZE=4` attempt looked fine by aggregate correlation
  (0.9954 vs. 0.9999986 at the broken default) but missed a real striping artifact concentrated at
  framelet boundaries, invisible to a correlation dominated by the much larger unaffected bulk of the
  image; a direct `PATCHSIZE` sweep (1/2/4/8/14) confirmed 8/14 markedly worse and 1 the clear best
  choice. **Not a complete fix**: a high-pass comparison found only a modest ~2.4% reduction in
  fine-scale energy vs. `PATCHSIZE=4`, and a faint striping residual remains visible at `PATCHSIZE=1`
  on close inspection — consistent with genuine, modest photometric discontinuities at framelet
  transitions (inherent to any patch-based warp), not pursued further.
- **Position residual — real at the time, since found to not be reproducible** (see
  `docs/data-sources/spice-kernels-isis.md`). Even after the fixes above, the crop's designated
  center pixel (checked directly via `campt`, not just an aggregate valid-pixel centroid) appeared
  to image ground ~11km from `crop_footprint`'s independently ray-traced center, despite both using
  the exact same frame-index formula. Ruled out a frame-selection bug first: reconstructed ISIS's
  own per-line time formula from three exact frame-boundary `campt` queries and confirmed
  `crop_window_for_camera` selects exactly the intended chronological frame range, correctly
  reflecting `framestitch`'s line-order reversal for this product's `flip=True` — ISIS's per-line
  time matches `camera.frame_et()` to within 0.016s for the corresponding frames. At the time, this
  was attributed to a missing second CK kernel (`moc42r_2019304_2019335_v01.bc`) that ISIS's
  `spiceinit web=yes` furnishes but `spice_kernels.py` didn't fetch. A later session built the fix
  for that and then, via direct re-verification against real `campt` output, found the ~11km
  discrepancy isn't reproducible at all — with or without the extra kernel. The true cause of this
  original number was never pinned down, most plausibly conflated with the `WARPALGORITHM` striping
  bug immediately above (both were being chased in the same investigation).
- **Also tried and ruled out**: ASP `mapproject -t isis` (uses ISIS's own native sensor model
  instead of `usgscsm`/CSM, given a plain `.cub` with no separate ISD sidecar) as a possible
  simpler alternative to the hand-written PVL + `cam2map` approach above. Tested directly against
  the same crop cube — immediately rejected: `"ERROR: Unusual input file... Seems to have Isis
  camera type 1. Check your data. Maybe it will work with CSM."` ASP's own ISIS session wrapper
  doesn't support this camera type (Pushframe) at all, not a flag/workaround issue. `cam2map`
  remains the only working native-ISIS reprojection path found for this sensor.

## The crop ISD sidecar's real accuracy

`isis_wac.run_isd_generate_for_crop`'s sidecar is accurate, not just informational — it exists
specifically so `crop/<edr_product>_crop.json` truthfully describes `crop/<edr_product>_crop.cub`'s
own dimensions and real acquisition time window, unlike a naive `isd_generate -i` run directly
against a cropped Pushframe cube (see the ISD-authoring bug above). **This does not make the
sidecar usable for actual reprojection** — like any Pushframe ISD in this codebase, `usgscsm`'s
`groundToImage` remains unreliable for that; real ground↔image lookups always go through
`resolve_ground_to_image_model`/`ground_to_image_pixel` instead, regardless of this patch. The
distinction matters: accuracy of the sidecar's own stated metadata and usability for reprojection
are two separate properties, and this only fixes the first.

**Live-validated on the real default candidate (`M1327210646CE`, `flip=true`/
`framelet_order_reversed=true`)**: `crop/M1327210646CE_crop.json`'s `image_samples`/`image_lines`
(704/980) exactly match `gdalinfo`'s real raster dimensions of `crop/M1327210646CE_crop.cub`.
`starting_ephemeris_time` matches real `campt` output at the crop's own line 1 to **0.016s**, and
`center_ephemeris_time` is exactly `(starting + ending) / 2` as expected. `ending_ephemeris_time`,
however, is **~1.39s** off from real `campt` output at the crop's own last line (980) — suspiciously
close to one whole `interframe_delay` (1.40625s here), not just numerical noise.

**Traced, not a bug in this patch**: re-ran the identical `campt`-vs-`isd_generate` comparison
directly on the full, *unpatched* stitched cube (`isis_wac.run_isd_generate`'s own pre-existing
output, untouched by any of this feature's new code) and found the exact same pattern: `campt` at
the full cube's **line 1** matches the ISD's `ending_ephemeris_time` (not `starting_ephemeris_time`)
to within ~1.39s, while `campt` at the full cube's **last line** (3612) matches
`starting_ephemeris_time` to within 0.017s. In other words, for this `flip=true` product, physical
row 1 corresponds to the *chronologically last* framelet and the physical last row to the
*chronologically first* one (consistent with `framestitch`'s `FLIP=TRUE` reordering, and with why
`campt`-based lookups — which read the cube's own embedded, physically-correct-by-construction
pointing data directly — remain the only trustworthy ground-to-image/image-to-ground path in this
codebase). `isis_wac.run_isd_generate_for_crop` shifts the full-cube ISD's already-computed
`starting_ephemeris_time`/`ending_ephemeris_time`/`center_ephemeris_time` by one shared
`time_offset_s`, so it faithfully **preserves** whatever gap already existed between
`isd_generate`'s own `ending_ephemeris_time` and true chronological end on the full cube — it
doesn't introduce a new one. This ~1-`interframe_delay` gap specifically on `ending_ephemeris_time`
(not `starting_ephemeris_time`) is therefore a pre-existing characteristic of `isd_generate -i`'s
own output for this flip direction, most plausibly an exclusive-fencepost convention (`ending` =
one frame *past* the last real sample, not the last sample's own time) — never previously noticed
because the `ck_table_end_time`/`ending_ephemeris_time`-have-zero-effect finding above meant nothing
before this feature ever checked its absolute accuracy against `campt`. Not investigated further
(out of scope for a field that was already known not to affect reprojection); worth reopening only
if a future consumer starts relying on `ending_ephemeris_time`'s own absolute accuracy, or if a
`flip=false` product ever shows a different-shaped discrepancy (in which case the fencepost
hypothesis above would be a good first thing to test directly, rather than re-deriving this
investigation from scratch).

## Patching a cube's cached pointing via `tabledump`/`csv2table`

`isis_wac.apply_pose_correction_to_crop` bakes a fitted 6-DOF pose correction into a copy of a
cube's `InstrumentPointing` Table, so ISIS's own `cam2map` picks up the corrected pose with no new
hand-rolled warp/resampling code. Two `tabledump`/`csv2table` gotchas this relies on:

- `csv2table`'s `label=` parameter (a flat-PVL file of the table's own extra keywords beyond the raw
  field records) is required, not optional, for `InstrumentPointing` specifically: it carries
  load-bearing metadata (`ConstantRotation`, `TimeDependentFrames`, `ConstantFrames`,
  `CkTableStartTime`/`EndTime`/`OriginalSize`, `FrameTypeCode`, `Description`, `Kernels`) that
  `tabledump`'s own CSV export doesn't include. Round-tripping without it silently drops
  `ConstantRotation`, producing a systematic ~0.08 deg pointing error, not an obvious crash.
  Extracting the label from `catlab` output via the `pvl` library (not hand-transcribing it) avoids
  precision/typo risk.
- ISIS 9.0's `csv2table` converts every CSV column to floating point unconditionally -- no
  `coltypes` parameter exists to declare otherwise (an earlier ISIS version needed one; `csv2table
  -help` confirms the current one doesn't accept it).

For a Pushframe WAC cube, the single fixed `ConstantRotation` matrix carries the -85621->-85620
(camera-to-spacecraft-bus) rotation; the 259-row time-dependent quaternion/AV/ET table represents
bus-to-J2000 and doesn't need to change for a camera-frame correction. The composition is
`ConstantRotation_new = delta_rotation.T @ ConstantRotation_original`, not `ConstantRotation_original
@ delta_rotation` -- cross-validated against `wac_camera_model`'s own forward projector using a
known synthetic test rotation (matched the projector's predicted pixel to ~1e-6; the untransposed
order placed the point outside the crop's coverage entirely). Likely explanation: ISIS's stored
matrix is the transpose of this project's own `R_A_to_B` convention (`v_B = R_A_to_B @ v_A`).

## LightGlue tie-point matching

`src/trntest/pose_alignment.py`'s `match_features_lightglue` is a second feature-matcher option
alongside the module's original SIFT-based `match_features` — a deep-learned local-feature
extractor (DISK) + learned matcher (LightGlue) pair, tried specifically to push match count/quality
higher for more challenging future EDRs (shadowed terrain, low texture) than classical SIFT can
reliably deliver.

- **No official PyPI package.** `cvg/LightGlue`'s own `pyproject.toml` declares `version = "0.0"`
  and reads dependencies from a separate `requirements.txt`; the upstream-documented install is
  `git clone` + `pip install -e .`. `docker/Dockerfile` instead installs directly from a pinned git
  commit (`lightglue @ git+https://github.com/cvg/LightGlue.git@<sha>`) for reproducibility — a
  floating `@main` reference would silently change behavior on every image rebuild.
- **Installed `--no-deps`, deliberately not listed in `pyproject.toml`'s own `dependencies`.**
  LightGlue's `requirements.txt` (`torch`, `torchvision`, `numpy`, `opencv-python`, `matplotlib`,
  `kornia`) would otherwise reinstall `opencv-python` alongside this project's own
  `opencv-python-headless` — both provide the `cv2` import and collide on install (confirmed: this
  is a real, known footgun, not a hypothetical one — pip/uv resolve them as two independently-named
  packages and don't detect the import-name clash, so both get installed and silently
  overwrite each other's files depending on install order). This project supplies `torch`/
  `torchvision` (CPU-only wheels, see `docker/Dockerfile`'s comment) and `kornia` (plain PyPI)
  itself instead, plus `opencv-python-headless` already covers the `cv2` import LightGlue's own code
  needs at import time (`lightglue/utils.py`, `lightglue/disk.py`, `lightglue/sift.py` all `import
  cv2` at module load — confirmed via direct inspection of the pinned commit's source, since
  `lightglue/__init__.py` eagerly imports every extractor submodule regardless of which one is
  actually used, so all of their import-time dependencies apply unconditionally: `torchvision` is a
  hard dependency too, via `aliked.py`'s `torchvision.ops.deform_conv2d`, even though this project
  only uses DISK).
- **DISK, not SuperPoint, as the local-feature extractor** — SuperPoint is the more commonly-used
  LightGlue pairing in tutorials/benchmarks, but its inference file and pretrained weights (`lightglue/
  superpoint.py`, adapted from Magic Leap's original release) carry a proprietary-style notice ("The
  receipt or possession of this source code and/or related information does not convey or imply any
  rights to reproduce, disclose or distribute its contents, or to manufacture, use, or sell anything
  that it may describe"), not a standard permissive OSS license — a real constraint for a repo pushed
  publicly. DISK (Apache-2.0) and ALIKED (BSD-3-Clause) are the other two extractors LightGlue ships
  pretrained weights for, comparable match quality to SuperPoint in the paper's own benchmarks;
  DISK was chosen as the direct drop-in with no further licensing question. Explicit user decision,
  not assumed.
- **Pretrained weights** (LightGlue matcher + DISK extractor) are fetched from a `github.com/cvg/
  LightGlue` release and via `kornia.feature.DISK.from_pretrained` respectively, both through
  `torch.hub` — cached under `TORCH_HOME` (`docker/Dockerfile` sets this to `/workspace/cache/torch`,
  see `docs/caching.md`'s own section on this), tens of MB total, fetched once.
- **CPU-only**: confirmed via direct inspection of the pinned commit's source that no file in the
  LightGlue/DISK code path attempts to compile or load a custom CUDA extension at import or runtime
  — `aliked.py`'s `torchvision.ops.deform_conv2d` (the one op that could plausibly need a CUDA
  build) works fine on CPU tensors. Real published CPU benchmark: ~20 FPS at 512 keypoints on an
  Intel i7 10700K — this project isn't latency-sensitive (a one-shot notebook cell, not a real-time
  loop), so CPU-only is a straightforward choice, no GPU passthrough needed in `docker-compose.yml`.

## ISIS PushFrame `campt` ground-to-image: a real, scattered ~38% failure rate, not an edge artifact

`control_network.resolve_control_points` (see its own module docstring) queries `isis_wac.
ground_to_image_pixel` once per matched tie point, against the original (pre-`cam2map`) WAC crop
cube's native PushFrame camera model. On the current default candidate, a real 767-point LightGlue
match set resolved only 477 (290 failures, ~38%) — investigated directly rather than assumed benign,
since this project's own `_CROP_EDGE_MARGIN_PX` precedent already established that `campt` has real
numerical instability near a cropped cube's edge, making that the obvious first suspect.

**Ruled out**: edge proximity. A direct measurement (distance from each matched pixel to the nearest
invalid/padding pixel, via `cv2.distanceTransform`) found resolved and dropped points have nearly
identical edge-distance distributions (median 119px vs. 122px in the downsampled matching grid), and
the drop rate stays ~38-39% even 40+px from the boundary — not concentrated at the crop's own edge at
all, contrary to that earlier pattern.

**Actual cause, confirmed live**: every one of the 290 failures is ISIS's "no surface intersection"
error specifically (zero were "not inside cube" — checked directly via each failing query's real
`campt` stderr, not assumed) — a fundamentally different failure than an index just missing the
cube's own pixel-array bounds. This matches a real, independently-documented upstream ISIS issue for
PushFrame cameras specifically: `GetLocalNormal`'s calculation can land outside the correct framelet
during the ground-to-image solve, producing an erroneous local normal and a failed/non-convergent
intersection search (DOI-USGS/ISIS3 GitHub issue #4256) — a known numerical fragility in ISIS's own
PushFrame geometry, not a bug introduced by this project's code, and consistent with failures
scattered roughly uniformly through the image rather than concentrated anywhere in particular.

**Not currently a concern for terrain-relief bias, but worth re-checking once it would be**: this
project's control points are still ellipsoid-only (`isis_wac.run_spiceinit`'s `shape=ellipsoid`, see
`control_network.py`'s own docstring for why) — a smooth ellipsoid's local surface normal varies only
slowly with position, so there's no real steep-terrain trigger for the local-normal bug to hit yet.
If/when a real DEM-aware shape model is added (a planned follow-up, see `docs/plan.md`'s open items),
this failure mode could plausibly get *worse* specifically at high-relief features like crater rims —
exactly the terrain the user's own visual parallax observation (motivating the whole 3D-alignment
investigation) depends on. Re-run this same edge-distance/failure-kind check against real terrain
once that lands, rather than assuming the ellipsoid-only finding still holds.

## `campt`'s `USECOORDLIST` batch mode: real gotchas, and why it matters here

`isis_wac.ground_to_image_pixels_batch` (used by `control_network.resolve_control_points`, see
above) runs one real `campt usecoordlist=true` call for many points at once instead of one
`ground_to_image_pixel` subprocess per point — confirmed live to matter a great deal: each
individual `campt` call pays real process-spawn/SPICE-load overhead (~300ms observed), which
dominated `resolve_control_points`' wall-clock for a multi-hundred-point control network (767 points
→ ~230s of pure subprocess overhead, collapsed to ~3s). Two real gotchas found live, not documented
anywhere obvious in `campt -help`'s short parameter list (confirmed via `campt.xml`, ISIS's own
per-app doc source, and direct experimentation):

- **`COORDLIST`'s ground-coordinate column order is `latitude, longitude`**, not `longitude,
  latitude` (`campt.xml`: "Expected order for ground coordinates: latitude, longitude") — the
  opposite of this project's own `(lon_deg, lat_deg)` convention everywhere else
  (`ground_to_image_pixel`'s own argument order, `GroundPoint`'s `PositiveEast360Longitude` field,
  etc.). Getting this backwards doesn't error — it silently returns a wrong-but-plausible-looking
  result (every row projecting to the same stale sample/line, no `Error`), which would be very easy
  to miss without an independent per-point cross-check.
- **A failed row's `Sample`/`Line` fields come back as a stale carryover from the last *successful*
  row in the batch**, not `NULL`/absent — confirmed live (`allowerror=true`, needed so one bad point
  doesn't abort the whole batch). The only reliable success signal is the row's own `Error` field:
  the literal string `"NULL"` on success, a real message (e.g. "Requested position does not project
  in camera model; no surface intersection") otherwise.
- `APPEND` defaults to `TRUE` (silently prepends onto whatever's already at the `TO=` path) —
  `append=false` is required for a fresh, correct result on a reused/shared output path.

Live-validated for correctness, not just speed: 100 real crop pixels' own real ground points
(round-tripped through `campt`), batched vs. the old per-point loop — 0 mismatches, 46x faster on
that sample size.
