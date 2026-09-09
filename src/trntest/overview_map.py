"""Dataset-wide overview map: a ground-track-style plot of every entry in a `TrnTestDataSet` on a
global lunar backdrop. Wired into `TrnTestDataSet.write_index()` (pass `write_overview_map=False`
there to skip it) and linked from the nav bar's "Map" link (`report.write_index_html`).
"""

from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio

from trntest import cache, illumination, spice_kernels, tie_points
from trntest.config import TrntestConfig, load_config
from trntest.trn_dataset import TrnTestDataSet

GLOBAL_BACKDROP_LAYER = "luna_wac_global"  # see docs/data-sources/lunaserv-wms.md's "Layers of
# interest" -- real, if slightly noisy, whole-Moon coverage; acceptable at this map's opacity as a
# geographic backdrop, not for measurement.
GLOBAL_BACKDROP_WIDTH_PX = 1440  # 0.25 deg/px -- plenty for a partial-opacity full-globe backdrop,
GLOBAL_BACKDROP_HEIGHT_PX = 720  # small/fast to fetch (one-time, then cached like any other tile).
BACKDROP_ALPHA = 0.4  # raised from an initial 0.2, which read too faint in practice
SHADOW_GRAY_LEVEL = 0.8  # "~80% white" (light grey) where in shadow -- a grayscale value (0.8 ->
# 80% of full white), not an alpha; the day/night layer itself is fully opaque, it's the backdrop
# drawn on top of it that's at BACKDROP_ALPHA.


def dataset_midpoint_datetime(dataset: TrnTestDataSet) -> datetime:
    """The dataset's temporal midpoint -- halfway between its earliest and latest time (see
    `TrnTestDataSet.time_span_columns` for which manifest columns those are, per `entry_kind`) --
    for the overview map's single global illumination snapshot (see module docstring: one shared
    snapshot, not per-entry lighting)."""
    # format="ISO8601": manifest rows aren't all the same sub-second precision (some carry
    # fractional seconds, some don't) -- pandas' own single-inferred-format guess from the first
    # rows raises on any later row that doesn't match it exactly.
    start_col, stop_col = dataset.time_span_columns
    start = pd.to_datetime(dataset.images[start_col], format="ISO8601").min()
    stop = pd.to_datetime(dataset.images[stop_col], format="ISO8601").max()
    midpoint = start + (stop - start) / 2
    return midpoint.to_pydatetime()


def _day_night_mask(et: float, width: int, height: int) -> np.ndarray:
    """Grayscale day/night mask at `et`, sized `(height, width)` to match the backdrop image
    pixel-for-pixel.

    Computed directly from the sub-solar point (`illumination.sub_solar_lonlat_deg`, one SPICE call
    total) via spherical trig -- the standard solar-elevation law-of-cosines formula, `sin(elevation)
    = sin(sub_lat)*sin(lat) + cos(sub_lat)*cos(lat)*cos(lon - sub_lon)` -- rather than a separate
    per-point `illumination.sun_elevation_deg`/SPICE `ilumin` call per grid point. Assumes the Moon is
    a sphere (no ellipsoid/DEM shape model), fine at this map's whole-Moon display scale; cheap enough
    (pure `numpy`, no per-point SPICE call) to run at the backdrop's own full resolution instead of a
    separate coarse grid.

    :returns: Array shaped `(height, width)`, row 0 = lat +90 (north), matching `imshow`'s default
        `origin="upper"` the same way a standard north-up raster does -- see `plot_overview_map`'s
        own backdrop `imshow` call, which relies on the same convention. `1.0` (white) where sunlit,
        `SHADOW_GRAY_LEVEL` where in shadow.
    """
    sub_lon_deg, sub_lat_deg = illumination.sub_solar_lonlat_deg(et)
    lons_rad = np.radians(np.linspace(-180.0, 180.0, width))
    lats_rad = np.radians(np.linspace(90.0, -90.0, height))
    lon_grid, lat_grid = np.meshgrid(lons_rad, lats_rad)
    sub_lon_rad, sub_lat_rad = np.radians(sub_lon_deg), np.radians(sub_lat_deg)
    sin_elevation = np.sin(sub_lat_rad) * np.sin(lat_grid) + np.cos(sub_lat_rad) * np.cos(lat_grid) * np.cos(
        lon_grid - sub_lon_rad
    )
    return np.where(sin_elevation > 0.0, 1.0, SHADOW_GRAY_LEVEL)


