# SLDEM2015 (planned DEM source for ±60°)

Index: [`docs/data-sources.md`](../data-sources.md).

Available as `TrntestConfig.dem_source = "sldem2015_gld100"` (SLDEM2015 within ±60°, GLD100 beyond;
`dem_sources.SLDEM2015`), not yet the default; see
[`docs/proposed-tasks/vira-dem-sources.md`](../proposed-tasks/vira-dem-sources.md)'s Milestone 1.

- **What**: the LOLA team's lunar DEM from LOLA altimetry co-registered with Kaguya Terrain Camera
  stereo (Barker et al. 2015), 60°S to 60°N.
- **Which product**: the 512 ppd (~59.2 m) float tiles, SLDEM2015's finest posting. NASA's VIRA
  project (`scripts/download_dems.sh` in `github.com/nasa/vira`) uses the 256 ppd (~118 m) tiles
  instead; at this project's ~100 m DEM grid those would be upsampled, the 512 ppd ones downsampled.
  The same directories also hold 256 ppd tiles, whole-globe 128/256 ppd float files, and a 5.5 GB
  whole-globe 512 ppd JPEG2000 (`GLOBAL/JP2/`), which stores heights as integers × 0.5 m.
- **URLs**: tiles at `https://imbrium.mit.edu/DATA/SLDEM2015/TILES/FLOAT_IMG/<tile>.IMG` and `.LBL`
  (`config.sldem2015_base_url`). 32 tiles (`config.SLDEM2015_TILE_NAMES`),
  `SLDEM2015_512_{30N_60N,00N_30N,30S_00S,60S_30S}_{000_045,...,315_360}_FLOAT`. Data-quality map
  `SLDEM2015_DATA_QUALITY_FLOAT` at `.../GLOBAL/FLOAT_IMG/` (`config.sldem2015_quality_base_url`).
  Browse pages: `https://imbrium.mit.edu/BROWSE/SLDEM2015/TILES/`.
- **Caching**: `cache.fetch_sldem2015_tile` fetches a product's `.LBL` and `.IMG` whole, once, into
  `cache/sldem2015/` (`cache.fetch_large_file`: resumable, locked per file). Each tile `.IMG` is
  1,415,577,600 bytes; ~45 GB for all 32.
- **Format** (checked with `gdalinfo` against the `.LBL`s): PDS3, detached label; open the `.LBL`
  with GDAL. 23040 × 15360 px, float32, one tile per 45° × 30°. CRS is Equidistant Cylindrical,
  `lon_0=180`, `R=1737400`, the same as GLD100's. Pixel size 59.2252938 m, exactly 512 ppd at that
  radius, so the tiles abut exactly at every multiple of 45° and 30°, with no overlap (checked on all
  32). The 256 ppd tiles are exactly the 2×2 block average of these (0.00 m difference on a 1000²
  sample), so they add nothing.
- **No nodata** in any of the 32 tiles (GDAL assigns `-3.4028227e+38` as nodata, but no pixel has
  it). Heights range from -8.719 to +10.783 km.
- **Values are kilometers**, height above the 1737.4 km sphere. GDAL reports the label's
  `OFFSET = 1737.4`/`SCALING_FACTOR = 1` as band offset/scale but doesn't apply them on read. The
  offset converts height to radius, so for elevation in meters multiply the raw value by 1000 and
  ignore the offset.
- **Data-quality map**: 1 ppd, 360 × 120, meters, each pixel the RMS vertical residual between
  unbinned LOLA shots and the co-registered TC tile (Barker et al. 2015, Fig. 5 bottom); higher is
  worse. Median 3.5 m, 95th percentile 6.5 m, max 90 m. **It has no data for 30°N-60°N** (that whole
  band is nodata); 60°S-30°N is complete.
