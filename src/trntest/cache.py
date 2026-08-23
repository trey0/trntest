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
import time
import urllib.parse
from pathlib import Path

import requests

# A from-cold `dataset.images_for_window()` sweep calls `cached_get` up to ~1600 times in a plain
# sequential loop with no pacing at all between requests -- confirmed (docs/history.md's Phase 36
# follow-up) to be enough on its own, no concurrent caller needed, to trip a real server-side
# rate limiter (~3.5 req/s sustained for ~8 minutes). This fixed floor between real requests (never
# applied on a cache hit -- see the early return below) costs nothing in the normal, mostly-cached
# case and only meaningfully slows down exactly the bulk-fresh-fetch case that needs slowing down.
# Raised from 0.2 to 0.5 after a real 429 (1hr ban) on the LROC EDR host, resolving one real
# ~200-candidate orbit-sequence window (docs/history.md's dated entry) -- root cause wasn't fully
# pinned down (the failure hit on the very first request of that run, suggesting the host may
# already have been degraded rather than this session's own pacing being the sole cause), but a more
# conservative floor is cheap insurance: deliberately a single, general constant (not host-specific)
# per the same reasoning as the original 0.2 -- real usage is either warm-cache (free either way) or
# a genuinely large batch, where the difference is an insignificant fraction of total time.
_REQUEST_PACING_SECONDS = 0.5
_MAX_FETCH_ATTEMPTS = 3
# A `Retry-After` longer than this isn't worth retrying inline -- e.g. the 3600s (1hr) CloudFront
# sent LROC's own EDR host during the same Phase 36 incident. Failing fast lets the caller decide
# whether to abort rather than have `cached_get` silently block for up to an hour.
_MAX_RETRY_AFTER_SECONDS = 30.0

# One shared connection pool (keep-alive) for every real fetch this process makes, rather than a
# fresh TCP/TLS handshake per `requests.get` call -- lighter on the remote server and faster for us.
_SESSION = requests.Session()

_HTTP_TOO_MANY_REQUESTS = 429


class FetchError(Exception):
    """Raised by `cached_get` when a remote fetch fails after exhausting `_MAX_FETCH_ATTEMPTS` (or
    hits a 429 with a `Retry-After` too long to wait out inline -- see `_MAX_RETRY_AFTER_SECONDS`).

    Signals a systemic problem (server down, rate-limited, network unreachable) rather than a single
    bad input, so a caller sweeping many items -- `dataset._evaluate_illuminated_candidates`,
    `dataset.generate_dataset` -- must let this propagate and abort the whole operation instead of
    catching it alongside ordinary per-item errors and skip-and-continuing: that exact anti-pattern
    (docs/history.md's Phase 36 follow-up) is what turned one rate-limited request into ~1350 more
    of them fired at an already-refusing server, in the same sweep that had no pacing either."""


def _parse_retry_after_seconds(header_value: str | None) -> float | None:
    """Only the delay-seconds form of `Retry-After` (e.g. "3600") is handled -- the HTTP-date form
    is rare in practice for this kind of API and not worth parsing; treated as unknown (`None`),
    same as a missing header, which `cached_get` conservatively treats as "too long to wait out"."""
    if header_value is None:
        return None
    try:
        return float(header_value)
    except ValueError:
        return None


def cached_get(
    url: str, rel_path: str, cache_root: Path, max_attempts: int = _MAX_FETCH_ATTEMPTS, **requests_kwargs
) -> Path:
    """Return a local path for `url`, downloading into cache_root/rel_path only if not already there.

    Downloads to a uniquely-named temp file (not a fixed `dest.name + ".part"` path) before
    renaming into place -- a fixed, shared temp path per destination was found to cause real,
    reproducible `tmp.rename(dest)` failures ("No such file or directory") under the rapid
    sequential I/O of evaluating many catalog candidates back-to-back (confirmed: ~1600 sequential
    fetches in one `select_dataset()` run, 69 hit this exact failure). `tempfile.mkstemp` claims
    the unique path atomically. On failure, the temp file is removed rather than left behind.

    Retries a failed fetch up to `max_attempts` times with a short exponential backoff (1s, 2s, 4s,
    ...), except a 429 -- handled separately via `Retry-After` (see `_MAX_RETRY_AFTER_SECONDS`).
    Once attempts are exhausted, raises `FetchError` (chained from the last real exception) rather
    than the raw underlying exception, so callers can distinguish "this fetch is systemically broken"
    from any other failure and choose to abort a whole batch rather than skip one item -- see
    `FetchError`'s own docstring for why that distinction matters here specifically."""
    dest = cache_root / rel_path
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        time.sleep(_REQUEST_PACING_SECONDS)
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".part")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            with _SESSION.get(url, stream=True, timeout=60, **requests_kwargs) as resp:
                if resp.status_code == _HTTP_TOO_MANY_REQUESTS:
                    retry_after = _parse_retry_after_seconds(resp.headers.get("Retry-After"))
                    if retry_after is None or retry_after > _MAX_RETRY_AFTER_SECONDS or attempt == max_attempts:
                        raise FetchError(
                            f"{url}: rate-limited (429) after {attempt} attempt(s); Retry-After="
                            f"{retry_after if retry_after is not None else 'unset'}s "
                            f"exceeds the {_MAX_RETRY_AFTER_SECONDS}s inline-retry cap, or attempts "
                            "are exhausted -- not waiting it out"
                        )
                    tmp.unlink(missing_ok=True)
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
            tmp.rename(dest)
            return dest
        except FetchError:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: any of these means "retry",
            # not KeyboardInterrupt/SystemExit, which must propagate immediately, not be retried.
            tmp.unlink(missing_ok=True)
            last_exc = exc
            if attempt == max_attempts:
                raise FetchError(f"{url}: failed after {max_attempts} attempt(s): {exc}") from exc
            time.sleep(min(2**attempt, 8))
    raise FetchError(f"{url}: failed after {max_attempts} attempt(s)") from last_exc


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


