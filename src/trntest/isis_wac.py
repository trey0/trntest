"""ISIS3 real-WAC reprojection -- steps a real WAC EDR through ISIS's own pipeline
(`lrowac2isis` -> `spiceinit web=yes` -> `lrowaccal` -> `framestitch`) as a camera-model alternative to
`wac.py`'s manual framelet-stacking, then reprojects the cropped result onto the map via ISIS's own
native Pushframe camera model and `cam2map` (`crop_for_camera`/`run_cam2map_for_crop`) -- not ALE's
CSM ISD + ASP's `mapproject`, which `render.py` uses for the synthetic render.
"""
# `run_isd_generate`/`run_mapproject` (the CSM path) are kept below for reference/comparison, but are
# no longer used by the notebook: `usgscsm`'s `UsgsAstroPushFrameSensorModel::groundToImage` (the
# function ASP's `mapproject` calls once per output pixel) has an unreliable secant search over
# framelet index for Pushframe images, especially on a short crop. ISIS's own native camera model has
# no such issue -- it reads pointing/timing directly from the cube's own cached SPICE data, with no
# separate ISD/sidecar file needed at all. See docs/external-tools.md's "ISIS Pushframe pipeline"
# section for the full backstory. Several functions below (`run_isd_generate`, `run_mapproject`,
# `crop_for_camera`, `resolve_ground_to_image_model`, `run_cam2map_for_crop`) point back to this
# paragraph rather than repeating it.
#
# House style matches render.py: frozen dataclass results holding `Path`s, `config = config or
# load_config()`, subprocess calls via the shared `run_quiet` helper (not raw `subprocess.run`).

import csv
import dataclasses
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pvl
import rasterio
import rasterio.warp
import rasterio.windows

from trntest import cache, lunaserv, render, wac_camera_model
from trntest.camera import Camera, FrameTiming
from trntest.config import MOON_RADIUS_M, TrntestConfig, load_config
from trntest.lunaserv import DemOrthoResult
from trntest.product_registry import atomic_publish_path, writes_product
from trntest.subprocess_utils import run_quiet
from trntest.wac import SAMPLES, VIS_BLOCK_HEIGHT

_BASE_KERNEL_INCLUDE = "{kernels/lsk/**,kernels/pck/**,kernels/sclk/**,kernels/fk/**,kernels/ik/**,kernels/iak/**}"


def ensure_isisdata(config: TrntestConfig | None = None) -> None:
    """Lazily fetch the ISIS reference data this pipeline needs, if not already present.

    :param config: Project config; `load_config()` if not given.
    """
    # `downloadIsisData base $ISISDATA --no-kernels` does NOT shrink `base` to near-zero --
    # `--no-kernels` only excludes the ck/ek/fk/ik/iak/lsk/mk/pck/sclk/spk/tspk/dsk kernel subdirs, and
    # `base`'s ~20GB is dominated by `dems/` (global shape models), untouched by the flag. None of that
    # DEM data is needed for this pipeline's scope (`mapproject` isn't reached), and
    # `spiceinit web=yes` still needs a few tiny, generic, mission-independent kernels locally (LSK for
    # time conversion at minimum -- without it, `spiceinit` fails with "Unable to load leadsecond
    # file") even though it doesn't need the bulky per-date CK/SPK ones. So: fetch only those small
    # kernel subdirs from `base` via `--include` (skips `dems/`/`examples/`/`kernelTesting/` entirely),
    # plus the full `lro` calibration tree (~5GB, no per-date kernels needed there either).
    config = config or load_config()
    isisdata = config.cache_root / "isisdata"
    if (isisdata / "base" / "kernels" / "lsk").is_dir():
        return
    isisdata.mkdir(parents=True, exist_ok=True)
    run_quiet(["downloadIsisData", "base", str(isisdata), "--include", _BASE_KERNEL_INCLUDE])
    run_quiet(["downloadIsisData", "lro", str(isisdata), "--no-kernels"])


_LUNAR_SHAPE_MODEL_REL_PATH = "base/dems/ldem_128ppd_Mar2011_clon180_radius_pad.cub"


def ensure_lunar_shape_model(config: TrntestConfig | None = None) -> Path:
    """Lazily fetch ISIS's own global lunar shape model.

    :param config: Project config; `load_config()` if not given.
    :returns: Path to the shape model cube.
    """
    # Real LOLA-derived radii (`LRO_LOLA_LDEM`: `SimpleCylindrical`, 128 px/degree, ~237m/px, pixel
    # values are body-fixed radius in meters via the cube's own `Base=1737400.0`/`Multiplier=0.5`) --
    # what `run_spiceinit` attaches to every WAC cube by default, and what
    # `sample_lunar_dem_radii_batch` samples camera-independently, without building a custom ISIS shape
    # cube from this project's own Astropedia GLD100 DEM -- a map-projection/radius-conversion/
    # labeling task this project hasn't validated, whereas this file is ISIS's own ready-made product
    # in exactly the format `spiceinit shape=user`/`mappt` expect.
    #
    # ~2GB, one-time (not the ~20GB `ensure_isisdata`'s own trailing comment warns `dems/` as a whole
    # costs; that figure is for every body ISIS supports, not just Moon). Also fetches two tiny index
    # files: `spiceinit shape=user` fails ("No existing files found with a numerical version
    # matching...") without `base/dems/kernels.*.db` and `base/kernels/spk/*.db` present, even though
    # the shape model itself needs no actual SPK kernel data, only these small index files.
    config = config or load_config()
    isisdata = config.cache_root / "isisdata"
    shape_model_path = isisdata / _LUNAR_SHAPE_MODEL_REL_PATH
    if shape_model_path.exists():
        return shape_model_path
    ensure_isisdata(config)
    run_quiet(
        ["downloadIsisData", "base", str(isisdata), "--include", "dems/ldem_128ppd_Mar2011_clon180_radius_pad.cub"]
    )
    run_quiet(["downloadIsisData", "base", str(isisdata), "--include", "dems/kernels.*.db"])
    run_quiet(["downloadIsisData", "base", str(isisdata), "--include", "kernels/spk/*.db"])
    return shape_model_path


@dataclasses.dataclass(frozen=True)
class EdrFetchResult:
    """The EDR product's fetched `.IMG` pixel data, as returned by `fetch_edr_img`."""

    img_path: Path


def fetch_edr_img(config: TrntestConfig | None = None) -> EdrFetchResult:
    """Fetch the EDR product's own `.IMG` pixel data.

    :param config: Project config; `load_config()` if not given.
    :returns: An `EdrFetchResult` for the fetched file.
    """
    # Not its `.xml` label, which `camera.fetch_frame_timing()` already fetches, and not the CDR
    # `.IMG`, which `wac.fetch_vis_mosaic()` already fetches -- `lrowac2isis` needs the EDR.
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
    """The entry-scoped ISIS working directory, `_work/<entry>/isis/`.

    :param config: Project config.
    :returns: The directory path, created if needed.
    """
    # Kept separate from the rest of `_work/<entry>/` so it survives routine pruning that the cheaper
    # stuff doesn't need to -- it's the single most expensive thing here to regenerate (a
    # multi-subprocess ISIS toolchain run). Keyed by entry (dataset-scoped), not by `edr_product` alone
    # (a prior, workspace-level layout): datasets are non-overlapping in `edr_product` by construction,
    # so the cross-dataset-reuse the old layout enabled isn't actually load-bearing.
    d = config.output_dir / "isis"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclasses.dataclass(frozen=True)
