"""Render the synthetic image with ASP's `sat_sim` using the real SPICE-derived camera, then convert
that exact camera to a CSM Frame model-state JSON sidecar with `cam_gen`. Replaces the old
`run_sat_sim.sh` -- direct subprocess calls instead of a shell script, so the DEM/ortho paths flow
in as plain Python values (no `dem_ortho_result.txt` handoff file needed).
"""

import dataclasses
import json
from pathlib import Path

import numpy as np
import spiceypy as spice

from trntest.camera import Camera
from trntest.config import TrntestConfig, load_config
from trntest.lunaserv import DemOrthoResult
from trntest.product_registry import writes_product
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


@writes_product("sat_sim_render")
def run_sat_sim(camera: Camera, dem_ortho_result: DemOrthoResult, config: TrntestConfig | None = None) -> RenderResult:
    """Not retrofit with `atomic_publish`/`atomic_publish_path` (docs/intermediate-product-plan.md's
    Phase 2) -- `sat_sim`/`cam_gen`'s own `-o <prefix>` convention appends its own suffix to whatever
    prefix they're given (`<prefix>-<camera_stem>.tif`/`.json`, not the exact literal path), which
    doesn't fit either helper's single-exact-path contract without extra bookkeeping this pass didn't
    take on -- registered here (`writes_product`) for legibility regardless."""
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
    alignment step. Kept generic (`camera_type`) rather than hardcoded, as good hygiene, even though
    its live caller (`trn_dataset.TrnTestHillshadeImage._mapprojected_path`) always uses the default
    `"csm"` now: an earlier, since-reverted anisotropic `fu`/`fv` FOV (`camera.solve_corrected_fov`)
    once made `cam_gen`'s CSM Frame conversion measurably wrong here (silently averaging `fu`/`fv`
    into one isotropic `m_focalLength`, a real ~5% reprojected-footprint error), which this parameter
    let the caller work around (`camera_type="pinhole"`, reading `camera.tsai_path` directly). Now
    that `solve_corrected_fov` is isotropic again (`fu == fv` always), CSM and Pinhole reprojections
    of the same camera agree by construction, so the parameter is no longer load-bearing for
    correctness -- just kept generic. See docs/reproject-fov-investigation.md for the full history."""
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


def patch_sun_position(csm_json_path: str | Path, et: float) -> None:
    """`cam_gen`'s CSM Frame conversion (`run_sat_sim`) never populates `m_sunPosition` -- ASP's own
    tools have no need for real sun geometry -- so a CSM state produced this way leaves ISIS's
    `csminit`/`campt`/`phocube` with a degenerate (zero) sun position, and therefore degenerate
    incidence/phase, once attached. Patches in the real one, in-place, via the same real-ephemeris
    call `illumination.sun_azimuth_elevation_deg` already makes (`spice.spkpos("SUN", et, "MOON_ME",
    "NONE", "MOON")`) -- SPICE's own native km, converted to meters (`* 1000.0`, matching this
    project's own `camera.py` convention, e.g. `camera_center_moon_me_m`) to match the CSM state's
    other position fields. First proven out by hand during Phase 70's `phocube` investigation
    (docs/history.md); this is that same fix, as a reusable function instead of a one-off notebook
    step. Assumes the relevant SPICE kernels are already furnished (true by the time a real `Camera`
    -- and therefore `et` -- has been built for this candidate), matching `illumination.py`'s own
    assumption; does not furnish kernels itself.

    Feed the resulting file to ISIS's `csminit` via its `state=` parameter, not `isd=` -- confirmed
    live (Phase 71, docs/history.md): `csminit isd=` expects a from-scratch ISD (the format ALE's
    `isd_generate` produces, `isis_wac.run_isd_generate`'s own ISD), which needs a real
    "constructModelFromISD" build step per candidate plugin/model and fails ("Could not parse the
    sensor model name") on a `cam_gen`-style pre-built model *state* string like this one even once
    it's valid JSON. `csminit state=` (a separate, documented parameter, not just an alias) is the one
    that actually wants this file's own native "bare model-name line + JSON state" format -- exactly
    what `read_csm_state`/`cam_gen` already produce, unmodified."""
    model_name, csm_state = read_csm_state(csm_json_path)
    sun_position_km, _ = spice.spkpos("SUN", et, "MOON_ME", "NONE", "MOON")
    csm_state["m_sunPosition"] = (np.asarray(sun_position_km, dtype=float) * 1000.0).tolist()
    with open(csm_json_path, "w") as f:
        f.write(model_name + "\n")
        json.dump(csm_state, f)
