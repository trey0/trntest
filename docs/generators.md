# TRN test image generators

Three interchangeable candidates for terrain-relative navigation (TRN) test imagery, all posed by
the real LRO SPICE trajectory at one EDR product's timestamp. Implemented as
`src/trntest/trn_dataset.py`'s `TrnTestHillshadeImage`/`TrnTestCropImage`/`TrnTestReprojectImage`;
generated and validated together by `notebooks/image_generation.py`.

| Generator | Data sources | Main processing steps | Purpose |
|---|---|---|---|
| [`hillshade`](generators/hillshade.md) | Astropedia GLD100 DEM, WAC_EMP PDS4 reflectance (fixed geometry) | Hapke relight, `sat_sim` render, `cam_gen` CSM sidecar | Synthetic image from real terrain, posed by the real trajectory |
| [`crop`](generators/crop.md) | Real WAC EDR (LROC) | ISIS3 `lrowac2isis` -> `spiceinit` -> `lrowaccal` -> `framestitch` -> `crop` | The real spacecraft image itself, calibrated and geometrically usable |
| [`reproject`](generators/reproject.md) | `crop`'s calibrated I/F (real acquisition geometry) | `cam2map` reproject, `sat_sim` render (no relighting) | Isolates the effect of texture source alone, geometry held fixed |

Each generator's doc has the full data-source/processing detail. `README.md`'s Source files
table covers the underlying modules (`lunaserv.py`, `render.py`, `isis_wac.py`, `trn_dataset.py`).

See [`resolution-investigation.md`](resolution-investigation.md) for why `crop` used to visibly
outresolve `hillshade`/`reproject` — largely `sat_sim`'s fixed render size, not a data-source limit —
and how `config.DEFAULT_IMAGE_SIZE` was chosen to close most of that gap.
