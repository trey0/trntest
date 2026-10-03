# ---
# jupyter:
#   jupytext:
#     formats: notebooks//ipynb,notebooks//py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.19.5
#   kernelspec:
#     display_name: Python 3 (ipykernel)
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Cast shadows: the sun-aligned sweep
#
# Per-facet shading (Lambertian or Hapke) darkens a slope that faces away from the Sun, but it can't
# darken a crater floor that faces the Sun while a rim between it and the Sun blocks the light. That
# is a *cast* shadow, and `hillshade` renders get it from `cast_shadow.illumination_fraction`: a
# per-pixel multiplier (`1` = fully lit, `0` = fully shadowed) applied on top of the per-facet shading
# (`hapke.despeckle_and_shade_ortho`'s `cast_shadows`, on by default).
#
# The method is a *sun-aligned sweep*. In a Cartesian frame with the Sun at infinity along `+x`
# (`cast_shadow.SunFrame`), every sun ray runs along the x-axis. The terrain is resampled onto a
# regular grid in that frame, so each grid column is an independent 1D problem: sweep from the
# sun-facing edge inward (in order of horizontal distance toward the Sun), track the tallest terrain
# seen so far (height measured perpendicular to the rays), and anything below it is in shadow. Each
# DEM pixel is then tested at four sub-pixel points against that running maximum, and the fraction
# lit becomes its value. `cast_shadow`'s module docstring and comments cover the details (true 3D
# positions rather than a flat plane, why the terrain is resampled rather than binned, streaming).
#
# This notebook runs the sweep on `M1327218454CE`, the lowest-sun candidate in the manifest (~13 deg
# elevation, so shadows are long), then compares it with two other views of the same scene: ISIS's
# own `shadow` application, and the real WAC image.

# %%
import numpy as np
import rasterio
import spiceypy as spice

import trntest
from trntest import cache, cast_shadow, dem_ortho, hapke, illumination, shadow_plotting, wac_resample
from trntest.config import MOON_RADIUS_M
from trntest.subprocess_utils import run_quiet

CANDIDATE_PRODUCT_ID = "M1327218454CE"
SELF_SHADOW_INCIDENCE_DEG = 90.0  # a facet at or beyond this incidence faces away from the Sun
CROP_ROWS, CROP_COLS = slice(500, 900), slice(800, 1200)
STRIP_ROWS, STRIP_COLS, STREAK_ROW = slice(990, 1050), slice(1350, 1800), 1016
ROWS_OF_INTEREST = {"row 1016 (primary streak)": 1016, "row 1023 (caching artifact)": 1023}

# %% [markdown]
# ## Candidate, DEM and Sun
#
# The DEM covers the synthetic camera's own footprint (row numbers below refer to this grid). The
# Sun direction comes straight from SPICE as a MOON_ME vector.

# %%
session = trntest.Session()
images = trntest.read_manifest("dataset_manifest.csv")
dataset = trntest.TrnTestDataSet.create(session.config.output_dir / "trn_dataset", images, session.config)
entry = dataset[CANDIDATE_PRODUCT_ID]
config, camera = entry.per_image_config, entry.camera
out_dir = config.scratch_dir / "sun_aligned_shadow_sweep"
out_dir.mkdir(parents=True, exist_ok=True)

dem_result = dem_ortho.fetch_dem(camera, config)
with rasterio.open(dem_result.dem) as src:
    dem = src.read(1).astype(np.float64)
    cellsize_m = src.res[0]
center = camera.footprint_lonlat_deg["center"]
sun_direction = illumination.sun_direction_moon_me(camera.et)
azimuth_deg, elevation_deg = illumination.sun_azimuth_elevation_deg(*center, camera.et)
print(f"{entry.edr_product}: DEM {dem.shape[1]}x{dem.shape[0]} at {cellsize_m:.1f} m/px")
print(f"Sun azimuth {azimuth_deg:.1f} deg, elevation {elevation_deg:.1f} deg")

# %% [markdown]
# ## The sweep
#
# `horizon_sweep` returns the illumination fraction plus a few diagnostics of the sun grid it swept.
# Only about half of that grid's nodes land on the DEM: the DEM's square footprint, rotated into the
# sun frame, fills only a diamond inside the grid's bounding box. Locating each node on the terrain is
# a small iterative solve (curvature tilts local vertical across the DEM); its worst leftover height
# error should stay within a centimeter.

# %%
sweep = cast_shadow.horizon_sweep(dem, dem_result.bbox, *center, sun_direction)
print(sweep.summary())

# %% [markdown]
# ## Hillshade x illumination
#
# The illumination fraction multiplies a per-facet hillshade directly. A plain Lambertian hillshade
# is used here so the figure isolates the shadows themselves from any texture.

