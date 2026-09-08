# Production run readiness: scaling `trn_dataset`

`docs/report-generation.md`'s four pages (nav bar, overview map, overview table, per-entry
report) are all built now, but tested only against `trn_dataset`'s own 2 hand-picked entries. The
natural next step is a real "production run" — populating more of `trn_dataset`'s own manifest
(`notebooks/dataset_manifest.csv`, 81 rows total, currently only 2 populated) at scale. This is a
readiness assessment done 2026-09-06, before attempting that, so a future session doesn't have to
re-derive it. Nothing below has been acted on yet — it's a list of what to check/fix first, plus a
recommended sequencing.

## Disk space: resolved — no longer a blocker for a full run

This section originally flagged disk as "the most urgent blocker" at 17GB free against an estimated
40-45GB full-run cost. Both halves of that math turned out stale by the time anyone re-measured
against a real, fresh entry (see below) — disk is no longer a real constraint on this manifest.

**Current state**: 24GB free (up from 17GB) — the orphaned-worktree `output/` reclaim this section
used to recommend as an "easy win" has since happened (`git worktree list` now shows only the main
checkout plus active worktrees, no leftovers).

**Real per-entry measurement** (not extrapolated — an actual fresh entry run through full `populate()`
default product types, `crop`+`hillshade`+`report`+`gallery`): **~114MB**, not the ~660MB this section
originally estimated. Two fixes account for nearly all of the gap, both already landed:

1. `isis_wac.ensure_crop_for_camera` publishes the crop to a permanent, cross-dataset cache tier
   (`cache/wac_crop/<edr_product>_crop.cub`) and wipes the rest of `_work/<entry>/isis/` by default
   (`config.delete_isis_intermediates`) — the raw/calibrated/stitched-cube cost drops to ~0/entry
   post-generation. See `docs/caching.md`'s "WAC crop caching" section.
2. `isis_wac.run_cam2map_for_crop`'s own intermediate cam2map cube (~59MB/entry — `cam2map` can only
   write a cube; `gdal_translate` then derives the actual, much-smaller published `.tif` from it) now
   lands in a real `TemporaryDirectory` instead of `_work/<entry>/crop/`, so it never persists. The
   `.msk` sidecar `gdal_translate` used to leave behind under an orphaned tmp name is also gone
   (`-mask none` — confirmed redundant with the source cube's own per-band `NoData` value, nothing
   lost).

**Updated full-run estimate**: with the seam/polar latitude fixes below, all 81 manifest rows are
viable candidates now (not just ~55-57) — at ~114MB/entry that's **~9GB** for a full run, comfortably
inside the 24GB free. Caveat: this is one real measurement from one typical, low-latitude entry
(`M1327210646CE`, 38.5°N) — an entry whose footprint mosaics across the 60° seam needs a second
WAC_EMP tile fetch and will cost somewhat more, but not by an order of magnitude.

## Latitude coverage: mostly resolved since this assessment was written

`notebooks/dataset_manifest.csv`'s 81 rows span `center_lat_deg` from -68.2° to +73.2°. At the time
this assessment was written, WAC_EMP (the live default ortho source) only covered ±60°, so 24 of 81
rows (`|center_lat_deg| > 60`) were guaranteed to fail outright.

**Resolved in a later session**: `wac_emp_tile_ids_for_bbox` now also fetches WAC_EMP's own
polar-stereographic tile pair (`P900N`/`P900S`) for a footprint entirely beyond 60° in one
hemisphere — see `docs/data-sources/wac-emp-pds4.md`'s polar-tile bullet for the confirmed
format/coverage facts. Since GLD100 (the DEM source `fetch_dem_and_ortho` calls first) still caps
out at ±79° and this manifest's own range (-68.2°..+73.2°) fits entirely inside that, essentially all
24 previously-failing rows should now succeed — confirmed end to end on two real high-latitude
candidates from a *different* dataset (`orbit_sequence_dataset`'s `M1314069739CE`/`-72.6°` and
`M1314073855CE`/`70.8°`), but **not yet re-verified against `trn_dataset`'s own manifest
specifically**.