class Lrowac2IsisResult:
    """The 4 cubes `lrowac2isis` splits an EDR into, as returned by `run_lrowac2isis`."""

    # Only the VIS cubes (`vis_even`/`vis_odd`) are used elsewhere in this module; `uv_even`/`uv_odd`
    # are recorded here since `lrowac2isis` always produces them, but this pipeline's Pushframe
    # reprojection has no use for the UV channel.

    uv_even: Path
    vis_even: Path
    uv_odd: Path
    vis_odd: Path


_LROWAC2ISIS_SUFFIXES = (".uv.even.cub", ".vis.even.cub", ".uv.odd.cub", ".vis.odd.cub")


def run_lrowac2isis(edr: EdrFetchResult, config: TrntestConfig | None = None) -> Lrowac2IsisResult:
    """Split an EDR into its 4 parity/channel cubes via ISIS's `lrowac2isis`.

    :param edr: The fetched EDR (`fetch_edr_img`'s output).
    :param config: Project config; `load_config()` if not given.
    :returns: A `Lrowac2IsisResult` for the 4 output cubes.
    """
    # `lrowac2isis` writes all 4 outputs (`_LROWAC2ISIS_SUFFIXES`) from one `to=<prefix>` call, not
    # atomically on its own. Built under a call-scoped temp subdirectory of `_spike_dir` (same
    # filesystem as the destination, required for `Path.rename` to stay atomic), then each output is
    # atomically renamed to its canonical path -- this closes a race where two workers on the same
    # entry's raw split let a concurrent caller's own idempotency check (`run_pipeline`'s
    # "vis_even/vis_odd both exist" reuse branch) see a partially-written set.
    config = config or load_config()
    spike_dir = _spike_dir(config)
    out_prefix = spike_dir / edr.img_path.stem
    dests = {suffix: out_prefix.with_name(out_prefix.name + suffix) for suffix in _LROWAC2ISIS_SUFFIXES}

    tmp_dir = Path(tempfile.mkdtemp(dir=spike_dir, prefix=f".{edr.img_path.stem}.tmp."))
    try:
        tmp_prefix = tmp_dir / edr.img_path.stem
        run_quiet(["lrowac2isis", f"from={edr.img_path}", f"to={tmp_prefix}"])
        for suffix, dest in dests.items():
            tmp_prefix.with_name(tmp_prefix.name + suffix).rename(dest)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return Lrowac2IsisResult(
        uv_even=dests[".uv.even.cub"],
        vis_even=dests[".vis.even.cub"],
        uv_odd=dests[".uv.odd.cub"],
        vis_odd=dests[".vis.odd.cub"],
    )


@dataclasses.dataclass(frozen=True)
class SpiceinitResult:
    """The spiceinit'd cube, as returned by `run_spiceinit` -- `spiceinit` edits the label in place,
    so no new file is written."""

    cub_path: Path


def run_spiceinit(cub_path: Path, config: TrntestConfig | None = None) -> SpiceinitResult:
    """Attach SPICE pointing/timing and a shape model to `cub_path` via ISIS's `spiceinit`.

    :param cub_path: Cube to spiceinit, in place.
    :param config: Project config; `load_config()` if not given.
    :returns: A `SpiceinitResult` wrapping `cub_path`.
    """
    # `shape=user model=<ldem>` attaches ISIS's own global lunar shape model
    # (`ensure_lunar_shape_model`) -- per-pixel LOLA-derived terrain, not a flat reference ellipsoid.
    # The ellipsoid choice was a real, live bug, not a harmless simplification: every ground<->image
    # computation downstream of this call inherited a systematic terrain-vs-ellipsoid gap that looked
    # like a parallax/scale error at crater rims and a swath-wide altitude-offset stretch. See
    # docs/plan.md's camera-pose-alignment item for that investigation. Costs a one-time ~2GB fetch
    # (`ensure_lunar_shape_model`, idempotent, shared cache) the first time any pipeline run reaches
    # this call.
    config = config or load_config()
    shape_model_path = ensure_lunar_shape_model(config)
    run_quiet(["spiceinit", f"from={cub_path}", "web=yes", "shape=user", f"model={shape_model_path}"])
    return SpiceinitResult(cub_path=cub_path)


def _resolved_wac_ck_cache_path(config: TrntestConfig) -> Path:
    return config.cache_root / "isis_ck_resolution" / f"{config.edr_product}.json"


