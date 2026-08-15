# Caching strategy

Both external data sources (Lunaserv WMS, NAIF SPICE archive) are large; we only ever need a
sliver for one region/timestamp. Goal: never re-download the same bytes twice across container
rebuilds or notebook re-runs.

## Layout

A `cache/` directory (mounted as a Docker volume, so it survives container rebuilds) mirrors the
remote structure:

```
cache/
  naif/pub/naif/pds/data/lro-l-spice-6-v1.0/lrosp_1000/data/ck/...   (exact remote paths)
  naif/pub/naif/pds/data/lro-l-spice-6-v1.0/lrosp_1000/data/spk/...
  naif/.../extras/mk/...
  lunaserv/<layer>/<bbox>_<width>x<height>_<format>.tif
  lroc_edr/<volume>/DATA/<subdir>/<doy>/WAC/<product>.*
  isisdata/base/...   (ISIS's own mission-independent reference data)
  isisdata/lro/...    (LRO/WAC calibration files, and -- see below -- WAC CK kernels ISIS resolves)
  astropedia/Lunar_LRO_WAC_GLD100_DTM_79S79N_100m_v1.1.tif   (one whole file, ~10GB -- see below)
  isis_ck_resolution/<edr_product>.json   (persisted spiceinit CK resolution -- see below)
```

