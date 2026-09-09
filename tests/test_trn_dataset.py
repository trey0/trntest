import dataclasses
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from _fake_worker_task import FailingWorkerEntry, FakeWorkerEntry
from huey.exceptions import ResultTimeout, TaskException

from trntest import isis_wac, overview_map, report, tasks, trn_dataset, trn_products
from trntest.config import TrntestConfig


@pytest.fixture(autouse=True)
def _no_real_overview_map(monkeypatch):
    """`write_index()` (called by `populate()`/`populate_via_workers()` by default) now also calls
    `overview_map.write_overview_map`, which builds a real `Camera` (SPICE) per entry and needs
    manifest columns (`start_time`/`stop_time`/`center_lat_deg`/`center_lon_deg`) this module's own
    `_minimal_manifest`/hand-built test manifests don't have. Autouse, not a per-test
    `monkeypatch.setattr` call, since nearly every test in this file populates/writes an index one
    way or another; still exercises that `write_index()` calls it, just not the real rendering."""
    monkeypatch.setattr(overview_map, "write_overview_map", lambda dataset, config=None: None)


def _minimal_manifest(product_ids: list[str]) -> pd.DataFrame:
    """A manifest DataFrame with just enough columns for the task-queue tests below -- none of which
    touch `TrnTestEntry.per_image_config`/`camera`/etc. (no real SPICE/ASP/ISIS), so a full
    `candidate_window.DATASET_COLUMNS` row isn't needed. `edr_product == product_id`, matching how
    today's real manifest always has them equal (see docs/data-sources.md's "on-disk layout" section)."""
    return pd.DataFrame({"product_id": product_ids, "edr_product": product_ids})


def _minimal_spice_manifest(product_ids: list[str]) -> pd.DataFrame:
    """The `entry_kind="spice"` equivalent of `_minimal_manifest` -- `trn_dataset.
    SPICE_DATASET_COLUMNS`' own minimal shape (`product_id`/`utc_time`), plain `datetime` objects
    (not `pd.Timestamp`) since `TrnTestEntrySpice.identifier` only needs `.strftime`, which both
    support -- no real SPICE/ISIS touched by any test using this, same as `_minimal_manifest`.
    Each row a distinct second so `identifier` (`"%Y%m%dT%H%M%S"`, no finer resolution) stays unique
    per entry."""
    utc_times = [datetime(2020, 1, 1, 0, 0, i, tzinfo=UTC) for i in range(len(product_ids))]
    return pd.DataFrame({"product_id": product_ids, "utc_time": utc_times})


def _fake_generate_impl(image) -> None:
    """Monkeypatch target for `TrnTestCropImage`/`TrnTestHillshadeImage._generate_impl` -- just
    touches the real (SPICE/ISIS-free) `raster_path`/`sidecar_json_path` those classes already
    compute from `entry.dataset_folder`/`edr_product` alone."""
    image.raster_path.parent.mkdir(parents=True, exist_ok=True)
    image.raster_path.write_text("raster")
    image.sidecar_json_path.write_text("{}")


def _fake_generate_impl_failing_crop_for(edr_product: str):
    def impl(image):
        if edr_product == image.entry.edr_product and isinstance(image, trn_products.TrnTestCropImage):
            raise RuntimeError(f"boom for {edr_product}")
        _fake_generate_impl(image)

    return impl


@pytest.fixture(autouse=True)
def _flush_huey_before_test():
    """Every test below shares `tasks.huey`/`tasks.huey_parallel` -- module-level singletons backed
    by sqlite files under `output_dir` (see `trntest.tasks`'s own docstring), which in this project's
    Docker Compose setup is bind-mounted to a *host-persistent* directory that outlives any one
    `docker compose run`. A test's own `tmp_path` is not similarly isolated across separate runs:
    pytest numbers it deterministically per test function (`.../pytest-0/test_foo0`, restarting from
    0 in every fresh container), so `tasks.task_id()` (keyed on `str(tmp_path)`, or on `tmp_path.name`
    for the real-subprocess tests below) can collide with a stale stored result left behind by an
    earlier, separate invocation of this exact same test -- confirmed live: re-running
    `test_populate_marks_failed_and_continues` standalone a few times in a row started failing on a
    fresh `populate()` call until `.huey/` was cleared by hand. Flushing (clears queue/schedule/
    results/counters, cheap even on an empty db) before every test closes that gap without needing to
    rebind the `@huey.task()`-decorated functions to a fresh instance per test."""
    tasks.huey.flush()
    tasks.huey_parallel.flush()


# -- TrnTestDataSet.create()/open() --------------------------------------------------------------


def test_create_writes_manifest_and_subfolders(tmp_path):
    folder = tmp_path / "ds"
    images = _minimal_manifest(["P1", "P2"])
    ds = trn_dataset.TrnTestDataSet.create(folder, images, TrntestConfig())

    for sub in ("crop", "hillshade", "reproject", "reports", "logs", "_work"):
        assert (folder / sub).is_dir()
    assert (folder / "manifest.csv").is_file()
    assert len(ds) == 2


