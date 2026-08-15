"""A self-contained, resumable dataset folder: `TrnTestDataSet` (a manifest + typed `crop`/
`hillshade`/`reproject` subfolders), `TrnTestEntry` (one manifest row's worth of shared, cached,
expensive-to-derive state), and `TrnTestImage` (one product type of one entry -- `entry.crop`/
`entry.hillshade` -- owning the genuinely shared generate/plot logic once, with small per-type
subclasses). Replaces `dataset.generate_dataset()`'s flat, all-at-once output layout with something
that can be populated incrementally/resumably, including across separate `docker compose run`
workers via the filesystem-based task queue at the bottom of this module. See docs/dataset-plan.md
for the full design this implements -- start there before changing anything here.

First-pass scope is `crop` + `hillshade` generation only; `reproject` is reserved (folder exists,
no generation code targets it yet) -- see docs/dataset-plan.md.
"""

import abc
import functools
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pandas as pd

from trntest import camera as camera_module
from trntest import dataset, isis_wac, lunaserv, orientation, plotting, render, tie_points
from trntest.camera import Camera, FrameTiming
from trntest.config import TrntestConfig, load_config
from trntest.lunaserv import LunaservResult
from trntest.orientation import DisplayRotations

PRODUCT_TYPES = ("crop", "hillshade")


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
    def lunaserv_result(self) -> LunaservResult:
        """Resumes from a prior `generate()` run's own DEM/ortho files (`lunaserv.result_from_files`,
        pure IO) when they already exist on disk, instead of re-fetching from Lunaserv/Astropedia --
        the real resumability win `dataset.populate()`'s second-run-near-instant behavior depends
        on, since a fresh fetch is by far the most expensive part of generating either product type."""
        ortho_path = self.per_image_config.output_dir / "ortho_shaded.tif"
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

    @property
    def images_by_type(self) -> dict[str, "TrnTestImage"]:
        return {"crop": self.crop, "hillshade": self.hillshade}


