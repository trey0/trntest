"""ISIS3 real-WAC reprojection -- steps a real WAC EDR through ISIS's own pipeline
(`lrowac2isis` -> `spiceinit web=yes` -> `lrowaccal` -> `framestitch`) as a genuine-camera-model
alternative to `wac.py`'s manual framelet-stacking, then (`crop_for_camera`/`run_cam2map_for_crop`)
reprojects the cropped result onto the map via ISIS's own *native* Pushframe camera model and
`cam2map` -- not ALE's CSM ISD + ASP's `mapproject`, which `render.py` uses for the synthetic
render. See docs/data-sources.md's "ISIS3/CSM spike" section and docs/history.md's Phases 12/19/24
for the full backstory, and the dated entry documenting *why* the CSM path was abandoned for this
specific use: `usgscsm`'s `UsgsAstroPushFrameSensorModel::groundToImage` (the function ASP's
`mapproject` calls once per output pixel, via `CsmModel::point_to_pixel`) does an unbracketed
secant search over framelet index that's measurably unreliable for Pushframe images -- severely so
for a short crop (confirmed via `cam_test` round-trip self-consistency: ~67px median error on a
70-framelet crop vs. ~17px on the full 258-framelet cube, and a chained-iteration test showing the
crop case never converges to a stable fixed point at all), and non-negligibly even on the *full*
cube (confirmed: `cam2map`'s output on the crop agrees with `cam2map`'s output on the full cube to
0.9999986 correlation over their full overlap, while the old ASP/CSM full-cube reference only
agrees with either at ~0.2-0.4). ISIS's own native camera model has no such issue (confirmed via
`campt` at the crop's center and all 4 edges, and `cam2map`'s own output contiguity) -- it reads
pointing/timing directly from the cube's own cached SPICE data, with no separate ISD/sidecar file
needed at all. `run_isd_generate`/`run_mapproject` (the CSM path) are kept below for reference/
comparison, but are no longer used by the notebook.

**Known residual, left undiagnosed-fix (documented, not chased further)**: even with the fixes
above, the crop images ground truth ~11km from `tie_points.crop_footprint_corners_for_camera`'s
independently ray-traced expectation, at the exact matched instant -- not a `crop_for_camera`
frame-selection bug (verified via `campt`), but a genuine pointing discrepancy between this
project's own SPICE computation (`camera.camera_pose_moon_me`) and ISIS's own camera model. Traced
to a CK kernel (`moc42r_*.bc`) ISIS's `spiceinit web=yes` furnishes that `spice_kernels.py` doesn't
fetch (and isn't in the NAIF metakernel it already parses) -- see docs/history.md's dated entry.

Only the VIS cubes are touched (`vis.even`/`vis.odd`); the UV cubes are irrelevant to a
VIS-striping investigation.

House style matches render.py: frozen dataclass results holding `Path`s, `config = config or
load_config()`, subprocess calls via the shared `run_quiet` helper (not raw `subprocess.run`).
"""

import dataclasses
import json
from pathlib import Path

import rasterio
import rasterio.windows

from trntest import cache, render
from trntest.camera import Camera, FrameTiming
from trntest.config import TrntestConfig, load_config
from trntest.lunaserv import LunaservResult
from trntest.subprocess_utils import run_quiet
from trntest.wac import SAMPLES, VIS_BLOCK_HEIGHT

_BASE_KERNEL_INCLUDE = "{kernels/lsk/**,kernels/pck/**,kernels/sclk/**,kernels/fk/**,kernels/ik/**,kernels/iak/**}"


