# `reproject`

`hillshade`'s exact camera (same pose, same corrected FOV), textured from `crop`'s calibrated
imagery instead of the Lunaserv/Astropedia basemap — isolates the effect of texture source alone,
with geometry held fixed. `trn_dataset.TrnTestReprojectImage`.

## Data sources

- `crop`'s calibrated I/F, at its real acquisition geometry (`isis_wac.run_cam2map_for_crop`'s
  output, reprojected onto `hillshade`'s DEM). Because that acquisition geometry is close to this
  render's own (same real spacecraft position/orientation/timestamp `hillshade` is posed from), no
  relighting is needed here — contrast `hillshade`'s WAC_EMP source, normalized to a fixed reference
  geometry and relit for every render (see [`hillshade.md`](hillshade.md)).

## Processing

1. `crop` is reprojected onto the map via ISIS's native Pushframe camera model (`cam2map`), not
   `mapproject`/CSM — same reason as `crop`'s reprojection, see [`crop.md`](crop.md).
2. That reprojected imagery replaces the Lunaserv/Astropedia ortho as `sat_sim`'s `--ortho` input,
   rendered through the exact same `Camera` as `hillshade` — no relighting step.

Byte-identical pixel grid to `hillshade` by construction, so no separate basemap validation is
needed — see `notebooks/image_generation.py`'s Phase 8.
