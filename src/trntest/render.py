"""Render the synthetic image with ASP's `sat_sim` using the real SPICE-derived camera, then convert
that exact camera to a CSM Frame model-state JSON sidecar with `cam_gen`. Takes the DEM/ortho paths
as plain Python values directly -- no file handoff needed.
"""

import dataclasses
import json
from pathlib import Path

import numpy as np
import spiceypy as spice

from trntest.camera import Camera
from trntest.config import TrntestConfig, load_config
from trntest.lunaserv import DemOrthoResult
from trntest.product_registry import atomic_publish_path, atomic_publish_prefix, writes_product
from trntest.subprocess_utils import run_quiet


@dataclasses.dataclass(frozen=True)
class RenderResult:
    """Paths written by `run_sat_sim`."""

    rendered_tif: Path
    csm_json: Path
    camera_list: Path


# `sat_sim --dem-height-error-tol` default is 0.001m -- far tighter than the DEM's achievable
# precision. Lunaserv's DTM layer serves planetocentric radius (~1.7e6 m) as float32; float32 has
# ~7.2 significant decimal digits, so its ULP (smallest representable step) at that magnitude is
# already ~0.125m (2**(20-23), since 2**20 < 1.7e6 < 2**21) -- baked into the source data itself
# before `lunaserv.radius_to_elevation` ever subtracts the reference radius, not something fixable
# on our end. The default tolerance causes `sat_sim`'s ray/DEM-intersection root-finder to misbehave
# at scattered pixels, producing salt-and-pepper speckle in the render; tightening it further makes
# this dramatically worse, and loosening it to comfortably clear the float32 precision floor
# eliminates it cleanly. 0.5m is a 4x safety margin above that ~0.125m floor while still far tighter
# than anything resolvable at the DEM's 100m/px posting.
DEM_HEIGHT_ERROR_TOL_M = 0.5


@writes_product("sat_sim_render")
def run_sat_sim(camera: Camera, dem_ortho_result: DemOrthoResult, config: TrntestConfig | None = None) -> RenderResult:
    """Render the synthetic image with ASP's `sat_sim`, then convert the camera to a CSM Frame
    model-state JSON sidecar with `cam_gen`.

    :returns: Paths to the rendered TIFF, CSM JSON sidecar, and camera-list file.
    """
    # `sat_sim`'s own `-o <prefix>` convention appends its own fixed `-<camera_stem>.tif` suffix to
    # whatever prefix it's given (`<camera_stem>` comes from the camera-list file's own contents,
    # not from the given prefix) -- `atomic_publish_prefix` fits this (see its own docstring),
    # unlike `atomic_publish_path`'s exact-final-path contract. `cam_gen`'s own `-o`, by contrast,
    # does take an exact path, so its own write goes through plain `atomic_publish_path`.
    config = config or load_config()
    config.output_dir.mkdir(parents=True, exist_ok=True)

    camera_list_path = config.output_dir / "camera_list.txt"
    camera_list_path.write_text(f"{camera.tsai_path}\n")

    render_dir = config.output_dir / "render"
    render_dir.mkdir(parents=True, exist_ok=True)
    camera_stem = Path(camera.tsai_path).stem
    rendered_tif = render_dir / f"run-{camera_stem}.tif"

    with atomic_publish_prefix(rendered_tif, f"-{camera_stem}.tif") as tmp_prefix:
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
                str(tmp_prefix),
            ]
        )

    csm_json = render_dir / f"run-{camera_stem}.json"
    # --save-as-csm only applies to cameras sat_sim itself generates, not ones passed via
    # --camera-list -- convert the rendered image's exact camera to a CSM Frame model-state JSON
    # ("ISD sidecar") with cam_gen instead. --refine-intrinsics none keeps the pose/intrinsics exact
    # (no re-solving), so this is purely a format conversion of our already-computed SPICE pose.
    with atomic_publish_path(csm_json) as tmp_json:
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
                str(tmp_json),
            ]
        )
    return RenderResult(rendered_tif=rendered_tif, csm_json=csm_json, camera_list=camera_list_path)


