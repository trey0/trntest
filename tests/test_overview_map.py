import pandas as pd

from trntest import overview_map, trn_dataset
from trntest.config import TrntestConfig


def test_dataset_midpoint_datetime_is_halfway_between_earliest_start_and_latest_stop(tmp_path):
    images = pd.DataFrame(
        {
            "product_id": ["P1", "P2"],
            "edr_product": ["P1", "P2"],
            "start_time": ["2019-01-01T00:00:00+00:00", "2019-01-01T02:00:00+00:00"],
            "stop_time": ["2019-01-01T01:00:00+00:00", "2019-01-01T04:00:00+00:00"],
        }
    )
    dataset = trn_dataset.TrnTestDataSet(tmp_path, images, TrntestConfig())

    midpoint = overview_map.dataset_midpoint_datetime(dataset)

    assert midpoint.isoformat() == "2019-01-01T02:00:00+00:00"


def test_dataset_midpoint_datetime_handles_mixed_subsecond_precision(tmp_path):
    """Manifest rows aren't all the same sub-second precision -- some carry fractional seconds,
    some don't. `pd.to_datetime` without an explicit `format` infers one format from the first rows
    and raises on any later row that doesn't match it exactly."""
    images = pd.DataFrame(
        {
            "product_id": ["P1", "P2"],
            "edr_product": ["P1", "P2"],
            "start_time": ["2019-01-01T00:00:00.123456+00:00", "2019-01-01T02:00:00+00:00"],
            "stop_time": ["2019-01-01T01:00:00.654321+00:00", "2019-01-01T04:00:00+00:00"],
        }
    )
    dataset = trn_dataset.TrnTestDataSet(tmp_path, images, TrntestConfig())

    midpoint = overview_map.dataset_midpoint_datetime(dataset)  # must not raise

    assert pd.Timestamp("2019-01-01T00:00:00.123456+00:00") < midpoint < pd.Timestamp("2019-01-01T04:00:00+00:00")


def test_dataset_midpoint_datetime_uses_utc_time_column_for_spice_entry_kind(tmp_path):
    """`entry_kind="spice"` has no `start_time`/`stop_time` columns -- `time_span_columns` maps both
    ends of the span to the same `utc_time` column instead, so the midpoint is still halfway between
    the earliest and latest real timestamp in the manifest."""
    images = pd.DataFrame(
        {
            "product_id": ["P1", "P2"],
            "utc_time": ["2019-01-01T00:00:00+00:00", "2019-01-01T02:00:00+00:00"],
        }
    )
    dataset = trn_dataset.TrnTestDataSet(tmp_path, images, TrntestConfig(), entry_kind="spice")

    midpoint = overview_map.dataset_midpoint_datetime(dataset)

    assert midpoint.isoformat() == "2019-01-01T01:00:00+00:00"