# %%
hillshade = shadow_plotting.lambertian_hillshade(dem, azimuth_deg, elevation_deg, cellsize_m)
fig = shadow_plotting.plot_illumination_composite(
    hillshade,
    sweep.illumination_fraction,
    title=f"{entry.edr_product} -- sun az={azimuth_deg:.1f}/el={elevation_deg:.1f} deg -- sun-aligned sweep",
)
fig.savefig(out_dir / f"{entry.edr_product}_sweep.png", dpi=150)

# %% [markdown]
# ## Comparison with ISIS `shadow`
#
# ISIS's `shadow` application ray-marches from each DEM pixel toward the Sun -- a second, independent
# implementation of the same horizon test, with its own approximations (a hard lit/shadowed result,
# per-ray step size, and a shadow-map cache by default). Its output also folds self-shadow (facets
# facing away from the Sun) into the same mask, so for this comparison the sweep's fraction is
# combined with self-shadow from the per-facet incidence angle.
#
# The next cell prepares the DEM as an ISIS radius cube and runs `shadow` at this candidate's
# acquisition time. It skips the run if its output already exists.

# %%
isis_dir = out_dir / "isis_shadow"
isis_shadow_tif = isis_dir / f"{entry.edr_product}_shadow.tif"
if not isis_shadow_tif.exists():
    isis_dir.mkdir(parents=True, exist_ok=True)
    # `shadow` wants radius, not elevation, as the DEM's value.
    radius_tif, radius_cub = isis_dir / "dem_radius.tif", isis_dir / "dem_radius.cub"
    with rasterio.open(dem_result.dem) as src:
        profile = src.profile | {"dtype": "float32"}
        radius = (src.read(1).astype(np.float64) + MOON_RADIUS_M).astype(np.float32)
    with rasterio.open(radius_tif, "w", **profile) as dst:
        dst.write(radius, 1)
    run_quiet(["gdal_translate", "-of", "ISIS3", str(radius_tif), str(radius_cub)])
    # `gdal_translate` writes a valid Orthographic Mapping group but omits these three keywords, and
    # `demprep` fails without them.
    minx, _, maxx, maxy = dem_result.bbox
    for keyword, value in (
        ("PixelResolution", (maxx - minx) / dem_result.width),
        ("UpperLeftCornerX", minx),
        ("UpperLeftCornerY", maxy),
    ):
        editlab = ["editlab", f"from={radius_cub}", "options=addkey", "grpname=Mapping"]
        run_quiet([*editlab, f"keyword={keyword}", f"value={value}"])
    demprep_cub = isis_dir / "dem_radius.demprep.cub"
    run_quiet(["demprep", f"from={radius_cub}", f"to={demprep_cub}"])
    # The text PCK, not the binary `moon_pa` one: `shadow` needs the IAU_MOON frame, and its single
    # `PCK=` slot can't also take the frame kernels MOON_PA depends on (it crashes with the binary PCK).
    pck = cache.fetch_naif_kernel("data/pck/pck00010.tpc", cache_root=config.cache_root, base_url=config.naif_base_url)
    spk = cache.fetch_naif_kernel("data/spk/de421.bsp", cache_root=config.cache_root, base_url=config.naif_base_url)
    # Default `PRESET=BALANCED`. Its shadow-map caching produces some of the row streaks seen below
    # (`PRESET=ACCURATE` removes those, at a much higher CPU cost, but not all of them).
    shadow_cub = isis_dir / "dem_radius.shadow.cub"
    utc = spice.et2utc(camera.et, "ISOC", 3)
    shadow_args = ["sunpositionsource=time", f"time={utc}", f"pck={pck}", f"spk={spk}"]
    run_quiet(["shadow", f"from={demprep_cub}", f"to={shadow_cub}", *shadow_args])
    partial_tif = isis_dir / "shadow.partial.tif"
    run_quiet(["gdal_translate", str(shadow_cub), str(partial_tif)])
    partial_tif.rename(isis_shadow_tif)

# ISIS's shadowed/facing-away special pixel reads back as the GeoTIFF's own nodata.
with rasterio.open(isis_shadow_tif) as src:
    isis_lit = ~np.ma.getmaskarray(src.read(1, masked=True))

incidence_deg, _, _ = hapke.real_geometry_photometric_angles(
    dem, dem_result.bbox, camera, azimuth_deg, elevation_deg, cellsize_m
)
sweep_with_self_shadow = np.where(incidence_deg >= SELF_SHADOW_INCIDENCE_DEG, 0.0, sweep.illumination_fraction)
print(
    f"lit fraction -- ISIS shadow: {isis_lit.mean():.3f}, sweep incl. self-shadow: {np.nanmean(sweep_with_self_shadow):.3f}"
)

