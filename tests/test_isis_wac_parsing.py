import dataclasses
import json

from trntest import isis_wac
from trntest.config import TrntestConfig

# Trimmed fixture modeled on a real `catlab` dump of a spiceinit'd WAC cube's label (captured live
# this session against product M1329714703CE) -- only the fields _parse_ck_kernels_from_label reads.
_LABEL_TEXT = """
Object = IsisCube
  Group = Kernels
    InstrumentPointing = (Table,
                           $lro/kernels/ck/lrolc_2019334_2020001_v01.bc,
                           $lro/kernels/ck/moc42r_2019334_2020001_v01.bc,
                           $lro/kernels/fk/lro_frames_2014049_v01.tf)
  End_Group
End_Object
End
"""


def test_strip_isis_alias_prefix():
    assert isis_wac._strip_isis_alias_prefix("$lro/kernels/ck/moc42r_x.bc") == "kernels/ck/moc42r_x.bc"


def test_parse_ck_kernels_from_label_extracts_ck_paths_only():
    ck_paths = isis_wac._parse_ck_kernels_from_label(_LABEL_TEXT)
    assert ck_paths == [
        "kernels/ck/lrolc_2019334_2020001_v01.bc",
        "kernels/ck/moc42r_2019334_2020001_v01.bc",
    ]


def test_parse_ck_kernels_from_label_skips_table_marker_and_frame_kernel():
    ck_paths = isis_wac._parse_ck_kernels_from_label(_LABEL_TEXT)
    assert "Table" not in ck_paths
    assert not any(p.endswith(".tf") for p in ck_paths)


def test_resolve_wac_ck_kernels_reads_persisted_cache_without_running_the_pipeline(tmp_path):
    config = dataclasses.replace(TrntestConfig(), cache_root=tmp_path, edr_product="TESTPRODUCT")
    cache_path = isis_wac._resolved_wac_ck_cache_path(config)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps(["kernels/ck/fake_x.bc"]))

    # No network/ISIS pipeline call happens here -- if it did, this would fail or hang (no Docker/ISIS
    # environment or network access in a plain pytest run), which is exactly what this test guards
    # against: the persisted-resolution cache must be checked *before* ever calling spiceinit.
    result = isis_wac.resolve_wac_ck_kernels(config)

    assert result == ["kernels/ck/fake_x.bc"]
