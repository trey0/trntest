# Data sources reference

Researched specifics for the external data this project depends on. Keep this file updated
as concrete choices are made (exact EDR product, exact kernel files, etc.) so that knowledge
isn't only in code comments or conversation history.

## Lunaserv WMS (DEM + visible imagery)

- Endpoint: `https://wms.im-ldi.com/lunaserv/lunaserv_stage?` (WMS 1.1.1). Run by ASU/LROC.
- `GetCapabilities`: `?request=GetCapabilities&service=WMS&version=1.1.1`
- Formats supported by GetMap include `image/tiff` and `image/tiff; mode=32bit` (float32 —
  needed for real elevation values, not a colorized/stretched render).
- SRS: confirmed empirically that `IAU2000:30100` works for GetMap on both layers below and
  returns a plain geographic (lon/lat, degrees) raster on a **sphere** of radius 1737400 m (GDAL
  reports it as an unprojected `GEOGCRS`) — i.e. this is the layers' native, unprojected grid; no
  reprojection needed before feeding `sat_sim`.
- **Gotcha (important):** `luna_wac_dtm_numeric_meters_absolute`'s pixel values are
  **planetocentric radius in meters** (empirically ~1.73-1.74 million, not small elevation values),
  not height-above-datum. `scripts/fetch_lunaserv.py` subtracts the reference radius
  (`MOON_RADIUS_M = 1737400.0`) before handing the DEM to ASP, producing normal-looking elevations
  (observed range for our ROI: -3563 to +948 m). Feeding the raw radius values straight to ASP would
  silently double-count the planet's radius.
- Near the poles, a lon/lat bbox is very non-square in physical km (1° longitude shrinks by
  `cos(lat)`); `pixel_dims_for_gsd()` in `scripts/fetch_lunaserv.py` computes width/height from the
  actual physical footprint size (not naive degrees-to-pixels) so both axes sample at the same
  ground resolution.
- Layers of interest:
  - `luna_wac_global` — "LROC WAC Global 100m/px" visible mosaic (GLD100-projected WAC mosaic).
    Global bbox `-180/-90/180/90`.
  - `luna_wac_dtm_numeric_meters_absolute` — GLD100 elevation, actual meters (not a shaded/colorized
    render — the "numeric" layers encode real values in pixels). This is the DEM to feed `sat_sim`.
  - Other candidates seen in capabilities if higher resolution is ever needed: `luna_nac_dtms`,
    `luna_pds_rdr_dtm`, per-Apollo-site DTMs/NAC mosaics (much higher res, smaller coverage).
- Usage policy: free/open, but credit "NASA/GSFC/Arizona State University" per their FAQ.

## ASP `sat_sim`

- Docs: https://stereopipeline.readthedocs.io/en/latest/tools/sat_sim.html
- Takes `--dem` + `--ortho` (a georeferenced/mapprojected image aligned to the DEM), and either
  auto-generates cameras (`--first/--last/--num/...`) or reads existing ones via `--camera-list`
  (one `.tsai`/CSM path per line).
- `--sensor-type pinhole` (default) writes `.tsai` Pinhole camera files; `--save-as-csm` also/instead
  emits a CSM model-state JSON sidecar. This CSM state JSON is what we're calling the "ISD sidecar" —
  technically a CSM *state* file, not a from-scratch ISD; double check at implementation time whether
  this distinction matters for whatever downstream tooling consumes it.
- Input DEM should have no holes (use `dem_mosaic --hole-fill-length`), extend well beyond the AOI.
  ASP's own docs suggest a local stereographic projection, but per user preference: try feeding
  `sat_sim` the DEM/ortho in Lunaserv's **native** projection first (avoids an extra `gdalwarp`
  resampling step) and only reproject if `sat_sim` demonstrably requires it.
- **`--save-as-csm` only applies when `sat_sim` generates its own cameras** — when using
  `--camera-list` with pre-existing `.tsai` files (our case), it's silently a no-op (only the
  rendered `.tif` is written, no camera file at all). To get the CSM/"ISD" JSON sidecar for a
  specific `.tsai`, use ASP's `cam_gen` instead:
  `cam_gen <rendered.tif> --input-camera <cam.tsai> --camera-type pinhole --refine-intrinsics none
  -o <cam.json>` — `--refine-intrinsics none` keeps the pose/intrinsics exactly as given (pure
  format conversion, not a re-solve). Confirmed working: `cam_gen` independently recovered the same
  sub-spacecraft lon/lat/altitude from our `.tsai`'s ECEF `C`/`R` as our own SPICE computation,
  cross-validating the whole pose pipeline.
