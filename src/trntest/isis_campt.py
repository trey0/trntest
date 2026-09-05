"""ISIS `campt`-based ground-truth ground<->image queries against an already-processed WAC cube, plus
the CSM ISD generation (`isd_generate`) those queries -- and `run_mapproject` -- depend on.
`isis_wac.py` covers running the pipeline itself (EDR through `cam2map`); this module answers "where
does this ground point land"/"what ground point is under this pixel" once a cube already exists.
"""
# House style matches isis_wac.py/render.py: frozen dataclass results holding `Path`s, `config =
# config or load_config()`, subprocess calls via the shared `run_quiet` helper (not raw
# `subprocess.run`).

from __future__ import annotations

import csv
import dataclasses
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pvl

from trntest import isis_wac, render
from trntest.config import TrntestConfig, load_config
from trntest.subprocess_utils import run_quiet

if TYPE_CHECKING:
    from trntest.camera import Camera
    from trntest.dem_ortho import DemOrthoResult


@dataclasses.dataclass(frozen=True)
class IsdGenerateResult:
    """The generated CSM ISD, as returned by `run_isd_generate`/`run_isd_generate_for_crop`."""

    json_path: Path


def run_isd_generate(stitched: isis_wac.FramestitchResult, config: TrntestConfig | None = None) -> IsdGenerateResult:
    """Generate a CSM Pushframe ISD (ALE's `isd_generate`) for the *stitched* cube.

    :param stitched: Full, uncropped stitched cube (`isis_wac.run_framestitch`'s output). Not valid
        for a cropped cube -- see `run_isd_generate_for_crop`.
    :param config: Project config; `load_config()` if not given.
    :returns: An `IsdGenerateResult` for the generated (or already-cached) ISD JSON.
    """
    # `-i` (`--only_isis_spice`) reads pointing/timing directly from the label `run_spiceinit` already
    # embedded, per-parity, before `framestitch` -- `framestitch`'s merge carries those groups through
    # intact: the resulting ISD's geometry/timing parameters (`interframe_delay`, the 259-sample
    # pointing table, etc.) come out identical whether generated from this stitched cube or a single
    # unstitched parity alone (see docs/external-tools.md's "ISIS Pushframe pipeline" section).
    # Despite that, which cube you actually reproject through this ISD matters a great deal -- see
    # `run_mapproject`'s docstring.
    #
    # Patches the ISD's `framelet_order_reversed` to match `stitched.flip`: `isd_generate` always
    # emits `false` here regardless of the cube's actual content -- it doesn't read `framestitch`'s own
    # `DataFlipped` label field, which does correctly record `FLIP=TRUE`/`FALSE`. Left at the wrong
    # (always-`false`) default, `mapproject` assigns each framelet the wrong pose whenever `flip=True`
    # was actually used (any mirrored/`k=3` pass), producing severe venetian-blind-style banding at
    # every framelet boundary; the correct value eliminates it. Two other ISD fields were also tested
    # and ruled out as unrelated: `framelets_flipped` (zero effect on `mapproject`'s output,
    # byte-for-byte, on a fixed output grid) and a uniform per-framelet internal line-order flip
    # applied directly to the pixel data (made the banding worse, introducing new ghosting).
    #
    # Only valid for the full, uncropped stitched cube -- generating one via this same
    # `isd_generate -i` call directly against a cropped cube gives wrong geometry, traced to a bug in
    # `usgscsm`'s `groundToImage` (see isis_wac.py's module docstring) rather than anything fixable in
    # the ISD itself. `isis_wac.crop_for_camera`'s WAC crop no longer uses an ISD at all -- see
    # `isis_wac.run_cam2map_for_crop`.
    #
    # Idempotent (matching isis_wac.py's usual convention, e.g. `crop_for_camera`): reuses the file on
    # disk if it already exists, rather than re-running `isd_generate` -- an expensive call (~240s for
    # this project's own crop, dominating `resolve_ground_to_image_model`'s total runtime on every
    # notebook re-run, even though its own output -- which Pushframe-vs-other `name_model` this
    # instrument resolves to -- never changes for a fixed product).
    config = config or load_config()
    json_path = stitched.cub_path.with_suffix(".json")
    if json_path.exists():
        return IsdGenerateResult(json_path=json_path)
    run_quiet(["isd_generate", "-i", str(stitched.cub_path), "-o", str(json_path)])
    with open(json_path) as f:
        isd = json.load(f)
    isd["framelet_order_reversed"] = stitched.flip
    with open(json_path, "w") as f:
        json.dump(isd, f)
    return IsdGenerateResult(json_path=json_path)


