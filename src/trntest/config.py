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
SCRATCH_DIR_ENV_VAR = "TRNTEST_SCRATCH_DIR"
DEFAULT_CONFIG_FILENAME = "trntest.toml"

DEFAULT_CACHE_ROOT = Path("/workspace/cache")
DEFAULT_OUTPUT_DIR = Path("/workspace/output")
# Large, disposable intermediate files (e.g. the ISIS/CSM WAC reprojection spike's intermediate
# cubes) -- not for final demo artifacts (that's output_dir) -- see docs/environment.md.
DEFAULT_SCRATCH_DIR = Path("/workspace/scratch")

DEFAULT_NAIF_BASE_URL = "https://naif.jpl.nasa.gov/pub/naif/pds/data/lro-l-spice-6-v1.0/lrosp_1000/"
DEFAULT_LUNASERV_BASE_URL = "https://wms.im-ldi.com/lunaserv/lunaserv_stage?"
# `IAU2000:30166` is Lunaserv's per-request-parametrized local Orthographic CRS (real Moon radius
# 1,737,400 m, confirmed via a live GetMap + gdalinfo check -- see docs/data-sources.md) --
# `{c_lon}`/`{c_lat}` are filled in per camera footprint (`lunaserv.fetch_dem_and_ortho`) with that
# footprint's own center, so the fetched DEM/ortho tile has genuinely isotropic meter pixels
# everywhere. Replaced the previously-used native unprojected geographic grid (`IAU2000:30100`),
# whose degree-pixels are anisotropic away from the equator -- see `lunaserv.fetch_dem_and_ortho`'s
# docstring for why that anisotropy matters (ASP `mapproject --ref-map` doesn't preserve it).
DEFAULT_LUNASERV_SRS_TEMPLATE = "IAU2000:30166,9001,{c_lon:.6f},{c_lat:.6f}"
# "LROC WAC 643 nm Normalized Reflectance" -- a >100,000-image photometric composite, not the raw
# ~15,000-image "luna_wac_global" mosaic -- chosen for having ~4x fewer isolated single-pixel
# outliers at comparable resolution (see docs/data-sources.md). sat_sim does no illumination
# modeling of its own (pure geometric reprojection of whatever's in the ortho -- see
# docs/data-sources.md), so lunaserv.fetch_dem_and_ortho blends a real-sun-lit hillshade onto this
# layer rather than relying on any shading baked into the source imagery.
DEFAULT_LUNASERV_ORTHO_LAYER = "luna_wac_normalized_reflectance"
DEFAULT_LROC_BASE_URL = "https://pds.lroc.im-ldi.com/data/"
DEFAULT_LROC_EDR_DATASET = "LRO-L-LROC-2-EDR-V1.0"
DEFAULT_LROC_CDR_DATASET = "LRO-L-LROC-3-CDR-V1.0"
DEFAULT_ODE_BASE_URL = "https://oderest.rsl.wustl.edu/live2/"

# Reference/regression-test WAC EDR/CDR product -- not the live default image (that's the
# checked-in dataset_manifest.csv, frozen output of the now-removed catalog-driven selection
# notebook, see docs/history.md); this is a known-good fallback/test fixture for
# TrntestConfig()'s built-in defaults. See docs/data-sources/lroc-wac-edr-cdr.md, "Reference/regression-test EDR
# products".
DEFAULT_EDR_VOLUME = "LROLRC_0041C"
DEFAULT_EDR_SUBDIR = "ESM4"
DEFAULT_EDR_DOY = "2019334"
DEFAULT_EDR_PRODUCT = "M1329714703CE"
DEFAULT_CDR_VOLUME = "LROLRC_1041C"
DEFAULT_CDR_PRODUCT = "M1329714703CC"

# Frame index (0-based) within the product's `nframes` framelets to pose the camera at, for the
# reference product above -- chosen to land in sunlit terrain, not the shadowed start of the swath
# (see docs/history.md, Phase 2). dataset.generate_dataset() overrides this per-image on the live,
# catalog-driven path.
DEFAULT_TARGET_FRAME_INDEX = 440
DEFAULT_IMAGE_SIZE = 256

# LROC EDR/CDR SIS color-mode cross-track FOV (see camera.py for the full derivation/rationale).
DEFAULT_WAC_VIS_COLOR_FOV_DEG = 61.4

MOON_RADIUS_KM = 1737.4
MOON_RADIUS_M = MOON_RADIUS_KM * 1000.0

