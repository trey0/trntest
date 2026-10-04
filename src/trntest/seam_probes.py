"""Seam probes: render a source mosaic on a synthetic AOI centered where its tiles meet, and measure
artifacts along each tile seam. Shared by `notebooks/reflectance_seams.ipynb` (an index of per-probe
health rows), the per-probe report notebooks it generates (`write_probe_reports`), and the heavy seam
tests.

A probe needs no camera or SPICE: its AOI is a square local-Orthographic grid, the same kind of grid
every generator's DEM/ortho uses. A source (`SeamSource`, registered in `SOURCES`) is its seams, its
probes, a renderer and pass limits: today the WAC_EMP reflectance mosaic and the GLD100 DEM.
"""

import dataclasses
import math
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import rasterio
from pyproj import Transformer

from trntest import dem_sources, ortho_wac_emp
from trntest.config import MOON_RADIUS_M, TrntestConfig, load_config
from trntest.geo_utils import geographic_crs, local_orthographic_crs, pixel_center_coords_m, write_local_grid_array
from trntest.report import render_template
from trntest.subprocess_utils import run_quiet


@dataclasses.dataclass(frozen=True)
class Seam:
    """A curve where a source's tiles meet: a parallel (`kind="lat"`) or a meridian (`kind="lon"`).

    :ivar name: Display label, e.g. `"lon 180"`.
    :ivar kind: `"lat"` or `"lon"`.
    :ivar value_deg: The seam's latitude, or its longitude in `[0, 360)`.
    :ivar lat_range_deg: `(min, max)` latitude where the seam exists (a lon seam between equirect tiles
        stops where the polar tile takes over).
    """

    name: str
    kind: Literal["lat", "lon"]
    value_deg: float
    lat_range_deg: tuple[float, float] = (-90.0, 90.0)

    @property
    def sides(self) -> tuple[str, str]:
        """`(negative, positive)` side of the seam's signed distance: `("south", "north")` for a lat
        seam, `("west", "east")` for a lon seam."""
        return ("south", "north") if self.kind == "lat" else ("west", "east")

    @property
    def along_direction(self) -> str:
        """The direction the seam runs in (positive along-seam coordinate): east or north."""
        return "east" if self.kind == "lat" else "north"


@dataclasses.dataclass(frozen=True)
class SeamProbe:
    """A square synthetic AOI.

    :ivar name: Short identifier, also the render's file stem.
    :ivar center_lon_deg: AOI center longitude, degrees.
    :ivar center_lat_deg: AOI center latitude, degrees.
    :ivar half_size_m: Half the AOI's side length, meters.
    """

    name: str
    center_lon_deg: float
    center_lat_deg: float
    half_size_m: float = 100_000.0


@dataclasses.dataclass
class ProbeGrid:
    """A probe's local-Orthographic grid, plus each pixel center's lon/lat.

    :ivar probe: The probe this grid is for.
    :ivar bbox_m: `(minx, miny, maxx, maxy)`, meters.
    :ivar width: Pixels.
    :ivar height: Pixels.
    :ivar gsd_m: Pixel size, meters.
    :ivar lon_deg: `(height, width)` longitude, `[0, 360)`.
    :ivar lat_deg: `(height, width)` latitude.
    """

    probe: SeamProbe
    bbox_m: tuple[float, float, float, float]
    width: int
    height: int
    gsd_m: float
    lon_deg: np.ndarray
    lat_deg: np.ndarray

    def signed_distance_px(self, seam: Seam) -> np.ndarray:
        """Approximate signed distance from each pixel to `seam`, in pixels (positive north of a lat
        seam, east of a lon seam), `NaN` where `seam` doesn't exist.

        :param seam: The seam.
        :returns: `(height, width)` float array.
        """
        # Spherical small-offset approximation: accurate to well under a pixel within the few tens of
        # pixels the profiles look at, which is all that matters here.
        m_per_deg = math.pi * MOON_RADIUS_M / 180.0
        if seam.kind == "lat":
            distance = (self.lat_deg - seam.value_deg) * m_per_deg / self.gsd_m
        else:
            dlon = (self.lon_deg - seam.value_deg + 180.0) % 360.0 - 180.0
            distance = dlon * np.cos(np.radians(self.lat_deg)) * m_per_deg / self.gsd_m
        lo, hi = seam.lat_range_deg
        return np.where((self.lat_deg >= lo) & (self.lat_deg <= hi), distance, np.nan)


def probe_grid(probe: SeamProbe, gsd_m: float) -> ProbeGrid:
    """Build `probe`'s grid at `gsd_m`.

    :param probe: The probe.
    :param gsd_m: Pixel size, meters.
    :returns: The grid.
    """
    half = probe.half_size_m
    bbox = (-half, -half, half, half)
    size = round(2 * half / gsd_m)
    xs, ys = pixel_center_coords_m(bbox, size, size)
    xx, yy = np.meshgrid(xs, ys)
    to_geo = Transformer.from_crs(
        local_orthographic_crs(probe.center_lon_deg, probe.center_lat_deg), geographic_crs(), always_xy=True
    )
    lon, lat = to_geo.transform(xx, yy)
    return ProbeGrid(probe, bbox, size, size, gsd_m, np.mod(lon, 360.0), lat)


