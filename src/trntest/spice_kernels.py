"""Select and fetch the minimal LRO SPICE kernel set for a given UTC timestamp.

Rather than furnishing a whole year's worth of kernels (the yearly metakernel under
extras/mk/ lists everything for that year, and CK pointing kernels dominate that volume),
we treat the metakernel purely as a manifest: parse it, then download only:

  - the small kernels needed regardless of date (LSK, SCLK, PCK, the two lunar frame
    kernels, the LRO frames kernel, the LROC IK, and the DE421 planetary ephemeris), and
  - the date-ranged CK/SPK files whose filename-encoded range covers our target day, and
    only the CK "flavors" relevant to LROC WAC pointing (`lrosc` = spacecraft bus attitude,
    `lrolc` = LROC-specific thermal offset of frame -85620) -- NOT `lrodv`/`lrohg`/`lrosa`
    (maneuver/antenna/solar-array attitude, irrelevant here).

See docs/data-sources.md and docs/caching.md for the background.
"""

import functools
import re
from datetime import datetime

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

# CK filename prefixes relevant to LROC WAC pointing (see docs/data-sources.md).
WAC_CK_PREFIXES = ("lrosc", "lrolc")

DATE_RANGE_RE = re.compile(r"_(\d{7})_(\d{7})_v\d+\.(bc|bsp)$")

LRO_ID = -85
LRO_SC_BUS_ID = -85000
LRO_LROCWAC_ID = -85620

# Tracks which date-ranged (CK/SPK) kernel paths are currently furnished, so fetch_and_furnish can
# unload ones no longer needed before furnishing a new date's set -- see fetch_and_furnish's
# docstring for why this matters. Process-global by necessity: SPICE's own kernel pool is itself
# global per-process state, so this just mirrors it, rather than introducing new global state on
# top of a purely-functional design.
_loaded_date_ranged_kernels: set[str] = set()

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


@functools.cache
def latest_metakernel_url(year: int, config: TrntestConfig) -> str:
    """Which metakernel is "latest" for `year` doesn't change within a process's lifetime, so this
    is memoized -- without it, callers that furnish kernels for many different dates in one process
    (e.g. dataset.select_dataset() evaluating hundreds of candidate images) would otherwise re-hit
    this live (uncached-by-design, since it's a directory listing, not a specific file) NAIF
    endpoint once per date. Safe to cache: `config` is a hashable frozen dataclass."""
    mk_dir_url = f"{config.naif_base_url}extras/mk/"
    resp = requests.get(mk_dir_url, timeout=30)
    resp.raise_for_status()
    versions = [int(m) for m in re.findall(rf"lro_{year}_v(\d+)\.tm", resp.text)]
    if not versions:
        raise RuntimeError(f"no metakernel found for year {year} at {mk_dir_url}")
    return f"extras/mk/lro_{year}_v{max(versions):02d}.tm"


def parse_metakernel(text: str) -> list[str]:
    """Return kernel paths like 'data/ck/lrosc_..._v01.bc' from a metakernel's KERNELS_TO_LOAD."""
    paths = re.findall(r"\$KERNELS/(\S+\.\w+)", text)
    return [f"data/{p}" for p in paths]


def select_date_ranged(paths: list[str], target_doy: int, subdir: str, prefixes=None) -> list[str]:
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
        if start <= target_doy <= end:
            selected.append(p)
    return selected


def select_kernels_for(target_dt: datetime, config: TrntestConfig) -> list[str]:
    mk_path = latest_metakernel_url(target_dt.year, config)
    mk_local = cache.fetch_naif_kernel(mk_path, cache_root=config.cache_root, base_url=config.naif_base_url)
    all_paths = parse_metakernel(mk_local.read_text())

    target_doy = doy_code(target_dt)
    ck_paths = select_date_ranged(all_paths, target_doy, "ck", prefixes=WAC_CK_PREFIXES)
    spk_paths = select_date_ranged(all_paths, target_doy, "spk", prefixes=("lrorg",))

    return ALWAYS_KERNELS + ck_paths + spk_paths


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
    kernel_paths = select_kernels_for(target_dt, config)
    always_paths = set(ALWAYS_KERNELS)
    needed_date_ranged = set(kernel_paths) - always_paths

    for stale_path in _loaded_date_ranged_kernels - needed_date_ranged:
        stale_local = str(
            cache.fetch_naif_kernel(stale_path, cache_root=config.cache_root, base_url=config.naif_base_url)
        )
        spice.unload(stale_local)
        _loaded_kernels.discard(stale_local)
    _loaded_date_ranged_kernels.clear()
    _loaded_date_ranged_kernels.update(needed_date_ranged)

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
