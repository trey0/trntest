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
  not height-above-datum. `src/trntest/lunaserv.py` subtracts the reference radius
  (`MOON_RADIUS_M = 1737400.0`) before handing the DEM to ASP, producing normal-looking elevations
  (observed range for our ROI: -3563 to +948 m). Feeding the raw radius values straight to ASP would
  silently double-count the planet's radius.
- Near the poles, a lon/lat bbox is very non-square in physical km (1° longitude shrinks by
  `cos(lat)`); `pixel_dims_for_gsd()` in `src/trntest/lunaserv.py` computes width/height from the
  actual physical footprint size (not naive degrees-to-pixels) so both axes sample at the same
  ground resolution.
- **Antimeridian:** LRO's near-polar orbit means a camera footprint can straddle +-180° longitude.
  Confirmed empirically that GetMap handles an out-of-range bbox (e.g. `170,40,190,45`) correctly —
  it returns the same real, non-blank pixel data as the equivalent in-range request expressed the
  other way (`-190,40,-170,45`), i.e. longitude is treated cyclically, not clipped to the layer's
  nominal `-180/180` global bbox. `footprint_bbox_deg()` in `src/trntest/lunaserv.py` relies on
  this: it unwraps footprint corner longitudes onto a common branch (relative to the first corner)
  before taking min/max, which can produce a bbox that extends slightly outside `[-180, 180]` — this
  is intentional and works, not a bug to "fix" by clamping.
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

### Ascending-node search: `gfposc`, and a `functools.cache` gotcha (Phase 10)

- `illumination.find_ascending_node_crossings` finds LRO's `MOON_ME`-frame latitude=0 crossings via
  SPICE's `gfposc` (geometry finder over position coordinates: `targ="LRO"`, `frame="MOON_ME"`,
  `obsrvr="MOON"`, `crdsys="LATITUDINAL"`, `coord="LATITUDE"`, `relate="="`, `refval=0.0`) — one
  call over the whole search window, SPICE's own compiled adaptive root-finder, instead of a
  hand-rolled 60s-step sample-and-bisect loop making thousands of Python↔SPICE round trips. Still
  returns both ascending and descending crossings (`relate="="` doesn't distinguish direction); keep
  filtering for ascending via a small ±5s latitude sign check, same as before. Needs SPK coverage for
  the *whole* confinement window furnished at once — `spice_kernels.furnish_spk_range` does this
  (SPK/`lrorg` only, not CK; safe to pre-furnish a whole search window's worth since SPK volume is
  small relative to CK, see above), unlike `fetch_and_furnish`'s per-epoch just-in-time pattern used
  everywhere else in this codebase for full camera-pose work (which does need CK).
- **Gotcha, worth remembering for any future `functools.cache`-on-`TrntestConfig` usage**:
  `spice_kernels.latest_metakernel_url` is `@functools.cache`d on `(year, ...)` specifically because
  it's a live, uncached-by-design NAIF directory listing that a sweep evaluating many candidates
  would otherwise re-hit once per candidate. It was originally keyed on `(year, config)` — a whole
  `TrntestConfig`, not just the one field (`naif_base_url`) it actually reads. Since
  `dataset.evaluate_candidate_image` builds a fresh per-candidate config via `dataclasses.replace`
  (different `edr_volume`/`edr_product`/etc. per row), every candidate produced a *different* cache
  key by value, silently defeating the memoization entirely — ~1600 real HTTP requests for a 7-day,
  1633-candidate sweep, ~495s of a ~500s total call (confirmed via `cProfile`; `spice.furnsh()`
  itself was only 0.178s total, ruling out kernel-file-size as the cause). Fixed by keying on
  `(year, naif_base_url: str)` instead of the whole config. **Lesson**: when memoizing on a
  `TrntestConfig` argument, the cache key is the whole dataclass by value — fine when callers reuse
  one shared config, a real footgun when callers legitimately vary it per-item (as `dataset.py`'s
  per-candidate `dataclasses.replace` does) but the cached function only cares about one field of it.
  Prefer keying on just the specific field(s) actually used, not the whole config, whenever the
  caller might vary other fields per call.

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
`src/trntest/wac.py`):

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
   from that stable, well-lit stretch — `config.target_frame_index = 440` — so
   both the synthetic camera and the real-image comparison land on visible terrain. (This also
   explains why the very first version of this comparison, which used frame 0, looked like nothing
   no matter how the data was decoded: there was essentially no signal to see there.)
