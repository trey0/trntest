"""PDS Geosciences Node Orbital Data Explorer (ODE) REST API client -- discovers LROC WAC EDR/CDR
products by time range without downloading each product's individual label. Low-level, takes plain
scalars/config the same way `cache.py` does. Lists of products are `pandas.DataFrame`s rather than a
list of dataclasses -- simpler grouping/filtering downstream (see `candidate_window.py`) and free, readable
table display in the notebook.
"""

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.parse import urlencode

import pandas as pd

from trntest import cache
from trntest.config import TrntestConfig

EDR_PRODUCT_TYPE = "EDRWAC4"
CDR_PRODUCT_TYPE = "CDRWAC4"
IHID = "lro"
IID = "LROC"

CATALOG_COLUMNS = [
    "product_id",
    "volume",
    "subdir",
    "doy",
    "product",
    "orbit_number",
    "start_time",
    "stop_time",
    "incidence_angle_deg",
    "emission_angle_deg",
    "center_lat_deg",
    "center_lon_deg",
]

# Matches e.g. ".../LRO-L-LROC-2-EDR-V1.0/LROLRC_0041C/DATA/ESM4/2019333/WAC/M1329711232CE.xml"
_LABEL_URL_RE = re.compile(r"/(?P<volume>[^/]+)/DATA/(?P<subdir>[^/]+)/(?P<doy>[^/]+)/WAC/(?P<product>[^/.]+)\.")

_PAGE_SIZE = 5000


def ode_rel_path(params: dict) -> str:
    """Deterministic cache key from sorted query params."""
    query_string = urlencode(sorted(params.items()))
    digest = hashlib.sha1(query_string.encode()).hexdigest()
    return f"ode/{digest}.xml"


def query_ode(params: dict, config: TrntestConfig) -> str:
    """Query the ODE REST API, cached.

    :param params: Query parameters.
    :param config: Project config.
    :returns: The raw XML response text.
    """
    query_string = urlencode(sorted(params.items()))
    url = f"{config.ode_base_url}?{query_string}"
    path = cache.cached_get(url, ode_rel_path(params), cache_root=config.cache_root)
    return path.read_text()


def _required_field(element: ET.Element, tag: str) -> str:
    """`element.findtext(tag)`, raising instead of returning `None` if missing.

    :param element: XML element to search.
    :param tag: Child tag name.
    :returns: The field's text.
    :raises ValueError: If the field is missing.
    """
    # So a product missing/malformed one of the expected fields is skipped by
    # `parse_catalog_entries`'s surrounding try/except, instead of silently producing a
    # None/garbage row.
    text = element.findtext(tag)
    if text is None:
        raise ValueError(f"missing expected field: {tag}")
    return text


def _parse_ode_datetime(text: str) -> datetime:
    return datetime.fromisoformat(text)


def parse_catalog_entries(xml_text: str) -> pd.DataFrame:
    """Parse an ODE `results=opmf` product-query XML response.

    :param xml_text: The raw XML response text.
    :returns: A `DataFrame` (columns: `CATALOG_COLUMNS`), one row per product that has every
        expected field -- a product missing/malformed one is skipped, not raised on.
    """
    # Extracts volume/subdir/doy/product from each product's `LabelURL` field (matching this
    # package's existing archive path convention exactly), not the awkward backslash-separated
    # `RelativePathtoVol` field.
    root = ET.fromstring(xml_text)
    rows = []
    for product in root.iter("Product"):
        label_url = product.findtext("LabelURL")
        match = _LABEL_URL_RE.search(label_url) if label_url else None
        if match is None:
            continue  # skip malformed/unexpected entries rather than fail the whole query
        try:
            rows.append(
                {
                    "product_id": match.group("product"),
                    "volume": match.group("volume"),
                    "subdir": match.group("subdir"),
                    "doy": match.group("doy"),
                    "product": match.group("product"),
                    "orbit_number": int(_required_field(product, "Start_orbit_number")),
                    "start_time": _parse_ode_datetime(_required_field(product, "UTC_start_time")),
                    "stop_time": _parse_ode_datetime(_required_field(product, "UTC_stop_time")),
                    "incidence_angle_deg": float(_required_field(product, "Incidence_angle")),
                    "emission_angle_deg": float(_required_field(product, "Emission_angle")),
                    "center_lat_deg": float(_required_field(product, "Center_latitude")),
                    "center_lon_deg": float(_required_field(product, "Center_longitude")),
                }
            )
        except (TypeError, ValueError):
            continue  # a product missing/malformed one of the expected fields -- skip it
    return pd.DataFrame(rows, columns=CATALOG_COLUMNS)


def list_products(
    config: TrntestConfig,
    product_type: str,
    min_time: datetime,
    max_time: datetime,
    extra_params: dict | None = None,
) -> pd.DataFrame:
    """Query ODE for all products of `product_type` observed in `[min_time, max_time]`, paginating
    as needed.

    :param config: Project config.
    :param product_type: ODE product type (`EDR_PRODUCT_TYPE`/`CDR_PRODUCT_TYPE`).
    :param min_time: Window start.
    :param max_time: Window end.
    :param extra_params: Additional query parameters, if any.
    :returns: Matching products.
    """
    frames = []
    offset = 0
    while True:
        params = {
            "target": "moon",
            "query": "product",
            "results": "opmf",
            "ihid": IHID,
            "iid": IID,
            "pt": product_type,
            "minobtime": min_time.strftime("%Y-%m-%dT%H:%M:%S"),
            "maxobtime": max_time.strftime("%Y-%m-%dT%H:%M:%S"),
            "limit": _PAGE_SIZE,
            "offset": offset,
            "order": "oba",
            "output": "XML",
        }
        if extra_params:
            params.update(extra_params)
        xml_text = query_ode(params, config)
        # Whether to fetch another page must be decided from the server's own raw entry count, not
        # len(page_df) below -- a page can (and, confirmed live on a full-year query, does) have a
        # handful of entries `parse_catalog_entries` drops for a missing/malformed field (see its
        # own docstring), landing page_df just under _PAGE_SIZE even though the server sent a
        # genuinely full page with more results still to come. Using the parsed count as the "was
        # this the last page" signal silently truncated a year-long query to its first 5000 raw
        # entries (4996 parsed) out of what should have been ~100k.
        raw_entry_count = xml_text.count("<Product>")
        if raw_entry_count == 0:
            break
        page_df = parse_catalog_entries(xml_text)
        if not page_df.empty:
            frames.append(page_df)
        if raw_entry_count < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    if not frames:
        return pd.DataFrame(columns=CATALOG_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def find_matching_cdr(edr_row: pd.Series, config: TrntestConfig) -> pd.Series | None:
    """Find the CDR counterpart of an EDR catalog row (same orbit, same acquisition).

    :param edr_row: An EDR catalog row (e.g. from `list_products`).
    :param config: Project config.
    :returns: The matching CDR row, or `None` if no match is found (caller should skip and warn, not
        crash).
    """
    # Queries a tight time window around the EDR's own start/stop time.
    pad = timedelta(minutes=10)
    candidates = list_products(config, CDR_PRODUCT_TYPE, edr_row["start_time"] - pad, edr_row["stop_time"] + pad)
    matches = candidates[candidates["orbit_number"] == edr_row["orbit_number"]]
    if matches.empty:
        return None
    return matches.iloc[0]
