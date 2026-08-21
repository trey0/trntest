# Data sources reference

Current, stable facts about the external data/formats this project depends on — endpoints, formats,
kernel layout, byte layouts, and known gotchas. Consult before writing new code against any of these
external systems; update this file (not just code comments) when a concrete choice changes. For *how*
these choices were reached (including wrong turns), see `docs/history.md`.

## Lunaserv WMS (visible imagery; DEM path deprecated)

**DEM fetching from this server is deprecated** — see "Astropedia GLD100 flat file" below for the
live default DEM source, and `docs/history.md`'s dated entry for the full investigation. Summary:
Lunaserv's DTM layer (`luna_wac_dtm_numeric_meters_absolute`) has a real, axis-aligned crosshatch
artifact baked into its own native tile (FFT-confirmed present regardless of requested ppd, CRS, or
resampling kernel) — not fixable client-side, since the server exposes no resampling control
(confirmed via several vendor `GetMap` parameter probes, all ignored) and no backing-store metadata.
`src/trntest/lunaserv.py`'s `fetch_dem_native`/`reproject_dem_to_local_grid` still implement the
native-CRS-fetch-plus-local-reprojection approach that fixed an *earlier*, different artifact from
this same server (see below) — kept for reference/comparison, no longer called by
`fetch_dem_and_ortho`'s default path. Everything below this deprecation note that's DTM-specific
(the local-CRS SRS discussion, the planetocentric-radius gotcha, the DTM layer list) describes that
deprecated path; the **ortho fetch** (`luna_wac_normalized_reflectance` et al., further down) is
unaffected and still current.

- Endpoint: `https://wms.im-ldi.com/lunaserv/lunaserv_stage?` (WMS 1.1.1). Run by ASU/LROC.
- `GetCapabilities`: `?request=GetCapabilities&service=WMS&version=1.1.1`
- Formats supported by GetMap include `image/tiff` and `image/tiff; mode=32bit` (float32 —
  needed for real elevation values, not a colorized/stretched render).
- SRS: `IAU2000:30100` returns a plain geographic (lon/lat, degrees) raster on a **sphere** of
  radius 1737400 m (GDAL reports it as an unprojected `GEOGCRS`) — the layers' native, unprojected
  grid. **No longer what `src/trntest/lunaserv.py` actually requests** (see the local-CRS entry
  below) but still useful as a plain lookup/degrees SRS if needed ad hoc.
