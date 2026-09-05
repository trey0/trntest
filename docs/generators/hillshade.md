# `hillshade`

Synthetic image rendered by ASP's `sat_sim` from real terrain data, posed by the real LRO SPICE
trajectory, at a fixed `config.image_size` (~100 m/px on the reference candidate — see
[`../resolution-investigation.md`](../resolution-investigation.md)). `trn_dataset.TrnTestHillshadeImage`;
entry point `render.run_sat_sim`.

## Data sources

- DEM: USGS Astropedia GLD100 (`dem_gld100.fetch_dem_astropedia`). See
  [`../data-sources/astropedia-gld100.md`](../data-sources/astropedia-gld100.md).
- Ortho: WAC_EMP PDS4 reflectance (I/F), normalized to a fixed reference geometry (incidence=30°,
  emission=0°) rather than any particular render's real geometry (`ortho_wac_emp.fetch_wac_emp_reflectance`),
  the default (`DEFAULT_ORTHO_SOURCE`). See
  [`../data-sources/wac-emp-pds4.md`](../data-sources/wac-emp-pds4.md). A deprecated Lunaserv WMS
  path (`ortho_source="lunaserv_wms"`) is kept for comparison — see
  [`../data-sources/lunaserv-wms.md`](../data-sources/lunaserv-wms.md).

## Processing

1. Both sources reprojected onto a shared, camera-centered local Orthographic CRS
   (`dem_ortho.fetch_dem_and_ortho`).
2. Ortho despeckled and **relit for this render's real sun/viewing geometry** — a Hapke BRDF via
   ISIS `photomet` by default (`hapke.hapke_shade_ortho`), needed because the WAC_EMP source
   above is normalized to a fixed reference geometry, not this geometry. Plus an along-track
   correction for this project's single-frozen-camera-pose approximation of WAC's multi-second
   pushframe scan. See [`../../notebooks/hapke_hillshade.py`](../../notebooks/hapke_hillshade.py)
   and [`../../notebooks/along_track_correction.py`](../../notebooks/along_track_correction.py) for
   comparisons against each fallback. (`reproject`, by contrast, needs no relighting step at all —
   see [`reproject.md`](reproject.md).)
3. `sat_sim --camera-list` renders the image from the shaded ortho + DEM through the posed camera.
4. `cam_gen` converts the same camera to a CSM Frame model-state JSON (the "ISD" sidecar).

See [`../external-tools.md`](../external-tools.md) for `sat_sim`/`cam_gen` flags and gotchas.
