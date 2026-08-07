"""ISIS3/CSM real-WAC reprojection -- steps a real WAC EDR through ISIS's own pipeline
(`lrowac2isis` -> `spiceinit web=yes` -> `lrowaccal` -> `framestitch`) as a genuine-camera-model
alternative to `wac.py`'s manual framelet-stacking, then (`run_isd_generate`/`run_mapproject`)
reprojects the result onto the map via ALE's CSM Pushframe sensor model, same as `render.py` does
for the synthetic render. See docs/data-sources.md's "ISIS3/CSM spike" section and docs/history.md's
Phases 12/19 for the full backstory and prior findings -- in particular, `run_mapproject`'s
docstring for why this must run against the stitched (interleaved) cube, not a lone even/odd
parity. Only the VIS cubes are touched (`vis.even`/`vis.odd`); the UV cubes are irrelevant to a
VIS-striping investigation.

House style matches render.py: frozen dataclass results holding `Path`s, `config = config or
load_config()`, subprocess calls via the shared `run_quiet` helper (not raw `subprocess.run`).
"""

import dataclasses
import json
from pathlib import Path

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
    (made the banding worse, introducing new ghosting)."""
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
    with real craters visible throughout, same real product, same DEM."""
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
