"""Cross-checks `lunaserv.hapke_shade_ortho`'s hand-rolled Hapke hillshade against Ames Stereo
Pipeline's `sfs` tool, run purely as a forward renderer (`--save-sim-intensity-only`, no DEM
refinement) -- a fully independent ray-DEM intersection and Hapke reflectance implementation, given
the *same* real inputs `hapke_shade_ortho` itself uses: the fetched DEM, the same real,
ISIS-calibration-sourced Hapke parameters (`lunaserv.fetch_real_hapke_params`), and a "true albedo"
map recovered by dividing `hapke_shade_ortho`'s own already-shaded output back down by the exact same
real-geometry reflectance factor it multiplied in (`lunaserv.real_geometry_hapke_reflectance`) --
see `true_albedo_map`'s own docstring for why this, and not `lunaserv.reference_hapke_reflectance`
(a real double-counting bug an earlier version of this module had). See `docs/history.md`'s dated
entries for the spike that validated this approach (including why the real WAC crop's own native
ISIS camera model can't be used here -- `sfs` rejects it outright) and `docs/plan.md`'s open items
for what's still unverified -- notably, `sfs`'s own reflectance uses our reconstructed CSM Frame
camera, which (unlike `hapke_shade_ortho`'s own default) has no way to represent the real WAC
pushframe scan's along-track motion (`along_track_correction`), so even with the double-counting
bug fixed, `sfs`'s implicit geometry is not a fully apples-to-apples match to `hapke_shade_ortho`'s
own default output.
"""

import dataclasses
from pathlib import Path

import numpy as np
import rasterio

from trntest import illumination, isis_wac, lunaserv, render
from trntest.camera import Camera
from trntest.config import TrntestConfig, load_config
from trntest.lunaserv import DemOrthoResult
from trntest.subprocess_utils import run_quiet

# ASP `sfs --model-coeffs` Hapke order is (omega, b, c, B0, h) -- confirmed via ISIS's own
# `photomet.xml` parameter descriptions (WH = single-scattering albedo = "omega"; HG1 = the
# Henyey-Greenstein asymmetry parameter = "b"; HG2 = the HG mixture/backscatter parameter = "c"; B0/
# HH = opposition-surge amplitude/width = "B0"/"h") to be a direct 1:1 correspondence with 5 of
# HAPKEHEN's 6 parameters. ISIS's 6th parameter, THETA (macroscopic roughness, real and non-trivial
# for this project's own candidates -- typically ~20-25 deg, see `fetch_real_hapke_params`), has no
# equivalent anywhere in ASP's `sfs`: confirmed live (`sfs --help | grep -iE 'roughness|theta'`)
# there is no roughness/opposition-shadowing flag at all beyond `--model-coeffs` itself. Silently
# dropped -- a real, permanent gap in this cross-check, not something fixable from this side.
_ASP_HAPKE_COEFF_ORDER = ("wh", "hg1", "hg2", "b0", "hh")


def _sfs_validation_dir(config: TrntestConfig) -> Path:
    """`_work/<entry>/sfs_validation/` -- this module's own generator-scoped tier
    (`docs/history.md`'s Phase 79 entry): investigation-only outputs, not consumed by any
    other generator."""
    d = config.output_dir / "sfs_validation"
    d.mkdir(parents=True, exist_ok=True)
    return d


def hapke_params_to_asp_model_coeffs(hapkehen_params: dict) -> str:
    """`hapkehen_params` (`lunaserv.hapkehen_params_from_source`'s 6-key dict) as ASP `sfs
    --model-coeffs`'s own space-separated `"omega b c B0 h"` string -- see this module's own
    docstring and `_ASP_HAPKE_COEFF_ORDER`'s comment for the parameter-name correspondence (and the
    one parameter, macroscopic roughness, that doesn't carry over)."""
    return " ".join(str(hapkehen_params[name]) for name in _ASP_HAPKE_COEFF_ORDER)


