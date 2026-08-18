"""Render the synthetic image with ASP's `sat_sim` using the real SPICE-derived camera, then convert
that exact camera to a CSM Frame model-state JSON sidecar with `cam_gen`. Replaces the old
`run_sat_sim.sh` -- direct subprocess calls instead of a shell script, so the DEM/ortho paths flow
in as plain Python values (no `dem_ortho_result.txt` handoff file needed).
"""

import dataclasses
import json
from pathlib import Path

from trntest.camera import Camera
from trntest.config import TrntestConfig, load_config
from trntest.lunaserv import DemOrthoResult
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


def _correct_csm_focal_length_anisotropy(csm_json_path: Path, fu: float, fv: float) -> None:
    """`cam_gen`'s Pinhole -> CSM Frame conversion collapses an asymmetric `fu`/`fv`
    (`camera.solve_corrected_fov`) into a single, averaged, isotropic `m_focalLength` -- confirmed
    live: for a real candidate, `cam_gen` wrote `m_focalLength = (fu+fv)/2` exactly, leaving
    `m_iTransL`/`m_iTransS`/`m_transX`/`m_transY` at trivial isotropic (`+-1`) coefficients, silently
    losing the real per-axis difference (harmless while `fu=fv` always held, a real ~5% one-axis
    reprojection error once it didn't -- see docs/reproject-fov-investigation.md for the live
    Phase 5B blink-overlay regression this caused).

    **The CSM Frame model itself has no such limitation** -- confirmed via `ale`'s own
    real-instrument formatters (`ale/drivers/lro_drivers.py`, installed in this image), which
    populate these same fields from NAIF's real, genuinely anisotropic `INS<id>_ITRANSL`/`ITRANSS`/
    `TRANSX`/`TRANSY` instrument-kernel keywords for actual flight cameras -- `cam_gen` just doesn't
    do this for a synthetic Pinhole input. This function restores it in place, after the fact:
    pivots `m_focalLength` to `fu` (the cross-track/sample-axis value), which leaves the sample-axis
    transforms (`m_iTransS`/`m_transX`) correct as `cam_gen` already wrote them (coefficient `+-1`
    against a pivot of `fu` is exactly what they already were), and rescales the along-track/line-
    axis transforms (`m_iTransL`/`m_transY`) so their effective scale becomes `fv` instead of `fu`.
    Reads back the *sign* of whichever slot `cam_gen` set nonzero rather than assuming a fixed index
    or sign, so this holds regardless of `cam_gen`'s own axis-order/flip convention -- but does
    assume that slot's original *magnitude* is exactly 1 (true for every `.tsai` this project writes,
    all with `pitch = 1`, i.e. already in pixel units -- `cam_gen`'s own doc: "If set to 1, the focal
    length and optical center are in units of pixel"). A no-op when `fu == fv`.

    Live-validated: a hand-patched sidecar's `mapproject -t csm` reprojected footprint matched the
    geometrically-correct `-t pinhole` result (146.0x139.1 km) to within ~0.2% (146.3x139.2 km) --
    vs. ~2-4% off (143.1x142.6 km, visibly too square) before this correction."""
    if fu == fv:
        return
    with open(csm_json_path) as f:
        header = f.readline()
        state = json.load(f)
    assert state["m_modelName"] == "USGS_ASTRO_FRAME_SENSOR_MODEL", (
        f"unexpected CSM model {state['m_modelName']!r} -- this correction assumes cam_gen's Frame sensor model layout"
    )

    def rescaled(transform: list[float], magnitude: float) -> list[float]:
        return [magnitude if v > 0 else -magnitude if v < 0 else v for v in transform]

    ratio = fv / fu
    state["m_focalLength"] = fu
    state["m_iTransL"] = rescaled(state["m_iTransL"], ratio)
    state["m_transY"] = rescaled(state["m_transY"], 1.0 / ratio)

    with open(csm_json_path, "w") as f:
        f.write(header)
        json.dump(state, f, indent=2)


