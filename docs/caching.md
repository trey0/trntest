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
  robbins_craters/lunar_crater_database_robbins_2018.zip   (one whole file, ~92MB -- see docs/data-sources.md)
  isis_ck_resolution/<edr_product>.json   (persisted spiceinit CK resolution -- see below)
  naif_latest_metakernel/<year>.txt   (persisted "latest metakernel" resolution -- see below)
  torch/hub/checkpoints/...   (LightGlue/DISK pretrained weights -- see below)
```

`isisdata/` is ISIS3's own reference data, fetched by `isis_wac.ensure_isisdata()` (see
`docs/data-sources.md`'s "ISIS3/CSM spike" section). Fully re-fetchable and safe to prune before
archiving, like the other trees. `--no-kernels` keeps the one-time download to ~5GB instead of ~30GB,
since `spiceinit web=yes` covers the pointing/position role the full `base` download would otherwise
serve.

Fetch helpers (`src/trntest/cache.py`) check "does this local mirrored path already exist" before
making any network request; if present, skip the request entirely.

## Retry/backoff/pacing policy

`cache.cached_get` (the fetch path behind everything except the Astropedia flat file, below) retries
a failed request a small, fixed number of times with capped exponential backoff. A 429 honors the
server's own `Retry-After` if it's short enough to wait out inline; a longer or malformed header
fails immediately instead of blocking indefinitely. Every real request (never a cache hit) is paced
by a small fixed delay, and all requests in one process share one `requests.Session`.

Once attempts are exhausted, `cached_get` raises `cache.FetchError` rather than the raw exception, so
a caller sweeping many items in a loop (`dataset._evaluate_illuminated_candidates`,
`dataset.generate_dataset`) can tell a systemic failure apart from an ordinary per-item one and abort
the whole operation instead of firing hundreds more requests at a server that's already refusing
them -- the failure mode that motivated this: an unpaced ~1600-request sweep against PDS triggered a
1-hour IP ban after the old catch-and-continue loop kept firing through the first 429.

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
`spice_kernels.py`'s `select_kernels_for()` handles this. The single `lrosc` (spacecraft bus
reconstructed attitude) file for that 10-day window is itself ~529 MB — CK format apparently samples
at high angular rate. Skipping `lrodv`/`lrohg`/`lrosa` and staying within the target date range keeps
this tractable — a full year across all five CK flavors would be tens of GB.

## "Latest metakernel" resolution caching

Step 1 above first has to ask NAIF *which* metakernel is current for the target year — `extras/mk/`
holds one versioned file per year (`lro_2019_v06.tm`, etc.), and "latest" is a live directory
listing, not a cacheable path, so `cached_get`'s usual existence check doesn't apply.
`spice_kernels.latest_metakernel_url()` persists this resolution instead: written to
`naif_latest_metakernel/<year>.txt` after the first successful lookup, read from there afterward.
Deliberately never invalidated — a year's "latest" version is fixed once its kernels are cached
locally. This is what lets a warmed-up `image_generation.ipynb` run with no network access at all,
not just no *new* downloads.

## WAC CK (pointing) kernel caching -- a different remote host, and a resolution-result cache

`spice_kernels.py`'s live-default WAC CK source isn't the NAIF metakernel above —
`select_isis_wac_ck_kernels`/`isis_wac.resolve_wac_ck_kernels` ask a real ISIS `spiceinit web=yes`
run what it furnishes (see `docs/data-sources.md`'s "ISIS's own LRO kernel database" section for
why). Two caching layers:

- **The kernel files** (`cache.fetch_isis_kernel`) come from USGS's S3 bucket (`asc-isisdata`),
  cached under `isisdata/lro/kernels/ck/...` — the same relative layout `$ISISDATA/lro/...` uses, so
  a file cached here already sits where a future local `spiceinit` or full `downloadIsisData lro`
  fetch would expect it. `isis_wac.ensure_isisdata()`'s `--include` filter excludes `kernels/ck/`;
  this is that exception. Sizes rival the NAIF chunks above (one `moc42r_*.bc` 30-day merge is
  ~1.7GB) but need no special resumable-download handling.
- **The resolution** — which kernel filename(s) apply to a given product/date — is cached
  separately and much smaller: `isis_ck_resolution/<edr_product>.json`, written after the first
  successful `spiceinit` run and read on every later call, short-circuiting the `ensure_isisdata →
  fetch_edr_img → run_lrowac2isis → run_spiceinit → catlab → parse` chain. Once resolved for a
  product, no code path needs to reach the live `spiceinit` web service again — only the plain HTTPS
  kernel-file download above matters afterward. No retry/backoff around the `spiceinit` call itself:
  a cold-cache failure surfaces immediately, per explicit user direction ("I'd rather have a
  relatively prompt exception and manually retry later").

## Lunaserv WMS caching

Cache `GetMap` responses keyed by `(layer, bbox, width, height, format)` — deterministic enough that
re-running any script/notebook cell for the same ROI hits the local cache, not the network.

## Astropedia GLD100 caching — a real exception to "just a sliver"

Unlike everything above, `cache.fetch_astropedia_gld100` downloads and caches the entire ~10GB flat
file, not a per-request sliver (see `docs/data-sources.md`'s "Astropedia GLD100 flat file" section
for why — it isn't a Cloud-Optimized GeoTIFF, so a windowed remote read pulls full-width row strips,
too slow to repeat per-camera).

Not built on `cached_get` — a stable partial-file path with `curl -C -`-based resume, and the
partial file is kept (not deleted) on failure so a retry resumes from the interrupted byte offset
rather than starting over. See `cache.fetch_astropedia_gld100`'s own docstring for the mechanics.

## LightGlue/DISK pretrained-weight caching

`pose_alignment.match_features_lightglue` (`docs/data-sources.md`'s "LightGlue tie-point matching"
section) loads two pretrained-weight files on first use — LightGlue's own weights (via
`torch.hub.load_state_dict_from_url`) and DISK's extractor weights (via
`kornia.feature.DISK.from_pretrained`, also routing through `torch.hub`) — tens of MB total, not an
Astropedia-sized case. Both follow `torch.hub`'s own caching convention, not `cache.cached_get` —
this project doesn't own that fetch code. `docker/Dockerfile` sets `TORCH_HOME=/workspace/cache/torch`
so this lands under the project's shared `cache/` (survives rebuilds) instead of torch's default
`~/.cache/torch`, which would be silently re-fetched every rebuild.