# GRAIL-derived lunar GM (DE430/DE440), km^3/s^2 -- not available via spice.bodvrd from any kernel
# this project furnishes (pck00010.tpc has body radii/orientation, not GM), so kept as a plain
# constant here rather than resolved from SPICE. Only consumer is maneuver_detection.py's osculating
# two-body element computation, where its precision is irrelevant next to the cm/s-scale signal
# being detected.
DEFAULT_MOON_GM_KM3_S2 = 4902.80007

# Working-grid resolution (the per-camera local Orthographic CRS both the ortho and the final,
# locally-reprojected DEM share) -- despite the name, this no longer also governs the DEM's own
# *fetch* resolution from Lunaserv (see `dem_native_ppd`/`lunaserv_dem_srs` below); it's fetched
# separately, at its own real native resolution, then reprojected onto this working grid.
DEFAULT_DEM_TARGET_GSD_M = 100.0
DEFAULT_DEM_PADDING_FRACTION = 0.3

# Deprecated -- Lunaserv's native, unprojected geographic grid for the Moon. Only used by
# `lunaserv.fetch_dem_native`/`lunaserv.reproject_dem_to_local_grid` (the pre-Astropedia DEM path,
# kept for reference/comparison, no longer called by `fetch_dem_and_ortho`'s default path -- see
# `docs/history.md`'s dated entry). Superseded because a second, axis-aligned crosshatch artifact
# was confirmed baked into Lunaserv's own native DTM tile itself (FFT-confirmed, present regardless
# of requested ppd/CRS/resampling kernel) -- not fixable client-side, since Lunaserv exposes no
# resampling control (confirmed via several vendor GetMap parameter probes, all ignored) and no
# backing-store metadata. `dem_native_ppd`/`lunaserv_dem_srs` remain valid config for that deprecated
# path specifically, not for the live default (`astropedia_gld100_url` below).
DEFAULT_LUNASERV_DEM_SRS = "IAU2000:30100"
# Confirmed empirically (FFT/periodicity analysis of a live resolution sweep, and independently by
# `luna_wac_dtm_numeric_meters_absolute`'s own `GetCapabilities` abstract, which states "available
# at 128 ppd in the same tiled format as the GLD100"): this is the real native resolution ceiling
# of Lunaserv's global numeric DTM layer, regardless of which CRS a request uses.
DEFAULT_DEM_NATIVE_PPD = 128.0

# Live default DEM source: USGS Astropedia's flat-file GLD100 distribution, not Lunaserv's WMS.
# Confirmed empirically (`gdalinfo` + a live windowed pull + the same FFT/periodicity diagnostic that
# found Lunaserv's artifact): a genuine 100.0 m/px, Int16, Equidistant Cylindrical (lon_0=180)
# GeoTIFF, 79 deg N to 79 deg S coverage (`gdalinfo`'s own corner coordinates: 79d0'6.57" both ways),
# ~10 GB. Shows none of Lunaserv's artifact at the frequencies it was confirmed elevated at. Not a
# Cloud-Optimized GeoTIFF (`Block=109165x1` -- row-strip, not 2D-tiled), so a remote windowed
# `/vsicurl/` read pulls full-width row strips rather than a small tile (~64s for one small AOI in
# testing) -- `cache.fetch_astropedia_gld100` downloads and caches the whole file locally once
# instead (resumable via `curl -C -`), after which local windowed reads are fast. See
# `docs/data-sources/astropedia-gld100.md`.
DEFAULT_ASTROPEDIA_GLD100_URL = "https://planetarymaps.usgs.gov/mosaic/Lunar_LRO_WAC_GLD100_DTM_79S79N_100m_v1.1.tif"

# Live default ortho/texture source: ASU/LROC's WAC_EMP product, fetched directly from its own PDS4
# archive rather than through Lunaserv's WMS render (see `lunaserv.wac_emp_tile_id_for_bbox`/
# `fetch_wac_emp_reflectance` and `docs/data-sources/wac-emp-pds4.md`) -- Lunaserv's
# `luna_wac_normalized_reflectance` WMS layer was confirmed to carry a real affine display stretch,
# not raw reflectance, mirroring the same "don't trust Lunaserv WMS rendering" lesson that already
# moved the DEM source to Astropedia's flat-file GLD100.
# One base URL covers every real tile this project fetches (the tile's own product ID, resolved per
# footprint by `wac_emp_tile_id_for_bbox`, is appended directly) -- confirmed live via the archive's
# own S3 listing, not guessed from a single example filename.
DEFAULT_WAC_EMP_BASE_URL = (
    "https://pds.mcp.nasa.gov/data/store/img/lunar_reconnaissance_orbiter/pds4/lroc/"
    "lro-l-lroc-5-rdr/LROLRC_2001/DATA/MDR/WAC_EMP/"
)