- The resulting file (`USGS_ASTRO_FRAME_SENSOR_MODEL` state) is **not plain JSON** — the first
  line is a bare model-name string, and the rest is the JSON blob (standard CSM "state string"
  convention: `f"{model_name}\n{json_body}"`). Parsers must skip line 1 before `json.loads`.
- `.tsai` Pinhole format (https://stereopipeline.readthedocs.io/en/latest/pinholemodels.html):
  `P = R*Q + C` — camera center `C` and rotation `R` (camera-to-world) are in a body-fixed Cartesian
  frame (ECEF-equivalent), independent of the DEM/ortho's map projection. For the Moon this should be
  the **Mean Earth (MOON_ME)** frame, matching USGS lunar cartographic conventions (GLD100/LOLA).

## LRO SPICE kernels (NAIF)

- Archive root: `https://naif.jpl.nasa.gov/pub/naif/pds/data/lro-l-spice-6-v1.0/lrosp_1000/`
  - Subdirs by kernel type: `data/ck`, `data/spk`, `data/ik`, `data/fk`, `data/sclk`, `data/lsk`,
    `data/pck`, plus `extras/mk` (one metakernel per time range/year).
- The yearly metakernel lists every kernel needed to cover that year — treat it as a **manifest to
  parse**, not something to furnish wholesale. CK (pointing) kernels dominate the data volume; only
  download the specific CK/SPK file(s) whose filename-encoded date range covers the timestamp we need,
  plus the small LSK/SCLK/PCK/FK/IK files (needed regardless, cheap).
- Request `MOON_ME` directly from spiceypy calls (position + orientation) rather than getting MOON_PA/
  J2000 and rotating manually — the standard lunar frame kernel defines `MOON_ME` for direct use.
- Yearly metakernels (`extras/mk/lro_YYYY_vNN.tm`) list, for CK, **five separate kernel "flavors"**
  covering the same date ranges: `lrosc` (spacecraft bus attitude — the main reconstructed
  pointing), `lrolc` (LROC-specific: small thermally-dependent offset of frame -85620 relative to
  the bus — see below), `lrodv` (delta-V/maneuver attitude), `lrohg` (high-gain antenna gimbal),
  `lrosa` (solar array gimbal). **Only `lrosc` + `lrolc` are relevant to the WAC boresight** — skip
  `lrodv`/`lrohg`/`lrosa` entirely, which cuts CK downloads roughly 5x for a given day.
- CK/SPK filenames encode a `YYYYDDD_YYYYDDD` date range but adjacent files can overlap by a day —
  don't just pick the filename whose range contains the target date; after furnishing a candidate,
  verify actual coverage with `spiceypy.ckcov`/`spkcov` and fall back to the neighboring file if the
  exact timestamp isn't covered.
- IK files are per-instrument (`lro_crater_v03.ti`, `lro_dlre_v05.ti`, `lro_lamp_v03.ti`,
  `lro_lend_v00.ti`, `lro_lola_v00.ti`, `lro_lroc_v20.ti`) — only `lro_lroc_v20.ti` is needed here.
- Always-needed small/generic kernels regardless of date: `lsk/naif0012.tls`,
  `sclk/lro_clkcor_2025351_v00.tsc` (~2.3 MB, single mission-long file, not date-ranged),
  `pck/pck00010.tpc`, `pck/moon_pa_de421_1900_2050.bpc`, `fk/lro_frames_2014049_v01.tf`,
  `fk/moon_assoc_me.tf`, `fk/moon_080317.tf` (defines `MOON_ME` as a fixed offset from `MOON_PA`),
  `ik/lro_lroc_v20.ti`, `spk/de421.bsp` (planetary ephemeris; the Moon PA/ME frame chain needs it).
- Confirmed: `fk/moon_assoc_me.tf` is exactly the NAIF-provided association kernel that makes
  `MOON_ME` the default lunar body-fixed frame — loading it (+ `moon_080317.tf` which defines
  `MOON_ME` itself, + the PA orientation kernel) lets `spkezr`/`pxform` be asked for `'MOON_ME'`
  directly. This confirms the plan's approach (no manual PA→ME rotation needed).
- WAC frame chain (from `lro_frames_2014049_v01.tf`): `LRO_LROCWAC` (NAIF ID **-85620**) is
  CK-dependent (small thermally-varying offset from `LRO_SC_BUS`, +Z boresight) — this is exactly
  what the `lrolc` CK provides. `LRO_LROCWAC_VIS` (-85621) and the 5 VIS filter frames
  (-85631..-85635) are then *fixed* (TKFRAME) offsets from -85620, defined right in the FK — no CK
  needed for those.

### Chosen EDR product for this demo

- Product `M1329714703CE`, path
  `LRO-L-LROC-2-EDR-V1.0/LROLRC_0041C/DATA/ESM4/2019334/WAC/M1329714703CE.{IMG,xml}` on
  `pds.lroc.im-ldi.com` (redirects from `pds.lroc.asu.edu`).
- `start_date_time` 2019-11-30T00:57:15.433Z, `stop_date_time` 2019-11-30T01:03:42.120Z (day-of-year
  334, matches the `2019334` in the archive path and in kernel filename date ranges).
- `spacecraft_clock_start_count` = `1/596768235:26909` (LRO SCLK partition/seconds:subticks —
  feed straight to `spiceypy.scs2e(sc, sclk_string)`).
- `orbit_number` 46980, mission phase "FOURTH EXTENDED SCIENCE MISSION".
- `nframes` 538, `interframe_delay` 718.75 ms, `exposure_duration` 60 ms — i.e. framelet *i*'s
  approximate mid-exposure time is `start_date_time + i * 0.71875s`. **Framelet 440 is used, not a
  low index near the start of the swath** — see "Real image comparison" below for why (frames
  0-~210 turned out to be in near-total shadow).
- Raw file is 704 samples x 41964 lines (multiple interleaved filter bands/framelets per the 538
  cycles) — 704 samples is the native WAC swath width in pixels at this framelet's resolution.
- Relevant kernel files for this specific date (day 334, 2019), found by day-of-year range match
  (still to be verified with `ckcov`/`spkcov` at implementation/runtime, not just by filename):
  `ck/lrosc_2019325_2019335_v01.bc`, `ck/lrolc_2019304_2019335_v01.bc` (or its neighbor
  `ck/lrolc_2019334_2020001_v01.bc` — the two overlap on day 334, verify with `ckcov`),
  `spk/lrorg_2019258_2019349_v01.bsp`.

## LROC WAC EDR products

- Browsable archive: `https://pds.lroc.asu.edu/data/LRO-L-LROC-2-EDR-V1.0/<volume>/DATA/<subdir>/<doy>/WAC/<product>.xml`
  (e.g. `LROLRC_0041C/DATA/ESM4/2019334/WAC/M1329767554CE.xml` — a real example found during research,
  not necessarily the one we'll use).
- PDS Geosciences Node **Orbital Data Explorer (ODE) REST API**: `https://oderest.rsl.wustl.edu/` —
  can search by instrument/time/lat-lon instead of browsing directories by hand.
- WAC is a 7-color **push-frame** camera (100 m/px visible, 400 m/px UV) — framelets captured
  periodically as the spacecraft moves, not a continuous line-scan. The EDR label carries
  `START_TIME`/SCLK and framelet timing needed to map "which part of the swath" to a timestamp.
- Raw EDR/CDR byte layout: EDR has a 7040-byte PDS3 attached header, CDR 10560 bytes (extra
  calibration metadata prepended); both then hold the same 704 samples x 41964 lines grid,
  row-major ("Last Index Fastest" = sample is the fast axis). EDR is `UnsignedByte` DN; CDR is
  `IEEE754LSBSingle` (float32) I/F (calibrated reflectance factor) — same raw multiplexed geometry
  in both, CDR calibration does **not** band-separate or geometrically reproject anything.
- 78 raw lines per framelet cycle = 2 UV filters x 4 TDI lines + 5 VIS filters x 14 TDI lines.
  **Confirmed from the official LROC EDR/CDR SIS** (`LROCSIS.PDF`, fetched from each product
  volume's `DOCUMENT/` dir): "WAC band passes are arranged first UV then VIS (320, 360, 415, 565,
  605, 645, 690), but the order is reversed after LRO performs a 180° yaw maneuver to align the
  solar panels with the sun." The SIS also states plainly: "the WAC CDR file will require further
  processing to separate framelets into their respective bands and to align the bands, in order to
  be viewed as a standard multi-band image" — i.e. a raw multiplexed strip (what an earlier version
  of this demo displayed) was never going to look like a picture; that's expected, not a bug.
- CDR `Special_Constants`: `missing_constant = 0xFF7FFFFB` (as float32, ≈ -3.4028e+38). A UV
  framelet line is 4 TDI lines but the UV detector is only 512 px binned to 128 px — the other
  ~576 (of 704) samples in a UV line are padding, hence a big chunk of `missing_constant` values
  concentrated in the 8 UV lines of each 78-line frame; a pure-VIS 14-line block has only ~0.4%
  missing (a handful of bad/edge columns), not the ~8.9% seen when averaging over all 78 lines.

### Real image comparison (Phase 5): band separation + finding sunlit frames

Two fixes were needed to get a real image that's actually comparable to the synthetic render (see
`scripts/fetch_wac_comparison.py`):

