# LRO SPICE kernels (NAIF)

Index: [`docs/data-sources.md`](../data-sources.md). See [`spice-kernels-isis.md`](spice-kernels-isis.md)
for the alternate, live-default ISIS/S3 kernel source this project actually uses for WAC CK pointing.

- Archive root: `https://naif.jpl.nasa.gov/pub/naif/pds/data/lro-l-spice-6-v1.0/lrosp_1000/`
  - Subdirs by kernel type: `data/ck`, `data/spk`, `data/ik`, `data/fk`, `data/sclk`, `data/lsk`,
    `data/pck`, plus `extras/mk` (one metakernel per time range/year).
- The yearly metakernel lists every kernel needed to cover that year — treat it as a **manifest to
  parse**, not something to furnish wholesale. CK (pointing) kernels dominate the data volume; only
  download the specific CK/SPK file(s) whose filename-encoded date range covers the timestamp
  needed, plus the small LSK/SCLK/PCK/FK/IK files (needed regardless, cheap).
- Request `MOON_ME` directly from spiceypy calls (position + orientation) rather than getting
  MOON_PA/J2000 and rotating manually — the standard lunar frame kernel defines `MOON_ME` for
  direct use (`fk/moon_assoc_me.tf` + `fk/moon_080317.tf` + the PA orientation kernel).
- Yearly metakernels (`extras/mk/lro_YYYY_vNN.tm`) list, for CK, **five separate kernel "flavors"**
  covering the same date ranges: `lrosc` (spacecraft bus attitude — the main reconstructed
  pointing), `lrolc` (LROC-specific: small thermally-dependent offset of frame -85620 relative to
  the bus), `lrodv` (delta-V/maneuver attitude), `lrohg` (high-gain antenna gimbal), `lrosa` (solar
  array gimbal). Of these, **only `lrolc` is actually needed for WAC pointing via plain SPICE**
  (`spice.pxform`/`spice.spkezr`) — see the frame-chain note below; `lrosc` is furnished by the
  deprecated NAIF-metakernel CK-selection path anyway (harmless, just unused for this purpose).
  `lrodv`/`lrohg`/`lrosa` are skipped entirely regardless, which cuts CK downloads roughly 5x for a
  given day.
- **Live default WAC CK source is not this metakernel path at all.** `spice_kernels.
  select_isis_wac_ck_kernels` (`TrntestConfig.wac_ck_source = "isis_resolved"`, the default) instead
  asks a real ISIS `spiceinit web=yes` run what it furnishes (`isis_wac.resolve_wac_ck_kernels`),
  which draws from an entirely different host — see [`spice-kernels-isis.md`](spice-kernels-isis.md).
  `select_naif_wac_ck_kernels` (this metakernel-manifest approach) is kept, deprecated, as
  `wac_ck_source = "naif_metakernel"` — confirmed numerically equivalent (see that file).
- CK/SPK filenames encode a `YYYYDDD_YYYYDDD` date range but adjacent files can overlap by a day —
  don't just pick the filename whose range contains the target date; after furnishing a candidate,
  verify actual coverage with `spiceypy.ckcov`/`spkcov` and fall back to the neighboring file if the
  exact timestamp isn't covered.
- IK files are per-instrument (`lro_crater_v03.ti`, `lro_dlre_v05.ti`, `lro_lamp_v03.ti`,
  `lro_lend_v00.ti`, `lro_lola_v00.ti`, `lro_lroc_v20.ti`) — only `lro_lroc_v20.ti` is needed here.
- Always-needed small/generic kernels regardless of date: `lsk/naif0012.tls`,
  `sclk/lro_clkcor_2025351_v00.tsc` (~2.3 MB, single mission-long file, not date-ranged),
  `pck/pck00010.tpc`, `pck/moon_pa_de421_1900_2050.bpc`, `fk/lro_frames_2014049_v01.tf`,
  `fk/moon_assoc_me.tf`, `fk/moon_080317.tf`, `ik/lro_lroc_v20.ti`, `spk/de421.bsp` (planetary
  ephemeris; the Moon PA/ME frame chain needs it).
