"""Thin convenience facade over the module-level functions, so notebook/interactive code doesn't
repeat `config=...` at every call. The actual logic stays in `camera.py`/`lunaserv.py`/`render.py`/
etc. as free functions (independently usable and unit-testable without constructing a `Session`);
`Session` methods are one-line delegators.
"""

from datetime import datetime

import numpy as np

from trntest import camera, lunaserv, orientation, render, spice_kernels, tie_points, wac
from trntest.camera import Camera, FrameTiming
from trntest.config import TrntestConfig, load_config
from trntest.lunaserv import LunaservResult
from trntest.orientation import DisplayRotations
from trntest.render import RenderResult


def _inherit_doc(source):
    """Decorator: copy `source`'s docstring onto the decorated method, so help() on the Session
    wrapper shows the real explanation instead of a blank one. Deliberately not `functools.wraps` --
    that would also copy `__wrapped__` and make inspect.signature()/help() show the free function's
    signature (including a `config` parameter the Session method doesn't actually take)."""

    def decorator(method):
        method.__doc__ = source.__doc__
        return method

    return decorator


class Session:
    """Convenience entry point: holds a resolved `TrntestConfig` so pipeline steps don't need
    `config=...` repeated at every call. See `trntest.camera`/`trntest.lunaserv`/etc. for the
    underlying free functions if you'd rather call them directly with an explicit config."""

    def __init__(self, config: TrntestConfig | None = None):
        self.config = config or load_config()

    @_inherit_doc(camera.build_camera)
    def build_camera(self, output_tsai_path=None) -> Camera:
        return camera.build_camera(config=self.config, output_tsai_path=output_tsai_path)

    @_inherit_doc(lunaserv.fetch_dem_and_ortho)
    def fetch_dem_and_ortho(self, camera: Camera) -> LunaservResult:
        return lunaserv.fetch_dem_and_ortho(camera, config=self.config)

    @_inherit_doc(render.run_sat_sim)
    def run_sat_sim(self, camera: Camera, lunaserv_result: LunaservResult) -> RenderResult:
        return render.run_sat_sim(camera, lunaserv_result, config=self.config)

    @_inherit_doc(camera.fetch_frame_timing)
    def fetch_frame_timing(self) -> FrameTiming:
        return camera.fetch_frame_timing(config=self.config)

    @_inherit_doc(tie_points.compute_tie_points)
    def compute_tie_points(self, camera: Camera, frame_timing: FrameTiming) -> dict:
        return tie_points.compute_tie_points(frame_timing, camera, config=self.config)

    @_inherit_doc(orientation.compute_display_rotations)
    def compute_display_rotations(self, camera: Camera, frame_timing: FrameTiming) -> DisplayRotations:
        return orientation.compute_display_rotations(camera, frame_timing, config=self.config)

    @_inherit_doc(wac.fetch_vis_mosaic)
    def fetch_vis_mosaic(self) -> np.ndarray:
        return wac.fetch_vis_mosaic(config=self.config)

    @_inherit_doc(spice_kernels.fetch_and_furnish)
    def fetch_and_furnish(self, target_dt: datetime) -> list[str]:
        return spice_kernels.fetch_and_furnish(target_dt, config=self.config)