1. **De-interleave one VIS filter across many frames.** WAC's push-frame design is meant to build
   continuous coverage by "repeated imaging such that each of the narrow framelets of each color
   band overlap" (SIS) — i.e. take the *same* filter's TDI-line block from each of many consecutive
   frames and stack them vertically; adjacent frames' blocks tile almost seamlessly (interframe
   ground advance ≈ 1.19 km vs. a ~1.05-1.4 km per-block footprint at this altitude/GSD). Lines
   `[22:36)` within the 78-line frame are used: since UV only ever occupies the first-or-last 8
   lines (depending on the yaw-dependent order above), `[22:36)` is guaranteed to fall entirely
   inside the VIS region either way — which exact one of the 5 VIS wavelengths it is depends on the
   yaw state, which wasn't determined (irrelevant for just getting a recognizable picture).
2. **Frame 0 (and up to ~210) is in near-total shadow.** Scanning I/F statistics across the de-
   interleaved VIS block for frames 0, 30, 60, ... 530 showed means and maxima at the noise floor
   (some even negative) through frame ~210, then jumping to real signal (mean ~0.003-0.018, max up
   to ~0.07) from frame ~240 onward, stable through at least frame ~530. **Framelet 440** was picked
   from that stable, well-lit stretch — `build_camera_from_spice.TARGET_FRAME_INDEX = 440` — so
   both the synthetic camera and the real-image comparison land on visible terrain. (This also
   explains why the very first version of this comparison, which used frame 0, looked like nothing
   no matter how the data was decoded: there was essentially no signal to see there.)
