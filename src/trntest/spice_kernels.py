"""Select and fetch the minimal LRO SPICE kernel set for a given UTC timestamp.

Rather than furnishing a whole year's worth of kernels (the yearly metakernel under
extras/mk/ lists everything for that year, and CK pointing kernels dominate that volume),
we treat the metakernel purely as a manifest: parse it, then download only:

  - the small kernels needed regardless of date (LSK, SCLK, PCK, the two lunar frame
    kernels, the LRO frames kernel, the LROC IK, and the DE421 planetary ephemeris), and
  - the date-ranged SPK files whose filename-encoded range covers our target day, restricted
    to LRO's own reconstructed-orbit prefix (`SPK_PREFIXES`), and
  - the WAC CK (pointing) kernel(s) for our target day -- live default: asks a real ISIS
    `spiceinit web=yes` run what it actually furnishes (`select_isis_wac_ck_kernels`), rather
    than this module's own NAIF-metakernel-based heuristic (`select_naif_wac_ck_kernels`,
    deprecated but numerically equivalent -- both give the same, ISIS-agreeing pointing;
    `select_isis_wac_ck_kernels`'s own docstring has the full story on why this exists anyway).
    Controlled by `TrntestConfig.wac_ck_source`.

See docs/data-sources.md and docs/caching.md for the background.
"""

import dataclasses
import re
from datetime import datetime
from pathlib import Path

import requests
import spiceypy as spice

from trntest import cache
from trntest.config import TrntestConfig, load_config

# Kernels needed no matter what date we're targeting.
ALWAYS_KERNELS = [
    "data/lsk/naif0012.tls",
    "data/sclk/lro_clkcor_2025351_v00.tsc",
    "data/pck/pck00010.tpc",
    "data/pck/moon_pa_de421_1900_2050.bpc",
    "data/fk/lro_frames_2014049_v01.tf",
    "data/fk/moon_assoc_me.tf",
    "data/fk/moon_080317.tf",
    "data/ik/lro_lroc_v20.ti",
    "data/spk/de421.bsp",
]

# CK filename prefixes relevant to LROC WAC pointing (see docs/data-sources.md) -- deprecated-path
# only now (select_naif_wac_ck_kernels), see that function's docstring.
WAC_CK_PREFIXES = ("lrosc", "lrolc")

# SPK (trajectory) filename prefix -- LRO's own reconstructed orbit.
SPK_PREFIXES = ("lrorg",)

DATE_RANGE_RE = re.compile(r"_(\d{7})_(\d{7})_v\d+\.(bc|bsp)$")

LRO_ID = -85
LRO_SC_BUS_ID = -85000
LRO_LROCWAC_ID = -85620


@dataclasses.dataclass(frozen=True)
class KernelRef:
    """One kernel to fetch+furnish, tagged with which cache.py fetch function resolves it. NAIF
    paths and USGS ISIS-kernel-db paths use different remote roots/cache-path conventions (see
    cache.fetch_naif_kernel vs. cache.fetch_isis_kernel) -- a bare path string can no longer say
    "how to fetch this," only "what local path it mirrors to." `path` is `source`'s own path
    convention: "naif" -> archive-relative (e.g. 'data/ck/lrosc_..._v01.bc'); "isis_resolved" ->
    'kernels/ck/<filename>', relative to USGS's S3 `usgs_data/lro/` prefix (see
    cache.isis_kernel_rel_path)."""

    source: str  # "naif" | "isis_resolved"
    path: str


# Tracks which date-ranged (CK/SPK) kernel paths are currently furnished, so fetch_and_furnish can
# unload ones no longer needed before furnishing a new date's set -- see fetch_and_furnish's
# docstring for why this matters. Process-global by necessity: SPICE's own kernel pool is itself
# global per-process state, so this just mirrors it, rather than introducing new global state on
# top of a purely-functional design.
_loaded_date_ranged_kernels: set[KernelRef] = set()

