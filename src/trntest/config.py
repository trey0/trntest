"""Project-specific configuration: service endpoints, cache/output paths, and the specific LROC
EDR/CDR products this demo targets. Override via a TOML file (see `load_config`) or the
`TRNTEST_CACHE_ROOT`/`TRNTEST_OUTPUT_DIR` env vars.
"""

import dataclasses
import os
import tomllib
from pathlib import Path

CONFIG_PATH_ENV_VAR = "TRNTEST_CONFIG"
CACHE_ROOT_ENV_VAR = "TRNTEST_CACHE_ROOT"
OUTPUT_DIR_ENV_VAR = "TRNTEST_OUTPUT_DIR"
SCRATCH_DIR_ENV_VAR = "TRNTEST_SCRATCH_DIR"
DEFAULT_CONFIG_FILENAME = "trntest.toml"

DEFAULT_CACHE_ROOT = Path("/workspace/cache")
DEFAULT_OUTPUT_DIR = Path("/workspace/output")
# Large, disposable intermediate files (e.g. the ISIS/CSM WAC reprojection spike's intermediate
# cubes) -- not for final demo artifacts (that's output_dir) -- see docs/environment.md.
DEFAULT_SCRATCH_DIR = Path("/workspace/scratch")

DEFAULT_NAIF_BASE_URL = "https://naif.jpl.nasa.gov/pub/naif/pds/data/lro-l-spice-6-v1.0/lrosp_1000/"
DEFAULT_LUNASERV_BASE_URL = "https://wms.im-ldi.com/lunaserv/lunaserv_stage?"
# Lunaserv's per-request-parametrized local Orthographic CRS -- `{c_lon}`/`{c_lat}` are filled in
# per camera footprint (`dem_ortho.fetch_dem_and_ortho`) with that footprint's own center, giving
# isotropic meter pixels everywhere. Replaces the native unprojected geographic grid
# (`IAU2000:30100`), whose degree-pixels are anisotropic away from the equator. See
# docs/data-sources/lunaserv-wms.md.
DEFAULT_LUNASERV_SRS_TEMPLATE = "IAU2000:30166,9001,{c_lon:.6f},{c_lat:.6f}"
# "LROC WAC 643 nm Normalized Reflectance" -- `config.lunaserv_ortho_layer` only, for the deprecated
# `ortho_source="lunaserv_wms"` fallback path (the live default ortho source is WAC_EMP's own PDS4
# archive, see `DEFAULT_WAC_EMP_BASE_URL` below). See docs/data-sources/lunaserv-wms.md for why this
# layer over the alternatives Lunaserv offers.
DEFAULT_LUNASERV_ORTHO_LAYER = "luna_wac_normalized_reflectance"
DEFAULT_LROC_BASE_URL = "https://pds.lroc.im-ldi.com/data/"
DEFAULT_LROC_EDR_DATASET = "LRO-L-LROC-2-EDR-V1.0"
DEFAULT_ODE_BASE_URL = "https://oderest.rsl.wustl.edu/live2/"

# Reference/regression-test WAC EDR product -- not the live default image (that's the
# checked-in, frozen `dataset_manifest.csv`; see docs/environment.md's "Multi-agent worktrees"
# section); this is a known-good fallback/test fixture for TrntestConfig()'s built-in defaults. See
# docs/data-sources/lroc-wac-edr-cdr.md, "Reference/regression-test EDR products".
DEFAULT_EDR_VOLUME = "LROLRC_0041C"
DEFAULT_EDR_SUBDIR = "ESM4"
DEFAULT_EDR_DOY = "2019334"
DEFAULT_EDR_PRODUCT = "M1329714703CE"

# Frame index (0-based) within the reference product's `nframes` framelets to pose the camera at --
# chosen to land in sunlit terrain, not the shadowed start of the swath. `dataset.generate_dataset()`
# overrides this per-image on the live, catalog-driven path.
DEFAULT_TARGET_FRAME_INDEX = 440