def test_create_twice_preserves_product_files_and_overwrites_manifest(tmp_path):
    folder = tmp_path / "ds"
    trn_dataset.TrnTestDataSet.create(folder, _minimal_manifest(["P1"]), TrntestConfig())
    marker = folder / "crop" / "already_generated.cub"
    marker.write_text("keep me")

    trn_dataset.TrnTestDataSet.create(folder, _minimal_manifest(["P1", "P2"]), TrntestConfig())

    assert marker.read_text() == "keep me"
    assert "P2" in (folder / "manifest.csv").read_text()


def test_open_needs_only_a_manifest(tmp_path):
    folder = tmp_path / "ds"
    folder.mkdir()
    base = datetime(2020, 1, 1, tzinfo=UTC)
    images = pd.DataFrame({"product_id": ["P1"], "edr_product": ["P1"], "start_time": [base], "stop_time": [base]})
    images.to_csv(folder / "manifest.csv", index=False)

    ds = trn_dataset.TrnTestDataSet.open(folder, TrntestConfig())

    assert len(ds) == 1
    assert ds[0].product_id == "P1"


# -- TrnTestDataSet indexing/iteration ------------------------------------------------------------


def test_len_iter_getitem_by_index_and_product_id(tmp_path):
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())

    assert len(ds) == 2
    assert [e.product_id for e in ds] == ["P1", "P2"]
    assert ds[0].product_id == "P1"
    assert ds[1].product_id == "P2"
    assert ds["P2"].product_id == "P2"


def test_getitem_by_missing_product_id_raises_key_error(tmp_path):
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    with pytest.raises(KeyError):
        ds["does-not-exist"]


# -- Task queue (trntest.tasks-backed) -----------------------------------------------------------


def test_task_state_pending_failed_done(tmp_path, monkeypatch):
    """`pending` before anything runs; `failed` after a failing `populate()`; `done` once the real
    product file exists (via a retried, now-succeeding `populate()`) -- exercised through the real
    `populate()`/`task_state()` path rather than poking `tasks.huey` directly, since there's no
    filesystem lock/error bookkeeping left to poke."""
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    entry = ds[0]

    assert trn_dataset.task_state(entry, "crop") == "pending"

    ds.populate(product_types=("crop",))
    assert trn_dataset.task_state(entry, "crop") == "failed"

    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    ds.populate(product_types=("crop",), retry_failed=True)
    assert trn_dataset.task_state(entry, "crop") == "done"


def test_await_result_swallows_timeout_without_hanging(capsys):
    """A `ResultTimeout` -- `populate_via_workers()`'s safety net for a task whose stored result
    never shows up (found live in a real 100-entry, 8-worker run against `trntest1`: the underlying
    work genuinely finished but the result was never stored, so an unbounded `.get()` hung forever
    -- see `docs/proposed-tasks/open-items.md`'s `populate_via_workers` hang item) -- must not
    propagate and abort the batch, the same way a `TaskException` already doesn't."""

    class _FakeResult:
        id = "fake-task-id"

        def get(self, blocking=True, timeout=None, preserve=True):
            raise ResultTimeout("timed out waiting for result")

    trn_dataset._await_result(_FakeResult(), timeout=5.0)  # must not raise/hang

    assert "fake-task-id" in capsys.readouterr().out


def test_task_state_done_wins_over_leftover_failed_result(tmp_path, monkeypatch):
    """`done` (a real generated file) takes priority even if a stale failed huey result is also
    present -- see `task_state`'s own docstring for why."""
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    entry = ds[0]
    ds.populate(product_types=("crop",))
    assert trn_dataset.task_state(entry, "crop") == "failed"

    entry.crop.raster_path.parent.mkdir(parents=True, exist_ok=True)
    entry.crop.raster_path.write_text("x")
    entry.crop.sidecar_json_path.write_text("{}")

    assert trn_dataset.task_state(entry, "crop") == "done"


