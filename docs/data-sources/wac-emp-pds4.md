# WAC_EMP PDS4 archive (live default ortho/texture source)

Index: [`docs/data-sources.md`](../data-sources.md).

Replaces Lunaserv's WMS-served `luna_wac_normalized_reflectance` layer (see
[`lunaserv-wms.md`](lunaserv-wms.md)) as `fetch_dem_and_ortho`'s default ortho/texture source
(`ortho_source="wac_emp_pds"`, `config.wac_emp_base_url`): that WMS layer was confirmed to carry a
real, uncorrected **affine** display stretch (`DN/255 = a*reflectance + b`, `a≈5.94-5.98`,
`b≈-0.213..-0.214`, measured at two independent real locations ~900km apart, agreeing to ~0.5%), not
raw reflectance and not just a harmless constant scale — a real structural bug candidate for
`hapke_shade_ortho`'s ratio-based relighting, which a nonzero offset doesn't cancel out of
algebraically. WAC_EMP's own README already names it as this project's own citation for that WMS
layer (ASU/LROC's Empirical Photometric Function product, Boyd, Robinson & Sato 2012); this switch
fetches it at its authoritative PDS4 source instead of through Lunaserv's intermediary render,
mirroring this project's earlier DEM-source move off Lunaserv to Astropedia's flat-file GLD100 (see
[`astropedia-gld100.md`](astropedia-gld100.md)) for the same class of reason.

- Base URL: `https://pds.mcp.nasa.gov/data/store/img/lunar_reconnaissance_orbiter/pds4/lroc/lro-l-lroc-5-rdr/LROLRC_2001/DATA/MDR/WAC_EMP/`
  (`config.wac_emp_base_url`) — an S3-backed archive host (`pds-img-archive-prod` bucket, confirmed
  via its own error/listing XML), reachable both as a plain per-object `GET` (what `cache.
  fetch_wac_emp_tile` actually does) and, with `?list-type=2&prefix=...&delimiter=/` query params
  appended to the bucket root, as a real S3 `ListObjectsV2` listing — used live (not guessed) to
  derive the tile-naming scheme below.
- **Tile naming, confirmed live via the archive's own real directory listing** (159 keys under
  `.../WAC_EMP/`, not inferred from one example filename): each tile is
  `WAC_EMP_<wavelength_nm>NM_E300<N|S><lon_center_deg*10:04d>_<ppd:03d>P.IMG` (`.xml` label sidecar of
  the same base name also present, unused by this project — GDAL's PDS3 driver reads the `.IMG`
  file's own attached label directly). `wac_emp_tile_ids_for_bbox` builds this string.
  - **Wavelength**: one of 7 real bands, `321/360/415/566/604/643/689` (nm) — the identical band set
    ISIS's own Hapke calibration cube already offers (`hapke.HAPKE_CALIBRATION_WAVELENGTHS_NM`).
    This project defaults to 643nm (`DEFAULT_HAPKE_CALIBRATION_WAVELENGTH_NM`), matching the
    wavelength every other real-photometry piece of this codebase already targets.
  - **Resolution (`ppd`)**: every band is offered at 64 ppd; 643nm *additionally* has a real 304 ppd
    product (confirmed live: both `WAC_EMP_643NM_E300N1350_064P.IMG` and
    `WAC_EMP_643NM_E300N1350_304P.IMG` exist) — this project's own default (`ppd=304` in
    `wac_emp_tile_ids_for_bbox`/`fetch_wac_emp_reflectance`).
  - **Tile grid**: the equirect (non-polar) coverage is exactly one 60°-tall latitude band per
    hemisphere (0-60°N, 0-60°S — center magnitude 30.0°, hence the fixed literal `"E300"` segment
    every equirect tile ID shares) × 4 lon zones 90° wide each, centered at 45°/135°/225°/315°
    (Positive-East 0-360° convention: 0-90, 90-180, 180-270, 270-360). Confirmed directly against a
    real fetched/opened tile: `WAC_EMP_643NM_E300N1350_304P.IMG` is `18240 x 27360` px at 304 ppd
    (`60*304=18240`, `90*304=27360`, exact) — real coverage 90-180°E, 0-60°N, matching its own ID's
    `lon_center=135.0`/hemisphere `N` exactly.
  - **Polar coverage** (60-90° both hemispheres): a real, *separate* tile pair also exists in the same
    listing, 643nm/304ppd only — `WAC_EMP_643NM_P900N0000_304P.IMG`/`..._P900S0000_304P.IMG` — and
    **is fetched by this project** (`wac_emp_tile_ids_for_bbox` dispatches to it automatically for a
    footprint touching latitude beyond `WAC_EMP_MAX_ABS_LATITUDE_DEG = 60.0` in one hemisphere; a
    footprint straddling that 60° boundary gets both the equirect and polar tile, mosaicked, rather
    than either alone — see the "multi-tile mosaic" bullet below. A footprint that needs polar
    coverage at a wavelength/ppd other than 643nm/304 still raises `ValueError` — no silently-wrong
    tile).
    Confirmed live via `gdalinfo`/`rasterio` on the real `P900N0000` tile (1,394,200,920 bytes,
    18669x18669 px at 304 ppd): a genuine Polar Stereographic (variant A, EPSG method 9810,
    `lat_0=90`/`lon_0=0`) projection GDAL's PDS3 driver reads natively, same as the equirect tiles —
    no hand-rolled polar-projection math needed, and no antimeridian-branch-cut risk either (that bug
    is specific to the equirect tiles' linear `x = R*lon_rad` formula — this projection's longitude
    dependence is smooth sin/cos, so `reproject_wac_emp_reflectance_to_local_grid` gates that fix off
    for it via a real PROJ4 `proj` tag check, not a hardcoded tile-family list).
    - **Seam with the equirect grid**: the polar tile's own raster edge lands almost exactly on the
      equirect grid's 60° boundary — real data confirmed starting at 61°, with a single-pixel nodata
      sliver right at the shared 60° edge (an ordinary rasterization artifact, not a real coverage
      gap) — so the two tile families meet cleanly with no real seam gap in practice.
    - **Real, scattered interior nodata voids**: unlike every equirect AOI fetched by this project so
      far (fully valid, no embedded nodata needed), the polar tile carries a genuine embedded
      `NoData Value` (`-3.4028227e+38`), and real single-pixel voids exist well within its nominal
      coverage — confirmed via a systematic 10°-longitude sweep: fully solid from 60-75°, then a
      growing but still sparse (roughly 10-30% of sampled points) speckle of voids from 80° up to the
      pole itself, presumably real imaging/illumination-geometry gaps in the underlying mosaic, not a
      processing bug. `reproject_raster_to_local_grid_array`'s existing `src_nodata`/`dst_nodata` handling
      (already exercised by the DEM path) propagates these through as ordinary NaN holes in the
      output — confirmed on two real candidates (`M1314069739CE`, -72.6°: 2,381/1,731,856 NaN
      pixels ≈0.14%; `M1314073855CE`, 70.8°: 6,689/1,731,856 ≈0.39%), both otherwise well-formed.
    - **Practical reach today**: `fetch_dem_and_ortho` fetches the DEM (GLD100, capped at
      `ASTROPEDIA_MAX_ABS_LATITUDE_DEG = 79.0`) before the ortho, so this polar-ortho support is only
      actually reachable for 60-79° until DEM coverage is separately extended past 79° (tracked, not
      yet done, in `docs/proposed-tasks/open-items.md`'s GLD100/VIRA bullet) — real 60-79° candidates
      from a live orbit-sequence dataset (`M1314069739CE`/`M1314073855CE` above) confirm this range
      works end to end today.
  - **Multi-tile mosaic**: an AOI straddling the equator, a 90°-lon zone boundary, or the
    equirect/polar 60° boundary is mosaicked across the boundary rather than raising —
    `wac_emp_tile_ids_for_bbox` returns every tile the padded AOI touches (usually one), each fetched
    (`fetch_wac_emp_reflectance`) and reprojected onto the shared destination grid independently
    (`reproject_wac_emp_reflectance_to_local_grid`), then combined pixel-by-pixel
    (`geo_utils.merge_local_grid_arrays`) — correct regardless of which combination of tile projection
    families (equirect, polar, or one of each) is involved, since the merge only ever looks at the
    shared destination grid, never the source tiles' own differing CRSs. A real, non-rare case for
    this project's own `trn_dataset` manifest: roughly a third of its rows sit close enough to the 60°
    equirect/polar split that any nonzero AOI padding pushes them across it.
- **File format, confirmed live via `gdalinfo`/`rasterio` on the real 304ppd tile**: IEEE754 float32,
  real physical reflectance (I/F), a genuine PDS3-attached-label GeoTIFF-equivalent GDAL's own `PDS3`
  driver reads natively (`Driver: PDS3/PDS3`) — real embedded map-projection keywords (Equidistant
  Cylindrical/"Equirectangular", real Moon radius) GDAL exposes as a normal `crs`/`transform`, no
  hand-rolled PROJ4 string or manual byte-offset math needed (unlike this migration's own throwaway
  diagnostic scripts, which predated confirming this and did the byte-range/PDS3-label math by hand).
  Every pixel is normalized to a fixed reference photometric geometry (incidence=30°, emission=0°,
  phase=30°) via an empirical (Boyd et al. 2012) function, not a raw albedo map — see
  `REFERENCE_INCIDENCE_DEG`'s own module-level comment in `hapke.py` for how `hapke_shade_ortho`
  relights this back out for a real candidate's own geometry.
- **At least the "180-270°" zone tile's own georeferencing is written in unwrapped, continuous
  longitude past +-180°, not PROJ's canonical (-180°, 180°] convention** — confirmed live on both
  `WAC_EMP_643NM_E300S2250_304P` and `..._E300N2250_304P`: each raster's projected X spans
  `[R*pi, R*1.5pi]` (both positive), under a `central_meridian=0` Equirectangular CRS. A generic
  `rasterio.warp` CRS-to-CRS transform (`transform_bounds`/`reproject`) normalizes any input
  longitude into (-180°, 180°] before applying that `central_meridian=0` formula, so a real-world
  point whose true longitude is e.g. 200° (SPICE's signed convention: -160°) lands at
  `x = R*radians(-160°)` — a full sphere circumference away from where this tile's own raster
  actually stores that location. This silently produced a degenerate, zero-width read `Window`
  (`CPLE_AppDefinedError: Invalid dataset dimensions`) and, once that was worked around, an
  all-nodata `rasterio.warp.reproject` output (the same branch cut, hit a second time by the
  pixel-level warp) — see `ortho_wac_emp._reproject_one_wac_emp_tile_to_array`'s own inline
  comments for the two-part fix (shifting the read window by one circumference when it lands outside
  the tile's own stored bounds; re-expressing the source CRS's `central_meridian` at the tile's own
  PROJ-normalized center before the warp, so no destination point needs to cross ±180° from it) and
  `tests/test_ortho_wac_emp.py`'s own antimeridian regression test. Only the "225°" zone (both
  hemispheres) is confirmed to need this; whether the "315°" zone's tiles use the same unwrapped
  (rather than canonical -90°..0°) convention is unverified -- the fix itself doesn't assume either
  way (it derives the tile's own domain from its real `bounds`/`crs`, not a hardcoded zone list), so
  it's correct regardless, but the fact isn't independently confirmed for that zone.
- **Size**: the 304ppd 643nm tile is ~1.86 GB (1,996,295,040 bytes, confirmed live) — comfortably
  within `cache.cached_get`'s normal per-call-unique-temp-file range (the same range
  `fetch_isis_kernel`'s ~1.65GB CK merges already use), not `fetch_astropedia_gld100`'s special
  resumable-curl path (that path exists specifically for GLD100's much larger ~10GB single file).
  `cache.fetch_wac_emp_tile` fetches/caches the whole tile once; `reproject_wac_emp_reflectance_to_local_grid`
  then does a local windowed read of just the AOI (`window_from_bounds`/`window_transform`, the same
  pattern `reproject_astropedia_elevation_to_local_grid` uses) — no repeated remote reads.
- **Numeric-pipeline consequence** (not just a data-source swap): this data has no embedded display
  stretch, unlike Lunaserv's WMS-served `uint8` DN — `hapke_shade_ortho`'s old `ortho.astype(np.float64)
  / 255.0` un-scaling step is no longer appropriate (there's no DN to un-scale, the array already *is*
  reflectance) and was removed; `relit_reflectance = ortho * ratio` operates directly on real physical
  units. A new, explicit, purely cosmetic `stretch_reflectance_to_uint8` step
  (`DISPLAY_STRETCH_REFLECTANCE_MIN`/`_MAX`, a fixed linear range, not adaptive) converts the result to
  a displayable `uint8` image at the very end of the pipeline, decoupled from the physics. `shade_ortho`
  (the plain-Lambertian fallback) is **unchanged**, deliberately still tied to the old WMS-DN
  convention — see its own docstring; it isn't meant to be combined with `ortho_source="wac_emp_pds"`.
- **Deprecated fallback**: `ortho_source="lunaserv_wms"` (`fetch_dem_and_ortho`) keeps the original
  Lunaserv-WMS ortho path reachable for comparison, unchanged, with its own distinct (suffix-less)
  `ortho_shaded_filename` so cached files from before this migration stay valid/resumable under their
  own names. Only numerically coherent with `hapke=False` after this migration (see
  `fetch_dem_and_ortho`'s own docstring) — its `uint8` DN is not the real reflectance
  `hapke_shade_ortho` now assumes.
