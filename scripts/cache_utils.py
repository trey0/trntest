"""Shared local-mirror caching for external data (NAIF SPICE kernels, Lunaserv WMS tiles, WAC EDRs).

Pattern: mirror the remote source's own folder structure under CACHE_ROOT, and never re-request a
path that's already present locally. See docs/caching.md for the rationale.
"""
import os
import urllib.parse
from pathlib import Path

import requests

CACHE_ROOT = Path(os.environ.get("LUNAR_DEMO_CACHE", "/workspace/cache"))


def cached_get(url: str, rel_path: str, cache_root: Path = CACHE_ROOT, **requests_kwargs) -> Path:
    """Return a local path for `url`, downloading into cache_root/rel_path only if not already there."""
    dest = cache_root / rel_path
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=60, **requests_kwargs) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    tmp.rename(dest)
    return dest


def naif_rel_path(kernel_path: str) -> str:
    """kernel_path like 'data/ck/lrosc_....bc' or 'extras/mk/lro_2019_v06.tm' -> mirrored cache path."""
    return f"naif/lro-l-spice-6-v1.0/lrosp_1000/{kernel_path}"


def naif_url(kernel_path: str) -> str:
    return f"https://naif.jpl.nasa.gov/pub/naif/pds/data/lro-l-spice-6-v1.0/lrosp_1000/{kernel_path}"


def fetch_naif_kernel(kernel_path: str, cache_root: Path = CACHE_ROOT) -> Path:
    return cached_get(naif_url(kernel_path), naif_rel_path(kernel_path), cache_root=cache_root)


def lunaserv_rel_path(layer: str, bbox, width: int, height: int, fmt: str) -> str:
    bbox_str = "_".join(f"{c:.6f}" for c in bbox)
    ext = "tif" if "tiff" in fmt else fmt.split("/")[-1]
    return f"lunaserv/{layer}/{bbox_str}_{width}x{height}.{ext}"


def fetch_lunaserv_getmap(
    layer: str,
    bbox,
    width: int,
    height: int,
    fmt: str = "image/tiff",
    srs: str = "IAU2000:30100",
    cache_root: Path = CACHE_ROOT,
    base_url: str = "https://wms.im-ldi.com/lunaserv/lunaserv_stage?",
) -> Path:
    params = {
        "request": "GetMap",
        "service": "WMS",
        "version": "1.1.1",
        "layers": layer,
        "styles": "",
        "srs": srs,
        "bbox": ",".join(f"{c:.6f}" for c in bbox),
        "width": str(width),
        "height": str(height),
        "format": fmt,
    }
    url = base_url + urllib.parse.urlencode(params)
    rel_path = lunaserv_rel_path(layer, bbox, width, height, fmt)
    return cached_get(url, rel_path, cache_root=cache_root)


# PDS4 dataset ids for the two LROC processing levels this repo uses.
LROC_EDR_DATASET = "LRO-L-LROC-2-EDR-V1.0"
LROC_CDR_DATASET = "LRO-L-LROC-3-CDR-V1.0"


def lroc_rel_path(dataset: str, volume: str, subdir: str, doy: str, product: str, ext: str) -> str:
    return f"{dataset}/{volume}/DATA/{subdir}/{doy}/WAC/{product}.{ext}"


def fetch_lroc_file(dataset: str, volume: str, subdir: str, doy: str, product: str, ext: str, cache_root: Path = CACHE_ROOT) -> Path:
    url = f"https://pds.lroc.im-ldi.com/data/{dataset}/{volume}/DATA/{subdir}/{doy}/WAC/{product}.{ext}"
    rel_path = lroc_rel_path(dataset, volume, subdir, doy, product, ext)
    return cached_get(url, rel_path, cache_root=cache_root)


def fetch_lroc_edr_file(volume: str, subdir: str, doy: str, product: str, ext: str, cache_root: Path = CACHE_ROOT) -> Path:
    return fetch_lroc_file(LROC_EDR_DATASET, volume, subdir, doy, product, ext, cache_root=cache_root)


def fetch_lroc_cdr_file(volume: str, subdir: str, doy: str, product: str, ext: str, cache_root: Path = CACHE_ROOT) -> Path:
    return fetch_lroc_file(LROC_CDR_DATASET, volume, subdir, doy, product, ext, cache_root=cache_root)
