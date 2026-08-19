import dataclasses
import json

import pytest

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

# Trimmed fixture modeled on a real `catlab` dump's `InstrumentPointing`/`InstrumentPosition` Table
# objects (captured live this session against product M1327210646CE's cropped cube) -- the real
# on-disk shape `apply_pose_correction_to_crop`'s `_table_extra_label` parses, including a second,
# same-shaped Table object to confirm name-filtering actually discriminates between them.
_TABLES_LABEL_TEXT = """
Object = Table
  Name                = InstrumentPointing
  StartByte           = 13863937
  Bytes               = 16576
  Records             = 259
  ByteOrder           = Lsb
  TimeDependentFrames = (-85620, -85000, 1)
  ConstantFrames      = (-85621, -85620)
  ConstantRotation    = (0.99982051808596, 0.0014619008152411,
                         -0.018889003688109, -0.0013858576920097,
                         0.99999088592261, 0.0040382508789192,
                         0.01889473505452, -0.0040113486148665,
                         0.99981343163088)
  CkTableStartTime    = 625843448.25011
  CkTableEndTime      = 625843811.06261
  CkTableOriginalSize = 259
  FrameTypeCode       = 3
  Description         = "Created by spiceinit"
  Kernels             = ($lro/kernels/ck/lrolc_2019304_2019335_v01.bc,
                         $lro/kernels/ck/moc42r_2019304_2019335_v01.bc,
                         $lro/kernels/fk/lro_frames_2014049_v01.tf)

  Group = Field
    Name = J2000Q0
    Type = Double
    Size = 1
  End_Group
End_Object

Object = Table
  Name                 = InstrumentPosition
  StartByte            = 13880513
  Bytes                = 504
  Records              = 9
  ByteOrder            = Lsb
  CacheType            = HermiteSpline
  SpkTableStartTime    = 625843448.25011
  SpkTableEndTime      = 625843811.06261
  SpkTableOriginalSize = 259.0
  Description          = "Created by spiceinit"
  Kernels              = $lro/kernels/spk/fdf29r_2019305_2019335_v01.bsp

  Group = Field
    Name = J2000X
    Type = Double
    Size = 1
  End_Group
End_Object
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


def test_table_extra_label_keeps_only_the_named_tables_extra_keywords():
    extra = isis_wac._table_extra_label(_TABLES_LABEL_TEXT, "InstrumentPointing")

    for excluded in ("Name", "StartByte", "Bytes", "Records", "ByteOrder", "Field"):
        assert excluded not in extra
    assert extra["TimeDependentFrames"] == [-85620, -85000, 1]
    assert extra["ConstantFrames"] == [-85621, -85620]
    assert len(extra["ConstantRotation"]) == 9
    assert extra["ConstantRotation"][0] == pytest.approx(0.99982051808596)
    assert extra["FrameTypeCode"] == 3


def test_table_extra_label_discriminates_between_same_shaped_tables():
    extra = isis_wac._table_extra_label(_TABLES_LABEL_TEXT, "InstrumentPosition")

    assert "ConstantRotation" not in extra  # only InstrumentPointing has this keyword
    assert extra["CacheType"] == "HermiteSpline"


def test_table_extra_label_raises_for_a_table_name_not_present():
    with pytest.raises(ValueError, match="NotPresent"):
        isis_wac._table_extra_label(_TABLES_LABEL_TEXT, "NotPresent")