def _catlab(cub_path: Path) -> str:
    """Dump `cub_path`'s full PVL label as text, via ISIS's `catlab`.

    :param cub_path: Cube whose label to dump.
    :returns: The label text.
    """
    # Not run through `run_quiet` -- that helper only captures stdout to discard it on success, but
    # this call's entire point is its stdout.
    result = subprocess.run(["catlab", f"from={cub_path}"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="")
        result.check_returncode()
    return result.stdout


def _strip_isis_alias_prefix(path: str) -> str:
    """Strip ISIS's `$<mission>/` alias prefix, e.g. `'$lro/kernels/ck/moc42r_....bc'` ->
    `'kernels/ck/moc42r_....bc'`.

    :param path: An ISIS kernel path with a `$<mission>/` prefix.
    :returns: The path with the prefix removed.
    """
    # `cache.isis_kernel_rel_path`/`cache.fetch_isis_kernel` already know the mission root; this
    # project only ever resolves LRO kernels via this path, so the alias itself (always '$lro/' in
    # practice here) doesn't need to be preserved or parametrized.
    return path.split("/", 1)[1]


def _parse_ck_kernels_from_label(label_text: str) -> list[str]:
    """Extract the CK (`.bc`) kernel path(s) from a spiceinit'd cube's `Kernels.InstrumentPointing`
    label field.

    :param label_text: A cube's `catlab` PVL text, already spiceinit'd.
    :returns: The CK kernel path(s), alias prefix stripped.
    """
    # A cube's `Kernels.InstrumentPointing` field looks like `['Table',
    # '$lro/kernels/ck/lrolc_2019334_2020001_v01.bc', '$lro/kernels/ck/moc42r_2019334_2020001_v01.bc',
    # '$lro/kernels/fk/lro_frames_2014049_v01.tf']` for this project's WAC product -- both an `lrolc_*`
    # and a `moc42r_*` kernel together (see `resolve_wac_ck_kernels`'s own trailing comment for why
    # that matters). Skips the literal 'Table' marker and the frame kernel entry (already covered by
    # `spice_kernels.ALWAYS_KERNELS` -- same filename).
    label = pvl.loads(label_text)
    pointing = label["IsisCube"]["Kernels"]["InstrumentPointing"]
    return [_strip_isis_alias_prefix(entry) for entry in pointing if "/kernels/ck/" in entry]


def _is_spiceinit_complete(cub_path: Path) -> bool:
    """Whether `cub_path`'s label already has `spiceinit`'s own `Kernels.InstrumentPointing` group.

    :param cub_path: Cube to check.
    :returns: `True` if spiceinit has already run on this cube.
    """
    # The completion signal `_spiceinit_vis_even_cube` needs -- a cube's mere existence on disk does
    # not imply this: `run_lrowac2isis`'s own output is complete (atomically published) the moment it
    # exists, but not yet spiceinit'd. A concurrent caller's bare existence check on exactly this
    # window produced a `KeyError: 'InstrumentPointing'` in `_parse_ck_kernels_from_label` before this
    # check existed.
    try:
        label = pvl.loads(_catlab(cub_path))
        _ = label["IsisCube"]["Kernels"]["InstrumentPointing"]
        return True
    except (KeyError, subprocess.CalledProcessError):
        return False


def _spiceinit_vis_even_cube(config: TrntestConfig) -> Path:
    """The spiceinit'd `vis_even` cube, building it if needed.

    :param config: Project config.
    :returns: Path to the spiceinit'd `vis_even` cube.
    """
    # Needed by `resolve_wac_ck_kernels`, which only needs this cube's spiceinit-resolved label
    # (pointing/timing/camera model), not calibrated pixel data. Reuses the file on disk if it already
    # exists and is already spiceinit'd (`_is_spiceinit_complete`) -- checking existence alone isn't
    # sufficient (see that function's own trailing comment for the race this closes).
    # `run_lrowac2isis` is itself atomic (see its own docstring), so this can safely call it again if
    # reached concurrently by another worker -- either a fresh build or a redundant one, both produce
    # equally valid content, and the atomic rename just lets whichever finishes first "win" (a later
    # one's rename harmlessly overwrites it with an equivalent result). `run_spiceinit` itself is not
    # similarly hardened against two workers both reaching it for the exact same physical file at the
    # same moment -- an existing, deliberate design tradeoff (see `run_pipeline`'s own trailing
    # comment: spiceinit is confirmed idempotent, never specially guarded), not something newly
    # introduced or fixed here; a narrower residual risk than the one this function's own fix closes,
    # left open rather than silently assumed safe.
    edr = fetch_edr_img(config)
    out_prefix = _spike_dir(config) / edr.img_path.stem
    vis_even_path = out_prefix.with_name(out_prefix.name + ".vis.even.cub")
    if vis_even_path.exists() and _is_spiceinit_complete(vis_even_path):
        return vis_even_path
    ensure_isisdata(config)
    stitch_inputs = run_lrowac2isis(edr, config)
    run_spiceinit(stitch_inputs.vis_even, config)
    return stitch_inputs.vis_even


def resolve_wac_ck_kernels(config: TrntestConfig | None = None) -> list[str]:
    """Determine which CK (pointing) kernel(s) ISIS's own `spiceinit web=yes` furnishes for this
    project's target WAC product/date.

    :param config: Project config; `load_config()` if not given.
    :returns: `kernels/ck/<filename>` paths (relative to USGS's S3 `usgs_data/lro/` prefix, matching
        `cache.isis_kernel_rel_path`'s convention) -- there may be more than one.
    """
    # Runs a spiceinit against the target product and reads the resulting cube's `Kernels` label,
    # rather than reimplementing USGS's own kernel-db selection algorithm in Python:
    # `spice_kernels.select_naif_wac_ck_kernels` (deprecated, NAIF-metakernel-based) was missing a
    # second CK, `moc42r_*.bc`, that ISIS furnishes alongside the usual `lrolc_*` one. The
    # `lroc_kernels.db` route the alternative algorithm would need is currently empty in USGS's bucket,
    # even though ISIS's own live resolution still furnishes an `lrolc`-equivalent kernel from
    # somewhere -- asking ISIS directly sidesteps needing to know how/why.
    #
    # Result is persisted to `cache_root/isis_ck_resolution/<edr_product>.json` after a successful
    # resolution, and read from there first on every call -- once resolved for this project's fixed
    # demo product, no code path needs to reach the live spiceinit web service again, only the plain
    # HTTPS kernel-file download (`cache.fetch_isis_kernel`) matters for ongoing runs. Deliberately no
    # retry/backoff around the `spiceinit` call itself -- a failure should surface immediately, not
    # loop silently; this persisted cache is the resilience mechanism, not automatic retry. A
    # never-before-resolved EDR product still needs the live web service reachable at least once --
    # acceptable given this project's current fixed-date scope.
    #
    # Reuses the four already-implemented pipeline steps below (`ensure_isisdata`, `fetch_edr_img`,
    # `run_lrowac2isis`, `run_spiceinit`) rather than any new ISIS-side code -- `kernels.0001.conf`
    # routes WAC-VIS and WAC-UV identically, so any one of the four output cubes gives the same CK
    # resolution; `vis_even` is picked arbitrarily.
    config = config or load_config()
    cache_path = _resolved_wac_ck_cache_path(config)
    if cache_path.exists():
        result: list[str] = json.loads(cache_path.read_text())
        return result

    cub_path = _spiceinit_vis_even_cube(config)
    ck_paths = _parse_ck_kernels_from_label(_catlab(cub_path))

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(ck_paths))
    return ck_paths


@dataclasses.dataclass(frozen=True)
class LrowaccalResult:
    """The calibrated cube, as returned by `run_lrowaccal`."""

    cub_path: Path


def run_lrowaccal(spiceinit_result: SpiceinitResult, config: TrntestConfig | None = None) -> LrowaccalResult:
    config = config or load_config()
    in_path = spiceinit_result.cub_path
    out_path = in_path.with_name(in_path.stem + ".cal.cub")
    run_quiet(["lrowaccal", f"from={in_path}", f"to={out_path}"])
    return LrowaccalResult(cub_path=out_path)


@dataclasses.dataclass(frozen=True)
class FramestitchResult:
    """The stitched cube, as returned by `run_framestitch`."""

    cub_path: Path
    flip: bool  # the FLIP value framestitch was actually run with -- see run_isd_generate's docstring


@writes_product("isis_stitched_cube")
def run_framestitch(
    even: LrowaccalResult,
    odd: LrowaccalResult,
    flip: bool,
    config: TrntestConfig | None = None,
) -> FramestitchResult:
    """Combine even/odd parity cubes into one stitched cube via ISIS's `framestitch`.

    :param even: Calibrated even-parity cube.
    :param odd: Calibrated odd-parity cube.
    :param flip: Must match `camera.boresight_rotation_k`'s sign for this product (see
        docs/data-sources.md) -- a per-pass manual decision, not derived automatically by ISIS.
    :param config: Project config; `load_config()` if not given.
    :returns: A `FramestitchResult` for the stitched cube.
    """
    # Parameter names (`EVEN`/`ODD`/`TO`/`FLIP`, uppercase; ISIS params are case-insensitive but this
    # matches `framestitch -help`'s own spelling) confirmed against a built image -- `-help` doesn't
    # document `FRAMEHEIGHT`/`NUM_LINES_OVERLAP` beyond their `Null` defaults, so those are left unset
    # here (ISIS auto-computes when left `Null`) unless runs show that's wrong.
    #
    # `TO=` goes through `atomic_publish_path` -- `run_pipeline` (this function's only caller) already
    # checks the final stitched cube doesn't exist before reaching here, so this never collides with
    # that check; the only change is that a crash mid-`framestitch` now leaves nothing at the real path
    # (temp file cleaned up) instead of a partial cube ISIS's own overwrite-refusal would otherwise
    # leave stuck on the next run.
    config = config or load_config()
    out_path = even.cub_path.with_name(even.cub_path.stem.replace(".even", "") + ".stitched.cub")
    with atomic_publish_path(out_path) as tmp:
        run_quiet(
            [
                "framestitch",
                f"EVEN={even.cub_path}",
                f"ODD={odd.cub_path}",
                f"TO={tmp}",
                f"FLIP={'TRUE' if flip else 'FALSE'}",
            ]
        )
    return FramestitchResult(cub_path=out_path, flip=flip)