def ensure_isisdata(config: TrntestConfig | None = None) -> None:
    """Lazily fetch the ISIS reference data this pipeline needs, if not already present.

    Corrected from an earlier assumption (see docs/data-sources.md's "ISIS3/CSM spike" section for
    the original claim, now corrected below): `downloadIsisData base $ISISDATA --no-kernels` does
    NOT shrink `base` to near-zero -- `--no-kernels` only excludes the ck/ek/fk/ik/iak/lsk/mk/pck/
    sclk/spk/tspk/dsk kernel subdirs, and `base`'s ~20GB is dominated by `dems/` (global shape
    models), which isn't a "kernel" in that sense and isn't touched by the flag at all. None of
    that DEM data is needed for this notebook's scope (`mapproject` isn't reached yet), and
    `spiceinit web=yes` still needs a few tiny, generic, mission-independent kernels locally (LSK
    for time conversion at minimum, confirmed by a real failure: "Unable to load leadsecond file"
    when `base/kernels/lsk` was empty) even though it doesn't need the bulky per-date CK/SPK ones.
    So: fetch only those small kernel subdirs from `base` via `--include` (skips `dems/`/
    `examples/`/`kernelTesting/` entirely), plus the full `lro` calibration tree (still `--no-kernels`,
    that part of the original claim held -- confirmed ~5GB, no per-date kernels needed there either)."""
    config = config or load_config()
    isisdata = config.cache_root / "isisdata"
    if (isisdata / "base" / "kernels" / "lsk").is_dir():
        return
    isisdata.mkdir(parents=True, exist_ok=True)
    run_quiet(["downloadIsisData", "base", str(isisdata), "--include", _BASE_KERNEL_INCLUDE])
    run_quiet(["downloadIsisData", "lro", str(isisdata), "--no-kernels"])


@dataclasses.dataclass(frozen=True)
class EdrFetchResult:
    img_path: Path


def fetch_edr_img(config: TrntestConfig | None = None) -> EdrFetchResult:
    """Fetch the EDR product's own `.IMG` pixel data (not its `.xml` label, which
    `camera.fetch_frame_timing()` already fetches, and not the CDR `.IMG`, which
    `wac.fetch_vis_mosaic()` already fetches) -- `lrowac2isis` needs the EDR."""
    config = config or load_config()
    img_path = cache.fetch_lroc_file(
        config.lroc_edr_dataset,
        config.edr_volume,
        config.edr_subdir,
        config.edr_doy,
        config.edr_product,
        "IMG",
        cache_root=config.cache_root,
        base_url=config.lroc_base_url,
    )
    return EdrFetchResult(img_path=img_path)


def _spike_dir(config: TrntestConfig) -> Path:
    d = config.scratch_dir / "isis_wac" / config.edr_product
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclasses.dataclass(frozen=True)
class Lrowac2IsisResult:
    uv_even: Path
    vis_even: Path
    uv_odd: Path
    vis_odd: Path


def run_lrowac2isis(edr: EdrFetchResult, config: TrntestConfig | None = None) -> Lrowac2IsisResult:
    config = config or load_config()
    out_prefix = _spike_dir(config) / edr.img_path.stem
    run_quiet(["lrowac2isis", f"from={edr.img_path}", f"to={out_prefix}"])
    return Lrowac2IsisResult(
        uv_even=out_prefix.with_name(out_prefix.name + ".uv.even.cub"),
        vis_even=out_prefix.with_name(out_prefix.name + ".vis.even.cub"),
        uv_odd=out_prefix.with_name(out_prefix.name + ".uv.odd.cub"),
        vis_odd=out_prefix.with_name(out_prefix.name + ".vis.odd.cub"),
    )


@dataclasses.dataclass(frozen=True)
class SpiceinitResult:
    cub_path: Path  # spiceinit edits the label in place -- no new file


def run_spiceinit(cub_path: Path, config: TrntestConfig | None = None) -> SpiceinitResult:
    """`shape=ellipsoid` overrides ISIS's default (`SHAPE=*SYSTEM`), which resolves to a real lunar
    DSK/DEM cube (e.g. `$base/dems/ldem_128ppd_Mar2011_clon180_radius_pad.cub`) -- confirmed via a
    real failure ("USER ERROR NAIF DSK file [...] does not exist") against `ensure_isisdata()`'s
    deliberately dems/-free minimal fetch (see its docstring: `base`'s ~20GB is dominated by
    `dems/`, skipped on purpose). This module's scope stops at `framestitch` (no `isd_generate`/
    `mapproject` precision-terrain step yet -- see the module docstring), so the simple reference
    ellipsoid is sufficient here; revisit this if/when real terrain intersection is added."""
    config = config or load_config()
    run_quiet(["spiceinit", f"from={cub_path}", "web=yes", "shape=ellipsoid"])
    return SpiceinitResult(cub_path=cub_path)


@dataclasses.dataclass(frozen=True)
class LrowaccalResult:
    cub_path: Path