def run_mapproject_image(
    image_path: Path,
    camera_path: Path,
    output_path: Path,
    dem_ortho_result: DemOrthoResult,
    config: TrntestConfig | None = None,
    camera_type: str = "csm",
) -> Path:
    """Reproject `image_path` back onto the map via `camera_path`'s own camera model and ASP
    `mapproject`'s `--ref-map`.

    :param camera_path: A camera file matching `camera_type` ("csm" or "pinhole").
    :param dem_ortho_result: Supplies the reference DEM (`--ref-map`) both this and `run_sat_sim`
        share, so the output lands on the exact same pixel grid.
    :param camera_type: See the comment below for the full rationale for keeping this generic
        rather than hardcoded.
    :returns: `output_path`.
    """
    # The shared low-level worker both `run_mapproject` (the synthetic render's own `cam_gen` CSM
    # sidecar) and `isis_wac.run_mapproject` (the WAC cube's ALE-derived ISD) use -- currently dead
    # code, no live caller for either. Kept generic (`camera_type`) rather than hardcoded, as good
    # hygiene, even though its live caller (`trn_dataset.TrnTestHillshadeImage._mapprojected_path`)
    # always uses the default `"csm"` now: an earlier, since-reverted anisotropic `fu`/`fv` FOV
    # (`camera.solve_corrected_fov`) once made `cam_gen`'s CSM Frame conversion measurably wrong
    # here (silently averaging `fu`/`fv` into one isotropic `m_focalLength`, a ~5%
    # reprojected-footprint error), which this parameter let the caller work around
    # (`camera_type="pinhole"`, reading `camera.tsai_path` directly). Now that `solve_corrected_fov`
    # is isotropic again (`fu == fv` always), CSM and Pinhole reprojections of the same camera agree
    # by construction, so the parameter is no longer load-bearing for correctness -- just kept
    # generic. See docs/reproject-fov-investigation.md for the full history.
    config = config or load_config()
    output_path.parent.mkdir(parents=True, exist_ok=True)
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
    the geometric inverse of `run_sat_sim`'s forward DEM+camera-to-image render.

    :returns: Path to the reprojected TIFF, sharing an exact pixel grid with every other raster in
        `dem_ortho_result` (the hillshade-based ortho included).
    """
    # `--ref-map` reads the projection and grid size from `dem_ortho_result.dem` -- the same DEM
    # `run_sat_sim` rendered from -- letting outputs be overlaid directly with no separate
    # reprojection/alignment step. This round trip aligns terrain features pixel-precisely, as
    # expected for going forward and back through one consistent camera model. Opt-in/on-demand
    # (not part of `dataset.generate_dataset`'s default pipeline) -- a ~4s subprocess call not
    # every run needs.
    config = config or load_config()
    camera_stem = render_result.rendered_tif.stem
    render_dir = render_result.rendered_tif.parent
    mapproj_tif = render_dir / f"{camera_stem}-mapproj.tif"
    return run_mapproject_image(
        render_result.rendered_tif, render_result.csm_json, mapproj_tif, dem_ortho_result, config
    )


def read_csm_state(csm_json_path: str | Path) -> tuple[str, dict]:
    """Parses a CSM state file: a bare model-name string on the first line (not JSON, per the
    standard CSM "state string" convention), then the JSON state on the rest.

    :returns: `(model_name, csm_state)`.
    """
    with open(csm_json_path) as f:
        lines = f.readlines()
    model_name = lines[0].strip()
    csm_state = json.loads("".join(lines[1:]))
    return model_name, csm_state


def patch_sun_position(csm_json_path: str | Path, et: float) -> None:
    """Patch `m_sunPosition` into a CSM state file produced by `run_sat_sim`'s `cam_gen` conversion,
    in-place.

    :param csm_json_path: A CSM state file in `read_csm_state`'s format.
    :param et: SPICE ET (seconds) to compute the sun position at.
    """
    # `cam_gen`'s CSM Frame conversion never populates `m_sunPosition` -- ASP's own tools have no
    # need for sun geometry -- so a CSM state produced this way leaves ISIS's `csminit`/`campt`/
    # `phocube` with a degenerate (zero) sun position, and therefore degenerate incidence/phase,
    # once attached. Patches in the actual one via the same ephemeris call
    # `illumination.sun_azimuth_elevation_deg` already makes (`spice.spkpos("SUN", et, "MOON_ME",
    # "NONE", "MOON")`) -- SPICE's own native km, converted to meters (`* 1000.0`, matching this
    # project's own `camera.py` convention, e.g. `camera_center_moon_me_m`) to match the CSM state's
    # other position fields. Assumes the relevant SPICE kernels are already furnished (true by the
    # time a `Camera` -- and therefore `et` -- has been built for this candidate), matching
    # `illumination.py`'s own assumption; does not furnish kernels itself.
    #
    # Feed the resulting file to ISIS's `csminit` via its `state=` parameter, not `isd=`: `csminit
    # isd=` expects a from-scratch ISD (the format ALE's `isd_generate` produces,
    # `isis_wac.run_isd_generate`'s own ISD), which needs a "constructModelFromISD" build step per
    # candidate plugin/model and fails ("Could not parse the sensor model name") on a
    # `cam_gen`-style pre-built model *state* string like this one even once it's valid JSON.
    # `csminit state=` (a separate, documented parameter, not just an alias) is the one that
    # actually wants this file's own native "bare model-name line + JSON state" format -- exactly
    # what `read_csm_state`/`cam_gen` already produce, unmodified.
    model_name, csm_state = read_csm_state(csm_json_path)
    sun_position_km, _ = spice.spkpos("SUN", et, "MOON_ME", "NONE", "MOON")
    csm_state["m_sunPosition"] = (np.asarray(sun_position_km, dtype=float) * 1000.0).tolist()
    with open(csm_json_path, "w") as f:
        f.write(model_name + "\n")
        json.dump(csm_state, f)
