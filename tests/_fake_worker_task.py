"""Minimal, picklable, SPICE/ASP/ISIS-free stand-ins for `TrnTestEntry`/`TrnTestImage`, used only by
`test_trn_dataset.py`'s real-subprocess `huey_consumer` test. Live in their own top-level-importable
module (not inside `test_trn_dataset.py` itself) so a fresh `huey_consumer -k process` worker
process -- which only gets `tests/` on its `PYTHONPATH`, not the full `trntest` package's own heavy
dependencies -- can unpickle and run them without ever needing to import `trntest.trn_dataset`."""

from pathlib import Path


class FakeWorkerImage:
    """`generate()` matches `TrnTestImage`'s own contract closely enough for
    `trntest.tasks._generate_entry` (calls `entry.images_by_type[pt].generate()`) -- writes
    `marker_path` and returns it, no real image generation involved."""

    def __init__(self, marker_path: str):
        self.marker_path = marker_path

    def generate(self) -> Path:
        path = Path(self.marker_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("done")
        return path


class FakeWorkerEntry:
    """Minimal stand-in for `TrnTestEntry` -- just the `images_by_type` mapping
    `tasks._generate_entry` actually needs."""

    def __init__(self, marker_path: str):
        self.images_by_type = {"fake": FakeWorkerImage(marker_path)}


class FailingWorkerImage:
    """Same shape, always raises -- for testing that a worker-process failure is still visible via
    `tasks.huey_parallel`'s stored result."""

    def generate(self) -> Path:
        raise RuntimeError("boom from worker subprocess")


class FailingWorkerEntry:
    def __init__(self):
        self.images_by_type = {"fake": FailingWorkerImage()}
