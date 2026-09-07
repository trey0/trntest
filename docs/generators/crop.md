# `crop`

The real WAC image itself: a footprint-matched crop of the same LROC WAC EDR `hillshade`/`reproject`
are posed against, calibrated and made geometrically usable via ISIS3. `trn_products.TrnTestCropImage`;
entry point `isis_wac.ensure_crop_for_camera` (falls through to `run_pipeline`/`crop_for_camera` only
on this product's first-ever generation).

## Data sources

- Real WAC EDR (LROC), fetched via `isis_wac.fetch_edr_img`. See
  [`../data-sources/lroc-wac-edr-cdr.md`](../data-sources/lroc-wac-edr-cdr.md).
- SPICE pointing/timing, attached in place by `spiceinit web=yes` — no local kernel files needed for
  this step.

## Processing

1. `lrowac2isis` splits the EDR into even/odd x UV/VIS cubes.
2. `spiceinit web=yes` attaches SPICE geometry to each VIS cube.
3. `lrowaccal` calibrates each to I/F (a reflectance factor) at the image's real acquisition
   geometry — unlike `hillshade`'s WAC_EMP ortho, not renormalized to any fixed reference geometry
   (see [`hillshade.md`](hillshade.md)).
4. `framestitch` combines even+odd into one calibrated, framelet-interleaved cube.
5. ISIS `crop` crops that cube to the footprint being compared.
6. `isd_generate` produces an accurately-scoped ISD sidecar for the crop — not usable for
   reprojection (`usgscsm`'s ground-to-image solve is unreliable for this sensor's Pushframe camera
   model). Reprojection instead uses ISIS's native camera model via `cam2map` — see
   [`reproject.md`](reproject.md).

See [`../external-tools.md`](../external-tools.md) for ISIS app flags and gotchas.

## Where the crop actually lives

The crop's real published home is `cache/wac_crop/<edr_product>_crop.cub` (permanent, shared across
every dataset, keyed by `edr_product`) — not `_work/<entry>/isis/`, which by default
(`config.delete_isis_intermediates=True`) is wiped entirely right after the crop is published there,
alongside the raw EDR and every other pipeline intermediate. `TrnTestDataSet`'s own
`crop/<edr_product>_crop.cub` (see `docs/intermediate-product-discipline.md`) remains a per-dataset
copy of it, as today. See [`../caching.md`](../caching.md)'s "WAC crop caching" section for the full
rationale and the two config flags governing this.