# `hillshade`/`reproject`'s fixed `sat_sim --image-size` (square, `fu == fv` -- see
# `camera.solve_corrected_fov`). The rendered footprint's own size doesn't depend on this value (it's
# solved from the real WAC crop's footprint first, then sampled at whatever pixel count this is), so
# it's chosen directly as a target ground sample distance: ~100 m/px (matching the DEM/ortho inputs'
# own resolution) on this project's reference candidate (`M1327210646CE`, ~131 km render footprint).
# A different candidate's footprint size differs slightly with slant range/off-nadir angle -- not
# recomputed per candidate, see docs/resolution-investigation.md.
DEFAULT_IMAGE_SIZE = 1316

# LROC EDR/CDR SIS color-mode cross-track FOV (see camera.py for the full derivation/rationale).
DEFAULT_WAC_VIS_COLOR_FOV_DEG = 61.4

MOON_RADIUS_KM = 1737.4
MOON_RADIUS_M = MOON_RADIUS_KM * 1000.0

# GRAIL-derived lunar GM (DE430/DE440), km^3/s^2 -- not available via spice.bodvrd from any kernel
# this project furnishes (pck00010.tpc has body radii/orientation, not GM), so kept as a plain
# constant here rather than resolved from SPICE. A physical constant, not something a user has any
# need to override -- same treatment as MOON_RADIUS_KM/MOON_RADIUS_M above, not a TrntestConfig
# field. Only consumer is maneuver_detection.py's osculating two-body element computation, where its
# precision is irrelevant next to the cm/s-scale signal being detected.
MOON_GM_KM3_S2 = 4902.80007

# Working-grid resolution (the per-camera local Orthographic CRS both the ortho and the final,
# locally-reprojected DEM share) -- despite the name, this no longer also governs the DEM's own
# *fetch* resolution from Lunaserv (see `dem_native_ppd`/`lunaserv_dem_srs` below); it's fetched
# separately, at its own real native resolution, then reprojected onto this working grid.
DEFAULT_DEM_TARGET_GSD_M = 100.0
DEFAULT_DEM_PADDING_FRACTION = 0.3

# Deprecated -- Lunaserv's native, unprojected geographic grid for the Moon. Only used by
# `lunaserv_wms.fetch_dem_native`/`lunaserv_wms.reproject_dem_to_local_grid`, the pre-Astropedia DEM
# path kept for reference/comparison; no longer called by `fetch_dem_and_ortho`'s default path.
# Superseded by an unfixable crosshatch artifact baked into Lunaserv's own native DTM tile -- see
# docs/data-sources/lunaserv-wms.md. `dem_native_ppd`/`lunaserv_dem_srs` are valid config for this
# deprecated path only, not for the live default (`astropedia_gld100_url` below).
DEFAULT_LUNASERV_DEM_SRS = "IAU2000:30100"
# Lunaserv's global numeric DTM layer's native resolution ceiling, regardless of requested CRS --
# deprecated path only (see `lunaserv_dem_srs` above). See docs/data-sources/lunaserv-wms.md.
DEFAULT_DEM_NATIVE_PPD = 128.0

# Live default DEM source: USGS Astropedia's flat-file GLD100 distribution, not Lunaserv's WMS.
# 100.0 m/px, Int16, Equidistant Cylindrical (lon_0=180) GeoTIFF, 79 deg N to 79 deg S coverage,
# ~10 GB. Not a Cloud-Optimized GeoTIFF (row-strip, not 2D-tiled), so `cache.fetch_astropedia_gld100`
# downloads and caches the whole file locally once rather than reading it with remote windowed
# reads. See docs/data-sources/astropedia-gld100.md.
DEFAULT_ASTROPEDIA_GLD100_URL = "https://planetarymaps.usgs.gov/mosaic/Lunar_LRO_WAC_GLD100_DTM_79S79N_100m_v1.1.tif"