def test_failed_task_state_survives_a_fresh_process(tmp_path, monkeypatch):
    """Regression check for `trntest.tasks`'s `immediate_use_memory=False`: a stored failure must be
    visible to a genuinely different process reading the same huey sqlite file, not just the process
    that produced it -- otherwise `status()` in a fresh `docker compose run` couldn't see a prior
    run's failure, the same property the old `.error` files had. See `trntest.tasks`'s docstring."""
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    ds.populate(product_types=("crop",))
    assert trn_dataset.task_state(ds[0], "crop") == "failed"

    tid = tasks.task_id(str(ds.folder), "P1")
    probe = (
        "from trntest import tasks\n"
        "from huey.exceptions import TaskException\n"
        "try:\n"
        f"    tasks.huey.result({tid!r}, preserve=True)\n"
        "except TaskException:\n"
        "    print('FAILED-AS-EXPECTED')\n"
        "else:\n"
        "    print('NOT-FOUND-OR-NOT-FAILED')\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert "FAILED-AS-EXPECTED" in result.stdout


# -- populate() ---------------------------------------------------------------------------------


def test_populate_drives_every_task_to_done(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())

    # product_types scoped to crop/hillshade -- "report"/"gallery" (PRODUCT_TYPES' other two default
    # members) aren't faked here and would otherwise attempt a real jupytext/papermill/nbconvert
    # pipeline / a real reproject render.
    ds.populate(product_types=("crop", "hillshade"))

    status = ds.status()
    assert (status[["crop", "hillshade"]] == "done").all(axis=None)


def test_populate_marks_failed_and_continues(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())

    ds.populate(product_types=("crop", "hillshade"))  # see test_populate_drives_every_task_to_done

    status = ds.status().set_index("product_id")
    assert status.loc["P1", "crop"] == "failed"
    assert status.loc["P1", "hillshade"] == "done"
    assert status.loc["P2", "crop"] == "done"
    assert status.loc["P2", "hillshade"] == "done"


def test_populate_retry_failed_clears_errors_and_reruns(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    ds.populate(product_types=("crop", "hillshade"))  # see test_populate_drives_every_task_to_done
    assert ds.status().set_index("product_id").loc["P1", "crop"] == "failed"

    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    ds.populate(product_types=("crop", "hillshade"), retry_failed=True)

    assert ds.status().set_index("product_id").loc["P1", "crop"] == "done"


def test_populate_limit_stops_after_n_entries_and_is_resumable(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2", "P3"]), TrntestConfig())
    product_types = ("crop", "hillshade")  # see test_populate_drives_every_task_to_done

    ds.populate(product_types=product_types, limit=2)

    status = ds.status(product_types=product_types).set_index("product_id")
    assert (status.loc["P1"] == "done").all()
    assert (status.loc["P2"] == "done").all()
    assert (status.loc["P3"] == "pending").all()

    ds.populate(product_types=product_types, limit=2)  # a later worker resumes against the same folder

    status = ds.status(product_types=product_types).set_index("product_id")
    assert (status.loc["P3"] == "done").all()


def test_populate_limit_zero_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())

    ds.populate(product_types=("crop", "hillshade"), limit=0)

    assert (ds.status()[["crop", "hillshade"]] == "pending").all(axis=None)


def test_populate_limit_does_not_count_already_done_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())
    product_types = ("crop", "hillshade")  # see test_populate_drives_every_task_to_done
    ds.populate(product_types=product_types, limit=1)
    assert (ds.status(product_types=product_types).set_index("product_id").loc["P1"] == "done").all()

    # P1 is already fully done -- a fresh call with limit=1 should skip straight past it (free) and
    # do new work on P2, not stop having "used up" its budget on P1 again.
    ds.populate(product_types=product_types, limit=1)

    assert (ds.status(product_types=product_types).set_index("product_id").loc["P2"] == "done").all()


# -- Generator logging (trntest.tasks._generate_entry / _capture_generator_log) ------------------


def test_populate_captures_generator_output_on_success(tmp_path, monkeypatch):
    def loud_generate_impl(image):
        print("hello from crop generation")
        _fake_generate_impl(image)

    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", loud_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    entry = ds[0]

    ds.populate(product_types=("crop",))

    log_text = entry.log_path("crop").read_text()
    assert "hello from crop generation" in log_text
    assert "crop generation started" in log_text
    assert "crop generation ended" in log_text


def test_populate_captures_traceback_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    entry = ds[0]

    ds.populate(product_types=("crop",))

    log_text = entry.log_path("crop").read_text()
    assert "Traceback (most recent call last)" in log_text
    assert "boom for P1" in log_text


def test_generate_entry_skips_log_capture_for_an_already_done_type(tmp_path, monkeypatch):
    """`_generate_entry`'s own `image.exists()` guard, not just `_enqueue_pending`'s pending-only
    filter (which would never even call `_generate_entry` for an already-done type in practice) --
    exercised directly so a stale log from a genuinely new attempt is never silently overwritten by
    a same-entry, same-process no-op."""
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    entry = ds[0]
    ds.populate(product_types=("crop",))
    log_path = entry.log_path("crop")
    original_log = log_path.read_text()

    tasks._generate_entry(entry, ("crop",))

    assert log_path.read_text() == original_log


# -- truncate() -----------------------------------------------------------------------------------


def test_truncate_single_entry_reverts_to_pending_and_leaves_others_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())
    product_types = ("crop", "hillshade")  # see test_populate_drives_every_task_to_done
    ds.populate(product_types=product_types)
    assert ds[0].crop.exists() and ds[0].hillshade.exists()

    ds.truncate(ds[0], product_types=product_types)

    status = ds.status(product_types=product_types).set_index("product_id")
    assert (status.loc["P1"] == "pending").all()
    assert (status.loc["P2"] == "done").all()
    assert not ds[0].crop.raster_path.exists()
    assert not ds[0].crop.sidecar_json_path.exists()
    assert not ds[0].hillshade.raster_path.exists()


