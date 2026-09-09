import re

import pytest
from test_trn_dataset import (
    _fake_generate_impl,
    _fake_generate_impl_failing_crop_for,
    _minimal_manifest,
    _use_immediate_parallel_queue,
)

from trntest import health_monitor, overview_map, tasks, trn_dataset, trn_products
from trntest.config import TrntestConfig


@pytest.fixture(autouse=True)
def _no_real_overview_map(monkeypatch):
    """See `test_trn_dataset._no_real_overview_map`'s own docstring -- needed here too since
    `populate_via_workers()`'s default `write_index=True` calls it, and this file's minimal
    manifests don't have the real columns it needs."""
    monkeypatch.setattr(overview_map, "write_overview_map", lambda dataset, config=None: None)


@pytest.fixture(autouse=True)
def _flush_huey_before_test():
    """See `test_trn_dataset._flush_huey_before_test`'s own docstring -- same rationale, needed
    here too since `test_health_monitor_consumer_pid_none_reports_mem_cpu_as_na` reads
    `tasks.huey_parallel.pending_count()` directly and would be thrown off by a stale queue."""
    tasks.huey.flush()
    tasks.huey_parallel.flush()


def _parse_logfmt_line(line: str) -> dict[str, str]:
    return dict(re.findall(r"(\w+)=(\S+)", line))


def _log_lines(ds: trn_dataset.TrnTestDataSet) -> list[str]:
    log_path = ds.folder / "logs" / "health_monitor_log.txt"
    return log_path.read_text().splitlines()


def test_populate_via_workers_writes_a_parseable_health_log(tmp_path, monkeypatch):
    _use_immediate_parallel_queue(monkeypatch)
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())

    ds.populate_via_workers(product_types=("crop", "hillshade"))

    lines = _log_lines(ds)
    assert lines  # at least the one final line HealthMonitor.stop() always writes
    fields = _parse_logfmt_line(lines[-1])
    # Every declared field is present, and every value is a single space-free token -- the whole
    # point of the logfmt format is that a line stays readable/parseable without a header.
    expected_keys = {
        "ts",
        "done",
        "failed",
        "pending",
        "pct_ok",
        "pct_done",
        "eta_min",
        "disk_free_gb",
        "disk_eta_gb",
        "mem_mb",
        "cpu_pct",
        "active",
    }
    assert set(fields) == expected_keys
    assert fields["done"] == "2"
    assert fields["failed"] == "0"
    assert fields["pending"] == "0"
    assert fields["pct_ok"] == "100.0"
    assert fields["pct_done"] == "100.0"
    assert fields["active"] == "0/4"


def test_populate_via_workers_health_log_counts_failures(tmp_path, monkeypatch):
    _use_immediate_parallel_queue(monkeypatch)
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())

    ds.populate_via_workers(product_types=("crop", "hillshade"))

    fields = _parse_logfmt_line(_log_lines(ds)[-1])
    assert fields["done"] == "1"
    assert fields["failed"] == "1"
    assert fields["pct_ok"] == "50.0"


def test_no_health_log_when_nothing_pending(tmp_path, monkeypatch):
    """No enqueued work -> populate_via_workers() never starts a monitor at all (mirrors
    test_populate_via_workers_does_not_start_a_consumer_when_nothing_pending)."""
    monkeypatch.setattr(tasks, "start_consumer", lambda workers: pytest.fail("start_consumer should not be called"))
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest([]), TrntestConfig())

    ds.populate_via_workers(product_types=("crop", "hillshade"))

    assert not (ds.folder / "logs" / "health_monitor_log.txt").exists()


def test_health_monitor_consumer_pid_none_reports_mem_cpu_as_na(tmp_path):
    """`consumer_pid=None` (a faked-out consumer in tests, or genuinely unavailable) must not crash
    the memory/CPU fields -- they degrade to `n/a` instead."""
    ds_folder = tmp_path / "ds"
    ds_folder.mkdir()
    monitor = health_monitor.HealthMonitor(
        dataset_folder=ds_folder,
        huey_instance=tasks.huey_parallel,
        results=[],
        workers=4,
        consumer_pid=None,
    )

    fields = _parse_logfmt_line(monitor._poll_line())

    assert fields["mem_mb"] == "n/a"
    assert fields["cpu_pct"] == "n/a"
    assert fields["active"] == "0/4"


def test_health_monitor_creates_dataset_folder_logs_dir(tmp_path):
    """Constructing a HealthMonitor against a dataset folder a bare TrnTestDataSet(...) constructor
    hasn't created yet (unlike TrnTestDataSet.create()) must not raise -- see this module's own
    comment on why the logs dir is made in __init__, not start()."""
    ds_folder = tmp_path / "not_yet_created"

    monitor = health_monitor.HealthMonitor(
        dataset_folder=ds_folder, huey_instance=tasks.huey_parallel, results=[], workers=1, consumer_pid=None
    )

    assert monitor.log_path.parent.is_dir()