Renderer = Callable[[ProbeGrid, TrntestConfig, Path], tuple[np.ndarray, list[str]]]
"""`(grid, config, output_path) -> (array, source_ids)`: render one source onto `grid`, returning the
`(height, width)` array (`NaN` = no data) and the source tiles/products it used."""


# --- WAC_EMP reflectance ---------------------------------------------------------------------------

WAC_EMP_SEAMS: tuple[Seam, ...] = (
    Seam("lat -60", "lat", -ortho_wac_emp.WAC_EMP_MAX_ABS_LATITUDE_DEG),
    Seam("lat 0", "lat", 0.0),
    Seam("lat +60", "lat", ortho_wac_emp.WAC_EMP_MAX_ABS_LATITUDE_DEG),
    *(
        Seam(
            f"lon {lon:g}",
            "lon",
            lon,
            (-ortho_wac_emp.WAC_EMP_MAX_ABS_LATITUDE_DEG, ortho_wac_emp.WAC_EMP_MAX_ABS_LATITUDE_DEG),
        )
        for lon in (0.0, 90.0, 180.0, 270.0)
    ),
)
"""Where WAC_EMP's tiles meet: the equirect/polar split at +-60 deg, the equirect N/S split at the
equator, and the equirect grid's 90-deg longitude zones."""

WAC_EMP_PROBES: tuple[SeamProbe, ...] = tuple(
    SeamProbe(f"{abs(lat):g}{'N' if lat > 0 else 'S' if lat < 0 else ''}_{lon:03d}E", float(lon), float(lat))
    for lat in (60, -60, 0)
    for lon in (0, 90, 180, 270)
)
"""Every point where three or four WAC_EMP tiles meet: each lon zone boundary at +-60 deg (two
equirect tiles and a polar tile) and at the equator (four equirect tiles)."""


def render_wac_emp_reflectance(
    grid: ProbeGrid, config: TrntestConfig, output_path: Path
) -> tuple[np.ndarray, list[str]]:
    """`Renderer` for the WAC_EMP reflectance mosaic, through the same production path the `hillshade`
    generator's texture uses (`ortho_wac_emp`), edge correction per `config`.

    :param grid: The probe grid.
    :param config: Project config (cache location, edge-correction toggle).
    :param output_path: Where to write the mosaic GeoTIFF.
    :returns: `(reflectance, tile_ids)`.
    """
    probe = grid.probe
    tiles = ortho_wac_emp.fetch_wac_emp_reflectance(grid.bbox_m, probe.center_lon_deg, probe.center_lat_deg, config)
    ortho_wac_emp.reproject_wac_emp_reflectance_to_local_grid(
        [path for path, _ in tiles],
        grid.bbox_m,
        grid.width,
        grid.height,
        probe.center_lon_deg,
        probe.center_lat_deg,
        MOON_RADIUS_M,
        output_path,
        apply_edge_correction=config.wac_emp_edge_correction_enabled,
    )
    with rasterio.open(output_path) as src:
        array = src.read(1).astype(np.float64)
    return array, sorted(tile_id for _, tile_id in tiles)


# --- DEM elevation ---------------------------------------------------------------------------------

_DEM_SEAM_LATS_DEG = (60.0, 30.0, 0.0, -30.0, -60.0)
_DEM_SEAM_LONS_DEG = tuple(float(lon) for lon in range(0, 360, 45))

DEM_SEAMS: tuple[Seam, ...] = (
    *(Seam(f"lat {lat:+g}" if lat else "lat 0", "lat", lat) for lat in _DEM_SEAM_LATS_DEG),
    *(Seam(f"lon {lon:g}", "lon", lon) for lon in _DEM_SEAM_LONS_DEG),
)
"""Where a GLD100/SLDEM2015 DEM mosaic's sources meet: SLDEM2015's 512-ppd tile edges (every 30 deg of
latitude up to +-60 deg, every 45 deg of longitude; 180 deg, the geographic CRS's branch cut, is one of
them). GLD100 alone is one file whose only edge is 0 deg (it is centered on 180 deg), so for it the
others are ordinary terrain, except +-60 deg, where GLD100 is itself assembled."""

DEM_PROBES: tuple[SeamProbe, ...] = (
    *(
        SeamProbe(f"{abs(lat):g}{'N' if lat > 0 else 'S' if lat < 0 else ''}_{lon:03.0f}E", lon, lat)
        for lat in _DEM_SEAM_LATS_DEG
        for lon in _DEM_SEAM_LONS_DEG
    ),
    SeamProbe("45N_000E", 0.0, 45.0),
    SeamProbe("45S_000E", 0.0, -45.0),
)
"""Every point where four `DEM_SEAMS` tiles meet (or three, at +-60 deg), plus 0 deg away from any
lat seam (the wrap alone)."""


