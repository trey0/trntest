# Data sources reference

Current, stable facts about the external data/formats this project depends on — endpoints, formats,
kernel layout, byte layouts, and known gotchas. Consult before writing new code against any of these
external systems; update this file (not just code comments) when a concrete choice changes. For *how*
these choices were reached (including wrong turns), see `docs/history.md`.

## Lunaserv WMS (DEM + visible imagery)

- Endpoint: `https://wms.im-ldi.com/lunaserv/lunaserv_stage?` (WMS 1.1.1). Run by ASU/LROC.
- `GetCapabilities`: `?request=GetCapabilities&service=WMS&version=1.1.1`
- Formats supported by GetMap include `image/tiff` and `image/tiff; mode=32bit` (float32 —
  needed for real elevation values, not a colorized/stretched render).
- SRS: `IAU2000:30100` works for GetMap on both layers below and returns a plain geographic
  (lon/lat, degrees) raster on a **sphere** of radius 1737400 m (GDAL reports it as an unprojected
  `GEOGCRS`) — i.e. this is the layers' native, unprojected grid; no reprojection needed before
  feeding `sat_sim`.
- **Gotcha:** `luna_wac_dtm_numeric_meters_absolute`'s pixel values are **planetocentric radius in
  meters** (~1.73-1.74 million), not height-above-datum. `src/trntest/lunaserv.py` subtracts the
  reference radius (`MOON_RADIUS_M = 1737400.0`) before handing the DEM to ASP. Feeding the raw
  radius values straight to ASP would silently double-count the planet's radius.
- Near the poles, a lon/lat bbox is very non-square in physical km (1° longitude shrinks by
  `cos(lat)`); `pixel_dims_for_gsd()` in `src/trntest/lunaserv.py` computes width/height from the
  actual physical footprint size so both axes sample at the same ground resolution.
- **Antimeridian:** LRO's near-polar orbit means a camera footprint can straddle ±180° longitude.
  GetMap handles an out-of-range bbox (e.g. `170,40,190,45`) correctly — same real pixel data as the
  in-range equivalent (`-190,40,-170,45`); longitude is treated cyclically, not clipped to
  `[-180,180]`. `footprint_bbox_deg()` relies on this: it unwraps footprint corner longitudes onto a
  common branch (relative to the first corner) before taking min/max, which can produce a bbox that
  extends slightly outside `[-180, 180]` — intentional, not a bug to "fix" by clamping.
- Layers of interest:
  - `luna_wac_normalized_reflectance` — "LROC WAC 643 nm Normalized Reflectance," a >100,000-image
    photometric composite. **The ortho layer this project actually fetches**
    (`config.lunaserv_ortho_layer`), chosen over `luna_wac_global` on image-quality grounds (see
    "Ortho layer noise" below). Global bbox `-180/-90/180/90`.
  - `luna_wac_global` — "LROC WAC Global 100m/px" visible mosaic, composited from ~15,000 raw WAC
    images (no evident per-pixel outlier rejection). No longer the default ortho source (see below)
    but still a reasonable fallback/reference. Global bbox `-180/-90/180/90`.
  - `luna_wac_hapke_321nm`/`_360nm`/`_415nm`/`_566nm`/`_604nm`/`_643nm`/`_689nm` — single-band,
    Hapke-photometrically-normalized (fixed phase=incidence=60°, emission=0° geometry) median
    composites over ~40 months of repeat observations (visible: ~136-140 observations/pixel).
    `luna_wac_hapke_normalized` is just these three (321/415/689nm) stacked as an RGB composite, not
    independently-processed data. Tested and **rejected** as the ortho source: visibly blurrier
    (lower effective resolution) than `luna_wac_global`/`luna_wac_normalized_reflectance`, and
    introduces its own large saturation-blowout artifact on at least one bright crater.
  - `luna_wac_dtm_hillshade` — standalone grayscale GLD100 hillshade (no imagery). `luna_wac_dtm`/
    `luna_exp_colorshade_gld100`/`luna_wac_alternate_color_flat` — "color shaded relief": hillshade
    blended with an *elevation* color ramp (hypsometric tint), not real albedo/reflectance — a
    topographic-map style product, not a photoreal one; not usable as a `sat_sim` ortho texture.
  - `luna_wac_dtm_numeric_meters_absolute` — GLD100 elevation, actual meters. This is the DEM fed
    to `sat_sim`.
  - Other candidates seen in capabilities if higher resolution is ever needed: `luna_nac_dtms`,
    `luna_pds_rdr_dtm`, per-Apollo-site DTMs/NAC mosaics (much higher res, smaller coverage).
