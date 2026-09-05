"""ISIS3 real-WAC reprojection -- steps a real WAC EDR through ISIS's own pipeline
(`lrowac2isis` -> `spiceinit web=yes` -> `lrowaccal` -> `framestitch`) as a camera-model alternative to
`wac.py`'s manual framelet-stacking, then reprojects the cropped result onto the map via ISIS's own
native Pushframe camera model and `cam2map` (`crop_for_camera`/`run_cam2map_for_crop`) -- not ALE's
CSM ISD + ASP's `mapproject`, which `render.py` uses for the synthetic render. `isis_campt.py` covers
ground-truth ground<->image queries against an already-processed cube, and the CSM ISD generation
those queries (and a possible future CSM reprojection path) depend on.
"""
# `isis_campt.py`'s `run_isd_generate`/`run_mapproject` (the CSM path) are kept for reference/
# comparison -- not currently used: `usgscsm`'s `UsgsAstroPushFrameSensorModel::groundToImage` (the
# function ASP's `mapproject` calls once per output pixel) has an unreliable secant search over
# framelet index for Pushframe images, especially on a short crop. ISIS's own native camera model has
# no such issue -- it reads pointing/timing directly from the cube's own cached SPICE data, with no
# separate ISD/sidecar file needed at all. See docs/external-tools.md's "ISIS Pushframe pipeline"
# section for the full backstory. `crop_for_camera`/`run_cam2map_for_crop` here, and `isis_campt.py`'s
# `run_isd_generate`/`run_mapproject`/`resolve_ground_to_image_model`, point back to this paragraph
# rather than repeating it.
#
# House style matches render.py: frozen dataclass results holding `Path`s, `config = config or
# load_config()`, subprocess calls via the shared `run_quiet` helper (not raw `subprocess.run`).

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
import rasterio
import rasterio.warp
import rasterio.windows

from trntest import cache, lunaserv
from trntest.config import MOON_RADIUS_M, TrntestConfig, load_config
from trntest.lunaserv import DemOrthoResult
from trntest.product_registry import atomic_publish_path, writes_product
from trntest.subprocess_utils import run_quiet
from trntest.wac import SAMPLES, VIS_BLOCK_HEIGHT

if TYPE_CHECKING:
    from trntest import wac_camera_model
    from trntest.camera import Camera, FrameTiming

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
    # docs/pose-alignment.md for that investigation. Costs a one-time ~2GB fetch
    # (`ensure_lunar_shape_model`, idempotent, shared cache) the first time any pipeline run reaches
    # this call.
    config = config or load_config()
    shape_model_path = ensure_lunar_shape_model(config)
    run_quiet(["spiceinit", f"from={cub_path}", "web=yes", "shape=user", f"model={shape_model_path}"])
    return SpiceinitResult(cub_path=cub_path)


def _resolved_wac_ck_cache_path(config: TrntestConfig) -> Path:
    """Where `resolve_wac_ck_kernels` persists its resolved CK kernel list for the current product.

    :param config: Project config.
    :returns: The cache JSON path.
    """
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
    """Calibrate a spiceinit'd cube via ISIS's `lrowaccal`.

    :param spiceinit_result: Spiceinit'd cube (`run_spiceinit`'s output).
    :param config: Project config; `load_config()` if not given.
    :returns: A `LrowaccalResult` for the calibrated cube.
    """
    config = config or load_config()
    in_path = spiceinit_result.cub_path
    out_path = in_path.with_name(in_path.stem + ".cal.cub")
    run_quiet(["lrowaccal", f"from={in_path}", f"to={out_path}"])
    return LrowaccalResult(cub_path=out_path)


@dataclasses.dataclass(frozen=True)
class FramestitchResult:
    """The stitched cube, as returned by `run_framestitch`."""

    cub_path: Path
    flip: bool  # the FLIP value framestitch was actually run with -- see isis_campt.run_isd_generate's docstring


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
        docs/external-tools.md's "ISIS Pushframe pipeline" section) -- a per-pass manual decision,
        not derived automatically by ISIS.
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
    # The real-WAC counterpart to `isis_campt.run_mapproject`, but through ISIS's native Pushframe
    # camera model instead of ASP/CSM (see the module docstring for why). `_orthographic_map_pvl` clones
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
    # docs/external-tools.md's "Patching a cube's cached pointing via tabledump/csv2table" section.
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
    # hand-rolled warp/resampling code. See docs/external-tools.md's "Patching a cube's cached
    # pointing via tabledump/csv2table" section for the mechanism's own gotchas and the
    # `ConstantRotation_new = correction.delta_rotation.T @ ConstantRotation_original` composition
    # formula this implements.
    #
    # Only the rotation is injected here -- `correction.delta_position_m` is deliberately not
    # applied: `InstrumentPosition`'s cache is a coarser Hermite spline in a different frame, and the
    # fit found position's effect negligible, ~9m -> ~0.06px, so rotation alone accounts for
    # essentially all of it.
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
    # pose-correction fit's own ~600-900m residual gap (see docs/pose-alignment.md).
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
    # Same `(latitude, longitude)` COORDLIST column order as `isis_campt.ground_to_image_pixels_batch`
    # (matching `campt`'s own convention, per `mappt.xml`) -- opposite of this function's own
    # `(lon_deg, lat_deg)` argument order, kept consistent with the rest of this module.
    #
    # Unlike `isis_campt.ground_to_image_pixels_batch`, does not tolerate a per-point failure -- `mappt` has no
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
