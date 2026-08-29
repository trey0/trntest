# ISIS's own LRO kernel database (USGS S3, not NAIF)

Index: [`docs/data-sources.md`](../data-sources.md). Compare
[`spice-kernels-naif.md`](spice-kernels-naif.md) — the deprecated, NAIF-metakernel-based alternative.
See `docs/external-tools.md` for the ISIS/`spiceinit`/`cam2map` tool-behavior facts this kernel
source feeds into.

`spice_kernels.py`'s NAIF-metakernel-based CK selection isn't the only source of truth for which
kernels apply to a given LRO product/date. ISIS3 resolves kernels via a completely separate
mechanism, confirmed live by reading ISIS's own config files inside the Docker image and directly
querying the real bucket:

- **`spiceinit web=yes`** calls USGS's own ALE-based SPICE web service, found via
  `/opt/conda/envs/isis/bin/xml/spiceinit.xml`'s `URL` parameter default:
  `https://astrogeology.usgs.gov/apis/ale/v0.9.1/spiceserver/` (the `v0.9.1/spiceserver` path names
  the backend explicitly — ALE, USGS's own Python "Abstraction Layer for Ephemerides" library). This
  web service — and local, non-`web` `spiceinit` — both resolve kernels via the *same* mechanism:
  `kernels.*.db` PVL index files (ISIS's `kerneldbgen` app format: `Object = SpacecraftPointing`
  containing many `Group = Selection` entries, each a `Time = (start, stop)` range + `File` +
  `Type`).
- **These `.db` files, and the kernels they reference, are not NAIF-hosted for LRO.**
  `/opt/conda/envs/isis/etc/isis/rclone.conf`'s `[lro]` alias is `remote =
  asc_s3:asc-isisdata/usgs_data/lro/` — a plain alias to USGS's own public AWS S3 bucket
  (`asc-isisdata`, `us-west-2`), with **no** `naif:` union (unlike `[dawn]`/`[cassini]`/`[tgo]`,
  which explicitly union their own USGS data with a `naif:` remote). The whole bucket is
  unauthenticated/anonymously readable over plain HTTPS
  (`https://asc-isisdata.s3.us-west-2.amazonaws.com/usgs_data/lro/...`, including S3's own
  `?list-type=2&prefix=...` listing API).
- **`kernels.0001.conf`** (`kernels/ck/kernels.0001.conf` in that bucket) routes each instrument to
  which `.db` file(s) to consult — confirmed live: `WAC-VIS`/`WAC-UV` both route to *two* sources,
  `kernels/ck/moc_kernels.????.db` (bus attitude — resolves to `moc42r_*.bc`, a real,
  ~1.7GB-per-30-day-merge product that exists **only** in this bucket, absent from every NAIF-hosted
  path checked: neither the yearly metakernel's own `data/ck/` nor NAIF's separate operational
  mirror at `naif.jpl.nasa.gov/pub/naif/LRO/kernels/ck/`) and `kernels/ck/lroc_kernels.????.db`
  (presumably the `lrolc`-equivalent role — confirmed live to currently have **zero** matching files
  in the bucket, a real gap; see below for why this doesn't actually block anything).
- **`moc42r` is not more accurate than NAIF's `lrosc`/`lrolc`** — both are tagged `Type =
  Reconstructed` in `kerneldbgen`'s own vocabulary, which has a distinctly higher `Smithed` tier for
  genuinely photogrammetric/bundle-adjustment-refined products, never used for either. NAIF's own
  `ckinfo.txt` documents `lrosc` as itself a merge of daily `moc42_*.bc` files "produced by the LRO
  project during operations" — `moc42r` is USGS's own independent ~30-day merge of that *same*
  underlying daily series, not a different/better source. Confirmed the bucket periodically
  re-merges: `moc42r_2019304_2019334_v01.bc` (uploaded 2022-08-12) and a newer, differently-dated
  `moc42r_2019334_2020001_v01.bc` both exist for overlapping coverage of the same period — a real,
  live demonstration that hardcoding a filename found in one session can go stale by the next.

**How this project uses it**: rather than reimplementing the `.conf`/`.db` selection algorithm in
Python (the `lroc_kernels.db` gap above means that reimplementation would have a real, hard-to-detect
hole — it would silently never furnish an `lrolc`-equivalent kernel at all, even though ISIS's own
live resolution clearly does furnish one from somewhere), `isis_wac.resolve_wac_ck_kernels` asks a
real `spiceinit web=yes` run directly: runs the existing `ensure_isisdata → fetch_edr_img →
run_lrowac2isis → run_spiceinit` pipeline against this project's one fixed reference EDR product,
then reads the resulting cube's `Group = Kernels` label (via ISIS's `catlab` app, parsed with the
`pvl` library — the format's genuine nested/duplicate-key structure isn't cleanly regex-able the way
the flat NAIF metakernel manifest is). Confirmed live: the label's `InstrumentPointing` field lists
`(Table, $lro/kernels/ck/lrolc_2019334_2020001_v01.bc,
$lro/kernels/ck/moc42r_2019334_2020001_v01.bc, $lro/kernels/fk/lro_frames_2014049_v01.tf)` — both
kernels together, resolving the apparent `lroc_kernels.db` gap (ISIS's live resolution clearly finds
an `lrolc`-equivalent file some other way than that specific route) without this project needing to
know *how*. Result persisted to `cache/isis_ck_resolution/<edr_product>.json`, checked before ever
calling `spiceinit` again — the live web service is only hit once per distinct `edr_product`, not
once per requested date (`select_isis_wac_ck_kernels` filters the persisted result's own
filename-encoded date range against whatever date is actually requested, falling back to the
deprecated NAIF path for dates outside that one product's own resolution window — see
`spice_kernels.select_kernels_for`'s docstring).

**The kernel this mechanism adds turns out to be inert for plain SPICE calls, and the original bug it
was built to fix isn't reproducible.** `spice.ckobj` on a real `lrolc_*.bc` file shows it stores
segments **directly** under `-85620` (not `-85000`) — `spice.pxform('LRO_LROCWAC_VIS', 'MOON_ME',
et)` gives byte-identical output whether `lrosc` or `moc42r` (bus-only, `-85000`) is the
co-furnished kernel, and fails outright with `SPICE(NOFRAMECONNECT)` if `lrolc` is omitted even with
a bus CK present. Direct comparison against real `campt` output (`SpacecraftPosition`,
`LookDirectionCamera`/`LookDirectionBodyFixed`) at four independent points spread across a real
cube — transforming `campt`'s own reported camera-space look vector through *our* `pxform`-derived
rotation and comparing against `campt`'s own body-fixed result — found **zero** measurable
discrepancy (sub-centimeter position, 0.000000° pointing) at every point, with or without `moc42r`
furnished. The originally-reported ~11-13km discrepancy this mechanism was built to fix is not
reproducible; its true cause was never identified (most likely conflated with the separate, also-real
`cam2map` `WARPALGORITHM` striping bug — see `docs/external-tools.md`'s ISIS `cam2map` notes). Kept
as the live default anyway (`TrntestConfig.wac_ck_source = "isis_resolved"`) for independent
reasons: it makes this project's furnished kernel set match ISIS's own real-world resolution by
construction, which is more principled and immune to future NAIF/USGS drift than a hand-picked
prefix list — not because it's fixing a currently-known bug.
