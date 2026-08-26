import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from _fake_worker_task import FailingWorkerEntry, FakeWorkerEntry
from huey.exceptions import TaskException

from trntest import tasks, trn_dataset
from trntest.config import TrntestConfig


def _minimal_manifest(product_ids: list[str]) -> pd.DataFrame:
    """A manifest DataFrame with just enough columns for the task-queue/class-hierarchy tests below
    -- none of which touch `TrnTestEntry.per_image_config`/`camera`/etc. (no real SPICE/ASP/ISIS), so
    a full `dataset.DATASET_COLUMNS` row isn't needed. `edr_product == product_id`, matching how
    today's real manifest always has them equal (see docs/data-sources.md's "on-disk layout" section)."""
    return pd.DataFrame({"product_id": product_ids, "edr_product": product_ids})


class _FakeImage(trn_dataset.TrnTestImage):
    """Minimal concrete `TrnTestImage` -- exercises the shared base-class logic (`exists`,
    `generate`'s idempotency, `_require_generated`) without any real generation/plotting."""

    def __init__(self, entry):
        super().__init__(entry)
        self.generate_impl_calls = 0

    @property
    def raster_path(self):
        return self.entry.dataset_folder / "fake.raster"

    @property
    def sidecar_json_path(self):
        return self.entry.dataset_folder / "fake.json"

    @property
    def rotation_k(self):
        return 0

    @property
    def width_km(self):
        return 1.0

    @property
    def height_km(self):
        return 1.0

    @property
    def footprint_lonlat_deg(self):
        return {}

    @property
    def render_label(self):
        return "Fake"

    @property
    def tie_point_px_key(self):
        return "synthetic_px"

    def _generate_impl(self):
        self.generate_impl_calls += 1
        self.raster_path.parent.mkdir(parents=True, exist_ok=True)
        self.raster_path.write_text("raster")
        self.sidecar_json_path.write_text("{}")

    def _mapprojected_path(self):
        return self.entry.dataset_folder / "fake-mapproj.tif"


def _fake_generate_impl(image) -> None:
    """Monkeypatch target for `TrnTestCropImage`/`TrnTestHillshadeImage._generate_impl` -- just
    touches the real (SPICE/ISIS-free) `raster_path`/`sidecar_json_path` those classes already
    compute from `entry.dataset_folder`/`edr_product` alone."""
    image.raster_path.parent.mkdir(parents=True, exist_ok=True)
    image.raster_path.write_text("raster")
    image.sidecar_json_path.write_text("{}")


def _fake_generate_impl_failing_crop_for(edr_product: str):
    def impl(image):
        if edr_product == image.entry.edr_product and isinstance(image, trn_dataset.TrnTestCropImage):
            raise RuntimeError(f"boom for {edr_product}")
        _fake_generate_impl(image)

    return impl


# -- TrnTestDataSet.create()/open() --------------------------------------------------------------


def test_create_writes_manifest_and_subfolders(tmp_path):
    folder = tmp_path / "ds"
    images = _minimal_manifest(["P1", "P2"])
    ds = trn_dataset.TrnTestDataSet.create(folder, images, TrntestConfig())

    for sub in ("crop", "hillshade", "reproject", "_work"):
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


# -- Exact path naming ------------------------------------------------------------------------


def test_crop_and_hillshade_path_naming(tmp_path):
    folder = tmp_path / "ds"
    entry = trn_dataset.TrnTestEntry(
        pd.Series({"product_id": "M1327210646CE", "edr_product": "M1327210646CE"}), folder, TrntestConfig()
    )

    assert entry.crop.raster_path == folder / "crop" / "M1327210646CE_crop.cub"
    assert entry.crop.sidecar_json_path == folder / "crop" / "M1327210646CE_crop.json"
    assert entry.hillshade.raster_path == folder / "hillshade" / "M1327210646CE_hillshade.tif"
    assert entry.hillshade.sidecar_json_path == folder / "hillshade" / "M1327210646CE_hillshade.json"


def test_hillshade_and_reproject_mapprojected_path_are_generator_scoped(tmp_path, monkeypatch):
    # docs/history.md's Phase 79 entry: _work/<entry>/<generator>/<label>, not a flat
    # _work/<entry>/<label> -- hillshade and reproject must land in their own separate subfolders
    # even though they share this same inherited _mapprojected_path implementation.
    folder = tmp_path / "ds"
    row = pd.Series(
        {
            "product_id": "M1327210646CE",
            "edr_product": "M1327210646CE",
            "edr_volume": "LROLRC_0041C",
            "edr_subdir": "ESM4",
            "edr_doy": "2019334",
            "cdr_volume": "LROLRC_1041C",
            "cdr_product": "M1327210646CC",
            "start_frame": 440,
        }
    )
    entry = trn_dataset.TrnTestEntry(row, folder, TrntestConfig())
    work_dir = folder / "_work" / "M1327210646CE"

    captured_out_paths = []

    def fake_run_mapproject_image(image_path, camera_path, output_path, dem_ortho_result, config):
        captured_out_paths.append(output_path)
        return output_path

    monkeypatch.setattr(trn_dataset.render, "run_mapproject_image", fake_run_mapproject_image)
    monkeypatch.setattr(trn_dataset.TrnTestEntry, "dem_ortho_result", None)

    entry.hillshade._mapprojected_path()
    entry.reproject._mapprojected_path()

    assert captured_out_paths[0] == work_dir / "hillshade" / "M1327210646CE_hillshade-mapproj.tif"
    assert captured_out_paths[1] == work_dir / "reproject" / "M1327210646CE_reproject-mapproj.tif"


