"""A self-contained, resumable dataset folder: `TrnTestDataSet` (a manifest + typed `crop`/
`hillshade`/`reproject` subfolders), `TrnTestEntry` (one manifest row's worth of shared, cached,
expensive-to-derive state), and `TrnTestImage` (one product type of one entry -- `entry.crop`/
`entry.hillshade` -- owning the genuinely shared generate/plot logic once, with small per-type
subclasses). Replaces `dataset.generate_dataset()`'s flat, all-at-once output layout with something
that can be populated incrementally/resumably, driven by the `trntest.tasks` `huey` task queue (see
that module's docstring and docs/dataset-plan.md's "Task queue" section for the full design). See
docs/dataset-plan.md for the full design this implements -- start there before changing anything
here.

**`populate()` no longer supports running several concurrent `docker compose run` invocations
against the same dataset folder as a way to parallelize** -- the old filesystem lock files that made
that safe are gone (see "Task queue" below). Only one `populate()` call should run against a given
dataset folder at a time. For real multi-worker parallel population, use `populate_via_workers()`
instead (a separate `huey` queue + a real `huey_consumer` subprocess this method manages itself --
see `trntest.tasks`'s docstring) -- not a substitute for running several `populate()` calls
concurrently, a different mechanism entirely.

First-pass scope was `crop` + `hillshade` generation only; `reproject` (`TrnTestReprojectImage`,
`sat_sim` fed by the real WAC crop's own reflectance instead of the Lunaserv/Astropedia basemap,
through the exact same camera as `hillshade`) is now implemented too, but deliberately kept out of
`PRODUCT_TYPES` (`populate()`/`status()`'s default) -- opt-in only (pass
`product_types=(..., "reproject")` explicitly) until it's wired into a notebook and validated at
dataset scale, not just the one image `docs/reproject-fov-investigation.md` cross-validated. See
docs/dataset-plan.md for the original design and docs/reproject-fov-investigation.md for
`reproject`'s own history.
"""

import abc
import functools
import shutil
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
from huey import Huey
from huey.api import Result, TaskWrapper
from huey.exceptions import TaskException

from trntest import camera as camera_module
from trntest import dataset, isis_wac, lunaserv, orientation, plotting, render, tasks, tie_points
from trntest.camera import Camera, FrameTiming
from trntest.config import TrntestConfig, load_config
from trntest.lunaserv import DemOrthoResult
from trntest.orientation import DisplayRotations

PRODUCT_TYPES = ("crop", "hillshade")  # "reproject" is implemented (TrnTestReprojectImage) but opt-in
# only -- pass product_types=("crop", "hillshade", "reproject") explicitly; see module docstring.