def test_truncate_deletes_the_log_file(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    entry = ds[0]
    ds.populate(product_types=("crop",))
    assert entry.log_path("crop").exists()

    ds.truncate(entry, product_types=("crop",))

    assert not entry.log_path("crop").exists()


def test_truncate_none_reverts_every_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())
    ds.populate(product_types=("crop", "hillshade"))  # see test_populate_drives_every_task_to_done

    ds.truncate()

    assert (ds.status()[["crop", "hillshade"]] == "pending").all(axis=None)


def test_truncate_then_populate_actually_regenerates(tmp_path, monkeypatch):
    call_count = {"n": 0}

    def counting_generate_impl(image):
        call_count["n"] += 1
        _fake_generate_impl(image)

    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", counting_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", counting_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    product_types = ("crop", "hillshade")  # see test_populate_drives_every_task_to_done
    ds.populate(product_types=product_types)
    assert call_count["n"] == 2  # crop + hillshade

    ds.populate(product_types=product_types)  # already done -- no new calls
    assert call_count["n"] == 2

    ds.truncate(ds[0], product_types=product_types)
    ds.populate(product_types=product_types, limit=1)

    assert call_count["n"] == 4  # crop + hillshade regenerated
    assert (ds.status(product_types=product_types).set_index("product_id").loc["P1"] == "done").all()


def test_truncate_clears_stored_results_from_both_queues(tmp_path, monkeypatch):
    """`truncate()` must clear a stored failure from `tasks.huey_parallel` too, not just
    `tasks.huey` -- a task's most recent attempt could have gone through `populate_via_workers()`,
    and a stale failure there would otherwise still show up via
    `status(huey_instance=tasks.huey_parallel)` after truncate() claims to have reset everything."""
    _use_immediate_parallel_queue(monkeypatch)
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    entry = ds[0]
    ds.populate_via_workers(product_types=("crop",))
    assert trn_dataset.task_state(entry, "crop", huey_instance=tasks.huey_parallel) == "failed"

    ds.truncate(entry, product_types=("crop",))

    assert trn_dataset.task_state(entry, "crop", huey_instance=tasks.huey_parallel) == "pending"


def _use_fake_per_image_config(monkeypatch, cache_root: Path) -> None:
    """Stands in for the real `_per_image_config` (needs `edr_volume`/`edr_subdir`/`edr_doy`/
    `start_frame` manifest columns `_minimal_manifest` deliberately omits) with a fixed
    `TrntestConfig` per entry, keyed by `edr_product` -- just enough for
    `isis_wac.cached_crop_path(entry.per_image_config)` to resolve to a real, entry-specific path."""
    monkeypatch.setattr(
        trn_dataset.TrnTestEntryEdr,
        "per_image_config",
        property(
            lambda self: dataclasses.replace(TrntestConfig(), cache_root=cache_root, edr_product=self.edr_product)
        ),
    )