def run_pipeline(flip: bool, frame_timing: FrameTiming, config: TrntestConfig | None = None) -> FramestitchResult:
    """Run the full EDR-fetch-through-`framestitch` pipeline for this product.

    :param flip: Should be `camera.reverse_crop_along_track` -- the same SPICE-derived per-pass
        yaw-state signal `framestitch`'s FLIP needs to match, not hardcoded per-product.
    :param frame_timing: This product's frame timing.
    :param config: Project config; `load_config()` if not given.
    :returns: A `FramestitchResult` for the stitched cube.
    """
    # `flip` confirmed twice in the original spike, on two products with opposite yaw states. Takes
    # the bare `bool` rather than a full `Camera` so this can run during `camera.build_camera` itself
    # (which needs a stitched cube to pose the camera correctly -- see that function's docstring)
    # without a circular data dependency on the `Camera` it's still constructing.
    #
    # Idempotent at two levels, since `build_camera()` and this notebook's own explicit Phase 6 call
    # both reach this for the same product: (1) if the final stitched cube already exists, returns it
    # directly, no ISIS calls at all; (2) if just `lrowac2isis`'s split already exists (e.g.
    # `spice_kernels.fetch_and_furnish`'s default `isis_resolved` CK resolution already ran it as a
    # side effect, via `resolve_wac_ck_kernels`/`_spiceinit_vis_even_cube`), reuses those files rather
    # than re-running `lrowac2isis` -- purely an efficiency choice now, not a correctness requirement:
    # `run_lrowac2isis` is atomic (its own docstring), so calling it again is safe, just redundant
    # work. `spiceinit`, unlike the old (pre-atomic) `lrowac2isis`, is confirmed idempotent -- safe to
    # re-run on an already-spiceinit'd cube, same result -- so it's never specially guarded here.
    config = config or load_config()
    ensure_isisdata(config)
    edr = fetch_edr_img(config)
    out_prefix = _spike_dir(config) / edr.img_path.stem
    stitched_path = out_prefix.with_name(out_prefix.name + ".vis.cal.stitched.cub")
    if stitched_path.exists():
        return FramestitchResult(cub_path=stitched_path, flip=flip)

    vis_even_path = out_prefix.with_name(out_prefix.name + ".vis.even.cub")
    vis_odd_path = out_prefix.with_name(out_prefix.name + ".vis.odd.cub")
    if vis_even_path.exists() and vis_odd_path.exists():
        split = Lrowac2IsisResult(
            uv_even=out_prefix.with_name(out_prefix.name + ".uv.even.cub"),
            vis_even=vis_even_path,
            uv_odd=out_prefix.with_name(out_prefix.name + ".uv.odd.cub"),
            vis_odd=vis_odd_path,
        )
    else:
        split = run_lrowac2isis(edr, config)

    even = run_lrowaccal(run_spiceinit(split.vis_even, config), config)
    odd = run_lrowaccal(run_spiceinit(split.vis_odd, config), config)
    return run_framestitch(even, odd, flip=flip, config=config)


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
    # Not run through `run_quiet` -- like `_catlab`, this call's entire point is its stdout on success,
    # which `run_quiet` discards; failure still prints stdout/stderr before raising, same as
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
    # `crop_window_for_camera` row-offset/flip calculation) -- see that function's docstring.
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
class IsdGenerateResult:
    """The generated CSM ISD, as returned by `run_isd_generate`/`run_isd_generate_for_crop`."""

    json_path: Path