- Usage policy: free/open, but credit "NASA/GSFC/Arizona State University" per their FAQ.
- **NoData convention**: this server documents `0 = NoData` for related layers (Clementine basemap:
  "leaving 0 for NODATA"; GREDR: "set to NoData (0)") — not white, despite white being a common WMS
  background-fill convention elsewhere. **Empirically confirmed reusable diagnostic**: requesting
  `transparent=true` (undocumented in the layer capabilities entries themselves, but works) with
  either `format=image/png` (returns 4-band RGBA) or `format=image/tiff` (returns 2-band gray+alpha)
  yields a real alpha/mask band — `alpha=0` marks genuine NoData, distinct from real signal that
  happens to be dark. Confirmed for a real fetched AOI: **zero actual NoData pixels** (alpha=255
  everywhere), including at pixels with DN=0 — those are real dark/shadow signal, not missing data.
- **Ortho layer noise ("hot pixels")**: `luna_wac_global` has ~16,000 isolated single-pixel outliers
  per ~2600x2600px tile (0.235% of pixels; ~91% are genuinely isolated — no adjacent outlier pixel —
  rather than part of a larger feature; typical deviation from the local neighborhood ±15-20 DN, not
  saturated to 0/255). Confirmed via the NoData test above that these are **real signal, not
  nodata** — most likely uncaught single-frame sensor/cosmic-ray noise from the ~15,000 source
  images, since this mosaic (unlike the Hapke/normalized-reflectance composites) doesn't appear to
  be a multi-observation composite. `luna_wac_normalized_reflectance` has the same *character* of
  noise (~91.6% isolated) but ~4x fewer outliers (0.059%) — consistent with its much larger
  (>100,000-image) source count suppressing, but not eliminating, single-frame noise.
  `src/trntest/lunaserv.py`'s `despeckle()` (a MAD-based local-outlier filter, applied to whichever
  layer is fetched) cleans the residual before the ortho is used for anything. A large real
  saturated-crater feature seen in both `luna_wac_hapke_643nm` and `luna_wac_normalized_reflectance`
  (a genuine high-albedo feature blown out under fixed-geometry photometric normalization, not
  noise) is *not* touched by this filter by design — it fails the filter's "locally smooth
  neighborhood" precondition, the same way a real crater rim/edge does.
- **`lrowaccal`/ISIS `noisefilter` do not apply here.** ISIS's `lrowaccal` (used in
  `src/trntest/isis_wac.py`) has a real, default-on `SpecialPixels` correction — a
  temperature/mode-matched known-bad-detector-pixel mask — but it's keyed to raw EDR framelet
  geometry, which no longer exists once ASU composites/reprojects thousands of frames into a global
  mosaic; it cannot be applied post-hoc to `luna_wac_global`/`luna_wac_normalized_reflectance`. ISIS
  `noisefilter` (a generic boxcar-tolerance outlier filter) *could* in principle be run on any raster
  via `std2isis`/`isis2std` (both present in this project's Docker image), but was not used —
  `lunaserv.despeckle()`'s in-process numpy filter was already validated against this exact data and
  avoids the ISIS round-trip subprocess/environment overhead for what's a display/render-texture
  concern, not primary scientific analysis. See `docs/history.md` Phase 15 for the full
  investigation.

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
  Fed in Lunaserv's **native** projection (avoids an extra `gdalwarp` resampling step); no evidence
  yet that `sat_sim` demands a local stereographic projection instead.
- **`sat_sim` applies no illumination/shading model of its own.** Per its own docs, it
  "unproject[s] an ortho image into a given camera... in the spirit of ISIS `map2cam`," generating
  output pixels via bicubic interpolation of the `--ortho` input. The DEM is used purely for
  ray/terrain-intersection *geometry* (which ground point a given camera ray hits) — the output
  pixel value is a direct geometric resample of whatever's already in the ortho, with no per-ray
  reflectance/sun-angle computation applied. Any relief/shading visible in a render is therefore
  whatever was already baked into the ortho texture, not something `sat_sim` computes — see
  "Lunaserv WMS" below for how this project supplies that shading (`lunaserv.shade_ortho`, lit with
  real SPICE sun geometry, not relying on any shading baked into the source imagery, which was never
  guaranteed to match the simulated frame's real sun angle in the first place).
- **`--dem-height-error-tol`'s default (0.001m) is too tight for this project's DEM and causes
  visible salt-and-pepper speckle** in the render (`sat_sim`'s ray/DEM-intersection root-finder
  misbehaves at scattered pixels). Root cause: Lunaserv's DTM layer serves planetocentric radius
  (~1.7e6 m) as float32, whose ULP (smallest representable step) at that magnitude is already
  ~0.125m — baked into the source data itself, not something fixable in
  `lunaserv.radius_to_elevation`'s own subtraction. **Confirmed empirically** (see `docs/history.md`
  Phase 15): tightening the tolerance further makes the speckle dramatically worse (more, denser
  artifacts), loosening it to comfortably clear that ~0.125m floor eliminates it cleanly — neither
  outcome is subtle. `src/trntest/render.py`'s `DEM_HEIGHT_ERROR_TOL_M = 0.5` (a 4x margin above the
  float32 floor) is what `run_sat_sim` actually passes. Two other theories were tested and ruled
  out first: ortho-side noise/aliasing (despeckling the ortho, and even a large `--blur-sigma`, left
  the speckle pattern essentially unchanged) and the ortho source layer's own quality (switching
  layers changed the *baseline* noise level but not this specific artifact).
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

