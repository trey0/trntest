"""Product-type classes for one `TrnTestEntry`: `TrnTestProduct` (`crop`/`hillshade`/`reproject`/
`report`, generated once, on demand, and tracked by the task queue via `exists()`/`generate()`),
`TrnTestImage` (the `TrnTestProduct` subclass adding plot-comparison geometry for the three raster
types), and their four concrete implementations (`TrnTestCropImage`, `TrnTestHillshadeImage`,
`TrnTestReprojectImage`, `TrnTestReport`). Split out of `trn_dataset.py`, which keeps
`TrnTestEntry`/`TrnTestDataSet` -- see that module's own docstring for the dataset-folder/task-queue
side of this split.
"""

from __future__ import annotations

import abc
import functools
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from trntest import dem_ortho, isis_campt, isis_wac, plotting, render
from trntest.dem_ortho import DemOrthoResult

if TYPE_CHECKING:
    # Annotation-only below (never constructed or isinstance-checked here) -- constructing a
    # `TrnTestCropImage`/etc. is `TrnTestEntry`'s own job (its `crop`/`hillshade`/`reproject`/
    # `report` properties), not this module's, so a real top-level import here would recreate the
    # exact cycle that split avoids: `trn_dataset.py` needs this module for real (to construct
    # those instances), so this module can only need `trn_dataset.py` for types.
    from trntest.trn_dataset import TrnTestEntry


class TrnTestProduct(abc.ABC):
    """One product type of one entry (`crop`/`hillshade`/`reproject`/`report`) -- generated once,
    on demand, and tracked by the task queue via `exists()`/`generate()`. `TrnTestImage` (below)
    adds the plot-comparison geometry the three raster types need; `TrnTestReport` doesn't need it
    and subclasses this directly."""

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
    def generator_name(self) -> str:
        """Short generator name (`"crop"`/`"hillshade"`/`"reproject"`/`"report"`) -- `TrnTestImage`
        subclasses also use this as plot titles' own default label, via `plotting.mathtt`."""

    @abc.abstractmethod
    def _generate_impl(self) -> None:
        """Produce + copy `raster_path`/`sidecar_json_path` into place. Only called by `generate()`
        when `exists()` is already false, so implementations don't need their own idempotency check."""

    def exists(self) -> bool:
        return self.raster_path.exists() and self.sidecar_json_path.exists()

    def generate(self) -> Path:
        if not self.exists():
            self._generate_impl()
        return self.raster_path