def run_mapproject(
    stitched: isis_wac.FramestitchResult,
    isd: IsdGenerateResult,
    dem_ortho_result: DemOrthoResult,
    config: TrntestConfig | None = None,
) -> Path:
    """Reproject the ISIS-processed WAC cube back onto the map via its CSM/ISD sidecar.

    Not used today: depends on `usgscsm`'s Pushframe `groundToImage`, which has an unreliable secant
    search over framelet index (see isis_wac.py's module docstring). Preferable to
    `isis_wac.run_cam2map_for_crop` once that's fixed upstream -- reprojects through a portable CSM
    ISD rather than ISIS's own native camera model.

    :param stitched: The stitched (interleaved) cube -- not a lone even/odd parity in isolation.
    :param isd: `run_isd_generate`'s output for `stitched`.
    :param dem_ortho_result: DEM/ortho pair whose grid this reprojects onto (`--ref-map`).
    :param config: Project config; `load_config()` if not given.
    :returns: Path to the reprojected GeoTIFF.
    """
    # `render.run_mapproject_image` is the same low-level worker the synthetic render's own mapproject
    # step uses, so both land on the exact same DEM grid.
    #
    # Must be run against the stitched cube, not a lone parity: WAC only writes pixel data to
    # alternating nominal frame slots (each parity cube is ~50% populated, strictly alternating -- not
    # a same-frame split like interlaced video fields, as might be assumed from the name).
    # Mapprojecting one parity alone leaves `mapproject` to resample across that sparsity, producing
    # severe venetian-blind-style smearing -- previously (wrongly) attributed to a fundamental CSM
    # Pushframe modeling limitation "not fully mature... artifacts at framelet borders" (see
    # docs/external-tools.md's "ISIS Pushframe pipeline" section). Mapprojecting the
    # properly-interleaved stitched cube instead resolves the vast majority of it: measured 31% valid
    # coverage with no recognizable terrain -> 81% valid coverage with craters visible throughout, same
    # product, same DEM.
    #
    # Not fully accurate even at full-cube size: `usgscsm`'s `groundToImage` -- which this ultimately
    # calls into, once per output pixel -- has a size-dependent self-consistency weakness (see
    # isis_wac.py's module docstring). ISIS's own native reprojection of this same cube agrees with
    # itself (crop vs. full) to 0.9999986 correlation, but only agrees with this function's output at
    # ~0.2-0.4 -- the `usgscsm` bug this function's own docstring points to.
    config = config or load_config()
    mapproj_tif = stitched.cub_path.with_name(stitched.cub_path.stem + "-mapproj.tif")
    return render.run_mapproject_image(stitched.cub_path, isd.json_path, mapproj_tif, dem_ortho_result, config)


