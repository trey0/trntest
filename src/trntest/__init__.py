"""trntest: SPICE-posed synthetic lunar satellite imagery, compared against real LROC WAC imagery.

Typical usage:

    import trntest
    from trntest import plotting

    session = trntest.Session()
    camera = session.build_camera()
    dem_ortho_result = session.fetch_dem_and_ortho(camera)
    render_result = session.run_sat_sim(camera, dem_ortho_result)

See `trntest.Session` for the full pipeline, or the individual modules (`trntest.camera`,
`trntest.dem_ortho`, `trntest.render`, `trntest.tie_points`, `trntest.orientation`)
for the underlying free functions, each independently callable with an explicit `config`.
"""

from trntest import plotting, report
from trntest.camera import Camera, FrameTiming, build_camera, fetch_frame_timing
from trntest.candidate_window import (
    DATASET_COLUMNS,
    GenerationResult,
    generate_dataset,
    read_manifest,
    write_manifest,
)
from trntest.catalog import CATALOG_COLUMNS
from trntest.config import TrntestConfig, load_config
from trntest.dem_ortho import DemOrthoResult, fetch_dem_and_ortho
from trntest.orientation import DisplayRotations, compute_display_rotations
from trntest.render import RenderResult, read_csm_state, run_sat_sim
from trntest.session import Session
from trntest.spice_kernels import fetch_and_furnish
from trntest.tie_points import resolve_crop_pixels, select_tie_points
from trntest.trn_dataset import TrnTestDataSet, TrnTestEntry
from trntest.trn_products import TrnTestImage, TrnTestProduct, TrnTestReport

__all__ = [
    "Session",
    "TrntestConfig",
    "load_config",
    "FrameTiming",
    "Camera",
    "DemOrthoResult",
    "RenderResult",
    "DisplayRotations",
    "GenerationResult",
    "CATALOG_COLUMNS",
    "DATASET_COLUMNS",
    "build_camera",
    "fetch_frame_timing",
    "fetch_dem_and_ortho",
    "select_tie_points",
    "resolve_crop_pixels",
    "compute_display_rotations",
    "run_sat_sim",
    "read_csm_state",
    "fetch_and_furnish",
    "generate_dataset",
    "write_manifest",
    "read_manifest",
    "plotting",
    "report",
    "TrnTestDataSet",
    "TrnTestEntry",
    "TrnTestProduct",
    "TrnTestImage",
    "TrnTestReport",
]
