"""Select and fetch the minimal LRO SPICE kernel set for a given UTC timestamp.

Rather than furnishing a whole year's kernels (the yearly metakernel under extras/mk/ lists
everything for that year; CK pointing kernels dominate its volume), the metakernel is treated purely
as a manifest: parsed, then only these are downloaded:

- the kernels needed regardless of date (LSK, SCLK, PCK, the two lunar frame kernels, the LRO frames
  kernel, the LROC IK, the DE421 planetary ephemeris);
- the date-ranged SPK trajectory files covering the target day, restricted to LRO's own
  reconstructed-orbit prefix (`SPK_PREFIXES`);
- the WAC CK (pointing) kernel(s) for the target day, per `TrntestConfig.wac_ck_source` --
  `select_isis_wac_ck_kernels` (live default) or `select_naif_wac_ck_kernels` (deprecated).

See docs/data-sources/spice-kernels-isis.md, docs/data-sources/spice-kernels-naif.md, and
docs/caching.md.
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

# CK filename prefixes relevant to LROC WAC pointing (see docs/data-sources/spice-kernels-naif.md) --
# deprecated-path only now (select_naif_wac_ck_kernels), see that function's docstring.
WAC_CK_PREFIXES = ("lrosc", "lrolc")

# SPK (trajectory) filename prefix -- LRO's own reconstructed orbit.
SPK_PREFIXES = ("lrorg",)

DATE_RANGE_RE = re.compile(r"_(\d{7})_(\d{7})_v\d+\.(bc|bsp)$")

LRO_ID = -85
LRO_SC_BUS_ID = -85000
LRO_LROCWAC_ID = -85620


@dataclasses.dataclass(frozen=True)
class KernelRef:
    """One kernel to fetch+furnish, tagged with which `cache.py` fetch function resolves it.

    :param source: `"naif"` or `"isis_resolved"` -- selects `cache.fetch_naif_kernel` vs.
        `cache.fetch_isis_kernel`.
    :param path: in `source`'s own path convention -- archive-relative for `"naif"` (e.g.
        `'data/ck/lrosc_..._v01.bc'`); `'kernels/ck/<filename>'`, relative to USGS's S3
        `usgs_data/lro/` prefix, for `"isis_resolved"` (see `cache.isis_kernel_rel_path`).
    """

    # NAIF and USGS ISIS-kernel-db paths use different remote roots/cache-path conventions, so a
    # bare path string alone can't say how to fetch it -- only what local path it mirrors to.

    source: str  # "naif" | "isis_resolved"
    path: str


# Tracks which date-ranged (CK/SPK) kernel paths are currently furnished, so fetch_and_furnish can
# unload ones no longer needed before furnishing a new date's set -- SPICE's kernel pool has a
# fixed-size character-value buffer that can fill (SPICE(KERNELPOOLFULL)) if many distinct kernels
# accumulate across a long-running process without ever being unloaded, e.g.
# dataset.images_for_window() evaluating hundreds of candidate images spanning several kernel
# date-ranges in one process. Process-global by necessity: SPICE's own kernel pool is itself global
# per-process state, so this just mirrors it, rather than introducing new global state on top of a
# purely-functional design.
_loaded_date_ranged_kernels: set[KernelRef] = set()

# Tracks every local kernel path currently furnished (ALWAYS_KERNELS included), so fetch_and_furnish
# can skip re-furnishing a kernel it already loaded. This is NOT redundant with SPICE's own kernel
# pool: empirically, spice.furnsh() does not dedupe repeat loads of the same file across separate
# calls -- each call consumes a fresh slot in SPICE's fixed-size KEEPER table (SPICE(NOMOREROOM) once
# ~5300 accumulate), so a long-running process re-furnishing ALWAYS_KERNELS on every call (e.g. once
# per sampled epoch in illumination.find_node_crossings) exhausts it. Tracking loaded state
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

    Persists the result to `cache_root/naif_latest_metakernel/<year>.txt` after first resolving it,
    and reads from there on every subsequent call for that year -- never invalidated.
    """
    # Deliberately never invalidated: a year's "latest" version is fixed once kernels selected from
    # it are already cached locally (a newer metakernel published later only adds entries covering
    # dates beyond that year, moot for this project's fixed demo timestamps), and re-checking on
    # every run would defeat the point: preserving the ability to run with zero network access once
    # warmed up.
    #
    # Takes the whole TrntestConfig (unlike most of this module's fetch helpers, which take bare
    # scalars) only for cache_root -- naif_base_url is read off it too since both call sites already
    # have config in scope.
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
    """**Deprecated** -- kept for reference/comparison, superseded by `select_isis_wac_ck_kernels`.
    Selects WAC CK kernels by parsing NAIF's yearly metakernel manifest and filtering to
    `WAC_CK_PREFIXES` (`lrosc` = spacecraft bus attitude, `lrolc` = LROC-specific thermal offset of
    frame -85620).
    """
    # Never fetches moc42r_*.bc, the second CK ISIS's spiceinit web=yes furnishes alongside lrolc_*
    # -- moc42r isn't in the NAIF metakernel this function parses, only in USGS's S3-hosted ISIS
    # kernel database. Kept for direct A/B comparison, not because it's less accurate: lrosc/lrolc
    # and moc42r are the same "Reconstructed" tier in ISIS's own kernel-db vocabulary, and both
    # sources give numerically identical WAC pointing (see select_isis_wac_ck_kernels's comment).
    mk_path = latest_metakernel_url(target_dt.year, config)
    mk_local = cache.fetch_naif_kernel(mk_path, cache_root=config.cache_root, base_url=config.naif_base_url)
    all_paths = parse_metakernel(mk_local.read_text())
    target_doy = doy_code(target_dt)
    return select_date_ranged(all_paths, target_doy, "ck", prefixes=WAC_CK_PREFIXES)