def run_isd_generate_for_crop(
    crop: isis_wac.CropResult, camera: Camera, flip: bool, config: TrntestConfig | None = None
) -> IsdGenerateResult:
    """Generate a CSM Pushframe ISD for `crop` itself, not the full stitched cube.

    :param crop: Cropped cube (`isis_wac.crop_for_camera`'s output).
    :param camera: Camera whose crop window (`isis_wac.crop_window_for_camera`) determines the time
        offset.
    :param flip: Written into the ISD's `framelet_order_reversed`.
    :param config: Project config; `load_config()` if not given.
    :returns: An `IsdGenerateResult` for the crop-sized ISD.
    """
    # So the resulting JSON's image dimensions/frame count are read from, and correctly reflect, the
    # crop's real size, for `trn_dataset.TrnTestCropImage`'s sidecar (see docs/external-tools.md's
    # "The crop ISD sidecar's real accuracy" section). Not a substitute for `run_isd_generate`'s
    # full-cube ISD, and not usable for actual reprojection -- like any Pushframe ISD in this codebase,
    # `usgscsm`'s `groundToImage` isn't reliable enough for that (see isis_wac.py's module docstring);
    # ground<->image lookups still go through `resolve_ground_to_image_model`/`ground_to_image_pixel`,
    # unaffected by any of this. This exists purely so the sidecar sitting next to `crop.cub` accurately
    # describes that same cube, on principle, not a differently-sized one.
    #
    # ISIS's `crop` app (even with its default `PROPSPICE=true`) does not re-anchor a Pushframe cube's
    # per-line pointing cache to the crop's new first line (see docs/external-tools.md's
    # "`isd_generate -i` on an ISIS-`crop`ped Pushframe cube" entry): a naive `isd_generate -i` against
    # `crop.cub_path` produces a wrong-but-plausible-looking ISD whose
    # `starting_ephemeris_time`/`ending_ephemeris_time`/`center_ephemeris_time` and
    # `instrument_pointing.ck_table_start_time`/`ck_table_end_time` all read as if the crop still
    # started at the original, pre-crop cube's first line -- even though `ck_table_original_size` (also
    # under `instrument_pointing`) is correctly updated to the cropped line count. The underlying
    # `instrument_pointing.ephemeris_times`/`quaternions`/`angular_velocities` arrays are untouched by
    # `crop` and still hold the entire pass's absolute-time-tagged samples, so shifting just the 5
    # scalar time fields above by the crop's own time offset is sufficient (same entry, above).
    #
    # `line_offset` -- how many lines into the stitched cube `crop.cub_path` actually starts -- comes
    # from `isis_wac.crop_window_for_camera(camera).row_off`, the exact same window
    # `isis_wac.crop_for_camera` itself cropped to.
    config = config or load_config()
    json_path = crop.cub_path.with_suffix(".json")
    run_quiet(["isd_generate", "-i", str(crop.cub_path), "-o", str(json_path)])
    with open(json_path) as f:
        isd = json.load(f)

    line_offset = isis_wac.crop_window_for_camera(camera).row_off
    time_offset_s = (line_offset / isis_wac.VIS_BLOCK_HEIGHT) * isd["interframe_delay"]
    for key in ("starting_ephemeris_time", "ending_ephemeris_time", "center_ephemeris_time"):
        isd[key] += time_offset_s
    for key in ("ck_table_start_time", "ck_table_end_time"):
        isd["instrument_pointing"][key] += time_offset_s
    isd["framelet_order_reversed"] = flip

    with open(json_path, "w") as f:
        json.dump(isd, f)
    return IsdGenerateResult(json_path=json_path)