def dem_renderer(dem_source: str) -> Renderer:
    """A `Renderer` for the DEM `dem_ortho.fetch_dem` builds with `TrntestConfig.dem_source =
    dem_source`, before `dem_ortho.hole_fill_dem`, so a gap shows as `NaN` instead of being filled
    over.

    :param dem_source: A `dem_sources.DEM_SOURCES` key.
    :returns: The renderer; its source ids are every tile of every source that overlaps the grid.
    """
    mosaic = dem_sources.DEM_SOURCES[dem_source]

    def render(grid: ProbeGrid, config: TrntestConfig, output_path: Path) -> tuple[np.ndarray, list[str]]:
        probe = grid.probe
        local = dem_sources.LocalGrid(grid.bbox_m, grid.width, grid.height, probe.center_lon_deg, probe.center_lat_deg)
        elevation = dem_sources.mosaic_elevation(mosaic, local, config)
        write_local_grid_array(
            elevation, grid.bbox_m, probe.center_lon_deg, probe.center_lat_deg, MOON_RADIUS_M, output_path
        )
        tile_ids = [tile.tile_id for source in mosaic.sources for tile in dem_sources.tiles_for_grid(source, local)]
        return elevation.astype(np.float64), tile_ids

    return render


# --- Profiles and metrics ------------------------------------------------------------------------

NEAR_SEAM_PX = 3
"""Half-width of the band treated as "at the seam" (metrics' spike/NaN/gradient window)."""
REFERENCE_PX = (6, 30)
"""`(min, max)` |distance| of the bands each side of the seam used as that side's reference level."""
PROFILE_MAX_PX = 40
CORNER_EXCLUSION_PX = 20
"""Pixels this close to a different seam are left out of a seam's profile, so a corner's other seam
doesn't leak into it."""
CONTROL_OFFSETS_PX = (-320, -240, -160, -80, 80, 160, 240, 320)
"""Offsets of the control lines profiled alongside each seam: parallel lines inside one tile, whose
metrics show how much terrain alone moves them."""


@dataclasses.dataclass
class SeamProfile:
    """Statistics of the rendered array in 1-px bins of signed distance from one seam.

    :ivar seam: The seam.
    :ivar distance_px: Bin centers, pixels: 1-px bins `[k, k + 1)` of signed distance, so the two
        bins either side of the seam are centered at `-0.5`/`+0.5`.
    :ivar median: Median value per bin (`NaN` for an all-`NaN` bin).
    :ivar p25: 25th percentile per bin.
    :ivar p75: 75th percentile per bin.
    :ivar nan_fraction: Fraction of the bin's pixels that are `NaN`.
    :ivar gradient_median: Median gradient magnitude per bin, value units per pixel.
    :ivar count: Pixels per bin.
    """

    seam: Seam
    distance_px: np.ndarray
    median: np.ndarray
    p25: np.ndarray
    p75: np.ndarray
    nan_fraction: np.ndarray
    gradient_median: np.ndarray
    count: np.ndarray


@dataclasses.dataclass
class SeamMetrics:
    """Summary numbers for one seam in one probe. The heavy tests threshold these. `step` and `spike`
    are relative to the probe's median for a relative source (`SeamSource.relative`, e.g. reflectance)
    and in the source's units otherwise (e.g. meters of elevation).

    :ivar probe: Probe name.
    :ivar seam: Seam name (a control line's name ends in its offset, e.g. `"lon 180 +80px"`).
    :ivar control: Whether this is a control line rather than a real seam.
    :ivar length_px: Pixels per bin adjacent to the seam (how much of the seam the profile sees).
    :ivar nan_near: `NaN` pixels within `NEAR_SEAM_PX` of the seam.
    :ivar step: North/east reference trend minus south/west, both extrapolated to the seam
        (`reference_trends`).
    :ivar spike: Largest |bin median - its own side's extrapolated trend| within `NEAR_SEAM_PX` (a
        bright/dark line at the seam, or a step's blended edge).
    :ivar gradient_ratio: Largest near-seam gradient median divided by the reference bins' median.
    """

    probe: str
    seam: str
    control: bool
    length_px: int
    nan_near: int
    step: float
    spike: float
    gradient_ratio: float


@dataclasses.dataclass
class ProbeResult:
    """One probe's render, the seams it crosses, and their profiles and metrics.

    :ivar grid: The probe grid.
    :ivar array: The rendered array (`NaN` = no data).
    :ivar source_ids: Tiles/products the renderer used.
    :ivar distances_px: Signed distance per crossing seam (`ProbeGrid.signed_distance_px`).
    :ivar profiles: One per crossing seam.
    :ivar metrics: One per crossing seam, then one per control line (`CONTROL_OFFSETS_PX`).
    :ivar value_name: What `array` holds, for labels, e.g. `"reflectance"`.
    :ivar value_units: Its units, e.g. `"unitless"` or `"m"`.
    :ivar relative: Whether `metrics`' `step`/`spike` are relative to the probe's median.
    :ivar shade: Whether to show `array` as a hillshade (elevation) rather than directly.
    """

    grid: ProbeGrid
    array: np.ndarray
    source_ids: list[str]
    distances_px: dict[str, np.ndarray]
    profiles: list[SeamProfile]
    metrics: list[SeamMetrics]
    value_name: str = "value"
    value_units: str = "unitless"
    relative: bool = True
    shade: bool = False

    @property
    def nan_count(self) -> int:
        return int(np.isnan(self.array).sum())