# %% [markdown]
# Both masks over the same hillshade crop. The shadows fall in the same places -- same sides of the
# same rims -- but ISIS's are consistently larger.

# %%
fig = shadow_plotting.plot_mask_crop_comparison(
    hillshade,
    isis_lit,
    sweep_with_self_shadow,
    CROP_ROWS,
    CROP_COLS,
    title=f"{entry.edr_product} -- same crop, same hillshade base -- rows 500-900, cols 800-1200",
)
fig.savefig(out_dir / f"{entry.edr_product}_crop_comparison.png", dpi=150)
print(
    f"crop lit fraction -- ISIS: {isis_lit[CROP_ROWS, CROP_COLS].mean():.3f}, "
    f"sweep: {np.nanmean(sweep_with_self_shadow[CROP_ROWS, CROP_COLS]):.3f}"
)

# %% [markdown]
# Row-mean shadow fraction across the whole DEM. ISIS's curve carries sharp single-row spikes --
# horizontal streaks in its mask -- that the sweep's curve does not. Rows 1016 and 1023 are two such
# streaks; each spans only part of the row, so the numbers below compare them with their neighbors
# over columns 1350-1800 rather than the full width.

# %%
fig = shadow_plotting.plot_row_mean_shadow_fraction(
    isis_lit,
    sweep_with_self_shadow,
    title=f"{entry.edr_product} -- row-mean shadow fraction, ISIS shadow vs. sun-aligned sweep",
)
fig.savefig(out_dir / f"{entry.edr_product}_row_mean_shadow_comparison.png", dpi=150)
for label, row in ROWS_OF_INTEREST.items():
    neighbors = [row - 1, row + 1]
    print(
        f"{label}: ISIS shadow frac={1 - isis_lit[row, STRIP_COLS].mean():.3f} "
        f"(neighbors {1 - isis_lit[neighbors, STRIP_COLS].mean():.3f}), "
        f"sweep shadow frac={1 - np.nanmean(sweep_with_self_shadow[row, STRIP_COLS]):.3f} "
        f"(neighbors {1 - np.nanmean(sweep_with_self_shadow[neighbors, STRIP_COLS]):.3f})"
    )

# %% [markdown]
# ## Comparison with the real WAC image
#
# The real WAC crop is what the sensor actually saw -- the closest thing to a reference for either
# shadow model. `wac_resample.crop_reflectance_on_dem_grid` resamples it pixel-for-pixel onto the DEM's
# grid. The strip below is at strict 1:1 pixel scale around row 1016, one of ISIS's streak rows
# (the one that survives `PRESET=ACCURATE`).

# %%
wac_on_dem_grid = wac_resample.crop_reflectance_on_dem_grid(entry.crop_result, dem_result, config)
fig = shadow_plotting.plot_strip_vs_wac(
    wac_on_dem_grid,
    hillshade,
    sweep.illumination_fraction,
    STRIP_ROWS,
    STRIP_COLS,
    STREAK_ROW,
    title=f"{entry.edr_product} -- rows 990-1050, cols 1350-1800 -- red line = row {STREAK_ROW}",
)
fig.savefig(out_dir / f"{entry.edr_product}_row{STREAK_ROW}_vs_real_wac.png", dpi=150)
wac_row = np.nanmean(wac_on_dem_grid[STREAK_ROW, STRIP_COLS])
wac_neighbors = np.nanmean(wac_on_dem_grid[[STREAK_ROW - 1, STREAK_ROW + 1], STRIP_COLS])
print(f"row {STREAK_ROW} real WAC brightness: {wac_row:.4f}, neighbors: {wac_neighbors:.4f}")

# %% [markdown]
# ## Open questions
#
# - **Extent.** The sweep and ISIS `shadow` agree on where shadows fall but not on how much area they
#   cover: ISIS marks roughly twice as many pixels shadowed. Neither is ground truth -- both work from
#   the same DEM with different approximations -- and the difference is unexplained.
# - **Streaks.** ISIS's mask has horizontal single-row streaks. Some come from its shadow-map caching
#   (`PRESET=ACCURATE` removes the row-1023 kind). Others, like row 1016, persist and even strengthen
#   under `ACCURATE`, yet appear in neither the sweep nor the real WAC image, and the DEM shows no
#   step there. These streaks are why ISIS `shadow` isn't used for rendering.
# - **DEM resolution.** Both models inherit GLD100's ~100 m posting, which smooths away small relief.
#   At a ~13 deg sun, a 20 m obstruction casts a shadow nearly a pixel long, so any DEM-based model
#   undercounts real shadow at low sun.
