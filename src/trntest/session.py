"""Thin convenience facade over the module-level functions, so notebook/interactive code doesn't
repeat `config=...` at every call. The logic stays in `camera.py`/`dem_ortho.py`/`render.py`/etc. as
free functions (independently usable and unit-testable without constructing a `Session`); `Session`
methods are one-line delegators.
"""

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from trntest import camera, dataset, dem_ortho, orientation, render, spice_kernels, tie_points, wac
from trntest.camera import Camera, FrameTiming
from trntest.config import TrntestConfig, load_config
from trntest.dataset import GenerationResult
from trntest.dem_ortho import DemOrthoResult
from trntest.orientation import DisplayRotations
from trntest.render import RenderResult


def _inherit_doc(source):
    """Decorator: copy `source`'s docstring onto the decorated method.

    :param source: The free function whose docstring to copy.
    :returns: The decorator.
    """

    # So help() on the Session wrapper shows the explanation instead of a blank one. Deliberately
    # not `functools.wraps` -- that would also copy `__wrapped__` and make
    # inspect.signature()/help() show the free function's signature (including a `config` parameter
    # the Session method doesn't actually take).
    def decorator(method):
        method.__doc__ = source.__doc__
        return method

    return decorator


class Session:
    """Convenience entry point: holds a resolved `TrntestConfig` so pipeline steps don't need
    `config=...` repeated at every call. See `trntest.camera`/`trntest.dem_ortho`/etc. for the
    underlying free functions if you'd rather call them directly with an explicit config."""

    def __init__(self, config: TrntestConfig | None = None):
        self.config = config or load_config()

    @_inherit_doc(camera.build_camera)
    def build_camera(self, output_tsai_path=None) -> Camera:
        return camera.build_camera(config=self.config, output_tsai_path=output_tsai_path)

    @_inherit_doc(dem_ortho.fetch_dem_and_ortho)
    def fetch_dem_and_ortho(self, camera: Camera) -> DemOrthoResult:
        return dem_ortho.fetch_dem_and_ortho(camera, config=self.config)

    @_inherit_doc(render.run_sat_sim)
    def run_sat_sim(self, camera: Camera, dem_ortho_result: DemOrthoResult) -> RenderResult:
        return render.run_sat_sim(camera, dem_ortho_result, config=self.config)

    @_inherit_doc(render.run_mapproject)
    def run_mapproject(self, render_result: RenderResult, dem_ortho_result: DemOrthoResult) -> Path:
        return render.run_mapproject(render_result, dem_ortho_result, config=self.config)

    @_inherit_doc(camera.fetch_frame_timing)
    def fetch_frame_timing(self) -> FrameTiming:
        return camera.fetch_frame_timing(config=self.config)

    @_inherit_doc(tie_points.select_tie_points)
    def select_tie_points(self, camera: Camera, frame_timing: FrameTiming) -> dict:
        return tie_points.select_tie_points(frame_timing, camera, config=self.config)

    @_inherit_doc(orientation.compute_display_rotations)
    def compute_display_rotations(self, camera: Camera, frame_timing: FrameTiming) -> DisplayRotations:
        return orientation.compute_display_rotations(camera, frame_timing, config=self.config)

    @_inherit_doc(wac.fetch_vis_mosaic)
    def fetch_vis_mosaic(self, camera: Camera) -> np.ndarray:
        return wac.fetch_vis_mosaic(camera, config=self.config)

    @_inherit_doc(spice_kernels.fetch_and_furnish)
    def fetch_and_furnish(self, target_dt: datetime) -> list[str]:
        return spice_kernels.fetch_and_furnish(target_dt, config=self.config)

    @_inherit_doc(dataset.generate_dataset)
    def generate_dataset(self, images: pd.DataFrame, limit: int | None = None, **kwargs) -> list[GenerationResult]:
        return dataset.generate_dataset(images, config=self.config, limit=limit, **kwargs)