def select_isis_wac_ck_kernels(target_dt: datetime, config: TrntestConfig) -> list[str]:
    """Live default WAC CK source: asks a real ISIS `spiceinit web=yes` run what it actually
    furnishes (`isis_wac.resolve_wac_ck_kernels`), rather than reimplementing USGS's own kernel-db
    selection algorithm in Python.

    Filters `isis_wac.resolve_wac_ck_kernels`'s result (tied to `config.edr_product`, a single fixed
    EDR product, not an arbitrary requested epoch) to kernels whose filename-encoded date range
    covers `target_dt`.

    :returns: `[]` if none of the resolved kernels cover `target_dt` (e.g. `target_dt` falls outside
        that one product's own coverage window, as happens for `dataset.images_for_window()`'s
        multi-candidate sweeps) -- `select_kernels_for` falls back to the deprecated NAIF path for
        that case.
    """
    # Avoids reimplementing USGS's own kernel-db selection algorithm because it has a gap: the route
    # it would need, lroc_kernels.db, is currently empty in USGS's bucket, even though ISIS's live
    # resolution still furnishes an lrolc-equivalent kernel from somewhere.
    #
    # Kept even though it doesn't move any currently-known pointing number: direct verification
    # (comparing this project's SPICE computation against real campt output) found both
    # wac_ck_source options give numerically identical WAC pointing, and the extra kernel this
    # mechanism adds (moc42r_*.bc, bus attitude) has zero measurable effect (SPICE's frame
    # resolution for LRO_LROCWAC_VIS depends entirely on lrolc's own direct CK segments for -85620).
    # Preferred anyway because it makes the furnished kernel set match ISIS's own resolution by
    # construction, rather than hand-picked filename prefixes.
    from trntest import isis_wac  # noqa: PLC0415 -- avoids a circular import

    target_doy = doy_code(target_dt)
    covered = []
    for p in isis_wac.resolve_wac_ck_kernels(config):
        m = DATE_RANGE_RE.search(p)
        if m and int(m.group(1)) <= target_doy <= int(m.group(2)):
            covered.append(p)
    return covered


def select_kernels_for(target_dt: datetime, config: TrntestConfig) -> list[KernelRef]:
    """Resolve the full kernel set for `target_dt`: `ALWAYS_KERNELS`, the WAC CK kernel(s) per
    `config.wac_ck_source`, and the covering SPK trajectory kernel(s).

    :raises ValueError: if `config.wac_ck_source` is neither `"isis_resolved"` nor
        `"naif_metakernel"`.
    :raises RuntimeError: if no WAC CK kernel resolves for `target_dt`.
    """
    if config.wac_ck_source == "isis_resolved":
        ck_paths = select_isis_wac_ck_kernels(target_dt, config)
        if ck_paths:
            ck_refs = [KernelRef("isis_resolved", p) for p in ck_paths]
        else:
            # target_dt falls outside the one fixed EDR product's own resolved coverage window --
            # not a resolution failure, an expected case for wide date-range searches (see
            # select_isis_wac_ck_kernels's own docstring/comment). Safe to fall back to the
            # deprecated NAIF path here: both sources give numerically identical pointing (see
            # select_isis_wac_ck_kernels's comment).
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
    """Fetch `ref` to a local path, via `cache.fetch_naif_kernel` or `cache.fetch_isis_kernel` per
    `ref.source`.

    :raises ValueError: if `ref.source` is neither `"naif"` nor `"isis_resolved"`.
    """
    if ref.source == "naif":
        return str(cache.fetch_naif_kernel(ref.path, cache_root=config.cache_root, base_url=config.naif_base_url))
    if ref.source == "isis_resolved":
        return str(
            cache.fetch_isis_kernel(ref.path, cache_root=config.cache_root, base_url=config.isis_kernel_base_url)
        )
    raise ValueError(f"unknown KernelRef.source {ref.source!r}")


def fetch_and_furnish(target_dt: datetime, config: TrntestConfig | None = None) -> list[str]:
    """Download the minimal kernel set for `target_dt` and `spice.furnsh()` each one.

    Unloads previously-furnished date-ranged (CK/SPK) kernels not needed for `target_dt` first (see
    `_loaded_date_ranged_kernels`), and skips re-furnishing any kernel already loaded, `ALWAYS_KERNELS`
    included (see `_loaded_kernels`).

    :returns: local paths of every kernel furnished for `target_dt`.
    :raises RuntimeError: if the furnished CK kernels don't cover `target_dt` for the WAC frame.
    """
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
    """Furnish the union of `lrorg` trajectory SPK segments covering `[start_dt, end_dt]` all at
    once, left loaded (not tracked for later unloading, unlike `fetch_and_furnish`'s per-epoch
    date-ranged kernels). For SPICE geometry-finder searches (e.g.
    `illumination.find_node_crossings`'s `gfposc` call) that need coverage across a whole window in
    one call, rather than one epoch at a time.

    Reuses `_loaded_kernels`'s dedup tracking, so a later `fetch_and_furnish` call for a date inside
    this range won't re-furnish (or attempt to unload) anything furnished here.
    """
    # SPK-only (no CK): fetch_and_furnish's just-in-time furnish/unload pattern exists to bound CK
    # attitude-kernel accumulation specifically (the kernel type that risks exceeding the kernel
    # pool's fixed buffer, see _loaded_date_ranged_kernels above); SPK trajectory data alone is much
    # smaller, so furnishing a whole search window at once is safe for this repo's typical
    # max_search_days (7-30 days) -- a single SPK segment in practice for a 7-day window.
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