**Resolved in a later session**: the 60° equirect/polar seam itself is no longer a hard failure.
`wac_emp_tile_ids_for_bbox` now returns every tile a padded AOI touches (mosaicking across the
equator, a 90°-lon zone boundary, or the equirect/polar split) instead of raising when it straddles
one — see `docs/data-sources/wac-emp-pds4.md`'s "Multi-tile mosaic" bullet. This was a real, common
case for this manifest specifically, not a theoretical edge case: 31 of its 81 rows have
`|center_lat_deg| > 55°`, close enough to the 60° line that any nonzero AOI padding pushes at least
one of them across it (`M1327218124CE` at 59.28° almost certainly does). No pre-filtering step is
needed for this anymore -- a straddling row now just costs one extra tile fetch instead of failing.

## Resolved: the second, different failure mode from `orbit_sequence_dataset`

Populating a *different* multi-entry dataset earlier this session (`orbit_sequence_dataset`, from
`select_datasets.py`) hit `CPLE_AppDefinedError: Invalid dataset dimensions: 0 x N` on 5 of 10 tried
entries — at latitudes well inside ±60°, so *not* explained by the limit above. Root-caused and fixed
in a later session: a longitude branch-cut bug in `ortho_wac_emp.reproject_wac_emp_reflectance_to_local_grid`
(PROJ normalizes longitude into (-180°, 180°] before applying a WAC_EMP tile's own
`central_meridian=0` formula, but the "225°" zone tiles' own georeferencing is written in unwrapped,
continuous longitude past ±180° — see `docs/data-sources/wac-emp-pds4.md`'s own bullet for the full
mechanism). Confirmed fixed on all 4 of the originally-affected entries this session could directly
retest (`M1314068239CE`, `M1314069246CE`, `M1314074526CE`, `M1314074818CE`), plus a synthetic
regression test (`tests/test_ortho_wac_emp.py`). Affects any AOI whose true longitude falls in the
Moon's "180-270°" zone (confirmed) — `trn_dataset`'s own 79 not-yet-populated rows should no longer
be at risk from this specific bug, though they haven't been individually retested.

## This session's new features are untested past 2 entries

`overview_map`'s per-entry footprint-polygon labels (`_upper_right_label_point`, `darkred` outlines)
have only been exercised at n=2 for actual *legibility* at scale — visually checked against the real
81-row manifest in a later session (all real, non-degenerate polygons, correctly clustered by orbit
pass, antimeridian wrap handled correctly) but not specifically evaluated for whether labels stay
readable with 50+ overlapping entries.

**Resolved in a later session**: `write_index()`'s per-entry `Camera` rebuild for the overview map —
this doc's own original concern about wall-clock time at scale — is gone. `overview_map.
plot_overview_map` now uses `camera.lightweight_footprint_lonlat_deg` (a cheap, ISIS-free SPICE
approximation) instead of `entry.camera`, for every entry regardless of population state. Measured
against the real 81-row manifest: ~370s cold (first-ever kernel furnish + per-entry EDR label
fetches, a one-time cost), ~1s warm. See `docs/proposed-tasks/open-items.md` for the one real
caveat this introduces (the approximation's calibration constants are provisional, measured from a
single candidate).

## Recommended sequencing

1. Run a small trial (10-20 entries) first — not the full run — to get a real per-entry timing number
   for this dataset's own geometry before committing to a much larger batch. No manifest pre-filtering
   is needed for the 60° equirect/polar seam anymore (see "Latitude coverage" above) -- a straddling
   row now mosaics instead of failing. Disk space is no longer a reason to narrow scope (see "Disk
   space" above) -- the full 81-row manifest fits comfortably.
2. Follow `docs/batch-generation.md`'s existing guidance for the real run:
   `populate_via_workers()`, not sequential `populate()`; `write_index=False` for every call in an
   incremental loop except the last, since `write_overview_map`'s default `True` rebuilds cameras
   for the *entire* already-populated portion on every call otherwise.

Once this run happens (or the scope is deliberately narrowed and documented elsewhere), fold
whatever's still true into `README.md`'s Status section and delete this file.
