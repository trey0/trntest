from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from trntest import dataset


def test_throttle_by_time_keeps_first_and_filters_close_followers():
    base = datetime(2020, 1, 1, tzinfo=UTC)
    images = pd.DataFrame(
        {
            "start_time": [base, base + timedelta(minutes=1), base + timedelta(minutes=6), base + timedelta(minutes=7)],
            "product_id": ["a", "b", "c", "d"],
        }
    )
    throttled = dataset.throttle_by_time(images, min_gap_minutes=5.0)
    assert list(throttled["product_id"]) == ["a", "c"]


def test_throttle_by_time_sorts_before_throttling():
    base = datetime(2020, 1, 1, tzinfo=UTC)
    images = pd.DataFrame(
        {
            "start_time": [base + timedelta(minutes=10), base],
            "product_id": ["late", "early"],
        }
    )
    throttled = dataset.throttle_by_time(images, min_gap_minutes=5.0)
    assert list(throttled["product_id"]) == ["early", "late"]


def test_write_read_manifest_round_trip(tmp_path):
    base = datetime(2020, 1, 1, tzinfo=UTC)
    images = pd.DataFrame(
        [
            {
                "product_id": "M1TEST",
                "edr_volume": "LROLRC_0041C",
                "edr_subdir": "ESM4",
                "edr_doy": "2019333",
                "edr_product": "M1TEST",
                "cdr_volume": "LROLRC_1041C",
                "cdr_subdir": "ESM4",
                "cdr_doy": "2019333",
                "cdr_product": "M1TESTC",
                "orbit_number": 46980,
                "start_time": base,
                "stop_time": base + timedelta(minutes=6),
                "start_frame": 93.5,
                "center_frame_index": 129.0,
                "n_frames_for_square_crop": 71,
                "sun_elevation_deg": 15.2,
                "incidence_angle_deg": 74.8,
                "center_lat_deg": 86.1,
                "center_lon_deg": 228.5,
            }
        ],
        columns=dataset.DATASET_COLUMNS,
    )

    path = tmp_path / "manifest.csv"
    dataset.write_manifest(images, path)
    round_tripped = dataset.read_manifest(path)

    assert list(round_tripped.columns) == list(images.columns)
    assert round_tripped.loc[0, "product_id"] == "M1TEST"
    assert round_tripped.loc[0, "orbit_number"] == 46980
    assert round_tripped.loc[0, "start_frame"] == pytest.approx(93.5)
    assert round_tripped.loc[0, "start_time"] == base
