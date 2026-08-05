# Caching strategy

Both external data sources (Lunaserv WMS, NAIF SPICE archive) are large; we only ever need a
sliver for one region/timestamp. Goal: never re-download the same bytes twice across container
rebuilds or notebook re-runs.

## Layout

A `cache/` directory (mounted as a Docker volume, so it survives container rebuilds) mirrors the
remote structure:

```
cache/
  naif/pub/naif/pds/data/lro-l-spice-6-v1.0/lrosp_1000/data/ck/...   (exact remote paths)
  naif/pub/naif/pds/data/lro-l-spice-6-v1.0/lrosp_1000/data/spk/...
  naif/.../extras/mk/...
  lunaserv/<layer>/<bbox>_<width>x<height>_<format>.tif
  lroc_edr/<volume>/DATA/<subdir>/<doy>/WAC/<product>.*
  isisdata/base/...   (ISIS's own mission-independent reference data)
  isisdata/lro/...    (LRO/WAC calibration files)
```

`isisdata/` is a fourth tree, alongside the three above: ISIS3's own reference data, fetched by
`isis_wac.ensure_isisdata()` for the ISIS/CSM WAC reprojection spike (see
`docs/data-sources.md`'s "ISIS3/CSM spike" section). Fully re-fetchable and safe to prune before
archiving, same as the other three -- `downloadIsisData ... --no-kernels` keeps the real one-time
download to ~5GB (not the ~30GB a plain, un-flagged `downloadIsisData` would pull), since
`spiceinit web=yes` covers the pointing/position role the larger, un-flagged `base` download would
otherwise be needed for.

Fetch helpers (`src/trntest/cache.py`) check "does this local mirrored path already exist" before
making any network request; if present, skip the request entirely.

## SPICE kernel selection (avoid over-pulling)

The NAIF archive's yearly metakernel is a **manifest**, not something to furnish/download in full —
CK (pointing) kernels dominate a year's data volume. Process:

1. Download (and cache) the one metakernel covering the year of our chosen EDR timestamp.
2. Parse it (it's a text kernel — `\begindata`/`\begintext` blocks listing `KERNELS_TO_LOAD`) to get
   the list of referenced kernel filenames.
3. Kernel filenames encode a date range (e.g. `..._2010074_2010080_...`) — pick out just the CK and
   SPK file(s) whose range covers our timestamp.
4. Always also fetch the small kernels that don't vary by date range within a mission phase: LSK,
   SCLK, PCK/lunar frame kernel, LRO FK, WAC IK.
5. Download only that selected set into `cache/naif/...` (mirrored paths), then `furnsh` just those.

## Observed result

For the demo's chosen 2019-11-30 timestamp, selecting only the `lrosc`/`lrolc` CK flavors (out of
five) for one 10-day chunk, plus the always-needed kernels, downloaded **~585 MB** total —
`spice_kernels.py`'s `select_kernels_for()` handles this. Note the single `lrosc` (spacecraft
bus reconstructed attitude) file for just that 10-day window is itself ~529 MB; the CK format
apparently samples at high angular rate. Skipping `lrodv`/`lrohg`/`lrosa` (still avoids ~4-5x more
CK volume) and never touching kernels outside the target date range is what keeps this tractable at
all — pulling a full year's CK data across all five flavors would be tens of GB.

## Lunaserv WMS caching

Cache `GetMap` responses keyed by `(layer, bbox, width, height, format)` — deterministic enough that
re-running any script/notebook cell for the same ROI hits the local cache, not the network.