- WAC frame chain (from `lro_frames_2014049_v01.tf`): `LRO_LROCWAC` (NAIF ID **-85620**) is
  CK-dependent (small thermally-varying offset from `LRO_SC_BUS`, +Z boresight) — this is exactly
  what the `lrolc` CK provides. `LRO_LROCWAC_VIS` (-85621) and the 5 VIS filter frames
  (-85631..-85635) are then *fixed* (TKFRAME) offsets from -85620, defined right in the FK — no CK
  needed for those.
  - **Confirmed that this is a *direct* segment, not a runtime-composed delta**: `spice.ckobj` on a
    real `lrolc_*.bc` file lists `-85620` (plus `-85610`/`-85600`) as objects it stores segments for
    directly — the file bakes in `-85620`'s full orientation already, it doesn't need the bus
    (`-85000`, via `lrosc`/`moc42r`) at runtime to compose one. Verified decisively: furnishing only
    a bus CK (`moc42r` or `lrosc`) with `lrolc` *not* loaded makes
    `spice.pxform('LRO_LROCWAC_VIS', 'MOON_ME', et)` fail outright with `SPICE(NOFRAMECONNECT)` — if
    SPICE needed to chain through the bus at runtime, that call would have succeeded using bus data
    alone. Practical upshot: **plain SPICE frame resolution for WAC pointing is entirely determined
    by whichever `lrolc`-flavor file is loaded; a second, bus-only CK (`lrosc` or `moc42r`) makes zero
    difference to it** — see [`spice-kernels-isis.md`](spice-kernels-isis.md) for why this matters to
    the now-corrected "missing `moc42r`" diagnosis.
- `spice.furnsh()` does **not** dedupe repeat loads of the same kernel file across separate calls —
  each call consumes a fresh, limited KEEPER slot (~5300 max). `spice_kernels.py` tracks every
  currently-furnished local path (`_loaded_kernels`) and skips `furnsh()` for paths already loaded,
  unloading superseded date-ranged (CK/SPK) kernels when the target date moves to a different chunk
  (`fetch_and_furnish`). Any code that furnishes kernels across many distinct dates in one process
  must go through this tracking, not call `spice.furnsh()` directly.
- **Ascending-node search**: `illumination.find_ascending_node_crossings` finds LRO's `MOON_ME`-frame
  latitude=0 crossings via SPICE's `gfposc` (geometry finder over position coordinates: `targ="LRO"`,
  `frame="MOON_ME"`, `obsrvr="MOON"`, `crdsys="LATITUDINAL"`, `coord="LATITUDE"`, `relate="="`,
  `refval=0.0`) — one call over the whole search window, SPICE's own compiled adaptive root-finder.
  Still returns both ascending and descending crossings; filtered to ascending via a ±5s latitude
  sign check. Needs SPK coverage for the *whole* confinement window furnished at once —
  `spice_kernels.furnish_spk_range` does this (SPK/`lrorg` only, not CK — safe to pre-furnish a
  whole search window's worth since SPK volume is small relative to CK), unlike
  `fetch_and_furnish`'s per-epoch just-in-time pattern used everywhere else for full camera-pose
  work (which does need CK).
- **`functools.cache`-on-`TrntestConfig` gotcha**: `spice_kernels.latest_metakernel_url` is
  `@functools.cache`d on `(year, naif_base_url: str)` — keyed on just that one field, not a whole
  `TrntestConfig`, deliberately. A whole-config cache key silently breaks memoization whenever a
  caller varies the config per-item (e.g. `candidate_window.py`'s per-candidate `dataclasses.replace`) but
  the cached function only actually reads one field of it — every distinct config value becomes a
  cache miss even though the field that matters never changed. Prefer keying `functools.cache` on
  the specific field(s) a function actually uses, not a whole config object, whenever callers might
  vary other fields per call.
