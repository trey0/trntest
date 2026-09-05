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
  file's own attached label directly). `wac_emp_tile_id_for_bbox` builds this string.
  - **Wavelength**: one of 7 real bands, `321/360/415/566/604/643/689` (nm) — the identical band set
    ISIS's own Hapke calibration cube already offers (`hapke.HAPKE_CALIBRATION_WAVELENGTHS_NM`).
    This project defaults to 643nm (`DEFAULT_HAPKE_CALIBRATION_WAVELENGTH_NM`), matching the
    wavelength every other real-photometry piece of this codebase already targets.
  - **Resolution (`ppd`)**: every band is offered at 64 ppd; 643nm *additionally* has a real 304 ppd
    product (confirmed live: both `WAC_EMP_643NM_E300N1350_064P.IMG` and
    `WAC_EMP_643NM_E300N1350_304P.IMG` exist) — this project's own default (`ppd=304` in
    `wac_emp_tile_id_for_bbox`/`fetch_wac_emp_reflectance`).
  - **Tile grid**: the equirect (non-polar) coverage is exactly one 60°-tall latitude band per
    hemisphere (0-60°N, 0-60°S — center magnitude 30.0°, hence the fixed literal `"E300"` segment
    every equirect tile ID shares) × 4 lon zones 90° wide each, centered at 45°/135°/225°/315°
    (Positive-East 0-360° convention: 0-90, 90-180, 180-270, 270-360). Confirmed directly against a
    real fetched/opened tile: `WAC_EMP_643NM_E300N1350_304P.IMG` is `18240 x 27360` px at 304 ppd
    (`60*304=18240`, `90*304=27360`, exact) — real coverage 90-180°E, 0-60°N, matching its own ID's
    `lon_center=135.0`/hemisphere `N` exactly.
  - **Polar coverage** (60-90° both hemispheres): a real, *separate* tile pair also exists in the same
    listing (`WAC_EMP_643NM_P900N0000_304P.IMG`/`..._P900S0000_304P.IMG`, 643nm only) — presumably a
    polar-stereographic projection (`P900` = pole at 90°), but its real format is **unverified and not
    fetched by this project** (a deliberate scope decision, mirroring `astropedia_coverage_bbox_deg`'s
    own `ASTROPEDIA_MAX_ABS_LATITUDE_DEG` precedent): `wac_emp_tile_id_for_bbox` raises `ValueError`
    for any footprint whose padded AOI needs latitude beyond `WAC_EMP_MAX_ABS_LATITUDE_DEG = 60.0`,
    rather than guessing at the polar format or silently falling back to the deprecated Lunaserv path.
  - No multi-tile mosaic in this pass either: an AOI straddling the equator or a 90°-lon zone boundary
    also raises `ValueError` (same "no silent fallback/mosaic" stance).
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
