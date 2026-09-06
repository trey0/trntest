"""Dataset-wide overview map: a ground-track-style plot of every entry in a `TrnTestDataSet` on a
global lunar backdrop, for `docs/proposed-tasks/report-plan.md`'s planned overview-map page. Wired
into `TrnTestDataSet.write_index()` (pass `write_overview_map=False` there to skip it); not yet
linked from any nav bar (still one of that plan's open "Future work" items).
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
BACKDROP_ALPHA = 0.4  # report-plan.md's original "~20% opacity" read too faint in practice
SHADOW_GRAY_LEVEL = 0.8  # report-plan.md's "~80% white (light grey) where in shadow" -- a grayscale
# value (0.8 -> 80% of full white), not an alpha; the day/night layer itself is fully opaque, it's
# the backdrop drawn on top of it that's at BACKDROP_ALPHA.


def dataset_midpoint_datetime(dataset: TrnTestDataSet) -> datetime:
    """The dataset's temporal midpoint -- halfway between its earliest `start_time` and latest
    `stop_time` -- for the overview map's single global illumination snapshot (see module
    docstring: one shared snapshot, not per-entry lighting)."""
    start = pd.to_datetime(dataset.images["start_time"]).min()
    stop = pd.to_datetime(dataset.images["stop_time"]).max()
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


def _unwrap_ring_relative_to_first(ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """`ring`'s points, with longitude unwrapped onto the branch nearest the first point's own
    longitude -- same technique as `geo_utils.footprint_bbox_deg`, so a ring crossing +/-180 forms
    one geometrically contiguous shape (possibly outside `[-180, 180]`) instead of jumping across the
    whole plot when its own extent (e.g. a bounding-box corner) is computed."""
    ref_lon = ring[0][0]
    return [(illumination.unwrap_relative_deg(ref_lon, lon), lat) for lon, lat in ring]


def _upper_right_label_point(ring: list[tuple[float, float]]) -> tuple[float, float]:
    """The label anchor for one entry's FOV ring: its bounding box's upper-right corner (max
    longitude, max latitude among its own points) -- not any single actual vertex, and not the
    footprint's center (a center label collides with the polygon interior/edges). Unwraps first
    (`_unwrap_ring_relative_to_first`) so a ring that crosses +/-180 still gets its true upper-right
    corner, then wraps the result back into `[-180, 180]` for display.
    """
    unwrapped = _unwrap_ring_relative_to_first(ring)
    max_lon = max(lon for lon, _ in unwrapped)
    max_lat = max(lat for _, lat in unwrapped)
    return ((max_lon + 180.0) % 360.0) - 180.0, max_lat


def _antimeridian_split_xy(ring: list[tuple[float, float]]) -> tuple[list[float], list[float]]:
    """`ring`'s lon/lat points as `(lons, lats)` for a single `ax.plot` call, with a `nan` inserted
    at any edge that crosses +/-180 -- matplotlib skips drawing a line across a `nan`, avoiding a
    spurious line straight across the whole plot for a footprint that straddles the antimeridian
    (plain matplotlib has no built-in geographic wraparound). Same per-edge unwrap-then-clip
    technique as `dataset_selection_plots._underline_segments`, generalized from one line segment to
    a closed ring.

    :param ring: Closed polygon points (first point repeated at the end), degrees.
    """
    lons = [ring[0][0]]
    lats = [ring[0][1]]
    for (lon0, lat0), (lon1, lat1) in zip(ring, ring[1:], strict=False):
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


def plot_overview_map(dataset: TrnTestDataSet, config: TrntestConfig | None = None) -> plt.Figure:
    """Ground-track-style overview of every entry in `dataset`: a whole-Moon backdrop layered over a
    day/night mask at the dataset's temporal midpoint, with each entry's own FOV footprint (a
    straight-line quadrilateral through its camera's 4 corner points -- fine at this whole-Moon
    zoom level, no need for the real geodesic edges) and an `entry.index` label.

    Builds a real `Camera` per entry (`entry.camera.footprint_lonlat_deg`) to get its footprint
    corners -- a real per-entry SPICE cost (unlike everything else this function reads, which comes
    straight from the manifest), worth it for genuine FOV polygons rather than center-point markers.

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
    for entry in dataset:
        corners = entry.camera.footprint_lonlat_deg
        ring = [_require_point(corners[name]) for name in (*tie_points.CORNER_NAMES, tie_points.CORNER_NAMES[0])]
        lons, lats = _antimeridian_split_xy(ring)
        ax.plot(lons, lats, color="darkred", linewidth=0.8)
        ax.annotate(
            str(entry.index),
            _upper_right_label_point(ring),
            xytext=(3, 3),
            textcoords="offset points",
            color="darkred",
            fontsize=7,
        )
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