def ground_point_at_pixel(cub_path: Path, sample: float, line: float) -> tuple[float, float]:
    """Image-to-ground lookup via ISIS's own `campt`, against the cube's embedded camera model.

    :param cub_path: Cube to query.
    :param sample: Image sample (1-based).
    :param line: Image line (1-based).
    :returns: `(lon_deg, lat_deg)` (`PositiveEast360Longitude`/`PlanetocentricLatitude`).
    """
    # The reverse direction of `ground_to_image_pixel`. `allowoutside=true`: unlike
    # `ground_to_image_pixel`'s use case (does a chosen ground point actually land in the crop?), here
    # the pixel is already known to be a coordinate in `cub_path`'s own cube -- no "did this even land
    # inside the image" question to answer, so no need for a failure signal.
    #
    # Not run through `run_quiet` -- like `isis_wac._catlab`, this call's entire point is its stdout on
    # success, which `run_quiet` discards; failure still prints stdout/stderr before raising, same as
    # `run_quiet` does, so a `campt` diagnostic isn't lost here (this sits on `camera.build_camera()`'s
    # boresight re-aim path, not just a debug/QA one).
    result = subprocess.run(
        [
            "campt",
            f"from={cub_path}",
            "type=image",
            f"sample={sample}",
            f"line={line}",
            "format=pvl",
            "allowoutside=true",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="")
        result.check_returncode()
    ground_point = pvl.loads(result.stdout)["GroundPoint"]
    return float(ground_point["PositiveEast360Longitude"]), float(ground_point["PlanetocentricLatitude"])


def ephemeris_time_at_pixel(cub_path: Path, sample: float, line: float) -> float:
    """SPICE ephemeris time (seconds past J2000) `campt` resolves for a given image pixel.

    :param cub_path: Cube to query.
    :param sample: Image sample (1-based).
    :param line: Image line (1-based).
    :returns: Ephemeris time, seconds past J2000.
    """
    # Same `campt` call as `ground_point_at_pixel`, just reading `EphemerisTime` instead of
    # `GroundPoint`'s lon/lat. Used by `wac_camera_model.calibrate_et_per_crop_line` to empirically
    # calibrate a crop cube's own line-to-ET relationship (two queries, not a hand-derived
    # `isis_wac.crop_window_for_camera` row-offset/flip calculation) -- see that function's docstring.
    result = subprocess.run(
        [
            "campt",
            f"from={cub_path}",
            "type=image",
            f"sample={sample}",
            f"line={line}",
            "format=pvl",
            "allowoutside=true",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(pvl.loads(result.stdout)["GroundPoint"]["EphemerisTime"].value)


def cube_serial_number(cub_path: Path) -> str:
    """`cub_path`'s ISIS Serial Number, via `getsn`.

    :param cub_path: Cube to query.
    :returns: The serial number string.
    """
    # The identifier a control network measure uses to say which cube it belongs to
    # (`control_network.write_control_network`). `getsn` returns the literal string `"Unknown"`, not a
    # mission-specific SN, for every product tried on this project's stitched/cropped WAC cubes -- the
    # Archive group looks complete (`ProductId`/`OrbitNumber`/etc. all present), so this is presumably
    # WAC-VIS's own SN translation table expecting a label field this project's `framestitch`->`crop`
    # chain doesn't preserve, not a missing-data bug on this project's side. Not treated as an error: a
    # single-image control network only has one cube in play, so `"Unknown"` is unambiguous by
    # construction as long as it's used consistently for that same cube everywhere (which it is here,
    # since it's re-derived from the same `getsn` call rather than hardcoded) -- `jigsaw` resolves the
    # same cube to the same SN itself when it opens it, so the mapping still lines up correctly even
    # though the string isn't a meaningful mission identifier.
    result = subprocess.run(["getsn", f"from={cub_path}"], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@dataclasses.dataclass(frozen=True)
class GroundToImageModel:
    """Which camera-model authority `ground_to_image_pixel` should query for a given crop, and why --
    see `resolve_ground_to_image_model`."""

    cub_path: Path
    name_model: str
    used_csm: bool


def resolve_ground_to_image_model(
    stitched: isis_wac.FramestitchResult, crop: isis_wac.CropResult, config: TrntestConfig | None = None
) -> GroundToImageModel:
    """Resolve which camera-model authority ground-to-image queries should go through for this crop.

    :param stitched: Full stitched cube -- `run_isd_generate` is only valid there, not on a cropped
        cube.
    :param crop: The cropped cube ground-to-image queries will actually run against.
    :param config: Project config; `load_config()` if not given.
    :returns: A `GroundToImageModel` naming the resolved authority.
    """
    # Used by `tie_points.resolve_crop_pixels` -- the same resolution order
    # `isis_wac.run_cam2map_for_crop` already settled on for the DEM-reprojection path, generalized
    # into reusable logic instead of a one-off decision: (1) try building a CSM ISD sidecar for the
    # full stitched cube and inspect its `name_model`; (2) if it resolves to a Pushframe sensor,
    # `usgscsm`'s `groundToImage` is known unreliable for that class of camera (see isis_wac.py's
    # module docstring) -- fall back to the crop's own native, SPICE-embedded camera model, queried
    # directly (no CSM/ISD involved); (3) otherwise, the CSM model is safe to use -- attach it to a
    # private copy of the crop via ISIS's own `csminit`, so the crop's own native-model queries
    # elsewhere (e.g. `isis_wac.run_cam2map_for_crop`) aren't affected by this copy's attached CSM
    # state.
    #
    # Not hardcoded to "WAC-VIS is Pushframe, always use the native model" -- for this project's WAC
    # product it always resolves that way (`run_isd_generate`'s ISD reports
    # `name_model = "USGS_ASTRO_PUSH_FRAME_SENSOR_MODEL"`), but deriving it from an ISD each call keeps
    # this correct if this pipeline is ever pointed at a different, non-Pushframe instrument, rather
    # than baking today's answer in as a permanent assumption.
    config = config or load_config()
    isd = run_isd_generate(stitched, config)
    name_model = json.loads(isd.json_path.read_text())["name_model"]
    if "PUSH_FRAME" in name_model:
        return GroundToImageModel(cub_path=crop.cub_path, name_model=name_model, used_csm=False)

    csm_cub_path = crop.cub_path.with_name(crop.cub_path.stem + ".csm.cub")
    shutil.copy(crop.cub_path, csm_cub_path)
    run_quiet(["csminit", f"from={csm_cub_path}", f"isd={isd.json_path}"])
    return GroundToImageModel(cub_path=csm_cub_path, name_model=name_model, used_csm=True)


def ground_to_image_pixel(model: GroundToImageModel, lon_deg: float, lat_deg: float) -> tuple[float, float] | None:
    """Ground-to-image lookup via ISIS's own `campt`, against whichever cube/camera-model
    `resolve_ground_to_image_model` decided is authoritative.

    :param model: Resolved camera-model authority (`resolve_ground_to_image_model`'s output).
    :param lon_deg: Ground point longitude, degrees.
    :param lat_deg: Ground point latitude, degrees.
    :returns: `(sample, line)` in ISIS's own 1-based, pixel-center convention, or `None` if the ground
        point doesn't project into this cube.
    """
    # A ground-truth query through a validated tool, not a hand-derived approximation (see
    # `tie_points.py`'s module docstring for why this replaced a hand-rolled SPICE projection for the
    # WAC crop). `allowoutside=false` gives a clean, distinguishable failure -- "not inside cube" for a
    # point outside the crop's own extent, "no surface intersection" for one outside the camera's view
    # entirely -- rather than silently extrapolating past either boundary.
    result = subprocess.run(
        [
            "campt",
            f"from={model.cub_path}",
            "type=ground",
            f"latitude={lat_deg}",
            f"longitude={lon_deg}",
            "format=pvl",
            "allowoutside=false",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    label = pvl.loads(result.stdout)
    ground_point = label["GroundPoint"]
    return float(ground_point["Sample"]), float(ground_point["Line"])


def campt_photometric_angles(cub_path: Path, lon_deg: float, lat_deg: float) -> tuple[float, float, float] | None:
    """`campt` phase/incidence/emission angles at a given ground point.

    :param cub_path: Cube to query, with a camera model already attached (`csminit`).
    :param lon_deg: Ground point longitude, degrees.
    :param lat_deg: Ground point latitude, degrees.
    :returns: `(phase_deg, incidence_deg, emission_deg)`, or `None` if the ground point doesn't
        project into this cube.
    """
    # The ISIS ground-truth counterpart to this project's own hand-rolled
    # `hapke._terrain_photometric_angles`, used to validate it (see that function's own docstring).
    # Mirrors `ground_to_image_pixel`'s exact PVL-single-point-query pattern (same
    # `type=ground`/`allowoutside=false` convention) rather than the `usecoordlist=true` batched
    # flat-file approach `ground_to_image_pixels_batch` uses -- this project's own validation only ever
    # needs a handful of sparse sample points (not a full raster), so the per-call subprocess overhead
    # doesn't matter enough here to trade away PVL's more directly-verifiable field names for CSV's.
    #
    # `cub_path`'s camera model determines whether these are ellipsoid-based or DEM-aware ("local")
    # angles -- `campt` has no separate `local*` output names the way `phocube` does; it just reports
    # whatever its attached shape model (or lack of one) resolves to.
    result = subprocess.run(
        [
            "campt",
            f"from={cub_path}",
            "type=ground",
            f"latitude={lat_deg}",
            f"longitude={lon_deg}",
            "format=pvl",
            "allowoutside=false",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    ground_point = pvl.loads(result.stdout)["GroundPoint"]
    return (
        float(ground_point["Phase"]),
        float(ground_point["Incidence"]),
        float(ground_point["Emission"]),
    )


def ground_to_image_pixels_batch(model: GroundToImageModel, lonlat_deg: np.ndarray) -> list[tuple[float, float] | None]:
    """Batched ground-to-image lookup for many points at once, via a single `campt usecoordlist=true`
    call.

    :param model: Resolved camera-model authority (`resolve_ground_to_image_model`'s output).
    :param lonlat_deg: `(N, 2)`, `(lon_deg, lat_deg)` columns.
    :returns: A list the same length and order as `lonlat_deg`'s rows -- `None` for any point that
        doesn't project into `model.cub_path` (matching `ground_to_image_pixel`'s `None`-on-failure
        contract), `(sample, line)` otherwise.
    :raises RuntimeError: If `campt` returns a different row count than input points.
    """
    # Instead of one `ground_to_image_pixel` subprocess per point: each individual `campt` call pays
    # process-spawn/SPICE-load overhead (~300ms observed), which dominates wall-clock for a
    # multi-hundred-point control network (e.g. 767 points -> ~230s of subprocess overhead alone,
    # collapsed to a single call here) -- the dominant real cost of
    # `control_network.resolve_control_points`.
    #
    # `lonlat_deg` columns are reordered to `(latitude, longitude)` only for the COORDLIST file, since
    # `campt.xml` documents that exact, different column order for `COORDTYPE=ground`.
    #
    # `allowerror=true` lets `campt` continue past an individual point that fails to project rather
    # than aborting the whole batch. A failed row's own `Sample`/`Line` fields come back as a stale,
    # meaningless carryover from the last successful row in the batch, never `NULL`/absent -- so
    # failure is only ever detected via that row's own `Error` field, which is the literal string
    # `"NULL"` on success and an error message otherwise (e.g. "Requested position does not project in
    # camera model; no surface intersection"). `append=false` is required -- `campt`'s own default
    # (`APPEND=TRUE`) silently prepends this run's results after any stale content already at `to=`'s
    # path, hence the fresh `tempfile` dir.
    lonlat_deg = np.asarray(lonlat_deg)
    with tempfile.TemporaryDirectory() as tmp_dir:
        coordlist_path = Path(tmp_dir) / "coordlist.csv"
        out_path = Path(tmp_dir) / "campt_out.flat"
        with open(coordlist_path, "w") as f:
            for lon_deg, lat_deg in lonlat_deg:
                f.write(f"{lat_deg},{lon_deg}\n")

        run_quiet(
            [
                "campt",
                f"from={model.cub_path}",
                "usecoordlist=true",
                f"coordlist={coordlist_path}",
                "coordtype=ground",
                f"to={out_path}",
                "format=flat",
                "append=false",
                "allowoutside=false",
                "allowerror=true",
            ]
        )
        with open(out_path, newline="") as f:
            rows = list(csv.DictReader(f))

    if len(rows) != len(lonlat_deg):
        raise RuntimeError(
            f"campt usecoordlist returned {len(rows)} rows for {len(lonlat_deg)} input points -- "
            "expected exactly one row per point"
        )
    return [None if row["Error"] != "NULL" else (float(row["Sample"]), float(row["Line"])) for row in rows]


def image_to_ground_points_batch(
    cub_path: Path, pixels_sample_line: np.ndarray
) -> list[tuple[float, float, float] | None]:
    """Batched image-to-ground lookup for many pixels at once, via a single `campt usecoordlist=true`
    call.

    :param cub_path: Cube to query.
    :param pixels_sample_line: `(N, 2)`, `(sample, line)` columns, ISIS's own 1-based pixel-center
        convention.
    :returns: A list the same length and order as `pixels_sample_line`'s rows -- `None` for any row
        `campt` reports an error for, `(lon_deg, lat_deg, radius_m)` otherwise
        (`PositiveEast360Longitude`/`PlanetocentricLatitude`/`LocalRadius`).
    :raises RuntimeError: If `campt` returns a different row count than input pixels.
    """
    # The reverse-direction sibling of `ground_to_image_pixels_batch` (same subprocess-overhead
    # motivation, see that function's own docstring), and, unlike `ground_point_at_pixel`, also
    # returns each point's `LocalRadius` -- needed to build a true 3D ground point (not just
    # `(lon, lat)`) for a ground-space (not pixel-space) residual comparison. Ground-space is the only
    # legitimate metric for this project's actual 3D control points: converting `wac_camera_model`'s
    # own forward-predicted pixel back to ground and comparing that to the trusted ground point would
    # just re-litigate which framelet is "right" in an overlap band (see
    # `wac_camera_model.find_framelet_and_project`'s own docstring); this function instead only ever
    # queries a pixel that's already been resolved by some other process (never searches for one
    # itself), so there's nothing to litigate -- one pixel has exactly one ground point.
    #
    # Written to the COORDLIST file as `sample, line` (`campt.xml`'s own doc: "Expected order for image
    # coordinates: sample, line") -- opposite of `ground_to_image_pixels_batch`'s own
    # `latitude, longitude` convention for `coordtype=ground`.
    #
    # Every pixel here is expected to already be a valid coordinate in `cub_path`'s own cube (it came
    # from some prior, already-successful resolution) -- `allowerror=true` is still used defensively,
    # matching this module's usual convention, but a failure here would be a genuine surprise, not an
    # expected edge case the way it is in `ground_to_image_pixels_batch`.
    pixels_sample_line = np.asarray(pixels_sample_line)
    with tempfile.TemporaryDirectory() as tmp_dir:
        coordlist_path = Path(tmp_dir) / "coordlist.csv"
        out_path = Path(tmp_dir) / "campt_out.flat"
        with open(coordlist_path, "w") as f:
            for sample, line in pixels_sample_line:
                f.write(f"{sample},{line}\n")

        run_quiet(
            [
                "campt",
                f"from={cub_path}",
                "usecoordlist=true",
                f"coordlist={coordlist_path}",
                "coordtype=image",
                f"to={out_path}",
                "format=flat",
                "append=false",
                "allowerror=true",
            ]
        )
        with open(out_path, newline="") as f:
            rows = list(csv.DictReader(f))

    if len(rows) != len(pixels_sample_line):
        raise RuntimeError(
            f"campt usecoordlist returned {len(rows)} rows for {len(pixels_sample_line)} input pixels -- "
            "expected exactly one row per pixel"
        )
    return [
        None
        if row["Error"] != "NULL"
        else (
            float(row["PositiveEast360Longitude"]),
            float(row["PlanetocentricLatitude"]),
            float(row["LocalRadius"]),
        )
        for row in rows
    ]
