# Plan: what a dataset's metadata records

Status: **proposal, nothing implemented.** Today `dataset_meta.json` holds only `entry_kind` and
`primary_generator`. Nothing records which DEM, reflectance source, seam mitigations or shading
options built a dataset's products. This plan covers *what* the record should contain. How to get
there step by step is secondary and is sketched at the end. Once settled, fold the schema into
`docs/intermediate-product-discipline.md`'s dataset-layout section and delete this file.

## What the record is for

1. **Identity.** Say what a product *is*, in enough detail that two datasets (or two runs of one)
   can be checked for equivalence. Content hashes for intermediate filenames come from the same
   record (see "Identity and hashing" below).
2. **Comparison.** Diff two datasets' records to see exactly what differs, e.g. `trntest2` vs. a
   `trntest2` rebuilt on SLDEM2015.
3. **Staleness.** On `open()`, notice that the current config would build something different from
   what's already on disk, before a resumed `populate()` mixes the two.
4. **Reporting.** Show in each report what generated it.

It is not for documenting the source data. Facts about sources live in `docs/data-sources/` and in
the code's source registry.

## Principles

1. **Record inputs we choose, not facts we don't control.** A field belongs in the record only if
   changing it would change the output *and* it's our choice. Applied to a first-draft DEM record:

   | Draft field | Kind | Keep? |
   |---|---|---|
   | `dem_source`, which sources and in what order | functional, chosen | yes |
   | seam mitigation parameters | functional, chosen | yes |
   | `dem_target_gsd_m` | functional, chosen | yes |
   | `pixel_m`, `n_tiles`, `lat_range_deg`, units | descriptive: fixed by which product it is | no |
   | `url` | location, not content (a mirror serves the same bytes) | no |

   Some descriptive facts do drive behavior. `pixel_m` decides averaging vs. bilinear, for example.
   But they follow from the source's identity, so recording the identity covers them.
2. **Reference named registry entries; don't inline definitions.** Sources, seams and mitigation
   methods are each defined once in code under a stable name. The record says which names are in use
   and with what parameters. A reader looks the name up in the code, or in the doc for that source.
3. **Every registry entry carries a version token.** Principle 1 of
   `intermediate-product-discipline.md` says identity covers the code, not just the parameters. If a
   method's behavior changes with no parameter change, bump its version (`reject_fill@1` →
   `reject_fill@2`) and the record changes with it. The same goes for a source superseded by a new
   release.
4. **Record resolved values, never "default".** Defaults move, so "default" doesn't identify anything.
5. **Leave out housekeeping that doesn't change content:** paths (`cache_root`, `output_dir`), URLs,
   `delete_*` flags, and diagnostic-only outputs (`crop_map_write_fill_mask`).
6. **The dataset record is intent; each product records what actually happened.** A resumable
   `populate()` can span commits and config edits. `dataset_meta.json` holds the configuration the
   dataset was created with. Each product's sidecar holds its own resolved generation record plus the
   git commit (and dirty flag) that produced it. Comparing the two catches drift.

## Seams and mitigations

### Coordinate seams vs. data seams

`docs/map-seams.md` principle 2 splits seams into two kinds. Only one of them gets a mitigation:

- **Coordinate seams** (branch cuts, a raster's own 0°/360° wrap, read-window placement) are our
  code's bugs. They are fixed in the reader (`geo_utils.read_eqc_raster_to_local_grid_array`) and
  take no parameters. A longitude branch cut therefore never appears in the record. The code version
  covers it.
- **Data seams** are defects in, or disagreements between, source data. These get mitigations, and
  they occur at both latitudes and longitudes. Today's real examples:
  - SLDEM2015 ↔ GLD100 at ±60° (mitigated).
  - WAC_EMP equirectangular ↔ polar at ±60° (mitigated).
  - GLD100's internal lines at ±60° and at 90°/270° longitude (unmitigated).

  So the current `lat_seams` name is too narrow.

### Seam registry: where seams are

A seam is a fact about two sources' coverage, so it's defined next to the sources, once, by name.
Each seam's location is given in a named reference frame:

```python
SEAMS = {
    "sldem2015|gld100@lat60": Seam(
        sides=("sldem2015", "gld100"),
        frame="geographic",  # a named CRS defined once in geo_utils
        coord="abs_lat",
        value=60.0,
    ),
    "wac_emp_equirect|wac_emp_polar@lat60": Seam(...),
    "gld100@lon90": Seam(sides=("gld100", "gld100"), frame="gld100_eqc", coord="lon", value=90.0),
}
```

`sides` names which source lies on which side. That settles the asymmetry question: a mitigation's
per-side parameters are keyed by source name, not by "equatorward"/"poleward".

### Mitigation registry: what's done at a seam

A short list of named methods. Each one has named, unit-suffixed parameters and a version. A method
may be tuned to one particular seam; that's acceptable once it has a name and a place in the
registry. A new mitigation means a new registry entry, not a new parameter on an old one.

| Method | Parameters | Today's use |
|---|---|---|
| `hard_cut@1` | none | every SLDEM tile seam, `"sldem2015_gld100_hardcut"` |
| `reject_fill@1` | `band_<side>` per side; fill: IDW, `max_search_px` | `LatSeam.reject_deg` |
| `linear_feather@1` | `width_<side>` (side the blend lies on) | `LatSeam.feather_deg` |
| `wac_emp_edge_model@1` | the module's zone widths in WAC_EMP native px | `wac_emp_edge_correction.py` |

Today's `LatSeam` becomes two methods applied in order (reject/fill, then feather). Splitting it lets
each be enabled or retuned on its own.

**Units matter, and `LatSeam` currently picks the wrong ones.** The reject band exists to cover
GLD100's defective rows, which are a fixed number of GLD100 native pixels (~4.5 at 0.015°). It's
specified in degrees, though, and was tuned in destination-grid pixels (~7 px at 100 m). Change
`dem_target_gsd_m` and the band no longer means what it was tuned to mean. A parameter should take
the unit of what it covers: `band_gld100_native_px` for the defect, `max_search_dest_px` for the
fill reach. Integer pixels in a named projection are the natural unit for most such parameters. The record
then reads unambiguously without a general spec language.

## Proposed record

Generation settings grouped by the stage they affect. Each group is the input to one family of
intermediates, which keeps hashing straightforward (next section).

```json
{
  "schema_version": 1,
  "dataset": {"entry_kind": "edr", "primary_generator": "reproject", "created_utc": "2026-10-04T18:00:00Z",
              "created_commit": "e913f73", "created_dirty": false},
  "generation": {
    "camera": {"image_size": 1316, "wac_vis_color_fov_deg": 61.4, "wac_ck_source": "isis_resolved"},
    "dem": {
      "target_gsd_m": 100.0, "padding_fraction": 0.3,
      "sources": ["sldem2015_512@1", "gld100@1"],
      "seams": {
        "sldem2015|gld100@lat60": [
          {"method": "reject_fill@1", "band_sldem2015_dest_px": 2, "band_gld100_native_px": 5, "max_search_dest_px": 20},
          {"method": "linear_feather@1", "width_sldem2015_deg": 0.1}
        ]
      },
      "hole_fill": {"method": "dem_mosaic_hole_fill@1", "length_px": 50}
    },
    "reflectance": {
      "source": "wac_emp_pds@1",
      "seams": {"wac_emp_equirect|wac_emp_polar@lat60": [{"method": "wac_emp_edge_model@1"}]}
    },
    "shading": {"model": "hapke@1", "along_track_correction": true, "real_params": true,
                "calibration_wavelength_nm": 643, "cast_shadows": "horizon_sweep@1"},
    "crop": {"map_projection": "wac_resample@1"}
  }
}
```

Choices worth checking:

- **The `dem_source` config key doesn't appear in the record.** It becomes a named preset that
  expands into `sources` + `seams`. Presets are a convenience for selecting a configuration; the
  record holds the expansion.
- **A seam that's present but unmitigated is still listed**, as `hard_cut@1`, whenever a dataset's
  sources include one. That records "we knew and chose not to." Today that includes GLD100's own
  lines. That is also where a "known uncorrectable defect" caution (`open-items.md`) can be read from.
- **Shading flags move out of module constants.** `hapke.DEFAULT_*` and `dem_ortho.DEFAULT_ORTHO_SOURCE`
  aren't in `TrntestConfig` today. They need to be resolvable settings so the record can be built in
  one place. Their filename suffixes (`_atc`, `_castshadow2`, ...) are a hand-maintained version of
  what the hash would do.

## Identity and hashing

Each intermediate's filename carries a short hash of exactly the record groups its content depends
on. That's principle 1 of `intermediate-product-discipline.md`, made mechanical:

| Intermediate | Depends on |
|---|---|
| filled DEM | `dem` (+ the entry's grid, already per-entry) |
| shaded ortho | `dem`, `reflectance`, `shading` |
| crop map projection | `crop`, and the DEM grid (not its values) |
| hillshade render | all of the above, `camera` |

This replaces both the bare `_dem-<source>` suffix (which goes stale silently if a seam is retuned)
and, in time, the hand-built `ortho_shaded_*` suffix chain. Dataset-level product names
(`hillshade/<id>_hillshade.tif`) stay unhashed: one dataset has one generation record, and the
staleness check (purpose 3) guards it.

## Open questions

- **Staleness on `open()`: warn or refuse?** Proposed: refuse a `populate()` whose resolved record differs
  from the dataset's (with an explicit override), and only warn on read-only use.
- **Legacy datasets** (`trntest1`, `trntest2`): backfill a best-effort record marked
  `"reconstructed": true`, or leave them without one until they're regenerated (which they need anyway
  for the half-pixel DEM fix).
- **Source versions:** is `sldem2015_512@1` enough, or should a source record a content checksum of
  its cached tiles? Checksumming ~45 GB is slow. The version token is probably enough if a changed
  release always gets a new name.
- **Granularity of `camera`:** SPICE kernel set/versions arguably belong there too; not addressed.

## Incremental path

1. Seam and mitigation registries in code; `LatSeam` split into `reject_fill` + `linear_feather`
   with per-side, unit-suffixed parameters (re-verify the seam probes still pass).
2. `generation_record(config)`: resolves the full record; generator flags moved into `TrntestConfig`.
3. Written to `dataset_meta.json` by `create()` and to each product sidecar; staleness check in
   `open()`/`populate()`.
4. Hash-based intermediate names, starting with the DEM.