def seam_profile(array: np.ndarray, distance_px: np.ndarray, exclude: np.ndarray, seam: Seam) -> SeamProfile:
    """Bin `array` by signed distance from `seam`.

    :param array: Rendered array.
    :param distance_px: `ProbeGrid.signed_distance_px(seam)`.
    :param exclude: Boolean mask of pixels to leave out (e.g. near another seam).
    :param seam: The seam.
    :returns: The profile.
    """
    gy, gx = np.gradient(array)
    gradient = np.hypot(gx, gy)
    centers = np.arange(-PROFILE_MAX_PX, PROFILE_MAX_PX) + 0.5
    with np.errstate(invalid="ignore"):
        index = np.floor(distance_px) + PROFILE_MAX_PX
    usable = ~exclude & np.isfinite(index) & (index >= 0) & (index < len(centers))
    bin_index = index[usable].astype(int)
    values, grads = array[usable], gradient[usable]
    order = np.argsort(bin_index, kind="stable")
    bin_index, values, grads = bin_index[order], values[order], grads[order]
    splits = np.searchsorted(bin_index, np.arange(1, len(centers)))
    stats = np.full((6, len(centers)), np.nan)
    for i, (v, g) in enumerate(zip(np.split(values, splits), np.split(grads, splits), strict=True)):
        stats[5, i] = v.size
        if v.size == 0:
            continue
        stats[4, i] = np.isnan(v).mean()
        finite = v[np.isfinite(v)]
        if finite.size:
            stats[0:3, i] = np.percentile(finite, [50, 25, 75])
        finite_g = g[np.isfinite(g)]
        if finite_g.size:
            stats[3, i] = np.median(finite_g)
    return SeamProfile(seam, centers, stats[0], stats[1], stats[2], stats[4], stats[3], stats[5].astype(int))


