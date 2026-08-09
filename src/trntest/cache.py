"""Shared local-mirror caching for external data (NAIF SPICE kernels, Lunaserv WMS tiles, WAC EDRs).

Pattern: mirror the remote source's own folder structure under a cache root, and never re-request a
path that's already present locally. See docs/caching.md for the rationale.

Low-level module: takes plain scalars (cache roots, base URLs), not a `TrntestConfig` -- callers
(spice_kernels.py, lunaserv.py, camera.py, wac.py) source those values from a resolved config and
pass them down explicitly.
"""

import os
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

import requests


def cached_get(url: str, rel_path: str, cache_root: Path, **requests_kwargs) -> Path:
    """Return a local path for `url`, downloading into cache_root/rel_path only if not already there.

    Downloads to a uniquely-named temp file (not a fixed `dest.name + ".part"` path) before
    renaming into place -- a fixed, shared temp path per destination was found to cause real,
    reproducible `tmp.rename(dest)` failures ("No such file or directory") under the rapid
    sequential I/O of evaluating many catalog candidates back-to-back (confirmed: ~1600 sequential
    fetches in one `select_dataset()` run, 69 hit this exact failure). `tempfile.mkstemp` claims
    the unique path atomically. On failure, the temp file is removed rather than left behind."""
    dest = cache_root / rel_path
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".part")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with requests.get(url, stream=True, timeout=60, **requests_kwargs) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        tmp.rename(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def naif_rel_path(kernel_path: str) -> str:
    """kernel_path like 'data/ck/lrosc_....bc' or 'extras/mk/lro_2019_v06.tm' -> mirrored cache path."""
    return f"naif/lro-l-spice-6-v1.0/lrosp_1000/{kernel_path}"


def naif_url(kernel_path: str, base_url: str) -> str:
    return f"{base_url}{kernel_path}"


def fetch_naif_kernel(kernel_path: str, cache_root: Path, base_url: str) -> Path:
    return cached_get(naif_url(kernel_path, base_url), naif_rel_path(kernel_path), cache_root=cache_root)


def lunaserv_rel_path(layer: str, bbox, width: int, height: int, fmt: str) -> str:
    bbox_str = "_".join(f"{c:.6f}" for c in bbox)
    ext = "tif" if "tiff" in fmt else fmt.rsplit("/", maxsplit=1)[-1]
    return f"lunaserv/{layer}/{bbox_str}_{width}x{height}.{ext}"


def fetch_lunaserv_getmap(
    layer: str,
    bbox,
    width: int,
    height: int,
    cache_root: Path,
    srs: str,
    base_url: str,
    fmt: str = "image/tiff",
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


def astropedia_rel_path(url: str) -> str:
    """astropedia/<filename> -- a single named file, unlike the other fetch helpers' per-request-
    parametrized paths (there's only ever one Astropedia GLD100 file, not one per bbox/resolution)."""
    return f"astropedia/{url.rsplit('/', maxsplit=1)[-1]}"


def fetch_astropedia_gld100(cache_root: Path, base_url: str) -> Path:
    """Download and cache Astropedia's flat-file GLD100 DEM (~10GB, see docs/data-sources.md) once,
    resumably.

    Deliberately *not* built on `cached_get` above -- that function downloads to a freshly
    uniquely-named temp file every call and deletes it on any failure, both correct for small WMS
    tiles but actively wrong for one huge file: a fresh random name each attempt gives `curl -C -`
    nothing to resume *from*, and deleting a mostly-complete download on a transient failure would
    throw away exactly the progress being protected. Uses a stable `<dest>.part` path instead, and
    deliberately leaves it in place on failure for the next call to resume from.

    `curl -C -` (continue-at), not a hand-rolled `requests` Range-header implementation: `curl` is
    already a Docker image dependency (see `docker/Dockerfile`'s ASP tarball fetch), and its resume
    support is mature and well-tested -- confirmed empirically against this exact file/server (not
    just assumed from curl's own docs), see `docs/history.md`'s dated entry. Not run through
    `subprocess_utils.run_quiet` (that helper is scoped to ASP/ISIS binary calls) -- curl's own
    progress meter has real, live value for a transfer this size, unlike a quick ASP tool call's
    noise, so it's left to print directly rather than captured."""
    dest = cache_root / astropedia_rel_path(base_url)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.parent / (dest.name + ".part")
    result = subprocess.run(["curl", "-fL", "-C", "-", "-o", str(partial), base_url], check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"curl failed (exit {result.returncode}) downloading {base_url} -- "
            f"partial download kept at {partial} for the next call to resume from"
        )
    partial.rename(dest)
    return dest


def lroc_rel_path(dataset: str, volume: str, subdir: str, doy: str, product: str, ext: str) -> str:
    return f"{dataset}/{volume}/DATA/{subdir}/{doy}/WAC/{product}.{ext}"


def fetch_lroc_file(
    dataset: str,
    volume: str,
    subdir: str,
    doy: str,
    product: str,
    ext: str,
    cache_root: Path,
    base_url: str,
) -> Path:
    url = f"{base_url}{dataset}/{volume}/DATA/{subdir}/{doy}/WAC/{product}.{ext}"
    rel_path = lroc_rel_path(dataset, volume, subdir, doy, product, ext)
    return cached_get(url, rel_path, cache_root=cache_root)
