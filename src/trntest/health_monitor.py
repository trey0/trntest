"""Background health-monitor thread for `TrnTestDataSet.populate_via_workers()`. See
`docs/proposed-tasks/health-monitor.md` for the design this implements.
"""

import shutil
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import psutil
from huey import Huey
from huey.api import Result
from huey.exceptions import TaskException

POLL_SECONDS = 5.0
_RATE_WINDOW_SECONDS = 180.0  # rolling window used to derive throughput for the ETA/disk forecast


class HealthMonitor:
    """Polls one `populate_via_workers()` run's own progress every `poll_seconds` and appends one
    `key=value` line to `<dataset_folder>/logs/health_monitor_log.txt` -- readable via `tail -f`
    without needing a header, still trivially parseable back into rows for post-run plotting.

    Runs as a daemon thread, started/stopped alongside the run's own consumer subprocess (see
    `TrnTestDataSet.populate_via_workers`) -- never a standalone process, so a killed calling
    process takes it down for free instead of leaving it orphaned.
    """

    def __init__(
        self,
        dataset_folder: Path,
        huey_instance: Huey,
        results: list[Result],
        workers: int,
        consumer_pid: int | None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        """
        :param results: The `Result` handles `_enqueue_pending` returned for this run -- defines
            the exact set of tasks this monitor tracks (`huey_instance.pending_count()` is used to
            count how many are still queued, which is only correct because at most one
            `populate_via_workers()` run is ever active against one dataset folder at a time --
            see `docs/batch-generation.md`'s "Not safe to run concurrently with itself").
        :param consumer_pid: The consumer subprocess's PID, for the memory/CPU fields -- `None`
            skips those fields (reported `n/a`), for callers (tests) that fake out
            `tasks.start_consumer` and have no real subprocess to inspect.
        """
        self._huey = huey_instance
        self._task_ids = [r.id for r in results]
        self._workers = workers
        self._consumer_pid = consumer_pid
        self._poll_seconds = poll_seconds
        self.log_path = dataset_folder / "logs" / "health_monitor_log.txt"
        # Created here, not in start(): TrnTestDataSet.create() already ensures dataset_folder/logs
        # exists for real callers, but shutil.disk_usage below needs dataset_folder to exist right
        # now regardless -- true for any real run, not guaranteed for a bare TrnTestDataSet(folder,
        # ...) construction that skips create() (some tests do this directly).
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._start_free_bytes = shutil.disk_usage(dataset_folder).free
        self._samples: deque[tuple[float, int]] = deque()  # (time.monotonic(), completed count)
        self._tracked_procs: dict[int, psutil.Process] = {}

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Signals the thread to stop and waits for it -- it always writes one final line
        reflecting the run's true end state before exiting, so this blocks briefly even on a
        fast-finishing run, not just a slow one."""
        self._stop_event.set()
        self._thread.join(timeout=self._poll_seconds * 2)

    def _run(self) -> None:
        with open(self.log_path, "a") as log_file:
            while not self._stop_event.wait(self._poll_seconds):
                log_file.write(self._poll_line() + "\n")
                log_file.flush()
            log_file.write(self._poll_line() + "\n")
            log_file.flush()

    def _poll_line(self) -> str:
        # `queued_not_started` (still sitting in the queue) is a plain count, not a per-id check --
        # see `results`'s own docstring for why that's still exactly this run's own count. `done`/
        # `failed` are checked per task id since old results from a *previous* run against this
        # same folder can still be sitting in `huey_instance`'s result store (`preserve=True`
        # everywhere -- see `trn_dataset._await_result`), and would otherwise be double-counted.
        queued_not_started = self._huey.pending_count()
        done = failed = 0
        for task_id in self._task_ids:
            try:
                result = self._huey.result(task_id, blocking=False, preserve=True)
            except TaskException:
                failed += 1
                continue
            if result is not None:
                done += 1
        total = len(self._task_ids)
        completed = done + failed
        active = total - queued_not_started - completed

        now = time.monotonic()
        self._samples.append((now, completed))
        while len(self._samples) > 1 and now - self._samples[0][0] > _RATE_WINDOW_SECONDS:
            self._samples.popleft()
        eta_min = self._eta_minutes(now, completed, total)
        disk_free_gb, disk_eta_gb = self._disk_forecast(completed, total)
        mem_mb = self._memory_mb()
        cpu_pct = self._cpu_pct()

        fields = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "done": done,
            "failed": failed,
            "pending": total - completed,
            "pct_ok": _pct(done, completed),
            "pct_done": _pct(completed, total),
            "eta_min": _fmt(eta_min),
            "disk_free_gb": _fmt(disk_free_gb),
            "disk_eta_gb": _fmt(disk_eta_gb),
            "mem_mb": _fmt(mem_mb),
            "cpu_pct": _fmt(cpu_pct),
            "active": f"{active}/{self._workers}",
        }
        return " ".join(f"{key}={value}" for key, value in fields.items())

    def _eta_minutes(self, now: float, completed: int, total: int) -> float | None:
        remaining = total - completed
        if remaining <= 0:
            return 0.0
        window_start_time, window_start_completed = self._samples[0]
        elapsed = now - window_start_time
        completed_in_window = completed - window_start_completed
        if elapsed <= 0 or completed_in_window <= 0:
            return None  # not enough of a rate signal yet
        rate = completed_in_window / elapsed  # entries/sec
        return remaining / rate / 60.0

    def _disk_forecast(self, completed: int, total: int) -> tuple[float, float | None]:
        free_bytes = shutil.disk_usage(self.log_path.parent.parent).free
        free_gb = free_bytes / 1e9
        remaining = total - completed
        if remaining <= 0:
            return free_gb, free_gb
        used_bytes = self._start_free_bytes - free_bytes
        if completed <= 0 or used_bytes <= 0:
            return free_gb, None  # nothing measurable to extrapolate from yet
        forecast_used_bytes = (used_bytes / completed) * remaining
        return free_gb, free_gb - forecast_used_bytes / 1e9

    def _tracked_processes(self) -> list[psutil.Process]:
        if self._consumer_pid is None:
            return []
        try:
            consumer = psutil.Process(self._consumer_pid)
            current = {self._consumer_pid: consumer, **{c.pid: c for c in consumer.children(recursive=True)}}
        except psutil.NoSuchProcess:
            current = {}
        for pid, proc in current.items():
            if pid not in self._tracked_procs:
                try:
                    proc.cpu_percent(interval=None)  # primes the delta counter -- first reading is meaningless
                except psutil.NoSuchProcess:
                    continue  # exited between the children() snapshot above and this priming call
                self._tracked_procs[pid] = proc
        for pid in list(self._tracked_procs):
            if pid not in current:
                del self._tracked_procs[pid]  # a worker process recycled between polls
        return list(self._tracked_procs.values())

    def _memory_mb(self) -> float | None:
        procs = self._tracked_processes()
        if not procs:
            return None
        total = 0
        for proc in procs:
            try:
                total += proc.memory_info().rss
            except psutil.NoSuchProcess:
                continue
        return total / (1024 * 1024)

    def _cpu_pct(self) -> float | None:
        procs = self._tracked_processes()
        if not procs:
            return None
        total = 0.0
        for proc in procs:
            try:
                total += proc.cpu_percent(interval=None)
            except psutil.NoSuchProcess:
                continue
        return total


def _pct(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator * 100:.1f}" if denominator else "n/a"


def _fmt(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "n/a"
