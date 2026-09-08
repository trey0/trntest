# TRN test image generators

Three interchangeable candidates for terrain-relative navigation (TRN) test imagery, all posed by
the real LRO SPICE trajectory at one EDR product's timestamp. Implemented as
`src/trntest/trn_products.py`'s `TrnTestHillshadeImage`/`TrnTestCropImage`/`TrnTestReprojectImage`;
generated and validated together by `notebooks/image_generation.py`.

| Generator | Data sources | Main processing steps | Purpose |
|---|---|---|---|
| [`hillshade`](generators/hillshade.md) | Astropedia GLD100 DEM, WAC_EMP PDS4 reflectance (fixed geometry) | Hapke relight, `sat_sim` render, `cam_gen` CSM sidecar | Synthetic image from real terrain, posed by the real trajectory |
| [`crop`](generators/crop.md) | Real WAC EDR (LROC) | ISIS3 `lrowac2isis` -> `spiceinit` -> `lrowaccal` -> `framestitch` -> `crop` | The real spacecraft image itself, calibrated and geometrically usable |
| [`reproject`](generators/reproject.md) | `crop`'s calibrated I/F (real acquisition geometry) | `cam2map` reproject, `sat_sim` render (no relighting) | Isolates the effect of texture source alone, geometry held fixed |

Each generator's doc has the full data-source/processing detail. `README.md`'s Source files
table covers the underlying modules (`dem_ortho.py`/`hapke.py`, `render.py`, `isis_wac.py`, `trn_products.py`).

See [`resolution-investigation.md`](resolution-investigation.md) for why `crop` used to visibly
outresolve `hillshade`/`reproject` — largely `sat_sim`'s fixed render size, not a data-source limit —
and how `config.DEFAULT_IMAGE_SIZE` was chosen to close most of that gap.

## EDR-free entries: `TrnTestEntrySpice`

The three generators above are all properties of one `TrnTestEntry`, built from a real WAC EDR
(`TrnTestEntryEdr`). `src/trntest/trn_dataset.py` also has a second, EDR-free entry kind,
`TrnTestEntrySpice`: posed purely from SPICE trajectory data at an arbitrary, SPICE-resolvable time,
with intrinsics reused from an existing `.tsai` (typically one `build_camera`'s own default
`fixed_sensor=True` path already produced). Only `hillshade` is supported for this kind — `crop`/
`reproject` fundamentally need a real EDR's own pixel data, which this kind never fetches. Each
entry's `primary_generator` (`"hillshade"` here, `"reproject"` for a normal `TrnTestEntryEdr`)
names which product `entry.primary_image` resolves to — the accessor other code (`report.py`'s
`primary_overlay`/`primary_zoom_blink`) uses instead of hardcoding a generator name, so it works for
either kind. See `notebooks/spice_entry_poc.ipynb` for a proof-of-concept dataset (5 entries along
one real orbit) and `camera.build_spice_camera`'s own docstring for the pose-accuracy tradeoff this
makes by having no real crop to refine the boresight re-aim against.