def robbins_craters_rel_path(url: str) -> str:
    """robbins_craters/<filename>.zip -- a single named file (there's only ever one Robbins database
    to fetch, not one per bbox/resolution), same shape as `astropedia_rel_path`. `.zip` is appended
    explicitly since the upstream URL's own final path segment has no extension (a CKAN
    resource-download route) -- confirmed via the real response's `Content-Type: application/zip`."""
    return f"robbins_craters/{url.rsplit('/', maxsplit=1)[-1]}.zip"


def fetch_robbins_craters(cache_root: Path, base_url: str) -> Path:
    """Download and cache the Robbins lunar crater database's raw zip (~92MB, see
    docs/data-sources.md) once. Plain `cached_get`, not `fetch_astropedia_gld100`'s special
    resumable-curl path -- that path exists specifically because GLD100 is ~10GB, too large for
    `cached_get`'s whole-response-then-rename shape to be a good fit; 92MB is comfortably fine for
    it."""
    return cached_get(base_url, robbins_craters_rel_path(base_url), cache_root=cache_root)


def isis_kernel_rel_path(rel_path: str) -> str:
    """rel_path like 'kernels/ck/moc42r_2019334_2020001_v01.bc' -> cache path under isisdata/lro/... --
    deliberately mirrors $ISISDATA/lro/...'s own real layout (not a new independent subtree), so a
    file cached here already sits where a future local (non-web) spiceinit run, or a fuller
    `downloadIsisData lro` fetch, would expect to find it. `isis_wac.ensure_isisdata()`'s own
    --include filter deliberately excludes kernels/ck/ -- this is a narrow, additive exception living
    alongside it, not a change to it. See docs/data-sources.md for the USGS S3 bucket this mirrors."""
    return f"isisdata/lro/{rel_path}"


def fetch_isis_kernel(rel_path: str, cache_root: Path, base_url: str) -> Path:
    """Fetch a kernel USGS's ISIS kernel database resolved (see spice_kernels.py's
    `select_isis_wac_ck_kernels`), from USGS's public S3 bucket rather than NAIF's archive -- same
    `cached_get` shape as `fetch_naif_kernel`. Confirmed live: a 30-day `moc42r_*` CK merge is
    ~1.65 GiB, comparable to what `fetch_naif_kernel` already streams for `lrosc`/`lrolc` today
    (~529MB/10-day chunk per docs/caching.md) -- no special resumable-curl handling needed here,
    unlike `fetch_astropedia_gld100`'s ~10GB single-file case."""
    return cached_get(f"{base_url}{rel_path}", isis_kernel_rel_path(rel_path), cache_root=cache_root)


def wac_emp_rel_path(product_id: str) -> str:
    """product_id like 'WAC_EMP_643NM_E300N1350_304P' (no extension) -> cache path -- one file per
    tile/wavelength/ppd combination, same one-named-file-per-real-thing shape as
    `astropedia_rel_path`, just parametrized instead of singular (WAC_EMP has multiple real tiles,
    GLD100 doesn't)."""
    return f"pds_wac_emp/{product_id}.IMG"


def fetch_wac_emp_tile(product_id: str, cache_root: Path, base_url: str) -> Path:
    """Fetch one WAC_EMP PDS4 archive tile (see `lunaserv.wac_emp_tile_id_for_bbox`) and cache it
    locally, once. Plain `cached_get`, not `fetch_astropedia_gld100`'s special resumable-curl path --
    a WAC_EMP tile is ~1.86GB at 304 ppd (see docs/data-sources.md), comfortably within `cached_get`'s
    range, the same call already made for `fetch_isis_kernel`'s ~1.65GB CK merges."""
    return cached_get(f"{base_url}{product_id}.IMG", wac_emp_rel_path(product_id), cache_root=cache_root)


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