def run_sat_sim(camera: Camera, dem_ortho_result: DemOrthoResult, config: TrntestConfig | None = None) -> RenderResult:
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
            str(dem_ortho_result.dem),
            "--ortho",
            str(dem_ortho_result.ortho),
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
    _correct_csm_focal_length_anisotropy(csm_json, camera.focal_length_u_px, camera.focal_length_v_px)

    return RenderResult(rendered_tif=rendered_tif, csm_json=csm_json, camera_list=camera_list_path)


def run_mapproject_image(
    image_path: Path,
    camera_path: Path,
    output_path: Path,
    dem_ortho_result: DemOrthoResult,
    config: TrntestConfig | None = None,
    camera_type: str = "csm",
) -> Path:
    """Reproject any image back onto the map via its own camera model and ASP `mapproject`'s
    `--ref-map` -- see `run_mapproject`'s docstring for the full rationale. The shared low-level
    worker both `run_mapproject` (the synthetic render's own `cam_gen` CSM sidecar, dead code -- no
    live caller, see below) and `isis_wac.run_mapproject` (the real, ISIS-processed WAC cube's
    ALE-derived ISD, also dead code) use, so both land on the exact same DEM grid with no separate
    alignment step -- kept generic (`camera_type`) rather than hardcoded `-t csm` since its one live
    caller (`trn_dataset.TrnTestHillshadeImage._mapprojected_path`) must NOT use CSM: `cam_gen`'s
    CSM Frame conversion of our own `.tsai` only has a single, isotropic `m_focalLength` field --
    confirmed live it silently *averages* an asymmetric `fu`/`fv` (`camera.solve_corrected_fov`) into
    one value, giving `mapproject` a measurably wrong (near-square, ~5% off) reprojected footprint
    and a real, user-visible Phase 5B/6B blink-overlay misalignment that didn't exist before `fu`
    could differ from `fv`. Passing `camera_path=camera.tsai_path, camera_type="pinhole"` instead --
    ASP's own Pinhole model, read directly, has no such isotropy limitation -- fixed it: confirmed
    live the reprojected footprint's own aspect ratio (145989x139090, non-square) then matches
    `Camera.render_cross_track_km`/`render_along_track_km`'s real ~1.05 ratio, instead of CSM's
    142589-ish near-1:1 square. See docs/reproject-fov-investigation.md."""
    config = config or load_config()
    run_quiet(
        [
            "mapproject",
            str(dem_ortho_result.dem),
            str(image_path),
            str(camera_path),
            str(output_path),
            "--ref-map",
            str(dem_ortho_result.dem),
            "-t",
            camera_type,
        ]
    )
    return output_path


def run_mapproject(
    render_result: RenderResult, dem_ortho_result: DemOrthoResult, config: TrntestConfig | None = None
) -> Path:
    """Reproject `render_result`'s synthetic image back onto the map using its own CSM sidecar --
    the geometric inverse of `run_sat_sim`'s forward DEM+camera-to-image render, through the same
    camera model, so the output lands back on real ground coordinates (not just a visually-similar
    crop). `--ref-map` reads the projection and grid size from `dem_ortho_result.dem` -- the same DEM
    `run_sat_sim` rendered from -- so the output shares an exact pixel grid with every other raster
    in `dem_ortho_result` (the hillshade-based ortho included), letting them be overlaid directly with
    no separate reprojection/alignment step. Confirmed empirically (see docs/data-sources.md): this
    round trip aligns real terrain features pixel-precisely, as expected for going forward and back
    through one consistent camera model. Opt-in/on-demand (not part of `dataset.generate_dataset`'s
    default pipeline) -- a real ~4s subprocess call not every run needs."""
    config = config or load_config()
    camera_stem = render_result.rendered_tif.stem
    render_dir = render_result.rendered_tif.parent
    mapproj_tif = render_dir / f"{camera_stem}-mapproj.tif"
    return run_mapproject_image(
        render_result.rendered_tif, render_result.csm_json, mapproj_tif, dem_ortho_result, config
    )


def read_csm_state(csm_json_path: str | Path) -> tuple[str, dict]:
    """The CSM state file's first line is a bare model-name string (not JSON) -- standard CSM
    "state string" convention; skip it before parsing. Returns (model_name, csm_state)."""
    with open(csm_json_path) as f:
        lines = f.readlines()
    model_name = lines[0].strip()
    csm_state = json.loads("".join(lines[1:]))
    return model_name, csm_state
