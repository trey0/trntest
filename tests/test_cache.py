from unittest import mock

import pytest

from trntest import cache


@pytest.fixture(autouse=True)
def _no_real_sleeps(monkeypatch):
    """`cached_get` now sleeps for pacing/backoff on every real fetch attempt -- patched to a no-op
    everywhere in this file so the suite stays fast and deterministic regardless of the real
    `_REQUEST_PACING_SECONDS`/backoff values."""
    monkeypatch.setattr(cache.time, "sleep", lambda _seconds: None)


def _fake_response(body: bytes = b"data", status_code: int = 200, headers: dict | None = None):
    response = mock.MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.raise_for_status = mock.Mock()
    response.iter_content = mock.Mock(return_value=[body])
    response.__enter__ = mock.Mock(return_value=response)
    response.__exit__ = mock.Mock(return_value=False)
    return response


def test_naif_rel_path():
    assert cache.naif_rel_path("data/ck/lrosc_x.bc") == "naif/lro-l-spice-6-v1.0/lrosp_1000/data/ck/lrosc_x.bc"


def test_naif_url():
    assert (
        cache.naif_url("data/ck/lrosc_x.bc", "https://example.com/base/")
        == "https://example.com/base/data/ck/lrosc_x.bc"
    )


def test_lunaserv_rel_path_tiff():
    rel = cache.lunaserv_rel_path("luna_wac_global", (1.0, 2.0, 3.0, 4.0), 256, 256, "image/tiff")
    assert rel == "lunaserv/luna_wac_global/1.000000_2.000000_3.000000_4.000000_256x256.tif"


def test_lunaserv_rel_path_non_tiff_format():
    rel = cache.lunaserv_rel_path("layer", (0.0, 0.0, 1.0, 1.0), 10, 10, "image/png")
    assert rel.endswith(".png")


def test_isis_kernel_rel_path():
    assert cache.isis_kernel_rel_path("kernels/ck/moc42r_x.bc") == "isisdata/lro/kernels/ck/moc42r_x.bc"


def test_fetch_isis_kernel_constructs_expected_url(tmp_path):
    cache_root = tmp_path / "cache"
    calls = []

    def fake_get(url, stream, timeout, **kwargs):
        calls.append(url)
        return _fake_response()

    with mock.patch.object(cache._SESSION, "get", side_effect=fake_get):
        dest = cache.fetch_isis_kernel(
            "kernels/ck/moc42r_x.bc", cache_root=cache_root, base_url="https://example.com/usgs_data/lro/"
        )

    assert calls == ["https://example.com/usgs_data/lro/kernels/ck/moc42r_x.bc"]
    assert dest == cache_root / "isisdata" / "lro" / "kernels" / "ck" / "moc42r_x.bc"


def test_robbins_craters_rel_path():
    rel = cache.robbins_craters_rel_path("https://example.com/ckan/dataset/x/resource/y/download/lunar_crater_db")
    assert rel == "robbins_craters/lunar_crater_db.zip"


def test_fetch_robbins_craters_constructs_expected_url(tmp_path):
    cache_root = tmp_path / "cache"
    calls = []

    def fake_get(url, stream, timeout, **kwargs):
        calls.append(url)
        return _fake_response()

    url = "https://example.com/ckan/dataset/x/resource/y/download/lunar_crater_db"
    with mock.patch.object(cache._SESSION, "get", side_effect=fake_get):
        dest = cache.fetch_robbins_craters(cache_root=cache_root, base_url=url)

    assert calls == [url]
    assert dest == cache_root / "robbins_craters" / "lunar_crater_db.zip"


def test_lroc_rel_path():
    rel = cache.lroc_rel_path("LRO-L-LROC-2-EDR-V1.0", "LROLRC_0041C", "ESM4", "2019334", "M1329714703CE", "xml")
    assert rel == "LRO-L-LROC-2-EDR-V1.0/LROLRC_0041C/DATA/ESM4/2019334/WAC/M1329714703CE.xml"


def test_cached_get_downloads_once_then_hits_cache(tmp_path):
    cache_root = tmp_path / "cache"
    calls = []

    def fake_get(url, stream, timeout, **kwargs):
        calls.append(url)
        return _fake_response(b"hello")

    with mock.patch.object(cache._SESSION, "get", side_effect=fake_get):
        dest1 = cache.cached_get("https://example.com/f", "sub/f.bin", cache_root=cache_root)
        assert dest1.read_bytes() == b"hello"
        assert len(calls) == 1

        # Second call for the same rel_path must not re-request the URL.
        dest2 = cache.cached_get("https://example.com/f", "sub/f.bin", cache_root=cache_root)
        assert dest2 == dest1
        assert len(calls) == 1


def test_cached_get_uses_part_file_then_renames(tmp_path):
    cache_root = tmp_path / "cache"

    def fake_get(url, stream, timeout, **kwargs):
        return _fake_response()

    with mock.patch.object(cache._SESSION, "get", side_effect=fake_get):
        dest = cache.cached_get("https://example.com/f", "sub/f.bin", cache_root=cache_root)

    assert dest.exists()
    # Unique-per-call temp name (not a fixed dest.name + ".part" path) -- glob for any leftover.
    assert list(dest.parent.glob(f"{dest.name}.*.part")) == []