3. Verified visually: stacking 19 consecutive frames' VIS blocks (266 lines x 704 samples) starting
   at frame 440, contrast-stretched over valid (non-`missing_constant`) pixels, produces a clearly
   recognizable cratered lunar scene that visibly matches the synthetic render's terrain (same
   bright diagonal feature, same dark crater) — confirms both the band-separation logic and the
   frame choice are correct.

Product chosen and used (see above): `M1329714703CE`, posed at **framelet index 440** (not 0).
Computed LRO position in `MOON_ME` at that instant: sub-spacecraft/output camera center lon/lat/alt
≈ (112.03°, -82.56°, 68.51 km) — still consistent with LRO's low south-polar Fourth Extended
Science Mission orbit, just a different point along the same pass than frame 0. Camera intrinsics:
`fu=fv` derived from the actual slant range at this instant, `cu=cv=128`, 256x256, `pitch=1`
(chosen so GSD ≈ 100 m/px to match Lunaserv's WAC/GLD100 source resolution).

**Gotcha (fixed):** the Lunaserv WMS tile cache is keyed by `(layer, bbox, width, height, format)`,
so after `TARGET_FRAME_INDEX` moved from 0 to 440 the cache ended up holding tiles for *both*
footprints. `scripts/run_sat_sim.sh` originally picked the ortho tile via
`ls .../luna_wac_global/*.tif | head -1` — which silently grabbed the *stale* (frame-0) tile,
mismatched against the freshly-regenerated (frame-440) DEM. Fixed by having
`fetch_lunaserv.fetch_dem_and_ortho()` write the exact resolved paths it used to
`output/lunaserv_result.txt`, which `run_sat_sim.sh` now sources — never glob the cache dir for
"any" tile of a layer.