class TrnTestImage(TrnTestProduct):
    """A `TrnTestProduct` that's also a plottable/comparable raster (`crop`/`hillshade`/
    `reproject`) -- adds the geometry `plot_vs_basemap`/`plot_overlay`/`plot_zoom_blink_over` need."""

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
    def render_label(self) -> str:
        """Longer descriptive form of `generator_name`, for non-plot contexts (e.g.
        `_require_generated`'s error message)."""

    @property
    @abc.abstractmethod
    def tie_point_px_key(self) -> str: ...

    @abc.abstractmethod
    def _mapprojected_path(self) -> Path:
        """The type-specific mapproject/`cam2map` step `plot_overlay` needs."""
        # Not cached on the instance, since it's only ever called from plot_overlay (display-only,
        # not part of exists()/the task queue's done/pending state).

    def _require_generated(self) -> None:
        if not self.exists():
            raise FileNotFoundError(
                f"{self.render_label} not generated yet for {self.entry.edr_product} -- "
                "call .generate() or dataset.populate() first"
            )

    def plot_vs_basemap(
        self, tie_point_results: dict | None = None, title: str | None = None, render_label: str | None = None
    ):
        """Plots this image against `self.entry.dem_ortho_result.ortho` via
        `plotting.plot_render_vs_basemap`.

        :param render_label: Overrides the default label (`self.generator_name` via
            `plotting.mathtt`, matching `image_generation.ipynb`'s own title convention) in the
            plot's own labeling.
        """
        self._require_generated()
        label = render_label or plotting.mathtt(self.generator_name)
        return plotting.plot_render_vs_basemap(
            plotting.read_raster_band(self.raster_path),
            self.rotation_k,
            self.width_km,
            self.height_km,
            self.footprint_lonlat_deg,
            self.entry.dem_ortho_result.ortho,
            title=title or f"{label} vs. basemap",
            render_label=label,
            tie_point_results=tie_point_results,
            render_px_key=self.tie_point_px_key,
        )

    def plot_overlay(
        self,
        title: str | None = None,
        overlay_label: str | None = None,
        layers: list[plotting.OverlayLayer] | None = None,
        margin_frac: float = 0.3,
    ):
        """Plots this image over `self.entry.dem_ortho_result.ortho` via
        `plotting.plot_overlay_toggle`. Returns an `IPython.display.HTML` object -- callers must
        not add a trailing `;` in a notebook cell, same requirement as calling
        `plot_overlay_toggle` directly.

        :param overlay_label: Overrides the default checkbox-suffix label (`self.generator_name` via
            `plotting.mathtt`) -- see `plotting.plot_overlay_toggle`'s own docstring for the
            checkbox-title format this switches to.
        :param layers: See `plotting.OverlayLayer`'s docstring. Each layer's geometry must already
            be in `self.entry.dem_ortho_result.ortho`'s own raster CRS and already AOI-filtered --
            this class does no fetch/filter/reprojection of its own.
        :param margin_frac: See `plotting.plot_overlay`'s docstring.
        """
        # Shared by both TrnTestHillshadeImage and TrnTestCropImage with no special-casing.
        self._require_generated()
        label = overlay_label or plotting.mathtt(self.generator_name)
        return plotting.plot_overlay_toggle(
            self.entry.dem_ortho_result.ortho,
            self._mapprojected_path(),
            title=title or f"{label} over basemap",
            overlay_label=label,
            layers=layers,
            margin_frac=margin_frac,
        )

    def plot_zoom_blink_over(self, other: TrnTestImage | None = None, crop_px: int = 200, show_self_first: bool = True):
        """Blink comparator (`plotting.plot_zoom_blink`) between this image's own map-projected
        raster and `other`'s, at a full-resolution square crop from the middle of *this* image's
        own footprint (never `other`'s -- see `plotting.plot_zoom_blink`'s own docstring for why
        that matters for a padded basemap AOI specifically).

        `other` is the blink's left-hand entry, matching `plot_overlay`'s own
        `(base_raster_path, overlay_raster_path)` argument order -- `other` stands in for
        `plot_overlay`'s always-implicit basemap by default.

        :param other: The other `TrnTestImage` to compare against (its own already map-projected
            raster is used directly, e.g. `entry.crop.plot_zoom_blink_over(entry.hillshade)`).
            `None` (default) compares against `self.entry.dem_ortho_result.ortho` instead -- the
            same render-then-reproject round trip `plot_overlay` shows, at full pixel detail.
        :param crop_px: Square crop width/height, pixels.
        :param show_self_first: Which frame plays first in the loop (matching `plot_overlay`'s own
            overlay-first default).
        """
        self._require_generated()
        if other is None:
            other_path, other_label = self.entry.dem_ortho_result.ortho, "basemap"
        else:
            other._require_generated()
            other_path, other_label = other._mapprojected_path(), plotting.mathtt(other.generator_name)
        return plotting.plot_zoom_blink(
            other_path,
            self._mapprojected_path(),
            label_a=other_label,
            label_b=plotting.mathtt(self.generator_name),
            crop_px=crop_px,
            show_a_first=not show_self_first,
        )