# Tracks every local kernel path currently furnished (ALWAYS_KERNELS included), so fetch_and_furnish
# can skip re-furnishing a kernel it already loaded. This is NOT redundant with SPICE's own kernel
# pool: empirically, spice.furnsh() does not dedupe repeat loads of the same file across separate
# calls -- each call consumes a fresh slot in SPICE's fixed-size KEEPER table (SPICE(NOMOREROOM) once
# ~5300 accumulate), so a long-running process re-furnishing ALWAYS_KERNELS on every call (e.g. once
# per sampled epoch in illumination.find_ascending_node_crossings) exhausts it. Tracking loaded state
# ourselves and only calling furnsh() for genuinely-new paths avoids that.
_loaded_kernels: set[str] = set()


def doy_code(dt: datetime) -> int:
    """YYYYDDD integer used in NAIF's LRO kernel filenames, e.g. 2019-11-30 -> 2019334."""
    return dt.year * 1000 + dt.timetuple().tm_yday


def _latest_metakernel_cache_path(cache_root: Path, year: int) -> Path:
    return cache_root / "naif_latest_metakernel" / f"{year}.txt"


def latest_metakernel_url(year: int, config: TrntestConfig) -> str:
    """Which metakernel is "latest" for `year` -- a live NAIF directory listing, not a specific
    file, so `cache.cached_get`'s usual "does this local path already exist" check doesn't apply
    directly to it.

    Result is persisted to `cache_root/naif_latest_metakernel/<year>.txt` after a successful
    resolution, and read from there first on every subsequent call for that year -- once resolved,
    no code path needs to reach this live endpoint again for that year, the same resilience
    tradeoff as `isis_wac.resolve_wac_ck_kernels`'s own resolution cache (see docs/caching.md).
    Deliberately never invalidated: a year's "latest" version is fixed once kernels selected from it
    are already cached locally (a newer metakernel published later for the same year would only add
    entries covering dates beyond that year, moot for this project's fixed demo timestamps), and
    re-checking on every run would defeat the point -- preserving the ability to run this notebook
    with zero network access at all once genuinely warmed up.

    Takes the whole `TrntestConfig` (unlike most of this module's fetch helpers, which take bare
    scalars) only for `cache_root` -- `naif_base_url` is read off it too rather than as a second
    positional, since both call sites already have `config` in scope."""
    cache_path = _latest_metakernel_cache_path(config.cache_root, year)
    if cache_path.exists():
        return cache_path.read_text().strip()

    mk_dir_url = f"{config.naif_base_url}extras/mk/"
    resp = requests.get(mk_dir_url, timeout=30)
    resp.raise_for_status()
    versions = [int(m) for m in re.findall(rf"lro_{year}_v(\d+)\.tm", resp.text)]
    if not versions:
        raise RuntimeError(f"no metakernel found for year {year} at {mk_dir_url}")
    result = f"extras/mk/lro_{year}_v{max(versions):02d}.tm"

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(result)
    return result


def parse_metakernel(text: str) -> list[str]:
    """Return kernel paths like 'data/ck/lrosc_..._v01.bc' from a metakernel's KERNELS_TO_LOAD."""
    paths = re.findall(r"\$KERNELS/(\S+\.\w+)", text)
    return [f"data/{p}" for p in paths]


def select_date_ranged(
    paths: list[str], target_doy: int, subdir: str, prefixes=None, end_doy: int | None = None
) -> list[str]:
    """Paths under `subdir` (optionally restricted to `prefixes`) whose filename-encoded date range
    overlaps [target_doy, end_doy] -- end_doy defaults to target_doy, i.e. a single-day query (the
    overlap condition then reduces to today's `start <= target_doy <= end` membership check)."""
    query_end_doy = target_doy if end_doy is None else end_doy
    selected = []
    for p in paths:
        if f"/{subdir}/" not in p:
            continue
        name = p.rsplit("/", 1)[-1]
        if prefixes and not name.startswith(prefixes):
            continue
        m = DATE_RANGE_RE.search(p)
        if not m:
            continue
        start, end = int(m.group(1)), int(m.group(2))
        if start <= query_end_doy and end >= target_doy:
            selected.append(p)
    return selected


