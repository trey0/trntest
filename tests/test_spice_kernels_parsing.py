from datetime import UTC, datetime

from trntest import spice_kernels


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
