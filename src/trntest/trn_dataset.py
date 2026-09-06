"""A self-contained, resumable dataset folder: `TrnTestDataSet` (a manifest + typed `crop`/
`hillshade`/`reproject`/`reports` subfolders) and `TrnTestEntry` (one manifest row's shared, cached
state, including `entry.crop`/`entry.hillshade`/`entry.reproject`/`entry.report` -- each a
`trn_products.py` product instance). See `trn_products.py`'s own docstring for the product-type
class hierarchy (`TrnTestProduct`/`TrnTestImage`/etc.) those properties construct.

**Only one `populate()` call should run against a given dataset folder at a time** -- for
multi-worker parallel population, use `populate_via_workers()` instead.

`PRODUCT_TYPES` (`populate()`/`status()`'s default) is `("crop", "hillshade", "report")`;
`reproject` is implemented but opt-in (pass `product_types=(..., "reproject")` explicitly).
"""
# An incrementally/resumably populated alternative to candidate_window.generate_dataset()'s flat,
# all-at-once output layout, driven by trntest.tasks's huey task queue -- see that module's
# docstring for the full design, and README.md's trn_dataset.py/tasks.py rows for the current
# architecture summary.
#
# populate_via_workers() routes through a separate huey queue plus a huey_consumer subprocess it
# manages itself (see trntest.tasks's docstring) -- not a substitute for running several
# populate() calls concurrently, a different mechanism entirely.
#
# reproject (TrnTestReprojectImage, sat_sim fed by the WAC crop's own reflectance instead of the
# Lunaserv/Astropedia basemap, through the same camera as hillshade) is kept opt-in until it's
# wired into a notebook and validated at dataset scale, not just the one image
# docs/reproject-fov-investigation.md cross-validated -- see that doc for reproject's own history.
#
# report (TrnTestReport, see src/trntest/report.py) is default-on, unlike reproject: it's cheap
# (no SPICE/ISIS/sat_sim of its own) and self-ensures its own dependency
# (TrnTestReport._generate_impl calls entry.hillshade.generate() itself) rather than relying on
# callers passing product_types in a particular order -- same reasoning TrnTestReprojectImage
# already applies via entry.crop_result's cached_property chain.

import functools
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
from huey import Huey
from huey.api import Result, TaskWrapper
from huey.exceptions import TaskException

from trntest import camera as camera_module
from trntest import (
    candidate_window,
    dem_ortho,
    hapke,
    isis_wac,
    orientation,
    tasks,
    tie_points,
    trn_products,
)
from trntest.camera import Camera, FrameTiming
from trntest.config import TrntestConfig, load_config
from trntest.dem_ortho import DemOrthoResult
from trntest.orientation import DisplayRotations

PRODUCT_TYPES = ("crop", "hillshade", "report")  # "reproject" is implemented
# (TrnTestReprojectImage) but opt-in only -- pass product_types=(..., "reproject") explicitly; see
# module docstring.