def _fetch_global_backdrop(config: TrntestConfig) -> Path:
    """Fetch (and cache) a coarse whole-Moon `GLOBAL_BACKDROP_LAYER` mosaic in plain geographic
    lon/lat."""
    bbox = (-180.0, -90.0, 180.0, 90.0)
    return cache.fetch_lunaserv_getmap(
        GLOBAL_BACKDROP_LAYER,
        bbox,
        GLOBAL_BACKDROP_WIDTH_PX,
        GLOBAL_BACKDROP_HEIGHT_PX,
        cache_root=config.cache_root,
        srs=config.lunaserv_dem_srs,  # the fixed plain-geographic CRS (IAU2000:30100) -- DEM-flavored
        # name, but body/CRS-generic; reused as-is rather than adding a second identical constant.
        base_url=config.lunaserv_base_url,
        fmt="image/tiff",
    )


def _require_point(corner: tuple[float, float] | None) -> tuple[float, float]:
    """Narrows one `Camera.footprint_lonlat_deg` entry -- every corner is a real ground point for a
    generated entry's own camera, this just satisfies mypy at the call site."""
    assert corner is not None, "camera footprint corner must be a real ground point"
    return corner


def _antimeridian_split_xy(points: list[tuple[float, float]]) -> tuple[list[float], list[float]]:
    """`points`' lon/lat as `(lons, lats)` for a single `ax.plot` call, with a `nan` inserted at any
    edge that crosses +/-180 -- matplotlib skips drawing a line across a `nan`, avoiding a spurious
    line straight across the whole plot wherever the path (a closed FOV ring, or an open ground
    track) straddles the antimeridian (plain matplotlib has no built-in geographic wraparound). Same
    per-edge unwrap-then-clip technique as `dataset_selection_plots._underline_segments`,
    generalized from one line segment to an arbitrary-length path -- works identically whether
    `points` closes back on itself (a ring) or not (a track), since only consecutive pairs matter.

    :param points: Ordered lon/lat points, degrees -- a closed ring (first point repeated at the
        end) or an open path, either works.
    """
    lons = [points[0][0]]
    lats = [points[0][1]]
    for (lon0, lat0), (lon1, lat1) in zip(points, points[1:], strict=False):
        lon1_unwrapped = illumination.unwrap_relative_deg(lon0, lon1)
        boundary = 180.0 if lon1_unwrapped > lon0 else -180.0
        crosses = lon1_unwrapped != lon0 and min(lon0, lon1_unwrapped) <= boundary <= max(lon0, lon1_unwrapped)
        if crosses:
            frac = (boundary - lon0) / (lon1_unwrapped - lon0)
            lat_at_boundary = lat0 + frac * (lat1 - lat0)
            lons.extend([boundary, float("nan"), -boundary])
            lats.extend([lat_at_boundary, float("nan"), lat_at_boundary])
        lons.append(lon1)
        lats.append(lat1)
    return lons, lats


GROUND_TRACK_STEP_S = 60.0  # LRO covers ~1.6 km/s -- ~96km/sample, plenty smooth at whole-Moon
# map scale; far coarser than _LRO_ORBITAL_PERIOD_S (113 min), so consecutive orbits' own tracks
# (which may land close together in MOON_ME -- the Moon barely rotates between them) are still each
# fully resolved, not aliased into each other by an overly coarse step.


def _ground_track_lonlat(dataset: TrnTestDataSet) -> list[tuple[float, float]]:
    """Sub-spacecraft ground track (`illumination.spacecraft_lonlat_deg`) sampled every
    `GROUND_TRACK_STEP_S` across `dataset`'s own real time span (earliest to latest -- see
    `TrnTestDataSet.time_span_columns` for which manifest columns those are, per `entry_kind`) --
    pure position-vector geometry, no shape model, no per-entry camera cost.
    """
    # format="ISO8601": see dataset_midpoint_datetime's own comment -- rows mix sub-second
    # precision, and pandas' single-inferred-format guess raises on whichever rows don't match it.
    start_col, stop_col = dataset.time_span_columns
    start_et = illumination.utc_to_et(pd.to_datetime(dataset.images[start_col], format="ISO8601").min().to_pydatetime())
    stop_et = illumination.utc_to_et(pd.to_datetime(dataset.images[stop_col], format="ISO8601").max().to_pydatetime())
    n_samples = max(2, round((stop_et - start_et) / GROUND_TRACK_STEP_S) + 1)
    return [illumination.spacecraft_lonlat_deg(et) for et in np.linspace(start_et, stop_et, n_samples)]


