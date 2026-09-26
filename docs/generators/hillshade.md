# `hillshade`

Synthetic image rendered by ASP's `sat_sim` from real terrain data, posed by the real LRO SPICE
trajectory, at a fixed `config.image_size` (~100 m/px on the reference candidate — see
[`../resolution-investigation.md`](../resolution-investigation.md)). `trn_products.TrnTestHillshadeImage`;
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
3. **Cast shadows**: the relit ortho is multiplied by `cast_shadow.illumination_fraction`, which
   darkens terrain the Sun can't reach past other terrain (e.g. a crater floor behind its rim) —
   per-facet shading alone can't. On by default; `cast_shadows=False` turns it off. See "Cast
   shadows" below.
4. `sat_sim --camera-list` renders the image from the shaded ortho + DEM through the posed camera.
5. `cam_gen` converts the same camera to a CSM Frame model-state JSON (the "ISD" sidecar).

## Cast shadows

`cast_shadow.py` computes a per-pixel illumination fraction (`1` = fully lit, `0` = fully shadowed)
with a sun-aligned sweep: in a frame with the Sun at infinity along `+x`, each row is swept from the
sun-facing edge inward, in order of horizontal distance toward the Sun, with a running maximum of
height perpendicular to the rays. Ordering by horizontal distance (not distance along the ray) keeps
it correct at any sun elevation. The module docstring covers the method;
[`../../notebooks/sun_aligned_shadow_sweep.ipynb`](../../notebooks/sun_aligned_shadow_sweep.ipynb)
shows it on the lowest-sun candidate, next to ISIS `shadow` and the real WAC image.

- Self-shadow (a facet facing away from the Sun) isn't part of the fraction; the per-facet shading
  already renders it dark.
- The sun-facing edge of the DEM is always lit: occluders outside the fetched AOI aren't modeled.
  At low sun this can miss real shadows near that edge.
- GLD100's 100 m posting smooths away small relief, so shadows are undercounted at low sun (see
  [`../data-sources/astropedia-gld100.md`](../data-sources/astropedia-gld100.md)).
- Runtime is ~6 s and ~200 MB extra memory for a ~2400 px DEM (streamed in row chunks).
- `sfs_validation.py` always uses a `cast_shadows=False` ortho: ASP `sfs` models per-facet shading
  only, so a cast shadow would be a disagreement unrelated to what that check measures.
- Changing the default changed the shaded-ortho filename (`_castshadow` suffix), so existing
  entries re-shade on next access. Already-rendered `hillshade` rasters are not regenerated
  automatically — remove them (or use a fresh dataset folder) to pick up shadows.

See [`../external-tools.md`](../external-tools.md) for `sat_sim`/`cam_gen` flags and gotchas.
