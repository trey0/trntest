import dataclasses
from datetime import UTC, datetime
from unittest import mock

import pytest

from trntest import spice_kernels
from trntest.config import TrntestConfig


def test_doy_code():
    dt = datetime(2019, 11, 30, tzinfo=UTC)
    assert spice_kernels.doy_code(dt) == 2019334


def test_doy_code_first_day_of_year():
    dt = datetime(2020, 1, 1, tzinfo=UTC)
    assert spice_kernels.doy_code(dt) == 2020001


def test_parse_metakernel():
    text = """
    KERNELS_TO_LOAD = (
        '$KERNELS/lsk/naif0012.tls'
        '$KERNELS/ck/lrosc_2019334_2019335_v01.bc'
    )
    """
    paths = spice_kernels.parse_metakernel(text)
    assert paths == ["data/lsk/naif0012.tls", "data/ck/lrosc_2019334_2019335_v01.bc"]


def test_select_date_ranged_filters_by_subdir_and_date():
    paths = [
        "data/ck/lrosc_2019330_2019340_v01.bc",
        "data/ck/lrosc_2019001_2019010_v01.bc",
        "data/spk/lrorg_2019330_2019340_v01.bsp",
        "data/ck/lrodv_2019330_2019340_v01.bc",
    ]
    selected = spice_kernels.select_date_ranged(paths, target_doy=2019334, subdir="ck", prefixes=("lrosc",))
    assert selected == ["data/ck/lrosc_2019330_2019340_v01.bc"]


def test_select_date_ranged_excludes_out_of_range():
    paths = ["data/ck/lrosc_2019001_2019010_v01.bc"]
    selected = spice_kernels.select_date_ranged(paths, target_doy=2019334, subdir="ck", prefixes=("lrosc",))
    assert selected == []


def test_kernel_ref_hashable_and_deduplicates_in_a_set():
    a = spice_kernels.KernelRef("naif", "data/ck/lrolc_x.bc")
    b = spice_kernels.KernelRef("naif", "data/ck/lrolc_x.bc")
    c = spice_kernels.KernelRef("isis_resolved", "data/ck/lrolc_x.bc")
    assert a == b
    assert {a, b, c} == {a, c}


def test_fetch_kernel_ref_dispatches_by_source(tmp_path, monkeypatch):
    config = TrntestConfig(cache_root=tmp_path)
    monkeypatch.setattr(
        spice_kernels.cache, "fetch_naif_kernel", lambda path, cache_root, base_url: tmp_path / "naif" / path
    )
    monkeypatch.setattr(
        spice_kernels.cache, "fetch_isis_kernel", lambda path, cache_root, base_url: tmp_path / "isis" / path
    )

    naif_result = spice_kernels._fetch_kernel_ref(spice_kernels.KernelRef("naif", "data/ck/x.bc"), config)
    isis_result = spice_kernels._fetch_kernel_ref(spice_kernels.KernelRef("isis_resolved", "kernels/ck/x.bc"), config)

    assert naif_result == str(tmp_path / "naif" / "data/ck/x.bc")
    assert isis_result == str(tmp_path / "isis" / "kernels/ck/x.bc")


def test_fetch_kernel_ref_raises_on_unknown_source():
    with pytest.raises(ValueError, match="unknown"):
        spice_kernels._fetch_kernel_ref(spice_kernels.KernelRef("bogus", "x"), TrntestConfig())


def test_select_kernels_for_raises_on_unknown_wac_ck_source():
    config = dataclasses.replace(TrntestConfig(), wac_ck_source="bogus")
    with pytest.raises(ValueError, match="unknown wac_ck_source"):
        spice_kernels.select_kernels_for(datetime(2019, 11, 30, tzinfo=UTC), config)


def _fake_mk_dir_response(versions=(6,)):
    response = mock.MagicMock()
    response.raise_for_status = mock.Mock()
    response.text = "\n".join(f"lro_2019_v{v:02d}.tm" for v in versions)
    return response


def test_latest_metakernel_url_persists_to_disk_cache(tmp_path, monkeypatch):
    config = TrntestConfig(cache_root=tmp_path)
    get = mock.Mock(return_value=_fake_mk_dir_response())
    monkeypatch.setattr(spice_kernels.requests, "get", get)

    result = spice_kernels.latest_metakernel_url(2019, config)

    assert result == "extras/mk/lro_2019_v06.tm"
    cache_path = tmp_path / "naif_latest_metakernel" / "2019.txt"
    assert cache_path.read_text().strip() == result
    get.assert_called_once()


def test_latest_metakernel_url_reads_disk_cache_without_network(tmp_path, monkeypatch):
    config = TrntestConfig(cache_root=tmp_path)
    cache_path = tmp_path / "naif_latest_metakernel" / "2019.txt"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("extras/mk/lro_2019_v06.tm")
    get = mock.Mock(side_effect=AssertionError("should not hit the network once disk-cached"))
    monkeypatch.setattr(spice_kernels.requests, "get", get)

    result = spice_kernels.latest_metakernel_url(2019, config)

    assert result == "extras/mk/lro_2019_v06.tm"
    get.assert_not_called()


def test_latest_metakernel_url_raises_when_no_version_found(tmp_path, monkeypatch):
    config = TrntestConfig(cache_root=tmp_path)
    monkeypatch.setattr(spice_kernels.requests, "get", mock.Mock(return_value=_fake_mk_dir_response(versions=())))

    with pytest.raises(RuntimeError, match="no metakernel found"):
        spice_kernels.latest_metakernel_url(2019, config)

    assert not (tmp_path / "naif_latest_metakernel" / "2019.txt").exists()