def _reference_bins(distance_px: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lo, hi = REFERENCE_PX
    return (distance_px <= -lo) & (distance_px >= -hi), (distance_px >= lo) & (distance_px <= hi)


def reference_trends(profile: SeamProfile) -> tuple[np.poly1d, np.poly1d]:
    """Straight-line fits to each side's reference bins (`REFERENCE_PX`), for extrapolating that side's
    level to the seam without a terrain gradient reading as a step.

    :param profile: `seam_profile`'s output.
    :returns: `(south_or_west, north_or_east)` trends as functions of signed distance, pixels; a side
        with fewer than two finite reference bins gets a constant `NaN`.
    """
    fits = []
    for ref in _reference_bins(profile.distance_px):
        finite = ref & np.isfinite(profile.median)
        if finite.sum() <= 1:
            fits.append(np.poly1d([math.nan]))
        else:
            fits.append(np.poly1d(np.polyfit(profile.distance_px[finite], profile.median[finite], 1)))
    return fits[0], fits[1]


def seam_metrics(
    probe_name: str,
    profile: SeamProfile,
    array: np.ndarray,
    distance_px: np.ndarray,
    control: bool = False,
    relative: bool = True,
) -> SeamMetrics:
    """Reduce a profile to `SeamMetrics`.

    :param probe_name: For labeling.
    :param profile: `seam_profile`'s output.
    :param array: The rendered array (for the near-seam `NaN` count and the median).
    :param distance_px: The seam's signed distance (same as passed to `seam_profile`).
    :param control: Whether `profile` is a control line's.
    :param relative: Divide `step`/`spike` by `array`'s median.
    :returns: The metrics.
    """
    d, med = profile.distance_px, profile.median
    south_ref, north_ref = _reference_bins(d)
    near = np.abs(d) <= NEAR_SEAM_PX
    trend_s, trend_n = reference_trends(profile)
    scale = np.nanmedian(array) if relative else 1.0
    # Each near-seam bin is compared with its own side's trend. Bilinear blending across a pure step
    # puts up to half the step into the two bins beside the seam, so a step also shows up here.
    trend = np.where(d < 0, trend_s(d), trend_n(d))
    deviation = np.abs(med - trend)
    spike = float(np.nanmax(deviation[near])) if np.isfinite(deviation[near]).any() else math.nan
    grad_ref = np.nanmedian(profile.gradient_median[south_ref | north_ref])
    return SeamMetrics(
        probe=probe_name,
        seam=profile.seam.name,
        control=control,
        length_px=int(profile.count[np.abs(d) < 1].mean()),
        nan_near=int((np.isnan(array) & (np.abs(distance_px) <= NEAR_SEAM_PX)).sum()),
        step=float((trend_n(0.0) - trend_s(0.0)) / scale),
        spike=spike / scale,
        gradient_ratio=float(np.nanmax(profile.gradient_median[near]) / grad_ref),
    )


@dataclasses.dataclass
class SeamStrip:
    """The band around one seam, straightened: rows are along-seam position, columns signed distance.

    :ivar seam: The seam.
    :ivar distance_px: Column centers, pixels (same bins as `SeamProfile.distance_px`).
    :ivar along_px: Row centers, pixels along the seam from the AOI center (`Seam.along_direction`
        positive).
    :ivar mean: Mean value per cell (`NaN` where empty or all-`NaN`).
    :ivar nan_fraction: Fraction of each cell's pixels that are `NaN`.
    """

    seam: Seam
    distance_px: np.ndarray
    along_px: np.ndarray
    mean: np.ndarray
    nan_fraction: np.ndarray

    def contrast(self) -> np.ndarray:
        """`mean` divided by each row's median, so albedo changes along the seam drop out and
        across-seam structure (a line, a step) stands out."""
        with np.errstate(invalid="ignore", divide="ignore"):
            return self.mean / np.nanmedian(self.mean, axis=1, keepdims=True)


def seam_strip(
    result: "ProbeResult", seam_name: str, along_bin_px: int = 10, array: np.ndarray | None = None
) -> SeamStrip:
    """Straighten the `PROFILE_MAX_PX` band around one of `result`'s seams.

    :param result: `run_probe`'s output.
    :param seam_name: One of `result.distances_px`'s keys.
    :param along_bin_px: Row height, pixels.
    :param array: What to straighten, on `result`'s grid (e.g. a hillshade of it); defaults to
        `result.array`.
    :returns: The strip.
    """
    grid, distance = result.grid, result.distances_px[seam_name]
    seam = next(p.seam for p in result.profiles if p.seam.name == seam_name)
    px_per_deg = math.pi * MOON_RADIUS_M / 180.0 / grid.gsd_m
    if seam.kind == "lat":
        dlon = (grid.lon_deg - grid.probe.center_lon_deg + 180.0) % 360.0 - 180.0
        along = dlon * np.cos(np.radians(grid.lat_deg)) * px_per_deg
    else:
        along = (grid.lat_deg - grid.probe.center_lat_deg) * px_per_deg
    with np.errstate(invalid="ignore"):
        column = np.floor(distance) + PROFILE_MAX_PX
    n_cols = 2 * PROFILE_MAX_PX
    in_band = np.isfinite(column) & (column >= 0) & (column < n_cols)
    along_edges = np.arange(
        np.floor(along[in_band].min()), np.ceil(along[in_band].max()) + along_bin_px, along_bin_px, dtype=float
    )
    row = np.clip(np.digitize(along[in_band], along_edges) - 1, 0, len(along_edges) - 2)
    cell = row * n_cols + column[in_band].astype(int)
    n_cells = (len(along_edges) - 1) * n_cols
    values = (result.array if array is None else array)[in_band]
    finite = np.isfinite(values)
    count = np.bincount(cell, minlength=n_cells)
    finite_count = np.bincount(cell[finite], minlength=n_cells)
    total = np.bincount(cell[finite], weights=values[finite], minlength=n_cells)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(finite_count > 0, total / finite_count, np.nan)
        nan_fraction = np.where(count > 0, 1 - finite_count / count, np.nan)
    shape = (len(along_edges) - 1, n_cols)
    return SeamStrip(
        seam,
        np.arange(-PROFILE_MAX_PX, PROFILE_MAX_PX) + 0.5,
        (along_edges[:-1] + along_edges[1:]) / 2,
        mean.reshape(shape),
        nan_fraction.reshape(shape),
    )


def run_probe(
    probe: SeamProbe,
    seams: Sequence[Seam],
    renderer: Renderer,
    config: TrntestConfig,
    output_dir: Path,
    gsd_m: float | None = None,
    value_name: str = "value",
    value_units: str = "unitless",
    relative: bool = True,
    shade: bool = False,
) -> ProbeResult:
    """Render `probe` with `renderer` and profile every seam in `seams` that crosses it.

    :param probe: The probe.
    :param seams: The source's seams (e.g. `WAC_EMP_SEAMS`).
    :param renderer: e.g. `render_wac_emp_reflectance`.
    :param config: Project config.
    :param output_dir: Where the renderer writes `<probe.name>.tif`.
    :param gsd_m: Pixel size; defaults to `config.dem_target_gsd_m`.
    :param value_name: What the renderer produces, for labels.
    :param value_units: Its units.
    :param relative: Report `step`/`spike` relative to the probe's median.
    :param shade: Show the render as a hillshade.
    :returns: The result.
    """
    grid = probe_grid(probe, gsd_m or config.dem_target_gsd_m)
    output_dir.mkdir(parents=True, exist_ok=True)
    array, source_ids = renderer(grid, config, output_dir / f"{probe.name}.tif")
    distances = {seam.name: grid.signed_distance_px(seam) for seam in seams}
    crossing = [seam for seam in seams if _crosses(distances[seam.name])]
    near_seam = {
        seam.name: np.abs(np.nan_to_num(distances[seam.name], nan=np.inf)) <= CORNER_EXCLUSION_PX for seam in crossing
    }
    profiles, metrics = [], []
    for seam in crossing:
        exclude = np.logical_or.reduce(
            [near_seam[s.name] for s in crossing if s is not seam] + [np.zeros_like(array, bool)]
        )
        profile = seam_profile(array, distances[seam.name], exclude, seam)
        profiles.append(profile)
        metrics.append(seam_metrics(probe.name, profile, array, distances[seam.name], relative=relative))
    all_seams = np.logical_or.reduce(list(near_seam.values()) + [np.zeros_like(array, bool)])
    for seam in crossing:
        for offset_px in CONTROL_OFFSETS_PX:
            control = _offset_seam(seam, offset_px, grid)
            distance = grid.signed_distance_px(control)
            if not _crosses(distance):
                continue
            profile = seam_profile(array, distance, all_seams, control)
            metrics.append(seam_metrics(probe.name, profile, array, distance, control=True, relative=relative))
    distances_crossing = {s.name: distances[s.name] for s in crossing}
    return ProbeResult(
        grid, array, source_ids, distances_crossing, profiles, metrics, value_name, value_units, relative, shade
    )


def _crosses(distance_px: np.ndarray) -> bool:
    # Pixels within 1 px on both sides, not just both signs: a meridian's far-side wrap at +-180 deg
    # of longitude difference also flips sign.
    with np.errstate(invalid="ignore"):
        return bool(((distance_px >= -1) & (distance_px < 0)).any() and ((distance_px > 0) & (distance_px <= 1)).any())


def _offset_seam(seam: Seam, offset_px: int, grid: ProbeGrid) -> Seam:
    offset_deg = offset_px * grid.gsd_m / (math.pi * MOON_RADIUS_M / 180.0)
    if seam.kind == "lon":
        offset_deg /= math.cos(math.radians(grid.probe.center_lat_deg))
    return dataclasses.replace(seam, name=f"{seam.name} {offset_px:+d}px", value_deg=seam.value_deg + offset_deg)


def metrics_table(results: Sequence[ProbeResult]) -> pd.DataFrame:
    """Every probe's seam metrics as one table, one row per (probe, seam).

    :param results: `run_probe` outputs.
    :returns: The table.
    """
    return pd.DataFrame([dataclasses.asdict(m) for r in results for m in r.metrics])


@dataclasses.dataclass(frozen=True)
class SeamThresholds:
    """Pass limits for one seam's `SeamMetrics`.

    :ivar nan_near_max: Max `nan_near`.
    :ivar abs_step_max: Max |`step`|.
    :ivar spike_max: Max `spike`.
    :ivar gradient_ratio_max: Max `gradient_ratio`.
    """

    nan_near_max: int
    abs_step_max: float
    spike_max: float
    gradient_ratio_max: float


# Set from the control lines' spread across all of `WAC_EMP_PROBES` (max |step| 0.048, spike 0.035,
# gradient ratio 1.23), with some margin. `nan_near_max` leaves room for the archive's own small holes.
_WAC_EMP_DEFAULT_THRESHOLDS = SeamThresholds(
    nan_near_max=20, abs_step_max=0.06, spike_max=0.045, gradient_ratio_max=1.3
)
# The +-60 deg seams get a looser gradient limit than the controls' 1.3: 1.4 is where the one
# visually acceptable corner (60N 0E, 1.32) passes. `wac_emp_edge_correction` doesn't get most
# corners under it (see `docs/proposed-tasks/open-items.md`); `tests/test_reflectance_seams.py`
# lists those as expected failures.
_WAC_EMP_POLAR_SEAM_THRESHOLDS = dataclasses.replace(_WAC_EMP_DEFAULT_THRESHOLDS, gradient_ratio_max=1.4)


def wac_emp_thresholds(seam_name: str) -> SeamThresholds:
    """Pass limits for a `WAC_EMP_SEAMS` seam.

    :param seam_name: The seam's name.
    :returns: Its limits.
    """
    return _WAC_EMP_POLAR_SEAM_THRESHOLDS if seam_name in ("lat +60", "lat -60") else _WAC_EMP_DEFAULT_THRESHOLDS


# Set from the control lines' spread across `DEM_PROBES` on GLD100 (gradient ratio p95 1.16, max
# 1.51: terrain occasionally reaches a real seam's level, so this limit is a judgment call, not a
# clean separation). Step and spike get no limit: at 100 m posting terrain alone moves them by up to ~280/330 m
# on the controls, far more than any seam offset worth catching, so only the gradient ratio and
# `NaN` separate seams from terrain. `nan_near_max` leaves room for a lon seam crossing a lat seam's
# gap, which the lat seam already reports.
_DEM_THRESHOLDS = SeamThresholds(nan_near_max=20, abs_step_max=math.inf, spike_max=math.inf, gradient_ratio_max=1.35)


def dem_thresholds(seam_name: str) -> SeamThresholds:
    """Pass limits for a `DEM_SEAMS` seam.

    :param seam_name: The seam's name.
    :returns: Its limits.
    """
    return _DEM_THRESHOLDS


def threshold_violations(table: pd.DataFrame, thresholds: Callable[[str], SeamThresholds]) -> pd.DataFrame:
    """The real (non-control) seams in `table` that exceed their limits.

    :param table: `metrics_table`'s output.
    :param thresholds: Seam name -> limits, e.g. `wac_emp_thresholds`.
    :returns: The failing rows, with a `failed` column naming the metrics over their limit (empty if
        everything passes).
    """
    rows = []
    for _, row in table[~table.control].iterrows():
        limits = thresholds(row.seam)
        failed = [
            name
            for name, over in (
                ("nan_near", row.nan_near > limits.nan_near_max),
                ("step", not abs(row.step) <= limits.abs_step_max),
                ("spike", not row.spike <= limits.spike_max),
                ("gradient_ratio", not row.gradient_ratio <= limits.gradient_ratio_max),
            )
            if over
        ]
        if failed:
            rows.append({**row.to_dict(), "failed": ", ".join(failed)})
    return pd.DataFrame(rows, columns=[*table.columns, "failed"])


def _limit_fractions(row, limits: SeamThresholds) -> dict[str, float]:
    # Each metric as a fraction of its limit (1.0 = at the limit). `nan_near` uses limit + 1 so a
    # count of zero reads as 0 and any count over the limit exceeds 1.
    return {
        "nan_near": row.nan_near / (limits.nan_near_max + 1),
        "step": abs(row.step) / limits.abs_step_max,
        "spike": row.spike / limits.spike_max,
        "gradient_ratio": (row.gradient_ratio - 1) / (limits.gradient_ratio_max - 1),
    }


def probe_health_table(results: Sequence[ProbeResult], thresholds: Callable[[str], SeamThresholds]) -> pd.DataFrame:
    """One row per probe: its worst value of each metric over its seams, how close the closest metric
    is to its limit, and pass/fail. The heavy tests assert on `status`.

    :param results: `run_probe` outputs.
    :param thresholds: Seam name -> limits, e.g. `wac_emp_thresholds`.
    :returns: Columns `probe`, `tiles`, `nan_px`, `nan_near`, `max_abs_step`, `max_spike`,
        `max_gradient_ratio`, `limit_use` (the largest metric/limit fraction; `gradient_ratio` counts
        from 1, `nan_near` from 0), `limit_use_by` (`"<seam>: <metric>"` behind it), `status`
        (`"pass"`/`"FAIL"`) and `failed` (`threshold_violations`' failures, `"; "`-joined).
    """
    rows = []
    for result in results:
        table = metrics_table([result])
        real = table[~table.control]
        worst_use, worst_by = -math.inf, ""
        for _, row in real.iterrows():
            for metric, fraction in _limit_fractions(row, thresholds(row.seam)).items():
                use = math.inf if math.isnan(fraction) else fraction
                if use > worst_use:
                    worst_use, worst_by = use, f"{row.seam}: {metric}"
        violations = threshold_violations(table, thresholds)
        rows.append(
            {
                "probe": result.grid.probe.name,
                "tiles": len(result.source_ids),
                "nan_px": result.nan_count,
                "nan_near": int(real.nan_near.max()),
                "max_abs_step": float(real.step.abs().max()),
                "max_spike": float(real.spike.max()),
                "max_gradient_ratio": float(real.gradient_ratio.max()),
                "limit_use": worst_use,
                "limit_use_by": worst_by,
                "status": "pass" if violations.empty else "FAIL",
                "failed": "; ".join(f"{r.seam}: {r.failed}" for _, r in violations.iterrows()),
            }
        )
    return pd.DataFrame(rows)


# --- Sources and reports -------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SeamSource:
    """Everything needed to probe one source's seams.

    :ivar name: Registry key, e.g. `"reflectance"`; also the reports' and renders' folder name.
    :ivar seams: Where its tiles meet.
    :ivar probes: The AOIs to probe.
    :ivar renderer: Renders the source onto a probe grid.
    :ivar thresholds: Seam name -> pass limits.
    :ivar value_name: What the renderer produces, for labels.
    :ivar value_units: Its units.
    :ivar relative: Report `step`/`spike` relative to each probe's median (right for reflectance, not
        for elevation, whose median can be near zero).
    :ivar shade: Show renders as a hillshade (elevation) rather than directly.
    """

    name: str
    seams: tuple[Seam, ...]
    probes: tuple[SeamProbe, ...]
    renderer: Renderer
    thresholds: Callable[[str], SeamThresholds]
    value_name: str
    value_units: str
    relative: bool = True
    shade: bool = False


SOURCES: dict[str, SeamSource] = {
    "reflectance": SeamSource(
        "reflectance",
        WAC_EMP_SEAMS,
        WAC_EMP_PROBES,
        render_wac_emp_reflectance,
        wac_emp_thresholds,
        value_name="WAC_EMP reflectance",
        value_units="unitless",
    ),
    "dem_gld100": SeamSource(
        "dem_gld100",
        DEM_SEAMS,
        DEM_PROBES,
        dem_renderer("gld100"),
        dem_thresholds,
        value_name="GLD100 elevation, before hole fill",
        value_units="m",
        relative=False,
        shade=True,
    ),
    "dem_sldem2015_gld100_hardcut": SeamSource(
        "dem_sldem2015_gld100_hardcut",
        DEM_SEAMS,
        DEM_PROBES,
        dem_renderer("sldem2015_gld100_hardcut"),
        dem_thresholds,
        value_name="SLDEM2015 + GLD100 elevation, hard cut, before hole fill",
        value_units="m",
        relative=False,
        shade=True,
    ),
    "dem_sldem2015_gld100": SeamSource(
        "dem_sldem2015_gld100",
        DEM_SEAMS,
        DEM_PROBES,
        dem_renderer("sldem2015_gld100"),
        dem_thresholds,
        value_name="SLDEM2015 + GLD100 elevation, seam treated, before hole fill",
        value_units="m",
        relative=False,
        shade=True,
    ),
}
"""Every probed source, by name."""

_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "notebooks" / "seam_probe_template.py"
_NOTEBOOKS_DIR = _TEMPLATE_PATH.parent


def render_dir(source_name: str, config: TrntestConfig) -> Path:
    """Where `source_name`'s probe renders (GeoTIFFs) go: `<scratch>/seam_probes/<source>/`."""
    return config.scratch_dir / "seam_probes" / source_name


def report_dir(source_name: str, config: TrntestConfig) -> Path:
    """Where `source_name`'s per-probe report notebooks go: `<output>/seam_probes/<source>/`."""
    return config.output_dir / "seam_probes" / source_name


def run_source(source_name: str, config: TrntestConfig | None = None) -> list[ProbeResult]:
    """Run every probe of a registered source.

    :param source_name: A `SOURCES` key.
    :param config: Project config; defaults to `load_config()`.
    :returns: One result per probe, in `SeamSource.probes` order.
    """
    config = config or load_config()
    source = SOURCES[source_name]
    return [_run_source_probe(source, probe, config) for probe in source.probes]


def _run_source_probe(source: SeamSource, probe: SeamProbe, config: TrntestConfig) -> ProbeResult:
    return run_probe(
        probe,
        source.seams,
        source.renderer,
        config,
        render_dir(source.name, config),
        value_name=source.value_name,
        value_units=source.value_units,
        relative=source.relative,
        shade=source.shade,
    )


def load_probe(source_name: str, probe_name: str, config: TrntestConfig | None = None) -> ProbeResult:
    """Run one probe of a registered source (renders afresh; a few seconds).

    :param source_name: A `SOURCES` key.
    :param probe_name: One of that source's probe names.
    :param config: Project config; defaults to `load_config()`.
    :returns: The result.
    """
    config = config or load_config()
    source = SOURCES[source_name]
    probe = next(p for p in source.probes if p.name == probe_name)
    return _run_source_probe(source, probe, config)


def write_probe_reports(source_name: str, config: TrntestConfig | None = None) -> dict[str, Path]:
    """Render `notebooks/seam_probe_template.py` into one executed notebook per probe of a registered
    source, under `report_dir`. Runs in-process (call from inside the container).

    :param source_name: A `SOURCES` key.
    :param config: Project config; defaults to `load_config()`.
    :returns: Probe name -> report notebook path.
    """
    config = config or load_config()
    out_dir = report_dir(source_name, config)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for probe in SOURCES[source_name].probes:
        report_py, report_ipynb = out_dir / f"{probe.name}.py", out_dir / f"{probe.name}.ipynb"
        params = {"source": source_name, "probe_name": probe.name}
        report_py.write_text(render_template(_TEMPLATE_PATH.read_text(), params))
        run_quiet(["jupytext", "--to", "notebook", str(report_py), "--output", str(report_ipynb)])
        run_quiet(["papermill", str(report_ipynb), str(report_ipynb), "--cwd", str(out_dir), "--no-progress-bar"])
        report_py.unlink()
        paths[probe.name] = report_ipynb
    return paths


def report_links(paths: dict[str, Path]) -> dict[str, str]:
    """Report paths as links relative to `notebooks/`, where the index notebooks live.

    :param paths: `write_probe_reports`' output.
    :returns: Probe name -> relative link.
    """
    return {name: os.path.relpath(path, _NOTEBOOKS_DIR) for name, path in paths.items()}