class TrnTestEntry:
    """One manifest row's worth of shared, cached, expensive-to-derive state -- computed once per
    entry (`functools.cached_property` throughout, so each dependency is fetched/computed at most
    once no matter how many of `entry.crop`/`entry.hillshade`'s methods touch it) and reused by
    every product-type image below it."""

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

    @functools.cached_property
    def per_image_config(self) -> TrntestConfig:
        return dataset._per_image_config(self.row, self.config, self.dataset_folder / "_work" / self.edr_product)

    @functools.cached_property
    def frame_timing(self) -> FrameTiming:
        return camera_module.fetch_frame_timing(self.per_image_config)

    @functools.cached_property
    def camera(self) -> Camera:
        return camera_module.build_camera(self.per_image_config)

    @functools.cached_property
    def stitched(self) -> isis_wac.FramestitchResult:
        """Idempotent, same cube `self.camera` (via `build_camera`) already produced internally --
        see `isis_wac.run_pipeline`'s own docstring for why re-deriving it here (rather than caching
        it off `camera`) is cheap, not duplicated ISIS work."""
        return isis_wac.run_pipeline(self.camera.reverse_crop_along_track, self.frame_timing, self.per_image_config)

    @functools.cached_property
    def crop_result(self) -> isis_wac.CropResult:
        """The scratch-dir crop cube (`config.scratch_dir/isis_wac/<edr_product>/...`) -- distinct
        from `TrnTestCropImage.raster_path`, the dataset-folder copy `crop.generate()` makes of it."""
        return isis_wac.crop_for_camera(self.stitched, self.camera, self.per_image_config)

    @functools.cached_property
    def crop_footprint(self) -> dict:
        return tie_points.crop_footprint_corners_for_camera(self.frame_timing, self.camera, self.per_image_config)

    @functools.cached_property
    def dem_ortho_result(self) -> DemOrthoResult:
        """Resumes from a prior `generate()` run's own DEM/ortho files (`lunaserv.result_from_files`,
        pure IO) when they already exist on disk, instead of re-fetching from Lunaserv/Astropedia --
        the real resumability win `dataset.populate()`'s second-run-near-instant behavior depends
        on, since a fresh fetch is by far the most expensive part of generating either product type.
        Looks for `lunaserv.DEFAULT_HAPKE_SHADING`/`DEFAULT_ALONG_TRACK_CORRECTION`/
        `DEFAULT_REAL_HAPKE_PARAMS`/`DEFAULT_ORTHO_SOURCE`'s own filename specifically
        (`ortho_shaded_filename`) rather than a hardcoded name, so this can never resume a stale
        *other*-mode ortho left over from before any default changed (or from a one-off non-default
        call elsewhere) under the current defaults' name -- `fetch_dem_and_ortho` below picks up the
        same defaults itself."""
        ortho_path = self.per_image_config.output_dir / lunaserv.ortho_shaded_filename(
            lunaserv.DEFAULT_HAPKE_SHADING,
            lunaserv.DEFAULT_ALONG_TRACK_CORRECTION,
            lunaserv.DEFAULT_REAL_HAPKE_PARAMS,
            lunaserv.DEFAULT_ORTHO_SOURCE,
        )
        dem_path = self.per_image_config.output_dir / "dem_filled-tile-0.tif"
        if ortho_path.exists() and dem_path.exists():
            return lunaserv.result_from_files(ortho_path, dem_path)
        return lunaserv.fetch_dem_and_ortho(
            self.camera, self.per_image_config, extra_footprint_lonlat_deg=self.crop_footprint
        )

    @functools.cached_property
    def rotations(self) -> DisplayRotations:
        return orientation.compute_display_rotations(self.camera, self.frame_timing, self.per_image_config)

    @functools.cached_property
    def crop(self) -> "TrnTestCropImage":
        return TrnTestCropImage(self)

    @functools.cached_property
    def hillshade(self) -> "TrnTestHillshadeImage":
        return TrnTestHillshadeImage(self)

    @functools.cached_property
    def reproject(self) -> "TrnTestReprojectImage":
        return TrnTestReprojectImage(self)

    @property
    def images_by_type(self) -> dict[str, "TrnTestImage"]:
        return {"crop": self.crop, "hillshade": self.hillshade, "reproject": self.reproject}


