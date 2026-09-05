# Crater depth & sharpness grading

Depth measurement (Breton et al. 2019 method) and the whole-database batch precompute built on it,
against the [Robbins crater database](data-sources/robbins-craters.md). **Purpose: the actual input
to grading crater sharpness**, not a standalone validation exercise — `ARC_IMG` (Robbins' own field)
isn't a real freshness proxy and that database has no degradation field at all, so this project needed
its own depth measurement to build one from. Breton et al.'s method was adopted specifically because
it's already validated in the literature, not derived from scratch.

## Method

- Source: Breton, S., Quantin-Nataf, C., Bodin, T., Loizeau, D., Volat, M., Lozac'h, L. (2019),
  *MethodsX* 6, 2293–2304, "Semi-Automated crater depth measurements", DOI
  `10.1016/j.mex.2019.08.007`. Depth = the 60th percentile of elevation in a ring around a crater's
  rim, minus the 3rd percentile of elevation inside it, from a DEM + crater shapefile. The authors'
  own reference implementation (not part of this repo) is a Tkinter GUI script doing manual
  per-pixel OGR polygon intersection over a lon/lat GDAL raster to get an area-weighted interior
  percentile — necessary there since degree-pixels don't all cover the same real ground area.
- `src/trntest/crater_depth.py` adapts this against this project's own Robbins ellipse
  polygons (`craters._ellipse_polygon`) and local, **isotropic-meters** DEM
  (`dem_ortho.fetch_dem_and_ortho`) instead: every "inside" pixel already covers the same real area
  on this grid, so the original's area-weighting machinery is dropped entirely (a provable no-op
  here, not an approximation), and `rasterio.features.geometry_mask` on a `pixel_size_m *
  sqrt(2) / 2`-buffered *real* ellipse polygon stands in for the original's manual per-pixel
  circle-distance test and OGR intersection calls.

## DEM source choice: deliberately GLD100

Not a genuinely global (pole-to-pole) alternative — two real candidates were found and set aside:

- `Lunar_LRO_LOLA_Global_LDEM_118m_Mar2014.tif` — confirmed live (`curl`, its real PDS3 label):
  hosted at the same `planetarymaps.usgs.gov/mosaic/` → S3 flat-file pattern GLD100 uses (302
  redirect to `asc-pds-services.s3.us-west-2.amazonaws.com`, HTTP Range-resumable), 256 ppd/
  118.450588 m/px, **90°N–90°S / -180–180° lon, genuinely global**, Int16 (`LSB_INTEGER`),
  `SIMPLE_CYLINDRICAL` projection, radius 1737.4 km, ~8.49 GB (confirmed via `curl -I`:
  8,494,203,833 bytes — smaller than GLD100 despite global coverage, since it's coarser).
- The ISIS global lunar shape model this project already caches for the real-WAC pipeline
  (`isis_wac.ensure_lunar_shape_model`, `base/dems/ldem_128ppd_Mar2011_clon180_radius_pad.cub`,
  ~2 GB, zero incremental download) — the older-vintage 128 ppd/~237 m/px LOLA product, also
  genuinely global (the standard "outside ±60°" polar-coverage product in the literature).
- **Both are real LOLA-gridded products with a confirmed real accuracy caveat GLD100 doesn't
  have**: LOLA ground-track cross-track gaps are ~1–2 km at the equator (up to 4 km), several
  pixels wide at either resolution above — the gridded product spline-interpolates across them
  (real value only along tracks), vs. GLD100's WAC-stereo-photogrammetry, which has actual
  relief data there. This directly matters for depth/sharpness grading: interpolation smooths
  exactly the small-scale rim relief a sharpness grade needs to detect, worst at low latitude for
  craters near this database's own `D≥1km` floor (closest in scale to the gap width). Not
  empirically checked here (e.g. via the same FFT/periodicity method that caught Lunaserv's own
  DTM striping artifact, see [`data-sources/lunaserv-wms.md`](data-sources/lunaserv-wms.md)) — a
  real follow-up if either global source is picked up later.
- **Decision (2026-08-23)**: stay on GLD100, and have `crater_depths_for_footprint` store a
  `None` depth (kept as a row, not dropped) for any crater whose own extent could reach past
  `dem_gld100.ASTROPEDIA_MAX_ABS_LATITUDE_DEG` (79.0), rather than adopt either global source now.
  A real, separate future step (precomputing depth for the whole non-polar Robbins database
  without any new DEM fetch) remains open, not blocked by this — GLD100 is already a single flat
  file cached locally once. If it's picked up: both candidate files share GLD100's own row-strip
  (not tiled) TIFF block layout, so a naive independent per-crater windowed read isn't
  necessarily fine at ~1.3M-crater scale — worth profiling (not guessing) a raster-row-ordered
  batch read or a one-time local re-tile (`gdal_translate -co TILED=YES`) before assuming either
  mitigation is actually needed.

## Whole-database batch precompute

`src/trntest/crater_depth_batch.py`: tiled, not a per-crater loop — a fixed lon/lat grid
(`DEFAULT_TILE_SIZE_DEG = 2.0`, tunable), each tile's DEM reprojected once and shared across every
crater it owns. A crater is *owned* by exactly one tile via its center point falling in that tile's
nominal (unpadded) bounds (`craters.query_craters_in_bbox`), but each tile's DEM actually covers a
larger, independently-tunable *padded* bbox (`DEFAULT_PADDED_TILE_SIZE_DEG = 3.0`), so a crater near
a tile edge still gets its full extent. A crater whose ellipse doesn't fit even the padded raster gets
`depth_m=None` — both tile sizes are fixed globals, a deliberate simplification since this
precompute's purpose (prioritizing sharper craters for a debug view) doesn't need the rare
oversized-crater miss fixed.

**Reading raw global GLD100 tiles directly needs a local reprojection first, not an optional
nicety**: a crater ellipse built directly in GLD100's raw global Equidistant Cylindrical CRS is
compressed east-west by `cos(latitude)` — measured ~0.87x/0.71x/0.50x/**0.20x** true extent at
30/45/60/78.5 deg latitude, severe enough near GLD100's own ±79 deg edge to badly misplace the
floor/rim masks. `crater_depth_batch.py` reprojects per tile for exactly this reason (via
`dem_gld100.reproject_astropedia_elevation_to_local_grid`); `crater_depths_for_footprint`'s own
per-camera path is unaffected (always a local Orthographic DEM, genuinely isotropic near its own
center regardless of latitude).

**A real correctness bug caught while building this**: `geo_utils.reproject_raster_to_local_grid`'s
raw output has no `nodata` tag set, even though real gaps are filled with literal `NaN` — invisible
in the per-camera pipeline because `fetch_dem` always runs the reprojected DEM through
`hole_fill_dem` first, which also sets the `nodata` tag; the batch tiler initially skipped that step
and would have silently leaked `NaN` into `crater_depth_m`'s percentile as real elevation. Fixed by
running every tile through `hole_fill_dem` before grading it.

**Real measured timing** (10 tiles sampled pole-to-pole): ~2.2s/tile fixed overhead (mostly the
`dem_mosaic` subprocess call) plus ~0.014s/crater marginal cost. Total for the whole grid (14,220
tiles, 1,250,659 craters within GLD100's ±79 deg coverage): **~13.6 hours single-threaded** — the
fixed per-tile overhead dominates despite the per-crater marginal cost being cheaper than a naive
per-crater loop's ~19.4ms (DEM access is shared within a tile, not reopened per crater).
`grade_database` (sequential) and `grade_database_via_workers` (a real `-k process` worker pool,
mirroring `trn_dataset.TrnTestDataSet.populate_via_workers`'s pattern via a generalized
`tasks.start_consumer(huey_module=...)` and this module's own `huey_crater_depth` instance) are
both implemented; live-validated end to end (6 tiles, 3 workers, ~1.7x wall-clock speedup,
resumability confirmed across the sequential/parallel boundary). Output: one small CSV per tile,
atomically published under `default_output_dir` (keyed in its own directory name by the tuning
parameters); `load_graded_database` concatenates them back for querying. Stores measured depth
only, not a sharpness grade — combining it with `crater_depth.stoffler_fresh_depth_km` into an
actual grade is left to the caller.

## Third-party dataset investigated and rejected as a shortcut

A third-party HuggingFace dataset, `huggingface.co/datasets/juliensimon/lunar-craters-robbins`,
claims to be this same Robbins database plus a pre-computed `depth_km` column. Direct inspection
found it isn't safe to trust: its `crater_id` values don't exist anywhere in this project's own
cached Robbins GeoPackage (no usable join key), and matching by position/diameter found no real
correspondence either — several of its "giant" (400-1,100 km) craters have no similar-sized real
crater anywhere near their claimed position in this project's own verified Robbins data, and one row
has an impossible value (`latitude_deg = -105.383`, `diameter_km = 3411.69`, bigger than the Moon
itself). The dataset card also doesn't explain how `depth_km` was derived. Not proof of malice, but
strong enough evidence of unreliability that this project's own from-scratch `crater_depth_batch.py`
pipeline (verifiable against real DEM data, not a black-box third-party column) was kept instead.