def run_lrowaccal(spiceinit_result: SpiceinitResult, config: TrntestConfig | None = None) -> LrowaccalResult:
    config = config or load_config()
    in_path = spiceinit_result.cub_path
    out_path = in_path.with_name(in_path.stem + ".cal.cub")
    run_quiet(["lrowaccal", f"from={in_path}", f"to={out_path}"])
    return LrowaccalResult(cub_path=out_path)


@dataclasses.dataclass(frozen=True)
class FramestitchResult:
    cub_path: Path
    flip: bool  # the FLIP value framestitch was actually run with -- see run_isd_generate's docstring


def run_framestitch(
    even: LrowaccalResult,
    odd: LrowaccalResult,
    flip: bool,
    config: TrntestConfig | None = None,
) -> FramestitchResult:
    """Combine even/odd parity cubes into one stitched cube. `flip` is a real, per-pass manual
    decision that must match `camera.boresight_rotation_k`'s sign for this product (see
    docs/data-sources.md) -- not derived automatically by ISIS.

    Parameter names (`EVEN`/`ODD`/`TO`/`FLIP`, uppercase; ISIS params are case-insensitive but this
    matches `framestitch -help`'s own spelling) confirmed against a real built image -- `-help`
    doesn't document `FRAMEHEIGHT`/`NUM_LINES_OVERLAP` beyond their `Null` defaults, so those are
    left unset here (ISIS auto-computes when left `Null`, per its own convention) unless real runs
    show that's wrong."""
    config = config or load_config()
    out_path = even.cub_path.with_name(even.cub_path.stem.replace(".even", "") + ".stitched.cub")
    run_quiet(
        [
            "framestitch",
            f"EVEN={even.cub_path}",
            f"ODD={odd.cub_path}",
            f"TO={out_path}",
            f"FLIP={'TRUE' if flip else 'FALSE'}",
        ]
    )
    return FramestitchResult(cub_path=out_path, flip=flip)


def run_pipeline(camera: Camera, frame_timing: FrameTiming, config: TrntestConfig | None = None) -> FramestitchResult:
    """Runs the full EDR-fetch-through-`framestitch` pipeline for the product `camera`/
    `frame_timing` describe. `flip` is derived from `camera.reverse_crop_along_track` -- the same
    real, SPICE-derived per-pass yaw-state signal `framestitch`'s FLIP needs to match (confirmed
    twice in the original spike, on two products with opposite yaw states) -- not hardcoded
    per-product like the spike notebook's own `FLIP = False`."""
    config = config or load_config()
    ensure_isisdata(config)
    edr = fetch_edr_img(config)
    split = run_lrowac2isis(edr, config)
    even = run_lrowaccal(run_spiceinit(split.vis_even, config), config)
    odd = run_lrowaccal(run_spiceinit(split.vis_odd, config), config)
    return run_framestitch(even, odd, flip=camera.reverse_crop_along_track, config=config)


@dataclasses.dataclass(frozen=True)
class IsdGenerateResult:
    json_path: Path


