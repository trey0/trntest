"""Ortho despeckling and shading: the default Hapke bidirectional-reflectance relighting
(ISIS `photomet`, `PHTNAME=HAPKEHEN`) and its plain-Lambertian fallback, plus the photometric-angle
geometry both need. `dem_ortho.py` calls `despeckle_and_shade_ortho` as the last step of its own
DEM/ortho fetch pipeline.
"""

from __future__ import annotations

import math
import tempfile
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import rasterio
from matplotlib.colors import LightSource
from rasterio.errors import NotGeoreferencedWarning
from rasterio.warp import transform

from trntest import illumination, isis_wac
from trntest.config import MOON_RADIUS_M, TrntestConfig
from trntest.geo_utils import geographic_crs, local_orthographic_crs, moon_geocentric_crs
from trntest.product_io import atomic_publish
from trntest.subprocess_utils import run_quiet

if TYPE_CHECKING:
    from trntest.camera import Camera

# Placeholder Hapke-Henyey-Greenstein coefficients for `hapke_shade_ortho` -- illustrative values in
# each parameter's documented valid range (see ISIS's photomet.xml), not calibrated against lunar
# photometry. Kept as the `real_hapke_params=False` fallback -- see `fetch_real_hapke_params()` below
# for the calibrated alternative.
_HAPKE_PLACEHOLDER_PARAMS = {"wh": 0.52, "hg1": 0.213, "hg2": 1.0, "hh": 0.17, "b0": 0.025, "theta": 0.0}

# The ortho texture this project fetches (`config.lunaserv_ortho_layer`, WAC_EMP's own PDS4 tile) is
# itself a photometric composite, not raw albedo: every pixel is normalized to a reference geometry of
# 30 deg incidence, 0 deg emission, 30 deg phase, using an empirical photometric function (Boyd et al.
# 2012), not Hapke. `hapke_shade_ortho` relights it for a candidate's own geometry by multiplying by
# H(i,e,g)/H(these reference angles), where H is this project's own Hapke evaluation
# (`_hapke_reflectance`) -- an approximation of undoing Boyd et al.'s normalization, not a bit-exact
# inverse. The Sato et al. (2014) Hapke parameters this project samples (`fetch_real_hapke_params`)
# were themselves cross-validated against this same WAC photometric dataset, so this isn't an
# arbitrary stand-in.
REFERENCE_INCIDENCE_DEG = 30.0
REFERENCE_EMISSION_DEG = 0.0
REFERENCE_PHASE_DEG = 30.0

# ISIS's own spatially-resolved lunar Hapke calibration (Sato et al. 2014's fit, converted to ISIS's
# native parameterization) -- $ISISDATA/lro/calibration/
# WAC_global_7bands_1x1_wbhs70NS_const_each_pole.<version>.cub, a 63-band (7 wavelengths x 9 params)
# global 1deg/px cube. Already part of this project's `isis_wac.ensure_isisdata` fetch (the `lro` ISIS
# data package `lrowaccal`/`spiceinit` already need), not a new download. See
# `fetch_real_hapke_params()` below. Also the archive's own band set for `ortho_wac_emp.py`'s WAC_EMP
# tiles (`config.lunaserv_ortho_layer`'s own wavelength, 643nm, is one of these) -- shared here since
# the calibration is deliberately chosen to describe the same imagery `ortho_wac_emp.py` fetches.
HAPKE_CALIBRATION_WAVELENGTHS_NM = (321, 360, 415, 566, 604, 643, 689)
_HAPKE_CALIBRATION_PARAM_ORDER = ("wh", "hg1", "hg2", "bc0", "hc", "b0", "hh", "theta", "phi")
_HAPKE_CALIBRATION_CUBE_GLOB = "WAC_global_7bands_1x1_wbhs70NS_const_each_pole.*.cub"
# Matches `config.lunaserv_ortho_layer`'s own wavelength (`luna_wac_normalized_reflectance`, 643nm --
# see docs/data-sources/lunaserv-wms.md) -- the calibration should describe the same imagery being
# shaded.
DEFAULT_HAPKE_CALIBRATION_WAVELENGTH_NM = 643

# `dem_ortho.fetch_dem_and_ortho`/`despeckle_and_shade_ortho`'s own `hapke`/`along_track_correction`/
# `real_hapke_params` parameter defaults -- shared with `trn_dataset.TrnTestEntry.dem_ortho_result`'s
# resumption check (via `dem_ortho.ortho_shaded_filename`) so the two can't disagree about which
# shading mode's cached ortho file is "the" default one to resume from. `shade_ortho`'s plain
# Lambertian blend (`hapke=False`), the uncorrected per-pixel geometry
# (`along_track_correction=False`), and the illustrative placeholder Hapke coefficients
# (`real_hapke_params=False`) all remain available as explicit fallbacks.
#
# The DEM-gradient normal-tilt correction (`_terrain_photometric_angles`) and the Hapke-ratio
# relighting correction (`hapke_shade_ortho`) are unconditional, with no parameter to opt out: both are
# judged physically correct on their own terms (independent `campt` validation for the normal-tilt fix;
# WAC_EMP's own documented i=30/e=0/g=30 normalization convention for the Hapke-ratio fix), even though
# neither is confirmed to improve -- and the Hapke-ratio fix is confirmed to worsen -- the
# brightness-matched diff against a WAC crop for the one candidate tested so far. That regression
# remains open and unexplained (see `docs/proposed-tasks/open-items.md`).
DEFAULT_HAPKE_SHADING = True
DEFAULT_ALONG_TRACK_CORRECTION = True
DEFAULT_REAL_HAPKE_PARAMS = True

# `hapke_shade_ortho`'s final, purely cosmetic reflectance->uint8 display stretch
# (`stretch_reflectance_to_uint8`) -- a fixed linear range, not a per-image adaptive/percentile
# stretch, matching this project's general preference for deterministic behavior. Chosen to keep
# typical lunar reflectance at WAC_EMP's own i=30/e=0/g=30 reference geometry (mare ~0.05-0.10,
# highlands ~0.12-0.20, fresh crater rays/ejecta up to ~0.3) inside [0, 255] without saturating most of
# a typical scene. Applied once, at the very end of the pipeline, to `relit_reflectance`
# (`hapke_shade_ortho`'s own output) -- after all physics (relighting), not baked into the input
# texture.
DISPLAY_STRETCH_REFLECTANCE_MIN = 0.0
DISPLAY_STRETCH_REFLECTANCE_MAX = 0.30