3. Verified visually (first with a fixed 19-frame crop, then with the real-geometry-sized crop
   below), contrast-stretched over valid (non-`missing_constant`) pixels: produces a clearly
   recognizable, dramatic cratered lunar scene (a large crater with a bright central peak/rim and
   dark, likely-permanently-shadowed floor) — confirms both the band-separation logic and the
   frame choice are correct.

Product chosen and used (see above): `M1329714703CE`, posed at **framelet index 440** (not 0).
Computed LRO position in `MOON_ME` at that instant: sub-spacecraft/output camera center lon/lat/alt
≈ (112.03°, -82.56°, 68.51 km) — still consistent with LRO's low south-polar Fourth Extended
Science Mission orbit, just a different point along the same pass than frame 0.

### Square-crop sizing: real ground area, not a fixed pixel/frame count

Originally the synthetic camera's FOV was sized to hit a fixed ~100 m/px GSD at 256x256, and the
real CDR comparison crop used a fixed 19 frames (chosen ad hoc to look roughly 256 px tall) —
neither was grounded in the instrument's actual FOV, so the two images didn't reliably cover the
same real ground area. Fixed by deriving both from the real WAC color-mode field of view:

- Tried reading the real FOV straight out of the loaded WAC-VIS IK via
  `spice.getfov(-85621, ...)` — it returns a symmetric ~91.6°-derived pyramid, which matches the
  SIS's **monochrome**-mode cross-track FOV (91.7°), not the narrower color-mode readout (which
  only uses the center 704 of the full ~1024-wide detector). So the IK's generic FOV entry isn't
  usable directly for the color-mode crop; the SIS's explicit color-mode figure — **61.4°** — is
  used instead (`config.wac_vis_color_fov_deg`).
- The synthetic camera's `fu=fv` is now `(config.image_size/2) / tan(61.4°/2)` (≈215.6 px at 256x256) —
  its angular FOV literally equals the real WAC color-mode FOV at the same pose, so its footprint
  matches the real swath width by construction.
- The real cross-track ground width at frame 440's exact pose is computed by ray-tracing the
  ±30.7° rays (half of 61.4°) along the camera's cross-track axis to the Moon's sphere and taking
  the chord distance between the two ground points (`cross_track_width_km`) — **≈82.0 km**
  (implied GSD ≈82.0 km/704 ≈ 116 m/px, a plausible value for WAC at this ~68.5 km altitude).
- The real per-frame ground advance (`km_per_frame`) is the chord distance between the boresight
  ground point at frame 440 and frame 450, divided by 10 — **≈1.147 km/frame**.
- `n_frames_for_square_crop = round(cross_track_width_km / km_per_frame)` — **71 frames**, giving
  a `71*14 = 994` line x 704 sample real CDR crop. Not square in *pixels* (cross-track and
  along-track have different native GSD), but square in real km, matching the synthetic camera's
  FOV — this was the user's explicit request and tolerance.
- All of this lives in `camera.compute_n_frames_for_square_crop()`, reusable by
  both `build()` (included in its returned info dict) and `fetch_wac_comparison.fetch_vis_mosaic()`
  (used as its default `n_frames` when not given explicitly).

**Gotcha (fixed):** the Lunaserv WMS tile cache is keyed by `(layer, bbox, width, height, format)`,
so after `config.target_frame_index` moved from 0 to 440 the cache ended up holding tiles for *both*
footprints. `src/trntest/render.py`'s `run_sat_sim()` originally picked the ortho tile via
`ls .../luna_wac_global/*.tif | head -1` — which silently grabbed the *stale* (frame-0) tile,
mismatched against the freshly-regenerated (frame-440) DEM. Fixed by having
`fetch_lunaserv.fetch_dem_and_ortho()` write the exact resolved paths it used to
`output/lunaserv_result.txt`, which `run_sat_sim.sh` now sources — never glob the cache dir for
"any" tile of a layer.

### Pose epoch fix: crop's temporal midpoint, not its start