# -- TrnTestImage shared base-class logic (via the fake subclass) -----------------------------


def test_image_exists_false_before_generate(tmp_path):
    entry = trn_dataset.TrnTestEntry(pd.Series({"product_id": "P1", "edr_product": "P1"}), tmp_path, TrntestConfig())
    image = _FakeImage(entry)
    assert not image.exists()


def test_image_require_generated_raises_before_generate(tmp_path):
    entry = trn_dataset.TrnTestEntry(pd.Series({"product_id": "P1", "edr_product": "P1"}), tmp_path, TrntestConfig())
    image = _FakeImage(entry)
    with pytest.raises(FileNotFoundError):
        image._require_generated()


def test_image_generate_is_idempotent(tmp_path):
    entry = trn_dataset.TrnTestEntry(pd.Series({"product_id": "P1", "edr_product": "P1"}), tmp_path, TrntestConfig())
    image = _FakeImage(entry)

    first = image.generate()
    second = image.generate()

    assert first == second == image.raster_path
    assert image.exists()
    assert image.generate_impl_calls == 1


# -- Task queue (trntest.tasks-backed) -----------------------------------------------------------


def test_task_state_pending_failed_done(tmp_path, monkeypatch):
    """`pending` before anything runs; `failed` after a failing `populate()`; `done` once the real
    product file exists (via a retried, now-succeeding `populate()`) -- exercised through the real
    `populate()`/`task_state()` path rather than poking `tasks.huey` directly, since there's no
    filesystem lock/error bookkeeping left to poke."""
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    entry = ds[0]

    assert trn_dataset.task_state(entry, "crop") == "pending"

    ds.populate(product_types=("crop",))
    assert trn_dataset.task_state(entry, "crop") == "failed"

    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    ds.populate(product_types=("crop",), retry_failed=True)
    assert trn_dataset.task_state(entry, "crop") == "done"


def test_task_state_done_wins_over_leftover_failed_result(tmp_path, monkeypatch):
    """`done` (a real generated file) takes priority even if a stale failed huey result is also
    present -- see `task_state`'s own docstring for why."""
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
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
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
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
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_dataset.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())

    ds.populate()

    status = ds.status()
    assert (status[["crop", "hillshade"]] == "done").all(axis=None)


def test_populate_marks_failed_and_continues(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    monkeypatch.setattr(trn_dataset.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())

    ds.populate()

    status = ds.status().set_index("product_id")
    assert status.loc["P1", "crop"] == "failed"
    assert status.loc["P1", "hillshade"] == "done"
    assert status.loc["P2", "crop"] == "done"
    assert status.loc["P2", "hillshade"] == "done"


def test_populate_retry_failed_clears_errors_and_reruns(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    monkeypatch.setattr(trn_dataset.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    ds.populate()
    assert ds.status().set_index("product_id").loc["P1", "crop"] == "failed"

    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    ds.populate(retry_failed=True)

    assert ds.status().set_index("product_id").loc["P1", "crop"] == "done"


def test_populate_limit_stops_after_n_entries_and_is_resumable(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_dataset.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2", "P3"]), TrntestConfig())

    ds.populate(limit=2)

    status = ds.status().set_index("product_id")
    assert (status.loc["P1"] == "done").all()
    assert (status.loc["P2"] == "done").all()
    assert (status.loc["P3"] == "pending").all()

    ds.populate(limit=2)  # a later worker resumes against the same folder

    status = ds.status().set_index("product_id")
    assert (status.loc["P3"] == "done").all()


def test_populate_limit_zero_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_dataset.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())

    ds.populate(limit=0)

    assert (ds.status()[["crop", "hillshade"]] == "pending").all(axis=None)


def test_populate_limit_does_not_count_already_done_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_dataset.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())
    ds.populate(limit=1)
    assert (ds.status().set_index("product_id").loc["P1"] == "done").all()

    # P1 is already fully done -- a fresh call with limit=1 should skip straight past it (free) and
    # do new work on P2, not stop having "used up" its budget on P1 again.
    ds.populate(limit=1)

    assert (ds.status().set_index("product_id").loc["P2"] == "done").all()


# -- truncate() -----------------------------------------------------------------------------------


def test_truncate_single_entry_reverts_to_pending_and_leaves_others_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_dataset.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())
    ds.populate()
    assert ds[0].crop.exists() and ds[0].hillshade.exists()

    ds.truncate(ds[0])

    status = ds.status().set_index("product_id")
    assert (status.loc["P1"] == "pending").all()
    assert (status.loc["P2"] == "done").all()
    assert not ds[0].crop.raster_path.exists()
    assert not ds[0].crop.sidecar_json_path.exists()
    assert not ds[0].hillshade.raster_path.exists()


def test_truncate_none_reverts_every_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_dataset.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())
    ds.populate()

    ds.truncate()

    assert (ds.status()[["crop", "hillshade"]] == "pending").all(axis=None)