# Live default ortho/texture source: ASU/LROC's WAC_EMP product, fetched directly from its own PDS4
# archive (`ortho_wac_emp.wac_emp_tile_id_for_bbox`/`fetch_wac_emp_reflectance`) rather than through
# Lunaserv's WMS render, which carries an uncorrected affine display stretch. One base URL covers
# every tile: the tile's own product ID, resolved per footprint, is appended directly. See
# docs/data-sources/wac-emp-pds4.md.
DEFAULT_WAC_EMP_BASE_URL = (
    "https://pds.mcp.nasa.gov/data/store/img/lunar_reconnaissance_orbiter/pds4/lroc/"
    "lro-l-lroc-5-rdr/LROLRC_2001/DATA/MDR/WAC_EMP/"
)

# Robbins (2019) lunar crater database -- ~1.3M craters (D>=1km), distributed by USGS Astropedia's
# PDS Annex. This exact URL is a CKAN resource-download route, not the catalog/search page a
# browser lands on -- the indexed search-page URLs 404 due to a USGS-side site reorganization,
# unrelated to bot-protection. See docs/data-sources/robbins-craters.md.
DEFAULT_ROBBINS_CRATERS_URL = (
    "https://astrogeology.usgs.gov/ckan/dataset/f89f5478-b69a-486c-b9b5-30d7b0c5ad2b/"
    "resource/c4f25cc2-4f8a-4207-a845-5e176da3ac5a/download/lunar_crater_database_robbins_2018"
)

# USGS's own S3-hosted LRO ISIS kernel-db tree -- the same source ISIS's own `spiceinit` (web or
# local) draws from for LRO, not proxied from NAIF. Anonymously readable over plain HTTPS. See
# docs/data-sources/spice-kernels-isis.md.
DEFAULT_ISIS_KERNEL_BASE_URL = "https://asc-isisdata.s3.us-west-2.amazonaws.com/usgs_data/lro/"

# Which source resolves the WAC CK (pointing) kernel(s): "isis_resolved" (live default -- see
# `spice_kernels.select_isis_wac_ck_kernels`) asks a real ISIS `spiceinit web=yes` run what it
# furnishes; "naif_metakernel" (`spice_kernels.select_naif_wac_ck_kernels`) resolves via NAIF's own
# metakernel and is kept for comparison. Direct verification found no measurable pointing
# difference between the two -- "isis_resolved" is the default because it matches ISIS's own kernel
# resolution by construction, not because it fixes a known discrepancy. Not a silent fallback: it
# raises if it can't resolve a kernel for the target date. See
# docs/data-sources/spice-kernels-isis.md.
DEFAULT_WAC_CK_SOURCE = "isis_resolved"


