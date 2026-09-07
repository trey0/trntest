# Production run readiness: scaling `trn_dataset`

`docs/report-generation.md`'s four pages (nav bar, overview map, overview table, per-entry
report) are all built now, but tested only against `trn_dataset`'s own 2 hand-picked entries. The
natural next step is a real "production run" — populating more of `trn_dataset`'s own manifest
(`notebooks/dataset_manifest.csv`, 81 rows total, currently only 2 populated) at scale. This is a
readiness assessment done 2026-09-06, before attempting that, so a future session doesn't have to
re-derive it. Nothing below has been acted on yet — it's a list of what to check/fix first, plus a
recommended sequencing.

## Disk space: the most urgent blocker, and it's already tight today

```
/mnt/trntest        98G total,  76G used,  17G available (82% full)
  cache/            59G  (naif 18G, pds_wac_emp 16G, isisdata 13G, astropedia/GLD100 9.8G, ...)
  output/           14G  (spread across worktrees, several orphaned -- see below)
```

**Per-entry disk footprint**, measured directly from the 2 already-populated entries:
**440MB–886MB each** (avg ~660MB), overwhelmingly `_work/` — the ISIS/ASP intermediates (stitched
cubes, DEM/ortho tiles, pre-copy render output). Per `docs/intermediate-product-discipline.md`,
`_work/` is retained by design; confirmed no pruning mechanism exists anywhere in the codebase today
(the doc's mention of "routine `_work/` pruning" is aspirational, not implemented) — this cost
accumulates and stays.

**Extrapolated to the full 81-row manifest**: of those 81, 24 sit above WAC_EMP's ±60° coverage
limit and are guaranteed to fail outright (see below), so a full run would really only populate
~55-57 entries. At ~660MB each that's **~37GB** for successful entries alone, plus smaller partial
footprints for the ~24 failures (they fail after `crop` succeeds but before `hillshade`) — call it
**40-45GB total**. That's ~2.5x the 17GB currently free — a full run as-is would fill the volume
before finishing.

**Easy win, not yet acted on**: ~7.9GB sits in orphaned `output/` folders from worktrees that no
longer exist (confirmed via `git worktree list` — only the main checkout and one active worktree
exist; `notebooks-tone-structure-37bc88`, `phase5_validation`, `source-code-org-analysis-52dadf`,
`phase5_validation_dem_ortho`, `agents-task-granularity-docs-5c060f`,
`docs-proposed-tasks-style-0defc6`, `crater-sharpness-grading-dad2c9`,
`crater-sharpness-parallel-workers-83efce`, and a few smaller ones are all leftover). Reclaiming
these takes free space from 17GB to ~25GB — still not enough for the full 81-row run, but a real
buffer. Ask the user before deleting (these are other past worktrees' output, not this session's own
scratch).

**Update, same day**: the "`_work/` is retained by design, no pruning mechanism exists" statement
above no longer holds for the `crop` generator's own `_work/<entry>/isis/` subtree specifically —
measured at ~223-260MB/entry (raw+calibrated split cubes, the full stitched swath, plus the 14MB
crop), of which only the crop itself is ever read again once generated. `isis_wac.
ensure_crop_for_camera` now publishes the crop to a new permanent, cross-dataset cache tier
(`cache/wac_crop/<edr_product>_crop.cub`) and wipes the rest of `_work/<entry>/isis/` by default
(`config.delete_isis_intermediates`) — per-entry footprint there drops to ~0 post-generation. See
`docs/caching.md`'s "WAC crop caching" section. Doesn't change the `_work/` estimate for the *other*
per-entry subtrees (DEM/ortho tiles, pre-copy render output) — those are still retained as before, so
the ~660MB/entry average above should be revised downward but not eliminated once someone re-measures
against a fresh entry.

## Latitude coverage: mostly resolved since this assessment was written

`notebooks/dataset_manifest.csv`'s 81 rows span `center_lat_deg` from -68.2° to +73.2°. At the time
this assessment was written, WAC_EMP (the live default ortho source) only covered ±60°, so 24 of 81
rows (`|center_lat_deg| > 60`) were guaranteed to fail outright.

**Resolved in a later session**: `wac_emp_tile_id_for_bbox` now also fetches WAC_EMP's own
polar-stereographic tile pair (`P900N`/`P900S`) for a footprint entirely beyond 60° in one
hemisphere — see `docs/data-sources/wac-emp-pds4.md`'s polar-tile bullet for the confirmed
format/coverage facts. Since GLD100 (the DEM source `fetch_dem_and_ortho` calls first) still caps
out at ±79° and this manifest's own range (-68.2°..+73.2°) fits entirely inside that, essentially all
24 previously-failing rows should now succeed — confirmed end to end on two real high-latitude
candidates from a *different* dataset (`orbit_sequence_dataset`'s `M1314069739CE`/`-72.6°` and
`M1314073855CE`/`70.8°`), but **not yet re-verified against `trn_dataset`'s own manifest
specifically** — a real remaining risk is any row whose padded AOI happens to straddle the exact 60°
equirect/polar seam (still an unmosaiced hard `ValueError`, by design), which this pre-filtering pass
would still need to catch.

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

1. Free disk space first: at minimum reclaim the ~7.9GB of orphaned worktree `output/` (with the
   user's go-ahead); reconsider whether the full 81-row manifest is the right scope at all given the
   remaining headroom, versus a deliberately-chosen low-latitude subset.
2. Pre-filter `trn_dataset`'s manifest to exclude any row whose padded AOI would straddle the exact
   60° equirect/polar seam (now the only remaining hard latitude cutoff within GLD100's own ±79°
   DEM coverage — see "Latitude coverage" above) before a real run, rather than hitting it one entry
   at a time.
3. Run a small trial (10-20 entries from that filtered set) first — not the full run — to get a real
   per-entry timing number for this dataset's own geometry before committing to a much larger batch.
4. Follow `docs/batch-generation.md`'s existing guidance for the real run:
   `populate_via_workers()`, not sequential `populate()`; `write_index=False` for every call in an
   incremental loop except the last, since `write_overview_map`'s default `True` rebuilds cameras
   for the *entire* already-populated portion on every call otherwise.

Once this run happens (or the scope is deliberately narrowed and documented elsewhere), fold
whatever's still true into `README.md`'s Status section and delete this file.