def test_truncate_then_populate_actually_regenerates(tmp_path, monkeypatch):
    call_count = {"n": 0}

    def counting_generate_impl(image):
        call_count["n"] += 1
        _fake_generate_impl(image)

    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", counting_generate_impl)
    monkeypatch.setattr(trn_dataset.TrnTestHillshadeImage, "_generate_impl", counting_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    ds.populate()
    assert call_count["n"] == 2  # crop + hillshade

    ds.populate()  # already done -- no new calls
    assert call_count["n"] == 2

    ds.truncate(ds[0])
    ds.populate(limit=1)

    assert call_count["n"] == 4  # crop + hillshade regenerated
    assert (ds.status().set_index("product_id").loc["P1"] == "done").all()


def test_truncate_clears_stored_results_from_both_queues(tmp_path, monkeypatch):
    """`truncate()` must clear a stored failure from `tasks.huey_parallel` too, not just
    `tasks.huey` -- a task's most recent attempt could have gone through `populate_via_workers()`,
    and a stale failure there would otherwise still show up via
    `status(huey_instance=tasks.huey_parallel)` after truncate() claims to have reset everything."""
    _use_immediate_parallel_queue(monkeypatch)
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
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
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_dataset.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())

    ds.populate_via_workers()

    status = ds.status(huey_instance=tasks.huey_parallel)
    assert (status[["crop", "hillshade"]] == "done").all(axis=None)


def test_populate_via_workers_marks_failed_and_continues(tmp_path, monkeypatch):
    _use_immediate_parallel_queue(monkeypatch)
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    monkeypatch.setattr(trn_dataset.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2"]), TrntestConfig())

    ds.populate_via_workers()

    status = ds.status(huey_instance=tasks.huey_parallel).set_index("product_id")
    assert status.loc["P1", "crop"] == "failed"
    assert status.loc["P1", "hillshade"] == "done"
    assert status.loc["P2", "crop"] == "done"


def test_populate_via_workers_retry_failed_clears_and_reruns(tmp_path, monkeypatch):
    _use_immediate_parallel_queue(monkeypatch)
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1"]), TrntestConfig())
    ds.populate_via_workers(product_types=("crop",))
    assert trn_dataset.task_state(ds[0], "crop", huey_instance=tasks.huey_parallel) == "failed"

    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    ds.populate_via_workers(product_types=("crop",), retry_failed=True)

    assert trn_dataset.task_state(ds[0], "crop", huey_instance=tasks.huey_parallel) == "done"


def test_populate_via_workers_limit_stops_after_n_entries_and_is_resumable(tmp_path, monkeypatch):
    _use_immediate_parallel_queue(monkeypatch)
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl)
    monkeypatch.setattr(trn_dataset.TrnTestHillshadeImage, "_generate_impl", _fake_generate_impl)
    ds = trn_dataset.TrnTestDataSet(tmp_path / "ds", _minimal_manifest(["P1", "P2", "P3"]), TrntestConfig())

    ds.populate_via_workers(limit=2)

    status = ds.status(huey_instance=tasks.huey_parallel).set_index("product_id")
    assert (status.loc["P1"] == "done").all()
    assert (status.loc["P2"] == "done").all()
    assert (status.loc["P3"] == "pending").all()

    ds.populate_via_workers(limit=2)

    status = ds.status(huey_instance=tasks.huey_parallel).set_index("product_id")
    assert (status.loc["P3"] == "done").all()


def test_populate_via_workers_uses_a_queue_separate_from_populate(tmp_path, monkeypatch):
    """A failure recorded via `populate_via_workers()` is invisible to a plain `status()` call
    (`tasks.huey`'s own queue) -- the two are independent, by design (see `trntest.tasks`'s
    docstring)."""
    _use_immediate_parallel_queue(monkeypatch)
    monkeypatch.setattr(trn_dataset.TrnTestCropImage, "_generate_impl", _fake_generate_impl_failing_crop_for("P1"))
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
