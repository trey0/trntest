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

**Ellipsoid-only, deliberately, for now**: this pipeline's entire existing ground<->image geometry
(`isis_wac.run_spiceinit`'s `shape=ellipsoid`, and `run_cam2map_for_crop`'s warp, which inherits that
same shape model) treats the Moon as a smooth reference ellipsoid, never real per-pixel DEM
elevation -- confirmed by `isis_wac.ground_to_image_pixel`/`ground_point_at_pixel` only taking/
returning `(lon, lat)`, no elevation. Control points built here follow that same convention
(ellipsoid-only ground truth, no DEM sampling), for internal consistency with the camera model
`jigsaw` will actually reproject through -- feeding it elevation-aware ground truth while the camera
model itself stays ellipsoid-only would conflate real camera-pose error with the ellipsoid-vs-real-
terrain gap (worst exactly at high-relief features like crater rims), which is a real, live concern
here, not a hypothetical one (a real, user-observed parallax-like effect at crater edges in the blink
overlay motivated moving to a 3D fit in the first place). A DEM-aware shape model (`spiceinit
shape=user model=<dem>`) is a deliberate, real follow-up, not implemented here -- see `docs/plan.md`."""

import csv
import os
import subprocess
from pathlib import Path

import numpy as np
import rasterio.warp

from trntest import isis_wac, lunaserv
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
      basemap -- then querying `isis_wac.ground_to_image_pixels_batch` (one real batched `campt`
      call for every point at once, not one subprocess per point -- confirmed live to dominate this
      function's runtime otherwise, e.g. ~230s of subprocess overhead alone for 767 real matches)
      against the original crop cube (`ground_to_image_model`, from
      `isis_wac.resolve_ground_to_image_model`) for that ground point's real image location. This
      does not depend on trusting the current camera pose at all:
      it's a pure function of the WAC map-pixel and *whatever* camera model produced it, right or
      wrong, and would give the same answer either way.
    - `ground_lonlat` is the trusted ground truth for the same matched feature, taken directly from
      the matched *basemap* map-pixel's own georeferencing (ellipsoid-only, no DEM elevation -- see
      this module's own docstring for why that has to match `observed_pixels`'s own ellipsoid-only
      camera model).

    Tie points whose implied ground point doesn't actually project into the original crop (can
    happen right at the crop's own edge, e.g. if `PIXRES=map` resampling extended slightly past the
    camera's real coverage) are dropped with a printed warning -- unlike `tie_points.
    resolve_crop_pixels`, which raises on any unresolved point now that its candidate points are
    placed inside the shared FOV's own local-meters inscribed box (so a failure there means
    something is fundamentally wrong). Here, an edge-of-crop resampling miss on a handful of a
    many-point matched set is a real, expected case, not a sign anything is broken -- so this raises
    only if *none* resolve."""
    wac_lons, wac_lats = map_points_to_lonlat(wac_points_map, map_crs, config)
    basemap_lons, basemap_lats = map_points_to_lonlat(basemap_points_map, map_crs, config)

    wac_lonlat = np.stack([wac_lons, wac_lats], axis=1)
    pixels = isis_wac.ground_to_image_pixels_batch(ground_to_image_model, wac_lonlat)

    observed_pixels = []
    ground_lonlat = []
    n_dropped = 0
    for pixel, basemap_lon, basemap_lat in zip(pixels, basemap_lons, basemap_lats, strict=True):
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
            "implied ground point doesn't project into the original crop"
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
