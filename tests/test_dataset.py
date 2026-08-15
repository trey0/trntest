from datetime import UTC, datetime, timedelta
from unittest import mock

import pandas as pd
import pytest
import spiceypy as spice

from trntest import cache, dataset
from trntest.config import TrntestConfig


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


def test_evaluate_illuminated_candidates_lets_fetch_error_abort_the_whole_sweep(monkeypatch):
    """A systemic fetch failure (rate-limited/server down) on one candidate must abort the whole
    sweep, not just skip that candidate and keep firing requests at the rest -- see
    docs/history.md's Phase 36 follow-up for why (this exact catch-and-continue is what turned one
    real rate-limit response into ~1350 more of them in one incident)."""
    edr_candidates = pd.DataFrame([{"product_id": "a"}, {"product_id": "b"}])

    def fake_evaluate(edr_row, config, min_sun_elevation_deg):
        if edr_row["product_id"] == "b":
            raise cache.FetchError("simulated rate limit")
        return {
            "start_frame": 1.0,
            "center_frame_index": 1.0,
            "n_frames_for_square_crop": 1,
            "sun_elevation_deg": 20.0,
        }

    monkeypatch.setattr(dataset, "evaluate_candidate_image", fake_evaluate)

    with pytest.raises(cache.FetchError):
        dataset._evaluate_illuminated_candidates(edr_candidates, config=None, min_sun_elevation_deg=10.0)


def test_evaluate_illuminated_candidates_skips_only_the_two_anticipated_exception_types(monkeypatch):
    """Only `AssertionError` (e.g. `camera.boresight_ground_point_km`'s "does not intersect the
    Moon" check) and `spiceypy.utils.exceptions.SpiceyError` (a kernel-coverage gap) are anticipated
    per-candidate geometry/coverage edge cases and get skipped -- not any exception whatsoever."""
    edr_candidates = pd.DataFrame([{"product_id": "a"}, {"product_id": "b"}, {"product_id": "c"}])

    def fake_evaluate(edr_row, config, min_sun_elevation_deg):
        if edr_row["product_id"] == "a":
            raise AssertionError("camera boresight does not intersect the Moon")
        if edr_row["product_id"] == "b":
            raise spice.utils.exceptions.SpiceyError("simulated kernel coverage gap")
        return {
            "start_frame": 1.0,
            "center_frame_index": 1.0,
            "n_frames_for_square_crop": 1,
            "sun_elevation_deg": 20.0,
        }

    monkeypatch.setattr(dataset, "evaluate_candidate_image", fake_evaluate)

    result = dataset._evaluate_illuminated_candidates(edr_candidates, config=None, min_sun_elevation_deg=10.0)

    assert list(result["product_id"]) == ["c"]


def test_evaluate_illuminated_candidates_lets_other_errors_abort_the_sweep(monkeypatch):
    """A real bug (e.g. a typo producing a `KeyError`) must NOT be silently skipped alongside the
    two anticipated geometry/coverage exception types -- it should abort the sweep, same as
    `cache.FetchError`."""
    edr_candidates = pd.DataFrame([{"product_id": "a"}, {"product_id": "b"}])

    def fake_evaluate(edr_row, config, min_sun_elevation_deg):
        raise KeyError("simulated real bug, e.g. a typo'd dict key")

    monkeypatch.setattr(dataset, "evaluate_candidate_image", fake_evaluate)

    with pytest.raises(KeyError):
        dataset._evaluate_illuminated_candidates(edr_candidates, config=None, min_sun_elevation_deg=10.0)


def test_generate_dataset_lets_fetch_error_abort_the_whole_batch(monkeypatch):
    """Same principle as `_evaluate_illuminated_candidates` above, for the image-generation batch."""
    images = pd.DataFrame(
        [
            {
                "product_id": "M1TEST",
                "edr_volume": "LROLRC_0041C",
                "edr_subdir": "ESM4",
                "edr_doy": "2019333",
                "edr_product": "M1TEST",
                "cdr_volume": "LROLRC_1041C",
                "cdr_product": "M1TESTC",
                "start_frame": 93.5,
            }
        ]
    )
    monkeypatch.setattr(dataset.camera, "build_camera", mock.Mock(side_effect=cache.FetchError("simulated rate limit")))

    with pytest.raises(cache.FetchError):
        dataset.generate_dataset(images, config=TrntestConfig())


def test_generate_dataset_still_records_the_anticipated_dem_coverage_value_error(monkeypatch):
    """`ValueError` -- e.g. `lunaserv.astropedia_coverage_bbox_deg`'s real DEM-latitude-coverage
    check -- is the one anticipated per-image failure this catches and skips."""
    images = pd.DataFrame(
        [
            {
                "product_id": "M1TEST",
                "edr_volume": "LROLRC_0041C",
                "edr_subdir": "ESM4",
                "edr_doy": "2019333",
                "edr_product": "M1TEST",
                "cdr_volume": "LROLRC_1041C",
                "cdr_product": "M1TESTC",
                "start_frame": 93.5,
            }
        ]
    )
    monkeypatch.setattr(
        dataset.camera, "build_camera", mock.Mock(side_effect=ValueError("simulated DEM coverage problem"))
    )

    results = dataset.generate_dataset(images, config=TrntestConfig())

    assert results == []


def test_generate_dataset_lets_other_errors_abort_the_batch(monkeypatch):
    """A real bug must NOT be silently skipped alongside the one anticipated `ValueError` case --
    it should abort the batch, same as `cache.FetchError`."""
    images = pd.DataFrame(
        [
            {
                "product_id": "M1TEST",
                "edr_volume": "LROLRC_0041C",
                "edr_subdir": "ESM4",
                "edr_doy": "2019333",
                "edr_product": "M1TEST",
                "cdr_volume": "LROLRC_1041C",
                "cdr_product": "M1TESTC",
                "start_frame": 93.5,
            }
        ]
    )
    monkeypatch.setattr(
        dataset.camera, "build_camera", mock.Mock(side_effect=KeyError("simulated real bug, e.g. a typo'd dict key"))
    )

    with pytest.raises(KeyError):
        dataset.generate_dataset(images, config=TrntestConfig())
