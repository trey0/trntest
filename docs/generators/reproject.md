# `reproject`

`hillshade`'s exact camera (same pose, same corrected FOV), textured from `crop`'s calibrated
imagery instead of the Lunaserv/Astropedia basemap — isolates the effect of texture source alone,
with geometry held fixed. `trn_products.TrnTestReprojectImage`.

## Data sources

- `crop`'s calibrated I/F, at its real acquisition geometry (`wac_resample.map_project_crop`'s
  output, on `hillshade`'s DEM grid). Because that acquisition geometry is close to this
  render's own (same real spacecraft position/orientation/timestamp `hillshade` is posed from), no
  relighting is needed here — contrast `hillshade`'s WAC_EMP source, normalized to a fixed reference
  geometry and relit for every render (see [`hillshade.md`](hillshade.md)).

## Processing

1. `crop` is map-projected by `wac_resample.map_project_crop`, not `mapproject`/CSM (same reason as
   `crop`'s reprojection, see [`crop.md`](crop.md)). By default that is `wac_resample`'s own
   map-to-image resampler: for each DEM-grid pixel it finds the framelet that sees the ground point
   (through `wac_camera_model`, SPICE poses, ISIS's lunar shape model) and interpolates within that
   framelet alone, by cubic convolution, choosing the framelet whose center line is nearest and
   steering around NULL pixels. ISIS's `cam2map` (`TrntestConfig.crop_map_projection = "cam2map"`)
   leaves short dashes from the NULL pixels `lrowaccal` leaves on each framelet's first line, and
   misplaces each framelet's last line by 3-5 map px; see `notebooks/wac_framelet_null_fill.py`.
2. That reprojected imagery replaces the Lunaserv/Astropedia ortho as `sat_sim`'s `--ortho` input,
   rendered through the exact same `Camera` as `hillshade` — no relighting step.

Byte-identical pixel grid to `hillshade` by construction, so no separate basemap validation is
needed — see `notebooks/image_generation.py`'s Phase 8.
