"""ISIS3/CSM real-WAC reprojection spike -- steps a real WAC EDR through ISIS's own pipeline
(`lrowac2isis` -> `spiceinit web=yes` -> `lrowaccal` -> `framestitch`) as a genuine-camera-model
alternative to `wac.py`'s manual framelet-stacking. See docs/data-sources.md's "ISIS3/CSM spike"
section and docs/history.md's Phase 12 for the full backstory and prior findings.

Scoped only through `framestitch` -- no `isd_generate`/`mapproject` wrappers here yet, that's
future work once the framelet-boundary striping this spike is chasing is understood. Only the VIS
cubes are touched (`vis.even`/`vis.odd`); the UV cubes are irrelevant to a VIS-striping
investigation.

House style matches render.py: frozen dataclass results holding `Path`s, `config = config or
load_config()`, subprocess calls via `_run_quiet` below (not raw `subprocess.run`).
"""

import dataclasses
import subprocess
from pathlib import Path

from trntest import cache
from trntest.config import TrntestConfig, load_config

_BASE_KERNEL_INCLUDE = "{kernels/lsk/**,kernels/pck/**,kernels/sclk/**,kernels/fk/**,kernels/ik/**,kernels/iak/**}"


def _run_quiet(cmd: list[str]) -> None:
    """Like `subprocess.run(cmd, check=True)`, but captures stdout/stderr instead of letting them
    flood the notebook cell -- ISIS/ASP binaries are noisy (progress bars, Qt library init
    messages, verbose completion logs) and inherit the kernel's own stdout/stderr by default.
    Printed only on failure, so nothing useful is lost for debugging."""
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="")
        result.check_returncode()


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
    _run_quiet(["downloadIsisData", "base", str(isisdata), "--include", _BASE_KERNEL_INCLUDE])
    _run_quiet(["downloadIsisData", "lro", str(isisdata), "--no-kernels"])


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
    _run_quiet(["lrowac2isis", f"from={edr.img_path}", f"to={out_prefix}"])
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
    config = config or load_config()
    _run_quiet(["spiceinit", f"from={cub_path}", "web=yes"])
    return SpiceinitResult(cub_path=cub_path)


@dataclasses.dataclass(frozen=True)
class LrowaccalResult:
    cub_path: Path


def run_lrowaccal(spiceinit_result: SpiceinitResult, config: TrntestConfig | None = None) -> LrowaccalResult:
    config = config or load_config()
    in_path = spiceinit_result.cub_path
    out_path = in_path.with_name(in_path.stem + ".cal.cub")
    _run_quiet(["lrowaccal", f"from={in_path}", f"to={out_path}"])
    return LrowaccalResult(cub_path=out_path)


@dataclasses.dataclass(frozen=True)
class FramestitchResult:
    cub_path: Path


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
    _run_quiet(
        [
            "framestitch",
            f"EVEN={even.cub_path}",
            f"ODD={odd.cub_path}",
            f"TO={out_path}",
            f"FLIP={'TRUE' if flip else 'FALSE'}",
        ]
    )
    return FramestitchResult(cub_path=out_path)