def run_isd_generate(stitched: FramestitchResult, config: TrntestConfig | None = None) -> IsdGenerateResult:
    """Generate a CSM Pushframe ISD (ALE's `isd_generate`) for the *stitched* cube. `-i`
    (`--only_isis_spice`) reads pointing/timing directly from the label `run_spiceinit` already
    embedded, per-parity, before `framestitch` -- confirmed empirically that `framestitch`'s merge
    carries those groups through intact: the resulting ISD's geometry/timing parameters
    (`interframe_delay`, the 259-sample pointing table, etc.) come out identical whether generated
    from this stitched cube or a single unstitched parity alone (see docs/data-sources.md). Despite
    that, which cube you actually reproject through this ISD matters a great deal -- see
    `run_mapproject`'s docstring.

    **Patches the ISD's `framelet_order_reversed` to match `stitched.flip`** -- `isd_generate`
    always emits `false` here regardless of the cube's actual content (confirmed empirically: it
    doesn't read `framestitch`'s own `DataFlipped` label field, which *does* correctly record
    `FLIP=TRUE`/`FALSE`). Left at the wrong (always-`false`) default, `mapproject` assigns each
    framelet the wrong pose whenever `flip=True` was actually used (any mirrored/`k=3` pass) --
    confirmed empirically via a real product: severe venetian-blind-style banding at every framelet
    boundary with the wrong value, gone entirely with the correct one (see docs/history.md's dated
    entry). Two other ISD fields were also tested and ruled out as unrelated: `framelets_flipped`
    (rigorously confirmed zero effect on `mapproject`'s output, byte-for-byte, on a fixed output
    grid) and a uniform per-framelet internal line-order flip applied directly to the pixel data
    (made the banding worse, introducing new ghosting).

    Only valid for the *full*, uncropped stitched cube -- generating one via this same
    `isd_generate -i` call directly against a cropped cube was tried and found to give wrong
    geometry, which further investigation traced to a real bug in `usgscsm`'s `groundToImage`
    (see the module docstring) rather than anything fixable in the ISD itself. `crop_for_camera`'s
    real WAC crop no longer uses an ISD at all -- see `run_cam2map_for_crop`."""
    config = config or load_config()
    json_path = stitched.cub_path.with_suffix(".json")
    run_quiet(["isd_generate", "-i", str(stitched.cub_path), "-o", str(json_path)])
    with open(json_path) as f:
        isd = json.load(f)
    isd["framelet_order_reversed"] = stitched.flip
    with open(json_path, "w") as f:
        json.dump(isd, f)
    return IsdGenerateResult(json_path=json_path)


def run_mapproject(
    stitched: FramestitchResult,
    isd: IsdGenerateResult,
    lunaserv_result: LunaservResult,
    config: TrntestConfig | None = None,
) -> Path:
    """Reproject the real, ISIS-processed WAC cube back onto the map via its own CSM/ISD sidecar
    (`run_isd_generate`) -- `render.run_mapproject_image` is the same low-level worker the synthetic
    render's own mapproject step uses, so both land on the exact same DEM grid (`--ref-map`).

    **Must be run against the stitched (interleaved) cube -- `stitched`, not a lone even/odd parity
    in isolation.** Confirmed empirically (see docs/data-sources.md's "ISIS3/CSM spike" section):
    WAC only writes real pixel data to alternating nominal frame slots (each parity cube is ~50%
    populated, strictly alternating -- not a same-frame split like interlaced video fields, as might
    be assumed from the name). Mapprojecting one parity alone leaves `mapproject` to resample across
    that sparsity, producing severe venetian-blind-style smearing -- previously (wrongly) attributed
    to a fundamental CSM Pushframe modeling limitation "not fully mature... artifacts at framelet
    borders". Mapprojecting the properly-interleaved stitched cube instead resolves the vast
    majority of it: measured 31% valid coverage with no recognizable terrain -> 81% valid coverage
    with real craters visible throughout, same real product, same DEM.

    **Not fully accurate even at full-cube size.** Later investigation (see the module docstring)
    found `usgscsm`'s `groundToImage` -- which this ultimately calls into, once per output pixel --
    has a real, if size-dependent, self-consistency weakness. Confirmed via `cam2map` cross-check:
    ISIS's own native reprojection of this same cube agrees with itself (crop vs. full) to
    0.9999986 correlation, but only agrees with *this* function's output at ~0.2-0.4. Kept for
    reference/comparison; `run_cam2map_for_crop` is the accurate path now."""
    config = config or load_config()
    mapproj_tif = stitched.cub_path.with_name(stitched.cub_path.stem + "-mapproj.tif")
    return render.run_mapproject_image(stitched.cub_path, isd.json_path, mapproj_tif, lunaserv_result, config)


def crop_window_for_camera(camera: Camera) -> rasterio.windows.Window:
    """The stitched cube preserves 14 lines per original EDR frame (confirmed empirically: cached
    cubes for two real products both measure exactly nframes * 14 lines -- `lrowac2isis` does not
    TDI-sum each frame down to one line, it keeps the same per-frame line structure `wac.py`'s raw
    CDR byte-layout code already assumes) -- `wac.VIS_BLOCK_HEIGHT`, not 1. So both
    `camera.center_frame_index` and `camera.n_frames_for_square_crop` need to be scaled by that
    factor to land on the same real footprint `wac.fetch_vis_mosaic`'s own crop covers."""
    height = camera.n_frames_for_square_crop * VIS_BLOCK_HEIGHT
    center_line = camera.center_frame_index * VIS_BLOCK_HEIGHT
    line_start = round(center_line - height / 2)
    return rasterio.windows.Window(col_off=0, row_off=line_start, width=SAMPLES, height=height)