class TrnTestDataSet:
    """A self-contained dataset folder: `manifest.csv` (the same `dataset.DATASET_COLUMNS` shape
    `dataset.write_manifest`/`read_manifest` already use) plus `crop`/`hillshade`/`reproject`/
    `_work` subfolders (task-queue state itself lives outside the dataset folder now -- see
    `trntest.tasks`'s docstring for why). Iterating/indexing yields `TrnTestEntry` objects;
    `populate()` drives the `trntest.tasks` huey queue until nothing's left `pending`."""

    def __init__(self, folder: Path | str, images: pd.DataFrame, config: TrntestConfig):
        self.folder = Path(folder)
        self.images = images.reset_index(drop=True)
        self.config = config

    @classmethod
    def create(cls, folder: Path | str, images: pd.DataFrame, config: TrntestConfig | None = None) -> "TrnTestDataSet":
        """Idempotent: (re)writes `manifest.csv` from `images`, ensures `crop`/`hillshade`/
        `reproject`/`_work` exist. Never touches already-generated product files -- those live
        under `crop`/`hillshade`, untouched by this call."""
        config = config or load_config()
        folder = Path(folder)
        for sub in ("crop", "hillshade", "reproject", "_work"):
            (folder / sub).mkdir(parents=True, exist_ok=True)
        dataset.write_manifest(images, folder / "manifest.csv")
        return cls(folder, images, config)

    @classmethod
    def open(cls, folder: Path | str, config: TrntestConfig | None = None) -> "TrnTestDataSet":
        """Reads `manifest.csv` from an existing folder -- no `images` needed. This is what makes
        "start from an empty folder with just a manifest" work."""
        config = config or load_config()
        folder = Path(folder)
        images = dataset.read_manifest(folder / "manifest.csv")
        return cls(folder, images, config)

    def __len__(self) -> int:
        return len(self.images)

    def __iter__(self) -> Iterator[TrnTestEntry]:
        for i in range(len(self)):
            yield self[i]

    def __getitem__(self, key: int | str) -> TrnTestEntry:
        """`int` indexes positionally; `str` looks up by `product_id` (matching
        `dataset.generate_dataset()`'s existing per-image folder convention)."""
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
    ) -> None:
        """Drives the `trntest.tasks` huey queue (manifest rows x `product_types`) sequentially,
        entry by entry: for each still-`pending` product type, enqueues `tasks.generate_product` and
        blocks on its result before moving on. `huey`'s default `immediate=True` (see
        `trntest.tasks`'s docstring) means this executes synchronously in this process, same
        sequential shape as before -- consistent with this project's existing rule that
        SPICE/spiceypy state is process-global and unsafe across concurrent calls within one
        process. One row's failure doesn't stop the rest (`TaskException` is caught, not raised) --
        a batch of real network/ISIS calls is expected to have occasional real failures.

        `limit`, if given, stops this call after it has done genuinely new work on `limit` distinct
        entries -- an entry this call finds already fully done or fully failed doesn't count against
        it. Meant for splitting a large dataset's population across multiple separate calls: run
        `populate(limit=N)` repeatedly (from this process or a fresh one against the same folder)
        until `status()` shows nothing `pending` -- each call picks up wherever the last one left
        off, via `task_state`'s same disk-plus-huey-result check. Unlike before this module's huey
        migration, this is **not** safe to run from more than one process concurrently against the
        same dataset folder -- see this module's own docstring."""
        if retry_failed:
            for entry in self:
                for product_type in product_types:
                    if task_state(entry, product_type) == "failed":
                        _clear_stored_result(self.folder, entry.product_id, product_type, huey_instance=tasks.huey)

        # huey's immediate=True (trntest.tasks's docstring) means each task already ran, synchronously,
        # by the time huey.enqueue() returns inside _enqueue_pending -- so waiting on every Result only
        # after collecting them all is equivalent to waiting right after each one, not a behavior change.
        for result in _enqueue_pending(self, product_types, limit, tasks.huey, tasks.generate_product):
            _await_result(result)

    def populate_via_workers(
        self,
        product_types: tuple[str, ...] = PRODUCT_TYPES,
        retry_failed: bool = False,
        limit: int | None = None,
        workers: int = 4,
    ) -> None:
        """`populate()`'s real multi-worker equivalent: same `product_types`/`retry_failed`/`limit`
        semantics (see `populate()`'s own docstring -- `limit` still counts distinct entries with
        genuinely new pending work, not completions), but routed through `trntest.tasks.huey_parallel`
        instead of the `immediate=True` default queue, so the actual `image.generate()` calls run in
        `workers` separate `-k process` worker processes rather than sequentially in this one. This
        call still blocks until the whole batch finishes -- from the caller's side, a drop-in
        replacement for `populate()`, just backed by real parallelism instead of one process.

        Manages its own `huey_consumer` subprocess for the duration of this call (`tasks.
        start_consumer`/`stop_consumer`) -- no separate terminal/process to set up first. If this
        call is interrupted (an exception, Ctrl-C) partway through, the consumer subprocess is still
        torn down (`finally`), but any tasks it had already claimed keep running in their own worker
        processes until they finish -- huey's own `SIGTERM` handling, not this method's; check
        `status(huey_instance=tasks.huey_parallel)` and re-run to pick up whatever's still pending.

        Uses `tasks.huey_parallel`'s own separate queue/result store -- a task's `failed` state
        recorded here is invisible to a plain `status()` call (which only checks `tasks.huey`) unless
        you pass `huey_instance=tasks.huey_parallel` explicitly; `done` is unaffected either way
        (always disk-based). Safe to run concurrently with `populate()` itself (different queues,
        different sqlite files) but, like `populate()`, only one `populate_via_workers()` call should
        run against a given dataset folder at a time -- nothing here re-adds the old cross-process
        claim safety, it just moves where the single caller's own parallelism comes from."""
        if retry_failed:
            for entry in self:
                for product_type in product_types:
                    if task_state(entry, product_type, huey_instance=tasks.huey_parallel) == "failed":
                        _clear_stored_result(
                            self.folder, entry.product_id, product_type, huey_instance=tasks.huey_parallel
                        )

        results = _enqueue_pending(self, product_types, limit, tasks.huey_parallel, tasks.generate_product_parallel)
        if not results:
            return

        consumer = tasks.start_consumer(workers)
        try:
            for result in results:
                _await_result(result)
        finally:
            tasks.stop_consumer(consumer)

    def status(self, product_types: tuple[str, ...] = PRODUCT_TYPES, huey_instance: Huey = tasks.huey) -> pd.DataFrame:
        """`huey_instance`: which queue's stored results to check for `failed` -- `tasks.huey`
        (`populate()`'s queue, the default) or `tasks.huey_parallel` (`populate_via_workers()`'s).
        `done` is unaffected either way (always disk-based)."""
        rows = [
            {"product_id": entry.product_id, **{pt: task_state(entry, pt, huey_instance) for pt in product_types}}
            for entry in self
        ]
        return pd.DataFrame(rows, columns=["product_id", *product_types])

    def truncate(
        self,
        entries: "TrnTestEntry | list[TrnTestEntry] | None" = None,
        product_types: tuple[str, ...] = PRODUCT_TYPES,
    ) -> None:
        """Delete already-generated product file(s) (`raster_path`/`sidecar_json_path`) and any
        task-queue lock/error state for `entries` (a single `TrnTestEntry`, a list of them, or
        `None` for every entry in this dataset) across `product_types` -- reverting their
        `task_state` back to `"pending"` so a subsequent `populate()` call regenerates them from
        scratch. For forcing a clean re-run -- e.g. a notebook that always wants fresh output
        reflecting the latest pipeline code rather than silently reusing a stale prior run, unlike
        `populate()`'s own default "skip what's already done" behavior -- without deleting/recreating
        the whole dataset folder.

        Leaves `_work/<edr_product>/` intermediates (DEM/ortho, `.tsai`) alone -- regeneration reuses
        those where still valid (see `TrnTestEntry.dem_ortho_result`'s own resume-from-files check);
        delete `dataset.folder / "_work" / <edr_product>` yourself first if you also want those
        re-fetched from scratch. Clears stored results from *both* `tasks.huey` and
        `tasks.huey_parallel` -- a task's most recent attempt could have gone through either
        `populate()` or `populate_via_workers()`, and this should revert to `pending` for both
        regardless of which one last touched it."""
        target_entries = list(self) if entries is None else entries if isinstance(entries, list) else [entries]
        for entry in target_entries:
            for product_type in product_types:
                image = entry.images_by_type[product_type]
                image.raster_path.unlink(missing_ok=True)
                image.sidecar_json_path.unlink(missing_ok=True)
                _clear_stored_result(self.folder, entry.product_id, product_type, huey_instance=tasks.huey)
                _clear_stored_result(self.folder, entry.product_id, product_type, huey_instance=tasks.huey_parallel)


