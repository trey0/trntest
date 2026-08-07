"""Render the synthetic image with ASP's `sat_sim` using the real SPICE-derived camera, then convert
that exact camera to a CSM Frame model-state JSON sidecar with `cam_gen`. Replaces the old
`run_sat_sim.sh` -- direct subprocess calls instead of a shell script, so the DEM/ortho paths flow
in as plain Python values (no `lunaserv_result.txt` handoff file needed).
"""

import dataclasses
import json
from pathlib import Path

from trntest.camera import Camera
from trntest.config import TrntestConfig, load_config
from trntest.lunaserv import LunaservResult
from trntest.subprocess_utils import run_quiet


@dataclasses.dataclass(frozen=True)
class RenderResult:
    """Paths written by `run_sat_sim`."""

    rendered_tif: Path
    csm_json: Path
    camera_list: Path


# `sat_sim --dem-height-error-tol` default is 0.001m -- far tighter than the DEM's actual achievable
# precision. Lunaserv's DTM layer serves planetocentric radius (~1.7e6 m) as float32; float32 has
# ~7.2 significant decimal digits, so its ULP (smallest representable step) at that magnitude is
# already ~0.125m (2**(20-23), since 2**20 < 1.7e6 < 2**21) -- baked into the source data itself
# before `lunaserv.radius_to_elevation` ever subtracts the reference radius, not something fixable
# on our end. Confirmed empirically (see docs/history.md): the default tolerance causes `sat_sim`'s
# ray/DEM-intersection root-finder to misbehave at scattered pixels, producing salt-and-pepper
# speckle in the render; tightening it further makes this dramatically worse, and loosening it to
# comfortably clear the float32 precision floor eliminates it cleanly. 0.5m is a 4x safety margin
# above that ~0.125m floor while still far tighter than anything resolvable at the DEM's 100m/px
# posting.
DEM_HEIGHT_ERROR_TOL_M = 0.5


def run_sat_sim(camera: Camera, lunaserv_result: LunaservResult, config: TrntestConfig | None = None) -> RenderResult:
    config = config or load_config()
    config.output_dir.mkdir(parents=True, exist_ok=True)

    camera_list_path = config.output_dir / "camera_list.txt"
    camera_list_path.write_text(f"{camera.tsai_path}\n")

    render_dir = config.output_dir / "render"
    render_dir.mkdir(parents=True, exist_ok=True)
    render_prefix = render_dir / "run"

    run_quiet(
        [
            "sat_sim",
            "--dem",
            str(lunaserv_result.dem),
            "--ortho",
            str(lunaserv_result.ortho),
            "--camera-list",
            str(camera_list_path),
            "--image-size",
            str(config.image_size),
            str(config.image_size),
            "--dem-height-error-tol",
            str(DEM_HEIGHT_ERROR_TOL_M),
            "-o",
            str(render_prefix),
        ]
    )

    camera_stem = Path(camera.tsai_path).stem
    rendered_tif = render_dir / f"run-{camera_stem}.tif"
    csm_json = render_dir / f"run-{camera_stem}.json"

    # --save-as-csm only applies to cameras sat_sim itself generates, not ones passed via
    # --camera-list -- convert the rendered image's exact camera to a CSM Frame model-state JSON
    # ("ISD sidecar") with cam_gen instead. --refine-intrinsics none keeps the pose/intrinsics exact
    # (no re-solving), so this is purely a format conversion of our already-computed SPICE pose.
    run_quiet(
        [
            "cam_gen",
            str(rendered_tif),
            "--input-camera",
            str(camera.tsai_path),
            "--camera-type",
            "pinhole",
            "--refine-intrinsics",
            "none",
            "-o",
            str(csm_json),
        ]
    )

    return RenderResult(rendered_tif=rendered_tif, csm_json=csm_json, camera_list=camera_list_path)


def run_mapproject_image(
    image_path: Path,
    csm_json_path: Path,
    output_path: Path,
    lunaserv_result: LunaservResult,
    config: TrntestConfig | None = None,
) -> Path:
    """Reproject any image back onto the map via its own CSM/ISD sidecar and ASP `mapproject`'s
    `--ref-map` -- see `run_mapproject`'s docstring for the full rationale. The shared low-level
    worker both `run_mapproject` (the synthetic render's own `cam_gen` sidecar) and
    `isis_wac.run_mapproject` (the real, ISIS-processed WAC cube's ALE-derived ISD) use, so both
    land on the exact same DEM grid with no separate alignment step."""
    config = config or load_config()
    run_quiet(
        [
            "mapproject",
            str(lunaserv_result.dem),
            str(image_path),
            str(csm_json_path),
            str(output_path),
            "--ref-map",
            str(lunaserv_result.dem),
            "-t",
            "csm",
        ]
    )
    return output_path


def run_mapproject(
    render_result: RenderResult, lunaserv_result: LunaservResult, config: TrntestConfig | None = None
) -> Path:
    """Reproject `render_result`'s synthetic image back onto the map using its own CSM sidecar --
    the geometric inverse of `run_sat_sim`'s forward DEM+camera-to-image render, through the same
    camera model, so the output lands back on real ground coordinates (not just a visually-similar
    crop). `--ref-map` reads the projection and grid size from `lunaserv_result.dem` -- the same DEM
    `run_sat_sim` rendered from -- so the output shares an exact pixel grid with every other raster
    in `lunaserv_result` (the hillshade-based ortho included), letting them be overlaid directly with
    no separate reprojection/alignment step. Confirmed empirically (see docs/data-sources.md): this
    round trip aligns real terrain features pixel-precisely, as expected for going forward and back
    through one consistent camera model. Opt-in/on-demand (not part of `dataset.generate_dataset`'s
    default pipeline) -- a real ~4s subprocess call not every run needs."""
    config = config or load_config()
    camera_stem = render_result.rendered_tif.stem
    render_dir = render_result.rendered_tif.parent
    mapproj_tif = render_dir / f"{camera_stem}-mapproj.tif"
    return run_mapproject_image(
        render_result.rendered_tif, render_result.csm_json, mapproj_tif, lunaserv_result, config
    )


def read_csm_state(csm_json_path: str | Path) -> tuple[str, dict]:
    """The CSM state file's first line is a bare model-name string (not JSON) -- standard CSM
    "state string" convention; skip it before parsing. Returns (model_name, csm_state)."""
    with open(csm_json_path) as f:
        lines = f.readlines()
    model_name = lines[0].strip()
    csm_state = json.loads("".join(lines[1:]))
    return model_name, csm_state
