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
- SRS: `IAU2000:30100` returns a plain geographic (lon/lat, degrees) raster on a **sphere** of
  radius 1737400 m (GDAL reports it as an unprojected `GEOGCRS`) — the layers' native, unprojected
  grid. **No longer what `src/trntest/lunaserv.py` actually requests** (see the local-CRS entry
  below) but still useful as a plain lookup/degrees SRS if needed ad hoc.
- **`fetch_dem_and_ortho` requests a per-camera local Orthographic CRS, not the native geographic
  grid**: `IAU2000:30166,9001,{c_lon},{c_lat}` (`c_lon`/`c_lat` = that camera footprint's own
  center, filled in via `config.lunaserv_srs_template`). Confirmed via a live GetMap + `gdalinfo`
  check that this reports the Moon's real radius (`ELLIPSOID["unknown",1737400,0,...]`) and genuinely
  isotropic meter pixels (`Pixel Size = (500.0, -500.0)` for a 500 m/px test request) — unlike the
  generic OGC `AUTO:42003` Orthographic code, which is hardcoded to **Earth's** WGS84 ellipsoid
  (`ELLIPSOID["WGS 84",6378137,...]`) and would silently misplace every ground point by the
  Earth/Moon radius ratio (~3.67x) if used directly against lunar lon/lat. `IAU2000:30166` is one of
  a parametrized family Lunaserv exposes per body/projection — discovered by diffing
  `GetCapabilities`' `<SRS>` list around the known-working `IAU2000:30100`/`30101` entries (a
  parallel `301xx` block mirrors a `199xx` Mercury block one digit over, with placeholder
  `c_lon`/`c_lat`/`scale` tokens for the parametrized ones); `30162`/`30163` (`+scale`) are the
  matching lunar Stereographic variants, untried here.
  **Why the switch**: the native geographic grid's degree-pixels are anisotropic away from the
  equator (a degree of longitude covers less ground distance than a degree of latitude); ASP's
  `mapproject --ref-map` (see below) turned out not to preserve that anisotropy when reprojecting
  onto it, silently stretching the output. A local Orthographic CRS has square meter pixels
  everywhere, so that failure mode can't arise. See `docs/history.md`'s dated entry for the full
  investigation.
- **Gotcha:** `luna_wac_dtm_numeric_meters_absolute`'s pixel values are **planetocentric radius in
  meters** (~1.73-1.74 million), not height-above-datum. `src/trntest/lunaserv.py` subtracts the
  reference radius (`MOON_RADIUS_M = 1737400.0`) before handing the DEM to ASP. Feeding the raw
  radius values straight to ASP would silently double-count the planet's radius.
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
  `lunaserv.py` outputs, e.g. `LunaservResult.ortho`), with no separate reprojection/alignment step
  needed to overlay them. This is what `render.run_mapproject` uses.
- Output nodata is real `NaN` (confirmed empirically, `Float32` output by default) — not a
  huge-magnitude sentinel like `wac.MISSING_CONSTANT` elsewhere in this codebase, and not something
  `plotting.valid_pixel_mask`'s threshold check is needed for; ordinary NaN-aware handling
  (`rioxarray`/matplotlib already treat NaN as transparent/masked) is sufficient.