class TrnTestCropImage(TrnTestImage):
    """The real, ISIS-processed WAC crop: `crop/<edr_product>_crop.cub` + its ISD sidecar."""

    # The sidecar ISD is accurately-scoped but not reprojection-reliable -- see
    # isis_campt.run_isd_generate_for_crop.

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
    def generator_name(self) -> str:
        return "crop"

    @property
    def tie_point_px_key(self) -> str:
        return "crop_px"

    def _generate_impl(self) -> None:
        isd = isis_campt.run_isd_generate_for_crop(
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
    """The synthetic `sat_sim` render: `hillshade/<edr_product>_hillshade.tif` + its CSM/ISD
    sidecar."""

    # The hillshade is baked into the ortho before sat_sim ever runs (see
    # hapke.despeckle_and_shade_ortho), so run_sat_sim's own output already is "hillshade
    # basemap data reprojected via sat_sim".

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
    def generator_name(self) -> str:
        return "hillshade"

    @property
    def tie_point_px_key(self) -> str:
        return "synthetic_px"

    def _generate_impl(self) -> None:
        render_result = render.run_sat_sim(self.entry.camera, self.entry.dem_ortho_result, self.entry.per_image_config)
        self.raster_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(render_result.rendered_tif, self.raster_path)
        shutil.copy(render_result.csm_json, self.sidecar_json_path)

    def _mapprojected_path(self) -> Path:
        # _work/<entry>/<generator>/, not the canonical hillshade/reproject/ folder itself -- same
        # "don't spill mapproject's own intermediates into the published pair's folder" reasoning as
        # TrnTestCropImage's own override. self.raster_path.parent.name ("hillshade"/"reproject")
        # already names this image's own generator -- reused here instead of a second, separate
        # per-subclass constant (this project's own generator-scoped `_work/` tier).
        # camera_type="csm" (the default) against self.sidecar_json_path is safe: camera.
        # solve_corrected_fov is isotropic (fu == fv), so cam_gen's CSM Frame conversion of our own
        # .tsai has no anisotropy to lose -- see docs/reproject-fov-investigation.md for the
        # anisotropic version this once was and why it was reverted.
        out_dir = self.entry.per_image_config.output_dir / self.raster_path.parent.name
        out_path = out_dir / (self.raster_path.stem + "-mapproj.tif")
        return render.run_mapproject_image(
            self.raster_path, self.sidecar_json_path, out_path, self.entry.dem_ortho_result, self.entry.per_image_config
        )


class TrnTestReprojectImage(TrnTestHillshadeImage):
    """The synthetic `sat_sim` render, textured with the WAC crop's own reflectance instead of the
    Lunaserv/Astropedia basemap: `reproject/<edr_product>_reproject.tif` + its CSM/ISD sidecar."""

    # Subclasses TrnTestHillshadeImage, not TrnTestImage directly: it goes through the same
    # sat_sim-render-then-mapproject shape, only the --ortho texture source differs, so raster_path/
    # sidecar_json_path/render_label/generator_name/_generate_impl are the only overrides needed --
    # width_km/height_km/footprint_lonlat_deg/rotation_k/tie_point_px_key/_mapprojected_path are
    # all inherited unchanged.
    #
    # Uses self.entry.camera -- the same Camera hillshade renders with, not a separate one -- so
    # the two are byte-identical in pose and FOV (camera.build_camera()'s FOV correction is
    # applied once, shared by every product type that renders through it -- see
    # solve_corrected_fov's docstring), deliberately, for pixel-grid-identical comparison between
    # them later (e.g. SSIM/LPIPS/diff scoring) -- see docs/reproject-fov-investigation.md. crop
    # (this class's own texture source) is unaffected and naturally larger, providing the margin
    # reproject's render needs.

    @property
    def raster_path(self) -> Path:
        return self.entry.dataset_folder / "reproject" / f"{self.entry.edr_product}_reproject.tif"

    @property
    def sidecar_json_path(self) -> Path:
        return self.entry.dataset_folder / "reproject" / f"{self.entry.edr_product}_reproject.json"

    @property
    def render_label(self) -> str:
        return "Synthetic (sat_sim, real-WAC-textured)"

    @property
    def generator_name(self) -> str:
        return "reproject"

    @functools.cached_property
    def _reproject_dem_ortho(self) -> DemOrthoResult:
        """The real WAC crop's own reflectance, reprojected (`isis_wac.run_cam2map_for_crop`) and
        wrapped as a `DemOrthoResult` sharing `entry.dem_ortho_result`'s own DEM."""
        wac_ortho_path = isis_wac.run_cam2map_for_crop(
            self.entry.crop_result, self.entry.dem_ortho_result, self.entry.per_image_config
        )
        return dem_ortho.result_from_files(wac_ortho_path, self.entry.dem_ortho_result.dem)

    def _generate_impl(self) -> None:
        render_result = render.run_sat_sim(self.entry.camera, self._reproject_dem_ortho, self.entry.per_image_config)
        self.raster_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(render_result.rendered_tif, self.raster_path)
        shutil.copy(render_result.csm_json, self.sidecar_json_path)


class TrnTestReport(TrnTestProduct):
    """The rendered per-entry HTML report (`notebooks/report_template.py`, via
    `report.generate_report`): `reports/<edr_product>/report.html` + its executed notebook."""

    # Not a TrnTestImage: a report isn't itself a comparable raster, so it has no
    # rotation_k/width_km/footprint_lonlat_deg/etc. to supply.

    @property
    def report_dir(self) -> Path:
        return self.entry.dataset_folder / "reports" / self.entry.edr_product

    @property
    def raster_path(self) -> Path:
        return self.report_dir / "report.html"

    @property
    def sidecar_json_path(self) -> Path:
        return self.report_dir / "report.ipynb"  # not JSON -- reuses the "second generated file"
        # slot every other product type has, for the executed notebook rather than an ISD sidecar.

    @property
    def generator_name(self) -> str:
        return "report"

    def exists(self) -> bool:
        return self.raster_path.exists()  # report.html is the deliverable; the .ipynb is a byproduct

    def _generate_impl(self) -> None:
        self.entry.reproject.generate()  # self-ensures its own dependency (report_template.py
        # displays entry.reproject's own raster) rather than relying on callers passing
        # product_types in a particular order -- same reasoning TrnTestReprojectImage already
        # applies via entry.crop_result's cached_property chain. A no-op if already done.
        from trntest import report  # noqa: PLC0415 -- circular otherwise: report.py imports
        # TrnTestDataSet/TrnTestEntry from trn_dataset.py, which imports this module
        # (trn_products.py) to construct product instances.

        report.generate_report(str(self.entry.dataset_folder), self.entry.index, self.report_dir)