The real CDR crop spans `n_frames` (71) frames *starting at* `config.target_frame_index` (440) — frames
440 through 510. The synthetic camera's pose was being computed at frame 440's exact timestamp —
the crop's *start*, not its middle — so the synthetic image's center should have lined up with the
real crop's *top edge*, not its center. Fixed in `camera.build_camera()`: compute
`crop_info` (and thus `n_frames_for_square_crop`) first using `config.target_frame_index`'s geometry as
the estimate (negligible drift over ~71 frames/~49 seconds), then derive
`center_frame_index = config.target_frame_index + n_frames/2 = 475.5` and use *that* epoch for the actual
pose (`C`/`R`, focal length base, footprint corners, and hence the Lunaserv ROI too). No change
needed in `fetch_wac_comparison.py` — the real crop correctly starts at frame 440 regardless.

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
- **Finding — the two images are rotated ~90° relative to each other, and this is real, not a
  bug.** Cross-projecting each image's own (inset) corners into the *other* image's pixel space
  shows synthetic `top_left` ≈ crop `bottom_left`, synthetic `top_right` ≈ crop `top_left`, and so
  on around — a consistent 90° rotation, confirmed numerically (closest-corner matching, ~0.06-0.4°
  residual — plausible given the two corner sets are defined differently: synthetic's from one
  fixed pose, the crop's from its start/end poses). This is a direct consequence of the two images'
  differing pixel-axis conventions given the WAC-VIS **X = along-track, Y = cross-track** finding
  above: the crop's rows are explicitly built to be along-track (X) and columns cross-track (Y),
  while the synthetic image's `pixel_ray_cam` maps `px`(columns)`->`camera `X` and
  `py`(rows)`->`camera `Y` — i.e. the synthetic render's rows/columns are swapped relative to the
  crop's convention. The tie-point markers in the comparison figure correctly reflected this
  rotation (that was them doing their job) at the time — left unfixed in that round since the user
  had only asked for tie points and their two specified checks. **Now fixed** — see "Fixed
  sensor-model axis convention" below.

### Sensor-model axis convention (originally believed fixed; actually pass-dependent -- see "Fixed: WAC CDR mirror" below)

The 90° mismatch above came from the synthetic camera's pixel axes being an arbitrary in-house
choice (`px→X, py→Y`) with no relation to any instrument convention. Fixed in
`camera.py` by rotating the camera's `R` by 90° about its own
boresight before writing the `.tsai` — deliberately **not** influenced by which way is "north" for
this pass (that's a separate, later concern; see "North-up display rotation" below). This section
is preserved as-derived for the original single-demo product; the claim below that `k=1` is a fixed
hardware constant turned out to be wrong (Phase 9) — `boresight_rotation_k` now computes `k`
per-pose instead. See "Fixed: WAC CDR mirror relative to synthetic image" (after "North-up display
rotation") for the corrected understanding and the full story of how this was found.

- Checked NAC's own convention too (LROC SIS): NAC is a simple pushbroom line-scan camera,
  "5064-pixel CCD line-array providing a cross-track field-of-view" — i.e. NAC's samples are
  cross-track too. So this isn't actually a WAC-vs-NAC fork: both instruments' real archived-image
  layouts agree (samples/columns = cross-track, lines/rows = along-track) — one convention to
  adopt and motivate, not a choice between two.
- A pinhole camera's rendered image is fixed only up to rotation about its own boresight (a proper,
  handedness-preserving rotation — unlike swapping two axes while holding the boresight fixed,
  which is a reflection and would require flipping the boresight too, breaking "forward"). Rotating
  `R` by `rotation_about_boresight(k)` for `k=0,1,2,3` cycles which raw camera axis (`X`, `Y`, `-X`,
  `-Y`) maps to `px` (and correspondingly the other to `py`). Two of the four (`k=1`, `k=3`) put
  `px∥Y` (cross-track) and `py∥X` (along-track) — the desired convention; `k=0`/`k=2` keep the
  original, unmotivated mapping.
- Between `k=1` and `k=3`: picked `k=1` (for *this* product) so that increasing `py` (row) matches
  the **same temporal sense** as the real archived WAC image's row axis (which increases forward in
  time, by construction of how `fetch_wac_comparison.py` stacks frames). Consecutive-frame
  ground-track motion measures as dominantly `-X` in the raw WAC-VIS frame (`[-1.146, 0.001,
  -0.022]` km, from the earlier finding) — i.e. "forward in time" is `-X` for this product. `k=1`
  maps `py→-X`, matching that sense; `k=3` would map `py→+X`, the opposite (backward-in-time) sense.
  **This was originally asserted to be a hardware/data-format property, fixed regardless of orbit
  pass/yaw state — that assertion was wrong** (Phase 9): a second, independently-selected product
  measured dominant `+X` instead. See "Fixed: WAC CDR mirror relative to synthetic image" below.
- `camera.build_camera()` now stores the rotated `R` in the returned `Camera`
  (`camera.r_cam_to_me`) — `tie_points.py` uses this directly (`compute_tie_points()`)
  rather than recomputing/re-deciding anything, so there's a single source of truth for "the
  camera's actual pose as used for the `.tsai`."
- This changes the actual rendered pixels (a real 90° rotation of the output image), so the
  pipeline must be (and was) re-run: `src/trntest/render.py`'s `run_sat_sim()` (render) and `cam_gen` (CSM/ISD JSON).
- **Verified**: re-ran the crop-corner self-consistency check (still exact:
  `(0,0)`/`(704,0)`/`(0,994)`/`(704,994)`) and the synthetic-vs-crop closest-corner match — now
  `top_left↔top_left`, `top_right↔top_right`, etc. directly (not the previous 90°-rotated pairing) —
  and visually, all 5 tie-point markers now sit on matching terrain in both panels.

### North-up display rotation (`src/trntest/orientation.py`, notebook-only)

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
  - Synthetic image: all 4 `k∈{0,1,2,3}` are valid candidates (this is a free display rotation of
    an already-rendered array; the sensor-model's fixed convention above is irrelevant to *this*
    choice). `right_orig = R[:,0]`, `up_orig = -R[:,1]` (using the *already boresight-rotated* `R`
    from `camera_info["r_cam_to_me"]`).
  - Real crop: only `k∈{0,2}` are meaningful (its row axis is real along-track data; a 90°/270°
    rotation would put cross-track on the vertical axis, not "north-up"). `right_orig` comes from
    the **raw** (not sensor-model-rotated) `R` at the crop's center-frame pose (`R_raw[:,1]`,
    cross-track). `up_orig` is **no longer** a fixed raw-axis assumption (`R_raw[:,0]`) — that
    turned out to be pass-dependent (see "Fixed: WAC CDR mirror relative to synthetic image" below)
    — it's now the real, empirically-measured "forward in time" ground-track direction
    (`camera.ground_track_step_km`, negated or not depending on
    `camera.reverse_crop_along_track`, to stay consistent with whichever along-track order
    `wac.fetch_vis_mosaic` actually used for this pass).