def select_naif_wac_ck_kernels(target_dt: datetime, config: TrntestConfig) -> list[str]:
    """**Deprecated** -- kept for reference/comparison (see docs/history.md's dated entry). Selects
    WAC CK kernels by parsing NAIF's yearly metakernel manifest and filtering to `WAC_CK_PREFIXES`
    (`lrosc` = spacecraft bus attitude, `lrolc` = LROC-specific thermal offset of frame -85620).
    Confirmed to disagree with ISIS's own camera model by ~11-13km: it never fetches a second CK,
    `moc42r_*.bc`, that ISIS's `spiceinit web=yes` furnishes alongside the usual `lrolc_*` one (and
    that kernel isn't even in the NAIF metakernel this function parses -- it lives only in USGS's own
    S3-hosted ISIS kernel database, an entirely different source). Superseded by
    `select_isis_wac_ck_kernels`, which asks a real `spiceinit` run directly instead of reimplementing
    kernel selection. Left in place for direct A/B comparison, not for any accuracy reason -- both
    `lrosc`/`lrolc` and `moc42r` are the same "Reconstructed" tier in ISIS's own kernel-db vocabulary,
    no evidence one is more accurate than the other."""
    mk_path = latest_metakernel_url(target_dt.year, config)
    mk_local = cache.fetch_naif_kernel(mk_path, cache_root=config.cache_root, base_url=config.naif_base_url)
    all_paths = parse_metakernel(mk_local.read_text())
    target_doy = doy_code(target_dt)
    return select_date_ranged(all_paths, target_doy, "ck", prefixes=WAC_CK_PREFIXES)


def select_isis_wac_ck_kernels(target_dt: datetime, config: TrntestConfig) -> list[str]:
    """Live default WAC CK source: asks a real ISIS `spiceinit web=yes` run what it actually
    furnishes (`isis_wac.resolve_wac_ck_kernels`), rather than reimplementing USGS's own kernel-db
    selection algorithm in Python -- a real gap was found in that alternative (the route it would
    need, `lroc_kernels.db`, is currently empty in USGS's bucket, even though ISIS's live resolution
    still furnishes an `lrolc`-equivalent kernel from somewhere). See docs/history.md's dated entry
    for the full story, including a since-corrected premise: this mechanism was originally built to
    fix a documented ~11-13km pointing discrepancy, but direct verification (comparing our SPICE
    computation against real `campt` output at several points) found that discrepancy isn't
    reproducible at all -- and that the extra kernel this mechanism adds (`moc42r_*.bc`, bus
    attitude) has zero measurable effect on WAC pointing regardless (SPICE's frame resolution for
    `LRO_LROCWAC_VIS` turns out to depend entirely on `lrolc`'s own direct CK segments for -85620,
    confirmed via a `SPICE(NOFRAMECONNECT)` failure when `lrolc` isn't furnished even with `moc42r`
    present). Kept anyway for its independent, real value: it makes our furnished kernel set match
    ISIS's own resolution by construction, a more principled and future-proof design than hand-picked
    filename prefixes, even though it doesn't move any currently-known number.

    `isis_wac.resolve_wac_ck_kernels`'s resolution is tied to `config.edr_product` (a single fixed
    EDR product, not an arbitrary requested epoch) -- filters the resolved kernel(s) to just the ones
    whose filename-encoded date range actually covers `target_dt`, returning `[]` if none do (e.g.
    `target_dt` falls well outside that one product's own narrow coverage window, as happens for
    `dataset.select_dataset()`'s wide date-range searches) -- `select_kernels_for` falls back to the
    deprecated NAIF path for that case, safe per the equivalence just confirmed."""
    from trntest import isis_wac  # noqa: PLC0415 -- see docstring, avoids a circular import

    target_doy = doy_code(target_dt)
    covered = []
    for p in isis_wac.resolve_wac_ck_kernels(config):
        m = DATE_RANGE_RE.search(p)
        if m and int(m.group(1)) <= target_doy <= int(m.group(2)):
            covered.append(p)
    return covered


