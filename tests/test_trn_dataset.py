import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from _fake_worker_task import FailingWorkerEntry, FakeWorkerEntry
from huey.exceptions import TaskException

from trntest import report, tasks, trn_dataset, trn_products
from trntest.config import TrntestConfig


def _minimal_manifest(product_ids: list[str]) -> pd.DataFrame:
    """A manifest DataFrame with just enough columns for the task-queue tests below -- none of which
    touch `TrnTestEntry.per_image_config`/`camera`/etc. (no real SPICE/ASP/ISIS), so a full
    `candidate_window.DATASET_COLUMNS` row isn't needed. `edr_product == product_id`, matching how
    today's real manifest always has them equal (see docs/data-sources.md's "on-disk layout" section)."""
    return pd.DataFrame({"product_id": product_ids, "edr_product": product_ids})


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

    for sub in ("crop", "hillshade", "reproject", "reports", "_work"):
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

    # product_types scoped to crop/hillshade -- "report" (PRODUCT_TYPES' third default member) isn't
    # faked here and would otherwise attempt a real jupytext/papermill/nbconvert pipeline.
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
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    ds.populate(product_types=("crop", "hillshade"))
    assert trn_dataset.task_state(ds[0], "report") == "pending"

    ds.populate()  # default PRODUCT_TYPES now includes "report"

    status = ds.status().set_index("product_id")
    assert (status.loc["P1"] == "done").all()


def test_write_index_writes_status_csv_and_index_html(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestReport, "_generate_impl", _fake_report_generate_impl)
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

    index_html = (ds.folder / "reports" / "index.html").read_text()
    assert "P1/report.html" in index_html
    assert "P2/report.html" in index_html


def test_populate_write_index_false_skips_status_csv_and_index_html(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_products.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_products.TrnTestReport, "_generate_impl", _fake_report_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())

    ds.populate(write_index=False)

    assert not (ds.folder / "status.csv").exists()


def test_problem_flags_low_sun_elevation(tmp_path):
    entry = trn_dataset.TrnTestEntry(
        pd.Series({"product_id": "P1", "edr_product": "P1", "sun_elevation_deg": 2.0}), tmp_path, TrntestConfig()
    )
    assert any("low sun elevation" in flag for flag in report.problem_flags(entry))


def test_problem_flags_tolerates_a_missing_column(tmp_path):
    entry = trn_dataset.TrnTestEntry(pd.Series({"product_id": "P1", "edr_product": "P1"}), tmp_path, TrntestConfig())
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
    task = tasks.generate_product_parallel.s(FailingWorkerEntry(), ("fake",))
    task.id = f"test-real-consumer-failure-{tmp_path.name}"
    result = tasks.huey_parallel.enqueue(task)

    consumer = tasks.start_consumer(workers=1, env=_consumer_env(tmp_path))
    try:
        with pytest.raises(TaskException, match="boom from worker subprocess"):
            result.get(blocking=True, timeout=30, preserve=True)
    finally:
        tasks.stop_consumer(consumer)
