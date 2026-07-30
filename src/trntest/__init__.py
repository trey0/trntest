"""trntest: SPICE-posed synthetic lunar satellite imagery, compared against real LROC WAC imagery.

Typical usage:

    import trntest
    from trntest import plotting

    session = trntest.Session()
    camera = session.build_camera()
    lunaserv_result = session.fetch_dem_and_ortho(camera)
    render_result = session.run_sat_sim(camera, lunaserv_result)

See `trntest.Session` for the full pipeline, or the individual modules (`trntest.camera`,
`trntest.lunaserv`, `trntest.render`, `trntest.wac`, `trntest.tie_points`, `trntest.orientation`)
for the underlying free functions, each independently callable with an explicit `config`.
"""

from trntest import plotting
from trntest.camera import Camera, FrameTiming, build_camera, fetch_frame_timing
from trntest.config import TrntestConfig, load_config
from trntest.lunaserv import LunaservResult, fetch_dem_and_ortho
from trntest.orientation import DisplayRotations, compute_display_rotations
from trntest.render import RenderResult, read_csm_state, run_sat_sim
from trntest.session import Session
from trntest.spice_kernels import fetch_and_furnish
from trntest.tie_points import compute_tie_points
from trntest.wac import fetch_vis_mosaic

__all__ = [
    "Session",
    "TrntestConfig",
    "load_config",
    "FrameTiming",
    "Camera",
    "LunaservResult",
    "RenderResult",
    "DisplayRotations",
    "build_camera",
    "fetch_frame_timing",
    "fetch_dem_and_ortho",
    "fetch_vis_mosaic",
    "compute_tie_points",
    "compute_display_rotations",
    "run_sat_sim",
    "read_csm_state",
    "fetch_and_furnish",
    "plotting",
]