def test_truncate_invalidate_crop_cache_deletes_cached_crop(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    _use_fake_per_image_config(monkeypatch, tmp_path / "cache")
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    entry = ds[0]
    ds.populate(product_types=("crop",))
    cached_crop = isis_wac.cached_crop_path(entry.per_image_config)
    cached_crop.parent.mkdir(parents=True, exist_ok=True)
    cached_crop.write_text("cached crop bytes")

    ds.truncate(entry, product_types=("crop",), invalidate_crop_cache=True)

    assert not cached_crop.exists()


def test_truncate_leaves_crop_cache_alone_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    _use_fake_per_image_config(monkeypatch, tmp_path / "cache")
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    entry = ds[0]
    ds.populate(product_types=("crop",))
    cached_crop = isis_wac.cached_crop_path(entry.per_image_config)
    cached_crop.parent.mkdir(parents=True, exist_ok=True)
    cached_crop.write_text("cached crop bytes")

    ds.truncate(entry, product_types=("crop",))  # invalidate_crop_cache defaults to False

    assert cached_crop.exists()


def test_truncate_invalidate_crop_cache_is_noop_outside_crop_product_type(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    _use_fake_per_image_config(monkeypatch, tmp_path / "cache")
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    entry = ds[0]
    ds.populate(product_types=("hillshade",))
    cached_crop = isis_wac.cached_crop_path(entry.per_image_config)
    cached_crop.parent.mkdir(parents=True, exist_ok=True)
    cached_crop.write_text("cached crop bytes")

    ds.truncate(entry, product_types=("hillshade",), invalidate_crop_cache=True)

    assert cached_crop.exists()


# -- populate_via_workers() (huey_parallel-backed) -----------------------------------------------


def _use_immediate_parallel_queue(monkeypatch) -> None:
    """`populate_via_workers()`'s tests below exercise its real control flow (which tasks get
    enqueued, `limit`/`retry_failed` semantics, which queue `status()` needs to check) without a
    real `huey_consumer` subprocess: flips `tasks.huey_parallel.immediate` to `True` (huey's own
    documented pattern for testing without a consumer -- see `trntest.tasks`'s docstring) so
    `huey_parallel.enqueue()` executes synchronously in this process, then no-ops
    `tasks.start_consumer`/`stop_consumer` so `populate_via_workers()` doesn't try to spawn a real
    (now unnecessary) subprocess. The real subprocess machinery itself is covered separately, below,
    by the `-k process` consumer tests using `_fake_worker_task.py`'s picklable, SPICE-free tasks."""
    monkeypatch.setattr(tasks.huey_parallel, "immediate", True)
    monkeypatch.setattr(tasks, "start_consumer", lambda workers: None)
    monkeypatch.setattr(tasks, "stop_consumer", lambda proc: None)


def test_populate_via_workers_drives_every_task_to_done(tmp_path, monkeypatch):
    _use_immediate_parallel_queue(monkeypatch)
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())

    # product_types scoped to crop/hillshade -- see test_populate_drives_every_task_to_done
    ds.populate_via_workers(product_types=("crop", "hillshade"))

    status = ds.status(huey_instance=tasks.huey_parallel)
    assert (status[["crop", "hillshade"]] == "done").all(axis=None)


def test_populate_via_workers_marks_failed_and_continues(tmp_path, monkeypatch):
    _use_immediate_parallel_queue(monkeypatch)
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())

    ds.populate_via_workers(product_types=("crop", "hillshade"))  # see test_populate_drives_every_task_to_done

    status = ds.status(huey_instance=tasks.huey_parallel).set_index("product_id")
    assert status.loc["P1", "crop"] == "failed"
    assert status.loc["P1", "hillshade"] == "done"
    assert status.loc["P2", "crop"] == "done"


def test_populate_via_workers_retry_failed_clears_and_reruns(tmp_path, monkeypatch):
    _use_immediate_parallel_queue(monkeypatch)
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    ds.populate_via_workers(product_types=("crop",))
    assert trn_dataset.task_state(ds[0], "crop", huey_instance=tasks.huey_parallel) == "failed"

    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    ds.populate_via_workers(product_types=("crop",), retry_failed=True)

    assert trn_dataset.task_state(ds[0], "crop", huey_instance=tasks.huey_parallel) == "done"


def test_populate_via_workers_limit_stops_after_n_entries_and_is_resumable(tmp_path, monkeypatch):
    _use_immediate_parallel_queue(monkeypatch)
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2", "P3"]), TrntestConfig())
    product_types = ("crop", "hillshade")  # see test_populate_drives_every_task_to_done

    ds.populate_via_workers(product_types=product_types, limit=2)

    status = ds.status(product_types=product_types, huey_instance=tasks.huey_parallel).set_index("product_id")
    assert (status.loc["P1"] == "done").all()
    assert (status.loc["P2"] == "done").all()
    assert (status.loc["P3"] == "pending").all()

    ds.populate_via_workers(product_types=product_types, limit=2)

    status = ds.status(product_types=product_types, huey_instance=tasks.huey_parallel).set_index("product_id")
    assert (status.loc["P3"] == "done").all()


def test_populate_via_workers_uses_a_queue_separate_from_populate(tmp_path, monkeypatch):
    """A failure recorded via `populate_via_workers()` is invisible to a plain `status()` call
    (`tasks.huey`'s own queue) -- the two are independent, by design (see `trntest.tasks`'s
    docstring)."""
    _use_immediate_parallel_queue(monkeypatch)
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())

    ds.populate_via_workers(product_types=("crop",))

    assert trn_dataset.task_state(ds[0], "crop", huey_instance=tasks.huey_parallel) == "failed"
    assert trn_dataset.task_state(ds[0], "crop", huey_instance=tasks.huey) == "pending"


def test_populate_via_workers_does_not_start_a_consumer_when_nothing_pending(tmp_path, monkeypatch):
    """No pending work -> no subprocess spawned at all, not even a short-lived one -- confirmed by
    monkeypatching `start_consumer` to fail loudly if called, rather than a silent no-op like the
    other tests here use."""
    monkeypatch.setattr(tasks, "start_consumer", lambda workers: pytest.fail("start_consumer should not be called"))
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest([]), TrntestConfig())

    ds.populate_via_workers()  # no entries at all -- nothing to enqueue


# -- TrnTestReport / write_index() ---------------------------------------------------------------


