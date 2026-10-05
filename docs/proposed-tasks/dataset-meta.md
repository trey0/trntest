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

## How a pixel is resolved

The same work `dem_sources.mosaic_elevation` does today, in a fixed order with a stated contract
for each step. The default path (no mitigations) costs what today's precedence merge costs. A
mitigation does work only inside its own region.

1. **Source layers.** Each source is resampled onto the output grid on its own, with `NaN` where it
   has no valid data. A source's own tiles are merged here as hard cuts (SLDEM's 45°/30° tile seams
   live entirely inside this step).
2. **Precedence merge.** Each pixel comes from the first source, in list order, that's valid there.
3. **Mitigations**, seam by seam, each confined to its declared region (below).
4. **Hole fill** (`dem_mosaic --hole-fill-length`) of whatever is still `NaN`.

Where an output pixel comes from is then: the precedence winner, unless it falls in a mitigation's
region, in which case that method's docstring says what it does there.

## Seams and mitigations

### Coordinate seams vs. data seams

`docs/map-seams.md` principle 2 splits seams into two kinds. Only one of them gets a mitigation:

- **Coordinate seams** (branch cuts, a raster's own 0°/360° wrap, read-window placement) are our
  code's bugs. They are fixed in the reader (`geo_utils.read_eqc_raster_to_local_grid_array`) and
  take no parameters. A longitude branch cut therefore never appears in the record. The code version
  covers it.
- **Data seams** are defects in, or disagreements between, source data. They occur at both latitudes
  and longitudes, so the current `lat_seams` name is too narrow. Today's real examples:
  - SLDEM2015 ↔ GLD100 at ±60° (mitigated).
  - WAC_EMP equirectangular ↔ polar at ±60° (mitigated).
  - GLD100's internal lines at ±60° and at 90°/270° longitude (unmitigated).

### Seams: where they are

A seam is a fact about the sources' coverage, defined once, by name, next to the sources. Its
location is one coordinate of a named reference frame, so each seam has a **low side** and a **high
side** (smaller and larger values of that coordinate):

```python
SEAMS = {
    "sldem2015|gld100@abslat60": Seam(frame="geographic", coord="abs_lat", value=60.0,
                                      low="sldem2015", high="gld100"),
    "gld100@lon90": Seam(frame="gld100_eqc", coord="lon", value=90.0, low="gld100", high="gld100"),
}
```

Frames are named CRSes defined once in `geo_utils`. Which source lies on which side is in the seam,
so a reader of the record looks it up there.

### Mitigation methods: what's done at a seam

A short registry of named methods, each with named parameters and a version. A seam's mitigations
are a list of `(method, parameters)`; **an empty list, or no entry, is a hard cut.**

**Generic where it's cheap, specific where it isn't.** A method takes its seam as an argument and
addresses the two sides as `low`/`high`, never by source name, so the same method serves any seam.
Where a parameter is in native pixels, it means the native pixels of whichever source is on that
side. A method that models one archive's particular defect is allowed to be specific to that
source; it says so in its name.

| Method | Parameters | Today's use |
|---|---|---|
| `reject_fill@1` | `low_native_px`, `high_native_px`, `max_search_dest_px` | `LatSeam.reject_deg` |
| `linear_feather@1` | `side` (`low`/`high`), `width_m` | `LatSeam.feather_deg` |
| `wac_emp_edge_model@1` | the module's zone widths, WAC_EMP native px | `wac_emp_edge_correction.py` |

**Each method's docstring states its regions**, in terms of its seam and parameters:

- **Write region:** the only pixels it may change. `reject_fill`: the band from `low_native_px`
  below the seam to `high_native_px` above it. `linear_feather`: `width_m` on `side`, where both
  sources are valid.
- **Read margin:** how far beyond the write region it reads. `reject_fill`: `max_search_dest_px`.
- **AOI-dependent: yes/no.** Whether a ground pixel's result depends on what else the AOI contains.
  `wac_emp_edge_model` fits its correction from the AOI's own pixels, so yes.

This is documentation, not runtime enforcement. Conformance is checked in tests for free: the seam
probes already render each seam with and without mitigations, and the two must agree outside the
write regions.

**Units follow what the parameter covers.** A defect band is a count of the defective source's
native pixels; a fill reach is output pixels; a smooth transition is meters. `LatSeam` uses degrees
throughout, which doesn't fit any of them: its reject band covers GLD100's defective rows (~4.5
GLD100 pixels at 0.015°) but was tuned in output pixels (~7 at 100 m), so changing
`dem_target_gsd_m` changes what it means.

### Where seams meet

Mitigations run in a priority order, as sources do: the seams' order in the mosaic definition, then
each seam's own list order. Where two write regions overlap, the higher-priority one's writes stand
(apply in reverse priority order, so it runs last). That's the default; a junction that needs more
gets its own method.

At today's junctions it never comes up. At (60°N, 0°) the other seam is SLDEM's own tile seam, which
step 1 resolves, so only one mitigation is active. The probes passing at all 16 ±60° corners confirms
it. Overlapping regions first become possible in Milestone 2, where the polar products' corners
reach ~48° and three sources can overlap near ±60°.

### What changes in today's output

Mapping `LatSeam` onto these methods changes the DEM slightly. Its reject band also discards SLDEM's
clean side: not a defect, but a way to spread GLD100's local disagreement over ~7 px. Under the new
methods, that's either `low_native_px > 0`, which states the choice, or dropped if the feather
covers it. Either way, rerun the seam probes and re-tune the widths.

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
        "sldem2015|gld100@abslat60": [
          {"method": "reject_fill@1", "low_native_px": 4, "high_native_px": 5, "max_search_dest_px": 20},
          {"method": "linear_feather@1", "side": "low", "width_m": 3000}
        ]
      },
      "hole_fill": {"method": "dem_mosaic_hole_fill@1", "length_px": 50}
    },
    "reflectance": {
      "source": "wac_emp_pds@1",
      "seams": {"wac_emp_equirect|wac_emp_polar@abslat60": [{"method": "wac_emp_edge_model@1"}]}
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
- **Only mitigated seams appear.** A seam the dataset's sources cross with no mitigations is a hard
  cut and needs no entry. Known-defective unmitigated seams (GLD100's own lines) are listed in the
  seam registry with a defect note, which is where a "known uncorrectable defect" caution
  (`open-items.md`) can be read from; the record doesn't need to repeat them.
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
   with `low`/`high`, unit-suffixed parameters; re-tune against the seam probes, and add the
   "mitigated and unmitigated passes agree outside write regions" check to the probe tests.
2. `generation_record(config)`: resolves the full record; generator flags moved into `TrntestConfig`.
3. Written to `dataset_meta.json` by `create()` and to each product sidecar; staleness check in
   `open()`/`populate()`.
4. Hash-based intermediate names, starting with the DEM.