- **Round-trip alignment validated**: mapprojecting `sat_sim`'s own synthetic render back through its
  own CSM sidecar (same DEM, same camera model, forward-then-inverse) overlays real terrain features
  pixel-precisely against the hillshade-based ortho — confirmed visually (individual crater rims line
  up across the full frame), consistent with going forward and back through one self-consistent
  camera model. This is a different, much simpler case than the still-unresolved real-WAC
  `mapproject` striping issue below ("ISIS3/CSM spike") — that pipeline mapprojects an
  ISIS-processed *real* WAC cube (with real sensor/framelet-stacking artifacts feeding in), not a
  clean synthetic render through its own exact camera model.

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
- **`isis_wac.py`'s framestitched VIS cube: the "framelet-boundary striping" visible in
  `plot_isis_comparison` is this same bad/edge-column phenomenon, confirmed empirically — a real,
  deterministic, low-density no-data pattern, not a rendering bug.** Checked a full stitched cube
  (`M1327210646CE`, 3612 lines x 704 samples): overall NULL fraction is only **0.96%**. Two
  components: (1) **columns 0-1 are NULL on every single line** (100%) — a fixed detector-edge dead
  strip; (2) on the **first line of every 14-line VIS framelet cycle**, a fixed set of **56 specific
  columns** go NULL — confirmed identical (same 56 column indices) at 6 widely-separated cycles
  spanning the full cube, i.e. a genuine fixed hardware bad-pixel mask (`lrowaccal`'s
  temperature/mode-matched `SpecialPixels` correction, see the ISIS3/CSM spike section below), not
  noise. Non-boundary lines are ~0.14% invalid (excl. the two edge columns) vs. **7.67%** on
  boundary lines — a >50x contrast concentrated exactly at framelet seams, which is why such a
  low overall density reads as a strong, regular visual "grid"/moiré pattern once
  displayed — confirmed this isn't a downsampling artifact either (same pattern at native
  resolution, `interpolation='none'`). `plotting._fill_dead_columns_for_display` now interpolates
  across these narrow (1-3 column) gaps row-wise for display in `plot_isis_comparison` only — purely
  cosmetic (contrast stretch is still computed from the real, unfilled valid data) — see
  `docs/history.md`'s dated entry.
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
  **Second correction**: "none of that DEM data is needed until `mapproject`" (above) was also
  wrong — `spiceinit`'s default `SHAPE=*SYSTEM` resolves to a real lunar DSK/DEM cube
  (`$base/dems/ldem_128ppd_Mar2011_clon180_radius_pad.cub`) even for plain pointing/calibration
  (`lrowaccal`/`framestitch`), well before any terrain-intersection step. Confirmed by a real
  failure ("USER ERROR NAIF DSK file [...] does not exist") the first time this module's minimal,
  `dems/`-free fetch was actually exercised against a truly empty `$ISISDATA` cache — every earlier
  "confirmed working" run had an already-populated `dems/` left over from an earlier full fetch,
  masking the gap. Fix: `spiceinit ... shape=ellipsoid` (see `spiceinit -h`'s `SHAPE =
  (ELLIPSOID, RINGPLANE, *SYSTEM, USER)`) — a plain reference ellipsoid, no DSK file needed, which
  is sufficient for this module's current scope (still stops at `framestitch`, no real
  terrain-intersection step yet); revisit if/when that changes.
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
- **`isd_generate -i` on an ISIS-`crop`ped Pushframe cube produces a wrong-but-plausible-looking
  ISD** — confirmed empirically (see `docs/history.md`'s dated Phase 24 entry for the full
  investigation): the generated `starting_ephemeris_time`/`ending_ephemeris_time`/
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
  automated checks had missed. Deeper investigation (see the "ISIS `cam2map` real-WAC reprojection"
  section below and `docs/history.md`'s dated entry) traced the true root cause to a bug in
  `usgscsm`'s own `groundToImage` implementation, not fixable by any ISD field patch — this whole
  ISD-authoring approach was abandoned in favor of ISIS's native `cam2map`.
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

**Root cause found and fixed (2026-08-07, see `docs/history.md`'s dated entry)**: the "structural/
geometric artifact in the current CSM Pushframe stitching itself, not something [fixable]" verdict
above was wrong. The real cause: both runs above mapprojected a single **lone even/odd parity cube
in isolation** — and "even"/"odd" turned out not to be a same-frame split (e.g. interlaced TDI rows),
as the name might suggest, but genuine temporal alternation: confirmed empirically that at *every*
single nominal frame slot, exactly one of `vis.even.cal.cub`/`vis.odd.cal.cub` has real (99% valid)
pixel data and the other is 100% NULL. Mapprojecting one parity alone therefore reprojects a
sparse, ~50%-populated sequence, and `mapproject`'s resampling smears real framelet content across
the large real gaps between sparse valid data — producing exactly the severe venetian-blind banding
described above. Mapprojecting the properly **interleaved, stitched** cube instead (same `isd_generate
-i` ISD-generation call — its output geometry/timing parameters, e.g. `interframe_delay`, the
259-sample pointing table, come out byte-for-byte identical regardless of which cube it's run
against, confirming `isd_generate` reads these from label metadata, not from which pixels happen to
be populated) resolves the vast majority of it: on `M1327210646CE`, 31% valid coverage with no
recognizable terrain → **81% valid coverage with real craters visible throughout the frame**, same
product, same DEM. What remains at that point is the small, already-understood, ~1%
framelet-boundary dead-pixel pattern (see the "LROC WAC EDR/CDR products" section above), not this
banding. `isis_wac.py`'s `run_isd_generate`/`run_mapproject` now implement this correctly (against
`stitched`, never a lone parity) — see their docstrings.

**Related gotcha, found via the fix above**: `mapproject`'s output nodata convention depends on its
*input* format. A synthetic render (plain GeoTIFF source) comes out with real IEEE NaN nodata
(as already documented under "ASP `mapproject`" above) — but an ISIS `.cub` source carries ISIS's own
huge-magnitude NULL sentinel (~-3.4e38) straight through into the output, with a GDAL `nodata` tag
set to match (confirmed via `gdalinfo`/`rasterio`). `plotting._open_raster_dataarray` now always
passes `rioxarray.open_rasterio(path, masked=True)` to handle both cases uniformly — without it, the
sentinel dominates `plot.imshow`'s automatic vmin/vmax and washes the real signal out to a flat gray.

**Second related gotcha, found chasing a follow-up striping report (see `docs/history.md`'s Phase 21
entry for the full investigation)**: ALE's `isd_generate` always emits `framelet_order_reversed:
false`, regardless of the cube's actual content — it does not read `framestitch`'s own `DataFlipped`
label field, which *does* correctly record whether `FLIP=TRUE`/`FALSE` was used. Left at the wrong
(always-`false`) default, `mapproject` assigns each framelet the wrong pose whenever `flip=True` was
actually used (any mirrored/`k=3` pass — see "Pass-dependent sensor axis convention" above) —
confirmed empirically: severe venetian-blind-style banding at every framelet boundary with the wrong
value, completely gone with the correct one, on the same real product/DEM. A separate, similarly-
named field, `framelets_flipped` (within-framelet *line* order, not framelet *sequence* order), was
also tested and rigorously ruled out as unrelated — patching it produced a byte-for-byte identical
`mapproject` output on a fixed grid; ASP's implementation doesn't appear to consume that field at
all. `isis_wac.run_isd_generate` now patches `framelet_order_reversed` to match the same `flip` value
`framestitch` was run with (threaded through via `FramestitchResult.flip`).

## `usgscsm`'s `groundToImage` bug for Pushframe sensors, and the ISIS `cam2map` fix

The 0.999-correlation "fix" documented above (patching the cropped cube's ISD ephemeris-time
fields) was a false positive, caught by a user's manual visual inspection of the actual notebook
output (a 3-region gap in the valid-data mask, plus a real ~33-35km position offset from the
synthetic render's own overlay) — this project's own automated checks (a too-small correlation
sample, and `mapproject_single --query-pixel`, which turned out to be unreliable for this sensor
model even on the known-good full cube) had missed both. Full investigation (see
`docs/history.md`'s dated entry for the blow-by-blow):

- **Consulted Laura, Mapel & Hare 2020** ("Planetary Sensor Models Interoperability Using the
  Community Sensor Model Specification", DOI 10.1029/2019EA000713) at the user's suggestion. Found
  it doesn't actually cover Pushframe sensors — Table 2 lists only framing (MDIS, ISS, Dawn) and
  line-scan (LROC-NAC, CTX, HRSC, Kaguya TC) sensors, and the paper's own conclusion lists "push
  frame sensors" under future work. It did usefully confirm (Table 1's field descriptions) that
  `center_ephemeris_time` shouldn't matter independently of `starting_ephemeris_time`/
  `ending_ephemeris_time` — later confirmed to be exactly right, and *why*, from the C++ source
  directly (see below).
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
- **Isolated the real bug**: a controlled 2x2 test (touching `ck_table_start_time`/`ck_table_end_time`
  vs. not, crossed with "forward"/physically-correct vs. "backward"/empirically-hacked
  `starting_ephemeris_time`) showed `ck_table_start_time`/`ck_table_end_time` have **zero** effect
  on `mapproject`'s output either way (byte-identical results) — ruling out the leading ISD-field
  hypothesis entirely. Only the timing direction mattered, and neither direction produced correct
  content: a direct correlation check against the known-good full-cube reference, *at the crop's
  own true location* (no shift search needed), gave only ~0.40 either way, and a ±5km shift search
  barely moved it (0.44 peak) — ruling out a pure translation error.
  **`cam_test`** (`--cam1`/`--cam2` set to the *same* camera, an image→ground→image round-trip
  self-consistency check) showed a real, non-random defect: median ~67px error on the 70-framelet
  crop vs. ~17px on the full 258-framelet cube, and — the decisive test — iterating the same
  transform repeatedly never converged to a stable fixed point, just drifted monotonically toward
  the image boundary. That rules out "found a different but valid answer" (which would show up as
  a stable fixed point after 1-2 iterations) in favor of genuine non-convergence.
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
  **Deliberately not pixel-grid-aligned** to `LunaservResult`'s own raster (no `UpperLeftCornerX`/
  `UpperLeftCornerY` pinning, no post-hoc `gdalwarp`/resampling pass) — `plotting.plot_overlay`
  composites both rasters via their own real georeferenced coordinates (`rioxarray`/`xarray`), not
  a shared pixel grid, so matching *projection* (verified above) is sufficient; a separate
  resampling pass would have reintroduced exactly the kind of interpolation-quality/subtle-
  misalignment risk this whole detour was meant to avoid.
- **`cam2map`'s own `WARPALGORITHM=AUTOMATIC` default introduces real striping for this sensor**,
  found via a user's manual visual check the automated correlation checks above had missed (a
  correlation check only sees pixels valid in *both* rasters — it can't detect matching coverage
  gaps). ISIS's docs recommend `AUTOMATIC` specifically for push frame cameras (it picks
  `FORWARDPATCH` with `PATCHSIZE` locked to the full framelet height, 14px, "to ensure the patch
  size does not cross multiple framelets") — but that same doc also says a patch is silently
  dropped if its 4-corner affine fit isn't within 0.1px of the camera model's own computation, and
  that check was failing for roughly half the framelets here (confirmed on the *full* cube too, not
  crop-specific — a raw boolean-grid dump of the output, not a coarse row/column average, is what
  actually revealed the real diagonal gap bands; coarse averaging was too blunt to catch it).
  **Fix**: explicit `WARPALGORITHM=forwardpatch PATCHSIZE=4` — verified coverage went from ~47% to
  ~71% (matching the crop's real footprint, no more gaps) with content correlation still excellent
  (0.9954, vs. 0.9999986 at the broken default — the small drop is patch-fit noise, not a real
  regression).
- **Position residual — real, diagnosed, left as a known residual per user direction (not a
  centroid artifact, and not an off-by-one in `crop_window_for_camera`)**. Even after the fixes
  above, the crop's designated center pixel (checked directly via `campt`, not just an aggregate
  valid-pixel centroid) genuinely images ground ~11km from `crop_footprint`'s independently
  ray-traced center, despite both using the exact same frame-index formula. Ruled out a
  frame-selection bug first: reconstructed ISIS's own per-line time formula from three exact
  frame-boundary `campt` queries and confirmed `crop_window_for_camera` selects exactly the
  intended chronological frame range, correctly reflecting `framestitch`'s line-order reversal for
  this product's `flip=True` — ISIS's per-line time matches `camera.frame_et()` to within 0.016s
  for the corresponding frames. The actual cause: at that same matched instant, our own
  SPICE-based pointing (`camera.camera_pose_moon_me`, via `spice.pxform`) and ISIS's own camera
  model disagree by ~11km. ISIS's `spiceinit web=yes` furnishes a second CK kernel,
  `moc42r_2019304_2019335_v01.bc`, that `spice_kernels.py`'s `WAC_CK_PREFIXES = ("lrosc", "lrolc")`
  never fetches — and it isn't even present in the NAIF metakernel our own code already parses, so
  it's not a simple missing-prefix fix; it would need an entirely different kernel source.
- **Also tried and ruled out**: ASP `mapproject -t isis` (uses ISIS's own native sensor model
  instead of `usgscsm`/CSM, given a plain `.cub` with no separate ISD sidecar) as a possible
  simpler alternative to the hand-written PVL + `cam2map` approach above. Tested directly against
  the same crop cube — immediately rejected: `"ERROR: Unusual input file... Seems to have Isis
  camera type 1. Check your data. Maybe it will work with CSM."` ASP's own ISIS session wrapper
  doesn't support this camera type (Pushframe) at all, not a flag/workaround issue. `cam2map`
  remains the only working native-ISIS reprojection path found for this sensor.