def true_albedo_map(shaded_ortho: np.ndarray, real_reflectance: np.ndarray) -> np.ndarray:
    """A per-pixel "true albedo" proxy for ASP `sfs --input-albedo`, consistent with `sfs`'s own
    `image = exposure * albedo * reflectance(geometry)` formalization.

    **`shaded_ortho` must be `dem_ortho_result.ortho` -- `hapke_shade_ortho`'s already-shaded output
    (`raw_ortho * H(real)/H(reference)`, clipped to `[0,255]`), not the raw pre-shading WAC_EMP
    texture** (this project doesn't keep the raw texture around after shading, and doesn't need to):
    dividing back out `real_reflectance` -- the *same* H(real) factor `hapke_shade_ortho` itself
    multiplied in (`lunaserv.real_geometry_hapke_reflectance`, called with matching
    `dem`/`bbox`/`camera`/`azimuth_deg`/`elevation_deg`/`along_track_correction` arguments) --
    algebraically recovers `raw_ortho_norm/H(reference)` directly:
    `shaded_norm/H(real) = (raw_norm * H(real)/H(reference)) / H(real) = raw_norm/H(reference)`,
    the actual reference-geometry-normalization-undone quantity this function exists to compute.

    **An earlier version of this function instead divided `shaded_ortho` by
    `lunaserv.reference_hapke_reflectance` (H(reference), a constant) -- a real double-counting bug**
    (caught 2026-08-22, see docs/history.md's dated entry): since `shaded_ortho` already has a full
    H(real) factor baked in, dividing by the constant H(reference) a second time left that same
    H(real) factor sitting in "albedo" uncanceled, and `sfs` then multiplied it by its *own*,
    independently-computed H(real) again -- silently squaring the geometry-dependent brightening
    factor wherever it's large (steep favorably-lit terrain, low-phase regions), a real,
    previously-unidentified contributor to the extreme brightening `sfs`'s own unclipped output
    showed toward this candidate's low-phase corner.

    **Residual limitation, not fixed here**: `shaded_ortho` is `hapke_shade_ortho`'s already-`uint8`-
    clipped output, so at any pixel where the original (unclipped) relit value exceeded 255, this
    division recovers an *under*-estimate of the true albedo, not the exact value -- most pronounced
    exactly at the same already-bright, high-H(real) pixels this fix targets. `hapke_shade_ortho`
    doesn't currently expose its own pre-clip float array to correct this further.

    Guards against zero/non-finite `real_reflectance` the same way `hapke_shade_ortho` guards
    `reference_reflectance` (an all-zero albedo at that pixel rather than a division blowup)."""
    shaded_norm = shaded_ortho.astype(np.float64) / 255.0
    valid = (real_reflectance > 0) & np.isfinite(real_reflectance)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = shaded_norm / real_reflectance
    return np.where(valid, ratio, 0.0)


def _camera_cub_for_sfs(
    camera: Camera, dem_ortho_result: DemOrthoResult, out_path: Path, config: TrntestConfig
) -> Path:
    """Builds an ISIS cube with this project's own reconstructed CSM Frame camera attached
    (`render.run_sat_sim`/`patch_sun_position`, `gdal_translate -of ISIS3`, `csminit state=...`) --
    the shared camera-attachment steps both `run_sfs_forward_render` and
    `run_sfs_lambertian_incidence` need as `sfs`'s own image argument, factored out so the two can't
    drift out of sync with each other. See `run_sfs_forward_render`'s own docstring for why this
    camera, not the real WAC crop's native ISIS camera model, is what `sfs` gets."""
    render_result = render.run_sat_sim(camera, dem_ortho_result, config)
    render.patch_sun_position(render_result.csm_json, camera.et)
    run_quiet(["gdal_translate", "-of", "ISIS3", str(render_result.rendered_tif), str(out_path)])
    shape_model_path = isis_wac.ensure_lunar_shape_model(config)
    run_quiet(["csminit", f"from={out_path}", f"state={render_result.csm_json}", f"shapemodel={shape_model_path}"])
    return out_path


