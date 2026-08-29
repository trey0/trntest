# Lunaserv WMS (visible imagery; DEM path deprecated)

Index: [`docs/data-sources.md`](../data-sources.md).

**DEM fetching from this server is deprecated** — see [`astropedia-gld100.md`](astropedia-gld100.md)
for the live default DEM source. Summary: Lunaserv's DTM layer
(`luna_wac_dtm_numeric_meters_absolute`) has a real, axis-aligned crosshatch artifact baked into its
own native tile (FFT-confirmed present regardless of requested ppd, CRS, or resampling kernel) — not
fixable client-side, since the server exposes no resampling control (confirmed via several vendor
`GetMap` parameter probes, all ignored) and no backing-store metadata. `src/trntest/lunaserv.py`'s
`fetch_dem_native`/`reproject_dem_to_local_grid` still implement the native-CRS-fetch-plus-local-
reprojection approach that fixed an *earlier*, different artifact from this same server (see below) —
kept for reference/comparison, no longer called by `fetch_dem_and_ortho`'s default path. Everything
below this deprecation note that's DTM-specific (the local-CRS SRS discussion, the planetocentric-
radius gotcha, the DTM layer list) describes that deprecated path; the **ortho fetch**
(`luna_wac_normalized_reflectance` et al., further down) is unaffected and still current.

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
  `mapproject --ref-map` (see `docs/external-tools.md`) turned out not to preserve that anisotropy
  when reprojecting onto it, silently stretching the output. A local Orthographic CRS has square
  meter pixels everywhere, so that failure mode can't arise.
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
    photometric composite. **No longer the live default ortho source** (see
    [`wac-emp-pds4.md`](wac-emp-pds4.md)) — kept reachable via `ortho_source="lunaserv_wms"` for
    comparison. Chosen over `luna_wac_global` on image-quality grounds (see "Ortho layer noise"
    below) when it *was* the default. Global bbox `-180/-90/180/90`. **Confirmed (2026-08-23) to
    carry a real, uncorrected affine display stretch, not raw reflectance**: `DN/255 = a*reflectance +
    b` with `a≈5.94-5.98`, `b≈-0.213..-0.214`, measured at two independent real locations (38.8°N and
    8.7°N, ~900km apart) agreeing to ~0.5% — this is the actual reason for the WAC_EMP-PDS migration,
    not just a general "prefer the authoritative source" preference.
  - `luna_wac_global` — "LROC WAC Global 100m/px" visible mosaic, composited from ~15,000 raw WAC
    images (no evident per-pixel outlier rejection). No longer the default ortho source (see above)
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
    source** (see this section's top note and [`astropedia-gld100.md`](astropedia-gld100.md)) —
    confirmed served at a real, coarser-than-advertised ~128 ppd/~237 m ceiling regardless of
    requested resolution/CRS, with a further, unfixable-client-side axis-aligned artifact baked into
    the tile itself.
  - Surveyed every other DTM/DEM-ish layer this server advertises looking for a finer global
    alternative before giving up on Lunaserv entirely: `luna_nac_dtms`/`luna_pds_rdr_dtm` are vector
    *footprint-index* shapefiles (not raster DEM layers) pointing at scattered individual LROC NAC
    stereo DTMs — real, much higher resolution where they exist, but local/opportunistic coverage,
    incompatible with this project's catalog-driven, essentially-anywhere-on-the-Moon image
    selection. Per-Apollo-site DTMs/NAC mosaics (one even advertised at 50 cm/px) have the same
    coverage problem, just smaller still. No global layer here is finer than what's already deprecated
    above — the problem was never *which* Lunaserv layer, it was that no Lunaserv layer at this
    resolution exists for arbitrary lunar coverage; see [`astropedia-gld100.md`](astropedia-gld100.md)
    for what does.
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
  concern, not primary scientific analysis.
