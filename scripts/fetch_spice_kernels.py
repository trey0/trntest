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
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
import spiceypy as spice

from cache_utils import fetch_naif_kernel, naif_url, CACHE_ROOT

MK_DIR_URL = "https://naif.jpl.nasa.gov/pub/naif/pds/data/lro-l-spice-6-v1.0/lrosp_1000/extras/mk/"

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


def doy_code(dt: datetime) -> int:
    """YYYYDDD integer used in NAIF's LRO kernel filenames, e.g. 2019-11-30 -> 2019334."""
    return dt.year * 1000 + dt.timetuple().tm_yday


def latest_metakernel_url(year: int) -> str:
    resp = requests.get(MK_DIR_URL, timeout=30)
    resp.raise_for_status()
    versions = [int(m) for m in re.findall(rf"lro_{year}_v(\d+)\.tm", resp.text)]
    if not versions:
        raise RuntimeError(f"no metakernel found for year {year} at {MK_DIR_URL}")
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


def select_kernels_for(target_dt: datetime) -> list[str]:
    mk_path = latest_metakernel_url(target_dt.year)
    mk_local = fetch_naif_kernel(mk_path)
    all_paths = parse_metakernel(mk_local.read_text())

    target_doy = doy_code(target_dt)
    ck_paths = select_date_ranged(all_paths, target_doy, "ck", prefixes=WAC_CK_PREFIXES)
    spk_paths = select_date_ranged(all_paths, target_doy, "spk", prefixes=("lrorg",))

    return ALWAYS_KERNELS + ck_paths + spk_paths


def fetch_and_furnish(target_dt: datetime) -> list[str]:
    """Download the minimal kernel set for target_dt and spice.furnsh() each one. Returns paths."""
    kernel_paths = select_kernels_for(target_dt)
    local_paths = [str(fetch_naif_kernel(p)) for p in kernel_paths]
    for lp in local_paths:
        spice.furnsh(lp)
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


LRO_ID = -85
LRO_SC_BUS_ID = -85000
LRO_LROCWAC_ID = -85620

if __name__ == "__main__":
    # Example / smoke test: the WAC EDR chosen for this demo (see docs/data-sources.md).
    target = datetime(2019, 11, 30, 0, 57, 15, tzinfo=timezone.utc)
    paths = fetch_and_furnish(target)
    print(f"Furnished {len(paths)} kernels for {target.isoformat()}:")
    for p in paths:
        print(" ", p)

    sclk_string = "1/596768235:26909"
    et = spice.scs2e(LRO_ID, sclk_string)
    print(f"\nSCLK '{sclk_string}' -> ET {et}")
    print("SC_BUS attitude (lrosc) covers this time:", verify_ck_coverage(LRO_SC_BUS_ID, et))
    print("WAC frame offset (lrolc) covers this time:", verify_ck_coverage(LRO_LROCWAC_ID, et))