def _fake_report_generate_impl(image) -> None:
    """Monkeypatch target for `TrnTestReport._generate_impl` -- skips the real hillshade dependency
    check and jupytext/papermill/nbconvert pipeline, just touches raster_path/sidecar_json_path
    like `_fake_generate_impl` above does for crop/hillshade."""
    image.raster_path.parent.mkdir(parents=True, exist_ok=True)
    image.raster_path.write_text("<html>fake report</html>")
    image.sidecar_json_path.write_text("{}")


def test_report_plugs_into_task_queue_generically(tmp_path, monkeypatch):
    """`report` isn't special-cased anywhere in the task queue -- `task_state`/`truncate`/
    `populate` all already treat it like any other product type once `TrnTestReport` is registered
    on `images_by_type`."""
    monkeypatch.setattr(trn_products.TrnTestReport, "_generate_impl", _fake_report_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    entry = ds[0]

    assert trn_dataset.task_state(entry, "report") == "pending"

    ds.populate(product_types=("report",))
    assert trn_dataset.task_state(entry, "report") == "done"
    assert entry.report.exists()

    ds.truncate(entry, product_types=("report",))
    assert trn_dataset.task_state(entry, "report") == "pending"
    assert not entry.report.exists()


def test_report_backfills_an_already_populated_entry(tmp_path, monkeypatch):
    """An entry whose crop/hillshade were already done before `report` existed as a product type
    gets its report generated on the next `populate()` call, without regenerating crop/hillshade --
    `_enqueue_pending` only enqueues an entry's still-pending product types, so this falls out of
    the existing task-queue logic for free."""
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestReport, "_generate_impl", _fake_report_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestGalleryThumb, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    ds.populate(product_types=("crop", "hillshade"))
    assert trn_dataset.task_state(ds[0], "report") == "pending"

    ds.populate()  # default PRODUCT_TYPES now includes "report"/"gallery"

    status = ds.status().set_index("product_id")
    assert (status.loc["P1"] == "done").all()


def test_gallery_plugs_into_task_queue_generically(tmp_path, monkeypatch):
    """`gallery` isn't special-cased anywhere in the task queue -- same generic
    `task_state`/`truncate`/`populate` treatment `report` gets, see
    `test_report_plugs_into_task_queue_generically`."""
    monkeypatch.setattr(trn_products.TrnTestGalleryThumb, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    entry = ds[0]

    assert trn_dataset.task_state(entry, "gallery") == "pending"

    ds.populate(product_types=("gallery",))
    assert trn_dataset.task_state(entry, "gallery") == "done"
    assert entry.gallery_thumb.exists()

    ds.truncate(entry, product_types=("gallery",))
    assert trn_dataset.task_state(entry, "gallery") == "pending"
    assert not entry.gallery_thumb.exists()


def test_gallery_html_shows_placeholder_for_missing_thumbnails(tmp_path):
    """An entry whose gallery thumbnail hasn't been generated yet shows a plain "(no data yet)"
    placeholder instead of a broken image -- `write_gallery_html` (via `write_index()`) can be run at
    any time regardless of how much of the dataset has actually been populated, like
    `write_overview_map`."""
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())

    ds.write_index()

    gallery_html = (ds.folder / "reports" / "gallery.html").read_text()
    assert "(no data yet)" in gallery_html
    assert "gallery/0_base.jpg" not in gallery_html


def test_write_index_writes_status_csv_and_index_html(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestReport, "_generate_impl", _fake_report_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestGalleryThumb, "_generate_impl", _fake_generate_impl)
    images = pd.DataFrame(
        {
            "product_id": ["P1", "P2"],
            "edr_product": ["P1", "P2"],
            "sun_elevation_deg": [3.0, 45.0],  # P1 low enough to trip the heuristic flag
        }
    )
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", images, TrntestConfig())

    ds.populate()

    status_csv = (ds.folder / "status.csv").read_text()
    assert "P1" in status_csv
    assert "P2" in status_csv
    assert "low sun elevation" in status_csv

    overview_table_html = (ds.folder / "reports" / "overview_table.html").read_text()
    assert "P1/report.html" in overview_table_html
    assert "P2/report.html" in overview_table_html

    gallery_html = (ds.folder / "reports" / "gallery.html").read_text()
    assert "gallery/0_base.jpg" in gallery_html and "gallery/0_overlay.jpg" in gallery_html
    assert "gallery/1_base.jpg" in gallery_html and "gallery/1_overlay.jpg" in gallery_html
    assert 'href="P1/report.html"' in gallery_html and 'href="P2/report.html"' in gallery_html
    assert "(no data yet)" not in gallery_html

    index_html = (ds.folder / "reports" / "index.html").read_text()
    assert "overview_table.html" in index_html  # the nav bar's content iframe default
    assert "gallery.html" in index_html  # the nav bar's own Gallery link
    assert '"P1"' in index_html and '"P2"' in index_html  # the jump-to-entry productIds array


def test_spice_entry_images_by_type_includes_report_and_gallery(tmp_path):
    """`TrnTestEntrySpice` supports `report`/`gallery` like any other entry kind -- only `crop`/
    `reproject` (which need a real EDR's own pixel data) are unavailable for this kind."""
    ds = trn_dataset.TrnTestDataSet(
        tmp_path / "ds", _minimal_spice_manifest(["P1"]), TrntestConfig(), entry_kind="spice"
    )
    entry = ds[0]

    assert isinstance(entry, trn_dataset.TrnTestEntrySpice)
    assert set(entry.images_by_type) == {"hillshade", "report", "gallery"}


def test_spice_entry_report_and_gallery_plug_into_task_queue_generically(tmp_path, monkeypatch):
    """Same generic task-queue treatment `test_report_plugs_into_task_queue_generically`/
    `test_gallery_plugs_into_task_queue_generically` confirm for `entry_kind="edr"` -- `TrnTestReport`/
    `TrnTestGalleryThumb` only ever touch `entry.primary_image`/`entry.index`, so nothing here needs
    to be different for a SPICE-only entry."""
    monkeypatch.setattr(trn_products.TrnTestReport, "_generate_impl", _fake_report_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestGalleryThumb, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(
        tmp_path / "ds", _minimal_spice_manifest(["P1"]), TrntestConfig(), entry_kind="spice"
    )
    entry = ds[0]

    assert trn_dataset.task_state(entry, "report") == "pending"
    assert trn_dataset.task_state(entry, "gallery") == "pending"

    ds.populate(product_types=("report", "gallery"))

    assert trn_dataset.task_state(entry, "report") == "done"
    assert trn_dataset.task_state(entry, "gallery") == "done"
    assert entry.report.exists()
    assert entry.gallery_thumb.exists()


def test_write_index_generates_full_html_for_spice_entry_kind(tmp_path, monkeypatch):
    """`write_index()` no longer stops at `status.csv` for `entry_kind="spice"` -- overview table/
    gallery/nav-bar HTML all generate the same as for `entry_kind="edr"` (see
    `test_write_index_writes_status_csv_and_index_html`), keyed by `entry.identifier` (a UTC
    timestamp string here, not an `edr_product`) rather than any EDR-only manifest column."""
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestReport, "_generate_impl", _fake_report_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestGalleryThumb, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(
        tmp_path / "ds", _minimal_spice_manifest(["P1", "P2"]), TrntestConfig(), entry_kind="spice"
    )
    identifiers = [ds[i].identifier for i in range(2)]

    ds.populate()  # default_product_types for "spice" now includes report/gallery

    status_csv = (ds.folder / "status.csv").read_text()
    assert "P1" in status_csv
    assert "P2" in status_csv

    overview_table_html = (ds.folder / "reports" / "overview_table.html").read_text()
    for identifier in identifiers:
        assert f"{identifier}/report.html" in overview_table_html

    gallery_html = (ds.folder / "reports" / "gallery.html").read_text()
    assert "gallery/0_base.jpg" in gallery_html and "gallery/0_overlay.jpg" in gallery_html
    assert "gallery/1_base.jpg" in gallery_html and "gallery/1_overlay.jpg" in gallery_html
    for identifier in identifiers:
        assert f'href="{identifier}/report.html"' in gallery_html
    assert "(no data yet)" not in gallery_html

    index_html = (ds.folder / "reports" / "index.html").read_text()
    assert "overview_table.html" in index_html
    assert "gallery.html" in index_html
    assert '"P1"' in index_html and '"P2"' in index_html


def test_time_span_columns_per_entry_kind(tmp_path):
    edr_ds = trn_dataset.TrnTestDataSet(tmp_path / "edr", _minimal_manifest([]), TrntestConfig())
    spice_ds = trn_dataset.TrnTestDataSet(
        tmp_path / "spice", _minimal_spice_manifest([]), TrntestConfig(), entry_kind="spice"
    )

    assert edr_ds.time_span_columns == ("start_time", "stop_time")
    assert spice_ds.time_span_columns == ("utc_time", "utc_time")


def test_print_viewing_url_prints_link_when_folder_is_under_output_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRNTEST_JUPYTER_PORT", "8891")
    config = dataclasses.replace(TrntestConfig(), output_dir=tmp_path)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "trn_dataset", _minimal_manifest([]), config)

    report.print_viewing_url(ds)

    assert "http://localhost:8891/output/trn_dataset/reports/index.html" in capsys.readouterr().out


def test_print_viewing_url_defaults_to_port_8888(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TRNTEST_JUPYTER_PORT", raising=False)
    config = dataclasses.replace(TrntestConfig(), output_dir=tmp_path)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "trn_dataset", _minimal_manifest([]), config)

    report.print_viewing_url(ds)

    assert "http://localhost:8888/output/trn_dataset/reports/index.html" in capsys.readouterr().out


def test_print_viewing_url_noop_when_folder_outside_output_dir(capsys):
    # TrntestConfig()'s default output_dir is /workspace/output -- this folder isn't under it.
    ds = trn_dataset.TrnTestDataSet(Path("/some/other/place"), _minimal_manifest([]), TrntestConfig())

    report.print_viewing_url(ds)

    assert capsys.readouterr().out == ""


def test_populate_write_index_false_skips_status_csv_and_index_html(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestReport, "_generate_impl", _fake_report_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestGalleryThumb, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())

    ds.populate(write_index=False)

    assert not (ds.folder / "status.csv").exists()


def test_logs_link_html_shows_placeholder_when_no_logs_exist(tmp_path):
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    assert report._logs_link_html(ds[0], "../logs") == "&mdash;"


def test_logs_link_html_links_to_the_whole_log_dir(tmp_path):
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    entry = ds[0]
    entry.log_path("crop").parent.mkdir(parents=True, exist_ok=True)
    entry.log_path("crop").write_text("...")

    html = report._logs_link_html(entry, "../logs")

    assert html == f'<a href="../logs/{entry.edr_product}/">logs/</a>'


def test_overview_table_links_to_a_failed_entrys_log_dir(tmp_path, monkeypatch):
    """The overview table's `logs` column is exactly where a `failed` status is most useful paired
    with a link -- confirms `write_overview_table_html` links to the entry's `log_dir` folder after
    a real (faked) generator failure, without needing the entry's own report to exist first."""
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())

    ds.populate(product_types=("crop",))

    overview_table_html = (ds.folder / "reports" / "overview_table.html").read_text()
    assert '<a href="../logs/P1/">logs/</a>' in overview_table_html