@dataclasses.dataclass(frozen=True)
class SfsForwardRenderResult:
    """Paths written by `run_sfs_forward_render`."""

    sim_intensity_tif: Path
    albedo_tif: Path
    camera_cub: Path
    model_coeffs: str
    hapkehen_params: dict


def run_sfs_forward_render(
    camera: Camera, dem_ortho_result: DemOrthoResult, config: TrntestConfig | None = None
) -> SfsForwardRenderResult:
    """Runs ASP `sfs -i <dem> --reflectance-type 2 --model-coeffs <...> --input-albedo <...>
    --save-sim-intensity-only <camera cube>` -- a pure forward-render, no DEM refinement -- against
    `dem_ortho_result`'s own DEM and a `true_albedo_map` built from its own ortho.

    **Uses our own reconstructed CSM camera (`render.run_sat_sim`/`render.patch_sun_position`,
    attached via `csminit state=`), not the real WAC crop's native ISIS camera model.** Confirmed
    live: `sfs` refuses the real crop cube outright ("Seems to have Isis camera type 1... Maybe it
    will work with CSM") -- ASP's own ISIS session support doesn't cover WAC-VIS's native Pushframe
    camera type at all, independent of the (different, `usgscsm`-`groundToImage`-specific) Pushframe
    CSM reliability concern `isis_wac.resolve_ground_to_image_model`'s docstring describes. Our own
    CSM Frame camera is the one already used throughout this project's Hapke shading pipeline itself
    and independently validated against real `campt` ground truth (~0.018 deg,
    `tests/test_lunaserv_campt_validation.py`) -- a real, if different, source of camera-pose truth
    than the real WAC crop's own camera, not a fallback of unknown quality.

    `--input-albedo` must match the DEM's own array shape/georeferencing exactly, not just be "close"
    -- confirmed live the hard way (see docs/history.md's dated entry): a stale, independently-cached
    ortho/DEM pair with genuinely different extents produced a silently misaligned albedo map with no
    error at write time (`rasterio` doesn't validate array-vs-profile shape on `write`, it just crops
    to the destination window). Always derives `ortho`/`dem` from the same `dem_ortho_result` here,
    which is only ever produced by one `fetch_dem_and_ortho` call fetching both together -- but a
    caller resuming a `dem_ortho_result` built by `lunaserv.result_from_files` from independently
    dated files on disk could still hit this; not re-guarded against here.

    `sim_intensity_tif` lands on the DEM's own grid (confirmed live), not the camera's image-space
    pixel grid -- directly comparable to `dem_ortho_result.ortho`/`.dem` with no separate
    reprojection step, but note `sfs` writes literal `0.0` (not a tagged nodata value) for DEM pixels
    outside the camera's actual coverage -- see `mask_sfs_uncovered` before doing any brightness
    comparison against it."""
    config = config or load_config()
    center = camera.footprint_lonlat_deg["center"]
    assert center is not None, "camera's nadir footprint center must be a real ground point"

    with rasterio.open(dem_ortho_result.ortho) as src:
        ortho = src.read(1)
    with rasterio.open(dem_ortho_result.dem) as src:
        dem = src.read(1)
        dem_profile = src.profile

    # The SAME real per-pixel geometry/reflectance `hapke_shade_ortho` itself used to produce
    # `dem_ortho_result.ortho` (default `along_track_correction`/`real_hapke_params`) -- required for
    # `true_albedo_map` to correctly divide it back out; see that function's own docstring for why
    # dividing by anything else (e.g. the constant `reference_hapke_reflectance`) double-counts.
    azimuth_deg, elevation_deg = illumination.sun_azimuth_elevation_deg(*center, camera.et)
    real_reflectance, hapkehen_params = lunaserv.real_geometry_hapke_reflectance(
        dem, dem_ortho_result.bbox, camera, azimuth_deg, elevation_deg, config.dem_target_gsd_m, config
    )
    model_coeffs = hapke_params_to_asp_model_coeffs(hapkehen_params)

    work_dir = _sfs_validation_dir(config)
    albedo = true_albedo_map(ortho, real_reflectance)
    albedo_path = work_dir / "sfs_albedo.tif"
    albedo_profile = dem_profile.copy()
    albedo_profile.update(dtype="float32", count=1, nodata=None)
    with rasterio.open(albedo_path, "w", **albedo_profile) as dst:
        dst.write(albedo.astype(np.float32), 1)

    camera_cub_path = _camera_cub_for_sfs(camera, dem_ortho_result, work_dir / "sfs_camera.cub", config)

    out_prefix = work_dir / "sfs_run" / "run"
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    run_quiet(
        [
            "sfs",
            "-i",
            str(dem_ortho_result.dem),
            "-o",
            str(out_prefix),
            "--reflectance-type",
            "2",
            "--model-coeffs",
            model_coeffs,
            "--input-albedo",
            str(albedo_path),
            "--save-sim-intensity-only",
            str(camera_cub_path),
        ]
    )
    sim_intensity_tif = out_prefix.parent / f"{out_prefix.name}-{camera_cub_path.stem}-sim-intensity.tif"
    return SfsForwardRenderResult(
        sim_intensity_tif=sim_intensity_tif,
        albedo_tif=albedo_path,
        camera_cub=camera_cub_path,
        model_coeffs=model_coeffs,
        hapkehen_params=hapkehen_params,
    )