`isisdata/` is a fourth tree, alongside the three above: ISIS3's own reference data, fetched by
`isis_wac.ensure_isisdata()` for the ISIS/CSM WAC reprojection spike (see
`docs/data-sources.md`'s "ISIS3/CSM spike" section). Fully re-fetchable and safe to prune before
archiving, same as the other three -- `downloadIsisData ... --no-kernels` keeps the real one-time
download to ~5GB (not the ~30GB a plain, un-flagged `downloadIsisData` would pull), since
`spiceinit web=yes` covers the pointing/position role the larger, un-flagged `base` download would
otherwise be needed for.

Fetch helpers (`src/trntest/cache.py`) check "does this local mirrored path already exist" before
making any network request; if present, skip the request entirely.

## Retry/backoff/pacing policy, and failing loudly on a systemic fetch problem

`cache.cached_get` (the fetch path behind everything above except the Astropedia flat file, see
below) retries a failed real request up to a small, fixed number of attempts (short exponential
backoff, capped) before giving up -- not infinite/silent retrying, and not the old
skip-on-first-failure behavior either. A 429 specifically honors the server's own `Retry-After` if
it's short enough to wait out inline; a longer one (or a missing/malformed header) fails immediately
rather than blocking for however long the server asks. Every real request (never a cache hit) is also
paced by a small fixed delay, and all real requests in one process share one `requests.Session`
(connection keep-alive) rather than a fresh connection per call.

Once attempts are exhausted, `cached_get` raises `cache.FetchError`, not the raw underlying
exception -- deliberately a distinct type so a caller sweeping many items in a loop (e.g.
`dataset._evaluate_illuminated_candidates`'s per-candidate catalog sweep,
`dataset.generate_dataset`'s per-image batch) can tell "this fetch is systemically broken" apart from
an ordinary per-item problem and let it propagate to abort the whole operation, rather than logging
"skipping" and immediately firing the next of possibly hundreds of further requests at a server
that's already refusing them. That distinction exists because of a real incident, not a hypothetical:
a from-cold `select_dataset(max_search_days=7)` sweep made ~1600 sequential, unpaced requests to the
same PDS host with no backoff at all, and once the server started responding 429, the old
catch-any-exception-and-continue loop kept right on firing -- 1354 more 429s in the same run -- into
what turned out to be a full 1-hour, IP-scoped ban (confirmed via the response's own `Retry-After:
3600` header). See `docs/history.md`'s dated entry for the full incident and the fix.

Same underlying preference as the `spiceinit` case below (no silent retry loop, fail promptly instead)
-- extended here to *bounded* retries plus pacing/backoff, since the failure mode observed was a
genuinely unpaced burst tripping a rate limiter, not a single flaky call; the "signal failure on the
whole operation" half of that direction is unchanged.

## SPICE kernel selection (avoid over-pulling)

The NAIF archive's yearly metakernel is a **manifest**, not something to furnish/download in full —
CK (pointing) kernels dominate a year's data volume. Process:

1. Download (and cache) the one metakernel covering the year of our chosen EDR timestamp.
2. Parse it (it's a text kernel — `\begindata`/`\begintext` blocks listing `KERNELS_TO_LOAD`) to get
   the list of referenced kernel filenames.
3. Kernel filenames encode a date range (e.g. `..._2010074_2010080_...`) — pick out just the CK and
   SPK file(s) whose range covers our timestamp.
4. Always also fetch the small kernels that don't vary by date range within a mission phase: LSK,
   SCLK, PCK/lunar frame kernel, LRO FK, WAC IK.
5. Download only that selected set into `cache/naif/...` (mirrored paths), then `furnsh` just those.

## Observed result

For the demo's chosen 2019-11-30 timestamp, selecting only the `lrosc`/`lrolc` CK flavors (out of
five) for one 10-day chunk, plus the always-needed kernels, downloaded **~585 MB** total —
`spice_kernels.py`'s `select_kernels_for()` handles this. Note the single `lrosc` (spacecraft
bus reconstructed attitude) file for just that 10-day window is itself ~529 MB; the CK format
apparently samples at high angular rate. Skipping `lrodv`/`lrohg`/`lrosa` (still avoids ~4-5x more
CK volume) and never touching kernels outside the target date range is what keeps this tractable at
all — pulling a full year's CK data across all five flavors would be tens of GB.

## WAC CK (pointing) kernel caching -- a different remote host, and a resolution-result cache

`spice_kernels.py`'s live-default WAC CK source isn't the NAIF metakernel above at all --
`select_isis_wac_ck_kernels`/`isis_wac.resolve_wac_ck_kernels` ask a real ISIS `spiceinit web=yes`
run what it furnishes (see `docs/data-sources.md`'s "ISIS's own LRO kernel database" section for the
full why). Two caching layers, both new:

- **The actual kernel files** (`cache.fetch_isis_kernel`) come from USGS's own S3 bucket
  (`asc-isisdata`), not NAIF -- cached under `isisdata/lro/kernels/ck/...`, deliberately the *same*
  relative layout `$ISISDATA/lro/...` itself uses (not a new independent subtree), so a file cached
  here already sits where a future local, non-web `spiceinit` run or a fuller `downloadIsisData lro`
  fetch would expect to find it. `isis_wac.ensure_isisdata()`'s own `--include` filter deliberately
  excludes `kernels/ck/` -- this is a narrow, additive exception living alongside it. Sizes are
  comparable to (sometimes larger than) the NAIF `lrosc`/`lrolc` chunks above -- one `moc42r_*.bc`
  30-day merge is ~1.7GB -- but `cached_get`'s existing streaming download handles this fine, no
  special resumable-curl treatment needed (unlike the Astropedia case below).
- **The resolution itself** -- *which* kernel filename(s) apply to this project's target
  product/date -- is a separate, much smaller cache: `isis_ck_resolution/<edr_product>.json`, a tiny
  JSON list of `kernels/ck/<filename>` paths, written after the first successful `spiceinit`
  run and read (short-circuiting the whole `ensure_isisdata → fetch_edr_img → run_lrowac2isis →
  run_spiceinit → catlab → parse` chain) on every subsequent call. This is deliberate, explicit
  resilience: once resolved for this project's one fixed demo product, no code path needs to reach
  the live `spiceinit` web service again -- only the plain HTTPS kernel-file download above matters
  for ongoing runs, confirmed live (a second resolution call after the network path to the web
  service is blocked still succeeds from cache). No retry/backoff around the `spiceinit` call itself
  -- a cold-cache failure surfaces immediately rather than looping silently, per explicit user
  direction ("I'd rather have a relatively prompt exception and manually retry later").

## Lunaserv WMS caching

Cache `GetMap` responses keyed by `(layer, bbox, width, height, format)` — deterministic enough that
re-running any script/notebook cell for the same ROI hits the local cache, not the network.

## Astropedia GLD100 caching — a real exception to "just a sliver"

Unlike everything above, `cache.fetch_astropedia_gld100` downloads and caches **the entire ~10GB
flat file**, not a per-request sliver (see `docs/data-sources.md`'s "Astropedia GLD100 flat file"
section for why: the file isn't a Cloud-Optimized GeoTIFF, so a remote windowed read pulls full-width
row strips rather than a small tile — confirmed too slow to repeat per-camera). This is a genuinely
different caching shape than the rest of this project, worth calling out explicitly:

- **Not built on `cached_get`** — a stable (not per-call-unique) partial-file path, `curl -C -`-based
  resume, and *not* deleting the partial file on failure (the opposite of `cached_get`'s behavior) —
  see `cache.fetch_astropedia_gld100`'s own docstring for the full reasoning. Confirmed empirically:
  interrupting a real download mid-transfer and re-running resumes from the exact byte offset, not
  from zero.
- **Archive/restore cost**: per `docs/environment.md`, `archive.sh` tars the *entire* `trntest_ws`
  directory including `cache/` — this one file adds ~10GB to that tarball (and the scp transfer time
  that implies) unless deliberately deleted before archiving. The previous largest single cache
  component (`isisdata/`) was ~5GB spread across many small files; this is double that in one file.
  Not a blocker — the download itself is a real, if one-time, cost regardless of whether it's
  archived or re-fetched fresh each session — but genuinely worth knowing before running `archive.sh`
  without thinking about it, unlike the rest of this project's cache contents, which are small enough
  not to matter either way.