class TrnTestEntry:
    """One manifest row's shared, cached, expensive-to-derive state, reused by every product-type
    image built from it (`entry.crop`/`entry.hillshade`/`entry.reproject`)."""

    # functools.cached_property throughout, so each dependency is fetched/computed at most once no
    # matter how many of entry.crop/entry.hillshade's methods touch it.

    def __init__(self, row: pd.Series, dataset_folder: Path, config: TrntestConfig):
        self.row = row
        self.dataset_folder = Path(dataset_folder)
        self.config = config

    @property
    def edr_product(self) -> str:
        return self.row["edr_product"]

    @property
    def product_id(self) -> str:
        return self.row["product_id"]

    @property
    def index(self) -> int:
        """This entry's positional index in its dataset (`TrnTestDataSet.images` is reset to a dense
        `0..n-1` index at construction, so `self.row.name` is always that position regardless of
        whether this entry was looked up by position or by `product_id`) -- `report.load_entry`'s
        primary lookup key."""
        return int(self.row.name)

    @functools.cached_property
    def per_image_config(self) -> TrntestConfig:
        return candidate_window._per_image_config(
            self.row, self.config, self.dataset_folder / "_work" / self.edr_product
        )

    @functools.cached_property
    def frame_timing(self) -> FrameTiming:
        return camera_module.fetch_frame_timing(self.per_image_config)

    @functools.cached_property
    def camera(self) -> Camera:
        return camera_module.build_camera(self.per_image_config)

    @functools.cached_property
    def stitched(self) -> isis_wac.FramestitchResult:
        """The stitched WAC cube."""
        # Idempotent with the cube self.camera (via build_camera) already produced internally --
        # see isis_wac.run_pipeline's own comment for why re-deriving it here, rather than caching
        # it off camera, is cheap, not duplicated ISIS work.
        return isis_wac.run_pipeline(self.camera.reverse_crop_along_track, self.frame_timing, self.per_image_config)

    @functools.cached_property
    def crop_result(self) -> isis_wac.CropResult:
        """The private crop cube (`_work/<edr_product>/isis/...`) -- distinct from
        `TrnTestCropImage.raster_path`, the published copy `crop.generate()` makes of it."""
        return isis_wac.crop_for_camera(self.stitched, self.camera, self.per_image_config)

    @functools.cached_property
    def crop_footprint(self) -> dict:
        return tie_points.crop_footprint_corners_for_camera(self.frame_timing, self.camera, self.per_image_config)

    @functools.cached_property
    def dem_ortho_result(self) -> DemOrthoResult:
        """The DEM/ortho pair for this entry -- resumed from a prior `generate()` run's own files
        on disk if present, else fetched fresh from Lunaserv/Astropedia."""
        # The resumability win `dataset.populate()`'s second-run-near-instant behavior depends on,
        # since a fresh fetch is by far the most expensive part of generating either product type.
        # Looks for `hapke.DEFAULT_HAPKE_SHADING`/`DEFAULT_ALONG_TRACK_CORRECTION`/
        # `DEFAULT_REAL_HAPKE_PARAMS`/`DEFAULT_ORTHO_SOURCE`'s own filename specifically
        # (`ortho_shaded_filename`) rather than a hardcoded name, so this can never resume a stale
        # *other*-mode ortho left over from before any default changed (or from a one-off
        # non-default call elsewhere) under the current defaults' name -- `fetch_dem_and_ortho`
        # below picks up the same defaults itself.
        ortho_path = self.per_image_config.output_dir / dem_ortho.ortho_shaded_filename(
            hapke.DEFAULT_HAPKE_SHADING,
            hapke.DEFAULT_ALONG_TRACK_CORRECTION,
            hapke.DEFAULT_REAL_HAPKE_PARAMS,
            dem_ortho.DEFAULT_ORTHO_SOURCE,
        )
        dem_path = self.per_image_config.output_dir / "dem_filled-tile-0.tif"
        if ortho_path.exists() and dem_path.exists():
            return dem_ortho.result_from_files(ortho_path, dem_path)
        return dem_ortho.fetch_dem_and_ortho(
            self.camera, self.per_image_config, extra_footprint_lonlat_deg=self.crop_footprint
        )

    @functools.cached_property
    def rotations(self) -> DisplayRotations:
        return orientation.compute_display_rotations(self.camera, self.frame_timing, self.per_image_config)

    @functools.cached_property
    def crop(self) -> trn_products.TrnTestCropImage:
        return trn_products.TrnTestCropImage(self)

    @functools.cached_property
    def hillshade(self) -> trn_products.TrnTestHillshadeImage:
        return trn_products.TrnTestHillshadeImage(self)

    @functools.cached_property
    def reproject(self) -> trn_products.TrnTestReprojectImage:
        return trn_products.TrnTestReprojectImage(self)

    @functools.cached_property
    def report(self) -> trn_products.TrnTestReport:
        return trn_products.TrnTestReport(self)

    @property
    def images_by_type(self) -> dict[str, trn_products.TrnTestProduct]:
        return {"crop": self.crop, "hillshade": self.hillshade, "reproject": self.reproject, "report": self.report}