def mask_sfs_uncovered(sim_intensity_path: Path, out_path: Path) -> Path:
    """`sfs --save-sim-intensity-only` writes literal `0.0` (not a tagged nodata value) for DEM
    pixels outside the camera's actual real coverage -- this project's DEM/ortho AOI is deliberately
    padded beyond the camera's real footprint (`dataset.py`'s own footprint-union padding), so a
    large fraction of `sim_intensity_path`'s own pixels are this "no coverage" zero, not real
    zero-intensity ground truth (confirmed live: ~68-72% of the raster, for this project's current
    default candidate). Masks them to a real `nodata`-tagged raster at `out_path` so downstream
    brightness-matching/diffing (`plotting.compute_brightness_matched_diff`,
    `plotting.plot_sfs_comparison`) isn't dominated by the uncovered region -- the same failure mode
    that showed up live as a >100x-too-large brightness-matched diff before this masking step
    existed (the median landed exactly on the uncovered region's own `0.0`)."""
    with rasterio.open(sim_intensity_path) as src:
        data = src.read(1)
        profile = src.profile
    masked = np.where(data == 0.0, np.nan, data)
    profile.update(nodata=np.nan)
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(masked.astype(profile["dtype"]), 1)
    return out_path


@dataclasses.dataclass(frozen=True)
class SfsLambertianIncidenceResult:
    """Paths/values written by `run_sfs_lambertian_incidence`."""

    sim_intensity_tif: Path
    exposure: float
    camera_cub: Path


