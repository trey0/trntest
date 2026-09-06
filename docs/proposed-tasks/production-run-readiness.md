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

## Latitude coverage: ~30-45% of the manifest will fail on a known, expected limit

`notebooks/dataset_manifest.csv`'s 81 rows span `center_lat_deg` from -68.2° to +73.2°. WAC_EMP
(the live default ortho source) only covers ±60°:

- 24 of 81 rows have `|center_lat_deg| > 60` — guaranteed `ValueError` ("beyond WAC_EMP's ±60.0 deg
  coverage").
- 36 of 81 have `|center_lat_deg| > 50` — at risk once the camera footprint's own padding is added.

This is an already-known, already-documented limit (`WAC_EMP_MAX_ABS_LATITUDE_DEG`,
`docs/data-sources/wac-emp-pds4.md`), not a bug — but worth pre-filtering the manifest to
low-latitude rows before a production run rather than discovering the failure rate one entry at a
time.

## Open, unresolved correctness risk: a second, different failure mode

Populating a *different* multi-entry dataset earlier this session (`orbit_sequence_dataset`, from
`select_datasets.py`) hit `CPLE_AppDefinedError: Invalid dataset dimensions: 0 x N` on 5 of 10 tried
entries — at latitudes well inside ±60°, so *not* explained by the limit above. Flagged in
`docs/proposed-tasks/open-items.md` but not root-caused. Whether this recurs in `trn_dataset`'s own
remaining 79 rows is an open question, not yet checked — a real unknown, not just a theoretical risk,
since it hit half the entries tried in the one other real multi-entry dataset attempted.

## This session's new features are untested past 2 entries

`overview_map`'s per-entry footprint-polygon labels (`_upper_right_label_point`, `darkred` outlines)
and `write_index()`'s overall runtime have only been exercised at n=2. Nothing specific is known to
be wrong at larger scale, but nothing has confirmed it's right either — e.g. whether footprint labels
stay legible with 50+ overlapping entries on one global map, or whether `write_index()`'s per-entry
`Camera` rebuild for the overview map takes an acceptable amount of wall-clock time at that count.

## Recommended sequencing

1. Free disk space first: at minimum reclaim the ~7.9GB of orphaned worktree `output/` (with the
   user's go-ahead); reconsider whether the full 81-row manifest is the right scope at all given the
   remaining headroom, versus a deliberately-chosen low-latitude subset.
2. Pre-filter `trn_dataset`'s manifest to `|center_lat_deg| <= 50` (or similar) before a real run,
   rather than hitting the known WAC_EMP limit one entry at a time.
3. Run a small trial (10-20 entries from that filtered set) first — not the full run — specifically
   to check whether the `CPLE_AppDefinedError` bug recurs here, and to get a real per-entry timing
   number for this dataset's own geometry before committing to a much larger batch.
4. Follow `docs/batch-generation.md`'s existing guidance for the real run:
   `populate_via_workers()`, not sequential `populate()`; `write_index=False` for every call in an
   incremental loop except the last, since `write_overview_map`'s default `True` rebuilds cameras
   for the *entire* already-populated portion on every call otherwise.

Once this run happens (or the scope is deliberately narrowed and documented elsewhere), fold
whatever's still true into `README.md`'s Status section and delete this file.