class TrnTestImage(abc.ABC):
    """One product type of one entry. Owns the genuinely shared logic ONCE; subclasses supply only
    the small type-specific pieces below -- this is the actual code-reuse point behind this design,
    not just a naming convenience."""

    def __init__(self, entry: TrnTestEntry):
        self.entry = entry

    @property
    @abc.abstractmethod
    def raster_path(self) -> Path: ...

    @property
    @abc.abstractmethod
    def sidecar_json_path(self) -> Path: ...

    @property
    @abc.abstractmethod
    def rotation_k(self) -> int: ...

    @property
    @abc.abstractmethod
    def width_km(self) -> float: ...

    @property
    @abc.abstractmethod
    def height_km(self) -> float: ...

    @property
    @abc.abstractmethod
    def footprint_lonlat_deg(self) -> dict: ...

    @property
    @abc.abstractmethod
    def render_label(self) -> str: ...

    @property
    @abc.abstractmethod
    def tie_point_px_key(self) -> str: ...

    @abc.abstractmethod
    def _generate_impl(self) -> None:
        """Produce + copy `raster_path`/`sidecar_json_path` into place. Only called by `generate()`
        when `exists()` is already false, so implementations don't need their own idempotency check."""

    @abc.abstractmethod
    def _mapprojected_path(self) -> Path:
        """The type-specific mapproject/`cam2map` step `plot_overlay` needs -- not cached on the
        instance, since it's only ever called from `plot_overlay` (display-only, not part of
        `exists()`/the task queue's done/pending state)."""

    def exists(self) -> bool:
        return self.raster_path.exists() and self.sidecar_json_path.exists()

    def generate(self) -> Path:
        if not self.exists():
            self._generate_impl()
        return self.raster_path

    def _require_generated(self) -> None:
        if not self.exists():
            raise FileNotFoundError(
                f"{self.render_label} not generated yet for {self.entry.edr_product} -- "
                "call .generate() or dataset.populate() first"
            )

    def plot_vs_basemap(self, tie_point_results: dict | None = None, title: str | None = None):
        self._require_generated()
        return plotting.plot_render_vs_basemap(
            plotting.read_raster_band(self.raster_path),
            self.rotation_k,
            self.width_km,
            self.height_km,
            self.footprint_lonlat_deg,
            self.entry.dem_ortho_result.ortho,
            title=title or f"{self.render_label} vs. hillshade-based basemap",
            render_label=self.render_label,
            tie_point_results=tie_point_results,
            render_px_key=self.tie_point_px_key,
        )

    def plot_overlay(self, title: str | None = None, layers: list[plotting.OverlayLayer] | None = None):
        """Uses `plotting.plot_overlay_toggle`, not the plain `plotting.plot_overlay`
        docs/dataset-plan.md's own pseudocode names -- the notebook this replaces already switched
        to the auto-blinking-GIF toggle version (see its own docstring) before this class existed,
        and reverting that UX improvement here would be a real regression, not a neutral relocation.
        Returns an `IPython.display.HTML` object -- callers must not add a trailing `;` in a
        notebook cell, same requirement as calling `plot_overlay_toggle` directly.

        `layers` passes straight through to `plotting.plot_overlay_toggle` -- see
        `plotting.OverlayLayer`'s docstring (each layer's geometry must already be in
        `self.entry.dem_ortho_result.ortho`'s own raster CRS and already AOI-filtered; this class does
        no fetch/filter/reprojection of its own, same "consumption only" split as the rest of
        `plotting.py`). Shared by both `TrnTestHillshadeImage` (5B) and `TrnTestCropImage` (6B) with
        no special-casing, same as the rest of this method."""
        self._require_generated()
        return plotting.plot_overlay_toggle(
            self.entry.dem_ortho_result.ortho,
            self._mapprojected_path(),
            title=title or f"{self.render_label} over basemap",
            layers=layers,
        )


