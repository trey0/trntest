# GLD100 row-banding artifact: root-cause investigation and next steps

Continuation of the tangent first found in `docs/history.md`'s Phase 121 (2026-09-12), during
`docs/proposed-tasks/isis-shadow-masking.md`'s step-1 spike. That session established the banding
is real and traces to GLD100's own upstream production (confirmed via an independently-fetched
NASA PDS source tile). This document picks up the follow-on question -- is it detectable and
correctable, and what's actually causing it -- with a full session's worth of findings that
meaningfully update, and partly overturn, that earlier framing.

## What this session found (2026-09-12 evening into 2026-09-13)

- Reconstructed Phase 121's hillshade-domain row-peak detector as real code (it only ever existed
  in disposable scratch cells) -- see `notebooks/gld100_banding_investigation.py`.
- Every elevation-magnitude-based detector tried against that detector's flagged rows -- raw
  Hampel z-score, mean-based fold-and-stack, median-based fold-and-stack (with a non-flagged
  control confirming the same shapes appear in ordinary terrain), a 10x10 grid of 100 individual
  raw column profiles -- failed to find any discrete elevation-domain signature distinguishing
  flagged rows from ordinary terrain.
- Recovered last night's actual output images and scripts from the shared `scratch/` dir. These
  revealed that the reconstructed hillshade detector had been finding the wrong rows: its flagged
  rows (e.g. row 962) turned out to be a real but *different* artifact -- diagonal, not
  horizontal, and a "negative" anomaly (unexpected lit pixels within an otherwise-shadowed region)
  -- easy to conflate with the actual target but not the same thing.
- Direct visual inspection (by the user, not any detector) correctly identified the real target
  phenomenon: a perfectly horizontal, single-row-wide line of shadow pixels within an otherwise-lit
  region. Pinned down precisely at row 1016 of `M1327218454CE`'s 2424x2437 local-Orthographic DEM
  (cols ~1460-1700+, shadow fraction 0.146 there vs. ~0 at every immediate neighboring row).
- Even at this confirmed-correct row and column range, the elevation diff shows no discrete
  anomaly (median ~1.0 m vs. ~0.8 m / ~0.6 m at immediate neighboring-row transitions) --
  ruling out a simple local DEM step/bias as this streak's cause.
- Tested `PRESET=ACCURATE` (disables `shadow`'s `SHADOWMAP`/`LIGHTCURTAIN` caching, which the
  application's own docs admit causes "approximately 1-2 pixels" of shadow-boundary inaccuracy)
  against the identical prepared cube:
  - A **secondary, weaker** streak (row 1023) vanished completely under `ACCURATE` -- a confirmed
    caching artifact.
  - The **primary** streak (row 1016) did *not* disappear -- it got stronger (shadow fraction
    0.146 -> 0.313), while every neighboring row was unchanged in both runs. That argues against
    caching as row 1016's cause: the higher-fidelity, uncached computation found *more* columns
    genuinely marginally shadowed there, not fewer.

## What this means

Two distinct phenomena, not one:

1. **Caching-induced spurious shadow pixels** (row-1023-type). Confirmed, understood, and cheaply
   fixable: switch `shadow` calls to `PRESET=ACCURATE` (or `CACHEINTERPOLATEDVALUES=true` under
   `BALANCED`) as the default, accepting the CPU cost the application's own docs note ("very heavy
   CPU usage but low memory usage").
2. **A genuine, tiny grazing-occlusion effect** (row-1016-type). Elevation-magnitude statistics
   can't isolate it -- the true perturbation appears smaller than this candidate's own terrain
   roughness noise floor (~4-5 m median row-to-row diff at this location) -- but it survives, and
   strengthens, under the least-approximated ray tracing available. That's consistent with real
   physics, not noise or a processing bug: something in the real terrain is genuinely, if very
   marginally, occluding these specific pixels' view of the sun.

## Next steps

1. **Ship the caching fix.** Default any future `shadow` integration (including this spike, if
   reused) to `PRESET=ACCURATE`. Low-risk, already validated to eliminate at least one confirmed
   artifact class. Worth re-checking whether the remaining row-1016-type streaks are rare/small
   enough to just ignore once this alone is in place.
2. **Ray-trace a specific row-1016 pixel directly** -- the one diagnostic this session didn't get
   to. Sample the DEM's real surface height along the true 3D line-of-sight toward the sun
   (azimuth 224.44 deg, elevation 13.32 deg for this candidate) from a specific marginal pixel
   (e.g. row 1016, col 1500) to find whatever real terrain feature is grazing-occluding it. Either
   finds and explains the real occluder, or further rules out a physical cause if nothing is there
   even along the exact ray.
3. **Re-validate against a second candidate.** Everything above was checked against a single
   candidate (`M1327218454CE`, 13.6 deg sun elevation) -- confirm row-1016-type streaks recur (and
   that the `ACCURATE`-preset fix holds) on a different low-sun-elevation candidate before treating
   any of this as general.
4. **Revisit the original binary-mask streak-suppression idea** (this investigation's starting
   point) now that the picture is clearer: not needed for caching-induced streaks (just fix the
   preset); for row-1016-type streaks, suppression may not even be the right move if they're
   genuine shadow -- decide only after step 2 clarifies whether they're real occlusion or
   something else entirely.
5. **Fold into permanent docs once resolved**, per this repo's usual proposed-tasks convention:
   the caching-fix finding belongs in `docs/external-tools.md`'s `shadow` notes; whatever the
   ray-trace finds belongs in `docs/proposed-tasks/isis-shadow-masking.md` or
   `docs/data-sources/astropedia-gld100.md` depending on the answer. Delete this file once both
   are folded in.

## Where the work lives

- `notebooks/gld100_banding_investigation.py`/`.ipynb` -- the reconstructed hillshade-domain
  detector, the elevation-domain cross-checks (including the negative control against non-flagged
  rows), and the raw-column-grid diagnostics. Kept in the same disposable-but-committed spirit as
  `notebooks/isis_shadow_spike.py`.
- `scratch/isis_shadow_spike/` (shared scratch, **not** tracked in git) -- last night's original
  scripts and output images (`side_by_side_hillshade_vs_shadow.png`, `streak_strip_ticked.png`,
  `find_exact_line.py`, etc.) plus this session's `PRESET=ACCURATE` comparison
  (`balanced_vs_accurate_995_1030.png`, `rerun_accurate2.py`). Since `scratch/` isn't
  version-controlled, anything here could be lost if the shared workspace is ever cleaned --
  worth re-deriving or copying into a tracked location before relying on it in a future session.
