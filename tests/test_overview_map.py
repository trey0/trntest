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
