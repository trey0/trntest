import subprocess
import sys
from datetime import UTC, datetime

import pandas as pd
import pytest

from trntest import tasks, trn_dataset
from trntest.config import TrntestConfig


def _minimal_manifest(product_ids: list[str]) -> pd.DataFrame:
    """A manifest DataFrame with just enough columns for the task-queue/class-hierarchy tests below
    -- none of which touch `TrnTestEntry.per_image_config`/`camera`/etc. (no real SPICE/ASP/ISIS), so
    a full `dataset.DATASET_COLUMNS` row isn't needed. `edr_product == product_id`, matching how
    today's real manifest always has them equal (see docs/dataset-plan.md's "On-disk layout" section)."""
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

    tid = tasks.task_id(str(ds.folder), "P1", "crop")
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
