"""Project-specific configuration: service endpoints, cache/output paths, and the specific LROC
EDR/CDR products this demo targets. Defaults match the values this repo has always used; override
via a TOML file (see `load_config`) or the `TRNTEST_CACHE_ROOT`/`TRNTEST_OUTPUT_DIR` env vars.

Field naming note: `edr_*`/`cdr_*` fields keep their literal PDS terminology since they're exact
identifiers into the PDS archive's own EDR/CDR product catalog (cross-reference
`docs/data-sources.md` or the PDS site itself) -- this is a different, more detail-oriented audience
than someone just calling `camera.fetch_frame_timing()`/`wac.fetch_vis_mosaic()`. EDR ("Experiment
Data Record") and CDR ("Calibrated Data Record") are two different processing levels of the *same*
LROC acquisition: `fetch_frame_timing()` reads only the EDR product's metadata label (frame timing),
never its pixel data; the actual image pixel data used for visual comparison comes entirely from the
CDR counterpart via `wac.fetch_vis_mosaic()`.
"""

import dataclasses
import os
import tomllib
from pathlib import Path

CONFIG_PATH_ENV_VAR = "TRNTEST_CONFIG"
CACHE_ROOT_ENV_VAR = "TRNTEST_CACHE_ROOT"
OUTPUT_DIR_ENV_VAR = "TRNTEST_OUTPUT_DIR"
DEFAULT_CONFIG_FILENAME = "trntest.toml"

DEFAULT_CACHE_ROOT = Path("/workspace/cache")
DEFAULT_OUTPUT_DIR = Path("/workspace/output")

DEFAULT_NAIF_BASE_URL = "https://naif.jpl.nasa.gov/pub/naif/pds/data/lro-l-spice-6-v1.0/lrosp_1000/"
DEFAULT_LUNASERV_BASE_URL = "https://wms.im-ldi.com/lunaserv/lunaserv_stage?"
DEFAULT_LUNASERV_SRS = "IAU2000:30100"
DEFAULT_LROC_BASE_URL = "https://pds.lroc.im-ldi.com/data/"
DEFAULT_LROC_EDR_DATASET = "LRO-L-LROC-2-EDR-V1.0"
DEFAULT_LROC_CDR_DATASET = "LRO-L-LROC-3-CDR-V1.0"

# The WAC EDR/CDR product chosen for this demo -- see docs/data-sources.md.
DEFAULT_EDR_VOLUME = "LROLRC_0041C"
DEFAULT_EDR_SUBDIR = "ESM4"
DEFAULT_EDR_DOY = "2019334"
DEFAULT_EDR_PRODUCT = "M1329714703CE"
DEFAULT_CDR_VOLUME = "LROLRC_1041C"
DEFAULT_CDR_PRODUCT = "M1329714703CC"

# Frame index (0-based) within the product's `nframes` framelets to pose the camera at -- chosen to
# land in sunlit terrain, not the shadowed start of the swath (see docs/data-sources.md).
DEFAULT_TARGET_FRAME_INDEX = 440
DEFAULT_IMAGE_SIZE = 256

# LROC EDR/CDR SIS color-mode cross-track FOV (see camera.py for the full derivation/rationale).
DEFAULT_WAC_VIS_COLOR_FOV_DEG = 61.4

DEFAULT_MOON_RADIUS_KM = 1737.4
DEFAULT_MOON_RADIUS_M = DEFAULT_MOON_RADIUS_KM * 1000.0

DEFAULT_DEM_TARGET_GSD_M = 100.0
DEFAULT_DEM_PADDING_FRACTION = 0.3


@dataclasses.dataclass(frozen=True)
class TrntestConfig:
    """Resolved configuration for a `Session`/pipeline run. Construct via `load_config()`, not
    directly, unless you specifically want the built-in defaults with no file/env-var resolution."""

    cache_root: Path = DEFAULT_CACHE_ROOT
    output_dir: Path = DEFAULT_OUTPUT_DIR

    naif_base_url: str = DEFAULT_NAIF_BASE_URL
    lunaserv_base_url: str = DEFAULT_LUNASERV_BASE_URL
    lunaserv_srs: str = DEFAULT_LUNASERV_SRS
    lroc_base_url: str = DEFAULT_LROC_BASE_URL
    lroc_edr_dataset: str = DEFAULT_LROC_EDR_DATASET
    lroc_cdr_dataset: str = DEFAULT_LROC_CDR_DATASET

    edr_volume: str = DEFAULT_EDR_VOLUME
    edr_subdir: str = DEFAULT_EDR_SUBDIR
    edr_doy: str = DEFAULT_EDR_DOY
    edr_product: str = DEFAULT_EDR_PRODUCT
    cdr_volume: str = DEFAULT_CDR_VOLUME
    cdr_product: str = DEFAULT_CDR_PRODUCT

    target_frame_index: int = DEFAULT_TARGET_FRAME_INDEX
    image_size: int = DEFAULT_IMAGE_SIZE
    wac_vis_color_fov_deg: float = DEFAULT_WAC_VIS_COLOR_FOV_DEG
    moon_radius_km: float = DEFAULT_MOON_RADIUS_KM
    dem_target_gsd_m: float = DEFAULT_DEM_TARGET_GSD_M
    dem_padding_fraction: float = DEFAULT_DEM_PADDING_FRACTION

    @property
    def moon_radius_m(self) -> float:
        return self.moon_radius_km * 1000.0


_PATH_FIELDS = ("cache_root", "output_dir")


def _resolve_config_file_path(path: str | Path | None) -> Path | None:
    if path is not None:
        return Path(path)
    env_path = os.environ.get(CONFIG_PATH_ENV_VAR)
    if env_path:
        return Path(env_path)
    cwd_path = Path.cwd() / DEFAULT_CONFIG_FILENAME
    if cwd_path.is_file():
        return cwd_path
    return None


def _validate_keys(raw: dict, base: TrntestConfig) -> None:
    known = {f.name for f in dataclasses.fields(base)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Unknown trntest config key(s) {sorted(unknown)!r} -- valid keys are {sorted(known)!r}")


def _coerce_path_fields(raw: dict) -> dict:
    return {k: (Path(v) if k in _PATH_FIELDS else v) for k, v in raw.items()}


def _apply_env_overrides(config: TrntestConfig) -> TrntestConfig:
    if CACHE_ROOT_ENV_VAR in os.environ:
        config = dataclasses.replace(config, cache_root=Path(os.environ[CACHE_ROOT_ENV_VAR]))
    if OUTPUT_DIR_ENV_VAR in os.environ:
        config = dataclasses.replace(config, output_dir=Path(os.environ[OUTPUT_DIR_ENV_VAR]))
    return config


def load_config(path: str | Path | None = None) -> TrntestConfig:
    """Resolve a `TrntestConfig`.

    Discovery order (highest to lowest priority):
      1. `path`, if given.
      2. `TRNTEST_CONFIG` env var (path to a TOML file).
      3. `./trntest.toml` in the current working directory.
      4. Built-in defaults.

    After that, `TRNTEST_CACHE_ROOT`/`TRNTEST_OUTPUT_DIR` env vars always override `cache_root`/
    `output_dir` on top of whatever was resolved above.
    """
    config = TrntestConfig()

    file_path = _resolve_config_file_path(path)
    if file_path is not None:
        with open(file_path, "rb") as f:
            raw = tomllib.load(f)
        _validate_keys(raw, config)
        raw = _coerce_path_fields(raw)
        config = dataclasses.replace(config, **raw)

    return _apply_env_overrides(config)