## LRO SPICE kernels (NAIF)

- Archive root: `https://naif.jpl.nasa.gov/pub/naif/pds/data/lro-l-spice-6-v1.0/lrosp_1000/`
  - Subdirs by kernel type: `data/ck`, `data/spk`, `data/ik`, `data/fk`, `data/sclk`, `data/lsk`,
    `data/pck`, plus `extras/mk` (one metakernel per time range/year).
- The yearly metakernel lists every kernel needed to cover that year — treat it as a **manifest to
  parse**, not something to furnish wholesale. CK (pointing) kernels dominate the data volume; only
  download the specific CK/SPK file(s) whose filename-encoded date range covers the timestamp
  needed, plus the small LSK/SCLK/PCK/FK/IK files (needed regardless, cheap).
- Request `MOON_ME` directly from spiceypy calls (position + orientation) rather than getting
  MOON_PA/J2000 and rotating manually — the standard lunar frame kernel defines `MOON_ME` for
  direct use (`fk/moon_assoc_me.tf` + `fk/moon_080317.tf` + the PA orientation kernel).
- Yearly metakernels (`extras/mk/lro_YYYY_vNN.tm`) list, for CK, **five separate kernel "flavors"**
  covering the same date ranges: `lrosc` (spacecraft bus attitude — the main reconstructed
  pointing), `lrolc` (LROC-specific: small thermally-dependent offset of frame -85620 relative to
  the bus), `lrodv` (delta-V/maneuver attitude), `lrohg` (high-gain antenna gimbal), `lrosa` (solar
  array gimbal). **Only `lrosc` + `lrolc` are relevant to the WAC boresight** — skip the other three
  entirely, which cuts CK downloads roughly 5x for a given day.
- CK/SPK filenames encode a `YYYYDDD_YYYYDDD` date range but adjacent files can overlap by a day —
  don't just pick the filename whose range contains the target date; after furnishing a candidate,
  verify actual coverage with `spiceypy.ckcov`/`spkcov` and fall back to the neighboring file if the
  exact timestamp isn't covered.
- IK files are per-instrument (`lro_crater_v03.ti`, `lro_dlre_v05.ti`, `lro_lamp_v03.ti`,
  `lro_lend_v00.ti`, `lro_lola_v00.ti`, `lro_lroc_v20.ti`) — only `lro_lroc_v20.ti` is needed here.
- Always-needed small/generic kernels regardless of date: `lsk/naif0012.tls`,
  `sclk/lro_clkcor_2025351_v00.tsc` (~2.3 MB, single mission-long file, not date-ranged),
  `pck/pck00010.tpc`, `pck/moon_pa_de421_1900_2050.bpc`, `fk/lro_frames_2014049_v01.tf`,
  `fk/moon_assoc_me.tf`, `fk/moon_080317.tf`, `ik/lro_lroc_v20.ti`, `spk/de421.bsp` (planetary
  ephemeris; the Moon PA/ME frame chain needs it).