class TrnTestDataSet:
    """A self-contained dataset folder: `manifest.csv` plus `crop`/`hillshade`/`reproject`/`_work`
    subfolders. Iterating/indexing yields `TrnTestEntry` objects; `populate()` drives the task
    queue until nothing's left `pending`."""

    # manifest.csv matches candidate_window.DATASET_COLUMNS' shape (candidate_window.write_manifest/read_manifest).
    # Task-queue state itself lives outside the dataset folder -- see trntest.tasks's docstring for
    # why.

    def __init__(self, folder: Path | str, images: pd.DataFrame, config: TrntestConfig):
        self.folder = Path(folder)
        self.images = images.reset_index(drop=True)
        self.config = config

    @property
    def name(self) -> str:
        """The dataset's display name, for page titles/headings -- just the folder's own name; no
        separate stored field, since the folder name already serves as the standard identifier."""
        return self.folder.name

    @classmethod
    def create(cls, folder: Path | str, images: pd.DataFrame, config: TrntestConfig | None = None) -> "TrnTestDataSet":
        """Idempotent: (re)writes `manifest.csv` from `images`, ensures `crop`/`hillshade`/
        `reproject`/`reports`/`_work` exist. Never touches already-generated product files -- those live
        under `crop`/`hillshade`, untouched by this call."""
        config = config or load_config()
        folder = Path(folder)
        for sub in ("crop", "hillshade", "reproject", "reports", "_work"):
            (folder / sub).mkdir(parents=True, exist_ok=True)
        candidate_window.write_manifest(images, folder / "manifest.csv")
        return cls(folder, images, config)

    @classmethod
    def open(cls, folder: Path | str, config: TrntestConfig | None = None) -> "TrnTestDataSet":
        """Reads `manifest.csv` from an existing folder -- no `images` needed."""
        config = config or load_config()
        folder = Path(folder)
        images = candidate_window.read_manifest(folder / "manifest.csv")
        return cls(folder, images, config)

    def __len__(self) -> int:
        return len(self.images)

    def __iter__(self) -> Iterator[TrnTestEntry]:
        for i in range(len(self)):
            yield self[i]

    def __getitem__(self, key: int | str) -> TrnTestEntry:
        """`int` indexes positionally; `str` looks up by `product_id`.

        :raises KeyError: if no entry matches `product_id`.
        """
        if isinstance(key, str):
            matches = self.images["product_id"] == key
            if not matches.any():
                raise KeyError(key)
            row = self.images[matches].iloc[0]
        else:
            row = self.images.iloc[key]
        return TrnTestEntry(row, self.folder, self.config)

    def populate(
        self,
        product_types: tuple[str, ...] = PRODUCT_TYPES,
        retry_failed: bool = False,
        limit: int | None = None,
        write_index: bool = True,
    ) -> None:
        """Drives the task queue sequentially, entry by entry: for each entry with any pending
        product type, generates its pending subset of `product_types` and waits for it before
        moving on.

        :param limit: Stop after doing new work on this many distinct entries (an entry already
            done or failed doesn't count against it). Call `populate(limit=N)` repeatedly to split
            a large dataset's population across several calls -- each pass picks up wherever the
            last one left off.
        :param write_index: Refresh `status.csv`/`reports/index.html` (see `write_index()`) after
            this call -- cheap, pure Python, safe to leave on.
        """
        # Task granularity is per-entry, not per `(entry, product_type)` -- see
        # `tasks._generate_entry`'s own comment for why. `huey`'s default `immediate=True` (see
        # `trntest.tasks`'s docstring) means this executes synchronously in this process --
        # consistent with this project's existing rule that SPICE/spiceypy state is process-global
        # and unsafe across concurrent calls within one process. One entry's failure doesn't stop
        # the rest (`TaskException` is caught, not raised) -- a batch of network/ISIS calls is
        # expected to have occasional failures.
        #
        # Not safe to run from more than one process concurrently against the same dataset folder
        # -- see this module's own docstring.
        if retry_failed:
            for entry in self:
                if any(task_state(entry, pt) == "failed" for pt in product_types):
                    _clear_stored_result(self.folder, entry.product_id, huey_instance=tasks.huey)

        # huey's immediate=True (trntest.tasks's docstring) means each task already ran, synchronously,
        # by the time huey.enqueue() returns inside _enqueue_pending -- so waiting on every Result only
        # after collecting them all is equivalent to waiting right after each one, not a behavior change.
        for result in _enqueue_pending(self, product_types, limit, tasks.huey, tasks.generate_product):
            _await_result(result)
        if write_index:
            self.write_index(product_types)

    def populate_via_workers(
        self,
        product_types: tuple[str, ...] = PRODUCT_TYPES,
        retry_failed: bool = False,
        limit: int | None = None,
        workers: int = 4,
        write_index: bool = True,
    ) -> None:
        """`populate()`'s multi-worker equivalent: same `product_types`/`retry_failed`/`limit`/
        `write_index` semantics, but runs `workers` worker processes in parallel instead of
        sequentially. Blocks until the whole batch finishes; manages its own consumer subprocess
        for the call's duration, so there's no separate terminal/process to set up first.

        :param workers: Number of parallel worker processes.
        """
        # Routes through trntest.tasks.huey_parallel (tasks.start_consumer/stop_consumer) so
        # image.generate() calls run in `-k process` worker processes.
        #
        # If this call is interrupted (an exception, Ctrl-C) partway through, the consumer
        # subprocess is still torn down (`finally`), but any tasks it had already claimed keep
        # running in their own worker processes until they finish -- huey's own `SIGTERM`
        # handling, not this method's; check `status(huey_instance=tasks.huey_parallel)` and
        # re-run to pick up whatever's still pending.
        #
        # Uses `tasks.huey_parallel`'s own separate queue/result store -- a task's `failed` state
        # recorded here is invisible to a plain `status()` call (which only checks `tasks.huey`)
        # unless you pass `huey_instance=tasks.huey_parallel` explicitly; `done` is unaffected
        # either way (always disk-based). Safe to run concurrently with `populate()` itself
        # (different queues, different sqlite files) but, like `populate()`, only one
        # `populate_via_workers()` call should run against a given dataset folder at a time -- this
        # just moves where the single caller's own parallelism comes from, it doesn't add
        # cross-process claim safety.
        if retry_failed:
            for entry in self:
                if any(task_state(entry, pt, huey_instance=tasks.huey_parallel) == "failed" for pt in product_types):
                    _clear_stored_result(self.folder, entry.product_id, huey_instance=tasks.huey_parallel)

        results = _enqueue_pending(self, product_types, limit, tasks.huey_parallel, tasks.generate_product_parallel)
        if results:
            consumer = tasks.start_consumer(workers)
            try:
                for result in results:
                    _await_result(result)
            finally:
                tasks.stop_consumer(consumer)
        if write_index:
            self.write_index(product_types)

    def status(self, product_types: tuple[str, ...] = PRODUCT_TYPES, huey_instance: Huey = tasks.huey) -> pd.DataFrame:
        """Per-entry, per-product-type status: `done`/`failed`/`pending` (see `task_state`).

        :param huey_instance: Which queue's stored results to check for `failed` -- `tasks.huey`
            (`populate()`'s queue, the default) or `tasks.huey_parallel`
            (`populate_via_workers()`'s). `done` is unaffected either way (always disk-based).
        """
        rows = [
            {"product_id": entry.product_id, **{pt: task_state(entry, pt, huey_instance) for pt in product_types}}
            for entry in self
        ]
        return pd.DataFrame(rows, columns=["product_id", *product_types])

    def write_index(self, product_types: tuple[str, ...] = PRODUCT_TYPES) -> None:
        """Writes `<folder>/status.csv` (`status()` plus a `problems` column, see
        `report.problem_flags`) and `<folder>/reports/index.html` (a nav bar linking to each
        entry's own `reports/<edr_product>/report.html`, alongside the same status/problem info) --
        covers every entry in the dataset, not just ones touched by whatever call (if any)
        triggered this.

        Cheap and pure Python (no subprocess) -- safe to call on its own to refresh the index
        without a full `populate()` pass. Like `populate()`/`populate_via_workers()`, not safe to
        run concurrently with itself against the same dataset folder (writes shared files).
        """
        from trntest import report  # noqa: PLC0415 -- circular otherwise (report.py imports
        # TrnTestDataSet/TrnTestEntry from this module)

        # mkdir here rather than relying on create() having already run -- some callers (e.g. this
        # project's own tests) construct a TrnTestDataSet directly.
        (self.folder / "reports").mkdir(parents=True, exist_ok=True)
        status_df = self.status(product_types)
        status_df["problems"] = ["; ".join(report.problem_flags(entry)) for entry in self]
        status_df.to_csv(self.folder / "status.csv", index=False)
        report.write_index_html(self, status_df)

    def truncate(
        self,
        entries: "TrnTestEntry | list[TrnTestEntry] | None" = None,
        product_types: tuple[str, ...] = PRODUCT_TYPES,
    ) -> None:
        """Delete already-generated product file(s) (`raster_path`/`sidecar_json_path`) and any
        task-queue result state for `entries` (a single `TrnTestEntry`, a list of them, or `None`
        for every entry in this dataset) across `product_types` -- reverting their `task_state`
        back to `"pending"` so a subsequent `populate()` call regenerates them from scratch.

        Leaves `_work/<edr_product>/` intermediates (DEM/ortho, `.tsai`) alone -- regeneration
        reuses those where still valid (see `TrnTestEntry.dem_ortho_result`'s own resume-from-files
        check); delete `dataset.folder / "_work" / <edr_product>` yourself first if you also want
        those re-fetched from scratch.
        """
        # For forcing a clean re-run -- e.g. a notebook that always wants fresh output reflecting
        # the latest pipeline code rather than silently reusing a stale prior run, unlike
        # `populate()`'s own default "skip what's already done" behavior -- without
        # deleting/recreating the whole dataset folder.
        #
        # Clears stored results from *both* `tasks.huey` and `tasks.huey_parallel` -- a task's
        # most recent attempt could have gone through either `populate()` or
        # `populate_via_workers()`, and this should revert to `pending` for both regardless of
        # which one last touched it.
        #
        # The stored huey result cleared is the whole *entry's* (task granularity is per-entry,
        # not per `(entry, product_type)` -- see `tasks._generate_entry`'s own comment), even if
        # `product_types` only names a subset -- harmless: a product type left off
        # `product_types` keeps its own file untouched, so its own `task_state()` still reports
        # correctly via `image.exists()` regardless of whether the entry's shared stored result
        # got cleared.
        target_entries = list(self) if entries is None else entries if isinstance(entries, list) else [entries]
        for entry in target_entries:
            for product_type in product_types:
                image = entry.images_by_type[product_type]
                image.raster_path.unlink(missing_ok=True)
                image.sidecar_json_path.unlink(missing_ok=True)
            _clear_stored_result(self.folder, entry.product_id, huey_instance=tasks.huey)
            _clear_stored_result(self.folder, entry.product_id, huey_instance=tasks.huey_parallel)


