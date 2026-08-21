"""A minimal, picklable, SPICE/ASP/ISIS-free stand-in for `TrnTestImage`, used only by
`test_trn_dataset.py`'s real-subprocess `huey_consumer` test. Lives in its own top-level-importable
module (not inside `test_trn_dataset.py` itself) so a fresh `huey_consumer -k process` worker
process -- which only gets `tests/` on its `PYTHONPATH`, not the full `trntest` package's own heavy
dependencies -- can unpickle and run it without ever needing to import `trntest.trn_dataset`."""

from pathlib import Path


class FakeWorkerTask:
    """`generate()` matches `TrnTestImage`'s own contract closely enough for
    `trntest.tasks.generate_product_parallel` (just calls `image.generate()` and returns the
    result) -- writes `marker_path` and returns it, no real image generation involved."""

    def __init__(self, marker_path: str):
        self.marker_path = marker_path

    def generate(self) -> Path:
        path = Path(self.marker_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("done")
        return path


class FailingWorkerTask:
    """Same shape, always raises -- for testing that a worker-process failure is still visible via
    `tasks.huey_parallel`'s stored result."""

    def generate(self) -> Path:
        raise RuntimeError("boom from worker subprocess")