def select_kernels_for(target_dt: datetime, config: TrntestConfig) -> list[KernelRef]:
    if config.wac_ck_source == "isis_resolved":
        ck_paths = select_isis_wac_ck_kernels(target_dt, config)
        if ck_paths:
            ck_refs = [KernelRef("isis_resolved", p) for p in ck_paths]
        else:
            # target_dt falls outside the one fixed EDR product's own resolved coverage window --
            # not a resolution failure, an expected case for wide date-range searches (see
            # select_isis_wac_ck_kernels's docstring). Confirmed safe to fall back to the deprecated
            # NAIF path here specifically: both sources give numerically identical pointing (see that
            # docstring's "since-corrected premise" note), so this isn't reintroducing any known gap.
            ck_paths = select_naif_wac_ck_kernels(target_dt, config)
            ck_refs = [KernelRef("naif", p) for p in ck_paths]
    elif config.wac_ck_source == "naif_metakernel":
        ck_paths = select_naif_wac_ck_kernels(target_dt, config)
        ck_refs = [KernelRef("naif", p) for p in ck_paths]
    else:
        raise ValueError(
            f"unknown wac_ck_source {config.wac_ck_source!r} -- expected 'isis_resolved' or 'naif_metakernel'"
        )
    if not ck_refs:
        raise RuntimeError(f"no WAC CK kernel resolved for {target_dt!r} via wac_ck_source={config.wac_ck_source!r}")

    mk_path = latest_metakernel_url(target_dt.year, config)
    mk_local = cache.fetch_naif_kernel(mk_path, cache_root=config.cache_root, base_url=config.naif_base_url)
    all_paths = parse_metakernel(mk_local.read_text())
    target_doy = doy_code(target_dt)
    spk_paths = select_date_ranged(all_paths, target_doy, "spk", prefixes=SPK_PREFIXES)

    return [KernelRef("naif", p) for p in ALWAYS_KERNELS] + ck_refs + [KernelRef("naif", p) for p in spk_paths]


def _fetch_kernel_ref(ref: KernelRef, config: TrntestConfig) -> str:
    if ref.source == "naif":
        return str(cache.fetch_naif_kernel(ref.path, cache_root=config.cache_root, base_url=config.naif_base_url))
    if ref.source == "isis_resolved":
        return str(
            cache.fetch_isis_kernel(ref.path, cache_root=config.cache_root, base_url=config.isis_kernel_base_url)
        )
    raise ValueError(f"unknown KernelRef.source {ref.source!r}")


def fetch_and_furnish(target_dt: datetime, config: TrntestConfig | None = None) -> list[str]:
    """Download the minimal kernel set for target_dt and spice.furnsh() each one. Returns paths.

    Unloads any previously-furnished date-ranged (CK/SPK) kernels not needed for `target_dt` before
    furnishing the new set -- SPICE's kernel pool has a fixed-size character-value buffer that can
    fill up (SPICE(KERNELPOOLFULL)) if many distinct kernels accumulate across a long-running
    process without ever being unloaded, e.g. `dataset.select_dataset()` evaluating hundreds of
    candidate images spanning several kernel date-ranges in one process. ALWAYS_KERNELS are
    unaffected -- they're the same fixed set regardless of date, so they're only furnished once:
    only paths not already in `_loaded_kernels` are actually passed to spice.furnsh(), since
    spice.furnsh() itself does NOT dedupe repeat loads of the same file across separate calls (each
    call consumes a fresh, limited KEEPER slot -- SPICE(NOMOREROOM) once ~5300 accumulate), which a
    long-running process calling this once per sampled epoch (e.g.
    illumination.find_ascending_node_crossings) would otherwise hit quickly."""
    config = config or load_config()
    kernel_refs = select_kernels_for(target_dt, config)
    always_refs = {KernelRef("naif", p) for p in ALWAYS_KERNELS}
    needed_date_ranged = set(kernel_refs) - always_refs

    for stale_ref in _loaded_date_ranged_kernels - needed_date_ranged:
        stale_local = _fetch_kernel_ref(stale_ref, config)
        spice.unload(stale_local)
        _loaded_kernels.discard(stale_local)
    _loaded_date_ranged_kernels.clear()
    _loaded_date_ranged_kernels.update(needed_date_ranged)

    local_paths = [_fetch_kernel_ref(ref, config) for ref in kernel_refs]
    for lp in local_paths:
        if lp not in _loaded_kernels:
            spice.furnsh(lp)
            _loaded_kernels.add(lp)
    ck_paths_furnished = [r.path for r in kernel_refs if "/ck/" in r.path]
    if not verify_ck_coverage(LRO_LROCWAC_ID, spice.utc2et(target_dt.strftime("%Y-%m-%dT%H:%M:%S.%f"))):
        raise RuntimeError(
            f"furnished CK kernels {ck_paths_furnished!r} do not actually cover {target_dt!r} for "
            f"frame {LRO_LROCWAC_ID} -- trust-but-verify failed"
        )
    return local_paths