def despeckle(data: np.ndarray, size: int = 3, n_mad: float = 6.0) -> np.ndarray:
    """Replace isolated single-pixel outliers with their local neighborhood median, leaving smooth
    terrain and large bright/saturated features (e.g. a crater) untouched.

    :param data: Input raster.
    :param size: Neighborhood window size, pixels.
    :param n_mad: Outlier threshold, in scaled median-absolute-deviations of the local neighborhood.
    :returns: `data` with outlier pixels replaced by their neighborhood median.
    """
    # A pixel is flagged only when it deviates from its `size`x`size` neighborhood median by more than
    # `n_mad` scaled MADs of that same neighborhood -- this makes the threshold self-scaling to local
    # contrast, so a pixel next to an edge or large feature (where the neighborhood's own MAD is
    # already high) is far less likely to be flagged than an isolated pixel in otherwise-smooth terrain.
    # Validated against fetched Lunaserv WAC tiles (see docs/data-sources/lunaserv-wms.md): ~90% of
    # statistical outliers under this test are isolated single pixels with no adjacent outlier, and a
    # known saturated-crater blob in that data is untouched by design (its neighborhood MAD is not
    # small).
    pad = size // 2
    padded = np.pad(data, pad, mode="edge")
    neighborhood = np.lib.stride_tricks.sliding_window_view(padded, (size, size)).reshape(*data.shape, -1)
    med = np.median(neighborhood, axis=-1)
    mad = np.median(np.abs(neighborhood - med[..., None]), axis=-1) * 1.4826  # normal-consistent scale
    is_outlier = np.abs(data.astype(np.float64) - med) > n_mad * np.maximum(mad, 1.0)
    return np.where(is_outlier, med, data).astype(data.dtype)


def shade_ortho(
    ortho: np.ndarray, dem: np.ndarray, azimuth_deg: float, elevation_deg: float, cellsize_m: float
) -> np.ndarray:
    """Blend a Lambertian hillshade, lit from `(azimuth_deg, elevation_deg)` and computed from `dem`,
    onto `ortho`.

    :param ortho: `[0, 255]`-range ortho image.
    :param dem: Elevation, meters.
    :param azimuth_deg: Sun azimuth, degrees.
    :param elevation_deg: Sun elevation, degrees.
    :param cellsize_m: DEM pixel size, meters.
    :returns: The shaded `uint8` image.
    """
    # `sat_sim` applies no illumination model of its own; it geometrically reprojects whatever's
    # already in the ortho (see docs/external-tools.md's ASP `sat_sim` section), so any relief in the
    # synthetic render has to come from here. A direct multiply, not `0.5 + 0.5 * hillshade` (an
    # earlier version's artificial floor that halved the shading term's usable dynamic range and
    # washed out the render relative to WAC imagery): terrain facing away from the sun should render
    # dark, not floored at ~50% gray. This is still just local per-facet shading, not cast-shadow
    # occlusion from other terrain, which remains out of scope (same section).
    light = LightSource(azdeg=azimuth_deg, altdeg=elevation_deg)
    hillshade = light.hillshade(dem.astype(np.float64), dx=cellsize_m, dy=cellsize_m)
    ortho_norm = ortho.astype(np.float64) / 255.0
    blended = ortho_norm * hillshade
    return np.clip(blended * 255.0, 0, 255).astype(np.uint8)