def test_problem_flags_low_sun_elevation(tmp_path):
    entry = trn_dataset.TrnTestEntryEdr(
        pd.Series({"product_id": "P1", "edr_product": "P1", "sun_elevation_deg": 2.0}), tmp_path, TrntestConfig()
    )
    assert any("low sun elevation" in flag for flag in report.problem_flags(entry))


def test_problem_flags_tolerates_a_missing_column(tmp_path):
    entry = trn_dataset.TrnTestEntryEdr(pd.Series({"product_id": "P1", "edr_product": "P1"}), tmp_path, TrntestConfig())
    assert report.problem_flags(entry) == []


# -- Real `huey_consumer -k process` subprocess (trntest.tasks.start_consumer/stop_consumer) ------


def _consumer_env(tmp_path: Path) -> dict[str, str]:
    """`tests/`, not the full `trntest` package, on `PYTHONPATH` -- lets a fresh worker subprocess
    unpickle `_fake_worker_task.FakeWorkerTask`/`FailingWorkerTask` without needing spiceypy/
    rasterio/torch/etc. installed or importable. `TRNTEST_OUTPUT_DIR` pointed at `tmp_path` so this
    test's `tasks.huey_parallel` (already imported, fixed sqlite path, unaffected by this env var)
    and the consumer subprocess's own fresh one still agree on the same queue -- unnecessary here
    since the test always uses `tasks.huey_parallel` directly rather than a fresh import, but kept
    for clarity that both processes must agree on it in general (see `trntest.tasks`'s docstring)."""
    return {**os.environ, "PYTHONPATH": str(Path(__file__).parent)}


