import dataclasses
from datetime import UTC, datetime

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
