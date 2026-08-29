"""Bridges `pose_alignment`'s 2D map-space tie points into real ISIS control points, ready for a
`jigsaw` bundle adjustment -- the prerequisite step for the projection-aware (3D camera pose)
alignment this project's 2D homography spike (`pose_alignment.py`) was deliberately left at, pending
this next step (see `docs/plan.md`'s open items, "camera-pose alignment").

`pose_alignment.match_features`/`match_features_lightglue` produce matched *pixel* positions in two
already map-projected rasters (the `cam2map`-warped WAC crop and the basemap), converted to real map
coordinates via `pose_alignment.pixel_points_to_map`. A `jigsaw` control point instead needs, per tie
point: the real image-space pixel it was actually observed at (in the *original*, pre-`cam2map` WAC
cube -- the one `jigsaw` will actually adjust), and a trusted 3D ground location. `resolve_control_points`
does that conversion.

**Ground-to-image now goes through `wac_camera_model`, not `campt`, for a real, confirmed reason**:
`isis_wac.ground_to_image_pixels_batch` (a real `campt` ground-to-image query) has a scattered ~38%
failure rate specifically for WAC's Pushframe sensor (`PushFrameCameraGroundMap::GetLocalNormal`
landing outside the correct framelet, a known upstream ISIS bug, DOI-USGS/ISIS3#4256, not an
edge-of-crop artifact -- see `docs/external-tools.md`'s campt failure-rate section) -- the same underlying bug class
that made `jigsaw` itself unusable for this camera (see `docs/wac-jigsaw-investigation.md`).
`resolve_control_points` uses `wac_camera_model.find_framelet_and_project` instead (matching
`tie_points.resolve_crop_pixels`'s own precedent), which sidesteps the bug with a real 2D containment
check rather than ISIS's own heuristic search.

Doing so needs a real 3D ground point (not just `(lon, lat)`) for the WAC-side matched pixel too, so
`resolve_control_points` now samples elevation for it via `isis_wac.sample_lunar_dem_radii_batch` --
the *same* real DEM `isis_wac.run_spiceinit` attaches to every real-WAC cube by default now (was
`shape=ellipsoid` -- confirmed live to be the actual root cause of a real, user-observed parallax-like
effect at crater edges in the blink overlay that originally motivated this 3D-fit investigation; see
`docs/plan.md`'s dated entry). The function's own *return value*, `ground_lonlat`, is still `(lon,
lat)` only, no elevation -- that side comes straight from the basemap's own map-pixel georeferencing,
which has no elevation attached to sample from at this point. A caller building a 3D ground point from
`ground_lonlat` must sample elevation from the *same* shape model `ground_to_image_model` resolved
through, exactly as `resolve_control_points` itself now does for the WAC side -- feeding
elevation-aware ground truth against a camera model that's still ellipsoid-only (or vice versa)
conflates real camera-pose error with the ellipsoid-vs-real-terrain gap, worst exactly at high-relief
features like crater rims."""

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
    """Converts `(x, y)` real map coordinates (e.g. `pose_alignment.pixel_points_to_map`'s output) in
    `crs` (this pipeline's shared local Orthographic CRS -- every camera's own is constructed the
    same way, see `lunaserv.DemOrthoResult`'s docstring) to `(lon_deg, lat_deg)` arrays in this
    project's own 0-360 Positive-East, ellipsoid-radius convention -- via `lunaserv.geographic_crs`,
    the one shared source of truth for this CRS string (also used by `lunaserv.py`/`craters.py`/
    `tie_points.py`), not an independently-built copy. `rasterio.warp.transform` (the
    point-wise sibling of `transform_bounds`, which only handles a bbox) returns longitude in the
    standard -180..180 convention regardless of the destination CRS's own definition (confirmed
    elsewhere in this project, see `craters.py`'s own note) -- normalized here via `% 360.0` to match
    `isis_wac.ground_to_image_pixel`'s own `PositiveEast360Longitude` convention."""
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
    """Converts matched map-space tie points (same-length `wac_points_map`/`basemap_points_map`
    arrays, both already in real map coordinates via `pose_alignment.pixel_points_to_map`, sharing
    one CRS) into real ISIS control points ready for a `jigsaw` bundle adjustment.

    Returns `(observed_pixels, ground_lonlat)`, same-length paired arrays:

    - `observed_pixels` (`sample`, `line`, ISIS's own 1-based pixel-center convention -- *not*
      adjusted to this project's display convention the way `tie_points.resolve_crop_pixels` does,
      since this feeds an ISIS-native control network, not a plot) is the real pixel in the
      *original*, pre-`cam2map` WAC crop cube that shows each matched feature. Recovered by
      converting the matched WAC map-pixel to its own implied ground point -- a deterministic
      un-warp of `cam2map`'s own resampling, using *only* the WAC crop's own map projection, not the
      basemap, plus a real DEM elevation sample (`isis_wac.sample_lunar_dem_radii_batch`) -- then
      projecting that real 3D point through `wac_camera_model.find_framelet_and_project` (not
      `isis_wac.ground_to_image_pixels_batch`/real `campt` -- see this module's own docstring for
      why). This does not depend on trusting the current camera pose at all: it's a pure function of
      the WAC map-pixel and *whatever* camera pose produced it, right or wrong, and would give the
      same answer either way.
    - `ground_lonlat` is the trusted ground truth for the same matched feature, taken directly from
      the matched *basemap* map-pixel's own georeferencing -- `(lon, lat)` only, no elevation (see
      this module's own docstring: a caller building a 3D ground point from this must sample
      elevation consistently with whatever shape model `ground_to_image_model` resolved through).

    Tie points whose implied ground point doesn't actually project into any real framelet of the
    original crop (`find_framelet_and_project` returns `None` -- a real 2D containment check, so this
    now means the point is genuinely outside the crop's real coverage, not a spurious solve failure
    the way a `campt` "no surface intersection" error could be) are dropped with a printed warning --
    unlike `tie_points.resolve_crop_pixels`, which raises on any unresolved point now that its
    candidate points are placed inside the shared FOV's own local-meters inscribed box (so a failure
    there means something is fundamentally wrong). Here, a genuine edge-of-crop resampling miss on a
    handful of a many-point matched set is still a real, expected case -- so this raises only if
    *none* resolve."""
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
# direct introspection of the real protobuf schema bundled with this project's conda `isis` install
# (`plio.io.ControlNetFileV0002_pb2`), not guessed from docs. `Fixed`: every control point this
# module builds is trusted, non-adjustable ground truth from the basemap (see this module's own
# docstring) -- never `Free`/`Constrained`, which are for points jigsaw is itself allowed to move.
# `RegisteredPixel`: found via automated feature matching + a real `campt` resolve, not hand-digitized
# (`Manual`) or a raw unverified candidate (`Candidate`).
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
    """Writes a real ISIS control network (`.net`) file from `resolve_control_points`'s output --
    every point `Fixed` (trusted ground truth, not adjustable), a single `RegisteredPixel` measure
    each, tying `observed_pixels`' image location in `cub_path` (the original, pre-`cam2map` WAC crop
    cube -- the same cube `jigsaw` will adjust) to `ground_lonlat`'s trusted 3D position (converted to
    body-fixed rectangular km, ISIS's own control-network convention, via
    `tie_points.lonlat_to_ground_km` -- ellipsoid-only, matching this module's own ground-truth
    convention throughout, see its docstring).

    The actual binary write happens in a separate subprocess, `scripts/isis_write_control_network.py`,
    run under the ISIS conda environment's own Python (`$ISISROOT/bin/python`) rather than this
    project's venv -- see that script's own docstring for why (`plio`, the library that understands
    ISIS's control-network protobuf format, ships with the conda `isis` install but is deliberately
    not a `pyproject.toml` dependency of this project). This function only prepares the CSV that
    subprocess reads: converts `observed_pixels` from `isis_wac.ground_to_image_pixel`'s ISIS-native
    1-based pixel-center convention to the 0-based convention `plio`'s own writer expects (it adds
    `(0.5, 0.5)` itself -- confirmed via direct source inspection, not assumed; feeding it
    already-1-based pixels would double-shift every measure by half a pixel)."""
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