def _fake_get_always_raising():
    def fake_get(url, stream, timeout, **kwargs):
        response = mock.MagicMock()
        response.status_code = 200
        response.raise_for_status = mock.Mock()

        def raising_iter_content(chunk_size):
            yield b"partial"
            raise ConnectionError("simulated mid-download failure")

        response.iter_content = raising_iter_content
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        return response

    return fake_get


def test_cached_get_cleans_up_temp_file_on_failure(tmp_path):
    cache_root = tmp_path / "cache"

    with mock.patch.object(cache._SESSION, "get", side_effect=_fake_get_always_raising()):
        with pytest.raises(cache.FetchError):
            cache.cached_get("https://example.com/f", "sub/f.bin", cache_root=cache_root, max_attempts=1)

    dest = cache_root / "sub" / "f.bin"
    assert not dest.exists()
    assert list(dest.parent.glob(f"{dest.name}.*.part")) == []


def test_cached_get_raises_fetch_error_chained_from_original_exception(tmp_path):
    cache_root = tmp_path / "cache"

    with mock.patch.object(cache._SESSION, "get", side_effect=_fake_get_always_raising()):
        with pytest.raises(cache.FetchError) as exc_info:
            cache.cached_get("https://example.com/f", "sub/f.bin", cache_root=cache_root, max_attempts=1)

    assert isinstance(exc_info.value.__cause__, ConnectionError)


def test_cached_get_retries_and_succeeds_after_a_transient_failure(tmp_path):
    cache_root = tmp_path / "cache"
    attempts = []

    def fake_get(url, stream, timeout, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("simulated transient failure")
        return _fake_response(b"hello")

    with mock.patch.object(cache._SESSION, "get", side_effect=fake_get):
        dest = cache.cached_get("https://example.com/f", "sub/f.bin", cache_root=cache_root, max_attempts=3)

    assert len(attempts) == 2
    assert dest.read_bytes() == b"hello"


def test_cached_get_raises_fetch_error_after_exhausting_max_attempts(tmp_path):
    cache_root = tmp_path / "cache"
    attempts = []

    def fake_get(url, stream, timeout, **kwargs):
        attempts.append(1)
        raise ConnectionError("simulated persistent failure")

    with mock.patch.object(cache._SESSION, "get", side_effect=fake_get):
        with pytest.raises(cache.FetchError):
            cache.cached_get("https://example.com/f", "sub/f.bin", cache_root=cache_root, max_attempts=3)

    assert len(attempts) == 3


def test_cached_get_retries_429_when_retry_after_is_within_cap(tmp_path):
    cache_root = tmp_path / "cache"
    attempts = []

    def fake_get(url, stream, timeout, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            return _fake_response(status_code=429, headers={"Retry-After": "5"})
        return _fake_response(b"hello")

    with mock.patch.object(cache._SESSION, "get", side_effect=fake_get):
        dest = cache.cached_get("https://example.com/f", "sub/f.bin", cache_root=cache_root, max_attempts=3)

    assert len(attempts) == 2
    assert dest.read_bytes() == b"hello"


def test_cached_get_raises_fetch_error_immediately_when_retry_after_exceeds_cap(tmp_path):
    cache_root = tmp_path / "cache"
    attempts = []

    def fake_get(url, stream, timeout, **kwargs):
        attempts.append(1)
        return _fake_response(status_code=429, headers={"Retry-After": "3600"})

    with mock.patch.object(cache._SESSION, "get", side_effect=fake_get):
        with pytest.raises(cache.FetchError):
            cache.cached_get("https://example.com/f", "sub/f.bin", cache_root=cache_root, max_attempts=3)

    # Not worth burning all 3 attempts sleeping out a 3600s Retry-After -- fails on the first.
    assert len(attempts) == 1


def test_cached_get_raises_fetch_error_immediately_when_retry_after_is_missing(tmp_path):
    cache_root = tmp_path / "cache"
    attempts = []

    def fake_get(url, stream, timeout, **kwargs):
        attempts.append(1)
        return _fake_response(status_code=429, headers={})

    with mock.patch.object(cache._SESSION, "get", side_effect=fake_get):
        with pytest.raises(cache.FetchError):
            cache.cached_get("https://example.com/f", "sub/f.bin", cache_root=cache_root, max_attempts=3)

    assert len(attempts) == 1


def test_cached_get_paces_real_requests(tmp_path, monkeypatch):
    cache_root = tmp_path / "cache"
    sleeps = []
    monkeypatch.setattr(cache.time, "sleep", sleeps.append)

    def fake_get(url, stream, timeout, **kwargs):
        return _fake_response()

    with mock.patch.object(cache._SESSION, "get", side_effect=fake_get):
        cache.cached_get("https://example.com/f", "sub/f.bin", cache_root=cache_root)

    assert cache._REQUEST_PACING_SECONDS in sleeps