def run_isd_generate(stitched: FramestitchResult, config: TrntestConfig | None = None) -> IsdGenerateResult:
    """Generate a CSM Pushframe ISD (ALE's `isd_generate`) for the *stitched* cube.

    :param stitched: Full, uncropped stitched cube (`run_framestitch`'s output). Not valid for a
        cropped cube -- see `run_isd_generate_for_crop`.
    :param config: Project config; `load_config()` if not given.
    :returns: An `IsdGenerateResult` for the generated (or already-cached) ISD JSON.
    """
    # `-i` (`--only_isis_spice`) reads pointing/timing directly from the label `run_spiceinit` already
    # embedded, per-parity, before `framestitch` -- `framestitch`'s merge carries those groups through
    # intact: the resulting ISD's geometry/timing parameters (`interframe_delay`, the 259-sample
    # pointing table, etc.) come out identical whether generated from this stitched cube or a single
    # unstitched parity alone (see docs/data-sources.md). Despite that, which cube you actually
    # reproject through this ISD matters a great deal -- see `run_mapproject`'s docstring.
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
    # `usgscsm`'s `groundToImage` (see the module docstring) rather than anything fixable in the ISD
    # itself. `crop_for_camera`'s WAC crop no longer uses an ISD at all -- see `run_cam2map_for_crop`.
    #
    # Idempotent (matching this module's usual convention, e.g. `crop_for_camera`): reuses the file on
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
    stitched: FramestitchResult,
    isd: IsdGenerateResult,
    dem_ortho_result: DemOrthoResult,
    config: TrntestConfig | None = None,
) -> Path:
    """**Deprecated** -- reproject the ISIS-processed WAC cube back onto the map via its CSM/ISD
    sidecar. `run_cam2map_for_crop` is the accurate path now.

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
    # calls into, once per output pixel -- has a size-dependent self-consistency weakness (see the
    # module docstring). ISIS's own native reprojection of this same cube agrees with itself (crop vs.
    # full) to 0.9999986 correlation, but only agrees with this function's output at ~0.2-0.4. Kept for
    # reference/comparison.
    config = config or load_config()
    mapproj_tif = stitched.cub_path.with_name(stitched.cub_path.stem + "-mapproj.tif")
    return render.run_mapproject_image(stitched.cub_path, isd.json_path, mapproj_tif, dem_ortho_result, config)


def crop_window_for_camera(camera: Camera) -> rasterio.windows.Window:
    """The pixel window `crop_for_camera` should crop the stitched cube to for `camera`'s footprint.

    :param camera: The camera whose footprint determines the crop window.
    :returns: The crop `Window`, in the stitched cube's own pixel space.
    """
    # The stitched cube preserves `wac.VIS_BLOCK_HEIGHT` (14) lines per original EDR frame, not 1 --
    # `lrowac2isis` does not TDI-sum each frame down to one line, it keeps the same per-frame line
    # structure `wac.py`'s raw CDR byte-layout code already assumes. So both
    # `camera.center_frame_index` and `camera.n_frames_for_square_crop` need to be scaled by that
    # factor to land on the same footprint `wac.fetch_vis_mosaic`'s own crop covers.
    height = camera.n_frames_for_square_crop * VIS_BLOCK_HEIGHT
    center_line = camera.center_frame_index * VIS_BLOCK_HEIGHT
    line_start = round(center_line - height / 2)
    return rasterio.windows.Window(col_off=0, row_off=line_start, width=SAMPLES, height=height)


@dataclasses.dataclass(frozen=True)
class CropResult:
    """The cropped cube, as returned by `crop_for_camera`/`apply_pose_correction_to_crop`/
    `attach_dem_shape_model`."""

    cub_path: Path


@writes_product("isis_crop_cube")
def crop_for_camera(stitched: FramestitchResult, camera: Camera, config: TrntestConfig | None = None) -> CropResult:
    """Crop `stitched` to `crop_window_for_camera(camera)`'s window via ISIS's `crop`.

    :param stitched: Stitched cube to crop.
    :param camera: Camera whose footprint determines the crop window.
    :param config: Project config; `load_config()` if not given.
    :returns: A `CropResult` for the cropped cube.
    """
    # The one "WAC crop" output product both a raw display of `.cub_path` directly and
    # `run_cam2map_for_crop` consume. No separate ISD/sidecar is generated here -- ISIS's own native
    # camera model reads pointing/timing directly from the cube's own cached SPICE data, so the
    # cropped cube is already fully self-describing; see the module docstring for why the CSM/ISD
    # route was abandoned instead of fixed.
    #
    # `lrowaccal` (already run before this, in `run_pipeline`) refuses to run on a cropped cube ("USER
    # ERROR: This application can not be run on any image that has been geometrically transformed ...
    # or cropped"), so cropping must happen after calibration/`framestitch`, on `stitched`, not earlier
    # in the pipeline.
    #
    # Idempotent (matching `run_pipeline`'s pattern): reuses the file on disk if it already exists --
    # `crop_footprint_corners_for_camera` and the notebook's own Phase 6 cell both reach this for the
    # same product, and ISIS's `crop` app, like `lrowac2isis`/`framestitch`, refuses to overwrite an
    # existing `to=` output.
    config = config or load_config()
    window = crop_window_for_camera(camera)
    out_path = stitched.cub_path.with_name(stitched.cub_path.stem + ".crop.cub")
    if not out_path.exists():
        with atomic_publish_path(out_path) as tmp:
            run_quiet(
                [
                    "crop",
                    f"from={stitched.cub_path}",
                    f"to={tmp}",
                    f"line={window.row_off + 1}",  # ISIS LINE is 1-based; Window.row_off is 0-based
                    f"nlines={window.height}",
                ]
            )
    return CropResult(cub_path=out_path)


def run_isd_generate_for_crop(
    crop: CropResult, camera: Camera, flip: bool, config: TrntestConfig | None = None
) -> IsdGenerateResult:
    """Generate a CSM Pushframe ISD for `crop` itself, not the full stitched cube.

    :param crop: Cropped cube (`crop_for_camera`'s output).
    :param camera: Camera whose crop window (`crop_window_for_camera`) determines the time offset.
    :param flip: Written into the ISD's `framelet_order_reversed`.
    :param config: Project config; `load_config()` if not given.
    :returns: An `IsdGenerateResult` for the crop-sized ISD.
    """
    # So the resulting JSON's image dimensions/frame count are read from, and correctly reflect, the
    # crop's real size, for `trn_dataset.TrnTestCropImage`'s sidecar (see docs/external-tools.md's
    # "The crop ISD sidecar's real accuracy" section). Not a substitute for `run_isd_generate`'s
    # full-cube ISD, and not usable for actual reprojection -- like any Pushframe ISD in this codebase,
    # `usgscsm`'s `groundToImage` isn't reliable enough for that (see the module docstring); ground<->
    # image lookups still go through `resolve_ground_to_image_model`/`ground_to_image_pixel`,
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
    # scalar time fields above by the crop's own time offset is sufficient (see docs/data-sources.md
    # for that formula's own validation).
    #
    # `line_offset` -- how many lines into the stitched cube `crop.cub_path` actually starts -- comes
    # from `crop_window_for_camera(camera).row_off`, the exact same window `crop_for_camera` itself
    # cropped to.
    config = config or load_config()
    json_path = crop.cub_path.with_suffix(".json")
    run_quiet(["isd_generate", "-i", str(crop.cub_path), "-o", str(json_path)])
    with open(json_path) as f:
        isd = json.load(f)

    line_offset = crop_window_for_camera(camera).row_off
    time_offset_s = (line_offset / VIS_BLOCK_HEIGHT) * isd["interframe_delay"]
    for key in ("starting_ephemeris_time", "ending_ephemeris_time", "center_ephemeris_time"):
        isd[key] += time_offset_s
    for key in ("ck_table_start_time", "ck_table_end_time"):
        isd["instrument_pointing"][key] += time_offset_s
    isd["framelet_order_reversed"] = flip

    with open(json_path, "w") as f:
        json.dump(isd, f)
    return IsdGenerateResult(json_path=json_path)


@dataclasses.dataclass(frozen=True)
class GroundToImageModel:
    """Which camera-model authority `ground_to_image_pixel` should query for a given crop, and why --
    see `resolve_ground_to_image_model`."""

    cub_path: Path
    name_model: str
    used_csm: bool


def resolve_ground_to_image_model(
    stitched: FramestitchResult, crop: CropResult, config: TrntestConfig | None = None
) -> GroundToImageModel:
    """Resolve which camera-model authority ground-to-image queries should go through for this crop.

    :param stitched: Full stitched cube -- `run_isd_generate` is only valid there, not on a cropped
        cube.
    :param crop: The cropped cube ground-to-image queries will actually run against.
    :param config: Project config; `load_config()` if not given.
    :returns: A `GroundToImageModel` naming the resolved authority.
    """
    # Used by `tie_points.resolve_crop_pixels` -- the same resolution order `run_cam2map_for_crop`
    # already settled on for the DEM-reprojection path, generalized into reusable logic instead of a
    # one-off decision: (1) try building a CSM ISD sidecar for the full stitched cube and inspect its
    # `name_model`; (2) if it resolves to a Pushframe sensor, `usgscsm`'s `groundToImage` is known
    # unreliable for that class of camera (see this module's docstring) -- fall back to the crop's own
    # native, SPICE-embedded camera model, queried directly (no CSM/ISD involved); (3) otherwise, the
    # CSM model is safe to use -- attach it to a private copy of the crop via ISIS's own `csminit`, so
    # the crop's own native-model queries elsewhere (e.g. `run_cam2map_for_crop`) aren't affected by
    # this copy's attached CSM state.
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
    # `lunaserv._terrain_photometric_angles`, used to validate it (see that function's own docstring).
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
    # just re-litigate which framelet is "right" in an overlap band (see `wac_camera_model.py`'s own
    # module docstring); this function instead only ever queries a pixel that's already been resolved
    # by some other process (never searches for one itself), so there's nothing to litigate -- one
    # pixel has exactly one ground point.
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


def _orthographic_map_pvl(dem_ortho_result: DemOrthoResult) -> str:
    """Build an ISIS PVL "Mapping" group cloning `dem_ortho_result`'s own local Orthographic CRS.

    :param dem_ortho_result: DEM/ortho pair whose projection to clone.
    :returns: The PVL text for a `cam2map` `MAP=` file.
    """
    # Same center lat/lon, spherical Moon radius, and pixel resolution (see `DemOrthoResult`'s
    # docstring: `config.lunaserv_srs_template`, centered on this camera's own footprint) -- so
    # `cam2map`'s output (`run_cam2map_for_crop`) lands in the same projected coordinate system as
    # `dem_ortho_result.dem`/`.ortho`. Verified empirically that ISIS's own Orthographic projection
    # implementation agrees with GDAL/PROJ's `+proj=ortho` to sub-micrometer precision for matching
    # center/radius parameters, via `cam2map`+`campt` cross-checked against `pyproj`'s own forward
    # projection at a test pixel.
    #
    # Deliberately does not pin `UpperLeftCornerX`/`UpperLeftCornerY` to match `dem_ortho_result`'s own
    # pixel grid -- `cam2map`'s output is left free to auto-size to the crop's own footprint
    # (`DEFAULTRANGE=CAMERA` in `run_cam2map_for_crop`). This is safe because `plotting.plot_overlay`
    # composites both rasters via their own georeferenced coordinates (`rioxarray`/`xarray`), not a
    # shared pixel grid -- so the two rasters only need to agree on the projection, not share
    # pixel-for-pixel alignment. Avoiding a shared-grid requirement also avoids needing a separate
    # resampling/warping pass after `cam2map`, which would risk the exact kind of
    # interpolation-quality/subtle-misalignment issues this whole detour was trying to avoid in the
    # first place.
    with rasterio.open(dem_ortho_result.dem) as src:
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


@writes_product("crop_cam2map")
def run_cam2map_for_crop(
    crop: CropResult, dem_ortho_result: DemOrthoResult, config: TrntestConfig | None = None
) -> Path:
    """Reproject `crop` onto the map via ISIS's own native `cam2map`.

    :param crop: Cropped cube to reproject.
    :param dem_ortho_result: DEM/ortho pair whose grid/projection this reprojects onto.
    :param config: Project config; `load_config()` if not given.
    :returns: Path to the reprojected, single-band GeoTIFF.
    """
    # The real-WAC counterpart to `run_mapproject`, but through ISIS's native Pushframe camera model
    # instead of ASP/CSM (see the module docstring for why). `_orthographic_map_pvl` clones
    # `dem_ortho_result`'s own projection so the output shares the same coordinate system (not pixel
    # grid -- see that function's docstring) as `dem_ortho_result.ortho`, letting
    # `plotting.plot_overlay` composite them directly.
    #
    # `PIXRES=map` is required -- `cam2map`'s `PIXRES` parameter defaults to `CAMERA` (auto-derives
    # resolution from the image itself), which silently ignores the map file's own `PixelResolution`
    # otherwise (without it, output resolution came out as the camera's native ~184m/px, not the
    # requested ~100m/px). `DEFAULTRANGE=camera` auto-sizes the output extent to the crop's own
    # footprint, matching how `sat_sim`/`mapproject` never render more than the camera's own FOV
    # either.
    #
    # `WARPALGORITHM=forwardpatch PATCHSIZE=1`, not the `AUTOMATIC` default (or a larger explicit
    # `PATCHSIZE`): ISIS's own docs recommend `AUTOMATIC` for push frame cameras (it picks
    # `FORWARDPATCH` with `PATCHSIZE` set to the full framelet height, 14, so a patch never crosses a
    # framelet boundary), but this leaves large diagonal gaps at this map resolution -- `AUTOMATIC`'s
    # patches fit an affine transform per patch from its four corners, and silently drop any patch
    # whose affine fit isn't within 0.1px of the camera model's own computation, which a 14-line-tall
    # patch fails for roughly half the framelets here (present even on the full, uncropped cube, not
    # just the crop -- see the module docstring). A smaller explicit `PATCHSIZE` fits each small patch
    # accurately enough to pass that check everywhere tested: overall valid coverage goes from ~47%
    # (`AUTOMATIC`) to ~71% (matching the crop's own footprint, no more diagonal gaps) at any
    # `PATCHSIZE` from 1-4.
    #
    # `PATCHSIZE=1` specifically, not the `4` an earlier version of this used: a visible striping
    # artifact in the mapprojected output gets markedly worse at `PATCHSIZE=8`/`14`, and `PATCHSIZE=1`
    # is a visible improvement over `4`. Not a complete fix -- a faint residual pattern remains visible
    # at `PATCHSIZE=1` on close inspection, consistent with modest photometric discontinuities where
    # the resampled product transitions between adjacent framelets (an inherent, small artifact of any
    # patch-based warp), not the more severe missing/bad-data-looking pattern `PATCHSIZE=4` showed.
    # `PATCHSIZE=1` costs runtime (~16s vs. ~10s for this crop) but doesn't trade away coverage at all
    # (71.39% vs. 71.38%, essentially identical) -- worthwhile, just not complete; diminishing returns
    # past this point.
    #
    # Converts the resulting multi-band cube (WAC VIS carries 5 filter bands) to a single-band GeoTIFF
    # via `gdal_translate -b 1`, matching this pipeline's existing band-1 convention
    # (`plotting.read_raster_band`'s default, and what ASP's own `mapproject` picked automatically:
    # "Detected multi-band image. Only the first band will be used."). `gdal_translate` prints a
    # `PROJ: proj_create_from_name` error to stderr here (an ISIS/GDAL `PROJ_LIB` environment mismatch)
    # -- harmless: the output CRS/transform were verified correct (matching `dem_ortho_result`'s own
    # projection exactly) despite it, and the process still exits 0.
    config = config or load_config()
    # _work/<entry>/crop/ -- generator-scoped, even though this is also reused by
    # TrnTestReprojectImage's own texture-source step: it's the crop's own mapproject output regardless
    # of which product type ends up consuming it, so it stays under the crop generator's own subtree
    # rather than the isis/ tier crop.cub_path itself now lives in.
    out_dir = config.output_dir / "crop"
    out_dir.mkdir(parents=True, exist_ok=True)

    map_path = out_dir / (crop.cub_path.stem + ".ortho.map")
    map_path.write_text(_orthographic_map_pvl(dem_ortho_result))

    mapproj_cub = out_dir / (crop.cub_path.stem + "-cam2map.cub")
    # atomic_publish_path also fixes a real pre-existing gap as a side effect: this function had no
    # existence guard at all, so a second call for the same crop (e.g. plot_overlay() called twice)
    # used to hit ISIS's own "to= already exists" refusal on mapproj_cub. cam2map now always writes to
    # a guaranteed-fresh temp path, and the final rename replaces any prior mapproj_cub atomically (a
    # plain POSIX rename over an existing destination, unlike ISIS's own to= semantics).
    with atomic_publish_path(mapproj_cub) as tmp_cub:
        run_quiet(
            [
                "cam2map",
                f"from={crop.cub_path}",
                f"map={map_path}",
                f"to={tmp_cub}",
                "pixres=map",
                "defaultrange=camera",
                "warpalgorithm=forwardpatch",
                "patchsize=1",
            ]
        )

    mapproj_tif = out_dir / (crop.cub_path.stem + "-cam2map.tif")
    with atomic_publish_path(mapproj_tif) as tmp_tif:
        run_quiet(["gdal_translate", "-b", "1", str(mapproj_cub), str(tmp_tif)])
    return mapproj_tif


_INSTRUMENT_POINTING_LABEL_EXCLUDE = {"Name", "StartByte", "Bytes", "Records", "ByteOrder", "Field"}
_INSTRUMENT_POINTING_COLTYPES = "(Double,Double,Double,Double,Double,Double,Double,Double)"  # J2000Q0-3,AV1-3,ET


def _table_extra_label(label_text: str, table_name: str) -> pvl.PVLModule:
    """Extract `table_name`'s label metadata beyond its raw field records from a cube's full `catlab`
    PVL text.

    :param label_text: A cube's `catlab` PVL text.
    :param table_name: Table to extract metadata for (e.g. `"InstrumentPointing"`).
    :returns: The metadata (e.g., for `InstrumentPointing`: `TimeDependentFrames`, `ConstantFrames`,
        `ConstantRotation`, `CkTableStartTime`/`EndTime`/`OriginalSize`, `FrameTypeCode`,
        `Description`, `Kernels`), in the shape `csv2table`'s own `label=` parameter expects.
    :raises ValueError: If `table_name` doesn't appear exactly once in the label.
    """
    # Round-tripping a Table via `tabledump`/`csv2table` without this metadata silently drops it,
    # producing a systematic ~0.08deg pointing error, not just precision loss -- see
    # docs/proposed-tasks/corrected-overlay-cam2map-plan.md.
    label = pvl.loads(label_text)
    tables = [obj for obj in label.getlist("Table") if obj.get("Name") == table_name]
    if len(tables) != 1:
        raise ValueError(f"expected exactly one Table named {table_name!r} in the label, found {len(tables)}")
    tbl = tables[0]
    return pvl.PVLModule({k: v for k, v in tbl.items() if k not in _INSTRUMENT_POINTING_LABEL_EXCLUDE})


def apply_pose_correction_to_crop(
    crop: CropResult, correction: wac_camera_model.PoseCorrection, config: TrntestConfig | None = None
) -> CropResult:
    """Bake a fitted `wac_camera_model.PoseCorrection` into a copy of `crop`'s cube.

    :param crop: Cube to correct (a copy is made; `crop` itself is untouched).
    :param correction: The fitted pose correction to apply.
    :param config: Project config; `load_config()` if not given.
    :returns: A `CropResult` for the corrected copy.
    """
    # Patches the copy's cached `InstrumentPointing` Table's single `ConstantRotation` matrix (the
    # -85621->-85620, camera-to-spacecraft-bus, time-independent rotation) -- so ISIS's own `cam2map`
    # (`run_cam2map_for_crop`, unmodified) picks up the corrected pose automatically, with no new
    # hand-rolled warp/resampling code. See docs/proposed-tasks/corrected-overlay-cam2map-plan.md for
    # the full background this implements.
    #
    # Only the rotation is injected here -- `correction.delta_position_m` is deliberately not applied
    # (see that plan doc's "Position correction: deliberately not implemented via this mechanism" for
    # why: `InstrumentPosition`'s cache is a coarser Hermite spline in a different frame, and the fit
    # found position's effect negligible, ~9m -> ~0.06px, so rotation alone accounts for essentially
    # all of it).
    #
    # `ConstantRotation_new = correction.delta_rotation.T @ ConstantRotation_original` --
    # cross-validated (not derived from ISIS source) against `wac_camera_model`'s own already-validated
    # forward projector using a known synthetic test rotation: matched the projector's predicted pixel
    # to ~1e-6, while the naively-expected `ConstantRotation_original @ delta_rotation` placed the
    # point outside the crop's coverage entirely. Likely explanation: ISIS's stored matrix is the
    # transpose of this project's own `R_A_to_B` convention (`v_B = R_A_to_B @ v_A`) --
    # `correction.delta_rotation` itself is defined in that convention (`camera.py`/
    # `wac_camera_model.py`), so transposing it before composing with ISIS's own (already-transposed)
    # stored matrix reconciles the two conventions.
    #
    # The cube's 259-row `InstrumentPointing` quaternion/AV/ET table itself is untouched -- `tabledump`
    # round-trips it byte-for-byte via `csv2table`, only the label's `ConstantRotation` keyword
    # changes. `coltypes` is hardcoded to WAC-VIS's own fixed `InstrumentPointing` column layout
    # (`J2000Q0..3`, `AV1..3`, `ET` -- 8 doubles), not derived generically since this project only ever
    # touches this one table shape.
    config = config or load_config()
    out_path = crop.cub_path.with_name(crop.cub_path.stem + ".corrected.cub")
    shutil.copy(crop.cub_path, out_path)

    csv_path = out_path.with_suffix(".pointing.csv")
    run_quiet(["tabledump", f"from={crop.cub_path}", f"to={csv_path}", "name=InstrumentPointing"])

    extra_label = _table_extra_label(_catlab(crop.cub_path), "InstrumentPointing")
    c_orig = np.array(extra_label["ConstantRotation"]).reshape(3, 3)
    c_new = correction.delta_rotation.T @ c_orig
    extra_label["ConstantRotation"] = list(c_new.flatten())

    label_path = out_path.with_suffix(".pointing_label.pvl")
    label_path.write_text(pvl.dumps(extra_label))

    run_quiet(
        [
            "csv2table",
            f"csv={csv_path}",
            f"label={label_path}",
            f"to={out_path}",
            "tablename=InstrumentPointing",
            f"coltypes={_INSTRUMENT_POINTING_COLTYPES}",
        ]
    )
    return CropResult(cub_path=out_path)


def attach_dem_shape_model(crop: CropResult, config: TrntestConfig | None = None) -> CropResult:
    """Copy `crop`'s cube and re-run `spiceinit shape=user model=<ldem>` on the copy.

    :param crop: Cube to attach a DEM shape model to (a copy is made; `crop` itself is untouched).
    :param config: Project config; `load_config()` if not given.
    :returns: A `CropResult` for the DEM-attached copy.
    """
    # Swaps the copy's camera model shape from this pipeline's usual `shape=ellipsoid` to ISIS's own
    # global lunar terrain (`ensure_lunar_shape_model`), for `control_network.resolve_control_points`'s
    # ground-to-image queries. Deliberately narrow/opt-in, not a change to the shared pipeline default
    # -- `run_spiceinit`'s own `shape=ellipsoid` and everything built on it elsewhere
    # (`run_cam2map_for_crop`'s output, `wac_camera_model.py`'s hand-rolled projector's own validation)
    # stays untouched; only this DEM-aware copy is affected.
    #
    # `spiceinit` is confirmed idempotent/safe to re-run on an already-spiceinit'd, already-cropped
    # cube (see `run_pipeline`'s own trailing comment) -- re-derives pointing/position from the same
    # kernels, only the shape changes. `web=yes` (matching `run_spiceinit`'s own call) is required here
    # too: without it, `spiceinit` attempts local kernel-database resolution instead of the web service
    # and fails outright on paths this pipeline's minimal `ensure_isisdata` fetch never populates.
    #
    # Live-validated: substantial, non-constant local elevation on this project's own current default
    # candidate (+600m to +3000m across 5 test pixels vs. the ellipsoid's constant 1737400.0m), and the
    # resulting ground-point shift (up to ~1.7km) is the right order of magnitude to matter for the
    # pose-correction fit's own ~600-900m residual gap (see docs/plan.md's status line).
    config = config or load_config()
    shape_model_path = ensure_lunar_shape_model(config)
    out_path = crop.cub_path.with_name(crop.cub_path.stem + ".dem.cub")
    if not out_path.exists():
        shutil.copy(crop.cub_path, out_path)
        run_quiet(["spiceinit", f"from={out_path}", "web=yes", "shape=user", f"model={shape_model_path}"])
    return CropResult(cub_path=out_path)


def sample_lunar_dem_radii_batch(lonlat_deg: np.ndarray, config: TrntestConfig | None = None) -> np.ndarray:
    """Local lunar radius (meters, body-fixed) at many arbitrary `(lon_deg, lat_deg)` points at once,
    via ISIS's own global lunar shape model.

    :param lonlat_deg: `(N, 2)`, `(lon_deg, lat_deg)` columns.
    :param config: Project config; `load_config()` if not given.
    :returns: Radius, meters, one per input point, same order.
    :raises RuntimeError: If `mappt` returns a different row count than input points.
    """
    # A single `mappt usecoordlist=true` call against the shape model cube directly
    # (`ensure_lunar_shape_model`) -- deliberately camera/image-independent, unlike `campt` against a
    # specific crop cube: `campt` reports `NULL` for every geometric field (not just the pixel) for a
    # point outside that specific camera's field of view, even though the point's real elevation is
    # well-defined regardless of which camera (if any) can see it. This matters for
    # `resolve_control_points`'s basemap-side trusted ground truth specifically, which has no reason
    # to be visible to this one crop's own camera at all for its own elevation to be meaningful.
    #
    # `mappt`'s own FLAT output `PixelValue` is already the calibrated radius in meters (the cube's
    # `Base`/`Multiplier` label values already applied by `mappt` itself; re-applying them again on top
    # produces a wildly wrong, ~867km-off result), not a raw DN needing manual conversion.
    # Cross-validated against `campt`'s own `LocalRadius` (via a DEM-attached crop cube,
    # `attach_dem_shape_model`) at 5 points: agreement to <=50m, a small fraction of the elevation
    # signal (hundreds to thousands of meters) being sampled here.
    #
    # Same `(latitude, longitude)` COORDLIST column order as `ground_to_image_pixels_batch` (matching
    # `campt`'s own convention, per `mappt.xml`) -- opposite of this function's own
    # `(lon_deg, lat_deg)` argument order, kept consistent with the rest of this module.
    #
    # Unlike `ground_to_image_pixels_batch`, does not tolerate a per-point failure -- `mappt` has no
    # `ALLOWERROR`-equivalent parameter at all (a single invalid coordinate, e.g. a latitude outside
    # [-90,90], aborts the entire batch with a `USER ERROR` and no output file, unlike `campt`'s own
    # graceful per-row `Error` field). Not expected to matter in practice here -- every matched tie
    # point's own `(lon, lat)` is by construction a valid point on the Moon, so a genuine failure would
    # mean something upstream is already wrong and should surface loudly, not be silently dropped.
    config = config or load_config()
    shape_model_path = ensure_lunar_shape_model(config)
    lonlat_deg = np.asarray(lonlat_deg)
    with tempfile.TemporaryDirectory() as tmp_dir:
        coordlist_path = Path(tmp_dir) / "coordlist.csv"
        out_path = Path(tmp_dir) / "mappt_out.flat"
        with open(coordlist_path, "w") as f:
            for lon_deg, lat_deg in lonlat_deg:
                f.write(f"{lat_deg},{lon_deg}\n")

        run_quiet(
            [
                "mappt",
                f"from={shape_model_path}",
                "usecoordlist=true",
                f"coordlist={coordlist_path}",
                "type=ground",
                f"to={out_path}",
                "format=flat",
                "append=false",
            ]
        )
        with open(out_path, newline="") as f:
            rows = list(csv.DictReader(f))

    if len(rows) != len(lonlat_deg):
        raise RuntimeError(
            f"mappt usecoordlist returned {len(rows)} rows for {len(lonlat_deg)} input points -- "
            "expected exactly one row per point"
        )
    return np.array([float(row["PixelValue"]) for row in rows])


def sample_local_dem_patch(
    center_lon_deg: float, center_lat_deg: float, cellsize_m: float, config: TrntestConfig | None = None
) -> np.ndarray:
    """A 3x3 local elevation patch centered on `(center_lon_deg, center_lat_deg)`, sampled from ISIS's
    own global lunar shape model.

    :param center_lon_deg: Patch center longitude, degrees.
    :param center_lat_deg: Patch center latitude, degrees.
    :param cellsize_m: Spacing between sample points, meters.
    :param config: Project config; `load_config()` if not given.
    :returns: `(3, 3)` elevation, meters, relative to `MOON_RADIUS_M`. Row 0 is north.
    """
    # Sampled via `sample_lunar_dem_radii_batch` at 9 points spaced `cellsize_m` apart in local
    # East/North. Built as a camera-independent ground-truth `dem` input for
    # `lunaserv._terrain_photometric_angles`'s own gradient stencil, specifically so its output can be
    # compared against ISIS `phocube`'s `LOCALINCIDENCE`/`LOCALEMISSION` backplanes computed from this
    # exact same shape model (rather than this project's own, differently-sourced Astropedia DEM).
    #
    # The 9 sample points are generated by converting a small local-meters grid (centered on the
    # tangent point) to lon/lat via the same forward Orthographic projection
    # `lunaserv.local_orthographic_crs`/`geographic_crs` already define elsewhere in this project --
    # not a second, independent coordinate-conversion implementation. Row 0 is north (matches
    # `_terrain_photometric_angles`'s own `y_centers` convention: row 0 = north/top), so this patch can
    # be fed to it directly as `dem` with a matching `bbox`.
    config = config or load_config()
    offsets = (-cellsize_m, 0.0, cellsize_m)
    xs = [dx for _dy in reversed(offsets) for dx in offsets]  # north (max y) row first
    ys = [dy for dy in reversed(offsets) for _dx in offsets]
    ortho_crs = lunaserv.local_orthographic_crs(center_lon_deg, center_lat_deg)
    lons, lats = rasterio.warp.transform(ortho_crs, lunaserv.geographic_crs(), xs, ys)
    lonlat_deg = np.array([(lon % 360.0, lat) for lon, lat in zip(lons, lats, strict=True)])
    radii = sample_lunar_dem_radii_batch(lonlat_deg, config)
    return (radii - MOON_RADIUS_M).reshape(3, 3)
