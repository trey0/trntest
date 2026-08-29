# `crop`

The real WAC image itself: a footprint-matched crop of the same LROC WAC EDR `hillshade`/`reproject`
are posed against, calibrated and made geometrically usable via ISIS3. `trn_dataset.TrnTestCropImage`;
entry points `isis_wac.run_pipeline`/`crop_for_camera`.

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
