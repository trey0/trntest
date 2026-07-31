import pytest

from trntest import catalog

# Structure based on a real captured ODE `results=opmf` response (see docs/data-sources.md-style
# research notes) -- trimmed to just the fields parse_catalog_entries actually reads.
FIXTURE_XML = """<?xml version="1.0" encoding="utf-8"?>
<ODEResults>
  <Products>
    <Product>
      <pdsid>wac.m1329711232ce</pdsid>
      <Start_orbit_number>46980</Start_orbit_number>
      <UTC_start_time>2019-11-29T23:59:24.293000Z</UTC_start_time>
      <UTC_stop_time>2019-11-30T00:05:31.137000Z</UTC_stop_time>
      <Incidence_angle>88.93</Incidence_angle>
      <Center_latitude>86.104</Center_latitude>
      <Center_longitude>228.5067</Center_longitude>
      <LabelURL>https://pds.lroc.im-ldi.com/data/LRO-L-LROC-2-EDR-V1.0/LROLRC_0041C/DATA/ESM4/2019333/WAC/M1329711232CE.xml</LabelURL>
    </Product>
    <Product>
      <pdsid>wac.m1329714703ce</pdsid>
      <Start_orbit_number>46981</Start_orbit_number>
      <UTC_start_time>2019-11-30T00:57:15.433000Z</UTC_start_time>
      <UTC_stop_time>2019-11-30T01:03:42.120000Z</UTC_stop_time>
      <Incidence_angle>75.2</Incidence_angle>
      <Center_latitude>81.4</Center_latitude>
      <Center_longitude>116.6</Center_longitude>
      <LabelURL>https://pds.lroc.im-ldi.com/data/LRO-L-LROC-2-EDR-V1.0/LROLRC_0041C/DATA/ESM4/2019334/WAC/M1329714703CE.xml</LabelURL>
    </Product>
  </Products>
</ODEResults>"""


def test_parse_catalog_entries_extracts_path_components_and_fields():
    df = catalog.parse_catalog_entries(FIXTURE_XML)
    assert list(df.columns) == catalog.CATALOG_COLUMNS
    assert len(df) == 2

    row = df.iloc[0]
    assert row["product_id"] == "M1329711232CE"
    assert row["volume"] == "LROLRC_0041C"
    assert row["subdir"] == "ESM4"
    assert row["doy"] == "2019333"
    assert row["product"] == "M1329711232CE"
    assert row["orbit_number"] == 46980
    assert row["incidence_angle_deg"] == pytest.approx(88.93)
    assert row["center_lat_deg"] == pytest.approx(86.104)
    assert row["center_lon_deg"] == pytest.approx(228.5067)


def test_parse_catalog_entries_skips_entry_missing_label_url():
    xml_text = "<ODEResults><Products><Product><pdsid>bad</pdsid></Product></Products></ODEResults>"
    df = catalog.parse_catalog_entries(xml_text)
    assert df.empty
    assert list(df.columns) == catalog.CATALOG_COLUMNS


def test_parse_catalog_entries_skips_entry_missing_other_fields():
    xml_text = """<ODEResults><Products><Product>
        <LabelURL>https://pds.lroc.im-ldi.com/data/LRO-L-LROC-2-EDR-V1.0/LROLRC_0041C/DATA/ESM4/2019333/WAC/M1329711232CE.xml</LabelURL>
    </Product></Products></ODEResults>"""
    df = catalog.parse_catalog_entries(xml_text)
    assert df.empty


def test_ode_rel_path_is_order_independent_and_unique():
    path_a = catalog.ode_rel_path({"a": 1, "b": 2})
    path_b = catalog.ode_rel_path({"b": 2, "a": 1})
    path_c = catalog.ode_rel_path({"a": 1, "b": 3})
    assert path_a == path_b
    assert path_a != path_c
