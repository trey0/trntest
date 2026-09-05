"""Bridges `pose_alignment`'s 2D map-space tie points into ISIS control points for a `jigsaw` bundle
adjustment -- the prerequisite step for the projection-aware (3D camera pose) alignment
`pose_alignment.py`'s 2D homography spike was deliberately left short of (see
`docs/pose-alignment.md`).

`resolve_control_points` converts `pose_alignment.match_features`/`match_features_lightglue`'s
matched map-pixel positions into what `jigsaw` needs per tie point: the pixel it was actually
observed at in the original, pre-`cam2map` WAC cube, and a trusted 3D ground location.
`write_control_network` writes the result to ISIS's own `.net` control-network format.
"""
# Ground-to-image goes through `wac_camera_model.find_framelet_and_project`, not `campt`:
# `isis_wac.ground_to_image_pixels_batch` has a scattered ~38% failure rate for WAC's Pushframe
# sensor (`PushFrameCameraGroundMap::GetLocalNormal` landing outside the correct framelet, a known
# upstream ISIS bug, DOI-USGS/ISIS3#4256, not an edge-of-crop artifact -- see
# docs/external-tools.md's campt failure-rate section), the same underlying bug class that made
# `jigsaw` itself unusable for this camera (see docs/pose-alignment.md). This matches
# `tie_points.resolve_crop_pixels`'s own precedent.
#
# This needs a 3D ground point (not just `(lon, lat)`) for the WAC-side matched pixel, so
# `resolve_control_points` samples elevation for it via `isis_wac.sample_lunar_dem_radii_batch` -- the
# same DEM `isis_wac.run_spiceinit` attaches to every WAC cube by default (was `shape=ellipsoid`; this
# DEM/ellipsoid mismatch was the root cause of a parallax-like effect at crater edges in the blink
# overlay that originally motivated this 3D-fit work -- see `docs/pose-alignment.md`). The
# function's own return value, `ground_lonlat`, is still `(lon, lat)` only, no elevation -- that side
# comes straight from the basemap's own map-pixel georeferencing, which has none to sample. A caller
# building a 3D ground point from `ground_lonlat` must sample elevation from the *same* shape model
# `ground_to_image_model` resolved through, exactly as `resolve_control_points` does for the WAC side
# -- mixing an elevation-aware ground truth against an ellipsoid-only camera model (or vice versa)
# conflates camera-pose error with the ellipsoid-vs-terrain gap, worst at high-relief features like
# crater rims.

import csv
import os
import subprocess
import warnings
from pathlib import Path

import numpy as np
import rasterio
import rasterio.errors
import rasterio.warp

from trntest import isis_wac, lunaserv, wac_camera_model
from trntest.config import TrntestConfig, load_config
from trntest.tie_points import lonlat_to_ground_km

_WRITER_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "isis_write_control_network.py"