class TrnTestDataSet:
    """A self-contained dataset folder: `manifest.csv` (the same `dataset.DATASET_COLUMNS` shape
    `dataset.write_manifest`/`read_manifest` already use) plus `crop`/`hillshade`/`reproject`/
    `_work`/`.locks` subfolders. Iterating/indexing yields `TrnTestEntry` objects; `populate()`
    drives the filesystem task queue at the bottom of this module until nothing's left claimable."""

    def __init__(self, folder: Path | str, images: pd.DataFrame, config: TrntestConfig):
        self.folder = Path(folder)
        self.images = images.reset_index(drop=True)
        self.config = config

    @classmethod
    def create(cls, folder: Path | str, images: pd.DataFrame, config: TrntestConfig | None = None) -> "TrnTestDataSet":
        """Idempotent: (re)writes `manifest.csv` from `images`, ensures `crop`/`hillshade`/
        `reproject`/`_work`/`.locks` exist. Never touches already-generated product files -- those
        live under `crop`/`hillshade`, untouched by this call."""
        config = config or load_config()
        folder = Path(folder)
        for sub in ("crop", "hillshade", "reproject", "_work", ".locks"):
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
        """Drives the task queue (manifest rows x `product_types`) sequentially, entry by entry:
        for each entry, `claim_task -> image.generate() -> mark_done/mark_failed` for every still-
        `pending` product type. Stays sequential in-process on purpose: `claim_task` is already safe
        across separate OS processes with zero extra code (multiple `docker compose run` invocations
        against the same folder is the real "workers" story here), consistent with this project's
        existing rule that SPICE/spiceypy state is process-global and unsafe across concurrent calls
        within one process -- see docs/dataset-plan.md's "Task queue" section. One row's failure
        doesn't stop the rest (`mark_failed`, not raised) -- a batch of real network/ISIS calls is
        expected to have occasional real failures.

        `limit`, if given, stops this call after it has done genuinely new work (claimed at least
        one task) on `limit` distinct entries -- an entry this call finds already fully done, fully
        in-progress (claimed by another worker), or fully failed doesn't claim anything and so
        doesn't count against it. Meant for splitting a large dataset's population across multiple
        separate worker invocations: run `populate(limit=N)` repeatedly (from this process or a
        fresh one against the same folder) until `status()` shows nothing `pending` -- each call
        picks up wherever the last one left off, via the same on-disk task-queue state."""
        if retry_failed:
            for entry in self:
                for product_type in product_types:
                    if task_state(entry, product_type) == "failed":
                        _error_path(self.folder, entry.product_id, product_type).unlink(missing_ok=True)

        entries_done = 0
        for entry in self:
            if limit is not None and entries_done >= limit:
                return
            touched = False
            for product_type in product_types:
                if task_state(entry, product_type) != "pending":
                    continue
                if not claim_task(self, entry.product_id, product_type):
                    continue  # another worker claimed it first
                touched = True
                image = entry.images_by_type[product_type]
                try:
                    image.generate()
                except Exception as exc:  # noqa: BLE001 -- one bad task shouldn't abort the whole batch
                    mark_failed(self, entry.product_id, product_type, exc)
                else:
                    mark_done(self, entry.product_id, product_type)
            if touched:
                entries_done += 1

    def status(self, product_types: tuple[str, ...] = PRODUCT_TYPES) -> pd.DataFrame:
        rows = [
            {"product_id": entry.product_id, **{pt: task_state(entry, pt) for pt in product_types}} for entry in self
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
        those where still valid (see `TrnTestEntry.lunaserv_result`'s own resume-from-files check);
        delete `dataset.folder / "_work" / <edr_product>` yourself first if you also want those
        re-fetched from scratch."""
        target_entries = list(self) if entries is None else entries if isinstance(entries, list) else [entries]
        for entry in target_entries:
            for product_type in product_types:
                image = entry.images_by_type[product_type]
                image.raster_path.unlink(missing_ok=True)
                image.sidecar_json_path.unlink(missing_ok=True)
                clear_lock(self, entry.product_id, product_type)
                _error_path(self.folder, entry.product_id, product_type).unlink(missing_ok=True)


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
            self.entry.lunaserv_result.ortho,
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
        `self.entry.lunaserv_result.ortho`'s own raster CRS and already AOI-filtered; this class does
        no fetch/filter/reprojection of its own, same "consumption only" split as the rest of
        `plotting.py`). Shared by both `TrnTestHillshadeImage` (5B) and `TrnTestCropImage` (6B) with
        no special-casing, same as the rest of this method."""
        self._require_generated()
        return plotting.plot_overlay_toggle(
            self.entry.lunaserv_result.ortho,
            self._mapprojected_path(),
            title=title or f"{self.render_label} (mapprojected) over hillshade-based basemap",
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
            self.entry.crop_result, self.entry.lunaserv_result, self.entry.per_image_config
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
        return self.entry.camera.cross_track_width_km

    @property
    def height_km(self) -> float:
        return self.entry.camera.cross_track_width_km  # square by construction

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
        render_result = render.run_sat_sim(self.entry.camera, self.entry.lunaserv_result, self.entry.per_image_config)
        self.raster_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(render_result.rendered_tif, self.raster_path)
        shutil.copy(render_result.csm_json, self.sidecar_json_path)

    def _mapprojected_path(self) -> Path:
        # _work/, not hillshade/ -- same "don't spill mapproject's own intermediates into the
        # canonical named pair's folder" reasoning as TrnTestCropImage's own override.
        out_path = self.entry.per_image_config.output_dir / (self.raster_path.stem + "-mapproj.tif")
        return render.run_mapproject_image(
            self.raster_path, self.sidecar_json_path, out_path, self.entry.lunaserv_result, self.entry.per_image_config
        )


# -- Task queue: filesystem-only, no persisted job DB -- task list is always manifest rows x
# implemented product types, state always derived from what's actually on disk (dataset-plan.md's
# "Task queue" section). `.locks/<product_id>_<product_type>.lock`/`.error` are the only new files
# this introduces; `done` is just `image.exists()`, no separate bookkeeping needed for it.


def _lock_path(dataset_folder: Path, product_id: str, product_type: str) -> Path:
    return dataset_folder / ".locks" / f"{product_id}_{product_type}.lock"


def _error_path(dataset_folder: Path, product_id: str, product_type: str) -> Path:
    return dataset_folder / ".locks" / f"{product_id}_{product_type}.error"


def task_state(entry: TrnTestEntry, product_type: str) -> str:
    """`done` (checked first, so a manually-fixed-up product file always wins regardless of
    leftover lock/error bookkeeping) | `failed` | `in_progress` | `pending`."""
    if entry.images_by_type[product_type].exists():
        return "done"
    if _error_path(entry.dataset_folder, entry.product_id, product_type).exists():
        return "failed"
    if _lock_path(entry.dataset_folder, entry.product_id, product_type).exists():
        return "in_progress"
    return "pending"


def claim_task(dataset_obj: TrnTestDataSet, product_id: str, product_type: str) -> bool:
    """Atomically claims one task by creating its lock file (`os.O_CREAT | os.O_EXCL` -- the
    filesystem itself arbitrates a race between separate processes; only one caller ever sees this
    return `True` for a given task). Returns `False` if already claimed (by this or another
    worker)."""
    lock_path = _lock_path(dataset_obj.folder, product_id, product_type)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def mark_done(dataset_obj: TrnTestDataSet, product_id: str, product_type: str) -> None:
    _lock_path(dataset_obj.folder, product_id, product_type).unlink(missing_ok=True)
    _error_path(dataset_obj.folder, product_id, product_type).unlink(missing_ok=True)


def mark_failed(dataset_obj: TrnTestDataSet, product_id: str, product_type: str, exc: Exception) -> None:
    _error_path(dataset_obj.folder, product_id, product_type).write_text(str(exc))
    _lock_path(dataset_obj.folder, product_id, product_type).unlink(missing_ok=True)


def claim_next_task(
    dataset_obj: TrnTestDataSet, product_types: tuple[str, ...] = PRODUCT_TYPES
) -> tuple[str, str] | None:
    """First `(product_id, product_type)` still `pending`, in manifest x `product_types` order, that
    this call successfully claims -- skips (rather than blocks on) a task another worker claims
    first between this call's own state check and its claim attempt."""
    for entry in dataset_obj:
        for product_type in product_types:
            if task_state(entry, product_type) != "pending":
                continue
            if claim_task(dataset_obj, entry.product_id, product_type):
                return entry.product_id, product_type
    return None


def clear_lock(dataset_obj: TrnTestDataSet, product_id: str, product_type: str) -> None:
    """Manual crash recovery: a worker that died mid-task leaves a `.lock` file with nothing to ever
    clear it (`in_progress` forever) -- remove it by hand once you've confirmed no worker actually
    still holds it, and the task reverts to `pending`."""
    _lock_path(dataset_obj.folder, product_id, product_type).unlink(missing_ok=True)
