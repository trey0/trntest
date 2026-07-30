from unittest import mock

from trntest import cache


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


def test_lroc_rel_path():
    rel = cache.lroc_rel_path("LRO-L-LROC-2-EDR-V1.0", "LROLRC_0041C", "ESM4", "2019334", "M1329714703CE", "xml")
    assert rel == "LRO-L-LROC-2-EDR-V1.0/LROLRC_0041C/DATA/ESM4/2019334/WAC/M1329714703CE.xml"


def test_cached_get_downloads_once_then_hits_cache(tmp_path):
    cache_root = tmp_path / "cache"
    calls = []

    def fake_get(url, stream, timeout, **kwargs):
        calls.append(url)
        response = mock.MagicMock()
        response.raise_for_status = mock.Mock()
        response.iter_content = mock.Mock(return_value=[b"hello"])
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        return response

    with mock.patch("trntest.cache.requests.get", side_effect=fake_get):
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
        response = mock.MagicMock()
        response.raise_for_status = mock.Mock()
        response.iter_content = mock.Mock(return_value=[b"data"])
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        return response

    with mock.patch("trntest.cache.requests.get", side_effect=fake_get):
        dest = cache.cached_get("https://example.com/f", "sub/f.bin", cache_root=cache_root)

    assert dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()