- WAC frame chain (from `lro_frames_2014049_v01.tf`): `LRO_LROCWAC` (NAIF ID **-85620**) is
  CK-dependent (small thermally-varying offset from `LRO_SC_BUS`, +Z boresight) — this is exactly
  what the `lrolc` CK provides. `LRO_LROCWAC_VIS` (-85621) and the 5 VIS filter frames
  (-85631..-85635) are then *fixed* (TKFRAME) offsets from -85620, defined right in the FK — no CK
  needed for those.
- `spice.furnsh()` does **not** dedupe repeat loads of the same kernel file across separate calls —
  each call consumes a fresh, limited KEEPER slot (~5300 max). `spice_kernels.py` tracks every
  currently-furnished local path (`_loaded_kernels`) and skips `furnsh()` for paths already loaded,
  unloading superseded date-ranged (CK/SPK) kernels when the target date moves to a different chunk
  (`fetch_and_furnish`). Any code that furnishes kernels across many distinct dates in one process
  must go through this tracking, not call `spice.furnsh()` directly.
- **Ascending-node search**: `illumination.find_ascending_node_crossings` finds LRO's `MOON_ME`-frame
  latitude=0 crossings via SPICE's `gfposc` (geometry finder over position coordinates: `targ="LRO"`,
  `frame="MOON_ME"`, `obsrvr="MOON"`, `crdsys="LATITUDINAL"`, `coord="LATITUDE"`, `relate="="`,
  `refval=0.0`) — one call over the whole search window, SPICE's own compiled adaptive root-finder.
  Still returns both ascending and descending crossings; filtered to ascending via a ±5s latitude
  sign check. Needs SPK coverage for the *whole* confinement window furnished at once —
  `spice_kernels.furnish_spk_range` does this (SPK/`lrorg` only, not CK — safe to pre-furnish a
  whole search window's worth since SPK volume is small relative to CK), unlike
  `fetch_and_furnish`'s per-epoch just-in-time pattern used everywhere else for full camera-pose
  work (which does need CK).
- **`functools.cache`-on-`TrntestConfig` gotcha**: `spice_kernels.latest_metakernel_url` is
  `@functools.cache`d on `(year, naif_base_url: str)` — keyed on just that one field, not a whole
  `TrntestConfig`, deliberately. A whole-config cache key silently breaks memoization whenever a
  caller varies the config per-item (e.g. `dataset.py`'s per-candidate `dataclasses.replace`) but
  the cached function only actually reads one field of it — every distinct config value becomes a
  cache miss even though the field that matters never changed. Prefer keying `functools.cache` on
  the specific field(s) a function actually uses, not a whole config object, whenever callers might
  vary other fields per call.

## LROC WAC EDR/CDR products

- Browsable archive: `https://pds.lroc.asu.edu/data/LRO-L-LROC-2-EDR-V1.0/<volume>/DATA/<subdir>/<doy>/WAC/<product>.xml`
- PDS Geosciences Node **Orbital Data Explorer (ODE) REST API**: `https://oderest.rsl.wustl.edu/`
  (`catalog.py`'s client) — search by instrument/time/lat-lon instead of browsing directories by
  hand. Confirmed live: `EDR_PRODUCT_TYPE = "EDRWAC4"`, `CDR_PRODUCT_TYPE = "CDRWAC4"`.
- WAC is a 7-color **push-frame** camera (100 m/px visible, 400 m/px UV) — framelets captured
  periodically as the spacecraft moves, not a continuous line-scan. The EDR label carries
  `START_TIME`/SCLK and framelet timing needed to map "which part of the swath" to a timestamp
  (`camera.fetch_frame_timing`, `camera.FrameTiming`) — EDR is used **only** for this timing
  metadata, never pixel data.
- Raw EDR/CDR byte layout: EDR has a 7040-byte PDS3 attached header, CDR 10560 bytes (extra
  calibration metadata prepended); both then hold the same 704-samples-wide, row-major
  ("Last Index Fastest") grid. EDR is `UnsignedByte` DN; CDR is `IEEE754LSBSingle` (float32) I/F
  (calibrated reflectance factor) — same raw multiplexed geometry in both; CDR calibration does
  **not** band-separate or geometrically reproject anything. The actual image pixel data used for
  visual comparison comes entirely from the CDR product (`wac.fetch_vis_mosaic`).
- 78 raw lines per framelet cycle = 2 UV filters x 4 TDI lines + 5 VIS filters x 14 TDI lines. Per
  the official LROC EDR/CDR SIS (`LROCSIS.PDF`): "WAC band passes are arranged first UV then VIS
  (320, 360, 415, 565, 605, 645, 690), but the order is reversed after LRO performs a 180° yaw
  maneuver to align the solar panels with the sun" — and "the WAC CDR file will require further
  processing to separate framelets into their respective bands and to align the bands, in order to
  be viewed as a standard multi-band image." A raw multiplexed strip is never going to look like a
  picture; that's expected, not a bug. `wac.fetch_vis_mosaic` extracts one VIS filter's 14-line
  block (lines `[22:36)` — guaranteed pure-VIS regardless of yaw-dependent order) from many
  consecutive frames and stacks them vertically, matching how WAC's push-frame design is meant to
  build continuous coverage.
- CDR `Special_Constants`: `missing_constant = 0xFF7FFFFB` (as float32, ≈ -3.4028e+38). A UV
  framelet line is 4 TDI lines but the UV detector is only 512 px binned to 128 px — the other
  ~576 (of 704) samples in a UV line are padding, hence a big chunk of `missing_constant` values
  concentrated in the 8 UV lines of each 78-line frame; a pure-VIS 14-line block has only ~0.4%
  missing (a handful of bad/edge columns).
- **Pass-dependent sensor axis convention** (formerly believed to be a fixed hardware property —
  see `docs/history.md`, Phase 9, for how this was found to be wrong): WAC's raw camera frame
  (`LRO_LROCWAC_VIS`) is body-fixed (no gimbal), and LRO performs periodic 180°-yaw-flip maneuvers
  (roughly every ~4 weeks) that rotate the *entire* raw camera frame together — this changes both
  the raw band ordering (documented in the SIS, above) and, less obviously, the along-track
  **chirality** of a stacked mosaic relative to the always-proper synthetic image (a mirror, not a
  rotation — rotations are always determinant +1 and can never produce or fix a mirror).
  `camera.boresight_rotation_k(r_cam_to_me_raw, forward_step_me_km)` measures, per-pose, which raw
  axis "forward in time" actually projects onto via real SPICE trajectory data, instead of assuming
  a constant; `camera.Camera.reverse_crop_along_track` (derived from it) tells
  `wac.fetch_vis_mosaic` to reverse along-track frame-stacking order (`vis[::-1]`) when this pass's
  real ground-track direction doesn't match the original reference convention. `tie_points.py`
  (`reverse` parameter) and `orientation.py` (crop `up_orig`) both stay consistent with whichever
  stacking order `wac.py` used for a given pose. `crop_footprint_corners` needs no such handling —
  it's pure ground geometry (lon/lat), independent of pixel row/reversal.

### Reference/regression-test EDR products

The live default image-selection path is `dataset.select_dataset()` (catalog-driven — see
`docs/plan.md`), not any single hardcoded product. Two specific products remain useful as known
test fixtures (one per yaw state, used to validate the axis-convention/chirality fix above still
holds for both):

- `M1329714703CE` — `LRO-L-LROC-2-EDR-V1.0/LROLRC_0041C/DATA/ESM4/2019334/WAC/M1329714703CE.{IMG,xml}`,
  orbit 46980, `nframes` 538, `interframe_delay` 718.75 ms. This repo's original single-demo
  product; non-mirrored (`k=1` convention).
- `M1327210646CE` — orbit 46625, ~26 days earlier than the above (opposite yaw state); mirrored
  under the old fixed-`k` assumption, correctly un-mirrored by `boresight_rotation_k`.
- Relevant kernel files for `M1329714703CE`'s date (day 334, 2019), for reference:
  `ck/lrosc_2019325_2019335_v01.bc`, `ck/lrolc_2019304_2019335_v01.bc` (or its neighbor
  `ck/lrolc_2019334_2020001_v01.bc` — the two overlap on day 334), `spk/lrorg_2019258_2019349_v01.bsp`.

## Current image-pipeline algorithm (square crop, pose epoch, comparison figure)

- **Crop sizing**: the synthetic camera's `fu=fv` is derived directly from WAC's real color-mode
  cross-track FOV — **61.4°**, from the SIS (`spice.getfov` on the WAC-VIS IK returns the wrong,
  monochrome-mode ~91.7° FOV, since color mode only reads the center 704 of the ~1024-wide
  detector). `camera.compute_n_frames_for_square_crop()` then ray-traces the real cross-track
  ground width (chord distance between the ±30.7°-ray ground intersections) and the real per-frame
  ground advance (chord distance between consecutive-frame boresight ground points), and picks
  `n_frames = round(cross_track_width_km / km_per_frame)` so the real CDR crop and the synthetic
  image cover the same real ground area — square in real km, not necessarily square in pixels
  (cross-track and along-track have different native GSD).
- **Pose epoch**: the synthetic camera is posed at the crop's own **temporal midpoint**
  (`center_frame_index = start_frame + n_frames/2`), not its start — so both images are centered on
  the same ground point. `camera.build_camera()` derives `n_frames`/`center_frame_index` together.
- **Comparison-figure aspect ratio**: both panels are plotted with `extent=` in real km (not raw
  pixel index), since the CDR crop's pixel array isn't square even though the ground area it covers
  is.
- **SPICE-derived tie points** (`tie_points.py`): 5 points (a die's "5"/X pattern: 4 corners +
  center) placed in the ground area both images share, projected into each image's real pixel
  coordinates — closed-form pinhole inverse for the synthetic image (exact, single fixed pose);
  frame-index bisection for the real crop (which mixes many real poses, one per frame). Verified via
  a self-consistency check: the crop's own 4 defining corners project back to exactly
  `(0,0)`/`(704,0)`/`(0,994)`/`(704,994)`.
- **North-up display rotation** (`orientation.py`, notebook-display-only — never touches the
  sensor model, `.tsai`, or CSM/ISD JSON): picks, per image, the multiple of 90° (no mirroring)
  whose on-screen "up" is closest to true north, via `best_k_for_north_up()` (verified numerically
  against `np.rot90` rather than trusted from hand-derived algebra alone). The synthetic image
  allows all 4 `k∈{0,1,2,3}`; the real crop only `k∈{0,2}` (its row axis is real along-track data —
  a 90°/270° rotation would put cross-track on the vertical axis). The crop's `up_orig` depends on
  `camera.reverse_crop_along_track`, since which end of the mosaic is "forward in time" is
  pass-dependent (see above).

## ISIS3/CSM spike: real-WAC DEM reprojection

Hands-on spike (see `docs/history.md` for the motivating discussion) validating whether a real WAC
CDR swath can be reprojected onto the DEM via a genuine CSM camera model (`mapproject`) and
re-rendered from a synthetic pose (`sat_sim --ortho`), as an alternative/complement to `wac.py`'s
manual framelet-stacking approach. The `mapproject`/`sat_sim --ortho` half of this was run entirely
in a throwaway container and never adopted into `trntest` source; the earlier part of the chain
(EDR fetch through `framestitch`) *has* since been implemented for real as `src/trntest/isis_wac.py`
(see `docs/history.md` Phases 13–14) and merged to `main` — recorded here so none of this needs to
be re-derived from scratch if picked up again.

- **Install**: `mamba create -n isis --override-channels -c usgs-astrogeology -c conda-forge
  --channel-priority flexible isis ale` — the plain `-c usgs-astro` channel name from older docs
  is wrong/404; the current channel is **`usgs-astrogeology`**. This single command pulled ISIS
  10.0.0 and ALE 1.2.0 (`isd_generate`) together with no dependency conflicts once the channel name
  was fixed and priority set to `flexible` (`strict` fails to solve — ISIS's own build pulls
  `embree`/`qt`/`bullet` pins that strict priority can't reconcile against conda-forge). Needs
  `ISISROOT=<conda env prefix>` set explicitly (e.g. `/opt/conda/envs/isis`) — without it,
  `IsisPreferences` isn't found and every app aborts immediately.
- **`$ISISDATA` size — bulk download is avoidable, confirming the on-demand approach works**:
  `spiceinit web=yes` (USGS's SPICE Web Service) **works for WAC**, not just NAC as the docs imply
  — confirmed directly: after running it, `$ISISDATA/lro/kernels/` doesn't exist locally at all
  (zero files), yet the resulting label correctly lists the real per-date CK/SPK files
  (`lrolc_2019334_2020001_v01.bc`, `fdf29r_2019305_2019335_v01.bsp`, etc.) that were used remotely.
  ~14s/call. This is the key finding for avoiding a bulk kernel download. What's still needed
  locally: the mission-independent `base` area (real measured size **26 GB**, not the ~10 GB
  estimated from secondhand docs — it includes generic multi-mission kernels like Neptune's SPK)
  and `lro`'s non-kernel calibration files that `lrowaccal` needs (dark/flat cubes, measured
  **~5 GB** via `downloadIsisData lro $ISISDATA --no-kernels`, includes NAC+WAC together — no
  narrower filter found). **Gotcha**: `downloadIsisData`'s `--dry-run` flag does not actually skip
  the transfer in this version (10.0.0) — real files were written to disk despite `--dry-run` being
  passed; don't rely on it to preview size before committing to a real download.
  **Correction (from actually running this, not just estimating it — see `isis_wac.py`'s
  `ensure_isisdata()`)**: the "`--no-kernels` shrinks `base` to near-zero" claim above was wrong.
  `--no-kernels` only excludes the `ck/ek/fk/ik/iak/lsk/mk/pck/sclk/spk/tspk/dsk` kernel subdirs —
  `base`'s ~26 GB (well, really ~20GB of it) is actually dominated by `base/dems/` (global shape
  models), which isn't a "kernel" and isn't touched by the flag at all; a real `downloadIsisData
  base $ISISDATA --no-kernels` run pulled the full 20 GB of `dems/`. None of that DEM data is
  needed until `mapproject` (out of scope for the framestitch-only spike this module currently
  covers). Worse, `spiceinit web=yes` genuinely does still need a handful of tiny,
  generic/mission-independent kernels locally even with `--no-kernels` — confirmed by a real
  failure ("Unable to load leadsecond file... No existing files found with a numerical version
  matching [naif????.tls] in [.../base/kernels/lsk]") when `base/kernels/lsk` was empty. The
  actually-minimal, correct fetch: `downloadIsisData base $ISISDATA --include
  "{kernels/lsk/**,kernels/pck/**,kernels/sclk/**,kernels/fk/**,kernels/ik/**,kernels/iak/**}"` —
  measured **~5 MB**, not 26 GB or even "near-zero" — skips `dems/`/`examples/`/`kernelTesting/`
  entirely. Combined with the `lro` ~5 GB above, the real one-time cost for this notebook's scope
  is **~5 GB total**, but via a completely different mechanism (a narrow `--include`, not
  `--no-kernels`) than originally claimed.
- **`lrowac2isis`** (EDR `.IMG` only, confirmed CDR is not accepted) splits into 4 cubes
  (`*.uv.even.cub`, `*.vis.even.cub`, `*.uv.odd.cub`, `*.vis.odd.cub`). Confirmed via `catlab`:
  `vis.even.cub` is **704 samples × 7532 lines × 5 bands** — the 5 VIS filters come out as 5
  distinct ISIS cube bands already correctly separated, no manual byte-offset extraction needed
  (unlike `wac.py`'s current hand-picked `VIS_BLOCK_OFFSET`).
  **Line count, confirmed empirically (`isis_wac.crop_window_for_camera`)**: the cube preserves
  **14 lines per original EDR frame** — exactly `wac.VIS_BLOCK_HEIGHT`, *not* 1 line/frame (an
  earlier, wrong assumption briefly shipped in `crop_window_for_camera` before this was checked
  against real data). Confirmed on two real products: `M1327210646CE` measures exactly `258 frames
  × 14 = 3612` lines (its EDR label's own `nframes`, cross-checked directly), and this product's
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
  "non-mirrored" characterization from `camera.boresight_rotation_k`'s convention (see above) — a
  useful cross-check, but ISIS did not derive this automatically; it had to be determined the same
  way `boresight_rotation_k` already does (real-SPICE-geometry-informed, here just visual A/B).
- **`isd_generate`** (ALE) run against the *unstitched*, calibrated even/odd cubes produces a CSM
  `USGS_ASTRO_PUSHFRAME_SENSOR_MODEL` state JSON per parity. Unlike ASP's own `cam_gen` output
  (bare model-name string on line 1, then JSON — see the `sat_sim`/ISD note above), ALE's
  `isd_generate` output is **plain, direct JSON** with `name_model` as a top-level key — no line to
  skip.
- **`mapproject -t csm <dem> <cub> <json> <out>`** works directly against the unstitched per-parity
  cube + its own ISD (no separate stitched-cube pairing step needed for this) — confirmed on real
  data: ASP's bundled GDAL reads `.cub` natively (no `isis2gdal`/conversion needed for the DEM
  input either — `isis2gdal` doesn't exist in ISIS 10; use GDAL's native ISIS3 driver directly). A
  4337×5367 output at 100 m/px took **~23s**. Confirms ASP's own caveat concretely: the resulting
  orthophoto has **real, significant periodic striping at framelet boundaries** (visually severe in
  a 1:1 crop, not a display artifact) — matches ASP's documented "not fully mature... artifacts at
  framelet borders" warning exactly, for this project's actual reference product.
- **One parity alone leaves large coverage gaps** — mapprojecting only `vis.even` and feeding it to
  `sat_sim --ortho` with this repo's existing `camera_frame440.tsai` pose left most of the 256×256
  render as nodata; mosaicking `vis.even` + `vis.odd` orthophotos (`dem_mosaic`) filled in more but
  the render was still mostly nodata — the DEM/ortho AOI used for this spike (a generous south-polar
  cap, lat -75 to -90) evidently didn't fully contain frame 440's real footprint. Not investigated
  further (spike scope) — a real integration would need a properly sized/centered AOI, not a
  generic polar cap.
- **End-to-end wall time** for one product, from a cold `.IMG` fetch through a rendered `sat_sim`
  frame: a few minutes total (dominated by the ~1min `mamba create`, `EDR` fetch, and two ~23s
  `mapproject` calls) — fast enough that per-product cost isn't a practical concern.
- **Net verdict**: the chain is technically real and works end-to-end on this project's actual
  reference product, but isn't yet a clean drop-in replacement for `wac.py` — the framelet-boundary
  striping is a genuine, visible quality problem (not just ASP being cautious in its docs), and a
  usable comparison needs both parities mosaicked plus a correctly-sized AOI, neither of which is
  spelled out in ASP's own WAC example. See the plan file this spike came from for the decision
  point on whether/how to integrate this into `trntest` itself.

**Second run, `M1327210646CE`/frame 94 (the product the live demo notebook's `select_dataset()`
path actually chose, not the old hand-picked `M1329714703CE`/440 — see `docs/history.md` Phase 2/5
and Phase 8 for why those are different selection strategies)** — reused this product's own
already-cached `dem_filled-tile-0.tif` and `camera_frame94.tsai` from the notebook's last real run
instead of a hand-built polar-cap DEM, to rule out AOI-sizing as a confound:

- Confirms illumination was never the striping's cause: this product's I/F range (0.02–0.17, per
  `lrowaccal`/`mapproject` stats) is far brighter than `M1329714703CE`'s near-terminator polar crop
  (max ~0.058, much of it noise-floor) — **and the striping is just as severe**, dominating roughly
  80% of the mapprojected frame (a clean, sharp, unstriped strip only survives at the image's left
  edge). This is a structural/geometric artifact in the current CSM Pushframe stitching itself, not
  something better lighting fixes.
- **`FLIP` cross-check confirmed exactly as predicted**: this product's `boresight_rotation_k=3`
  (a mirrored pass, opposite yaw state from `M1329714703CE`'s `k=1`) — and indeed `flip=true` (not
  `false`) was the coherent choice here, the reverse of the first product. Directly validates that
  ISIS's manual `FLIP` tracks the exact same real, per-pass yaw-flip geometry
  `camera.boresight_rotation_k` already computes from SPICE, just as a hand-set flag instead of a
  derived one.
- **Reusing the pipeline's own DEM fixed the coverage-gap problem**: mosaicking both parities
  against this product's real, correctly-sized/centered `dem_filled-tile-0.tif` gave ~63% valid
  coverage over the full mapprojected extent, and the final `sat_sim`-rendered 256×256 crop (same
  pose as the notebook's own `camera_frame94.tsai`) came out with real signal in every pixel except
  a narrow nodata sliver at one edge — unlike the first run's mostly-nodata result, confirming that
  problem was AOI sizing, not a fundamental gap in the approach.