- **`fetch_dem_and_ortho` requests the ortho in a per-camera local Orthographic CRS, not the native
  geographic grid** (still current — this is about the ortho fetch; the deprecated DEM path used the
  same CRS for the DEM too, see below): `IAU2000:30166,9001,{c_lon},{c_lat}` (`c_lon`/`c_lat` = that
  camera footprint's own center, filled in via `config.lunaserv_srs_template`). Confirmed via a live
  GetMap + `gdalinfo` check that this reports the Moon's real radius
  (`ELLIPSOID["unknown",1737400,0,...]`) and genuinely isotropic meter pixels (`Pixel Size = (500.0,
  -500.0)` for a 500 m/px test request) — unlike the generic OGC `AUTO:42003` Orthographic code,
  which is hardcoded to **Earth's** WGS84 ellipsoid (`ELLIPSOID["WGS 84",6378137,...]`) and would
  silently misplace every ground point by the Earth/Moon radius ratio (~3.67x) if used directly
  against lunar lon/lat. `IAU2000:30166` is one of a parametrized family Lunaserv exposes per
  body/projection — discovered by diffing `GetCapabilities`' `<SRS>` list around the known-working
  `IAU2000:30100`/`30101` entries (a parallel `301xx` block mirrors a `199xx` Mercury block one digit
  over, with placeholder `c_lon`/`c_lat`/`scale` tokens for the parametrized ones); `30162`/`30163`
  (`+scale`) are the matching lunar Stereographic variants, untried here.
  **Why the switch**: the native geographic grid's degree-pixels are anisotropic away from the
  equator (a degree of longitude covers less ground distance than a degree of latitude); ASP's
  `mapproject --ref-map` (see below) turned out not to preserve that anisotropy when reprojecting
  onto it, silently stretching the output. A local Orthographic CRS has square meter pixels
  everywhere, so that failure mode can't arise. See `docs/history.md`'s dated entry for the full
  investigation.
- **Gotcha (deprecated DEM path only):** `luna_wac_dtm_numeric_meters_absolute`'s pixel values are
  **planetocentric radius in meters** (~1.73-1.74 million), not height-above-datum —
  `lunaserv.radius_to_elevation` subtracted the reference radius (`MOON_RADIUS_M = 1737400.0`) before
  handing the DEM to ASP. Astropedia's GLD100 file (the live default DEM source) already serves real
  elevation directly — no equivalent subtraction needed or performed for that path.
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
  - `luna_wac_dtm_numeric_meters_absolute` — GLD100 elevation, actual meters. **Deprecated as a DEM
    source** (see this section's top note and "Astropedia GLD100 flat file" below) — confirmed
    served at a real, coarser-than-advertised ~128 ppd/~237 m ceiling regardless of requested
    resolution/CRS, with a further, unfixable-client-side axis-aligned artifact baked into the tile
    itself.
  - Surveyed every other DTM/DEM-ish layer this server advertises looking for a finer global
    alternative before giving up on Lunaserv entirely: `luna_nac_dtms`/`luna_pds_rdr_dtm` are vector
    *footprint-index* shapefiles (not raster DEM layers) pointing at scattered individual LROC NAC
    stereo DTMs — real, much higher resolution where they exist, but local/opportunistic coverage,
    incompatible with this project's catalog-driven, essentially-anywhere-on-the-Moon image
    selection. Per-Apollo-site DTMs/NAC mosaics (one even advertised at 50 cm/px) have the same
    coverage problem, just smaller still. No global layer here is finer than what's already deprecated
    above — the problem was never *which* Lunaserv layer, it was that no Lunaserv layer at this
    resolution exists for arbitrary lunar coverage; see the Astropedia section below for what does.
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

## Astropedia GLD100 flat file (live default DEM source)

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
  79°0'6.57"S — `lunaserv.ASTROPEDIA_MAX_ABS_LATITUDE_DEG = 79.0` encodes this exactly, and
  `lunaserv.astropedia_coverage_bbox_deg` raises rather than silently falling back to the deprecated
  Lunaserv path for any camera footprint that needs data outside it.
- CRS: a Moon-specific Equidistant Cylindrical ("Equirectangular") `PROJCRS`, standard parallel 0,
  central meridian 180° (`ELLIPSOID["Moon_localRadius",1737400,0,...]` — confirmed the real Moon
  radius, same check applied to Lunaserv's SRS codes above). `reproject_astropedia_elevation_to_local_grid`
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
  completed the remaining ~8.87 GB. See `docs/environment.md`/`docs/caching.md` for the cache-footprint
  size tradeoff this introduces for the ephemeral-VPS archive/restore workflow.
- **Also checked and ruled out for now**: the finer 256 ppd/~118.45 m GLD100 tier (documented on
  Astropedia's own product page: `Pixel Resolution: 118.45058759 m/pixel`, `Scale: 256 ppd`) exists
  only as 8 quadrangle tiles covering just ±60° latitude — narrower coverage than this 100 m/px
  file's ±79°, for a resolution gain not otherwise validated as necessary. Not pursued; see
  `docs/plan.md`'s open items for the >±79° polar case instead (a different, better real option
  exists there — LOLA-derived polar DEMs down to 5 m/px, via NASA's VIRA project).
- **Known DEM-precision follow-up, checked and cleared**: switching from Lunaserv's float32
  planetocentric-radius encoding (~0.125 m ULP, the reason `render.DEM_HEIGHT_ERROR_TOL_M = 0.5`
  exists — see the `ASP sat_sim` section below) to this file's coarser Int16 (1 m step) encoding
  raised a real question of whether that same tolerance might now be too tight again, reintroducing
  `sat_sim` ray-intersection speckle. Checked directly: rendered the same real camera/DEM/ortho at
  `--dem-height-error-tol` of 0.5 (current default), 1.0, 2.0, and 4.0, measuring each render's
  isolated-single-pixel-outlier rate (`lunaserv.despeckle`'s own outlier test, used as a pure
  measurement here, not applied) — all four came out ~0.444-0.447%, no meaningful difference, unlike
  Phase 15's original tolerance sweep (order-of-magnitude swings in both directions). No change
  needed to `DEM_HEIGHT_ERROR_TOL_M`.

## Robbins lunar crater database (planned vector overlay data)

- Source: Robbins, S.J. (2019), *JGR Planets*, "A New Global Database of Lunar Impact Craters
  >1–2 km", DOI `10.1029/2018JE005592`. Distributed by USGS Astropedia/PDS Cartography and Imaging
  Sciences Node Annex. URL (`config.robbins_craters_url`, `cache.fetch_robbins_craters`):
  ```
  https://astrogeology.usgs.gov/ckan/dataset/f89f5478-b69a-486c-b9b5-30d7b0c5ad2b/resource/c4f25cc2-4f8a-4207-a845-5e176da3ac5a/download/lunar_crater_database_robbins_2018
  ```
  — a CKAN resource-download route, not the catalog/search page a browser lands on. **Found by a
  human navigating the current live catalog page's own download link** (`search/map/
  moon_crater_database_v1_robbins`), not derived automatically: every specific catalog/download URL
  findable via search engines or a third-party library's own dated docs (SONIC, which cites this
  exact catalog page as of Oct 2024) 404s live on `astrogeology.usgs.gov` as of this writing — a
  real USGS-side site reorganization since then, confirmed unrelated to Cloudflare bot-protection
  (plain scripted requests with a browser User-Agent work fine against real files on this same
  host, e.g. this exact URL and other CKAN resource downloads — the *search/catalog* pages are what
  broke, not file serving).
- **Confirmed live** (`curl -I`): `200`, `Content-Type: application/zip`, `Content-Length:
  96227201` (~92 MB) — small enough for `cache.fetch_robbins_craters`'s plain `cache.cached_get`
  path, not `fetch_astropedia_gld100`'s special resumable-curl treatment (that exists specifically
  for GLD100's ~10 GB).
- **Format, confirmed by downloading and inspecting the real file** (not assumed from the paper or
  any third-party docs): a PDS4 bundle (zip), whose only actual data file is a single CSV —
  `.../data/lunar_crater_database_robbins_2018.csv`, 238,483,500 bytes uncompressed,
  **1,296,796 rows** (exactly matches the paper's "~1.3M craters with D≥1km" — this is the
  size-filtered subset, not the full ~2.03M-crater set). **No shapefile/GeoPackage/any native
  vector-geometry format is shipped** — confirms the plan's hypothesis that ellipse polygons must be
  constructed at render time from plain attribute columns, not read off the shelf. Header row:
  ```
  CRATER_ID,LAT_CIRC_IMG,LON_CIRC_IMG,LAT_ELLI_IMG,LON_ELLI_IMG,DIAM_CIRC_IMG,DIAM_CIRC_SD_IMG,
  DIAM_ELLI_MAJOR_IMG,DIAM_ELLI_MINOR_IMG,DIAM_ELLI_ECCEN_IMG,DIAM_ELLI_ELLIP_IMG,
  DIAM_ELLI_ANGLE_IMG,LAT_ELLI_SD_IMG,LON_ELLI_SD_IMG,DIAM_ELLI_MAJOR_SD_IMG,
  DIAM_ELLI_MINOR_SD_IMG,DIAM_ELLI_ANGLE_SD_IMG,DIAM_ELLI_ECCEN_SD_IMG,DIAM_ELLI_ELLIP_SD_IMG,
  ARC_IMG,PTS_RIM_IMG
  ```
  (matches the companion Mars Robbins database's own field-naming convention exactly — this wasn't
  guessed, both the header and the values below were read from the real downloaded file.)
- **Units, confirmed empirically** (the PDS4 label declares field names/types but not per-field
  units for the diameter columns — checked directly against the real data rather than assumed):
  `DIAM_CIRC_IMG` ranges **1.0 to 2491.87 across all 1,296,796 rows** — kilometers, not meters
  (1.0 km min exactly matches this file's own D≥1km filter; 2491.87 km max matches South
  Pole–Aitken basin's real ~2500 km diameter). `LAT_CIRC_IMG`/`LON_CIRC_IMG` (and the `_ELLI_`
  equivalents) are plain decimal degrees, **Planetocentric latitude, Positive-East longitude**, on
  spheroid `Moon_2000` — semi-major = semi-minor = polar radius = **1,737,400 m** (per the PDS4
  label's `cart:Geodetic_Model`), the exact same sphere radius this project's own
  `config.moon_radius_m`/`+proj=longlat +R={moon_radius_m}` convention already uses everywhere in
  `lunaserv.py` — no CRS reconciliation needed beyond the usual lon/lat→local-Orthographic
  reprojection every other vector/raster layer here already goes through.
  `DIAM_ELLI_MAJOR_IMG`/`DIAM_ELLI_MINOR_IMG` are the same km convention; `DIAM_ELLI_ANGLE_IMG` is
  decimal degrees (values like `35.99`, `127.00` seen in real rows — not radians, despite SONIC's
  own internal field naming (`angElp_RAD`) implying otherwise; SONIC evidently converts on load,
  the raw PDS4 CSV does not).
- Geometry is genuinely POINT-only (`LAT_CIRC_IMG`/`LON_CIRC_IMG` center, or the separate
  `LAT_ELLI_IMG`/`LON_ELLI_IMG` ellipse-fit center) — ellipse polygons need to be constructed at
  render time from `DIAM_ELLI_MAJOR_IMG`/`DIAM_ELLI_MINOR_IMG`/`DIAM_ELLI_ANGLE_IMG`, e.g. via
  `shapely.affinity.scale`/`.rotate`/`.translate` on a unit circle centered at the ellipse-fit
  center, in the same local-meters CRS the rest of `plotting.py`'s overlay drawing already uses.
  Since the CSV has no native spatial index at all, `geopandas.read_file(..., bbox=...)`'s
  read-time pushdown (see `docs/plan.md`'s open items for why this matters at ~1.3M rows) requires
  first converting to an indexed format (GeoPackage/FlatGeobuf) once — there's no shipped index to
  reuse, unlike a real GIS format might have.

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
  Fed in the per-camera local Orthographic projection (the same grid the ortho shares — see
  `fetch_dem_and_ortho`), not either DEM source's own native projection — both the deprecated
  Lunaserv-native path and the live Astropedia path reproject locally before `sat_sim` ever sees the
  DEM; no evidence `sat_sim` demands a local stereographic projection instead.
- **`sat_sim` applies no illumination/shading model of its own.** Per its own docs, it
  "unproject[s] an ortho image into a given camera... in the spirit of ISIS `map2cam`," generating
  output pixels via bicubic interpolation of the `--ortho` input. The DEM is used purely for
  ray/terrain-intersection *geometry* (which ground point a given camera ray hits) — the output
  pixel value is a direct geometric resample of whatever's already in the ortho, with no per-ray
  reflectance/sun-angle computation applied. Any relief/shading visible in a render is therefore
  whatever was already baked into the ortho texture, not something `sat_sim` computes — see
  "Lunaserv WMS" below for how this project supplies that shading (`lunaserv.despeckle_and_shade_ortho`
  -- a real Hapke BRDF via ISIS `photomet` by default, `hapke_shade_ortho`, with a plain Lambertian
  `shade_ortho` fallback; both lit with real SPICE sun geometry, not relying on any shading baked
  into the source imagery, which was never guaranteed to match the simulated frame's real sun angle
  in the first place -- see `docs/history.md`'s dated entries).
- **`--dem-height-error-tol`'s default (0.001m) is too tight for this project's DEM and causes
  visible salt-and-pepper speckle** in the render (`sat_sim`'s ray/DEM-intersection root-finder
  misbehaves at scattered pixels). Root cause: Lunaserv's DTM layer serves planetocentric radius
  (~1.7e6 m) as float32, whose ULP (smallest representable step) at that magnitude is already
  ~0.125m — baked into the source data itself, not something fixable in
  `lunaserv.radius_to_elevation`'s own subtraction. **Confirmed empirically** (see `docs/history.md`
  Phase 15): tightening the tolerance further makes the speckle dramatically worse (more, denser
  artifacts), loosening it to comfortably clear that ~0.125m floor eliminates it cleanly — neither
  outcome is subtle. `src/trntest/render.py`'s `DEM_HEIGHT_ERROR_TOL_M = 0.5` (a 4x margin above the
  float32 floor) is what `run_sat_sim` actually passes. Derived against Lunaserv's float32 data, but
  re-checked (not just assumed still valid) after switching the live default DEM source to
  Astropedia's coarser Int16 encoding — see "Astropedia GLD100 flat file"'s own precision note above;
  no change needed. Two other theories were tested and ruled
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
  `lunaserv.py` outputs, e.g. `DemOrthoResult.ortho`), with no separate reprojection/alignment step
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
  array gimbal). Of these, **only `lrolc` is actually needed for WAC pointing via plain SPICE**
  (`spice.pxform`/`spice.spkezr`) — see the frame-chain note below; `lrosc` is furnished by the
  deprecated NAIF-metakernel CK-selection path anyway (harmless, just unused for this purpose).
  `lrodv`/`lrohg`/`lrosa` are skipped entirely regardless, which cuts CK downloads roughly 5x for a
  given day.
- **Live default WAC CK source is not this metakernel path at all.** `spice_kernels.
  select_isis_wac_ck_kernels` (`TrntestConfig.wac_ck_source = "isis_resolved"`, the default) instead
  asks a real ISIS `spiceinit web=yes` run what it furnishes (`isis_wac.resolve_wac_ck_kernels`),
  which draws from an entirely different host — see "ISIS's own LRO kernel database (USGS S3, not
  NAIF)" below. `select_naif_wac_ck_kernels` (this metakernel-manifest approach) is kept, deprecated,
  as `wac_ck_source = "naif_metakernel"` — confirmed numerically equivalent (see that section).
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
  - **Confirmed (Phase 27) that this is a *direct* segment, not a runtime-composed delta**:
    `spice.ckobj` on a real `lrolc_*.bc` file lists `-85620` (plus `-85610`/`-85600`) as objects it
    stores segments for directly — the file bakes in `-85620`'s full orientation already, it doesn't
    need the bus (`-85000`, via `lrosc`/`moc42r`) at runtime to compose one. Verified decisively:
    furnishing only a bus CK (`moc42r` or `lrosc`) with `lrolc` *not* loaded makes
    `spice.pxform('LRO_LROCWAC_VIS', 'MOON_ME', et)` fail outright with `SPICE(NOFRAMECONNECT)` — if
    SPICE needed to chain through the bus at runtime, that call would have succeeded using bus data
    alone. Practical upshot: **plain SPICE frame resolution for WAC pointing is entirely determined
    by whichever `lrolc`-flavor file is loaded; a second, bus-only CK (`lrosc` or `moc42r`) makes zero
    difference to it** — see "ISIS's own LRO kernel database" below for why this matters to the
    now-corrected "missing `moc42r`" diagnosis.
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

## LRO maneuver detection (for TRN-OD dataset selection)

For TRN-based orbit-determination testing (image-matching as the OD input), a propulsive maneuver
between two dataset images corrupts the OD solve — but there's no known public source for LRO's
flight-dynamics team's own maneuver log (a "small forces file"/SFF equivalent). Two things fill that
gap:

- **Literature: Mesarch, "Long-Term Orbit Operations for the Lunar Reconnaissance Orbiter,"
  AAS-23-234 (2023)**, NTRS
  [20230010952](https://ntrs.nasa.gov/api/citations/20230010952/downloads/Mesarch_LROLongTermOrbit_Preprint_20230727.pdf).
  Key facts from it:
  - LRO's orbit has had **zero stationkeeping maneuvers of any kind since SK34 on 2015-05-04** —
    the mission stopped maintaining a frozen orbit in 2016 and has let it drift ever since (Figure
    19 explicitly labels 2016–2023 "No Maintenance"). Any date from 2016 onward is free of
    stationkeeping/frozen-orbit-reset burns by construction.
  - Table 2 lists every dedicated Eclipse Phasing Maneuver (EPM) date; there's a gap from
    2019-06-24 to 2021-05-03 — no EPM anywhere in H2 2019.
  - The only maneuver type that can still occur in the post-2016 "drift" era is a reaction-wheel
    momentum unload: small (Figure 15: ~0.05–0.3 m/s), every 2-4 weeks, ~302 total since launch.
    Three special phasing maneuvers (Chandrayaan/LCROSS/GRAIL coordination) and the three Frozen
    Orbit Reset burns (2013-04-29, 2014-04-03, 2015-05-04, each 2.7-5.7 m/s) are all pre-2016 and
    don't recur.
  - Real stationkeeping burns (pre-2016) were ~5.5 m/s each, in posigrade/retrograde pairs ~3 hours
    apart, roughly every 28 days.
- **`maneuver_detection.py`**: momentum unloads, though small, turn out to be directly detectable
  in LRO's own public reconstructed-orbit SPK — no SFF needed. Method: sample state vectors (r, v)
  at fine, uniform cadence, compute specific angular momentum `h = r x v` and specific orbital
  energy `eps = v^2/2 - GM/r`, and flag persistent step changes across all four channels jointly
  (quadrature sum of each channel's own MAD-normalized z-score) — a real burn shifts them to a new
  baseline and stays there, unlike gravity-driven periodic oscillation. Chosen over the classical
  elements (a, e, i) an earlier version of this tool used: `Delta h = r x Delta v` is *exact* (not a
  linearized rate) and has a clean, phase-INDEPENDENT null only on the radial impulse component —
  unlike inclination alone, whose Gauss-equation sensitivity is modulated by
  `cos(argument of latitude)` and vanishes at node crossings, which a momentum unload has no reason
  to avoid. This mattered concretely: Mesarch et al. note momentum unloads were flown "in the +/-
  orbit normal direction to minimize the along-track perturbative effects" early in the mission —
  i.e. designed to be invisible to a semi-major-axis-only check. See the module's own docstring for
  the full derivation, including the one honest residual gap it still has (a purely-radial impulse,
  weakly observable across LRO's whole near-circular orbit, not just near apsis — detection still
  works, but the reconstructed radial component isn't trusted, via an explicit `rcond` cutoff on the
  impulse-reconstruction least-squares solve).

  Validated against the literature above: run over H2 2019 (encompasses this repo's fixture EDR,
  `M1329714703CE`, 2019-11-30), it finds 11 candidates, 11–30 days apart, matching the paper's
  momentum-unload cadence almost exactly, in a window the paper independently confirms had no EPM or
  stationkeeping burn (`combined_z` stays under ~20 for all of them). **Notably, several are
  normal-direction-dominant, up to ~2.1 m/s total** — several times larger than the ~0.07–0.25 m/s
  a semi-major-axis-only estimate reports for the same dates, since that estimate is blind to
  exactly the component driving them. Cross-checked against a short 2010 window (pre-frozen-orbit):
  real stationkeeping pairs there are unmistakable (`combined_z` in the hundreds, ~5.2–5.6 m/s,
  tangential-dominant, alternating sign, ~2h38m apart — matching the paper's "~3 hours" and 2-burn
  posigrade/retrograde description almost exactly), cleanly separated from momentum-unload-scale
  candidates in the same window. Wired into `dataset_selection.add_maneuver_flags` (flags a whole
  orbit-level table at once) — not into `dataset.images_for_window()`'s own per-candidate filtering;
  also usable standalone (`find_maneuver_candidates(start_dt, end_dt, config)`) for vetting a
  candidate date range by hand.

## ISIS's own LRO kernel database (USGS S3, not NAIF)

`spice_kernels.py`'s NAIF-metakernel-based CK selection (above) isn't the only source of truth for
which kernels apply to a given LRO product/date. ISIS3 resolves kernels via a completely separate
mechanism, confirmed live (Phase 27) by reading ISIS's own config files inside the Docker image and
directly querying the real bucket:

- **`spiceinit web=yes`** calls USGS's own ALE-based SPICE web service, found via
  `/opt/conda/envs/isis/bin/xml/spiceinit.xml`'s `URL` parameter default:
  `https://astrogeology.usgs.gov/apis/ale/v0.9.1/spiceserver/` (the `v0.9.1/spiceserver` path names
  the backend explicitly — ALE, USGS's own Python "Abstraction Layer for Ephemerides" library). This
  web service — and local, non-`web` `spiceinit` — both resolve kernels via the *same* mechanism:
  `kernels.*.db` PVL index files (ISIS's `kerneldbgen` app format: `Object = SpacecraftPointing`
  containing many `Group = Selection` entries, each a `Time = (start, stop)` range + `File` +
  `Type`).
- **These `.db` files, and the kernels they reference, are not NAIF-hosted for LRO.**
  `/opt/conda/envs/isis/etc/isis/rclone.conf`'s `[lro]` alias is `remote =
  asc_s3:asc-isisdata/usgs_data/lro/` — a plain alias to USGS's own public AWS S3 bucket
  (`asc-isisdata`, `us-west-2`), with **no** `naif:` union (unlike `[dawn]`/`[cassini]`/`[tgo]`,
  which explicitly union their own USGS data with a `naif:` remote). The whole bucket is
  unauthenticated/anonymously readable over plain HTTPS
  (`https://asc-isisdata.s3.us-west-2.amazonaws.com/usgs_data/lro/...`, including S3's own
  `?list-type=2&prefix=...` listing API).
- **`kernels.0001.conf`** (`kernels/ck/kernels.0001.conf` in that bucket) routes each instrument to
  which `.db` file(s) to consult — confirmed live: `WAC-VIS`/`WAC-UV` both route to *two* sources,
  `kernels/ck/moc_kernels.????.db` (bus attitude — resolves to `moc42r_*.bc`, a real,
  ~1.7GB-per-30-day-merge product that exists **only** in this bucket, absent from every NAIF-hosted
  path checked: neither the yearly metakernel's own `data/ck/` nor NAIF's separate operational
  mirror at `naif.jpl.nasa.gov/pub/naif/LRO/kernels/ck/`) and `kernels/ck/lroc_kernels.????.db`
  (presumably the `lrolc`-equivalent role — confirmed live to currently have **zero** matching files
  in the bucket, a real gap; see below for why this doesn't actually block anything).
- **`moc42r` is not more accurate than NAIF's `lrosc`/`lrolc`** — both are tagged `Type =
  Reconstructed` in `kerneldbgen`'s own vocabulary, which has a distinctly higher `Smithed` tier for
  genuinely photogrammetric/bundle-adjustment-refined products, never used for either. NAIF's own
  `ckinfo.txt` documents `lrosc` as itself a merge of daily `moc42_*.bc` files "produced by the LRO
  project during operations" — `moc42r` is USGS's own independent ~30-day merge of that *same*
  underlying daily series, not a different/better source. Confirmed the bucket periodically
  re-merges: `moc42r_2019304_2019334_v01.bc` (uploaded 2022-08-12) and a newer, differently-dated
  `moc42r_2019334_2020001_v01.bc` both exist for overlapping coverage of the same period — a real,
  live demonstration that hardcoding a filename found in one session can go stale by the next.

**How this project uses it**: rather than reimplementing the `.conf`/`.db` selection algorithm in
Python (the `lroc_kernels.db` gap above means that reimplementation would have a real, hard-to-detect
hole — it would silently never furnish an `lrolc`-equivalent kernel at all, even though ISIS's own
live resolution clearly does furnish one from somewhere), `isis_wac.resolve_wac_ck_kernels` asks a
real `spiceinit web=yes` run directly: runs the existing `ensure_isisdata → fetch_edr_img →
run_lrowac2isis → run_spiceinit` pipeline against this project's one fixed reference EDR product,
then reads the resulting cube's `Group = Kernels` label (via ISIS's `catlab` app, parsed with the
`pvl` library — the format's genuine nested/duplicate-key structure isn't cleanly regex-able the way
the flat NAIF metakernel manifest is). Confirmed live: the label's `InstrumentPointing` field lists
`(Table, $lro/kernels/ck/lrolc_2019334_2020001_v01.bc,
$lro/kernels/ck/moc42r_2019334_2020001_v01.bc, $lro/kernels/fk/lro_frames_2014049_v01.tf)` — both
kernels together, resolving the apparent `lroc_kernels.db` gap (ISIS's live resolution clearly finds
an `lrolc`-equivalent file some other way than that specific route) without this project needing to
know *how*. Result persisted to `cache/isis_ck_resolution/<edr_product>.json`, checked before ever
calling `spiceinit` again — the live web service is only hit once per distinct `edr_product`, not
once per requested date (`select_isis_wac_ck_kernels` filters the persisted result's own
filename-encoded date range against whatever date is actually requested, falling back to the
deprecated NAIF path for dates outside that one product's own resolution window — see
`spice_kernels.select_kernels_for`'s docstring).

**The kernel this mechanism adds turns out to be inert for plain SPICE calls, and the original bug it
was built to fix isn't reproducible.** `spice.ckobj` on a real `lrolc_*.bc` file shows it stores
segments **directly** under `-85620` (not `-85000`) — `spice.pxform('LRO_LROCWAC_VIS', 'MOON_ME',
et)` gives byte-identical output whether `lrosc` or `moc42r` (bus-only, `-85000`) is the
co-furnished kernel, and fails outright with `SPICE(NOFRAMECONNECT)` if `lrolc` is omitted even with
a bus CK present. Direct comparison against real `campt` output (`SpacecraftPosition`,
`LookDirectionCamera`/`LookDirectionBodyFixed`) at four independent points spread across a real
cube — transforming `campt`'s own reported camera-space look vector through *our* `pxform`-derived
rotation and comparing against `campt`'s own body-fixed result — found **zero** measurable
discrepancy (sub-centimeter position, 0.000000° pointing) at every point, with or without `moc42r`
furnished. The originally-reported ~11-13km discrepancy this mechanism was built to fix is not
reproducible; its true cause was never identified (most likely conflated with the separate, also-real
`cam2map` `WARPALGORITHM` striping bug found in the same original investigation — see "ISIS3/CSM
spike" below). Kept as the live default anyway (`TrntestConfig.wac_ck_source = "isis_resolved"`) for
independent reasons: it makes this project's furnished kernel set match ISIS's own real-world
resolution by construction, which is more principled and immune to future NAIF/USGS drift than a
hand-picked prefix list — not because it's fixing a currently-known bug. See `docs/history.md`'s
Phase 27 for the full investigation.

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

The live default image comes from the checked-in, now-frozen `notebooks/dataset_manifest.csv` — a
real, catalog-driven multi-orbit search's result (see `docs/plan.md`), not any single hardcoded
product. Two specific products remain useful as known
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
  **Boresight direction is re-aimed, not trusted from raw SPICE**: `spice.pxform`'s `[0,0,1]` in the
  `LRO_LROCWAC_VIS` frame is confirmed *not* WAC-VIS's real optical boresight (see "WAC-VIS's real
  boresight isn't `spice.pxform`'s `[0,0,1]`" below) — `build_camera()` instead runs the real WAC
  pipeline and queries ISIS's own camera model (`isis_wac.ground_point_at_pixel`) for the real
  ground point at the crop's true center pixel, then points the boresight there directly
  (`camera.look_at_rotation`). Camera *position* is untouched — confirmed exactly correct.
- **Comparison-figure aspect ratio**: both panels are plotted with `extent=` in real km (not raw
  pixel index), since the CDR crop's pixel array isn't square even though the ground area it covers
  is.
- **Tie points** (`tie_points.py`): 5 points (a die's "5"/X pattern: 4 corners + center) placed in
  the ground area both images *approximately* share (SPICE-only estimate, used only to pick
  plausible candidate points), projected into each image's real pixel coordinates. Synthetic side:
  closed-form pinhole inverse (exact, single fixed pose — `select_tie_points`). Real WAC crop side:
  a genuine ISIS `campt` ground-to-image query (`resolve_crop_pixels`, via
  `isis_wac.resolve_ground_to_image_model`/`ground_to_image_pixel`) against the crop's own real,
  embedded camera model — not the deprecated frame-index-bisection SPICE approximation
  (`project_ground_to_crop_pixel`/`_crop_pixel_at_frame`, kept for reference). Switched after
  confirming live, on this project's real default candidate, that the SPICE approximation disagreed
  with the real camera model by ~92-96px (out of 994 total lines, ~10%, along-track) — see
  `docs/history.md`'s dated entry. `resolve_ground_to_image_model` tries a CSM ISD sidecar first
  (`isd_generate`, same tool 5B's `mapproject` uses) and only falls back to the crop's native model
  if the ISD's own `name_model` resolves to a Pushframe sensor — the class `usgscsm`'s
  `groundToImage` is known unreliable for (see below); for WAC-VIS this always takes that branch,
  but the check is real, not hardcoded. A die5 point the real camera doesn't actually see (confirmed
  live: happens for real near-polar candidates, since the SPICE-approximate footprint used for point
  *selection* can be off by enough to pick a point outside the real camera's view) is dropped with a
  warning, not raised — `resolve_crop_pixels` only raises if *none* of the 5 points resolve.
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
  **Deliberately not pixel-grid-aligned** to `DemOrthoResult`'s own raster (no `UpperLeftCornerX`/
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
  **Fix (partial — see caveat below)**: explicit `WARPALGORITHM=forwardpatch PATCHSIZE=1` — verified
  coverage went from ~47% to ~71% (matching the crop's real footprint, no more gaps), same as any
  `PATCHSIZE` from 1-4. An earlier version of this fix used `PATCHSIZE=4`, verified only by aggregate
  crop-vs-full-cube correlation (0.9954 vs. 0.9999986 at the broken default) and judged good enough —
  that correlation check missed a second, real problem the same way the original coverage check did:
  a visible striping artifact, confirmed via a direct `PATCHSIZE` sweep (1/2/4/8/14 at native
  resolution) to get markedly worse at 8/14, with `PATCHSIZE=1` a real, visible improvement over the
  `4` this pipeline used before — aggregate correlation is dominated by the much larger unaffected
  bulk of the image, not the boundary pixels where a structured artifact like this actually
  concentrates. **Not a complete fix**: a high-pass (Gaussian-blur-subtracted) comparison found only
  a modest ~2.4% reduction in fine-scale energy between `PATCHSIZE=1` and the old `PATCHSIZE=4`, and
  a faint residual remains visible on close inspection at `PATCHSIZE=1` — direct user visual
  confirmation matches this: consistent with genuine, modest photometric discontinuities at framelet
  transitions (inherent to any patch-based warp), not the more severe missing/bad-data-looking
  pattern `PATCHSIZE=4` showed. Diminishing returns past this point — not pursued further.
  `PATCHSIZE=1` costs real runtime (~16s vs. ~10s for one crop) but no coverage trade-off (71.39% vs.
  71.38%, essentially identical).
- **Position residual — real at the time, since found to not be reproducible (see "ISIS's own LRO
  kernel database" below for the full Phase 27 follow-up)**. Even after the fixes above, the crop's
  designated center pixel (checked directly via `campt`, not just an aggregate valid-pixel centroid)
  appeared to image ground ~11km from `crop_footprint`'s independently ray-traced center, despite
  both using the exact same frame-index formula. Ruled out a frame-selection bug first:
  reconstructed ISIS's own per-line time formula from three exact frame-boundary `campt` queries and
  confirmed `crop_window_for_camera` selects exactly the intended chronological frame range,
  correctly reflecting `framestitch`'s line-order reversal for this product's `flip=True` — ISIS's
  per-line time matches `camera.frame_et()` to within 0.016s for the corresponding frames. At the
  time, this was attributed to a missing second CK kernel (`moc42r_2019304_2019335_v01.bc`) that
  ISIS's `spiceinit web=yes` furnishes but `spice_kernels.py` didn't fetch. **A later session (Phase
  27) built the fix for that and then, via direct re-verification against real `campt` output,
  found the ~11km discrepancy isn't reproducible at all** — with or without the extra kernel. See
  "ISIS's own LRO kernel database (USGS S3, not NAIF)" below for the full corrected story; the true
  cause of this original number was never pinned down, most plausibly conflated with the
  `WARPALGORITHM` striping bug immediately above (both were being chased in the same investigation).
- **Also tried and ruled out**: ASP `mapproject -t isis` (uses ISIS's own native sensor model
  instead of `usgscsm`/CSM, given a plain `.cub` with no separate ISD sidecar) as a possible
  simpler alternative to the hand-written PVL + `cam2map` approach above. Tested directly against
  the same crop cube — immediately rejected: `"ERROR: Unusual input file... Seems to have Isis
  camera type 1. Check your data. Maybe it will work with CSM."` ASP's own ISIS session wrapper
  doesn't support this camera type (Pushframe) at all, not a flag/workaround issue. `cam2map`
  remains the only working native-ISIS reprojection path found for this sensor.

## WAC-VIS's real boresight isn't `spice.pxform`'s `[0,0,1]`

**The finding**: `camera.camera_pose_moon_me()`'s attitude (`spice.pxform("LRO_LROCWAC_VIS",
"MOON_ME", et)`) is exactly correct — confirmed via a Wahba/Kabsch rotation fit from real `campt`
`LookDirectionCamera`/`LookDirectionBodyFixed` correspondences reproducing it to 0.0000° (including
on a held-out point), and via SPICE position matching ISIS's own real position to 0.6m at the
matching instant. But treating `[0,0,1]` in that frame as "the boresight" is measurably wrong for
WAC-VIS specifically: `LookDirectionCamera` at the naively-assumed center pixel (image cross-track
center, mid-framelet) isn't `[0,0,1]` — off by a roughly constant ~5-6° (5.75° and 5.15° on two very
different real candidates), confirmed to hold across a wide line range with no zero-crossing nearby
(so not a line/timing-selection artifact — bisecting for where the angle crosses zero over a
200-line span just found it drifting slowly from ~0.102 to ~0.095 rad-equivalent, never reaching 0).

**Checked and ruled out as the source, so future work doesn't re-check these**:
- `spice.getfov(-85621)` ("LRO_LROCWAC_VIS") reports boresight exactly `[0, 0, 1]` — the IK itself
  doesn't encode a different nominal boresight.
- WAC-VIS has 5 separate per-filter NAIF frame IDs (`LRO_LROCWAC_VIS_FILTER_1..5`, IDs
  -85631..-85635 — found in `lro_instrumentAddendum_v05.ti`, the real IAK, which this project
  doesn't otherwise furnish; there's also `LRO_LROCWAC_UV_FILTER_1/2`, -85641/-85642). `spice.pxform`
  between any of these and the generic `LRO_LROCWAC_VIS` frame is identity to <0.001° — the
  per-filter frames exist (presumably for cross-track FOV-boundary bookkeeping, given
  `INS-85631_FOV_BOUNDARY_CORNERS`-style keywords) but carry no boresight tilt relative to each
  other or the generic frame.
- The IAK's own `INS-85621_*` entries are `SWAP_OBSERVER_TARGET`/`LIGHTTIME_CORRECTION`/
  `LT_SURFACE_CORRECT`/`CK_FRAME_ID`/`CK_REFERENCE_ID` — processing/frame-chain config, no
  geometric (boresight/distortion) override for -85621 anywhere in it.
- The real ISD's `detector_center` field (`{line: 775.76, sample: 509.54}` for this product) is
  **not** directly usable as an image sample/line coordinate — substituting `sample=509.54` for a
  ground-to-image query made the discrepancy *worse* (32km vs. ~10km), confirming it's expressed in
  some other (raw multi-band detector, pre-windowing) coordinate system this project never fully
  decoded, not the calibrated 704-sample VIS image's own sample axis.

## `TrnTestDataSet` on-disk layout, and the crop ISD sidecar's real accuracy

See `docs/dataset-plan.md` for the full design (class hierarchy, task queue) — this section is just
the durable, current-state facts about what ends up on disk, kept here alongside this file's other
concrete-format references.

**Layout**: `<output_dir>/trn_dataset/` (not `<output_dir>/dataset/`, which is
`dataset.generate_dataset()`'s own, separate flat per-`product_id` layout — the two don't collide in
meaning or content) holds `manifest.csv` plus `crop/<edr_product>_crop.{cub,json}`,
`hillshade/<edr_product>_hillshade.{tif,json}`, an empty reserved `reproject/`, per-entry
intermediates under `_work/<edr_product>/` (`.tsai`, DEM/ortho tiles, pre-copy render output — kept
out of `crop`/`hillshade` so those two only ever hold the canonical named pair). Task-queue state
lives outside this folder entirely now, in `<output_dir>/.huey/` — two separate `huey` sqlite
databases (`tasks.db` for `populate()`, `tasks_parallel.db` for `populate_via_workers()`'s real
worker pool), each shared by every dataset under that `output_dir` — see `src/trntest/tasks.py`'s
module docstring and `docs/dataset-plan.md`'s "Task queue" section. Filenames key on `edr_product` (`M1327210646CE` →
`crop/M1327210646CE_crop.cub`), matching `isis_wac.py`'s own scratch-dir convention; row lookup
(`TrnTestDataSet[key]`) keys on `product_id` instead, matching `dataset.generate_dataset()`'s
existing per-image folder convention — the two are always equal in today's real manifest, so this
split is currently low-risk, just future-proofing.

**The real WAC pipeline's own raw-EDR scratch** (`config.scratch_dir/isis_wac/<edr_product>/` —
stitched cube, calibration intermediates) is deliberately *not* duplicated inside a
`TrnTestDataSet`'s `_work/` — it stays in the shared scratch location, so two different datasets
referencing the same `edr_product` reuse that real ISIS work (`isis_wac.run_pipeline`/
`crop_for_camera` are already idempotent) instead of redoing it.

**`isis_wac.run_isd_generate_for_crop`'s sidecar is accurate, not just informational** — it exists
specifically so `crop/<edr_product>_crop.json` truthfully describes `crop/<edr_product>_crop.cub`'s
own dimensions and real acquisition time window, unlike a naive `isd_generate -i` run directly
against a cropped Pushframe cube (see "`isd_generate -i` on an ISIS-`crop`ped Pushframe cube" above
for the exact bug this patches: the crop's own `starting_ephemeris_time`/`ending_ephemeris_time`/
`center_ephemeris_time` and `instrument_pointing.ck_table_start_time`/`ck_table_end_time` otherwise
still read as if the crop started at the original, pre-`crop` cube's first line). **This does not
make the sidecar usable for actual reprojection** — like any Pushframe ISD in this codebase,
`usgscsm`'s `groundToImage` remains unreliable for that (see the `usgscsm` bug section above); real
ground↔image lookups always go through `resolve_ground_to_image_model`/`ground_to_image_pixel`
instead, regardless of this patch. The distinction matters: accuracy of the sidecar's own stated
metadata and usability for reprojection are two separate properties, and this only fixes the first.

**Live-validated on the real default candidate (`M1327210646CE`, `flip=true`/
`framelet_order_reversed=true`)**: `crop/M1327210646CE_crop.json`'s `image_samples`/`image_lines`
(704/980) exactly match `gdalinfo`'s real raster dimensions of `crop/M1327210646CE_crop.cub`.
`starting_ephemeris_time` matches real `campt` output at the crop's own line 1 to **0.016s**, and
`center_ephemeris_time` is exactly `(starting + ending) / 2` as expected. `ending_ephemeris_time`,
however, is **~1.39s** off from real `campt` output at the crop's own last line (980) — suspiciously
close to one whole `interframe_delay` (1.40625s here), not just numerical noise.

**Traced, not a bug in this patch**: re-ran the identical `campt`-vs-`isd_generate` comparison
directly on the full, *unpatched* stitched cube (`isis_wac.run_isd_generate`'s own pre-existing
output, untouched by any of this feature's new code) and found the exact same pattern: `campt` at
the full cube's **line 1** matches the ISD's `ending_ephemeris_time` (not `starting_ephemeris_time`)
to within ~1.39s, while `campt` at the full cube's **last line** (3612) matches
`starting_ephemeris_time` to within 0.017s. In other words, for this `flip=true` product, physical
row 1 corresponds to the *chronologically last* framelet and the physical last row to the
*chronologically first* one (consistent with `framestitch`'s `FLIP=TRUE` reordering, and with why
`campt`-based lookups — which read the cube's own embedded, physically-correct-by-construction
pointing data directly — remain the only trustworthy ground-to-image/image-to-ground path in this
codebase). `isis_wac.run_isd_generate_for_crop` shifts the full-cube ISD's already-computed
`starting_ephemeris_time`/`ending_ephemeris_time`/`center_ephemeris_time` by one shared
`time_offset_s`, so it faithfully **preserves** whatever gap already existed between
`isd_generate`'s own `ending_ephemeris_time` and true chronological end on the full cube — it
doesn't introduce a new one. This ~1-`interframe_delay` gap specifically on `ending_ephemeris_time`
(not `starting_ephemeris_time`) is therefore a pre-existing characteristic of `isd_generate -i`'s
own output for this flip direction, most plausibly an exclusive-fencepost convention (`ending` =
one frame *past* the last real sample, not the last sample's own time) — never previously noticed
because `docs/history.md`'s own investigation already confirmed `ck_table_end_time`/`ending_
ephemeris_time` have zero effect on `mapproject`'s output either way (`starting_ephemeris_time` is
the one field CSM interpolation actually keys off), so nothing before this feature ever checked its
absolute accuracy against `campt`. Not investigated further (out of scope for a field that was
already known not to affect reprojection); worth reopening only if a future consumer starts relying
on `ending_ephemeris_time`'s own absolute accuracy, or if a `flip=false` product ever shows a
different-shaped discrepancy (in which case the fencepost hypothesis above would be a good first
thing to test directly, rather than re-deriving this investigation from scratch).

**Why a constant correction rotation doesn't work, despite the "frame-relative constant offset"
signature initially suggesting one would**: a Wahba fit from real correspondences can only ever
recover the rotation that's *actually true* — and that's already proven to equal
`camera_pose_moon_me`'s own SPICE computation. So `correction = R_naive⁻¹ @ R_true` is
`≈ identity` (confirmed live: 0.47° from identity) by mathematical construction, not by bug — no
rotation exists that both matches the proven-correct attitude and changes where `[0,0,1]` points
without being a no-op. The ~5-6° gap is a statement about which pixel is the true optical center
(a principal-point fact), not about the camera's orientation.

**The fix in use** (`camera.build_camera()`): don't derive "where the crop centers" from a boresight
ray at all — run the real ISIS pipeline, query the real ground point at the crop's actual center
pixel via `campt`'s image-to-ground direction (`isis_wac.ground_point_at_pixel`), and re-aim the
synthetic camera's boresight directly at that real point (`camera.look_at_rotation`, Gram-Schmidt
against the original SPICE X axis for roll). See `docs/history.md`'s dated entry for the full
investigation, including the (built, then reverted) correction-rotation attempt and why live
validation caught it before it shipped.

## LightGlue tie-point matching

`src/trntest/pose_alignment.py`'s `match_features_lightglue` is a second feature-matcher option
alongside the module's original SIFT-based `match_features` (see `docs/history.md`'s dated entries
for the pose-alignment investigation this module is part of) — a deep-learned local-feature
extractor (DISK) + learned matcher (LightGlue) pair, tried specifically to push match count/quality
higher for more challenging future EDRs (shadowed terrain, low texture) than classical SIFT can
reliably deliver.

- **No official PyPI package.** `cvg/LightGlue`'s own `pyproject.toml` declares `version = "0.0"`
  and reads dependencies from a separate `requirements.txt`; the upstream-documented install is
  `git clone` + `pip install -e .`. `docker/Dockerfile` instead installs directly from a pinned git
  commit (`lightglue @ git+https://github.com/cvg/LightGlue.git@<sha>`) for reproducibility — a
  floating `@main` reference would silently change behavior on every image rebuild.
- **Installed `--no-deps`, deliberately not listed in `pyproject.toml`'s own `dependencies`.**
  LightGlue's `requirements.txt` (`torch`, `torchvision`, `numpy`, `opencv-python`, `matplotlib`,
  `kornia`) would otherwise reinstall `opencv-python` alongside this project's own
  `opencv-python-headless` — both provide the `cv2` import and collide on install (confirmed: this
  is a real, known footgun, not a hypothetical one — pip/uv resolve them as two independently-named
  packages and don't detect the import-name clash, so both get installed and silently
  overwrite each other's files depending on install order). This project supplies `torch`/
  `torchvision` (CPU-only wheels, see `docker/Dockerfile`'s comment) and `kornia` (plain PyPI)
  itself instead, plus `opencv-python-headless` already covers the `cv2` import LightGlue's own code
  needs at import time (`lightglue/utils.py`, `lightglue/disk.py`, `lightglue/sift.py` all `import
  cv2` at module load — confirmed via direct inspection of the pinned commit's source, since
  `lightglue/__init__.py` eagerly imports every extractor submodule regardless of which one is
  actually used, so all of their import-time dependencies apply unconditionally: `torchvision` is a
  hard dependency too, via `aliked.py`'s `torchvision.ops.deform_conv2d`, even though this project
  only uses DISK).
- **DISK, not SuperPoint, as the local-feature extractor** — SuperPoint is the more commonly-used
  LightGlue pairing in tutorials/benchmarks, but its inference file and pretrained weights (`lightglue/
  superpoint.py`, adapted from Magic Leap's original release) carry a proprietary-style notice ("The
  receipt or possession of this source code and/or related information does not convey or imply any
  rights to reproduce, disclose or distribute its contents, or to manufacture, use, or sell anything
  that it may describe"), not a standard permissive OSS license — a real constraint for a repo pushed
  publicly. DISK (Apache-2.0) and ALIKED (BSD-3-Clause) are the other two extractors LightGlue ships
  pretrained weights for, comparable match quality to SuperPoint in the paper's own benchmarks;
  DISK was chosen as the direct drop-in with no further licensing question. Explicit user decision,
  not assumed.
- **Pretrained weights** (LightGlue matcher + DISK extractor) are fetched from a `github.com/cvg/
  LightGlue` release and via `kornia.feature.DISK.from_pretrained` respectively, both through
  `torch.hub` — cached under `TORCH_HOME` (`docker/Dockerfile` sets this to `/workspace/cache/torch`,
  see `docs/caching.md`'s own section on this), tens of MB total, fetched once.
- **CPU-only**: confirmed via direct inspection of the pinned commit's source that no file in the
  LightGlue/DISK code path attempts to compile or load a custom CUDA extension at import or runtime
  — `aliked.py`'s `torchvision.ops.deform_conv2d` (the one op that could plausibly need a CUDA
  build) works fine on CPU tensors. Real published CPU benchmark: ~20 FPS at 512 keypoints on an
  Intel i7 10700K — this project isn't latency-sensitive (a one-shot notebook cell, not a real-time
  loop), so CPU-only is a straightforward choice, no GPU passthrough needed in `docker-compose.yml`.

## ISIS PushFrame `campt` ground-to-image: a real, scattered ~38% failure rate, not an edge artifact

`control_network.resolve_control_points` (see its own module docstring) queries `isis_wac.
ground_to_image_pixel` once per matched tie point, against the original (pre-`cam2map`) WAC crop
cube's native PushFrame camera model. On the current default candidate, a real 767-point LightGlue
match set resolved only 477 (290 failures, ~38%) — investigated directly rather than assumed benign,
since the project's own Phase 30 precedent (`_CROP_EDGE_MARGIN_PX`) already established that `campt`
has real numerical instability near a cropped cube's edge, making that the obvious first suspect.

**Ruled out**: edge proximity. A direct measurement (distance from each matched pixel to the nearest
invalid/padding pixel, via `cv2.distanceTransform`) found resolved and dropped points have nearly
identical edge-distance distributions (median 119px vs. 122px in the downsampled matching grid), and
the drop rate stays ~38-39% even 40+px from the boundary — not concentrated at the crop's own edge at
all, contrary to the Phase 30 pattern.

**Actual cause, confirmed live**: every one of the 290 failures is ISIS's "no surface intersection"
error specifically (zero were "not inside cube" — checked directly via each failing query's real
`campt` stderr, not assumed) — a fundamentally different failure than an index just missing the
cube's own pixel-array bounds. This matches a real, independently-documented upstream ISIS issue for
PushFrame cameras specifically: `GetLocalNormal`'s calculation can land outside the correct framelet
during the ground-to-image solve, producing an erroneous local normal and a failed/non-convergent
intersection search (DOI-USGS/ISIS3 GitHub issue #4256) — a known numerical fragility in ISIS's own
PushFrame geometry, not a bug introduced by this project's code, and consistent with failures
scattered roughly uniformly through the image rather than concentrated anywhere in particular.

**Not currently a concern for terrain-relief bias, but worth re-checking once it would be**: this
project's control points are still ellipsoid-only (`isis_wac.run_spiceinit`'s `shape=ellipsoid`, see
`control_network.py`'s own docstring for why) — a smooth ellipsoid's local surface normal varies only
slowly with position, so there's no real steep-terrain trigger for the local-normal bug to hit yet.
If/when a real DEM-aware shape model is added (a planned follow-up, see `docs/plan.md`'s open items),
this failure mode could plausibly get *worse* specifically at high-relief features like crater rims —
exactly the terrain the user's own visual parallax observation (motivating the whole 3D-alignment
investigation) depends on. Re-run this same edge-distance/failure-kind check against real terrain
once that lands, rather than assuming the ellipsoid-only finding still holds.

## `campt`'s `USECOORDLIST` batch mode: real gotchas, and why it matters here

`isis_wac.ground_to_image_pixels_batch` (used by `control_network.resolve_control_points`, see
above) runs one real `campt usecoordlist=true` call for many points at once instead of one
`ground_to_image_pixel` subprocess per point — confirmed live to matter a great deal: each
individual `campt` call pays real process-spawn/SPICE-load overhead (~300ms observed), which
dominated `resolve_control_points`' wall-clock for a multi-hundred-point control network (767 points
→ ~230s of pure subprocess overhead, collapsed to ~3s). Two real gotchas found live, not documented
anywhere obvious in `campt -help`'s short parameter list (confirmed via `campt.xml`, ISIS's own
per-app doc source, and direct experimentation):

- **`COORDLIST`'s ground-coordinate column order is `latitude, longitude`**, not `longitude,
  latitude` (`campt.xml`: "Expected order for ground coordinates: latitude, longitude") — the
  opposite of this project's own `(lon_deg, lat_deg)` convention everywhere else
  (`ground_to_image_pixel`'s own argument order, `GroundPoint`'s `PositiveEast360Longitude` field,
  etc.). Getting this backwards doesn't error — it silently returns a wrong-but-plausible-looking
  result (every row projecting to the same stale sample/line, no `Error`), which would be very easy
  to miss without an independent per-point cross-check.
- **A failed row's `Sample`/`Line` fields come back as a stale carryover from the last *successful*
  row in the batch**, not `NULL`/absent — confirmed live (`allowerror=true`, needed so one bad point
  doesn't abort the whole batch). The only reliable success signal is the row's own `Error` field:
  the literal string `"NULL"` on success, a real message (e.g. "Requested position does not project
  in camera model; no surface intersection") otherwise.
- `APPEND` defaults to `TRUE` (silently prepends onto whatever's already at the `TO=` path) —
  `append=false` is required for a fresh, correct result on a reused/shared output path.

Live-validated for correctness, not just speed: 100 real crop pixels' own real ground points
(round-tripped through `campt`), batched vs. the old per-point loop — 0 mismatches, 46x faster on
that sample size.