def run_sfs_lambertian_incidence(
    camera: Camera, dem_ortho_result: DemOrthoResult, config: TrntestConfig | None = None
) -> SfsLambertianIncidenceResult:
    """Runs `sfs --reflectance-type 0` (Lambertian) with a uniform `--input-albedo` of `1.0`, so its
    raw `sim-intensity` output is exactly `exposure * cos(incidence)` -- Lambert's law has no
    emission or phase term at all -- sidestepping every Hapke-parameterization concern
    `run_sfs_forward_render`'s own comparison has (the missing `theta` mapping,
    `hapke_params_to_asp_model_coeffs`'s 5-of-6-parameter correspondence, the along-track
    camera-model gap that limits that comparison to a rough cross-check). This gives `sfs`'s own,
    fully independent, real ray-DEM-intersection incidence angle, directly comparable to
    `lunaserv.real_geometry_photometric_angles`'s own `incidence_deg` (via
    `incidence_deg_from_lambertian_sim_intensity`) -- confirmed live to agree to ~0.02 deg mean
    |diff| (0.51 deg max, concentrated at crater rims -- discretization noise between the two
    independent normal computations, not a systematic difference; see
    `tests/test_sfs_validation_lambertian_incidence.py`, this project's first genuine DEM-aware
    ground-truth check -- Phase 70/73 found no ISIS tool, `phocube`'s `LOCAL*` backplanes or
    `campt`'s angles, that gives one at all).

    **`exposure` is not `1.0`** -- read back here from `sfs`'s own `<prefix>-exposures.txt`
    (confirmed live: `sfs` applies some non-unit internal default scaling even without
    `--estimate-exposure-haze-albedo`, 139.66 for this project's current default candidate). Always
    divide it out (`incidence_deg_from_lambertian_sim_intensity`) before comparing `sim_intensity_tif`
    to anything in degrees -- the raw values are not themselves `cos(incidence)`. Assumes exactly one
    image (this function's own single `camera`), matching this module's own single-image convention
    elsewhere -- `exposures.txt`'s one line, `"<image path> <exposure>"`, is parsed accordingly."""
    config = config or load_config()
    with rasterio.open(dem_ortho_result.dem) as src:
        dem = src.read(1)
        dem_profile = src.profile

    work_dir = _sfs_validation_dir(config)
    albedo_ones_path = work_dir / "sfs_lambert_albedo_ones.tif"
    albedo_profile = dem_profile.copy()
    albedo_profile.update(dtype="float32", count=1, nodata=None)
    with rasterio.open(albedo_ones_path, "w", **albedo_profile) as dst:
        dst.write(np.ones_like(dem, dtype=np.float32), 1)

    camera_cub_path = _camera_cub_for_sfs(camera, dem_ortho_result, work_dir / "sfs_lambert_camera.cub", config)

    out_prefix = work_dir / "sfs_lambert_run" / "run"
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    run_quiet(
        [
            "sfs",
            "-i",
            str(dem_ortho_result.dem),
            "-o",
            str(out_prefix),
            "--reflectance-type",
            "0",
            "--input-albedo",
            str(albedo_ones_path),
            "--save-sim-intensity-only",
            str(camera_cub_path),
        ]
    )
    sim_intensity_tif = out_prefix.parent / f"{out_prefix.name}-{camera_cub_path.stem}-sim-intensity.tif"
    exposures_text = (out_prefix.parent / f"{out_prefix.name}-exposures.txt").read_text()
    exposure = float(exposures_text.split()[-1])
    return SfsLambertianIncidenceResult(
        sim_intensity_tif=sim_intensity_tif, exposure=exposure, camera_cub=camera_cub_path
    )


def incidence_deg_from_lambertian_sim_intensity(sim_intensity_path: Path, exposure: float) -> np.ndarray:
    """Inverts `run_sfs_lambertian_incidence`'s raw `sim-intensity` output (`exposure *
    cos(incidence)`, with `sfs`'s own literal `0.0` for DEM pixels outside real camera coverage --
    same convention `mask_sfs_uncovered` handles for the Hapke case) into an incidence-angle array
    (degrees, `NaN` outside coverage). Clips `cos(incidence)` to `[-1, 1]` before `arccos` to absorb
    tiny floating-point overshoot right at near-zero incidence, rather than letting it raise/NaN."""
    with rasterio.open(sim_intensity_path) as src:
        sim = src.read(1)
    valid = sim != 0.0
    cos_incidence = sim / exposure
    incidence_deg = np.full(sim.shape, np.nan)
    incidence_deg[valid] = np.degrees(np.arccos(np.clip(cos_incidence[valid], -1.0, 1.0)))
    return incidence_deg
