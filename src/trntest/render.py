"""Render the synthetic image with ASP's `sat_sim` using the real SPICE-derived camera, then convert
that exact camera to a CSM Frame model-state JSON sidecar with `cam_gen`. Replaces the old
`run_sat_sim.sh` -- direct subprocess calls instead of a shell script, so the DEM/ortho paths flow
in as plain Python values (no `lunaserv_result.txt` handoff file needed).
"""

import dataclasses
import json
import subprocess
from pathlib import Path

from trntest.camera import Camera
from trntest.config import TrntestConfig, load_config
from trntest.lunaserv import LunaservResult


@dataclasses.dataclass(frozen=True)
class RenderResult:
    """Paths written by `run_sat_sim`."""

    rendered_tif: Path
    csm_json: Path
    camera_list: Path


def run_sat_sim(camera: Camera, lunaserv_result: LunaservResult, config: TrntestConfig | None = None) -> RenderResult:
    config = config or load_config()
    config.output_dir.mkdir(parents=True, exist_ok=True)

    camera_list_path = config.output_dir / "camera_list.txt"
    camera_list_path.write_text(f"{camera.tsai_path}\n")

    render_dir = config.output_dir / "render"
    render_dir.mkdir(parents=True, exist_ok=True)
    render_prefix = render_dir / "run"

    subprocess.run(
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
            "-o",
            str(render_prefix),
        ],
        check=True,
    )

    camera_stem = Path(camera.tsai_path).stem
    rendered_tif = render_dir / f"run-{camera_stem}.tif"
    csm_json = render_dir / f"run-{camera_stem}.json"

    # --save-as-csm only applies to cameras sat_sim itself generates, not ones passed via
    # --camera-list -- convert the rendered image's exact camera to a CSM Frame model-state JSON
    # ("ISD sidecar") with cam_gen instead. --refine-intrinsics none keeps the pose/intrinsics exact
    # (no re-solving), so this is purely a format conversion of our already-computed SPICE pose.
    subprocess.run(
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
        ],
        check=True,
    )

    return RenderResult(rendered_tif=rendered_tif, csm_json=csm_json, camera_list=camera_list_path)


def read_csm_state(csm_json_path: str | Path) -> tuple[str, dict]:
    """The CSM state file's first line is a bare model-name string (not JSON) -- standard CSM
    "state string" convention; skip it before parsing. Returns (model_name, csm_state)."""
    with open(csm_json_path) as f:
        lines = f.readlines()
    model_name = lines[0].strip()
    csm_state = json.loads("".join(lines[1:]))
    return model_name, csm_state