# -- Task queue: backed by trntest.tasks's huey instances, no filesystem lock/error files of our
# own anymore -- task list is one task per manifest row (entry), each covering every requested
# product type for it (see tasks._generate_entry's own comment for why); `done` is still just
# `image.exists()`, per product type, `failed` is whatever the given huey instance's own
# sqlite-backed result store says for that entry's deterministic id (see trntest.tasks.task_id).
# See that module's docstring for the full design and why there's no
# more `in_progress` state or manual crash-recovery step (a killed process just leaves nothing
# behind to clean up -- the next populate*() call re-enqueues based on disk state alone).


def task_state(entry: TrnTestEntry, product_type: str, huey_instance: Huey = tasks.huey) -> str:
    """This entry/product_type's state: `done`/`failed`/`pending`.

    :param huey_instance: Which queue's stored results to check for `failed` -- `tasks.huey`
        (`populate()`'s queue, the default) or `tasks.huey_parallel` (`populate_via_workers()`'s);
        the two are independent, so a `failed` state under one is invisible under the other.
    :returns: `done` if `entry.images_by_type[product_type].exists()` (checked first, so a
        manually-fixed-up product file always wins regardless of any stored huey result), else
        `failed` or `pending` per the stored huey result.
    """
    # The stored huey result this falls back to is keyed per *entry*, not per
    # `(entry, product_type)` (see `tasks._generate_entry`'s own comment for why task granularity
    # is entry-level) -- so if one product type in a task failed while another succeeded, both
    # share the same stored result. This still reports each product type correctly: the
    # succeeded one's `exists()` check above already returns `done` before this fallback is ever
    # reached, and the failed one correctly falls through to it -- imprecise only in attributing a
    # shared `failed` signal to a specific product type when more than one in the same task didn't
    # complete.
    if entry.images_by_type[product_type].exists():
        return "done"
    tid = tasks.task_id(str(entry.dataset_folder), entry.product_id)
    try:
        huey_instance.result(tid, preserve=True)
    except TaskException:
        return "failed"
    return "pending"