- `rotate_pixel_coords(col, row, k, height, width)`: maps a pixel coordinate through the same
  `np.rot90(arr, k)` transform, for repositioning tie-point markers on the rotated display. Also
  **verified numerically** (marked-pixel test) rather than trusted from hand-derived array-index
  algebra alone — an off-by-one crept into the first attempt (dropping a `-1` when moving from
  discrete array indices to continuous pixel coordinates) and was caught this way.
- **This run's result**: both images picked `k=2` (180°) with the same residual deviation from true
  north (26.7°) — expected, since after the sensor-model fix above, both images already share the
  same axis convention, so whatever rotation is needed for one is needed for the other. The 26.7°
  residual (not 0°) reflects that this pass's along-track direction isn't exactly north-south — the
  best achievable result under the "only multiples of 90°, no mirroring" constraint, not a bug.

### Fixed: WAC CDR mirror relative to synthetic image (pass-dependent chirality)

Found while manually reviewing the notebook right after Phase 8 (`docs/plan.md`) added
`generate_dataset()`-selected images in place of the single hand-picked demo product. In the Phase 5
comparison figure, the real WAC CDR panel looked **vertically flipped** relative to the synthetic
image on the newly-selected product `M1327210646CE`, alongside a separate, possibly-related
symptom: visible discontinuities landing on framelet (14-line VIS block) boundaries. Tie points
lined up with the underlying image data the same way across both images (unsurprising: they reuse
`wac.py`'s own row-stacking convention, so they stay self-consistent with it regardless of whether
that convention is physically correct — they can't catch this class of bug by construction).

**A wrong first fix, and how it was caught.** The first hypothesis explored was that this was
actually a *rotation* — that the crop's north-up display logic (`orientation.py`,
`best_k_for_north_up(..., candidates=(0, 2))`) was picking the wrong one of its two 180°-apart
rotation candidates, because the "forward in time is `-X` in the raw WAC-VIS frame" claim in the
sensor-model section above (derived from exactly one product) turned out to be pass-dependent, not
hardware-fixed — confirmed by directly measuring, via real SPICE trajectory data, that this new
product's ground-track direction projects to dominant **`+X`** (`[21.08, -0.06, 0.56]` km over 10
frames), the opposite sign from the original product's `[-1.146, 0.001, -0.022]`. A fix was built
around this (`camera.boresight_rotation_k`, computed per-pose instead of a fixed constant) and
initially believed to be the whole story. **The user correctly challenged this** — a fix built only
from `rotation_about_boresight`/`np.rot90` choices can only ever produce *proper* rotations
(determinant +1) at every layer of this pipeline, and can structurally never produce or repair a
genuine mirror (determinant −1), no matter how the rotation constant is chosen.