class TrnTestCropImage(TrnTestImage):
    """The real, ISIS-processed WAC crop -- `crop/<edr_product>_crop.cub` + its accurately-scoped
    (but not reprojection-reliable -- see `isis_wac.run_isd_generate_for_crop`) ISD sidecar."""

    @property
    def raster_path(self) -> Path:
        return self.entry.dataset_folder / "crop" / f"{self.entry.edr_product}_crop.cub"

    @property
    def sidecar_json_path(self) -> Path:
        return self.entry.dataset_folder / "crop" / f"{self.entry.edr_product}_crop.json"

    @property
    def rotation_k(self) -> int:
        return self.entry.rotations.k_crop

    @property
    def width_km(self) -> float:
        return self.entry.camera.cross_track_width_km

    @property
    def height_km(self) -> float:
        return self.entry.camera.n_frames_for_square_crop * self.entry.camera.km_per_frame

    @property
    def footprint_lonlat_deg(self) -> dict:
        return self.entry.crop_footprint

    @property
    def render_label(self) -> str:
        return "Real WAC (ISIS-processed)"

    @property
    def tie_point_px_key(self) -> str:
        return "crop_px"

    def _generate_impl(self) -> None:
        isd = isis_wac.run_isd_generate_for_crop(
            self.entry.crop_result, self.entry.camera, self.entry.stitched.flip, self.entry.per_image_config
        )
        self.raster_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.entry.crop_result.cub_path, self.raster_path)
        shutil.copy(isd.json_path, self.sidecar_json_path)

    def _mapprojected_path(self) -> Path:
        # Operates on the scratch-dir crop_result, not raster_path, so cam2map's own intermediates
        # (the .ortho.map PVL file, the intermediate .cub) don't spill into crop/.
        return isis_wac.run_cam2map_for_crop(
            self.entry.crop_result, self.entry.dem_ortho_result, self.entry.per_image_config
        )