def _clear_stored_result(dataset_folder: Path, product_id: str, huey_instance: Huey) -> None:
    """Pops (discards) an entry's stored task result from `huey_instance`, if any, so it's no
    longer reported `failed` there. No-op if the task never ran (on this instance) or was already
    cleared."""
    tid = tasks.task_id(str(dataset_folder), product_id)
    try:
        huey_instance.result(tid, preserve=False)
    except TaskException:
        pass


def _enqueue_pending(
    dataset_obj: "TrnTestDataSet",
    product_types: tuple[str, ...],
    limit: int | None,
    huey_instance: Huey,
    task_fn: TaskWrapper,
) -> list[Result]:
    """Shared by `populate()`/`populate_via_workers()`: enqueues one task per entry with any
    pending product type, covering that entry's own pending subset of `product_types`.

    :param limit: Stop after `limit` distinct entries with new pending work.
    :returns: The enqueued `Result` handles, not yet waited on.
    """
    # An already-done/failed type for an entry is left out; retry_failed=True clears a failed
    # entry first so its task gets rebuilt covering it again. Waiting is left to the caller since
    # populate() and populate_via_workers() want to wait differently (the former inherently
    # already has, by the time this returns -- see its own comment; the latter only after its
    # consumer subprocess is up).
    results = []
    entries_done = 0
    for entry in dataset_obj:
        if limit is not None and entries_done >= limit:
            break
        pending_types = tuple(pt for pt in product_types if task_state(entry, pt, huey_instance) == "pending")
        if not pending_types:
            continue
        task = task_fn.s(entry, pending_types)
        task.id = tasks.task_id(str(dataset_obj.folder), entry.product_id)
        results.append(huey_instance.enqueue(task))
        entries_done += 1
    return results


def _await_result(result: Result) -> None:
    """Blocks on `result`, discarding a `TaskException` so one bad task doesn't abort the batch."""
    # `preserve=True`: a plain `.get()` pops the stored result on read, which would erase a
    # failure's record before `task_state()` ever gets a chance to report it -- confirmed
    # empirically. Successes stay preserved too (harmless; `task_state()` never queries huey for
    # the `done` case, disk existence wins first).
    try:
        result.get(blocking=True, preserve=True)
    except TaskException:
        pass