def plot_overview_map(dataset: TrnTestDataSet, config: TrntestConfig | None = None) -> plt.Figure:
    """Ground-track-style overview of every entry in `dataset`: a whole-Moon backdrop layered over a
    day/night mask at the dataset's temporal midpoint, the sub-spacecraft ground track across the
    dataset's own time span underneath, and each entry's own FOV footprint on top (a straight-line
    quadrilateral through its camera's 4 corner points -- fine at this whole-Moon zoom level, no need
    for the real geodesic edges). No per-entry index label -- real datasets pack footprints too
    densely for one to stay readable; `status.csv`/the overview table are the place to look up a
    specific entry.

    Uses `entry.lightweight_footprint_lonlat_deg` for each entry's footprint corners, not
    `entry.camera` directly -- for a `TrnTestEntryEdr`, that's a cheap, ISIS-free SPICE
    approximation (`camera.lightweight_footprint_lonlat_deg`; see that function's own docstring for
    what it trades away and why, including forcing `wac_ck_source="naif_metakernel"` for the same
    reason `candidate_window.evaluate_candidate_image` does), deliberately avoiding a full ISIS
    pipeline run for a not-yet-populated entry -- an O(entries) ISIS blowup the live default
    (`"isis_resolved"`) would otherwise force, one per entry in the whole dataset, with no accuracy
    cost avoided (both sources give numerically identical WAC pointing). For a `TrnTestEntrySpice`
    entry, `entry.camera` is already this cheap (no ISIS involved at all), so that kind's own
    `lightweight_footprint_lonlat_deg` just returns it directly. Either way, this map never needs
    branching logic on population state or entry kind: every entry gets the same treatment.

    :returns: The `Figure`.
    """
    config = config or load_config()
    midpoint_dt = dataset_midpoint_datetime(dataset)
    spice_kernels.fetch_and_furnish(midpoint_dt, config)
    midpoint_et = illumination.utc_to_et(midpoint_dt)

    backdrop_path = _fetch_global_backdrop(config)
    with rasterio.open(backdrop_path) as src:
        backdrop = src.read(1)

    day_night = _day_night_mask(midpoint_et, width=backdrop.shape[1], height=backdrop.shape[0])

    fig, ax = plt.subplots(figsize=(12, 7))
    extent = (-180, 180, -90, 90)
    ax.imshow(day_night, cmap="gray", vmin=0, vmax=1, extent=extent)
    ax.imshow(backdrop, cmap="gray", extent=extent, alpha=BACKDROP_ALPHA)
    track_lons, track_lats = _antimeridian_split_xy(_ground_track_lonlat(dataset))
    ax.plot(track_lons, track_lats, color="darkblue", linewidth=0.5, alpha=0.6, zorder=1)
    for entry in dataset:
        corners = entry.lightweight_footprint_lonlat_deg
        ring = [_require_point(corners[name]) for name in (*tie_points.CORNER_NAMES, tie_points.CORNER_NAMES[0])]
        lons, lats = _antimeridian_split_xy(ring)
        ax.plot(lons, lats, color="darkred", linewidth=0.8, zorder=2)
    ax.set_xlim(-180, 180)
    ax.set_ylim(-90, 90)
    ax.set_xticks(range(-180, 181, 30))
    ax.set_yticks(range(-90, 91, 30))
    ax.grid(True, color="black", alpha=0.3, linewidth=0.5)
    ax.set_xlabel("longitude (deg)")
    ax.set_ylabel("latitude (deg)")
    midpoint_str = midpoint_dt.strftime("%Y-%m-%d %H:%M:%S")
    ax.set_title(f"{dataset.name} -- {len(dataset)} entries, illumination at {midpoint_str}")
    fig.tight_layout()
    return fig


def write_overview_map(dataset: TrnTestDataSet, config: TrntestConfig | None = None) -> Path:
    """Renders `plot_overview_map` and writes it to `<dataset.folder>/reports/overview_map.png`,
    plus a thin `<dataset.folder>/reports/map.html` wrapper around it -- called by
    `TrnTestDataSet.write_index()` (pass `write_overview_map=False` there to skip it), linked from
    the nav bar's "Map" link.

    The `.html` wrapper exists because linking directly to the raw `.png` as a nav-bar target makes
    the browser treat it as a standalone image document -- Firefox in particular shrinks it to a
    thumbnail with an unreliable click-to-zoom, confirmed live to look broken inside the nav bar's
    content frame. A plain page with a scaled `<img>` avoids that entirely.

    :returns: The written PNG's path (`map.html`'s own path is a fixed, predictable sibling).
    """
    fig = plot_overview_map(dataset, config)
    path = dataset.folder / "reports" / "overview_map.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    html = f"""<!DOCTYPE html>
<html>
<head>
<title>{dataset.name} map</title>
<style>
  body {{ margin: 0; }}
  img {{ display: block; max-width: 100%; height: auto; }}
</style>
</head>
<body>
<img src="overview_map.png" alt="{dataset.name} overview map">
</body>
</html>"""
    (dataset.folder / "reports" / "map.html").write_text(html)
    return path
