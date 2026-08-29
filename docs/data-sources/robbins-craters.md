# Robbins lunar crater database (vector overlay/crater-grading data)

Index: [`docs/data-sources.md`](../data-sources.md). See [`../crater-grading.md`](../crater-grading.md)
for how this database is used to grade crater sharpness.

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