@dataclasses.dataclass(frozen=True)
class CropResult:
    cub_path: Path


def crop_for_camera(stitched: FramestitchResult, camera: Camera, config: TrntestConfig | None = None) -> CropResult:
    """A single, real, persisted ISIS crop of `stitched` to `crop_window_for_camera(camera)`'s
    window -- the one "WAC crop" output product both Phase 6A (raw display of `.cub_path` directly)
    and Phase 6B (`run_cam2map_for_crop`) consume, matching how Phase 5A/5B already both derive from
    one synthetic render with no special-casing. No separate ISD/sidecar is generated here (unlike
    an earlier version of this function) -- ISIS's own native camera model reads pointing/timing
    directly from the cube's own cached SPICE data, so the cropped cube is already fully
    self-describing; see the module docstring for why the CSM/ISD route was abandoned instead of
    fixed.

    `lrowaccal` (already run before this, in `run_pipeline`) explicitly refuses to run on a cropped
    cube ("USER ERROR: This application can not be run on any image that has been geometrically
    transformed ... or cropped") -- confirmed empirically -- so cropping must happen after
    calibration/`framestitch`, on `stitched`, not earlier in the pipeline."""
    config = config or load_config()
    window = crop_window_for_camera(camera)
    out_path = stitched.cub_path.with_name(stitched.cub_path.stem + ".crop.cub")
    run_quiet(
        [
            "crop",
            f"from={stitched.cub_path}",
            f"to={out_path}",
            f"line={window.row_off + 1}",  # ISIS LINE is 1-based; Window.row_off is 0-based
            f"nlines={window.height}",
        ]
    )
    return CropResult(cub_path=out_path)


def _orthographic_map_pvl(lunaserv_result: LunaservResult) -> str:
    """Builds an ISIS PVL "Mapping" group cloning `lunaserv_result`'s own local Orthographic CRS
    (see `LunaservResult`'s docstring: `config.lunaserv_srs_template`, centered on this camera's own
    footprint) -- same center lat/lon, spherical Moon radius, and pixel resolution -- so
    `cam2map`'s output (`run_cam2map_for_crop`) lands in the *same projected coordinate system* as
    `lunaserv_result.dem`/`.ortho`. Verified empirically (not assumed) that ISIS's own Orthographic
    projection implementation agrees with GDAL/PROJ's `+proj=ortho` to sub-micrometer precision for
    matching center/radius parameters, via `cam2map`+`campt` cross-checked against `pyproj`'s own
    forward projection at a real test pixel (see docs/history.md's dated entry).

    Deliberately does *not* pin `UpperLeftCornerX`/`UpperLeftCornerY` to match
    `lunaserv_result`'s own pixel grid -- `cam2map`'s output is left free to auto-size to the crop's
    own footprint (`DEFAULTRANGE=CAMERA` in `run_cam2map_for_crop`). This is safe because
    `plotting.plot_overlay` composites both rasters via their own real georeferenced coordinates
    (`rioxarray`/`xarray`), not a shared pixel grid -- so the two rasters only need to agree on the
    *projection*, not share pixel-for-pixel alignment. Avoiding a shared-grid requirement also
    avoids needing a separate resampling/warping pass after `cam2map` (which would risk the exact
    kind of interpolation-quality/subtle-misalignment issues this whole detour was trying to avoid
    in the first place)."""
    with rasterio.open(lunaserv_result.dem) as src:
        proj = src.crs.to_dict()
        resolution = src.res[0]
    return (
        "Group = Mapping\n"
        "  ProjectionName     = Orthographic\n"
        f"  CenterLatitude     = {proj['lat_0']} <degrees>\n"
        f"  CenterLongitude    = {proj['lon_0']} <degrees>\n"
        "  TargetName         = Moon\n"
        f"  EquatorialRadius   = {float(proj['R'])} <meters>\n"
        f"  PolarRadius        = {float(proj['R'])} <meters>\n"
        "  LatitudeType       = Planetocentric\n"
        "  LongitudeDirection = PositiveEast\n"
        "  LongitudeDomain    = 360\n"
        f"  PixelResolution    = {resolution} <meters/pixel>\n"
        "End_Group\n"
    )