**The decisive empirical test**: a *chirality* check, independent of any display-rotation logic
entirely. `tie_points.py` already computes, for known real ground points, both `synthetic_px` (a
provably-always-proper pinhole projection — `r_cam_to_me` is always a proper rotation, from
`spice.pxform` composed with `rotation_about_boresight`, both determinant +1, always) and `crop_px`
(the real crop's own projection). Taking 3 tie points (`top_left`, `top_right`, `center`) as a
triangle and comparing the **sign of its 2D signed area** in `synthetic_px` space vs. `crop_px`
space directly tests whether the two pixel spaces are related by a rotation (same sign) or a mirror
(opposite sign) — entirely independent of `orientation.py`'s display-only rotation search. Result:
- Original demo product (`M1329714703CE`): same sign (synthetic area `9630.58`, crop area
  `102818.91`) — consistent with Phase 5's prior verification, no mirror.
- Flagged product (`M1327210646CE`): **opposite sign** (synthetic area `15352.84`, crop area
  `-166560.33`) — a genuine, real mirror, definitively confirmed, not a rotation. The rotation-only
  fix above could never have addressed this (and empirically didn't — `boresight_rotation_k` only
  ever changes the synthetic image's own display rotation, which the north-up search already
  explores fully, so it's provably invariant to whichever `k` is picked, and never touches the
  crop's own pixel-order construction at all).

**Actual root cause**: WAC's raw camera frame (`LRO_LROCWAC_VIS`) is body-fixed (no gimbal), and LRO
performs periodic 180°-yaw-flip maneuvers (for thermal/power reasons; roughly every ~4 weeks — the
original demo product and the flagged product are ~26 days apart, consistent with a flip having
occurred between them). `wac.fetch_vis_mosaic`'s frame-stacking order is always correct *in time*
(frames are read off disk in strict PDS-archival acquisition order, unconditionally), but a 180°
yaw flip rotates the *entire* raw camera frame together — including which raw axis "forward in
time" projects onto — and this changes the resulting mosaic's **chirality** (how its along-track row
order relates to its across-track column order), not just its rotation, relative to the always-proper
synthetic image. This is the same underlying hardware behavior `wac.py`'s own docstring already
documented for band ordering ("WAC band order is reversed after a 180 deg yaw maneuver") — the
along-track stacking order turns out to be the same kind of yaw-dependent property, confirming the
plan's original "framelet order reversed" hypothesis, just precisely conditioned on a real,
per-pass measurement rather than assumed universally true or false.

**The fix**:
- `camera.Camera.reverse_crop_along_track` (a property derived from `boresight_rotation_k`): true
  when this pass's real ground-track direction is dominant `+X` in the raw camera frame (opposite of
  the original reference convention).
- `wac.fetch_vis_mosaic` now takes the built `Camera` and reverses along-track frame-stacking order
  (`vis[::-1]`, before reshaping) when `reverse_crop_along_track` is true — a genuine mirror
  (negates one array axis), unlike anything `rotation_about_boresight`/`np.rot90` can produce.
- `tie_points.py`'s `_crop_pixel_at_frame`/`project_ground_to_crop_pixel` gained a `reverse`
  parameter (must match `wac.py`'s choice for the same product/pose) so tie-point rows are measured
  from the correct end (`start_frame + n_frames` instead of `start_frame`) when reversed.
- `orientation.py`'s crop `up_orig` (used only by the `k∈{0,2}` north-up display search) now also
  depends on `reverse_crop_along_track`, since which end of the mosaic is "forward in time" changed.
- `crop_footprint_corners` needed **no change** — it computes ground positions (lon/lat) at
  `start_frame`/`start_frame+n_frames`, entirely independent of pixel row/reversal; its "top"/"bottom"
  dict keys are just descriptive labels not consumed elsewhere, so a naming mismatch under reversal
  is cosmetic only, not a correctness issue.

**Verified**: the chirality check above, re-run after the fix, now shows **matching** sign for
*both* products (original: synthetic `9630.58` / crop `102818.91`; flagged: synthetic `15352.84` /
crop `166560.33`, now positive) — the mirror is gone in both cases, confirmed by direct computation,
not just visual impression. `trntest-lint` (ruff format/check, mypy) and the full test suite (73
tests, including a new regression test for the reversal behavior itself) pass. A full
`jupyter nbconvert --to notebook --execute` run on the flagged product's default selection shows the
comparison figure's two panels now showing genuinely matching terrain (same large crater, same
bright diagonal feature) with all 5 tie-point markers landing on the correct matching features in
both panels.

**Reproducibility**: the notebook's current default selection (`select_dataset(max_search_days=7)`
then `.head(1)`) deterministically picks product `M1327210646CE` (orbit 46625) as of the current
cache/window — this is the exact image the flip was found on and the fix was verified against.
