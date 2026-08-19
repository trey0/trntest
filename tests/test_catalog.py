from datetime import datetime

import pytest

from trntest import catalog
from trntest.config import TrntestConfig

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
      <Emission_angle>1.19</Emission_angle>
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
      <Emission_angle>0.54</Emission_angle>
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
    assert row["emission_angle_deg"] == pytest.approx(1.19)
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


def _product_xml(product_id: str, orbit_number: int) -> str:
    return f"""<Product>
      <pdsid>wac.{product_id.lower()}</pdsid>
      <Start_orbit_number>{orbit_number}</Start_orbit_number>
      <UTC_start_time>2019-11-29T23:59:24.293000Z</UTC_start_time>
      <UTC_stop_time>2019-11-30T00:05:31.137000Z</UTC_stop_time>
      <Incidence_angle>75.0</Incidence_angle>
      <Emission_angle>1.0</Emission_angle>
      <Center_latitude>10.0</Center_latitude>
      <Center_longitude>20.0</Center_longitude>
      <LabelURL>https://pds.lroc.im-ldi.com/data/LRO-L-LROC-2-EDR-V1.0/LROLRC_0041C/DATA/ESM4/2019333/WAC/{product_id}.xml</LabelURL>
    </Product>"""


_MALFORMED_PRODUCT_XML = "<Product><pdsid>bad</pdsid></Product>"  # missing LabelURL -- dropped by parse_catalog_entries


def test_list_products_keeps_paginating_past_a_page_with_a_parse_failure(monkeypatch):
    """Regression test: a real full-year query hit exactly this -- the server sent a genuinely full
    page (matching _PAGE_SIZE raw <Product> entries), but one entry failed to parse (a missing
    field), landing the *parsed* page just under _PAGE_SIZE. The old `len(page_df) < _PAGE_SIZE`
    termination check read that as "last page" and silently dropped everything after it (a real
    year-long query was truncated to its first 5000 raw entries out of ~100k). Pagination must
    continue based on the server's own raw entry count, not how many of them happened to parse."""
    monkeypatch.setattr(catalog, "_PAGE_SIZE", 3)

    page_1_xml = (
        "<ODEResults><Products>"
        + _product_xml("M1111111111CE", 1)
        + _MALFORMED_PRODUCT_XML
        + _product_xml("M2222222222CE", 2)
        + "</Products></ODEResults>"
    )
    page_2_xml = "<ODEResults><Products>" + _product_xml("M3333333333CE", 3) + "</Products></ODEResults>"

    calls = []

    def fake_query_ode(params, config):
        calls.append(params["offset"])
        return page_1_xml if params["offset"] == 0 else page_2_xml

    monkeypatch.setattr(catalog, "query_ode", fake_query_ode)

    df = catalog.list_products(TrntestConfig(), catalog.EDR_PRODUCT_TYPE, datetime(2019, 1, 1), datetime(2020, 1, 1))

    assert calls == [0, 3]  # a second page was fetched despite page 1 parsing to only 2 rows
    assert sorted(df["product_id"]) == ["M1111111111CE", "M2222222222CE", "M3333333333CE"]


def test_ode_rel_path_is_order_independent_and_unique():
    path_a = catalog.ode_rel_path({"a": 1, "b": 2})
    path_b = catalog.ode_rel_path({"b": 2, "a": 1})
    path_c = catalog.ode_rel_path({"a": 1, "b": 3})
    assert path_a == path_b
    assert path_a != path_c