def run_cam2map_for_crop(
    crop: CropResult, lunaserv_result: LunaservResult, config: TrntestConfig | None = None
) -> Path:
    """Reprojects `crop` onto the map via ISIS's own native `cam2map` -- the real-WAC counterpart to
    5B's `mapproject`, but through ISIS's native Pushframe camera model instead of ASP/CSM (see the
    module docstring for why). `_orthographic_map_pvl` clones `lunaserv_result`'s own projection so
    the output shares the same real-world coordinate system (not pixel grid -- see that function's
    docstring) as `lunaserv_result.ortho`, letting `plotting.plot_overlay` composite them directly.

    `PIXRES=map` is required -- `cam2map`'s `PIXRES` parameter defaults to `CAMERA` (auto-derives
    resolution from the image itself), which *silently ignores* the map file's own `PixelResolution`
    otherwise (confirmed empirically: without it, output resolution came out as the camera's native
    ~184m/px, not the requested ~100m/px). `DEFAULTRANGE=camera` auto-sizes the output extent to the
    crop's own real footprint, matching how `sat_sim`/`mapproject` never render more than the
    camera's own FOV either.

    **`WARPALGORITHM=forwardpatch PATCHSIZE=4`, not the `AUTOMATIC` default.** ISIS's own docs say
    `AUTOMATIC` is "recommended... for push frame cameras" (it picks `FORWARDPATCH` with
    `PATCHSIZE` set to the full framelet height, 14, specifically so a patch never crosses a
    framelet boundary) -- but confirmed empirically this leaves large, real diagonal gaps at this
    map resolution: `AUTOMATIC`'s patches fit an affine transform per patch from its four corners,
    and *silently drop* any patch whose affine fit isn't within 0.1px of the camera model's own
    computation (per `cam2map -help`'s parameter docs) -- a 14-line-tall patch apparently fails that
    check for roughly half the framelets here (confirmed present even on the *full*, uncropped cube,
    not just the crop -- see the module docstring). A much smaller `PATCHSIZE=4` (explicit
    `FORWARDPATCH`, since `PATCHSIZE` is locked/ignored under `AUTOMATIC`) fits each small patch
    accurately enough to pass that check everywhere tested: overall valid coverage went from ~47%
    to ~71% (matching the crop's own real footprint, no more diagonal gaps), while content stayed
    essentially identical (crop-vs-full correlation 0.9954 with this patch size, vs. 0.9999986 at
    the default -- both excellent; the tiny drop is patch-fit noise, not a correctness regression).

    Converts the resulting multi-band cube (WAC VIS carries 5 filter bands) to a single-band GeoTIFF
    via `gdal_translate -b 1`, matching this pipeline's existing band-1 convention
    (`plotting.read_raster_band`'s default, and what ASP's own `mapproject` picked automatically:
    "Detected multi-band image. Only the first band will be used."). `gdal_translate` prints a
    `PROJ: proj_create_from_name` error to stderr here (an ISIS/GDAL `PROJ_LIB` environment mismatch)
    -- confirmed harmless: the output CRS/transform were verified correct (matching
    `lunaserv_result`'s own projection exactly) despite it, and the process still exits 0."""
    config = config or load_config()
    map_path = crop.cub_path.with_suffix(".ortho.map")
    map_path.write_text(_orthographic_map_pvl(lunaserv_result))

    mapproj_cub = crop.cub_path.with_name(crop.cub_path.stem + "-cam2map.cub")
    run_quiet(
        [
            "cam2map",
            f"from={crop.cub_path}",
            f"map={map_path}",
            f"to={mapproj_cub}",
            "pixres=map",
            "defaultrange=camera",
            "warpalgorithm=forwardpatch",
            "patchsize=4",
        ]
    )

    mapproj_tif = crop.cub_path.with_name(crop.cub_path.stem + "-cam2map.tif")
    run_quiet(["gdal_translate", "-b", "1", str(mapproj_cub), str(mapproj_tif)])
    return mapproj_tif