def test_start_stop_consumer_lifecycle(tmp_path):
    """No task involved -- just confirms `start_consumer` really starts a live process and
    `stop_consumer` really stops it (SIGTERM, not left running)."""
    proc = tasks.start_consumer(workers=1, env=_consumer_env(tmp_path))
    try:
        assert proc.poll() is None  # still running
    finally:
        tasks.stop_consumer(proc)
    assert proc.poll() is not None  # exited


def test_generate_product_parallel_runs_in_a_real_worker_subprocess(tmp_path):
    marker_path = tmp_path / "marker.txt"
    task = tasks.generate_product_parallel.s(FakeWorkerEntry(str(marker_path)), ("fake",))
    task.id = f"test-real-consumer-success-{tmp_path.name}"
    result = tasks.huey_parallel.enqueue(task)

    consumer = tasks.start_consumer(workers=1, env=_consumer_env(tmp_path))
    try:
        value = result.get(blocking=True, timeout=30, preserve=True)
    finally:
        tasks.stop_consumer(consumer)

    assert marker_path.read_text() == "done"
    assert str(value["fake"]) == str(marker_path)


def test_generate_product_parallel_failure_visible_via_huey_parallel_result(tmp_path):
    task = tasks.generate_product_parallel.s(FailingWorkerEntry(str(tmp_path)), ("fake",))
    task.id = f"test-real-consumer-failure-{tmp_path.name}"
    result = tasks.huey_parallel.enqueue(task)

    consumer = tasks.start_consumer(workers=1, env=_consumer_env(tmp_path))
    try:
        with pytest.raises(TaskException, match="boom from worker subprocess"):
            result.get(blocking=True, timeout=30, preserve=True)
    finally:
        tasks.stop_consumer(consumer)