@dataclasses.dataclass(frozen=True)
class TrntestConfig:
    """Resolved configuration for a `Session`/pipeline run. Construct via `load_config()`, not
    directly, unless you specifically want the built-in defaults with no file/env-var resolution."""

    cache_root: Path = DEFAULT_CACHE_ROOT
    output_dir: Path = DEFAULT_OUTPUT_DIR
    scratch_dir: Path = DEFAULT_SCRATCH_DIR

    naif_base_url: str = DEFAULT_NAIF_BASE_URL
    lunaserv_base_url: str = DEFAULT_LUNASERV_BASE_URL
    lunaserv_srs_template: str = DEFAULT_LUNASERV_SRS_TEMPLATE
    lunaserv_ortho_layer: str = DEFAULT_LUNASERV_ORTHO_LAYER
    lunaserv_dem_srs: str = DEFAULT_LUNASERV_DEM_SRS  # deprecated path only, see comment above
    dem_native_ppd: float = DEFAULT_DEM_NATIVE_PPD  # deprecated path only, see comment above
    astropedia_gld100_url: str = DEFAULT_ASTROPEDIA_GLD100_URL
    wac_emp_base_url: str = DEFAULT_WAC_EMP_BASE_URL
    robbins_craters_url: str = DEFAULT_ROBBINS_CRATERS_URL
    isis_kernel_base_url: str = DEFAULT_ISIS_KERNEL_BASE_URL
    wac_ck_source: str = DEFAULT_WAC_CK_SOURCE  # "isis_resolved" | "naif_metakernel" (deprecated)
    lroc_base_url: str = DEFAULT_LROC_BASE_URL
    lroc_edr_dataset: str = DEFAULT_LROC_EDR_DATASET
    ode_base_url: str = DEFAULT_ODE_BASE_URL

    # `edr_*` field names keep their literal PDS terminology -- exact identifiers into the PDS
    # archive's own EDR catalog (docs/data-sources/lroc-wac-edr-cdr.md). EDR ("Experiment Data
    # Record") is what `camera.fetch_frame_timing()`/`isis_wac.py`'s pipeline both work from; the CDR
    # ("Calibrated Data Record") counterpart `candidate_window.py` still matches against
    # (`catalog.find_matching_cdr`) for manifest provenance has no fetch path or config fields of its
    # own -- nothing in this project fetches CDR pixel data any more, only `isis_wac.py`'s
    # EDR-based ISIS pipeline.
    edr_volume: str = DEFAULT_EDR_VOLUME
    edr_subdir: str = DEFAULT_EDR_SUBDIR
    edr_doy: str = DEFAULT_EDR_DOY
    edr_product: str = DEFAULT_EDR_PRODUCT

    target_frame_index: int = DEFAULT_TARGET_FRAME_INDEX
    image_size: int = DEFAULT_IMAGE_SIZE
    wac_vis_color_fov_deg: float = DEFAULT_WAC_VIS_COLOR_FOV_DEG
    dem_target_gsd_m: float = DEFAULT_DEM_TARGET_GSD_M
    dem_padding_fraction: float = DEFAULT_DEM_PADDING_FRACTION


_PATH_FIELDS = ("cache_root", "output_dir", "scratch_dir")


def _resolve_config_file_path(path: str | Path | None) -> Path | None:
    """Resolve which config file `load_config` should read, per its own documented precedence order."""
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
    """Check that every key in `raw` is a `TrntestConfig` field.

    :raises ValueError: If `raw` has a key that isn't a `TrntestConfig` field.
    """
    known = {f.name for f in dataclasses.fields(base)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Unknown trntest config key(s) {sorted(unknown)!r} -- valid keys are {sorted(known)!r}")


def _coerce_path_fields(raw: dict) -> dict:
    """Convert `_PATH_FIELDS` values in `raw` from `str` to `Path`."""
    return {k: (Path(v) if k in _PATH_FIELDS else v) for k, v in raw.items()}


def _apply_env_overrides(config: TrntestConfig) -> TrntestConfig:
    """Apply the `TRNTEST_CACHE_ROOT`/`TRNTEST_OUTPUT_DIR`/`TRNTEST_SCRATCH_DIR` env var overrides."""
    if CACHE_ROOT_ENV_VAR in os.environ:
        config = dataclasses.replace(config, cache_root=Path(os.environ[CACHE_ROOT_ENV_VAR]))
    if OUTPUT_DIR_ENV_VAR in os.environ:
        config = dataclasses.replace(config, output_dir=Path(os.environ[OUTPUT_DIR_ENV_VAR]))
    if SCRATCH_DIR_ENV_VAR in os.environ:
        config = dataclasses.replace(config, scratch_dir=Path(os.environ[SCRATCH_DIR_ENV_VAR]))
    return config


def load_config(path: str | Path | None = None) -> TrntestConfig:
    """Resolve a `TrntestConfig`.

    Discovery order (highest to lowest priority):
      1. `path`, if given.
      2. `TRNTEST_CONFIG` env var (path to a TOML file).
      3. `./trntest.toml` in the current working directory.
      4. Built-in defaults.

    After that, `TRNTEST_CACHE_ROOT`/`TRNTEST_OUTPUT_DIR`/`TRNTEST_SCRATCH_DIR` env vars always
    override `cache_root`/`output_dir`/`scratch_dir` on top of whatever was resolved above.
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
