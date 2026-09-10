"""A self-contained, resumable dataset folder: `TrnTestDataSet` (a manifest + typed `crop`/
`hillshade`/`reproject`/`reports` subfolders) and `TrnTestEntry` (one manifest row's shared, cached
state, including `entry.hillshade`/`entry.primary_image` -- each a `trn_products.py` product
instance). See `trn_products.py`'s own docstring for the product-type class hierarchy
(`TrnTestProduct`/`TrnTestImage`/etc.) those properties construct.

`TrnTestEntry` is an abstract base with two concrete kinds, both constructed by
`TrnTestDataSet.__getitem__` based on the dataset's own `entry_kind`:

- `TrnTestEntryEdr` (`entry_kind="edr"`, the default): built from a real WAC EDR, today's original
  full-featured behavior -- `crop`/`hillshade`/`reproject`/`report`/`gallery` all supported.
- `TrnTestEntrySpice` (`entry_kind="spice"`): posed purely from SPICE trajectory data at an
  arbitrary time, no EDR at all -- a proof of concept supporting `hillshade`/`report`/`gallery`, not
  `crop`/`reproject` (those need a real EDR's own pixel data). See that class's own docstring.

Each entry has a `primary_generator` (a key into its own `images_by_type`) and a derived
`primary_image` property -- the "representative" product other code (`report.py`'s
`primary_overlay`/`primary_zoom_blink`, `TrnTestReport`/`TrnTestGalleryThumb`) displays instead of
hardcoding a specific generator name, so the same code works for either entry kind.

**Only one `populate()` call should run against a given dataset folder at a time** -- for
multi-worker parallel population, use `populate_via_workers()` instead.

`PRODUCT_TYPES` (`("crop", "hillshade", "report", "gallery")`) and `SPICE_PRODUCT_TYPES`
(`("hillshade", "report", "gallery")`) are `TrnTestDataSet.default_product_types`'s two possible
values, per `entry_kind` -- `populate()`/`status()`/etc. fall back to it when not given
`product_types` explicitly. `reproject` is implemented but opt-in for `entry_kind="edr"` (pass
`product_types=(..., "reproject")` explicitly); it doesn't exist at all for `entry_kind="spice"`.
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
#
# gallery (TrnTestGalleryThumb, see src/trntest/trn_products.py) is default-on for the same reason
# as report: cheap (a couple of matplotlib renders, no new SPICE/ISIS/sat_sim of its own) and
# self-ensures its own entry.reproject dependency the same way. See docs/report-generation.md's
# "Gallery" section.

import abc
import dataclasses
import functools
import json
import shutil
import typing
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import spiceypy as spice
from huey import Huey
from huey.api import Result, TaskWrapper
from huey.exceptions import ResultTimeout, TaskException

from trntest import camera as camera_module
from trntest import (
    candidate_window,
    dem_ortho,
    hapke,
    health_monitor,
    isis_wac,
    orientation,
    spice_kernels,
    tasks,
    tie_points,
    trn_products,
)
from trntest.camera import Camera, FrameTiming
from trntest.config import TrntestConfig, load_config
from trntest.dem_ortho import DemOrthoResult
from trntest.orientation import DisplayRotations

PRODUCT_TYPES = ("crop", "hillshade", "report", "gallery")  # "reproject" is implemented
# (TrnTestReprojectImage) but opt-in only -- pass product_types=(..., "reproject") explicitly; see
# module docstring. `TrnTestDataSet`'s own default for `entry_kind="edr"` (see `default_product_types`).

SPICE_PRODUCT_TYPES = ("hillshade", "report", "gallery")  # `TrnTestDataSet`'s own default for
# `entry_kind="spice"` -- crop/reproject need a real EDR that a SPICE-only dataset doesn't have (see
# `TrnTestEntrySpice`'s own docstring); report/gallery work for any entry kind.

SPICE_DATASET_COLUMNS = ["product_id", "utc_time"]  # minimal manifest schema for entry_kind="spice"
# -- `utc_time` is parsed back to a real `datetime`/`Timestamp` by `candidate_window.read_manifest`'s
# own `date_columns` parameter, the same way EDR manifests parse `start_time`/`stop_time`.

_DATASET_META_FILENAME = "dataset_meta.json"  # {"entry_kind", "primary_generator"} -- see
# `TrnTestDataSet.create`/`open`.
_SPICE_TEMPLATE_TSAI_FILENAME = "camera_template.tsai"  # entry_kind="spice"'s shared intrinsics
# template, copied in by `create()` -- see `TrnTestEntrySpice`/`camera.build_spice_camera`.


class TrnTestEntry(abc.ABC):
    """Abstract base for one manifest row's shared, cached, expensive-to-derive state, reused by
    every product-type image built from it. Two concrete kinds -- `TrnTestEntryEdr` (built from a
    real WAC EDR, today's original and still primary behavior) and `TrnTestEntrySpice` (SPICE-only,
    no EDR involved at all) -- share this base's `dem_ortho_result`/`hillshade`/`primary_image`
    machinery but differ in how `camera`/`per_image_config` are derived and which product types
    `images_by_type` exposes. `TrnTestDataSet.__getitem__` constructs the right one based on its own
    `entry_kind`."""

    # functools.cached_property throughout, so each dependency is fetched/computed at most once no
    # matter how many of an entry's own product accessors touch it.

    def __init__(
        self, row: pd.Series, dataset_folder: Path, config: TrntestConfig, primary_generator: str = "reproject"
    ):
        self.row = row
        self.dataset_folder = Path(dataset_folder)
        self.config = config
        self.primary_generator = primary_generator  # a key into images_by_type -- see primary_image

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

    @property
    @abc.abstractmethod
    def identifier(self) -> str:
        """Stable per-entry string used for on-disk file naming (`raster_path`/`sidecar_json_path`
        in `trn_products.py`, `log_dir` below) -- `edr_product` for `TrnTestEntryEdr` (keeping every
        existing on-disk filename byte-identical), a timestamp for `TrnTestEntrySpice` (which has no
        EDR product id to use)."""

    @property
    @abc.abstractmethod
    def tsai_path(self) -> Path:
        """Deterministic path to this entry's `.tsai` Pinhole camera file (see
        `camera.write_tsai`) -- computable without building `camera` itself, so
        `TrnTestDataSet.write_entry_poses()` can check for/read an already-written file without
        forcing new work (real ISIS work, for `TrnTestEntryEdr`) for an as-yet-unpopulated entry."""

    @property
    @abc.abstractmethod
    def camera_et(self) -> float:
        """This entry's camera pose epoch -- SPICE ET (TDB seconds past the J2000 epoch) -- computed
        cheaply and ISIS-free, unlike the full `camera` cached_property (which for
        `TrnTestEntryEdr` can trigger a real ISIS boresight-refine pass). Matches `camera.et`
        exactly. Used by `TrnTestDataSet.write_entry_poses()` for its ROS-`header` `stamp`."""

    @functools.cached_property
    @abc.abstractmethod
    def per_image_config(self) -> TrntestConfig: ...

    @functools.cached_property
    @abc.abstractmethod
    def camera(self) -> Camera: ...

    @functools.cached_property
    @abc.abstractmethod
    def rotations(self) -> DisplayRotations:
        """North-up display rotation (see `orientation.py`'s module docstring). Both concrete kinds
        implement this -- `TrnTestEntryEdr` via `orientation.compute_display_rotations` (both the
        synthetic and crop halves), `TrnTestEntrySpice` via `compute_synthetic_display_rotation`
        alone (placeholder `k_crop`/`dev_crop_deg` -- no crop exists to compute a real one for)."""

    @functools.cached_property
    @abc.abstractmethod
    def lightweight_footprint_lonlat_deg(self) -> dict[str, tuple[float, float] | None]:
        """A cheap approximation of this entry's own FOV footprint, for `overview_map.
        plot_overview_map` -- never forces a full ISIS pipeline run for a not-yet-populated
        `TrnTestEntryEdr` entry (see that class's own implementation and `camera.
        lightweight_footprint_lonlat_deg`'s docstring for why); for `TrnTestEntrySpice`, `self.camera`
        is already this cheap (no ISIS involved at all), so that kind just returns
        `self.camera.footprint_lonlat_deg` directly."""

    @property
    def _dem_extra_footprint(self) -> dict | None:
        """Extra footprint corners to union into `dem_ortho_result`'s own fetch AOI, if any.
        `None` here; overridden by `TrnTestEntryEdr` to return its own WAC crop footprint (the
        fetched DEM/ortho must cover both the synthetic camera's FOV and the real crop's own extent)
        -- a `TrnTestEntrySpice` has no crop to cover, so `None` is exactly right for it too, not
        just a placeholder."""
        return None

    @functools.cached_property
    def dem_ortho_result(self) -> DemOrthoResult:
        """The DEM/ortho pair for this entry -- resumed from a prior `generate()` run's own files
        on disk if present, else fetched fresh from Lunaserv/Astropedia."""
        # The resumability win `dataset.populate()`'s second-run-near-instant behavior depends on,
        # since a fresh fetch is by far the most expensive part of generating either product type.
        # Looks for `hapke.DEFAULT_HAPKE_SHADING`/`DEFAULT_ALONG_TRACK_CORRECTION`/
        # `DEFAULT_REAL_HAPKE_PARAMS`/`DEFAULT_ORTHO_SOURCE`'s own filename specifically
        # (`ortho_shaded_filename`), and this entry's own `_dem_extra_footprint`'s specific
        # `dem_filled_filename` -- rather than either's hardcoded/bare name, so this can never
        # resume a stale *other*-mode ortho, or a DEM fetched for a *different* footprint, left over
        # from before a default changed or from a one-off non-default call elsewhere.
        # `fetch_dem_and_ortho` below picks up the exact same defaults/footprint itself.
        ortho_path = self.per_image_config.output_dir / dem_ortho.ortho_shaded_filename(
            hapke.DEFAULT_HAPKE_SHADING,
            hapke.DEFAULT_ALONG_TRACK_CORRECTION,
            hapke.DEFAULT_REAL_HAPKE_PARAMS,
            dem_ortho.DEFAULT_ORTHO_SOURCE,
        )
        dem_path = self.per_image_config.output_dir / dem_ortho.dem_filled_filename(self._dem_extra_footprint)
        if ortho_path.exists() and dem_path.exists():
            return dem_ortho.result_from_files(ortho_path, dem_path)
        return dem_ortho.fetch_dem_and_ortho(
            self.camera, self.per_image_config, extra_footprint_lonlat_deg=self._dem_extra_footprint
        )

    @functools.cached_property
    def hillshade(self) -> trn_products.TrnTestHillshadeImage:
        return trn_products.TrnTestHillshadeImage(self)

    @functools.cached_property
    def report(self) -> trn_products.TrnTestReport:
        return trn_products.TrnTestReport(self)

    @functools.cached_property
    def gallery_thumb(self) -> trn_products.TrnTestGalleryThumb:
        return trn_products.TrnTestGalleryThumb(self)

    @property
    def primary_image(self) -> trn_products.TrnTestImage:
        """This entry's own designated "representative" product -- `images_by_type[
        self.primary_generator]`. `"reproject"` for a `TrnTestEntryEdr` (the fullest pipeline: real
        acquisition geometry, real WAC texture), `"hillshade"` for a `TrnTestEntrySpice` (the only
        generator that exists there -- see that class's own docstring). `report.py`'s
        `primary_overlay`/`primary_zoom_blink` and `trn_products.TrnTestReport`/`TrnTestGalleryThumb`
        all go through this instead of hardcoding a generator name, so they work unmodified for
        either kind.

        `primary_generator` is expected to always name one of the raster/`TrnTestImage` product
        types (`"crop"`/`"hillshade"`/`"reproject"`), never `"report"`/`"gallery"` -- both
        `TrnTestDataSet.create`'s own defaults and this class's own `images_by_type` implementations
        maintain that; not re-checked here.
        """
        return typing.cast("trn_products.TrnTestImage", self.images_by_type[self.primary_generator])

    @property
    @abc.abstractmethod
    def images_by_type(self) -> dict[str, trn_products.TrnTestProduct]: ...

    @property
    def log_dir(self) -> Path:
        """This entry's captured-log folder -- `<dataset_folder>/logs/<identifier>/`, holding
        whichever generator logs (`log_path`) have actually been captured so far. Only created
        (`_capture_generator_log`'s own `mkdir`) once at least one log has been written, so
        `.is_dir()` doubles as "has anything been captured for this entry yet" -- `report.py`'s
        overview table/summary link to this folder as a whole (one link, not one per generator) once
        it exists, rather than enumerating individual `<product_type>_log.txt` files themselves."""
        return self.dataset_folder / "logs" / self.identifier

    def log_path(self, product_type: str) -> Path:
        """Where `tasks._generate_entry` captures this entry/product_type's console output
        (stdout/stderr, plus a traceback on failure). Only written when that product type is
        actually generated -- a no-op `generate()` call, because it already exists, never touches
        this file."""
        # ".txt", not ".log": a plain `python3 -m http.server` (what serves this folder, see
        # docs/report-generation.md's "Viewing reports" section) doesn't know the ".log" extension
        # and would otherwise serve it as application/octet-stream, which browsers download instead
        # of displaying; ".txt" is a real stdlib-recognized mimetypes extension.
        return self.log_dir / f"{product_type}_log.txt"


class TrnTestEntryEdr(TrnTestEntry):
    """A `TrnTestEntry` built from a real WAC EDR -- today's original, full-featured behavior: reads
    the EDR's own frame timing, poses the camera via `camera.build_camera` (real ISIS-refined
    boresight re-aim against a real crop), and supports all five product types (`crop`/`hillshade`/
    `reproject`/`report`/`gallery`)."""

    @property
    def edr_product(self) -> str:
        return self.row["edr_product"]

    @property
    def identifier(self) -> str:
        return self.edr_product

    @functools.cached_property
    def per_image_config(self) -> TrntestConfig:
        return candidate_window._per_image_config(
            self.row, self.config, self.dataset_folder / "_work" / self.edr_product
        )

    @functools.cached_property
    def frame_timing(self) -> FrameTiming:
        return camera_module.fetch_frame_timing(self.per_image_config)

    @property
    def tsai_path(self) -> Path:
        return self.per_image_config.output_dir / camera_module.edr_tsai_filename(
            self.per_image_config.target_frame_index
        )

    @property
    def camera_et(self) -> float:
        crop_info = camera_module.compute_n_frames_for_square_crop(
            self.frame_timing, self.per_image_config.target_frame_index, self.per_image_config
        )
        center_frame_index = camera_module.center_frame_index_for_square_crop(
            self.per_image_config.target_frame_index, crop_info
        )
        return camera_module.frame_et(self.frame_timing, center_frame_index)

    @functools.cached_property
    def camera(self) -> Camera:
        return camera_module.build_camera(self.per_image_config, output_tsai_path=self.tsai_path)

    @functools.cached_property
    def stitched(self) -> isis_wac.FramestitchResult:
        """The full stitched WAC cube -- a diagnostic accessor, not on `crop_result`'s own hot path
        (see that property below). Always forces a full ISIS reprocessing pass if this product's
        `_work/<edr_product>/isis/` scratch has been cleaned up (the default, once the crop is
        cached -- see `isis_wac.ensure_crop_for_camera`), since nothing but this accessor still needs
        the stitched cube itself. Real consumers: `notebooks/pose_alignment_spike.py`'s
        `isis_campt.resolve_ground_to_image_model`, which genuinely needs the full (not cropped)
        cube."""
        # Idempotent with the cube self.camera (via build_camera) already produced internally --
        # see isis_wac.run_pipeline's own comment for why re-deriving it here, rather than caching
        # it off camera, is cheap, not duplicated ISIS work -- *when* the stitched cube is still on
        # disk. If it isn't, this is genuinely expensive (full ISIS pipeline + live spiceinit web
        # calls), by design: keep `delete_isis_intermediates=False` while debugging if you need this
        # repeatedly.
        return isis_wac.run_pipeline(self.camera.reverse_crop_along_track, self.frame_timing, self.per_image_config)

    @functools.cached_property
    def crop_result(self) -> isis_wac.CropResult:
        """The crop cube, from its real published home (`cache/wac_crop/<edr_product>_crop.cub`,
        see `isis_wac.cached_crop_path`) -- distinct from `TrnTestCropImage.raster_path`, the
        per-dataset published copy `crop.generate()` makes of it.

        Doesn't route through `self.stitched`: cheap (a cache-path existence check only) whenever
        this product's crop has already been generated at all, by any dataset, not just this one --
        see `isis_wac.ensure_crop_for_camera`.
        """
        return isis_wac.ensure_crop_for_camera(
            self.camera, self.frame_timing, self.camera.reverse_crop_along_track, self.per_image_config
        )

    @functools.cached_property
    def crop_footprint(self) -> dict:
        return tie_points.crop_footprint_corners_for_camera(self.frame_timing, self.camera, self.per_image_config)

    @property
    def _dem_extra_footprint(self) -> dict | None:
        return self.crop_footprint

    @functools.cached_property
    def rotations(self) -> DisplayRotations:
        return orientation.compute_display_rotations(self.camera, self.frame_timing, self.per_image_config)

    @functools.cached_property
    def crop(self) -> trn_products.TrnTestCropImage:
        return trn_products.TrnTestCropImage(self)

    @functools.cached_property
    def reproject(self) -> trn_products.TrnTestReprojectImage:
        return trn_products.TrnTestReprojectImage(self)

    @functools.cached_property
    def lightweight_footprint_lonlat_deg(self) -> dict[str, tuple[float, float] | None]:
        per_image_config = dataclasses.replace(self.per_image_config, wac_ck_source="naif_metakernel")
        return camera_module.lightweight_footprint_lonlat_deg(
            self.frame_timing, per_image_config.target_frame_index, per_image_config
        )

    @property
    def images_by_type(self) -> dict[str, trn_products.TrnTestProduct]:
        return {
            "crop": self.crop,
            "hillshade": self.hillshade,
            "reproject": self.reproject,
            "report": self.report,
            "gallery": self.gallery_thumb,
        }


class TrnTestEntrySpice(TrnTestEntry):
    """A `TrnTestEntry` posed purely from SPICE trajectory data at an arbitrary, SPICE-resolvable
    ephemeris time -- no WAC EDR, no ISIS pipeline, no real acquired image anywhere in its own
    construction. A proof of concept, deliberately narrow: only `hillshade` is supported among the
    raster product types (`images_by_type` has no `crop`/`reproject` -- those fundamentally need a
    real EDR's own pixel data); `report`/`gallery` are supported like any other entry kind, since
    both go through `entry.primary_image` generically. See `camera.build_spice_camera`'s own
    docstring for the pose-accuracy tradeoff this makes by having no real crop to refine the
    boresight re-aim against.

    Constructed with a shared `template_tsai_path` (this dataset's `camera_template.tsai`, see
    `TrnTestDataSet.create(entry_kind="spice", ...)`) -- only its intrinsics (`fu`/`fv`/`cu`/`cv`)
    are reused; each entry still poses its own extrinsics fresh from SPICE at its own `row["utc_time"]`.
    """

    def __init__(
        self,
        row: pd.Series,
        dataset_folder: Path,
        config: TrntestConfig,
        template_tsai_path: Path,
        primary_generator: str = "hillshade",
    ):
        super().__init__(row, dataset_folder, config, primary_generator)
        self.template_tsai_path = Path(template_tsai_path)

    @property
    def identifier(self) -> str:
        return self.row["utc_time"].strftime("%Y%m%dT%H%M%S")

    @functools.cached_property
    def per_image_config(self) -> TrntestConfig:
        # No edr_* overrides -- nothing on this entry's own code path (camera/dem_ortho_result/
        # hillshade rendering) ever reads them, unlike TrnTestEntryEdr's per_image_config.
        return dataclasses.replace(self.config, output_dir=self.dataset_folder / "_work" / self.identifier)

    @property
    def tsai_path(self) -> Path:
        return self.per_image_config.output_dir / f"camera_{self.identifier}.tsai"

    @property
    def camera_et(self) -> float:
        utc_dt = self.row["utc_time"].to_pydatetime()
        spice_kernels.fetch_and_furnish(utc_dt, self.per_image_config)
        return spice.utc2et(utc_dt.strftime("%Y-%m-%dT%H:%M:%S"))

    @functools.cached_property
    def camera(self) -> Camera:
        return camera_module.build_spice_camera(
            self.camera_et, self.template_tsai_path, self.per_image_config, self.tsai_path
        )

    @functools.cached_property
    def rotations(self) -> DisplayRotations:
        k_synthetic, dev_synthetic = orientation.compute_synthetic_display_rotation(self.camera)
        # k_crop/dev_crop_deg are meaningless placeholders here -- no TrnTestCropImage is ever
        # constructed for this entry kind (see images_by_type below), so nothing ever reads them.
        return DisplayRotations(k_synthetic=k_synthetic, dev_synthetic_deg=dev_synthetic, k_crop=0, dev_crop_deg=0.0)

    @functools.cached_property
    def lightweight_footprint_lonlat_deg(self) -> dict[str, tuple[float, float] | None]:
        # self.camera is already SPICE-only/cheap for this kind -- no ISIS involved at all, unlike
        # TrnTestEntryEdr, so there's no forced-pipeline-run cost to avoid by approximating further.
        return self.camera.footprint_lonlat_deg

    @property
    def images_by_type(self) -> dict[str, trn_products.TrnTestProduct]:
        return {"hillshade": self.hillshade, "report": self.report, "gallery": self.gallery_thumb}


class TrnTestDataSet:
    """A self-contained dataset folder: `manifest.csv` plus `crop`/`hillshade`/`reproject`/`_work`
    subfolders. Iterating/indexing yields `TrnTestEntry` objects; `populate()` drives the task
    queue until nothing's left `pending`."""

    # manifest.csv matches candidate_window.DATASET_COLUMNS' shape (candidate_window.write_manifest/read_manifest).
    # Task-queue state itself lives outside the dataset folder -- see trntest.tasks's docstring for
    # why.

    def __init__(
        self,
        folder: Path | str,
        images: pd.DataFrame,
        config: TrntestConfig,
        *,
        entry_kind: str = "edr",
        primary_generator: str | None = None,
    ):
        assert entry_kind in ("edr", "spice"), f"entry_kind={entry_kind!r} must be 'edr' or 'spice'"
        self.folder = Path(folder)
        self.images = images.reset_index(drop=True)
        self.config = config
        self.entry_kind = entry_kind
        # "reproject" (today's implicit choice everywhere report/gallery code used to hardcode
        # entry.reproject) for "edr"; "hillshade" (the only generator entry_kind="spice" actually
        # has) for "spice" -- see TrnTestEntry.primary_image.
        self.primary_generator = primary_generator or ("hillshade" if entry_kind == "spice" else "reproject")

    @property
    def name(self) -> str:
        """The dataset's display name, for page titles/headings -- just the folder's own name; no
        separate stored field, since the folder name already serves as the standard identifier."""
        return self.folder.name

    @property
    def time_span_columns(self) -> tuple[str, str]:
        """Manifest column names spanning this dataset's real time range -- `("start_time",
        "stop_time")` for `entry_kind="edr"`, the same single `"utc_time"` column used as both ends
        for `"spice"` (each row is a single instant, not a span). Used by `overview_map`'s
        midpoint/ground-track calculations so those stay entry-kind-generic rather than hardcoding
        EDR-only column names."""
        return ("start_time", "stop_time") if self.entry_kind == "edr" else ("utc_time", "utc_time")

    @property
    def default_product_types(self) -> tuple[str, ...]:
        """`populate`/`populate_via_workers`/`status`/`write_index`/`truncate`'s own default
        `product_types` when the caller doesn't pass one explicitly -- `PRODUCT_TYPES` for
        `entry_kind="edr"`, `SPICE_PRODUCT_TYPES` for `"spice"` (crop/reproject need a real EDR that
        kind doesn't have)."""
        return PRODUCT_TYPES if self.entry_kind == "edr" else SPICE_PRODUCT_TYPES

    @classmethod
    def create(
        cls,
        folder: Path | str,
        images: pd.DataFrame,
        config: TrntestConfig | None = None,
        *,
        entry_kind: str = "edr",
        primary_generator: str | None = None,
        template_tsai_path: Path | str | None = None,
    ) -> "TrnTestDataSet":
        """Idempotent: (re)writes `manifest.csv` from `images`, ensures `crop`/`hillshade`/
        `reproject`/`reports`/`logs`/`_work` exist. Never touches already-generated product files --
        those live under `crop`/`hillshade`, untouched by this call.

        :param entry_kind: `"edr"` (default, today's original behavior) or `"spice"` (see
            `TrnTestEntrySpice`). Persisted to `dataset_meta.json` so `open()` recovers it.
        :param primary_generator: Which generator `TrnTestEntry.primary_image` resolves to for every
            entry in this dataset -- defaults to `"reproject"` for `"edr"`, `"hillshade"` for
            `"spice"` (its only generator).
        :param template_tsai_path: Required for `entry_kind="spice"`: an existing `.tsai` file (see
            `camera.build_spice_camera`) whose intrinsics every entry in this dataset shares --
            copied into the dataset folder as `camera_template.tsai`.
        """
        config = config or load_config()
        folder = Path(folder)
        for sub in ("crop", "hillshade", "reproject", "reports", "logs", "_work"):
            (folder / sub).mkdir(parents=True, exist_ok=True)
        if entry_kind == "spice":
            assert template_tsai_path is not None, "entry_kind='spice' requires template_tsai_path"
            shutil.copy(template_tsai_path, folder / _SPICE_TEMPLATE_TSAI_FILENAME)
        dataset = cls(folder, images, config, entry_kind=entry_kind, primary_generator=primary_generator)
        (folder / _DATASET_META_FILENAME).write_text(
            json.dumps({"entry_kind": dataset.entry_kind, "primary_generator": dataset.primary_generator})
        )
        candidate_window.write_manifest(images, folder / "manifest.csv")
        return dataset

    @classmethod
    def open(cls, folder: Path | str, config: TrntestConfig | None = None) -> "TrnTestDataSet":
        """Reads `manifest.csv` (plus `dataset_meta.json`, if present) from an existing folder -- no
        `images` needed."""
        config = config or load_config()
        folder = Path(folder)
        meta_path = folder / _DATASET_META_FILENAME
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            entry_kind, primary_generator = meta["entry_kind"], meta["primary_generator"]
        else:
            # Predates entry_kind support entirely -- every such dataset is "edr" (the only kind
            # that existed then), with "reproject" the implicit primary_generator report/gallery
            # code already hardcoded before TrnTestEntry.primary_image existed.
            entry_kind, primary_generator = "edr", "reproject"
        date_columns = ("utc_time",) if entry_kind == "spice" else ("start_time", "stop_time")
        images = candidate_window.read_manifest(folder / "manifest.csv", date_columns=date_columns)
        return cls(folder, images, config, entry_kind=entry_kind, primary_generator=primary_generator)

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
        if self.entry_kind == "spice":
            return TrnTestEntrySpice(
                row,
                self.folder,
                self.config,
                self.folder / _SPICE_TEMPLATE_TSAI_FILENAME,
                primary_generator=self.primary_generator,
            )
        return TrnTestEntryEdr(row, self.folder, self.config, self.primary_generator)

    def populate(
        self,
        product_types: tuple[str, ...] | None = None,
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
        product_types = product_types or self.default_product_types
        if retry_failed:
            skipped_ids = frozenset(self._load_skip_list())
            for entry in self:
                if any(task_state(entry, pt, skipped_ids=skipped_ids) == "failed" for pt in product_types):
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
        product_types: tuple[str, ...] | None = None,
        retry_failed: bool = False,
        limit: int | None = None,
        workers: int = 4,
        write_index: bool = True,
        result_timeout: float | None = 1800.0,
    ) -> None:
        """`populate()`'s multi-worker equivalent: same `product_types`/`retry_failed`/`limit`/
        `write_index` semantics, but runs `workers` worker processes in parallel instead of
        sequentially. Blocks until the whole batch finishes; manages its own consumer subprocess
        for the call's duration, so there's no separate terminal/process to set up first. Also
        starts a `health_monitor.HealthMonitor` for the call's duration, logging progress/ETA/disk/
        memory/CPU to `<folder>/logs/health_monitor_log.txt` every few seconds -- the path is
        printed at startup; `tail -f` it to watch a long run live.

        :param workers: Number of parallel worker processes.
        :param result_timeout: Seconds to wait for one entry's stored result before giving up on it
            and moving to the next -- `None` waits forever. Real-world default (30 min) is generous
            against a slow/cold entry but finite: a task whose result never gets stored (seen live
            under sustained 8-worker load, root-caused as a `pytest` run flushing this same live
            queue out from under the batch -- see `docs/batch-generation.md`'s "Don't run the test
            suite..." section for the actual mechanism) would otherwise block this call forever
            even though every other entry's own work keeps completing fine in the background. A
            timeout here is a safety net for that collision (or any other cause of a missing
            result), not a substitute for avoiding it.
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
        product_types = product_types or self.default_product_types
        if retry_failed:
            skipped_ids = frozenset(self._load_skip_list())
            for entry in self:
                if any(
                    task_state(entry, pt, huey_instance=tasks.huey_parallel, skipped_ids=skipped_ids) == "failed"
                    for pt in product_types
                ):
                    _clear_stored_result(self.folder, entry.product_id, huey_instance=tasks.huey_parallel)

        results = _enqueue_pending(self, product_types, limit, tasks.huey_parallel, tasks.generate_product_parallel)
        if results:
            consumer = tasks.start_consumer(workers)
            # A daemon thread, not a separate process -- tied to this call's own lifetime so it
            # can't be left running if the calling process is killed (unlike the consumer
            # subprocess above -- see this module's own "A killed calling process..." doc note in
            # docs/batch-generation.md). See docs/proposed-tasks/health-monitor.md for the design.
            monitor = health_monitor.HealthMonitor(
                dataset_folder=self.folder,
                huey_instance=tasks.huey_parallel,
                results=results,
                workers=workers,
                consumer_pid=consumer.pid if consumer is not None else None,
            )
            monitor.start()
            # flush=True: stdout is block-buffered, not line-buffered, once it's a pipe rather than
            # a real terminal (true for every `docker compose run` invocation) -- without this, the
            # announcement can sit unflushed until the whole run exits, defeating the point of
            # printing it at startup at all. Confirmed live: this was silently broken until caught.
            print(f"Health monitor: tail -f {monitor.log_path}", flush=True)
            try:
                for result in results:
                    _await_result(result, timeout=result_timeout)
            finally:
                # Stop the monitor (writes one final line) before tearing down the consumer, so
                # that last line's process-tree reads still see a live consumer to inspect.
                monitor.stop()
                tasks.stop_consumer(consumer)
        if write_index:
            self.write_index(product_types)

    def status(self, product_types: tuple[str, ...] | None = None, huey_instance: Huey = tasks.huey) -> pd.DataFrame:
        """Per-entry, per-product-type status: `done`/`skipped`/`failed`/`pending` (see
        `task_state`).

        :param huey_instance: Which queue's stored results to check for `failed` -- `tasks.huey`
            (`populate()`'s queue, the default) or `tasks.huey_parallel`
            (`populate_via_workers()`'s). `done` is unaffected either way (always disk-based).
        """
        product_types = product_types or self.default_product_types
        skipped_ids = frozenset(self._load_skip_list())
        rows = [
            {
                "product_id": entry.product_id,
                **{pt: task_state(entry, pt, huey_instance, skipped_ids) for pt in product_types},
            }
            for entry in self
        ]
        return pd.DataFrame(rows, columns=["product_id", *product_types])

    def write_index(self, product_types: tuple[str, ...] | None = None, write_overview_map: bool = True) -> None:
        """Writes `<folder>/status.csv` (`status()` plus a `problems` column, see
        `report.problem_flags`), `<folder>/reports/overview_table.html` (one row per entry, linking
        to its own `reports/<identifier>/report.html`, alongside the same status/problem info),
        `<folder>/reports/index.html` (a persistent nav bar over a content iframe defaulting to the
        overview table -- see `report.write_index_html`'s own docstring for its design),
        `<folder>/reports/gallery.html` (`report.write_gallery_html` -- a blink-thumbnail table, one
        entry per cell, synchronized across the whole page), and `<folder>/reports/overview_map.png`
        (`overview_map.write_overview_map`) -- covers every entry in the dataset, not just ones
        touched by whatever call (if any) triggered this. Works the same way for either `entry_kind`
        -- both report/gallery generation and the overview map are entry-kind-generic (see
        `TrnTestEntry.lightweight_footprint_lonlat_deg`/`report`/`gallery_thumb`, and
        `time_span_columns` above).

        `status.csv`/`reports/index.html` are cheap/pure-Python (no subprocess); the overview map is
        not -- it builds a real `Camera` (a SPICE pose rebuild) for every entry to get its FOV
        footprint, so this call's cost now scales with dataset size regardless of how many entries
        were actually just populated. Pass `write_overview_map=False` to skip it (e.g. for a large
        dataset's repeated incremental `populate(limit=N)` calls, see `docs/batch-generation.md`) and
        call `overview_map.write_overview_map(self)` directly whenever an up-to-date map is actually
        needed.

        Like `populate()`/`populate_via_workers()`, not safe to run concurrently with itself against
        the same dataset folder (writes shared files).

        Finishes by printing a link to the freshly-written `reports/index.html`
        (`report.print_viewing_url`).
        """
        from trntest import overview_map, report  # noqa: PLC0415 -- circular otherwise (both
        # import TrnTestDataSet/TrnTestEntry from this module)

        product_types = product_types or self.default_product_types
        # mkdir here rather than relying on create() having already run -- some callers (e.g. this
        # project's own tests) construct a TrnTestDataSet directly.
        (self.folder / "reports").mkdir(parents=True, exist_ok=True)
        status_df = self.status(product_types)
        status_df["problems"] = ["; ".join(report.problem_flags(entry)) for entry in self]
        status_df.to_csv(self.folder / "status.csv", index=False)
        report.write_index_html(self, status_df)  # also (re)writes overview_table.html/gallery.html
        # -- see that function's own docstring.
        if write_overview_map:
            overview_map.write_overview_map(self, self.config)
        report.print_viewing_url(self)

    def write_entry_poses(self, entries: "TrnTestEntry | list[TrnTestEntry] | None" = None) -> None:
        """Writes `<folder>/entry_poses.jsonl` -- one JSON Lines record per entry (or just
        `entries`, if given), each describing that entry's 6-DOF camera-frame pose (position +
        quaternion attitude, `MOON_ME`) exactly as recorded in its already-written `.tsai` file,
        loosely modeled on a ROS `geometry_msgs/PoseStamped` message (see
        `entry_poses.ENTRY_POSE_JSON_SCHEMA` for the exact shape/field descriptions). Also writes
        the companion `<folder>/entry_poses.schema.json`.

        Deliberately **not** called automatically by `populate()`/`populate_via_workers()`/
        `write_index()` -- call it explicitly whenever an up-to-date pose file is actually wanted
        (e.g. once after a run finishes), the same way `overview_map.write_overview_map(self)` is
        callable directly instead of only via `write_index()`.

        Reads each entry's pose straight from its `.tsai` file (`camera.read_tsai_pose`) rather
        than rebuilding `entry.camera` -- deliberately, so this stays cheap and ISIS-free
        regardless of dataset size, safe to call any time after a normal population run without
        re-triggering real ISIS work. A naive `for entry in dataset: entry.camera` loop would not
        have this property: for `entry_kind="edr"`, `camera` can force a real ISIS boresight-refine
        pass (see `TrnTestEntryEdr.camera`'s docstring) -- the exact trap `overview_map`'s
        `lightweight_footprint_lonlat_deg` was built to avoid for the FOV footprint, applying here
        too. `entry.camera_et` gives the pose's timestamp the same ISIS-free way (pure SPICE, no
        ISIS call).

        :param entries: A single entry, a list, or `None` (default) for every entry in the dataset
            -- an entry with no `.tsai` yet (never populated) is silently skipped in the output,
            not an error, since this harvests already-computed poses rather than triggering new
            ones.
        """
        from trntest import entry_poses  # noqa: PLC0415 -- circular otherwise (imports TrnTestDataSet
        # /TrnTestEntry from this module, for type hints only)

        entry_poses.write_entry_poses(self, entries)

    def truncate(
        self,
        entries: "TrnTestEntry | list[TrnTestEntry] | None" = None,
        product_types: tuple[str, ...] | None = None,
        invalidate_crop_cache: bool = False,
    ) -> None:
        """Delete already-generated product file(s) (`raster_path`/`sidecar_json_path`), their
        captured log file (`log_path`), and any task-queue result state for `entries` (a single
        `TrnTestEntry`, a list of them, or `None` for every entry in this dataset) across
        `product_types` -- reverting their `task_state` back to `"pending"` so a subsequent
        `populate()` call regenerates them from scratch.

        Leaves `_work/<edr_product>/` intermediates (DEM/ortho, `.tsai`) alone -- regeneration
        reuses those where still valid (see `TrnTestEntry.dem_ortho_result`'s own resume-from-files
        check); delete `dataset.folder / "_work" / <edr_product>` yourself first if you also want
        those re-fetched from scratch.

        :param invalidate_crop_cache: If `"crop"` is in `product_types`, also delete each entry's
            cached crop cube (`cache/wac_crop/<edr_product>_crop.cub`, see
            `isis_wac.cached_crop_path`) -- without this (the default), a subsequent `populate()`
            reuses the already-cached crop (cheap, matching this function's own "leaves `_work/`
            intermediates alone, reuses what's still valid" philosophy for every *other* product
            type) rather than genuinely re-running ISIS. Set `True` only when you actually need to
            force real reprocessing -- e.g. verifying an ISIS-pipeline code change still works from a
            clean slate, not routine report/hillshade re-testing. This cache entry is **shared across
            every dataset pointing at the same `cache_root`**, keyed by `edr_product` alone, not
            scoped to this dataset folder -- invalidating it here can force a *different* dataset (or
            a concurrent worktree agent's own dataset, if it shares `cache_root`) to redo real ISIS
            work the next time it touches that same `edr_product`. See docs/environment.md's
            "Multi-agent worktrees" section.
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
        product_types = product_types or self.default_product_types
        target_entries = list(self) if entries is None else entries if isinstance(entries, list) else [entries]
        for entry in target_entries:
            for product_type in product_types:
                image = entry.images_by_type[product_type]
                image.raster_path.unlink(missing_ok=True)
                image.sidecar_json_path.unlink(missing_ok=True)
                entry.log_path(product_type).unlink(missing_ok=True)  # stale otherwise -- would
                # keep pointing at the truncated attempt's own log until the next populate() call
                # regenerates it
            if invalidate_crop_cache and "crop" in product_types:
                isis_wac.cached_crop_path(entry.per_image_config).unlink(missing_ok=True)
            _clear_stored_result(self.folder, entry.product_id, huey_instance=tasks.huey)
            _clear_stored_result(self.folder, entry.product_id, huey_instance=tasks.huey_parallel)

    def skip(self, entries: "TrnTestEntry | list[TrnTestEntry]", reason: str) -> None:
        """Adds `entries` (a single `TrnTestEntry` or a list) to this dataset's persisted skip
        list (`<folder>/skip_list.csv`, `product_id, reason`). `task_state()` then reports
        `"skipped"` for any of their non-`done` product types instead of `"failed"`/`"pending"`,
        so `populate()`/`populate_via_workers()` never enqueue new work for them and
        `retry_failed=True` never clears their stored result either.

        For a failure already confirmed *reproducible* -- a real bug, not a transient
        network/server blip -- so a dataset-wide `retry_failed=True` sweep stops re-attempting it,
        and wasting a worker slot on it, every single pass until the bug is actually fixed. See
        `docs/batch-generation.md`'s "Retrying failures" section. Overwrites the reason if the
        entry is already on the list.
        """
        target_entries = entries if isinstance(entries, list) else [entries]
        current = self._load_skip_list()
        for entry in target_entries:
            current[entry.product_id] = reason
        self._write_skip_list(current)

    def unskip(self, entries: "TrnTestEntry | list[TrnTestEntry]") -> None:
        """Removes `entries` from this dataset's persisted skip list (see `skip()`) -- e.g. once
        the underlying bug is fixed and it's safe to let `populate()`/`populate_via_workers()`
        attempt them again. No-op for an entry not currently on the list.
        """
        target_entries = entries if isinstance(entries, list) else [entries]
        current = self._load_skip_list()
        for entry in target_entries:
            current.pop(entry.product_id, None)
        self._write_skip_list(current)

    def _load_skip_list(self) -> dict[str, str]:
        """`{product_id: reason}` from `<folder>/skip_list.csv`, or `{}` if it doesn't exist yet
        (the common case -- most datasets never need one)."""
        return _load_skip_list(self.folder)

    def _write_skip_list(self, mapping: dict[str, str]) -> None:
        """Overwrites `<folder>/skip_list.csv` with `mapping`, sorted by `product_id` for a stable
        diff -- or removes the file entirely once `mapping` is empty, so an unused dataset folder
        doesn't grow a permanent empty skip list."""
        # mkdir here rather than relying on create() having already run -- same reasoning as
        # write_index()'s own mkdir, since a caller can construct a TrnTestDataSet directly (e.g.
        # this project's own tests).
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / "skip_list.csv"
        if not mapping:
            path.unlink(missing_ok=True)
            return
        pd.DataFrame(sorted(mapping.items()), columns=["product_id", "reason"]).to_csv(path, index=False)


# -- Task queue: backed by trntest.tasks's huey instances, no filesystem lock/error files of our
# own anymore -- task list is one task per manifest row (entry), each covering every requested
# product type for it (see tasks._generate_entry's own comment for why); `done` is still just
# `image.exists()`, per product type, `failed` is whatever the given huey instance's own
# sqlite-backed result store says for that entry's deterministic id (see trntest.tasks.task_id).
# See that module's docstring for the full design and why there's no
# more `in_progress` state or manual crash-recovery step (a killed process just leaves nothing
# behind to clean up -- the next populate*() call re-enqueues based on disk state alone).


def _load_skip_list(folder: Path) -> dict[str, str]:
    """`{product_id: reason}` from `<folder>/skip_list.csv`, or `{}` if it doesn't exist yet (the
    common case -- most datasets never need one). Shared by `TrnTestDataSet._load_skip_list` and
    `task_state`'s own auto-load default below."""
    path = folder / "skip_list.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return dict(zip(df["product_id"], df["reason"], strict=True))


def task_state(
    entry: TrnTestEntry,
    product_type: str,
    huey_instance: Huey = tasks.huey,
    skipped_ids: frozenset[str] | None = None,
) -> str:
    """This entry/product_type's state: `done`/`skipped`/`failed`/`pending`.

    :param huey_instance: Which queue's stored results to check for `failed` -- `tasks.huey`
        (`populate()`'s queue, the default) or `tasks.huey_parallel` (`populate_via_workers()`'s);
        the two are independent, so a `failed` state under one is invisible under the other.
    :param skipped_ids: Product ids on this dataset's persisted skip list (see
        `TrnTestDataSet.skip()`) -- checked before the stored huey result, so a skip-listed entry
        reports `"skipped"` instead of whatever `"failed"`/`"pending"` it would otherwise show.
        Nothing is lost by this -- `unskip()` makes the underlying state visible again. Defaults
        to `None`, which loads `<entry.dataset_folder>/skip_list.csv` fresh on every call (cheap --
        a small file, same "always hit disk, no caching" pattern as the huey result lookup below)
        so a direct caller can't silently get a stale/wrong answer by forgetting to pass this;
        `status()`/`_enqueue_pending`/the `retry_failed` loops pass an already-loaded set instead,
        purely to avoid re-reading the same tiny file once per entry in a large dataset.
    :returns: `done` if `entry.images_by_type[product_type].exists()` (checked first, so a
        manually-fixed-up product file always wins regardless of any stored huey result or skip
        listing), else `skipped` if `entry.product_id in skipped_ids`, else `failed` or `pending`
        per the stored huey result.
    """
    # The stored huey result this falls back to is keyed per *entry*, not per
    # `(entry, product_type)` (see `tasks._generate_entry`'s own comment for why task granularity
    # is entry-level) -- so if one product type in a task failed while another succeeded, both
    # share the same stored result. This still reports each product type correctly: the
    # succeeded one's `exists()` check above already returns `done` before this fallback is ever
    # reached, and the failed one correctly falls through to it -- imprecise only in attributing a
    # shared `failed` signal to a specific product type when more than one in the same task didn't
    # complete. Skip-list membership is entry-level for the same reason -- `skip()` has no finer
    # resolution to key off of than the task granularity everything else here already uses.
    if entry.images_by_type[product_type].exists():
        return "done"
    if skipped_ids is None:
        skipped_ids = frozenset(_load_skip_list(entry.dataset_folder))
    if entry.product_id in skipped_ids:
        return "skipped"
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
    #
    # A skip-listed entry (see TrnTestDataSet.skip()) reports "skipped" rather than "pending" for
    # each of its own non-done product types (task_state()'s own skipped_ids check), so it's
    # never enqueued here -- printed below instead, so a skip list silently thinning out a batch
    # doesn't get mistaken for e.g. a stalled queue or an already-fully-populated dataset.
    skipped_ids = frozenset(dataset_obj._load_skip_list())
    results = []
    entries_done = 0
    newly_skipped_ids = []
    for entry in dataset_obj:
        if limit is not None and entries_done >= limit:
            break
        types_state = {pt: task_state(entry, pt, huey_instance, skipped_ids) for pt in product_types}
        pending_types = tuple(pt for pt, state in types_state.items() if state == "pending")
        if not pending_types:
            if "skipped" in types_state.values():
                newly_skipped_ids.append(entry.product_id)
            continue
        task = task_fn.s(entry, pending_types)
        task.id = tasks.task_id(str(dataset_obj.folder), entry.product_id)
        results.append(huey_instance.enqueue(task))
        entries_done += 1
    if newly_skipped_ids:
        # flush=True: stdout is block-buffered (not line-buffered) once it's a pipe rather than a
        # real terminal, true for every `docker compose run` invocation -- see this project's other
        # startup-announcement prints for the same reasoning.
        print(
            f"Skipping {len(newly_skipped_ids)} entries due to skip list: {', '.join(newly_skipped_ids)}",
            flush=True,
        )
    return results


def _await_result(result: Result, timeout: float | None = None) -> None:
    """Blocks on `result` (up to `timeout` seconds if given), discarding a `TaskException` so one
    bad task doesn't abort the batch."""
    # `preserve=True`: a plain `.get()` pops the stored result on read, which would erase a
    # failure's record before `task_state()` ever gets a chance to report it -- confirmed
    # empirically. Successes stay preserved too (harmless; `task_state()` never queries huey for
    # the `done` case, disk existence wins first).
    try:
        result.get(blocking=True, timeout=timeout, preserve=True)
    except TaskException:
        pass
    except ResultTimeout:
        # Not force-marked `failed` here: task_state() still reports whatever it already would
        # (usually `pending`, since no result was ever stored) -- honest, since a slow-but-alive
        # worker could still finish this entry later, unlike a real TaskException which is a
        # definitive outcome. See docs/batch-generation.md's "Don't run the test suite..." section
        # for the confirmed way a result can go missing at all; this is a safety net, not a fix for
        # avoiding that collision in the first place.
        print(
            f"WARNING: timed out after {timeout}s waiting for task {result.id}'s stored result -- "
            "moving on to the next entry. Its own work may still complete in the background; "
            "re-run status() later to check.",
            flush=True,
        )
