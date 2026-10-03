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
- **Raster edges meet at 0°, and overlap.** Central meridian 180° puts the raster's left edge at
  exactly 0° (x = -πR) and its right edge 109165 × 100 m later, ~94 m (almost a pixel) past 360°. An
  AOI straddling 0° needs both ends of the file; `reproject_astropedia_elevation_to_local_grid` reads
  the two as separate windows (in a CRS centered on the AOI) and warps each, rather than joining them
  into one array, which would misregister one side by that overlap.
- **±60° seam, in the file itself, full width.** GLD100 is assembled from parts that meet at ±60°:
  the typical (median) row-to-row elevation change jumps at the 60°S row (19 m vs. 3-5 m on nearby
  rows) and at 60°N (7 m vs. 2-4 m), with different texture on each side (row-to-row change 2-3 m
  equatorward of 60°N, 4 m poleward). There's no consistent vertical offset: the signed step's median
  is within ±12 m in every 30° longitude bin. 60°N also has a nodata row (row 5761, 40,383 of
  109,165 columns; 3,891 more in row 5760), and 60°S a short one (row 42151, 3,826 columns).
  `notebooks/dem_seams.ipynb` measures both on the local grid.
- **90° and 270° seams, between ±60°.** One-column lines exactly at 90° and 270° longitude, from
  about 59°S to 61°N and nowhere else. The mean column-to-column change across 270° is 1.5-2.4× its
  neighbors' in every 10° latitude band in that range; across 90° it is 0.7-1.6×, differing in
  every band but in both directions, so it nearly cancels over the full range. 0° (the raster edge),
  45°, 135°, 180°, 225° and 315° show nothing, nor do ±30° or ±45°. So the ±60° part looks
  assembled from a near-side and a far-side half meeting at 90°/270°.
- **Not a Cloud-Optimized GeoTIFF**: `gdalinfo` reports `Band 1 Block=109165x1` — row-strip internal
  layout (one TIFF strip per full-width row), not 2D-tiled. A remote windowed read via GDAL's
  `/vsicurl/` (HTTP range requests) therefore pulls full-width row strips for any AOI, not a small
  tile — confirmed empirically: one small AOI pull took ~64s. **Confirmed the same artifact-absence
  result on a real downloaded/reprojected AOI regardless** (the file's row-strip layout is a
  performance concern, not a data-quality one).
- **Caching**: `cache.fetch_astropedia_gld100` downloads and caches the **entire ~10 GB file locally
  once** (confirmed: final size 10,461,394,351 bytes), rather than repeated remote windowed reads —
  after which local windowed reads (`reproject_astropedia_elevation_to_local_grid`) are fast (no
  network, no row-strip-over-HTTP penalty). Resumable and locked per file (`cache.fetch_large_file`): `curl -fL -C - -o <stable .part path> <url>`
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
- **No row-level artifact confirmed.** ISIS `shadow`'s mask of this DEM shows horizontal single-row
  streaks at low sun, but no check has found anything in the elevation data that explains them: no
  elevation-domain detector (Hampel z-score, fold-and-stack against non-flagged control rows, raw
  column profiles) separates streak rows from ordinary terrain, and the real WAC image and
  `cast_shadow`'s sweep show nothing there either. See `docs/external-tools.md`'s "Terrain-shadow
  tools".
- **Low-sun caveat: 100 m posting smooths away small relief.** At low sun, shadow length is
  `height / tan(elevation)` — at ~13 deg, a 20 m obstruction the DEM can't resolve would cast a shadow
  nearly a pixel long. Any shadow model built on this DEM undercounts real shadow at low sun angles.