class TrnTestHillshadeImage(TrnTestImage):
    """The synthetic `sat_sim` render -- `hillshade/<edr_product>_hillshade.tif` + its CSM/ISD
    sidecar. "Hillshade base map data reprojected using sat_sim" is a literal description of what
    `render.run_sat_sim` already produces (the hillshade gets baked into the ortho *before* sat_sim
    ever runs -- see `lunaserv.despeckle_and_shade_ortho`), so this is a pure relocation, not new
    pipeline logic -- see docs/dataset-plan.md."""

    @property
    def raster_path(self) -> Path:
        return self.entry.dataset_folder / "hillshade" / f"{self.entry.edr_product}_hillshade.tif"

    @property
    def sidecar_json_path(self) -> Path:
        return self.entry.dataset_folder / "hillshade" / f"{self.entry.edr_product}_hillshade.json"

    @property
    def rotation_k(self) -> int:
        return self.entry.rotations.k_synthetic

    @property
    def width_km(self) -> float:
        return self.entry.camera.render_cross_track_km

    @property
    def height_km(self) -> float:
        return self.entry.camera.render_along_track_km  # not necessarily == width_km -- see
        # camera.solve_corrected_fov's docstring for why the corrected FOV isn't exactly square

    @property
    def footprint_lonlat_deg(self) -> dict:
        return self.entry.camera.footprint_lonlat_deg

    @property
    def render_label(self) -> str:
        return "Synthetic (sat_sim, SPICE-posed)"

    @property
    def tie_point_px_key(self) -> str:
        return "synthetic_px"

    def _generate_impl(self) -> None:
        render_result = render.run_sat_sim(self.entry.camera, self.entry.dem_ortho_result, self.entry.per_image_config)
        self.raster_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(render_result.rendered_tif, self.raster_path)
        shutil.copy(render_result.csm_json, self.sidecar_json_path)

    def _mapprojected_path(self) -> Path:
        # _work/, not hillshade/ -- same "don't spill mapproject's own intermediates into the
        # canonical named pair's folder" reasoning as TrnTestCropImage's own override.
        # camera_type="csm" (the default) against self.sidecar_json_path is safe: camera.
        # solve_corrected_fov is isotropic (fu == fv), so cam_gen's CSM Frame conversion of our own
        # .tsai has no anisotropy to lose -- see docs/reproject-fov-investigation.md for the
        # anisotropic version this once was and why it was reverted.
        out_path = self.entry.per_image_config.output_dir / (self.raster_path.stem + "-mapproj.tif")
        return render.run_mapproject_image(
            self.raster_path, self.sidecar_json_path, out_path, self.entry.dem_ortho_result, self.entry.per_image_config
        )


class TrnTestReprojectImage(TrnTestHillshadeImage):
    """The synthetic `sat_sim` render, textured with the real WAC crop's own reflectance
    (`isis_wac.run_cam2map_for_crop`) instead of the Lunaserv/Astropedia basemap --
    `reproject/<edr_product>_reproject.tif` + its CSM/ISD sidecar. The user's own framing: "use
    `sat_sim` but for input data use the RDR of our WAC crop essentially."

    Subclasses `TrnTestHillshadeImage`, not `TrnTestImage` directly, since it goes through the exact
    same sat_sim-render-then-mapproject shape (per docs/dataset-plan.md's own note) -- only the
    `--ortho` texture source differs, so `raster_path`/`sidecar_json_path`/`render_label`/
    `_generate_impl` are the only overrides needed; `width_km`/`height_km`/`footprint_lonlat_deg`/
    `rotation_k`/`tie_point_px_key`/`_mapprojected_path` are all inherited unchanged (dynamic
    dispatch already picks up this class's own `raster_path`/`sidecar_json_path` inside the
    inherited `_mapprojected_path`).

    Uses `self.entry.camera` -- the exact same `Camera` `hillshade` renders with, not a separate one
    -- so the two are byte-identical in pose *and* FOV (`camera.build_camera()`'s FOV correction is
    applied once, shared by every product type that renders through it -- see
    `solve_corrected_fov`'s docstring), deliberately, for pixel-grid-identical comparison between
    them later (e.g. SSIM/LPIPS/diff scoring) -- see docs/reproject-fov-investigation.md. `crop` (the
    real image, this class's own texture source) is unaffected and naturally larger, providing the
    margin `reproject`'s render needs."""

    @property
    def raster_path(self) -> Path:
        return self.entry.dataset_folder / "reproject" / f"{self.entry.edr_product}_reproject.tif"

    @property
    def sidecar_json_path(self) -> Path:
        return self.entry.dataset_folder / "reproject" / f"{self.entry.edr_product}_reproject.json"

    @property
    def render_label(self) -> str:
        return "Synthetic (sat_sim, real-WAC-textured)"

    @functools.cached_property
    def _reproject_dem_ortho(self) -> DemOrthoResult:
        """The real WAC crop's own reflectance, reprojected (`isis_wac.run_cam2map_for_crop`) and
        wrapped as a `DemOrthoResult` sharing `entry.dem_ortho_result`'s own DEM -- only the imagery
        source changes, matching `notebooks/reproject_spike.py`'s validated approach."""
        wac_ortho_path = isis_wac.run_cam2map_for_crop(
            self.entry.crop_result, self.entry.dem_ortho_result, self.entry.per_image_config
        )
        return lunaserv.result_from_files(wac_ortho_path, self.entry.dem_ortho_result.dem)

    def _generate_impl(self) -> None:
        render_result = render.run_sat_sim(self.entry.camera, self._reproject_dem_ortho, self.entry.per_image_config)
        self.raster_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(render_result.rendered_tif, self.raster_path)
        shutil.copy(render_result.csm_json, self.sidecar_json_path)