def _local_enu_basis(center_lon_deg: float, center_lat_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The (East, North, Up) unit vectors, in MOON_ME, of the local tangent plane at a point.

    :param center_lon_deg: Tangent point longitude, degrees.
    :param center_lat_deg: Tangent point latitude, degrees.
    :returns: `(east, north, up)`, each a MOON_ME unit vector.
    """
    # Used by `_moon_me_direction_from_local_enu` to rotate a single local-frame direction (the sun's
    # azimuth/elevation) into MOON_ME. `_terrain_photometric_angles` itself works entirely in MOON_ME
    # (positions -- DEM points, camera -- are never embedded into a local tangent plane, which is a
    # lossy approximation); a free direction has no such embedding step, so rotating one between
    # orthonormal frames stays exact, and this is the one remaining boundary that still needs it.
    lon0, lat0 = math.radians(center_lon_deg), math.radians(center_lat_deg)
    east = np.array([-math.sin(lon0), math.cos(lon0), 0.0])
    north = np.array([-math.sin(lat0) * math.cos(lon0), -math.sin(lat0) * math.sin(lon0), math.cos(lat0)])
    up = np.array([math.cos(lat0) * math.cos(lon0), math.cos(lat0) * math.sin(lon0), math.sin(lat0)])
    return east, north, up


def _moon_me_direction_from_local_enu(local_enu_vector, center_lon_deg: float, center_lat_deg: float) -> np.ndarray:
    """Rotate a local (East, North, Up) direction into MOON_ME.

    :param local_enu_vector: `(e, n, u)` components (e.g. the sun's azimuth/elevation, converted to a
        local ENU unit vector by `real_geometry_photometric_angles`).
    :param center_lon_deg: Tangent point longitude, degrees.
    :param center_lat_deg: Tangent point latitude, degrees.
    :returns: The direction as a MOON_ME vector.
    """
    # Via `_local_enu_basis`'s orthonormal (East, North, Up) triad -- the linear combination
    # `east*e + north*n + up*u`. Lossless either direction, since (east, north, up) are orthonormal.
    east, north, up = _local_enu_basis(center_lon_deg, center_lat_deg)
    e, n, u = np.asarray(local_enu_vector, dtype=np.float64)
    return e * east + n * north + u * up


def _terrain_photometric_angles(
    dem: np.ndarray,
    bbox: tuple,
    center_lon_deg: float,
    center_lat_deg: float,
    camera_center_moon_me_m,
    sun_direction_moon_me,
    cellsize_m: float,
    radius_m: float,
    along_track_direction_moon_me=None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-pixel incidence/emission/phase angles for `dem`'s own orthographic ortho, using the finite
    camera position rather than an idealized infinitely-distant nadir viewer.

    :param dem: Elevation, meters, on the local orthographic grid described by `bbox`.
    :param bbox: `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param center_lon_deg: Local Orthographic CRS tangent point longitude, degrees.
    :param center_lat_deg: Local Orthographic CRS tangent point latitude, degrees.
    :param camera_center_moon_me_m: Camera position, MOON_ME meters.
    :param sun_direction_moon_me: Sun direction, MOON_ME unit vector.
    :param cellsize_m: DEM pixel size, meters.
    :param radius_m: Sphere radius, meters.
    :param along_track_direction_moon_me: The camera's along-track attitude axis, MOON_ME unit vector,
        if the along-track correction should be applied; `None` to skip it.
    :returns: `(incidence_deg, emission_deg, phase_deg)`, each the same shape as `dem` -- raw geometry,
        not `LightSource.hillshade`'s own scene-relative contrast-stretched intensity (see its
        `shade_normals`), which is what ISIS `photomet` needs.
    """
    # Uses the finite camera position (`camera_center_moon_me_m`) rather than an idealized
    # infinitely-distant nadir viewer, so emission and phase vary per pixel from parallax (each pixel's
    # own vector to the spacecraft), not just from local terrain slope.
    #
    # Fully MOON_ME-native: no local tangent-plane position embedding anywhere in this function.
    # `ground`, each DEM pixel's true 3D position, comes from `rasterio.warp.transform` converting
    # `dem`'s local orthographic `(x, y)` plus its own elevation directly into `moon_geocentric_crs`'s
    # MOON_ME X/Y/Z. `camera_center_moon_me_m`/`along_track_direction_moon_me` are used directly, with
    # no rotation into any local frame. `sun_direction_moon_me` is the one input converted from a local
    # frame (the sun's azimuth/elevation, by `real_geometry_photometric_angles`'s caller, via
    # `_moon_me_direction_from_local_enu`) -- rotating a free direction between orthonormal frames is
    # exact and lossless, unlike embedding a position into a tangent plane, so this conversion carries
    # no approximation error; see that function's own docstring.
    #
    # The surface normal at each pixel comes from the same `np.gradient`-based convention
    # `LightSource.hillshade` uses internally, over MOON_ME coordinates, so this stays geometrically
    # consistent with `shade_ortho`'s Lambertian shading (which only needs incidence). The Sun, unlike
    # the camera, is treated as an effectively parallel-ray, scene-wide direction (lunar distance makes
    # this negligible -- see `illumination.sun_azimuth_elevation_deg`'s docstring), so
    # `sun_direction_moon_me` is a single vector rather than a per-pixel one.
    #
    # `along_track_direction_moon_me`, if given, projects the raw per-pixel view direction onto the
    # plane perpendicular to it before computing emission/phase -- a correction for this project's
    # single-frozen-camera-pose approximation of a multi-second pushframe scan: `camera_center_moon_me_m`
    # is one fixed position (matched to the crop's own center-frame time), but the spacecraft was
    # measurably elsewhere by the time any other line was actually captured, so the raw along-track
    # component of `view_dir` reflects the wrong position away from center. A scanning
    # pushframe/pushbroom sensor observes each line close to nadir in its own along-track direction at
    # the instant it's captured, so discarding the along-track component of the raw view direction and
    # keeping only the cross-track component approximates that per-line near-nadir geometry without
    # needing this project's per-line timing machinery that exists elsewhere (`isis_wac`'s per-line time
    # reconstruction). `camera_along_track_direction_moon_me` -- the sensor's own along-track axis,
    # derived from the camera's re-aimed attitude, not the spacecraft's raw orbital velocity direction --
    # is substantially more accurate against `campt` ground truth than either the spacecraft's raw
    # orbital velocity direction or the camera's cross-track axis.
    #
    # Independently validated against ISIS `campt` ground truth
    # (`tests/test_lunaserv_campt_validation.py`, `heavy`-marked) for the ellipsoid limit (`dem` all
    # zero), and against ASP `sfs`'s own ray-DEM intersection
    # (`tests/test_sfs_validation_lambertian_incidence.py`, `heavy`-marked) for the full DEM-aware case
    # across a candidate's entire coverage region -- both agree to within floating-point/interpolation
    # noise. ISIS's own `phocube` was investigated as a DEM-aware validation tool and shelved: its
    # `localincidence=true` mode returns degenerate values on a real scene (ISIS issue
    # DOI-USGS/ISIS3#3645), and `campt`'s own plain angle output stays ellipsoid-normal-based even with
    # a DEM shape model attached, so it has no DEM-aware mode of its own to fall back on either.
    #
    # A still-unexplained regression remains open, not addressed here: the normal-tilt correction made
    # the brightness-matched diff against a WAC crop measurably worse, despite the geometry itself being
    # independently confirmed correct above. Leading hypothesis, not verified: the base ortho texture is
    # already photometrically normalized in a way this project's own re-shading was never validated
    # against. See `docs/proposed-tasks/open-items.md`.
    height, width = dem.shape
    minx, miny, maxx, maxy = bbox
    x_centers = minx + (np.arange(width) + 0.5) * (maxx - minx) / width
    y_centers = maxy - (np.arange(height) + 0.5) * (maxy - miny) / height  # row 0 = north/top, matches `dy` below
    x_grid, y_grid = np.meshgrid(x_centers, y_centers)

    dy = -cellsize_m
    dem64 = dem.astype(np.float64)
    # Each DEM pixel's 3D MOON_ME position, via one vectorized `rasterio.warp.transform` call from
    # `dem`'s local orthographic (x, y) plus its elevation into `moon_geocentric_crs`'s MOON_ME X/Y/Z.
    ground_x, ground_y, ground_z = transform(
        local_orthographic_crs(center_lon_deg, center_lat_deg, radius_m),
        moon_geocentric_crs(radius_m),
        x_grid.ravel(),
        y_grid.ravel(),
        dem64.ravel(),
    )
    ground_shape = (height, width)
    ground = np.stack(
        [np.reshape(ground_x, ground_shape), np.reshape(ground_y, ground_shape), np.reshape(ground_z, ground_shape)],
        axis=-1,
    )

    # `normal`: the surface normal, via each of `ground`'s 3 MOON_ME coordinate channels' partial
    # derivatives (row/col index space -> physical space, `dy`/`cellsize_m`) and their cross product --
    # the general parametric-surface-normal construction.
    ground_d_row, ground_d_col = np.gradient(ground, dy, cellsize_m, axis=(0, 1))
    normal = np.cross(ground_d_col, ground_d_row)
    normal /= np.linalg.norm(normal, axis=-1, keepdims=True)

    camera_center_moon_me_m = np.asarray(camera_center_moon_me_m, dtype=np.float64)
    view_vec = camera_center_moon_me_m - ground
    view_dir = view_vec / np.linalg.norm(view_vec, axis=-1, keepdims=True)

    if along_track_direction_moon_me is not None:
        v_hat = np.asarray(along_track_direction_moon_me, dtype=np.float64)
        v_hat = v_hat / np.linalg.norm(v_hat)
        view_dir = view_dir - np.sum(view_dir * v_hat, axis=-1, keepdims=True) * v_hat
        view_dir /= np.linalg.norm(view_dir, axis=-1, keepdims=True)

    sun_dir = np.asarray(sun_direction_moon_me, dtype=np.float64)
    sun_dir = sun_dir / np.linalg.norm(sun_dir)

    incidence_deg = np.degrees(np.arccos(np.clip(normal @ sun_dir, -1.0, 1.0)))
    emission_deg = np.degrees(np.arccos(np.clip(np.sum(normal * view_dir, axis=-1), -1.0, 1.0)))
    phase_deg = np.degrees(np.arccos(np.clip(view_dir @ sun_dir, -1.0, 1.0)))
    return incidence_deg, emission_deg, phase_deg


def _write_backplane_cube(path: Path, values: np.ndarray) -> None:
    """Write `values` as a plain (non-georeferenced) single-band ISIS3 cube.

    :param path: Output `.cub` path.
    :param values: Single-band raster to write.
    """
    # `photomet`'s `ANGLESOURCE=BACKPLANE` mode only matches backplane files to the input cube by
    # sample/line dimensions, so no CRS/transform is needed here (and translating this project's local
    # Orthographic CRS into an ISIS `Mapping` group is unnecessary risk -- ISIS only recognizes a
    # specific set of projections). GDAL's own `ISIS3` driver (`rw+v`, per `gdalinfo --formats`)
    # writes/reads `.cub` files directly, so no `gdal2isis`/`isis2std` round-trip through a separate
    # conversion tool is needed either. Deliberately non-georeferenced, so `rasterio`'s own
    # `NotGeoreferencedWarning` is expected and suppressed here rather than left to print into a
    # notebook cell's output.
    path.unlink(missing_ok=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with rasterio.open(
            path, "w", driver="ISIS3", height=values.shape[0], width=values.shape[1], count=1, dtype="float32"
        ) as dst:
            dst.write(values.astype("float32"), 1)


def _hapke_calibration_cube_path(config: TrntestConfig) -> Path:
    """Resolve the ISIS lunar Hapke calibration cube's path, fetching the `lro` ISIS data package if
    not already present.

    :param config: Project config (`cache_root`).
    :returns: Path to the highest-numbered matching calibration cube.
    :raises FileNotFoundError: If no matching cube is found after fetching.
    """
    # Globs rather than hardcoding a specific version number (`.0001.cub` today), the same "don't
    # assume a specific version" discipline this project applies to other ISIS data area files.
    isis_wac.ensure_isisdata(config)
    isisdata = config.cache_root / "isisdata"
    candidates = sorted((isisdata / "lro" / "calibration").glob(_HAPKE_CALIBRATION_CUBE_GLOB))
    if not candidates:
        raise FileNotFoundError(
            f"No {_HAPKE_CALIBRATION_CUBE_GLOB} found under {isisdata / 'lro' / 'calibration'} -- "
            "ensure_isisdata should have fetched it as part of the 'lro' ISIS data package."
        )
    return candidates[-1]


def _sample_hapke_calibration(
    cube_path: Path, center_lon_deg: float, center_lat_deg: float, wavelength_nm: int
) -> dict[str, float]:
    """Read all 9 Hapke parameters for one wavelength band at one ground point from `cube_path`.

    :param cube_path: Any raster GDAL can open in the calibration cube's own Equirectangular CRS/band
        layout (the real cube, or a test fixture).
    :param center_lon_deg: Ground point longitude, degrees.
    :param center_lat_deg: Ground point latitude, degrees.
    :param wavelength_nm: One of the cube's 7 bands (`HAPKE_CALIBRATION_WAVELENGTHS_NM`).
    :returns: Dict of all 9 parameters, keyed by `_HAPKE_CALIBRATION_PARAM_ORDER`.
    :raises ValueError: If `wavelength_nm` isn't one of the cube's bands.
    """
    # Pure sampling logic, split out of `fetch_real_hapke_params` so it's unit-testable against a small
    # synthetic fixture, without needing `$ISISDATA` or network access -- the same reasoning
    # `_terrain_photometric_angles` being plain-Python (no ISIS subprocess) already follows.
    if wavelength_nm not in HAPKE_CALIBRATION_WAVELENGTHS_NM:
        raise ValueError(
            f"wavelength_nm={wavelength_nm} is not one of the cube's own bands {HAPKE_CALIBRATION_WAVELENGTHS_NM}"
        )
    band_offset = HAPKE_CALIBRATION_WAVELENGTHS_NM.index(wavelength_nm) * len(_HAPKE_CALIBRATION_PARAM_ORDER)
    lon_0_360 = center_lon_deg % 360.0  # cube's own convention, per its embedded georeferencing

    with rasterio.open(cube_path) as src:
        (x,), (y,) = transform(geographic_crs(MOON_RADIUS_M), src.crs, [lon_0_360], [center_lat_deg])
        row, col = src.index(x, y)
        return {
            name: float(src.read(band_offset + i + 1, window=((row, row + 1), (col, col + 1)))[0, 0])
            for i, name in enumerate(_HAPKE_CALIBRATION_PARAM_ORDER)
        }


def fetch_real_hapke_params(
    center_lon_deg: float,
    center_lat_deg: float,
    config: TrntestConfig,
    wavelength_nm: int = DEFAULT_HAPKE_CALIBRATION_WAVELENGTH_NM,
) -> dict[str, float]:
    """Spatially-resolved Hapke parameters (Sato et al. 2014's fit) sampled at one ground point from
    ISIS's own calibration cube -- the `real_hapke_params=True` alternative to
    `_HAPKE_PLACEHOLDER_PARAMS`'s illustrative constants (see `hapke_shade_ortho`).

    :param center_lon_deg: Ground point longitude, degrees.
    :param center_lat_deg: Ground point latitude, degrees.
    :param config: Project config, passed to `_hapke_calibration_cube_path`.
    :param wavelength_nm: One of the cube's 7 bands (321/360/415/566/604/643/689); the default matches
        `config.lunaserv_ortho_layer`'s own wavelength (643nm, see docs/data-sources/lunaserv-wms.md).
    :returns: All 9 parameters (ISIS's native `Wh`/`Hg1`/`Hg2`/`Bc0`/`hc`/`B0`/`Hh`/`Theta`/`phi`
        parameterization), keyed lowercase to match `_HAPKE_PLACEHOLDER_PARAMS`'s own keys.
    """
    # `hapke_shade_ortho` uses only the 6 the simpler shadow-hiding-only `HAPKEHEN` model this project
    # calls accepts (`wh`, `hg1`, `hg2`, `hh`, `b0`, `theta`); `bc0`/`hc`/`phi` describe the fuller
    # Hapke model's separate coherent-backscatter term, which this cube sets to `0`/`1`/`0` globally
    # for this WAC-derived product -- unused by it, not a modeling choice made here.
    #
    # A single value per image (this function's own footprint center), not per-pixel: within one
    # ~143km candidate footprint, `wh`/`b0`/`hg1` vary only a few percent of each parameter's full-Moon
    # range -- secondary next to the placeholder-vs-calibrated gap this exists to fix (e.g. `b0`: 0.025
    # placeholder vs. ~1.5-2.2 calibrated, a ~60x difference). `hg2`/`hh` vary somewhat more within one
    # footprint, but still a smaller effect. Per-pixel sampling (reprojecting the calibration cube onto
    # the same working grid `dem_gld100.reproject_astropedia_elevation_to_local_grid` builds the
    # DEM/ortho on) would be a further refinement, not implemented here -- see
    # `docs/proposed-tasks/open-items.md`.
    path = _hapke_calibration_cube_path(config)
    return _sample_hapke_calibration(path, center_lon_deg, center_lat_deg, wavelength_nm)


def _hapke_reflectance(
    phase_deg: np.ndarray,
    incidence_deg: np.ndarray,
    emission_deg: np.ndarray,
    hapkehen_params: dict,
) -> np.ndarray:
    """Run ISIS `photomet` (`PHTNAME=HAPKEHEN`) as a pure Hapke-model evaluator.

    :param phase_deg: Phase angle, degrees -- any shape.
    :param incidence_deg: Incidence angle, degrees -- same shape as `phase_deg`.
    :param emission_deg: Emission angle, degrees -- same shape as `phase_deg`.
    :param hapkehen_params: `HAPKEHEN` model parameters (`wh`, `hg1`, `hg2`, `hh`, `b0`, `theta`).
    :returns: H(angles), the Hapke reflectance factor, same shape as `phase_deg`.
    """
    # `ANGLESOURCE=BACKPLANE`/`NORMNAME=SHADE` give H(angles) with no dependence on any actual image
    # content -- the `from` cube fed to `photomet` is always zeroed, and `NORMNAME=SHADE` overwrites it
    # with the model's own output regardless. Factored out of `hapke_shade_ortho` so it can call this
    # twice (candidate geometry, and the fixed reference geometry `luna_wac_normalized_reflectance` is
    # itself normalized to -- see `REFERENCE_INCIDENCE_DEG`'s own module-level comment) without
    # duplicating the `photomet` subprocess-orchestration logic. `photomet` also requires a `FROM` cube
    # purely as a size/dtype template (a `BandBin` label group is enough to open the file at all -- not
    # documented in `photomet.xml` -- added via `editlab` since GDAL's `ISIS3` writer doesn't create
    # one from scratch).
    #
    # Uses a call-scoped `tempfile.TemporaryDirectory()` for its own scratch cubes, not
    # `config.output_dir` (see `docs/intermediate-product-discipline.md`'s access-mode discipline):
    # fixed-name scratch cubes under a shared directory let two workers computing different entries'
    # shading concurrently race on the same filenames and corrupt each other's writes. Nothing here
    # persists or resumes across calls -- pure single-call scratch, so a call-scoped temp dir
    # (auto-deleted on return, success or exception) is the right fix, not a unique-but-persistent path,
    # which would just trade a collision bug for an accumulation one.
    with tempfile.TemporaryDirectory() as tmp_dir:
        work_dir = Path(tmp_dir)
        from_cub, phase_cub = work_dir / "hapke_from.cub", work_dir / "hapke_phase.cub"
        incidence_cub, emission_cub = work_dir / "hapke_incidence.cub", work_dir / "hapke_emission.cub"
        out_cub = work_dir / "hapke_out.cub"

        _write_backplane_cube(from_cub, np.zeros_like(incidence_deg, dtype=np.float32))
        _write_backplane_cube(phase_cub, phase_deg)
        _write_backplane_cube(incidence_cub, incidence_deg)
        _write_backplane_cube(emission_cub, emission_deg)

        run_quiet(["editlab", f"from={from_cub}", "options=addg", "grpname=BandBin"])
        run_quiet(["editlab", f"from={from_cub}", "options=addkey", "grpname=BandBin", "keyword=Center", "value=1.0"])

        run_quiet(
            [
                "photomet",
                f"from={from_cub}",
                f"to={out_cub}",
                "anglesource=backplane",
                f"phase_angle_file={phase_cub}",
                f"incidence_angle_file={incidence_cub}",
                f"emission_angle_file={emission_cub}",
                "phtname=hapkehen",
                *(f"{name}={value}" for name, value in hapkehen_params.items()),
                "zerob0standard=true",
                "normname=shade",
                "albedo=1.0",
                "incref=0.0",
            ]
        )

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            with rasterio.open(out_cub) as src:
                return src.read(1).astype(np.float64)


def hapke_shade_ortho(
    ortho: np.ndarray,
    dem: np.ndarray,
    bbox: tuple,
    camera: Camera,
    azimuth_deg: float,
    elevation_deg: float,
    cellsize_m: float,
    config: TrntestConfig,
    along_track_correction: bool = DEFAULT_ALONG_TRACK_CORRECTION,
    real_hapke_params: bool = DEFAULT_REAL_HAPKE_PARAMS,
) -> np.ndarray:
    """The default ortho-shading mode (`DEFAULT_HAPKE_SHADING`): relight `ortho` using a Hapke
    bidirectional reflectance function (ISIS `photomet`, `PHTNAME=HAPKEHEN`) instead of `shade_ortho`'s
    fallback plain Lambertian blend.

    :param ortho: Physical reflectance (WAC_EMP's own PDS4 archive tile, no embedded display stretch --
        see `ortho_wac_emp.reproject_wac_emp_reflectance_to_local_grid`'s docstring), not Lunaserv's
        old WMS-served `uint8` DN.
    :param dem: Elevation, meters, on the local orthographic grid described by `bbox`.
    :param bbox: `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param camera: The camera this ortho/DEM pair was fetched for.
    :param azimuth_deg: Sun azimuth, degrees.
    :param elevation_deg: Sun elevation, degrees.
    :param cellsize_m: DEM pixel size, meters.
    :param config: Project config, passed to `fetch_real_hapke_params` when `real_hapke_params=True`.
    :param along_track_correction: Apply `_terrain_photometric_angles`'s along-track correction using
        `camera`'s own along-track attitude axis. On by default.
    :param real_hapke_params: Use `fetch_real_hapke_params()`'s ISIS-calibration-cube-sourced Hapke
        coefficients (the default) rather than `_HAPKE_PLACEHOLDER_PARAMS`'s illustrative constants
        (kept for comparison in `notebooks/real_hapke_params.ipynb`).
    :returns: Relit reflectance directly (float64, physical units), not a display-ready `uint8` image
        -- `despeckle_and_shade_ortho` applies the separate `stretch_reflectance_to_uint8` cosmetic
        step afterward.
    """
    # The angle rasters come from `_terrain_photometric_angles`, computed from `camera`'s own position
    # (this ortho has no ISIS camera model for `photomet` to derive angles from automatically).
    # `along_track_correction`, on by default, is substantially more accurate against `campt` ground
    # truth than the base per-pixel-camera-position approach alone.
    # `photomet`'s own subprocess-orchestration mechanics live in the shared `_hapke_reflectance`
    # helper -- see its own docstring. `_HAPKE_PLACEHOLDER_PARAMS` is illustrative, not
    # lunar-calibrated -- a feasibility prototype, not a validated photometric model; see
    # `fetch_real_hapke_params`'s own docstring for what its calibrated alternative does and doesn't
    # capture.
    #
    # Relights `ortho` by the ratio H(i,e,g)/H(reference), not a bare rescaled H(i,e,g): `ortho` isn't
    # raw albedo -- it's already a photometric composite, normalized to a fixed reference geometry
    # (`REFERENCE_INCIDENCE_DEG`'s own module-level comment explains the reference geometry and why
    # it's only an approximate, not exact, correction). Multiplying by a bare, arbitrarily-rescaled
    # H(i,e,g) double-counts the photometric function with no relationship to the geometry the texture
    # was actually produced at -- not a relighting operation, just an unprincipled per-pixel contrast
    # rescale. Dividing by H(reference) instead makes the multiplier a physically meaningful relighting
    # ratio (~1 for geometries close to the reference, deviating for geometries far from it), so no
    # separate display-range rescale is needed beyond simple `[0, 255]` clipping. An unusually bright
    # opposition-surge patch legitimately saturating toward white, rather than the whole frame being
    # dimmed to keep it in range, is treated as the physically correct behavior here, not an artifact
    # to avoid -- but that stance was never validated against real candidates and isn't a settled
    # design decision; whether/how often this saturation actually happens, and whether it's acceptable,
    # is an open question, see `docs/proposed-tasks/open-items.md`. Like `_terrain_photometric_angles`'s own
    # normal-tilt correction, this has no opt-out parameter, despite being confirmed to worsen the
    # brightness-matched diff against a WAC crop for the one candidate tested so far.
    #
    # `_terrain_photometric_angles`'s own 3D-geometry terrain embedding is unconditional, with no
    # parameter here to control it.
    reflectance, hapkehen_params = real_geometry_hapke_reflectance(
        dem, bbox, camera, azimuth_deg, elevation_deg, cellsize_m, config, along_track_correction, real_hapke_params
    )
    reference_reflectance = reference_hapke_reflectance(hapkehen_params)

    if reference_reflectance > 0 and np.isfinite(reference_reflectance):
        ratio = reflectance / reference_reflectance
    else:
        ratio = np.zeros_like(reflectance)

    return ortho.astype(np.float64) * ratio


def stretch_reflectance_to_uint8(
    reflectance: np.ndarray,
    lo: float = DISPLAY_STRETCH_REFLECTANCE_MIN,
    hi: float = DISPLAY_STRETCH_REFLECTANCE_MAX,
) -> np.ndarray:
    """Apply a fixed linear stretch to convert reflectance to a display-ready `uint8` image.

    :param reflectance: Physical reflectance (e.g. `hapke_shade_ortho`'s own output).
    :param lo: Reflectance mapped to 0.
    :param hi: Reflectance mapped to 255.
    :returns: The stretched `uint8` image, clipped to `[0, 255]`.
    """
    # The one, explicit place relit reflectance becomes a display-ready image -- a fixed range, not a
    # per-image adaptive/percentile stretch (see `DISPLAY_STRETCH_REFLECTANCE_MIN`/`_MAX`'s own
    # module-level comment for why), applied once at the very end of the pipeline rather than baked
    # into the input texture the way Lunaserv's old, uncorrected WMS display stretch effectively was
    # (see docs/data-sources/lunaserv-wms.md). Purely cosmetic -- has no bearing on this module's
    # photometric physics, all of which is already complete by the time this runs.
    #
    # `DISPLAY_STRETCH_REFLECTANCE_MAX = 0.30` was confirmed non-saturating for exactly one candidate,
    # not swept across others. Whether/how often input actually exceeds it (clipping to 255, with the
    # downstream effects that has -- e.g. biasing `sfs_validation.true_albedo_map`'s recovered albedo
    # at those pixels) is an open question, not a validated non-issue -- see `docs/proposed-tasks/open-items.md`.
    normalized = (reflectance.astype(np.float64) - lo) / (hi - lo)
    return np.clip(normalized * 255.0, 0, 255).astype(np.uint8)


def real_geometry_photometric_angles(
    dem: np.ndarray,
    bbox: tuple,
    camera: Camera,
    azimuth_deg: float,
    elevation_deg: float,
    cellsize_m: float,
    along_track_correction: bool = DEFAULT_ALONG_TRACK_CORRECTION,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """`(incidence_deg, emission_deg, phase_deg)` at `camera`'s own per-pixel geometry.

    :param dem: Elevation, meters, on the local orthographic grid described by `bbox`.
    :param bbox: `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param camera: The camera this ortho/DEM pair was fetched for.
    :param azimuth_deg: Sun azimuth relative to `camera`'s footprint center's local horizon, degrees.
    :param elevation_deg: Sun elevation relative to that same local horizon, degrees.
    :param cellsize_m: DEM pixel size, meters.
    :param along_track_correction: Apply the along-track correction using `camera`'s own along-track
        attitude axis (`camera.camera_along_track_direction_moon_me`). On by default.
    :returns: `(incidence_deg, emission_deg, phase_deg)`, each the same shape as `dem`.
    """
    # `_terrain_photometric_angles` given `camera`'s MOON_ME position (`camera.camera_center_moon_me_m`,
    # used directly -- no local-frame conversion, see `_terrain_photometric_angles`'s own docstring).
    # Factored out of `real_geometry_hapke_reflectance` for callers that only need the angles
    # themselves, not a Hapke evaluation -- e.g. `sfs_validation.py`'s Lambertian-mode incidence
    # cross-check, which compares this function's own `incidence_deg` directly against `sfs`'s
    # independently ray-traced one.
    #
    # `azimuth_deg`/`elevation_deg` are the one place this function still does a local-frame-to-MOON_ME
    # conversion (`_moon_me_direction_from_local_enu`), since it's the human-readable convention every
    # caller/test/notebook uses and `shade_ortho`'s Lambertian fallback needs az/el regardless for
    # matplotlib's `LightSource` API. This conversion is exact and lossless (a free direction rotated
    # between orthonormal frames, not a position embedding) -- see `_terrain_photometric_angles`'s own
    # docstring.
    center = camera.footprint_lonlat_deg["center"]
    assert center is not None, "camera's nadir footprint center must be a real ground point"
    center_lon_deg, center_lat_deg = center
    az_rad, el_rad = math.radians(90.0 - azimuth_deg), math.radians(elevation_deg)
    sun_local_enu = np.array(
        [math.cos(az_rad) * math.cos(el_rad), math.sin(az_rad) * math.cos(el_rad), math.sin(el_rad)]
    )
    sun_direction_moon_me = _moon_me_direction_from_local_enu(sun_local_enu, center_lon_deg, center_lat_deg)
    along_track_direction_moon_me = camera.camera_along_track_direction_moon_me if along_track_correction else None
    return _terrain_photometric_angles(
        dem,
        bbox,
        center_lon_deg,
        center_lat_deg,
        camera.camera_center_moon_me_m,
        sun_direction_moon_me,
        cellsize_m,
        MOON_RADIUS_M,
        along_track_direction_moon_me,
    )


def real_geometry_hapke_reflectance(
    dem: np.ndarray,
    bbox: tuple,
    camera: Camera,
    azimuth_deg: float,
    elevation_deg: float,
    cellsize_m: float,
    config: TrntestConfig,
    along_track_correction: bool = DEFAULT_ALONG_TRACK_CORRECTION,
    real_hapke_params: bool = DEFAULT_REAL_HAPKE_PARAMS,
) -> tuple[np.ndarray, dict]:
    """H(i,e,g) at `camera`'s own per-pixel geometry.

    :param dem: Elevation, meters, on the local orthographic grid described by `bbox`.
    :param bbox: `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param camera: The camera this ortho/DEM pair was fetched for.
    :param azimuth_deg: Sun azimuth, degrees.
    :param elevation_deg: Sun elevation, degrees.
    :param cellsize_m: DEM pixel size, meters.
    :param config: Project config, passed to `fetch_real_hapke_params` when `real_hapke_params=True`.
    :param along_track_correction: Passed through to `real_geometry_photometric_angles`.
    :param real_hapke_params: Use calibrated (vs. placeholder) Hapke parameters.
    :returns: `(reflectance, hapkehen_params)` -- the params dict too, since a caller building an
        albedo map also needs it for `sfs_validation.hapke_params_to_asp_model_coeffs`.
    """
    # The same computation `hapke_shade_ortho` itself makes to build its H(candidate)/H(reference)
    # relighting ratio, factored out so `sfs_validation.true_albedo_map` can divide this exact factor
    # back out of an already-shaded ortho to recover the geometry-independent albedo
    # `hapke_shade_ortho` started from, without duplicating this setup or risking it drifting out of
    # sync with `hapke_shade_ortho`'s own computation.
    incidence_deg, emission_deg, phase_deg = real_geometry_photometric_angles(
        dem, bbox, camera, azimuth_deg, elevation_deg, cellsize_m, along_track_correction
    )

    center = camera.footprint_lonlat_deg["center"]
    assert center is not None, "camera's nadir footprint center must be a real ground point"
    if real_hapke_params:
        hapkehen_source = fetch_real_hapke_params(*center, config)
    else:
        hapkehen_source = _HAPKE_PLACEHOLDER_PARAMS
    hapkehen_params = hapkehen_params_from_source(hapkehen_source)

    reflectance = _hapke_reflectance(phase_deg, incidence_deg, emission_deg, hapkehen_params)
    return reflectance, hapkehen_params


def hapkehen_params_from_source(hapkehen_source: dict) -> dict:
    """Slice a params dict down to the 6 keys the `HAPKEHEN` model this project calls actually accepts.

    :param hapkehen_source: `_HAPKE_PLACEHOLDER_PARAMS`, or `fetch_real_hapke_params`'s wider 9-key
        return.
    :returns: Dict with exactly `wh`, `hg1`, `hg2`, `hh`, `b0`, `theta`.
    """
    # Shared by `hapke_shade_ortho` and `sfs_validation.py`'s own use of the same calibration source, so
    # both stay in sync about which 6 of `fetch_real_hapke_params`'s 9 keys are the ones that matter
    # here.
    return {name: hapkehen_source[name] for name in _HAPKE_PLACEHOLDER_PARAMS}


def reference_hapke_reflectance(hapkehen_params: dict) -> float:
    """H(reference), the Hapke reflectance factor at the fixed reference geometry `ortho` is itself
    normalized to.

    :param hapkehen_params: `HAPKEHEN` model parameters (see `hapkehen_params_from_source`).
    :returns: The scalar H(reference) value.
    """
    # The shared denominator both `hapke_shade_ortho`'s relighting ratio and
    # `sfs_validation.true_albedo_map`'s "true albedo" proxy (`ortho / H(reference)`, undoing the same
    # normalization from the other direction) divide by. See `REFERENCE_INCIDENCE_DEG`'s own
    # module-level comment for what the reference geometry is. A small constant-valued array (no
    # per-pixel variation possible, same params/angles everywhere), not a full-size raster pass --
    # `_hapke_reflectance`'s own `photomet` call still needs an array shape to run against, hence the
    # `(3, 3)` filler.
    reference_shape = (3, 3)
    return _hapke_reflectance(
        np.full(reference_shape, REFERENCE_PHASE_DEG),
        np.full(reference_shape, REFERENCE_INCIDENCE_DEG),
        np.full(reference_shape, REFERENCE_EMISSION_DEG),
        hapkehen_params,
    )[0, 0]


def despeckle_and_shade_ortho(
    ortho_path,
    dem_path,
    camera: Camera,
    output_path,
    config: TrntestConfig,
    bbox: tuple,
    hapke: bool = DEFAULT_HAPKE_SHADING,
    along_track_correction: bool = DEFAULT_ALONG_TRACK_CORRECTION,
    real_hapke_params: bool = DEFAULT_REAL_HAPKE_PARAMS,
    ortho_source: str = "wac_emp_pds",
) -> None:
    """Despeckle the fetched ortho and blend in a sun-lit hillshade computed from the (already
    hole-filled) DEM, writing the result to `output_path`.

    :param ortho_path: Fetched raw ortho GeoTIFF path.
    :param dem_path: Hole-filled DEM GeoTIFF path.
    :param camera: The camera this ortho/DEM pair was fetched for.
    :param output_path: Where to write the shaded `uint8` ortho -- the single ortho used by both
        `sat_sim` and every display panel (see `dem_ortho.fetch_dem_and_ortho`).
    :param config: Project config, passed through to `hapke_shade_ortho`.
    :param bbox: `(minx, miny, maxx, maxy)`, meters, local Orthographic CRS.
    :param hapke: Use `hapke_shade_ortho`'s ISIS-`photomet`-backed Hapke shading (the default);
        `hapke=False` falls back to the plain Lambertian `shade_ortho` blend.
    :param along_track_correction: Passed straight through to `hapke_shade_ortho` (a no-op when
        `hapke=False`).
    :param real_hapke_params: Passed straight through to `hapke_shade_ortho` (a no-op when
        `hapke=False`).
    :param ortho_source: Which ortho/texture source `ortho_path` came from
        (`dem_ortho.ORTHO_SOURCES`; the `"wac_emp_pds"` default here mirrors
        `dem_ortho.DEFAULT_ORTHO_SOURCE`, duplicated rather than imported to avoid a dependency in the
        wrong direction -- every real caller passes this explicitly) -- affects how the Lambertian
        fallback (`hapke=False`) normalizes the input.
    """
    # `hapke=True`'s branch applies `stretch_reflectance_to_uint8` explicitly, right here, to
    # `hapke_shade_ortho`'s relit-reflectance output -- the one place that cosmetic display step
    # happens.
    #
    # `hapke=False`'s branch also needs `stretch_reflectance_to_uint8` applied before the ortho reaches
    # `shade_ortho` when `ortho_source="wac_emp_pds"`: `shade_ortho` assumes its `ortho` input is
    # already `[0, 255]` DN (see its own docstring for why it's kept that way rather than generalized),
    # but `cleaned` is physical reflectance (~0.05-0.3) when the source is WAC_EMP -- `shade_ortho`'s
    # own internal `/255.0` then `*255.0` round-trip cancels for values this small, truncating the
    # whole result to an all-zero, fully black image under `.astype(np.uint8)`. Applying the display
    # stretch first, turning the array back into DN-like `[0, 255]` before handing it to `shade_ortho`,
    # fixes this without touching `shade_ortho` itself. `ortho_source="lunaserv_wms"` skips this (its
    # `cleaned` is already DN, `shade_ortho`'s own native convention).
    with rasterio.open(ortho_path) as src:
        ortho = src.read(1)
        profile = src.profile
    with rasterio.open(dem_path) as src:
        dem = src.read(1)

    cleaned = despeckle(ortho)

    center = camera.footprint_lonlat_deg["center"]
    assert center is not None, "camera's nadir footprint center must be a real ground point"
    center_lon, center_lat = center
    azimuth_deg, elevation_deg = illumination.sun_azimuth_elevation_deg(center_lon, center_lat, camera.et)
    if hapke:
        relit_reflectance = hapke_shade_ortho(
            cleaned,
            dem,
            bbox,
            camera,
            azimuth_deg,
            elevation_deg,
            config.dem_target_gsd_m,
            config,
            along_track_correction=along_track_correction,
            real_hapke_params=real_hapke_params,
        )
        shaded = stretch_reflectance_to_uint8(relit_reflectance)
    else:
        lambertian_input = stretch_reflectance_to_uint8(cleaned) if ortho_source == "wac_emp_pds" else cleaned
        shaded = shade_ortho(lambertian_input, dem, azimuth_deg, elevation_deg, config.dem_target_gsd_m)

    profile.update(count=1, dtype="uint8")
    with atomic_publish(Path(output_path)) as tmp:
        with rasterio.open(tmp, "w", **profile) as dst:
            dst.write(shaded, 1)