def map_points_to_lonlat(
    points_map: np.ndarray, crs, config: TrntestConfig | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Convert `(x, y)` map coordinates to `(lon_deg, lat_deg)` arrays.

    :param points_map: `(N, 2)` `(x, y)` map coordinates, e.g. `pose_alignment.pixel_points_to_map`'s
        output.
    :param crs: The CRS `points_map` is in (this pipeline's shared local Orthographic CRS -- every
        camera's own is constructed the same way, see `lunaserv.DemOrthoResult`'s docstring).
    :param config: Project config; `load_config()` if not given.
    :returns: `(lon_deg, lat_deg)` arrays, this project's own 0-360 Positive-East, ellipsoid-radius
        convention.
    """
    # Via `lunaserv.geographic_crs`, the one shared source of truth for this CRS string (also used by
    # `lunaserv.py`/`craters.py`/`tie_points.py`), not an independently-built copy.
    # `rasterio.warp.transform` (the point-wise sibling of `transform_bounds`, which only handles a
    # bbox) returns longitude in the standard -180..180 convention regardless of the destination
    # CRS's own definition (confirmed elsewhere in this project, see `craters.py`'s own note) --
    # normalized here via `% 360.0` to match `isis_wac.ground_to_image_pixel`'s own
    # `PositiveEast360Longitude` convention.
    config = config or load_config()
    geo_crs = lunaserv.geographic_crs()
    lons, lats = rasterio.warp.transform(crs, geo_crs, points_map[:, 0], points_map[:, 1])
    return np.asarray(lons) % 360.0, np.asarray(lats)


def resolve_control_points(
    wac_points_map: np.ndarray,
    basemap_points_map: np.ndarray,
    map_crs,
    ground_to_image_model: isis_wac.GroundToImageModel,
    config: TrntestConfig | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Convert matched map-space tie points into ISIS control points ready for a `jigsaw` bundle
    adjustment.

    :param wac_points_map: `(N, 2)` matched WAC-side map coordinates (via
        `pose_alignment.pixel_points_to_map`), same CRS and length as `basemap_points_map`.
    :param basemap_points_map: `(N, 2)` matched basemap-side map coordinates, same CRS and length as
        `wac_points_map`.
    :param map_crs: The shared CRS both point arrays are in.
    :param ground_to_image_model: The original, pre-`cam2map` WAC crop cube's camera model.
    :param config: Project config; `load_config()` if not given.
    :returns: `(observed_pixels, ground_lonlat)`, same-length paired arrays (shorter than the input if
        some points were dropped, see below):

        - `observed_pixels`: `(sample, line)`, ISIS's own 1-based pixel-center convention (*not*
          adjusted to this project's display convention the way `tie_points.resolve_crop_pixels`
          does, since this feeds an ISIS-native control network, not a plot) -- the pixel in the
          original WAC crop cube that shows each matched feature. Recovered by converting the matched
          WAC map-pixel to its implied ground point (a deterministic un-warp of `cam2map`'s own
          resampling, using only the WAC crop's own map projection, plus a DEM elevation sample via
          `isis_wac.sample_lunar_dem_radii_batch`), then projecting that point through
          `wac_camera_model.find_framelet_and_project` (see the module comment for why not
          `isis_wac.ground_to_image_pixels_batch`). Doesn't depend on trusting the current camera
          pose: it's a pure function of the WAC map-pixel and whatever camera pose produced it.
        - `ground_lonlat`: the trusted ground truth for the same matched feature, taken directly from
          the matched basemap map-pixel's own georeferencing -- `(lon, lat)` only, no elevation (see
          the module comment: a caller building a 3D ground point from this must sample elevation
          consistently with whatever shape model `ground_to_image_model` resolved through).
    :raises RuntimeError: If no tie point's implied ground point projects into the original crop.
    """
    # A tie point whose implied ground point doesn't project into any framelet of the original crop
    # (`find_framelet_and_project` returns `None` -- a 2D containment check, so this means the point
    # is genuinely outside the crop's coverage, not a spurious solve failure the way a `campt` "no
    # surface intersection" error could be) is dropped with a printed warning -- unlike
    # `tie_points.resolve_crop_pixels`, which raises on any unresolved point now that its candidate
    # points are placed inside the shared FOV's own local-meters inscribed box (so a failure there
    # means something is fundamentally wrong). Here, an edge-of-crop resampling miss on a handful of a
    # many-point matched set is still an expected case, so this only raises if *none* resolve.
    config = config or load_config()
    wac_lons, wac_lats = map_points_to_lonlat(wac_points_map, map_crs, config)
    basemap_lons, basemap_lats = map_points_to_lonlat(basemap_points_map, map_crs, config)

    wac_lonlat = np.stack([wac_lons, wac_lats], axis=1)
    wac_radii_m = isis_wac.sample_lunar_dem_radii_batch(wac_lonlat, config)
    wac_ground_me_m = (
        np.array(
            [
                lonlat_to_ground_km(lon_deg, lat_deg, radius_m / 1000.0)
                for (lon_deg, lat_deg), radius_m in zip(wac_lonlat, wac_radii_m, strict=True)
            ]
        )
        * 1000.0
    )

    with warnings.catch_warnings():
        # NotGeoreferencedWarning is expected for an ISIS .cub at this pipeline stage (no
        # geotransform yet, not a bug) -- see plotting.read_raster_band's own docstring for this
        # same suppression.
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        with rasterio.open(ground_to_image_model.cub_path) as src:
            n_lines = src.height
    n_framelets = n_lines // wac_camera_model.FRAMELET_HEIGHT
    et0, et_per_line = wac_camera_model.calibrate_et_per_crop_line(ground_to_image_model.cub_path, n_lines)

    observed_pixels = []
    ground_lonlat = []
    n_dropped = 0
    for ground_me_m, basemap_lon, basemap_lat in zip(wac_ground_me_m, basemap_lons, basemap_lats, strict=True):
        pixel = wac_camera_model.find_framelet_and_project(ground_me_m, n_framelets, et0, et_per_line)
        if pixel is None:
            n_dropped += 1
            continue
        observed_pixels.append(pixel)
        ground_lonlat.append((basemap_lon, basemap_lat))

    if not observed_pixels:
        raise RuntimeError(
            "none of the matched tie points' implied ground points project into the original WAC "
            "crop cube -- something is fundamentally wrong, not just an edge-of-crop case"
        )
    if n_dropped:
        print(
            f"resolve_control_points: dropped {n_dropped}/{len(wac_points_map)} tie points whose "
            "implied ground point doesn't project into any real framelet of the original crop"
        )
    return np.array(observed_pixels, dtype="float64"), np.array(ground_lonlat, dtype="float64")


# ISIS `ControlPointFileEntryV0002.PointType`/`.Measure.MeasureType` enum values -- confirmed via
# direct introspection of the protobuf schema bundled with this project's conda `isis` install
# (`plio.io.ControlNetFileV0002_pb2`), not guessed from docs. `Fixed`: every control point this
# module builds is trusted, non-adjustable ground truth from the basemap (see the module comment
# above) -- never `Free`/`Constrained`, which are for points jigsaw is itself allowed to move.
# `RegisteredPixel`: found via automated feature matching + a `campt` resolve, not hand-digitized
# (`Manual`) or an unverified candidate (`Candidate`).
_POINT_TYPE_FIXED = 4
_MEASURE_TYPE_REGISTERED_PIXEL = 2

_CONTROL_NETWORK_CSV_COLUMNS = [
    "id",
    "pointType",
    "referenceIndex",
    "aprioriX",
    "aprioriY",
    "aprioriZ",
    "adjustedX",
    "adjustedY",
    "adjustedZ",
    "serialnumber",
    "measureType",
    "sample",
    "line",
]


def write_control_network(
    observed_pixels: np.ndarray,
    ground_lonlat: np.ndarray,
    cub_path: Path,
    out_path: Path,
    config: TrntestConfig | None = None,
) -> Path:
    """Write an ISIS control network (`.net`) file from `resolve_control_points`'s output.

    :param observed_pixels: `(N, 2)` `(sample, line)`, ISIS's own 1-based pixel-center convention.
    :param ground_lonlat: `(N, 2)` `(lon_deg, lat_deg)`, same length and point order as
        `observed_pixels`.
    :param cub_path: The original, pre-`cam2map` WAC crop cube -- the same cube `jigsaw` will adjust.
    :param out_path: Output `.net` path.
    :param config: Project config; `load_config()` if not given.
    :returns: `out_path`.
    """
    # Every point is `Fixed` (trusted ground truth, not adjustable) with a single `RegisteredPixel`
    # measure, tying `observed_pixels`' image location in `cub_path` to `ground_lonlat`'s 3D position
    # (converted to body-fixed rectangular km, ISIS's own control-network convention, via
    # `tie_points.lonlat_to_ground_km` -- ellipsoid-only, matching this module's ground-truth
    # convention throughout, see the module comment).
    #
    # The actual binary write happens in a separate subprocess,
    # `scripts/isis_write_control_network.py`, run under the ISIS conda environment's own Python
    # (`$ISISROOT/bin/python`) rather than this project's venv -- see that script's own docstring for
    # why (`plio`, the library that understands ISIS's control-network protobuf format, ships with the
    # conda `isis` install but is deliberately not a `pyproject.toml` dependency of this project).
    # This function only prepares the CSV that subprocess reads: converts `observed_pixels` from
    # ISIS's own 1-based pixel-center convention to the 0-based convention `plio`'s own writer expects
    # (it adds `(0.5, 0.5)` itself -- confirmed via direct source inspection, not assumed; feeding it
    # already-1-based pixels would double-shift every measure by half a pixel).
    config = config or load_config()

    out_path = Path(out_path)
    csv_path = out_path.with_suffix(".csv")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    serialnumber = isis_wac.cube_serial_number(cub_path)

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CONTROL_NETWORK_CSV_COLUMNS)
        writer.writeheader()
        points = zip(observed_pixels, ground_lonlat, strict=True)
        for i, ((sample, line), (lon_deg, lat_deg)) in enumerate(points):
            x_km, y_km, z_km = lonlat_to_ground_km(lon_deg, lat_deg)
            writer.writerow(
                {
                    "id": f"pt_{i:04d}",
                    "pointType": _POINT_TYPE_FIXED,
                    "referenceIndex": 0,
                    "aprioriX": x_km,
                    "aprioriY": y_km,
                    "aprioriZ": z_km,
                    "adjustedX": x_km,
                    "adjustedY": y_km,
                    "adjustedZ": z_km,
                    "serialnumber": serialnumber,
                    "measureType": _MEASURE_TYPE_REGISTERED_PIXEL,
                    "sample": sample - 0.5,
                    "line": line - 0.5,
                }
            )

    isis_python = Path(os.environ["ISISROOT"]) / "bin" / "python"
    result = subprocess.run(
        [
            str(isis_python),
            str(_WRITER_SCRIPT),
            "--csv",
            str(csv_path),
            "--out",
            str(out_path),
            "--target",
            "Moon",
            "--networkid",
            out_path.stem,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="")
        result.check_returncode()
    return out_path