# -- Task queue: backed by trntest.tasks's huey instances, no filesystem lock/error files of our
# own anymore -- task list is always manifest rows x implemented product types; `done` is still
# just `image.exists()`, `failed` is whatever the given huey instance's own sqlite-backed result
# store says for that task's deterministic id (see trntest.tasks.task_id). See that module's
# docstring and docs/dataset-plan.md's "Task queue" section for the full design and why there's no
# more `in_progress` state or manual crash-recovery step (a killed process just leaves nothing
# behind to clean up -- the next populate*() call re-enqueues based on disk state alone).


def task_state(entry: TrnTestEntry, product_type: str, huey_instance: Huey = tasks.huey) -> str:
    """`done` (checked first, so a manually-fixed-up product file always wins regardless of any
    stored huey result) | `failed` | `pending`. `huey_instance`: `tasks.huey` (the default,
    `populate()`'s queue) or `tasks.huey_parallel` (`populate_via_workers()`'s) -- the two queues
    are independent, so a task's `failed` state under one is invisible under the other."""
    if entry.images_by_type[product_type].exists():
        return "done"
    tid = tasks.task_id(str(entry.dataset_folder), entry.product_id, product_type)
    try:
        huey_instance.result(tid, preserve=True)
    except TaskException:
        return "failed"
    return "pending"


def _clear_stored_result(dataset_folder: Path, product_id: str, product_type: str, huey_instance: Huey) -> None:
    """Pops (discards) a task's stored result from `huey_instance`, if any, so it's no longer
    reported `failed` there -- used by `retry_failed=True` and `truncate()`. No-op if the task never
    ran (on this instance) or was already cleared."""
    tid = tasks.task_id(str(dataset_folder), product_id, product_type)
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
    """Shared by `populate()`/`populate_via_workers()`: enqueues every still-`pending` task (dataset
    entries x `product_types`) into `huey_instance` via `task_fn`, stopping after `limit` distinct
    entries with genuinely new pending work (same counting rule both methods' own docstrings
    describe). Returns the enqueued `Result` handles without waiting on any of them -- that's the
    caller's own job, since `populate()` and `populate_via_workers()` want to wait differently (the
    former inherently already has, by the time this returns -- see its own comment; the latter only
    after its consumer subprocess is up)."""
    results = []
    entries_done = 0
    for entry in dataset_obj:
        if limit is not None and entries_done >= limit:
            break
        touched = False
        for product_type in product_types:
            if task_state(entry, product_type, huey_instance) != "pending":
                continue
            touched = True
            image = entry.images_by_type[product_type]
            task = task_fn.s(image)
            task.id = tasks.task_id(str(dataset_obj.folder), entry.product_id, product_type)
            results.append(huey_instance.enqueue(task))
        if touched:
            entries_done += 1
    return results


def _await_result(result: Result) -> None:
    """`preserve=True`: a plain `.get()` pops the stored result on read, which would erase a
    failure's record before `task_state()` ever gets a chance to report it -- confirmed empirically.
    Successes stay preserved too (harmless; `task_state()` never queries huey for the `done` case,
    disk existence wins first). `TaskException` is caught, not raised -- one bad task shouldn't abort
    the whole batch; a batch of real network/ISIS calls is expected to have occasional real
    failures."""
    try:
        result.get(blocking=True, preserve=True)
    except TaskException:
        pass
