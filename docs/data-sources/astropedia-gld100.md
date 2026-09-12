# Astropedia GLD100 flat file (live default DEM source)

Index: [`docs/data-sources.md`](../data-sources.md).

- URL: `https://planetarymaps.usgs.gov/mosaic/Lunar_LRO_WAC_GLD100_DTM_79S79N_100m_v1.1.tif`
  (`config.astropedia_gld100_url`). Hosted by USGS Astrogeology's Astropedia service — a static flat
  file, not a WMS/WCS/any dynamic-subsetting service (checked: USGS's own Astro WMS at
  `planetarymaps.usgs.gov/cgi-bin/mapserv` doesn't carry GLD100 at any resolution, only LOLA/Kaguya
  shaded relief; no WMS anywhere serves this file's data).
- **Confirmed specs, via live `gdalinfo` on the real file** (not taken from the filename/product page
  at face value): `Size is 109165, 47912`, `Pixel Size = (100.000000000000000,-100.000000000000000)`
  — genuinely **100.0 m/pixel**, not the 128 ppd/~237 m Lunaserv's own DTM layer actually serves.
  `Type=Int16` (integer meters, not float32 — real elevation values directly, `Min=-9091 Max=10761`,
  `NoData Value=-32768`) — **not planetocentric radius**, unlike Lunaserv's layer; no
  `radius_to_elevation`-style subtraction needed or performed for this path
  (`reproject_astropedia_elevation_to_local_grid` reprojects the elevation values as-is).
  Coverage confirmed via the same `gdalinfo` output's corner coordinates: 79°0'6.57"N to
  79°0'6.57"S — `dem_gld100.ASTROPEDIA_MAX_ABS_LATITUDE_DEG = 79.0` encodes this exactly, and
  `dem_gld100.astropedia_coverage_bbox_deg` raises rather than silently falling back to the deprecated
  Lunaserv path for any camera footprint that needs data outside it.
- CRS: a Moon-specific Equidistant Cylindrical ("Equirectangular") `PROJCRS`, standard parallel 0,
  central meridian 180° (`ELLIPSOID["Moon_localRadius",1737400,0,...]` — confirmed the real Moon
  radius, same check applied to Lunaserv's SRS codes). `reproject_astropedia_elevation_to_local_grid`
  reads this directly from the file's own embedded `crs`/`transform` (`rasterio.open(path).crs`) rather
  than hardcoding the PROJ4 parameters by hand — unlike Lunaserv's GetMap responses, this file's
  embedded georeferencing is trustworthy.
- **Not a Cloud-Optimized GeoTIFF**: `gdalinfo` reports `Band 1 Block=109165x1` — row-strip internal
  layout (one TIFF strip per full-width row), not 2D-tiled. A remote windowed read via GDAL's
  `/vsicurl/` (HTTP range requests) therefore pulls full-width row strips for any AOI, not a small
  tile — confirmed empirically: one small AOI pull took ~64s. **Confirmed the same artifact-absence
  result on a real downloaded/reprojected AOI regardless** (the file's row-strip layout is a
  performance concern, not a data-quality one).
- **Caching**: `cache.fetch_astropedia_gld100` downloads and caches the **entire ~10 GB file locally
  once** (confirmed: final size 10,461,394,351 bytes), rather than repeated remote windowed reads —
  after which local windowed reads (`reproject_astropedia_elevation_to_local_grid`) are fast (no
  network, no row-strip-over-HTTP penalty). Resumable: `curl -fL -C - -o <stable .part path> <url>`
  (not built on `cache.cached_get` — see that function's own docstring for why: `cached_get`'s
  per-call-unique-temp-filename and delete-on-failure behavior, both correct for small WMS tiles,
  actively defeat resume for one huge file). **Confirmed empirically, not just assumed from `curl`'s
  own docs**: interrupted a real download mid-transfer (killed the container at 931,119,104 bytes),
  reran, and `curl` logged `** Resuming transfer from byte position 931119104` — exact match, then
  completed the remaining ~8.87 GB.
- **Also checked and ruled out for now**: the finer 256 ppd/~118.45 m GLD100 tier (documented on
  Astropedia's own product page: `Pixel Resolution: 118.45058759 m/pixel`, `Scale: 256 ppd`) exists
  only as 8 quadrangle tiles covering just ±60° latitude — narrower coverage than this 100 m/px
  file's ±79°, for a resolution gain not otherwise validated as necessary. Not pursued; see
  `docs/proposed-tasks/open-items.md` for the >±79° polar case instead (a different, better real option
  exists there — LOLA-derived polar DEMs down to 5 m/px, via NASA's VIRA project).
- **Known DEM-precision follow-up, checked and cleared**: switching from Lunaserv's float32
  planetocentric-radius encoding (~0.125 m ULP, the reason `render.DEM_HEIGHT_ERROR_TOL_M = 0.5`
  exists — see `docs/external-tools.md`'s ASP `sat_sim` section) to this file's coarser Int16 (1 m
  step) encoding raised a real question of whether that same tolerance might now be too tight again,
  reintroducing `sat_sim` ray-intersection speckle. Checked directly: rendered the same real
  camera/DEM/ortho at `--dem-height-error-tol` of 0.5 (current default), 1.0, 2.0, and 4.0, measuring
  each render's isolated-single-pixel-outlier rate (`hapke.despeckle`'s own outlier test, used as a
  pure measurement here, not applied) — all four came out ~0.444-0.447%, no meaningful difference,
  unlike the original tolerance sweep this default came from (order-of-magnitude swings in both
  directions). No change needed to `DEM_HEIGHT_ERROR_TOL_M`.
- **Known minor artifact: faint row-level banding, real and upstream of this project.** Rendering a
  candidate's DEM as a hillshade under very low (single-digit-to-teens-degrees) sun elevation reveals
  faint, roughly-horizontal bands recurring at an irregular but statistically real ~40-50-row
  (~4-5 km) interval — found via `docs/proposed-tasks/isis-shadow-masking.md`'s cast-shadow spike,
  which flags every such band as a spurious lit/shadowed transition (see that doc for the full
  investigation, including two wrong hypotheses ruled out along the way: it's neither a
  `hole_fill_dem`/`dem_mosaic` artifact nor confined to real terrain like crater walls, both directly
  checked and eliminated). **Confirmed to originate in GLD100's own upstream production, not this
  project's fetch or reprojection**: the same banding (55 peaks across 2965 rows, median 45.5-row
  spacing) is present in the original NASA PDS-archived source tile
  (`WAC_GLD100_P900N0000_100M.IMG`, fetched fresh and independently from
  `https://pds.lroc.im-ldi.com/data/LRO-L-LROC-5-RDR-V1.0/LROLRC_2001/DATA/SDP/WAC_GLD100/`, 693.6 MB,
  18622x18622px Polar Stereographic, confirmed via its own embedded PDS3 label) — a file this project
  never fetches or touches, ruling out both Astropedia's own mosaicking of that tile (and 9 others)
  into this file's single flat GeoTIFF, and `reproject_astropedia_elevation_to_local_grid`'s own warp,
  as the source. Most likely mechanism: a residual seam between adjacent individually-adjusted WAC
  stereo models in GLD100's photogrammetric block-adjustment (`WAC_GLD100_README.TXT`: ~69,000 stereo
  models combined) — a physically plausible scale for a WAC stereo-swath boundary, though not
  independently confirmed as the exact mechanism. Amplitude is small (~0.1-1% relative brightness in a
  plain hillshade) and invisible at ordinary sun angles; only became visible/relevant because
  `shadow`'s hard lit/shadowed threshold amplifies it right at low sun elevation, exactly where the
  cast-shadow spike's own candidates live. Not currently worked around anywhere in this codebase —
  worth a look before trusting this DEM for anything sub-few-meter-precision at low sun angles.