def furnish_spk_range(start_dt: datetime, end_dt: datetime, config: TrntestConfig | None = None) -> list[str]:
    """Furnish the union of `lrorg` trajectory SPK segments covering [start_dt, end_dt] all at once,
    left loaded (not tracked for later unloading, unlike fetch_and_furnish's per-epoch date-ranged
    kernels). For SPICE geometry-finder searches (e.g. illumination.find_ascending_node_crossings's
    gfposc call) that need coverage across a whole window in one call, rather than one epoch at a
    time. SPK-only (no CK) -- deliberately not using fetch_and_furnish's just-in-time
    furnish/unload pattern, since that pattern exists to bound *CK* attitude-kernel accumulation
    (the kernel type that actually risks exceeding the kernel pool's fixed-size buffer -- see
    WAC_CK_PREFIXES above); SPK trajectory data alone is much smaller, so furnishing a whole
    search window's worth at once is expected to be safe for this repo's typical max_search_days
    (7-30 days) -- confirmed in practice to be a single SPK segment for a 7-day window.

    Reuses `_loaded_kernels`'s dedup tracking, so a later fetch_and_furnish call for a date inside
    this range won't re-furnish (or attempt to unload) anything furnished here."""
    config = config or load_config()
    start_doy = doy_code(start_dt)
    end_doy = doy_code(end_dt)

    all_paths: list[str] = []
    for year in range(start_dt.year, end_dt.year + 1):
        mk_path = latest_metakernel_url(year, config)
        mk_local = cache.fetch_naif_kernel(mk_path, cache_root=config.cache_root, base_url=config.naif_base_url)
        all_paths.extend(parse_metakernel(mk_local.read_text()))

    spk_paths = select_date_ranged(all_paths, start_doy, "spk", prefixes=SPK_PREFIXES, end_doy=end_doy)
    kernel_paths = ALWAYS_KERNELS + spk_paths

    local_paths = [
        str(cache.fetch_naif_kernel(p, cache_root=config.cache_root, base_url=config.naif_base_url))
        for p in kernel_paths
    ]
    for lp in local_paths:
        if lp not in _loaded_kernels:
            spice.furnsh(lp)
            _loaded_kernels.add(lp)
    return local_paths


def verify_ck_coverage(idcode: int, et: float) -> bool:
    """Check that some loaded CK actually covers `et` (SPICE ET seconds) for `idcode`."""
    for i in range(spice.ktotal("ck")):
        file, *_ = spice.kdata(i, "ck")
        cover = spice.ckcov(file, idcode, False, "INTERVAL", 0.0, "TDB")
        for j in range(spice.wncard(cover)):
            start, stop = spice.wnfetd(cover, j)
            if start <= et <= stop:
                return True
    return False
