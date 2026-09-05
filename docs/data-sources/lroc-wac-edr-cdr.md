# LROC WAC EDR/CDR products

Index: [`docs/data-sources.md`](../data-sources.md).

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
  temperature/mode-matched `SpecialPixels` correction, see `docs/external-tools.md`), not noise.
  Non-boundary lines are ~0.14% invalid (excl. the two edge columns) vs. **7.67%** on boundary lines
  — a >50x contrast concentrated exactly at framelet seams, which is why such a low overall density
  reads as a strong, regular visual "grid"/moiré pattern once displayed — confirmed this isn't a
  downsampling artifact either (same pattern at native resolution, `interpolation='none'`).
  `plotting._fill_dead_columns_for_display` now interpolates across these narrow (1-3 column) gaps
  row-wise for display in `plot_isis_comparison` only — purely cosmetic (contrast stretch is still
  computed from the real, unfilled valid data).
- **Pass-dependent sensor axis convention** (not a fixed hardware property): WAC's raw camera frame
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

## Reference/regression-test EDR products

The live default image comes from the checked-in, now-frozen `notebooks/dataset_manifest.csv` — a
real, catalog-driven multi-orbit search's result (see `../../README.md`), not any single hardcoded
product. Two specific products remain useful as known test fixtures (one per yaw state, used to
validate the axis-convention/chirality fix above still holds for both):

- `M1329714703CE` — `LRO-L-LROC-2-EDR-V1.0/LROLRC_0041C/DATA/ESM4/2019334/WAC/M1329714703CE.{IMG,xml}`,
  orbit 46980, `nframes` 538, `interframe_delay` 718.75 ms. This repo's original single-demo
  product; non-mirrored (`k=1` convention).
- `M1327210646CE` — orbit 46625, ~26 days earlier than the above (opposite yaw state); mirrored
  under the old fixed-`k` assumption, correctly un-mirrored by `boresight_rotation_k`.
- Relevant kernel files for `M1329714703CE`'s date (day 334, 2019), for reference:
  `ck/lrosc_2019325_2019335_v01.bc`, `ck/lrolc_2019304_2019335_v01.bc` (or its neighbor
  `ck/lrolc_2019334_2020001_v01.bc` — the two overlap on day 334), `spk/lrorg_2019258_2019349_v01.bsp`.