# Robbins (2019) lunar crater database -- ~1.3-2M craters, distributed by USGS Astropedia's PDS
# Annex (see docs/data-sources/robbins-craters.md). This exact URL is a CKAN
# resource-download route, not the catalog/search page a browser lands on -- confirmed live via
# `curl` (200, `Content-Type: application/zip`, ~92MB); the search-page/details-page URLs that
# search engines index for this dataset (e.g. `search/map/moon_crater_database_v1_robbins`) 404 on
# the live site as of this writing, a real USGS-side site reorganization unrelated to any
# bot-protection -- see `docs/plan.md`'s open items for the full investigation trail. Found by
# manually navigating the current live catalog page's own download link, not guessed.
DEFAULT_ROBBINS_CRATERS_URL = (
    "https://astrogeology.usgs.gov/ckan/dataset/f89f5478-b69a-486c-b9b5-30d7b0c5ad2b/"
    "resource/c4f25cc2-4f8a-4207-a845-5e176da3ac5a/download/lunar_crater_database_robbins_2018"
)

# USGS's own S3-hosted LRO ISIS kernel-db tree -- the same source ISIS's own `spiceinit web=yes` (and
# local, non-web spiceinit) draws from for LRO. Confirmed live: LRO's `rclone` remote
# (`/opt/conda/envs/isis/etc/isis/rclone.conf`'s `[lro]` alias) has no `naif:` union, unlike
# Dawn/Cassini/TGO -- LRO's ISIS kernel tree isn't proxied from NAIF at all, it's this bucket
# directly, anonymously readable over plain HTTPS. See docs/data-sources.md for the full derivation.
DEFAULT_ISIS_KERNEL_BASE_URL = "https://asc-isisdata.s3.us-west-2.amazonaws.com/usgs_data/lro/"

# Which source resolves the WAC CK (pointing) kernel(s): "isis_resolved" (live default -- see
# spice_kernels.select_isis_wac_ck_kernels) asks a real ISIS `spiceinit web=yes` run what it actually
# furnishes, fixing a confirmed ~11-13km pointing discrepancy vs. the deprecated
# "naif_metakernel" path (spice_kernels.select_naif_wac_ck_kernels, kept for reference/comparison --
# see docs/history.md's dated entry). Not a silent fallback -- "isis_resolved" raises loudly if it
# can't resolve a kernel for the target date, rather than risk reintroducing the discrepancy this
# fixes.
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
    lunaserv_dem_srs: str = DEFAULT_LUNASERV_DEM_SRS  # deprecated path only, see docstring above
    dem_native_ppd: float = DEFAULT_DEM_NATIVE_PPD  # deprecated path only, see docstring above
    astropedia_gld100_url: str = DEFAULT_ASTROPEDIA_GLD100_URL
    wac_emp_base_url: str = DEFAULT_WAC_EMP_BASE_URL
    robbins_craters_url: str = DEFAULT_ROBBINS_CRATERS_URL
    isis_kernel_base_url: str = DEFAULT_ISIS_KERNEL_BASE_URL
    wac_ck_source: str = DEFAULT_WAC_CK_SOURCE  # "isis_resolved" | "naif_metakernel" (deprecated)
    lroc_base_url: str = DEFAULT_LROC_BASE_URL
    lroc_edr_dataset: str = DEFAULT_LROC_EDR_DATASET
    lroc_cdr_dataset: str = DEFAULT_LROC_CDR_DATASET
    ode_base_url: str = DEFAULT_ODE_BASE_URL

    edr_volume: str = DEFAULT_EDR_VOLUME
    edr_subdir: str = DEFAULT_EDR_SUBDIR
    edr_doy: str = DEFAULT_EDR_DOY
    edr_product: str = DEFAULT_EDR_PRODUCT
    cdr_volume: str = DEFAULT_CDR_VOLUME
    cdr_product: str = DEFAULT_CDR_PRODUCT

    target_frame_index: int = DEFAULT_TARGET_FRAME_INDEX
    image_size: int = DEFAULT_IMAGE_SIZE
    wac_vis_color_fov_deg: float = DEFAULT_WAC_VIS_COLOR_FOV_DEG
    moon_gm_km3_s2: float = DEFAULT_MOON_GM_KM3_S2
    dem_target_gsd_m: float = DEFAULT_DEM_TARGET_GSD_M
    dem_padding_fraction: float = DEFAULT_DEM_PADDING_FRACTION


_PATH_FIELDS = ("cache_root", "output_dir", "scratch_dir")


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
