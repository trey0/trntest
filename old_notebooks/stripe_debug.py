# ---
# jupyter:
#   jupytext:
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
# # Stripe artifact debug (throwaway diagnostic, not a tracked notebook pair)
#
# Focused repro for the open item in `../docs/plan.md`: "subtle stripe/crosshatch artifacts in the
# synthetic render, likely from `shade_ortho`'s hillshade step." Most visible in Phase 5B's *base
# layer* (`lunaserv_result.ortho`, the despeckled+hillshaded basemap `plotting.plot_overlay` shows
# under the mapprojected render) -- so this notebook skips straight to producing that same layer,
# without `dataset.select_dataset()`'s live catalog search. No `sat_sim` render, no ISIS/WAC crop,
# no mapproject, no tie points -- just `camera.build_camera` -> `lunaserv.fetch_dem_and_ortho`, plus
# a direct look at the raw `LightSource.hillshade` array *before* it's blended into the ortho (the
# first diagnostic step `docs/plan.md` suggests), and a couple of quantitative checks so we're not
# just eyeballing the image (see feedback: don't trust Claude's own visual read of subtle patterns
# -- inspect the actual arrays/numbers, and show the real images here for the user to judge).
#
# Product pinned to `M1327210646CE` (`LROLRC_0041B`/ESM4/doy 2019305, `target_frame_index=94`) --
# **not** `TrntestConfig`'s own built-in default (`M1329714703CE`, the older original single-demo
# product). That default is stale: the checked-in `lunar_sat_sim_demo.ipynb`'s actual last real run
# (`select_dataset()` -> `generate_dataset(limit=1)`, the live catalog-driven default path) picked
# `M1327210646CE` as row 0, and the user has since observed the stripe artifact against that more
# recent run -- so this notebook reproduces *that* exact image/frame, not the config default.

# %%
import dataclasses
import math

import matplotlib.pyplot as plt
import numpy as np
import rasterio
import rioxarray
from matplotlib.colors import LightSource
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import Resampling, reproject

from trntest import cache, camera as camera_mod
from trntest import illumination, lunaserv
from trntest.config import load_config

config = dataclasses.replace(
    load_config(),
    edr_volume="LROLRC_0041B",
    edr_subdir="ESM4",
    edr_doy="2019305",
    edr_product="M1327210646CE",
    cdr_volume="LROLRC_1041B",
    cdr_product="M1327210646CC",
    target_frame_index=94,
)
print(f"edr_product={config.edr_product} target_frame_index={config.target_frame_index}")


# %%
def plot_crop_grid(images: dict, gsd_m: float, vmin=0.0, vmax=1.0, ncols: int = 2, cmap: str = "gray"):
    """Grid of zoomed crops, capped at `ncols` columns -- more than 2 per row and matplotlib shrinks
    the rendered images below a resolution where fine crosshatching stays visible (see feedback: show
    real images the user can actually inspect, don't let display scaling defeat the point). Axes are
    labeled in real-world meters (crop shape * gsd_m), not pixel indices, so a stripe spacing read off
    the image directly gives its real wavelength."""
    n = len(images)
    nrows = math.ceil(n / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(7.5 * ncols, 7.5 * nrows), squeeze=False)
    for ax, (label, arr) in zip(axes.flat, images.items()):
        h, w = arr.shape
        extent = [0, w * gsd_m, h * gsd_m, 0]  # meters; top-left origin, matching imshow's row-major default
        ax.imshow(arr, cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest", extent=extent)
        ax.set_title(label, fontsize=11)
        ax.set_xlabel("east-west (m)")
        ax.set_ylabel("north-south (m)")
    for ax in list(axes.flat)[len(images) :]:
        ax.axis("off")
    fig.tight_layout()
    return fig


# %% [markdown]
# ## Build the camera pose and fetch DEM/ortho (Phases 2-3 only)

# %%
camera = camera_mod.build_camera(config)
print(f"footprint center (lon,lat): {camera.footprint_lonlat_deg['center']}")

# %%
lunaserv_result = lunaserv.fetch_dem_and_ortho(camera, config)
print(lunaserv_result)

# %% [markdown]
# ## Phase-5B-equivalent base layer
#
# Same display technique `plotting.plot_overlay` uses for its base panel: `rioxarray`, gray cmap,
# `vmin=0`/`vmax=`99.9th-percentile linear stretch -- reproduced directly here (not calling
# `plot_overlay` itself, since there's no overlay/mapproject in this notebook at all).

# %%
base = rioxarray.open_rasterio(lunaserv_result.ortho, masked=True).squeeze()
base_vmin, base_vmax = 0, np.nanpercentile(base.values, 99.9)

fig, ax = plt.subplots(figsize=(10, 10))
base.plot.imshow(ax=ax, cmap="gray", vmin=base_vmin, vmax=base_vmax, add_colorbar=False)
ax.set_title("Base layer (== Phase 5B's base panel): despeckled + hillshaded ortho")
fig.tight_layout()

# %% [markdown]
# ### Zoomed crop
#
# The stripes were reported as visible "on close zoom" -- a full-frame view at notebook resolution
# can hide them. Crop a quarter-frame region near the center and blow it up.

# %%
h, w = base.shape
y0, y1 = h * 3 // 8, h * 5 // 8
x0, x1 = w * 3 // 8, w * 5 // 8
zoom = base.values[y0:y1, x0:x1]

crop_extent_m = [0, (x1 - x0) * config.dem_target_gsd_m, (y1 - y0) * config.dem_target_gsd_m, 0]
fig, ax = plt.subplots(figsize=(9, 9))
im = ax.imshow(zoom, cmap="gray", vmin=base_vmin, vmax=base_vmax, interpolation="nearest", extent=crop_extent_m)
ax.set_title(f"Base layer, zoomed center crop [{y0}:{y1}, {x0}:{x1}] (nearest-neighbor, no smoothing)")
ax.set_xlabel("east-west (m)")
ax.set_ylabel("north-south (m)")
fig.colorbar(im, ax=ax, shrink=0.8)
fig.tight_layout()

# %% [markdown]
# ## Raw hillshade array, isolated (before blending onto the ortho)
#
# `docs/plan.md`'s suggested first diagnostic step: reproduce exactly what `lunaserv.shade_ortho`
# computes internally (`LightSource(azdeg=..., altdeg=...).hillshade(dem, dx=cellsize, dy=cellsize)`,
# same real-sun azimuth/elevation and same `config.dem_target_gsd_m` cellsize) and look at it on its
# own, with nothing else blended in.

# %%
with rasterio.open(lunaserv_result.dem) as src:
    dem = src.read(1)

center_lon, center_lat = camera.footprint_lonlat_deg["center"]
azimuth_deg, elevation_deg = illumination.sun_azimuth_elevation_deg(center_lon, center_lat, camera.et)
print(f"sun azimuth={azimuth_deg:.2f} deg, elevation={elevation_deg:.2f} deg")

light = LightSource(azdeg=azimuth_deg, altdeg=elevation_deg)
hillshade = light.hillshade(dem.astype(np.float64), dx=config.dem_target_gsd_m, dy=config.dem_target_gsd_m)

hh, hw = hillshade.shape
full_extent_m = [0, hw * config.dem_target_gsd_m, hh * config.dem_target_gsd_m, 0]
fig, axes = plt.subplots(1, 2, figsize=(16, 8))
im0 = axes[0].imshow(hillshade, cmap="gray", vmin=0, vmax=1, extent=full_extent_m)
axes[0].set_title("Raw hillshade array (full frame)")
axes[0].set_xlabel("east-west (m)")
axes[0].set_ylabel("north-south (m)")
fig.colorbar(im0, ax=axes[0], shrink=0.7)
im1 = axes[1].imshow(hillshade[y0:y1, x0:x1], cmap="gray", vmin=0, vmax=1, interpolation="nearest", extent=crop_extent_m)
axes[1].set_title("Raw hillshade array (same zoomed crop as above)")
axes[1].set_xlabel("east-west (m)")
axes[1].set_ylabel("north-south (m)")
fig.colorbar(im1, ax=axes[1], shrink=0.7)
fig.tight_layout()

# %% [markdown]
# ## Quantitative checks (not just a visual read)
#
# Real periodic/directional striping shows up as a bump in the frequency domain -- a much more
# reliable signal than eyeballing the image. But a naive "strongest off-DC FFT bins" search doesn't
# work: real terrain has a strongly low-frequency-dominated ("1/f-like") power spectrum on its own,
# with no artifact at all, so the very lowest-frequency bins next to DC always win regardless of any
# genuine periodic artifact -- they're just the bulk terrain shape/gradient, not a stripe signal. The
# real test is whether power at some *specific* frequency stands out **above the natural power-law
# trend**. `periodicity_report` below: windowed 2D FFT -> azimuthally-averaged radial power spectrum
# -> fit a power-law trend over the mid-frequency range -> report bins that most exceed that trend
# (candidate periodic-artifact frequencies) -> for the strongest one, power vs. angle around that
# frequency shell (one peak = one dominant stripe direction, two non-antipodal peaks = crosshatch,
# matching what the user described in at least one prior case).
#
# Run against **three** different arrays from this same crop, to localize where the periodicity
# actually originates: the raw DEM elevation (before any hillshade math at all), the raw hillshade
# (after `LightSource.hillshade`'s finite-differencing, before blending), and the raw fetched ortho
# (`docs/plan.md` already records the user confirming no stripes there -- included as a negative
# control/sanity check, not expected to show anything).


# %%
def periodicity_report(arr2d, gsd_m, label):
    crop = arr2d.astype(np.float64)
    crop = crop - crop.mean()
    window = np.outer(np.hanning(crop.shape[0]), np.hanning(crop.shape[1]))
    spectrum = np.fft.fftshift(np.fft.fft2(crop * window))
    magnitude = np.abs(spectrum)
    power = magnitude**2

    fig, ax = plt.subplots(figsize=(7, 7))
    im = ax.imshow(np.log1p(magnitude), cmap="inferno")
    ax.set_title(f"{label}: 2D FFT log-magnitude (DC at center)")
    fig.colorbar(im, ax=ax, shrink=0.8)
    fig.tight_layout()

    freqs_y = np.fft.fftshift(np.fft.fftfreq(crop.shape[0], d=1.0))  # cycles/pixel
    freqs_x = np.fft.fftshift(np.fft.fftfreq(crop.shape[1], d=1.0))
    fy, fx = np.meshgrid(freqs_y, freqs_x, indexing="ij")
    freq_radius = np.sqrt(fy**2 + fx**2)
    freq_angle_deg = np.degrees(np.arctan2(fy, fx))  # -180..180, 0 == along columns (x)

    n_radial_bins = crop.shape[0] // 2
    bin_edges = np.linspace(0, freq_radius.max(), n_radial_bins + 1)
    bin_idx = np.clip(np.digitize(freq_radius.ravel(), bin_edges) - 1, 0, n_radial_bins - 1)
    radial_power = np.array(
        [power.ravel()[bin_idx == i].mean() if np.any(bin_idx == i) else np.nan for i in range(n_radial_bins)]
    )
    bin_centers = 0.5 * (bin_edges[:-1] + bin_edges[1:])

    valid = np.isfinite(radial_power) & (bin_centers > 0) & (radial_power > 0)
    fit_range = valid & (bin_centers < 0.4 * freq_radius.max())
    log_f, log_p = np.log10(bin_centers[fit_range]), np.log10(radial_power[fit_range])
    slope, intercept = np.polyfit(log_f, log_p, 1)
    trend = 10 ** (intercept + slope * np.log10(bin_centers[valid]))
    residual_db = 10 * np.log10(radial_power[valid] / trend)
    valid_centers = bin_centers[valid]

    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.loglog(valid_centers, radial_power[valid], label="azimuthally-averaged power")
    ax.loglog(valid_centers, trend, "--", label=f"fitted power-law trend (slope={slope:.2f})")
    ax.set_xlabel("spatial frequency (cycles/pixel)")
    ax.set_ylabel("power")
    ax.set_title(f"{label}: radial power spectrum vs. natural trend")
    ax.legend()
    fig.tight_layout()

    top_residual_idx = np.argsort(residual_db)[::-1][:5]
    print(f"[{label}] frequency bins most above the fitted natural trend:")
    for i in top_residual_idx:
        f = valid_centers[i]
        period_px = 1.0 / f if f > 0 else float("inf")
        print(f"    freq={f:.4f} cyc/px  period={period_px:6.1f} px (~{period_px * gsd_m:8.0f} m)  +{residual_db[i]:5.1f} dB above trend")

    best_freq = valid_centers[top_residual_idx[0]]
    best_db = residual_db[top_residual_idx[0]]
    band_halfwidth = bin_edges[1] - bin_edges[0]
    in_shell = np.abs(freq_radius - best_freq) <= band_halfwidth
    shell_power = power[in_shell]
    shell_angle = freq_angle_deg[in_shell]

    angle_bins = np.linspace(-180, 180, 73)  # 5 deg bins
    angle_bin_idx = np.digitize(shell_angle, angle_bins) - 1
    angle_power = np.array(
        [shell_power[angle_bin_idx == i].mean() if np.any(angle_bin_idx == i) else 0.0 for i in range(len(angle_bins) - 1)]
    )
    angle_centers = 0.5 * (angle_bins[:-1] + angle_bins[1:])

    fig, ax = plt.subplots(figsize=(6, 6), subplot_kw={"projection": "polar"})
    ax.plot(np.radians(angle_centers), angle_power)
    ax.set_title(f"{label}: power vs. angle @ freq={best_freq:.4f} cyc/px")
    fig.tight_layout()

    sorted_angle_idx = np.argsort(angle_power)[::-1][:6]
    print(f"[{label}] top angle bins at freq={best_freq:.4f} cyc/px (period ~{1 / best_freq:.0f} px, ~{gsd_m / best_freq:.0f} m):")
    for i in sorted_angle_idx:
        print(f"    angle={angle_centers[i]:+7.1f} deg  power={angle_power[i]:.3g}")

    return best_freq, best_db


# %% [markdown]
# ### Raw DEM elevation (before any hillshade math)

# %%
_ = periodicity_report(dem[y0:y1, x0:x1], config.dem_target_gsd_m, "DEM elevation")

# %% [markdown]
# ### Raw hillshade array (after finite-differencing, before blending)

# %%
_ = periodicity_report(hillshade[y0:y1, x0:x1], config.dem_target_gsd_m, "hillshade")

# %% [markdown]
# ### Raw fetched ortho (negative control -- `docs/plan.md` records this as already-confirmed clean)

# %%
# lunaserv_result.ortho on disk is already despeckled+shaded -- re-fetch the pre-shade source
# directly (same cache `fetch_dem_and_ortho` already populated, so this is a free cache hit, not a
# second live network fetch).
raw_ortho_path = cache.fetch_lunaserv_getmap(
    config.lunaserv_ortho_layer,
    lunaserv_result.bbox,
    lunaserv_result.width,
    lunaserv_result.height,
    cache_root=config.cache_root,
    srs=config.lunaserv_srs_template.format(c_lon=center_lon, c_lat=center_lat),
    base_url=config.lunaserv_base_url,
    fmt="image/tiff",
)
with rasterio.open(raw_ortho_path) as src:
    raw_ortho = src.read(1)
_ = periodicity_report(raw_ortho[y0:y1, x0:x1], config.dem_target_gsd_m, "raw ortho (pre-shade)")

# %% [markdown]
# ### Does the artifact's real-world wavelength stay fixed as `dem_target_gsd_m` changes?
#
# Same bbox (`lunaserv_result.bbox`, gsd-independent -- computed from the camera's real ground
# footprint before any resolution choice), fetched again at two other DEM resolutions -- one finer
# (50 m/px, oversampling relative to GLD100's native ~100 m posting) and one coarser (300 m/px, a
# real 3x downsample). DEM-only (`cache.fetch_lunaserv_getmap` + `radius_to_elevation`, skipping
# `hole_fill_dem` -- Phase 15 in `docs/history.md` found it changes 0 pixels for this kind of
# non-polar crop -- and skipping the ortho fetch entirely), not the full `fetch_dem_and_ortho`: that
# also re-fetches the (much larger, unneeded here) ortho layer, which timed out requesting it at 2x
# linear resolution. If the artifact is a genuine feature of the *source* DTM grid (e.g. a WMS
# resampling/aliasing artifact tied to Lunaserv's own native grid, independent of what resolution we
# happen to request), its real-world wavelength (period_px * gsd_m) should come out ~the same at
# each resolution. If instead it's tied to *our own* request pixel grid, the wavelength in meters
# should scale with `dem_target_gsd_m` (i.e. stay fixed in *pixels*, not meters).

# %%
srs = config.lunaserv_srs_template.format(c_lon=center_lon, c_lat=center_lat)
gsd_variants = {}
for gsd_m in (50.0, 300.0):
    variant_width, variant_height = lunaserv.pixel_dims_for_gsd(lunaserv_result.bbox, gsd_m)
    dem_radius_path = cache.fetch_lunaserv_getmap(
        "luna_wac_dtm_numeric_meters_absolute",
        lunaserv_result.bbox,
        variant_width,
        variant_height,
        cache_root=config.cache_root,
        srs=srs,
        base_url=config.lunaserv_base_url,
        fmt="image/tiff; mode=32bit",
    )
    variant_elevation_path = config.output_dir / f"dem_elevation_{gsd_m:.0f}m.tif"
    lunaserv.radius_to_elevation(dem_radius_path, variant_elevation_path, config.moon_radius_m)
    with rasterio.open(variant_elevation_path) as src:
        variant_dem = src.read(1)
    vh, vw = variant_dem.shape
    vy0, vy1 = vh * 3 // 8, vh * 5 // 8
    vx0, vx1 = vw * 3 // 8, vw * 5 // 8
    gsd_variants[gsd_m] = (variant_dem, vy0, vy1, vx0, vx1)
    print(f"--- dem_target_gsd_m={gsd_m:.0f}: array shape {variant_dem.shape}, crop [{vy0}:{vy1}, {vx0}:{vx1}] ---")
    periodicity_report(variant_dem[vy0:vy1, vx0:vx1], gsd_m, f"DEM elevation @ {gsd_m:.0f} m/px")

# %% [markdown]
# ### DEM float32 quantization check
#
# `docs/plan.md`'s leading (unconfirmed) theory: Lunaserv's DTM layer's float32 encoding has a real
# ~0.125m ULP quantization step at this DEM's ~1.7e6 m radius magnitude, and `hillshade()`'s
# finite-differencing amplifies it. Check directly: histogram the DEM's nonzero elevation deltas
# between horizontally/vertically adjacent pixels, and see whether the values cluster on a ~0.125m
# grid rather than varying continuously.

# %%
dx_diffs = np.diff(dem, axis=1).ravel()
dy_diffs = np.diff(dem, axis=0).ravel()
print(f"DEM dtype: {dem.dtype}")
print(f"Row-wise diff: min={dx_diffs.min():.4f} max={dx_diffs.max():.4f} std={dx_diffs.std():.4f}")
print(f"Col-wise diff: min={dy_diffs.min():.4f} max={dy_diffs.max():.4f} std={dy_diffs.std():.4f}")

# Unique step sizes among the smallest nonzero |diffs| -- if quantization dominates, these should
# cluster near multiples of ~0.125m.
small_diffs = np.abs(dx_diffs[(dx_diffs != 0) & (np.abs(dx_diffs) < 1.0)])
if small_diffs.size:
    print(f"Smallest-magnitude nonzero row diffs (<1m), sample of unique values: {np.unique(np.round(small_diffs, 4))[:20]}")
else:
    print("No sub-1m row diffs found -- DEM has no near-flat regions at this crop, quantization test inconclusive here.")

fig, ax = plt.subplots(figsize=(8, 4))
ax.hist(dx_diffs, bins=200)
ax.set_title("Histogram of horizontally-adjacent DEM elevation differences")
ax.set_xlabel("elevation delta (m)")
fig.tight_layout()

# %% [markdown]
# ### DEM itself, and its raw finite-difference slope images (both axes)
#
# If DEM-grid-aligned artifacts are the cause, they should already be visible directly in these
# slope arrays, in DEM/map space -- before any hillshade nonlinearity or `sat_sim` reprojection.

# %%
slope_x = np.diff(dem, axis=1)
slope_y = np.diff(dem, axis=0)

# Capped at 2 columns (see feedback on subplot width) -- heterogeneous colormaps/units per panel, so
# plotted directly rather than via `plot_crop_grid`.
fig, axes = plt.subplots(2, 2, figsize=(15, 15), squeeze=False)
im0 = axes[0][0].imshow(dem[y0:y1, x0:x1], cmap="terrain", extent=[0, (x1 - x0) * config.dem_target_gsd_m, (y1 - y0) * config.dem_target_gsd_m, 0])
axes[0][0].set_title("DEM elevation (zoomed crop)")
fig.colorbar(im0, ax=axes[0][0], shrink=0.6)
im1 = axes[0][1].imshow(slope_x[y0:y1, x0 : x1 - 1], cmap="RdBu", extent=[0, (x1 - 1 - x0) * config.dem_target_gsd_m, (y1 - y0) * config.dem_target_gsd_m, 0])
axes[0][1].set_title("d(elevation)/dx (zoomed crop)")
fig.colorbar(im1, ax=axes[0][1], shrink=0.6)
im2 = axes[1][0].imshow(slope_y[y0 : y1 - 1, x0:x1], cmap="RdBu", extent=[0, (x1 - x0) * config.dem_target_gsd_m, (y1 - 1 - y0) * config.dem_target_gsd_m, 0])
axes[1][0].set_title("d(elevation)/dy (zoomed crop)")
fig.colorbar(im2, ax=axes[1][0], shrink=0.6)
for ax in (axes[0][0], axes[0][1], axes[1][0]):
    ax.set_xlabel("east-west (m)")
    ax.set_ylabel("north-south (m)")
axes[1][1].axis("off")
fig.tight_layout()

# %% [markdown]
# ## Follow-up: residual axis-aligned crosshatch after the native-fetch+reproject fix
#
# User-reported, from visually inspecting this same notebook's zoomed hillshade crop (see feedback:
# don't trust Claude's own visual read of subtle patterns -- inspect the actual arrays/numbers, and
# show real images for the user to judge): a *different*-looking crosshatch remains after the
# native-CRS-fetch + local-reproject fix -- axis-aligned to the image, straight lines, regular
# spacing (unlike the old artifact, which wasn't aligned to the final image's own axes and looked
# slightly curved). Leading theory: `lunaserv.reproject_dem_to_local_grid`'s `Resampling.cubic`
# upsamples ~2.4x from the native ~237 m/px grid to the 100 m/px working grid -- cubic spline
# reconstruction is a known source of small periodic curvature ripples at the source sample spacing,
# and `hillshade`'s finite-differencing (sensitive to *slope*, i.e. the reconstruction's derivative)
# would visually amplify even a subtle ripple invisible in the raw elevation. Since the native
# geographic grid is nearly axis-aligned with the local Orthographic grid near the tangent point, that
# ripple would appear axis-aligned and regularly spaced in the final image -- matching what was
# reported. Test directly: compare resampling methods for the same reprojection, and a post-reproject
# smoothing filter, against the actual periodicity numbers, not just a fresh visual guess.

# %%
native_path, native_bbox_deg, native_width, native_height = lunaserv.fetch_dem_native(camera, config)
native_minlon, native_minlat, native_maxlon, native_maxlat = native_bbox_deg
# Rough average m/px for axis labeling only (not used for any actual reprojection math).
native_gsd_m_approx = (
    (native_maxlon - native_minlon) / native_width + (native_maxlat - native_minlat) / native_height
) / 2 * (config.moon_radius_m * math.pi / 180.0)
print(f"native fetch: {native_width}x{native_height} px, approx native gsd ~{native_gsd_m_approx:.0f} m/px")

with rasterio.open(native_path) as src:
    native_radius = src.read(1)
nh, nw = native_radius.shape
ny0, ny1 = nh * 3 // 8, nh * 5 // 8
nx0, nx1 = nw * 3 // 8, nw * 5 // 8
_ = periodicity_report(
    native_radius[ny0:ny1, nx0:nx1].astype(np.float64), native_gsd_m_approx, "raw native DEM tile (pre-reproject)"
)

# %% [markdown]
# ### Compare resampling methods for the local reprojection step

# %%
resampling_methods = {
    "cubic (current default)": Resampling.cubic,
    "bilinear": Resampling.bilinear,
    "nearest": Resampling.nearest,
}
variant_hillshades = {}
for label, method in resampling_methods.items():
    variant_radius_path = config.output_dir / f"dem_radius_reprojected_{method.name}.tif"
    lunaserv.reproject_dem_to_local_grid(
        native_path,
        native_bbox_deg,
        native_width,
        native_height,
        lunaserv_result.bbox,
        lunaserv_result.width,
        lunaserv_result.height,
        center_lon,
        center_lat,
        config.moon_radius_m,
        variant_radius_path,
        resampling=method,
    )
    with rasterio.open(variant_radius_path) as src:
        variant_radius = src.read(1)
    variant_elevation = variant_radius.astype(np.float64) - config.moon_radius_m
    variant_hillshade = LightSource(azdeg=azimuth_deg, altdeg=elevation_deg).hillshade(
        variant_elevation, dx=config.dem_target_gsd_m, dy=config.dem_target_gsd_m
    )
    variant_hillshades[label] = variant_hillshade
    print(f"--- resampling={label} ---")
    periodicity_report(variant_hillshade[y0:y1, x0:x1], config.dem_target_gsd_m, f"hillshade ({label})")

# %%
plot_crop_grid({label: hs[y0:y1, x0:x1] for label, hs in variant_hillshades.items()}, config.dem_target_gsd_m)

# %% [markdown]
# ### Candidate fix: a post-reprojection smoothing filter
#
# Since the DEM's real information content already caps out at ~237 m/px (the native resolution
# ceiling this whole investigation established), smoothing the *reprojected* elevation at a scale
# comparable to the native/working GSD ratio (~2.4 working-grid pixels) shouldn't discard any real
# detail -- only numerical reconstruction ripple below that scale. A small dependency-free separable
# Gaussian (`scipy` isn't in this project's dependencies -- see `pyproject.toml` -- so implemented
# directly with `numpy.convolve`, throwaway-diagnostic-only; a real fix would just add `scipy` or use
# an existing GDAL/rasterio smoothing primitive) applied to the cubic-reprojected elevation, before
# `hillshade`.

# %%
def gaussian_blur_2d(arr: np.ndarray, sigma_px: float) -> np.ndarray:
    radius = max(1, int(3 * sigma_px))
    x = np.arange(-radius, radius + 1)
    kernel = np.exp(-(x**2) / (2 * sigma_px**2))
    kernel /= kernel.sum()
    blurred = np.apply_along_axis(lambda m: np.convolve(m, kernel, mode="same"), axis=1, arr=arr)
    blurred = np.apply_along_axis(lambda m: np.convolve(m, kernel, mode="same"), axis=0, arr=blurred)
    return blurred


with rasterio.open(config.output_dir / "dem_radius_reprojected_cubic.tif") as src:
    cubic_elevation = src.read(1).astype(np.float64) - config.moon_radius_m

blur_hillshades = {}
for sigma_px in (1.0, 1.5, 2.0):
    blurred_elevation = gaussian_blur_2d(cubic_elevation, sigma_px)
    blurred_hillshade = LightSource(azdeg=azimuth_deg, altdeg=elevation_deg).hillshade(
        blurred_elevation, dx=config.dem_target_gsd_m, dy=config.dem_target_gsd_m
    )
    label = f"cubic + Gaussian blur sigma={sigma_px}px"
    blur_hillshades[label] = blurred_hillshade
    print(f"--- {label} ---")
    periodicity_report(blurred_hillshade[y0:y1, x0:x1], config.dem_target_gsd_m, label)

# %%
plot_crop_grid({label: hs[y0:y1, x0:x1] for label, hs in blur_hillshades.items()}, config.dem_target_gsd_m)

# %% [markdown]
# ### A fairer cross-variant comparison: absolute power at a fixed frequency
#
# The blur-sigma numbers above are misleading to compare against each other directly:
# `periodicity_report`'s "dB above trend" is measured against a power-law trend **re-fit separately
# for each variant**, and Gaussian blur reshapes that whole trend (it suppresses high frequencies far
# faster than any power law would), not just the target artifact -- so "more dB above its own
# re-fit trend" doesn't necessarily mean "more absolute artifact power." Compare instead the raw,
# un-normalized power at one fixed frequency (the un-blurred cubic result's own flagged artifact,
# ~0.205 cyc/px / ~490 m) across every variant, same crop/window each time -- directly comparable.

# %%
def absolute_power_at_freq(arr2d, target_freq_cyc_px, band_frac=0.1):
    crop = arr2d.astype(np.float64)
    crop = crop - crop.mean()
    window = np.outer(np.hanning(crop.shape[0]), np.hanning(crop.shape[1]))
    spectrum = np.fft.fftshift(np.fft.fft2(crop * window))
    power = np.abs(spectrum) ** 2
    freqs_y = np.fft.fftshift(np.fft.fftfreq(crop.shape[0], d=1.0))
    freqs_x = np.fft.fftshift(np.fft.fftfreq(crop.shape[1], d=1.0))
    fy, fx = np.meshgrid(freqs_y, freqs_x, indexing="ij")
    freq_radius = np.sqrt(fy**2 + fx**2)
    band = np.abs(freq_radius - target_freq_cyc_px) <= band_frac * target_freq_cyc_px
    return power[band].mean()


def db_above_trend_at_freq(arr2d, target_freq_cyc_px):
    """Like `absolute_power_at_freq`, but relative to a fitted natural power-law trend (same
    methodology as `periodicity_report`) rather than raw power -- unit-invariant (a uniform amplitude
    rescaling of `arr2d` shifts both the measured power and the fitted trend by the same factor, so
    their ratio is unchanged), so this is the metric to use when comparing *different* quantities on
    different physical scales (e.g. raw elevation in meters vs. a bounded [0,1] hillshade), unlike
    `absolute_power_at_freq`'s raw power, which is only a fair comparison between variants of the same
    quantity already on the same scale."""
    crop = arr2d.astype(np.float64)
    crop = crop - crop.mean()
    window = np.outer(np.hanning(crop.shape[0]), np.hanning(crop.shape[1]))
    spectrum = np.fft.fftshift(np.fft.fft2(crop * window))
    power = np.abs(spectrum) ** 2
    freqs_y = np.fft.fftshift(np.fft.fftfreq(crop.shape[0], d=1.0))
    freqs_x = np.fft.fftshift(np.fft.fftfreq(crop.shape[1], d=1.0))
    fy, fx = np.meshgrid(freqs_y, freqs_x, indexing="ij")
    freq_radius = np.sqrt(fy**2 + fx**2)
    n_bins = crop.shape[0] // 2
    bin_edges = np.linspace(0, freq_radius.max(), n_bins + 1)
    bin_idx = np.clip(np.digitize(freq_radius.ravel(), bin_edges) - 1, 0, n_bins - 1)
    radial_power = np.array(
        [power.ravel()[bin_idx == i].mean() if np.any(bin_idx == i) else np.nan for i in range(n_bins)]
    )
    bin_centers = 0.5 * (bin_edges[:-1] + bin_edges[1:])
    valid = np.isfinite(radial_power) & (bin_centers > 0) & (radial_power > 0)
    fit_range = valid & (bin_centers < 0.4 * freq_radius.max())
    log_f, log_p = np.log10(bin_centers[fit_range]), np.log10(radial_power[fit_range])
    slope, intercept = np.polyfit(log_f, log_p, 1)
    trend_at_target = 10 ** (intercept + slope * np.log10(target_freq_cyc_px))
    measured = absolute_power_at_freq(arr2d, target_freq_cyc_px)
    return 10 * np.log10(measured / trend_at_target)


target_freq = 0.2047  # cubic reprojection's own flagged artifact frequency, cyc/px
print(f"Absolute power near freq={target_freq} cyc/px (period ~{1 / target_freq:.1f}px, ~{config.dem_target_gsd_m / target_freq:.0f}m):")
for label, hs in {**variant_hillshades, **blur_hillshades}.items():
    power = absolute_power_at_freq(hs[y0:y1, x0:x1], target_freq)
    print(f"  {label:40s} power={power:12.2f}")

# %% [markdown]
# A blur sigma<=2px (FWHM<=~470m) has a cutoff well above ~950m -- if a separate, coarser periodic
# component exists at that scale, blurring wouldn't touch it at all, and it would just become
# relatively more visible once the finer ~489m component above is suppressed. Check directly: each
# blur variant's own top-ranked frequency (from the per-variant report above) was consistently near
# 0.105 cyc/px (~952m) -- track *that* frequency's absolute power across all variants too.

# %%
target_freq_2 = 0.1050  # the blur variants' own consistently-top-ranked frequency, cyc/px
print(f"Absolute power near freq={target_freq_2} cyc/px (period ~{1 / target_freq_2:.1f}px, ~{config.dem_target_gsd_m / target_freq_2:.0f}m):")
for label, hs in {**variant_hillshades, **blur_hillshades}.items():
    power = absolute_power_at_freq(hs[y0:y1, x0:x1], target_freq_2)
    print(f"  {label:40s} power={power:12.2f}")

# %% [markdown]
# ## Testing option A: compute hillshade near native resolution, reproject the result
#
# `LightSource.hillshade()` (see its own source) just computes `np.gradient` on the elevation array
# for a per-pixel unit surface normal, then calls `LightSource.shade_normals(normal, fraction)` -- a
# per-pixel dot product against the sun direction, no further differentiation. So instead of
# differentiating an already->2x-upsampled elevation array (amplifying any reconstruction ripple, per
# the finding above -- that's the mechanism, not just a coincidence), compute the normals/hillshade on
# an intermediate grid near native resolution first, then reproject *that* (a smooth, bounded [0,1]
# scalar field, not raw elevation) up to the full working resolution -- no further derivative is taken
# after that upsample, so residual resampling ripple there isn't slope-amplified the same way.
#
# The intermediate grid still has to be the local Orthographic CRS, not the raw native lon/lat grid
# directly -- the native grid's degree-pixels are anisotropic (a real east-west meter/pixel that
# shrinks by cos(lat) relative to north-south, the same reason this whole project fetches DEM/ortho in
# a local Orthographic CRS to begin with -- see `fetch_dem_and_ortho`'s docstring), so a single scalar
# `dx`/`dy` into `LightSource.hillshade` would be physically wrong there. Reprojecting to an
# intermediate *local* grid at ~native resolution keeps pixels genuinely isotropic (a valid scalar
# `dx=dy=cellsize`) while barely upsampling at all (minimal new ringing, unlike the direct 2.4x jump
# straight to the 100 m/px working grid).

# %% [markdown]
# ### Is the ~952m component itself already present in the raw native tile?
#
# The native-tile check above only reported its own top-5 bins, dominated by a *different*, finer
# ~520-626m signal -- that doesn't rule out a real, weaker signal at other frequencies. Check the
# native array directly (in native pixel units) at the frequency equivalent to a 952m real-world
# period -- if it's there too, no amount of cleverness in *our* reprojection/differencing pipeline
# should be expected to remove it, since it would already be present in the actual elevation values
# Lunaserv serves. Raw elevation (meters) and a bounded [0,1] hillshade are on completely different
# physical scales, so use `db_above_trend_at_freq` (unit-invariant) here, not raw
# `absolute_power_at_freq` (only fair between variants already on the same scale).

# %%
native_freq_952m = native_gsd_m_approx / 952.0  # cyc/native-px equivalent to a 952m real-world period
native_db_952m = db_above_trend_at_freq(native_radius[ny0:ny1, nx0:nx1].astype(np.float64), native_freq_952m)
print(f"raw native tile: {native_db_952m:+.1f} dB above trend near freq={native_freq_952m:.4f} cyc/native-px (~952m)")
print(f"(compare: cubic hillshade {db_above_trend_at_freq(variant_hillshades['cubic (current default)'][y0:y1, x0:x1], target_freq_2):+.1f} dB, "
      f"raw ortho negative control {db_above_trend_at_freq(raw_ortho[y0:y1, x0:x1], target_freq_2):+.1f} dB at the same 952m wavelength)")

# %%
intermediate_gsd_m = native_gsd_m_approx
intermediate_width, intermediate_height = lunaserv.pixel_dims_for_gsd(lunaserv_result.bbox, intermediate_gsd_m)
print(f"intermediate grid: {intermediate_width}x{intermediate_height} px (~{intermediate_gsd_m:.0f} m/px)")

intermediate_radius_path = config.output_dir / "dem_radius_intermediate.tif"
lunaserv.reproject_dem_to_local_grid(
    native_path,
    native_bbox_deg,
    native_width,
    native_height,
    lunaserv_result.bbox,
    intermediate_width,
    intermediate_height,
    center_lon,
    center_lat,
    config.moon_radius_m,
    intermediate_radius_path,
    resampling=Resampling.cubic,
)
with rasterio.open(intermediate_radius_path) as src:
    intermediate_elevation = src.read(1).astype(np.float64) - config.moon_radius_m

intermediate_hillshade = LightSource(azdeg=azimuth_deg, altdeg=elevation_deg).hillshade(
    intermediate_elevation, dx=intermediate_gsd_m, dy=intermediate_gsd_m
)

# Upsample the smooth hillshade *scalar* (not elevation) to the full working grid -- same local CRS
# both times, only the resolution changes.
local_crs = f"+proj=ortho +lon_0={center_lon} +lat_0={center_lat} +R={config.moon_radius_m} +units=m +no_defs"
intermediate_transform = transform_from_bounds(*lunaserv_result.bbox, intermediate_width, intermediate_height)
working_transform = transform_from_bounds(*lunaserv_result.bbox, lunaserv_result.width, lunaserv_result.height)

gradient_first_hillshade = np.full((lunaserv_result.height, lunaserv_result.width), np.nan, dtype="float64")
reproject(
    source=intermediate_hillshade,
    destination=gradient_first_hillshade,
    src_transform=intermediate_transform,
    src_crs=local_crs,
    dst_transform=working_transform,
    dst_crs=local_crs,
    resampling=Resampling.cubic,
)

print("--- gradient-then-reproject (Option A) ---")
periodicity_report(gradient_first_hillshade[y0:y1, x0:x1], config.dem_target_gsd_m, "gradient-then-reproject")
p489 = absolute_power_at_freq(gradient_first_hillshade[y0:y1, x0:x1], target_freq)
p952 = absolute_power_at_freq(gradient_first_hillshade[y0:y1, x0:x1], target_freq_2)
print(f"power@489m={p489:.2f}  power@952m={p952:.2f}  (compare to cubic (current default): {absolute_power_at_freq(variant_hillshades['cubic (current default)'][y0:y1, x0:x1], target_freq):.2f} / {absolute_power_at_freq(variant_hillshades['cubic (current default)'][y0:y1, x0:x1], target_freq_2):.2f})")

# %%
plot_crop_grid(
    {
        "Current: reproject elevation, then hillshade": variant_hillshades["cubic (current default)"][y0:y1, x0:x1],
        "Option A: hillshade near-native, then reproject": gradient_first_hillshade[y0:y1, x0:x1],
    },
    config.dem_target_gsd_m,
)

# %% [markdown]
# ### Option A's own final-upsample step: does the kernel choice matter there too?
#
# The ~952m component only dropped ~2.3x under Option A, much less than the ~370x drop for the finer
# 489m one -- plausibly because the *final* upsample step (intermediate hillshade -> working grid)
# still spans the same ~2.4x ratio as before, just applied to a smoother, bounded scalar instead of
# raw elevation. Try `Resampling.bilinear` for that specific step instead of `cubic`.

# %%
gradient_first_bilinear = np.full((lunaserv_result.height, lunaserv_result.width), np.nan, dtype="float64")
reproject(
    source=intermediate_hillshade,
    destination=gradient_first_bilinear,
    src_transform=intermediate_transform,
    src_crs=local_crs,
    dst_transform=working_transform,
    dst_crs=local_crs,
    resampling=Resampling.bilinear,
)
print("--- gradient-then-reproject, bilinear final upsample ---")
periodicity_report(gradient_first_bilinear[y0:y1, x0:x1], config.dem_target_gsd_m, "gradient-then-reproject (bilinear)")
p489b = absolute_power_at_freq(gradient_first_bilinear[y0:y1, x0:x1], target_freq)
p952b = absolute_power_at_freq(gradient_first_bilinear[y0:y1, x0:x1], target_freq_2)
print(f"power@489m={p489b:.2f}  power@952m={p952b:.2f}")

# %% [markdown]
# ## Testing option B: oversample to a finer intermediate grid, then properly anti-aliased downsample
#
# A genuine two-stage multi-rate resampling: reproject straight from native to a grid 4x finer than
# the working resolution (real signal-processing practice -- any upsample-order ringing this
# introduces lands near *that* finer grid's own Nyquist, well above the working grid's), then
# decimate down to the working resolution with `Resampling.average` (real block-area anti-aliasing,
# not a reconstruction kernel) -- then compute hillshade once, at working resolution, same
# architecture as today, just fed a better-conditioned elevation array. Local CPU/memory cost only,
# no extra network fetch (`fetch_dem_native` isn't repeated).

# %%
oversample_factor = 4
fine_width, fine_height = lunaserv_result.width * oversample_factor, lunaserv_result.height * oversample_factor

fine_radius_path = config.output_dir / "dem_radius_fine_4x.tif"
lunaserv.reproject_dem_to_local_grid(
    native_path,
    native_bbox_deg,
    native_width,
    native_height,
    lunaserv_result.bbox,
    fine_width,
    fine_height,
    center_lon,
    center_lat,
    config.moon_radius_m,
    fine_radius_path,
    resampling=Resampling.cubic,
)

fine_transform = transform_from_bounds(*lunaserv_result.bbox, fine_width, fine_height)
downsampled_radius = np.full((lunaserv_result.height, lunaserv_result.width), np.nan, dtype="float32")
with rasterio.open(fine_radius_path) as src:
    fine_radius = src.read(1)
reproject(
    source=fine_radius,
    destination=downsampled_radius,
    src_transform=fine_transform,
    src_crs=local_crs,
    dst_transform=working_transform,
    dst_crs=local_crs,
    resampling=Resampling.average,
)
downsampled_elevation = downsampled_radius.astype(np.float64) - config.moon_radius_m
oversample_hillshade = LightSource(azdeg=azimuth_deg, altdeg=elevation_deg).hillshade(
    downsampled_elevation, dx=config.dem_target_gsd_m, dy=config.dem_target_gsd_m
)

print("--- 4x oversample + Resampling.average downsample (Option B) ---")
periodicity_report(oversample_hillshade[y0:y1, x0:x1], config.dem_target_gsd_m, "4x oversample + average downsample")
p489c = absolute_power_at_freq(oversample_hillshade[y0:y1, x0:x1], target_freq)
p952c = absolute_power_at_freq(oversample_hillshade[y0:y1, x0:x1], target_freq_2)
print(f"power@489m={p489c:.2f}  power@952m={p952c:.2f}")

# %%
plot_crop_grid(
    {
        "Current: reproject elevation, then hillshade": variant_hillshades["cubic (current default)"][y0:y1, x0:x1],
        "Option A (bilinear final upsample)": gradient_first_bilinear[y0:y1, x0:x1],
        "Option B: 4x oversample + average downsample": oversample_hillshade[y0:y1, x0:x1],
    },
    config.dem_target_gsd_m,
)

# %% [markdown]
# ## Combining A and B -- applied where decimation actually applies
#
# Option A (bilinear final upsample) still leaves the ~952m component at 78.1 (vs. baseline 242.1).
# User's idea: combine with Option B. But Option B applied to *Option A's own final step*
# (intermediate hillshade -> working grid) doesn't mechanistically make sense -- that step is an
# *upsample* (237m -> 100m, more samples out than in), and decimation/anti-aliasing is specifically a
# downsampling concept; there's nothing to "properly decimate" when creating new samples. There *is*
# a genuine downsample earlier in the chain, though: Lunaserv's own native 128 ppd tile already has a
# real near-Nyquist artifact baked in (~520-626m period -- measured directly on the raw native fetch,
# independent of anything this pipeline does), and Option A's native -> intermediate step currently
# just resamples that noisy data ~1:1, never actually filtering it. Oversampling *that* stage finer
# and properly decimating back down (`Resampling.average`, real anti-aliasing) is the textbook way to
# remove noise sitting near a grid's own Nyquist -- applied here, before differentiating, not after.

# %%
fine_intermediate_factor = 4
fine_intermediate_width = intermediate_width * fine_intermediate_factor
fine_intermediate_height = intermediate_height * fine_intermediate_factor

fine_intermediate_radius_path = config.output_dir / "dem_radius_fine_intermediate.tif"
lunaserv.reproject_dem_to_local_grid(
    native_path,
    native_bbox_deg,
    native_width,
    native_height,
    lunaserv_result.bbox,
    fine_intermediate_width,
    fine_intermediate_height,
    center_lon,
    center_lat,
    config.moon_radius_m,
    fine_intermediate_radius_path,
    resampling=Resampling.cubic,
)

fine_intermediate_transform = transform_from_bounds(*lunaserv_result.bbox, fine_intermediate_width, fine_intermediate_height)
cleaned_intermediate_radius = np.full((intermediate_height, intermediate_width), np.nan, dtype="float32")
with rasterio.open(fine_intermediate_radius_path) as src:
    fine_intermediate_radius = src.read(1)
reproject(
    source=fine_intermediate_radius,
    destination=cleaned_intermediate_radius,
    src_transform=fine_intermediate_transform,
    src_crs=local_crs,
    dst_transform=intermediate_transform,
    dst_crs=local_crs,
    resampling=Resampling.average,
)
cleaned_intermediate_elevation = cleaned_intermediate_radius.astype(np.float64) - config.moon_radius_m

cleaned_intermediate_hillshade = LightSource(azdeg=azimuth_deg, altdeg=elevation_deg).hillshade(
    cleaned_intermediate_elevation, dx=intermediate_gsd_m, dy=intermediate_gsd_m
)

combined_hillshade = np.full((lunaserv_result.height, lunaserv_result.width), np.nan, dtype="float64")
reproject(
    source=cleaned_intermediate_hillshade,
    destination=combined_hillshade,
    src_transform=intermediate_transform,
    src_crs=local_crs,
    dst_transform=working_transform,
    dst_crs=local_crs,
    resampling=Resampling.bilinear,
)

print("--- combined: decimate native noise, then Option A (bilinear final upsample) ---")
periodicity_report(combined_hillshade[y0:y1, x0:x1], config.dem_target_gsd_m, "combined A+B")
p489d = absolute_power_at_freq(combined_hillshade[y0:y1, x0:x1], target_freq)
p952d = absolute_power_at_freq(combined_hillshade[y0:y1, x0:x1], target_freq_2)
print(f"power@489m={p489d:.2f}  power@952m={p952d:.2f}")

# %%
plot_crop_grid(
    {
        "Option A (bilinear final upsample)": gradient_first_bilinear[y0:y1, x0:x1],
        "Combined: decimate native noise first, then Option A": combined_hillshade[y0:y1, x0:x1],
    },
    config.dem_target_gsd_m,
)

# %% [markdown]
# ## Reading off the real wavelength/direction directly, instead of guessing frequencies
#
# The last several rounds guessed a specific frequency, tested it, and got contradicted or muddied by
# the next check -- unreliable. Instead: annotate the 2D FFT log-magnitude plot itself with concentric
# circles at labeled real-world wavelengths and angle gridlines, so the actual bright spot(s) can be
# read off directly by eye (see feedback: show real images, don't have Claude guess/assert what's in
# them). `raw ortho (pre-shade)` is included as the known-clean reference for comparison -- what a
# genuinely clean FFT looks like in this same annotated format.

# %%
def annotated_fft_plot(arr2d, gsd_m, label, wavelengths_m=(300, 500, 750, 1000, 1500, 2500, 5000)):
    crop = arr2d.astype(np.float64)
    crop = crop - crop.mean()
    window = np.outer(np.hanning(crop.shape[0]), np.hanning(crop.shape[1]))
    spectrum = np.fft.fftshift(np.fft.fft2(crop * window))
    log_magnitude = np.log1p(np.abs(spectrum))

    freqs_y = np.fft.fftshift(np.fft.fftfreq(crop.shape[0], d=1.0))
    freqs_x = np.fft.fftshift(np.fft.fftfreq(crop.shape[1], d=1.0))
    extent = [freqs_x[0], freqs_x[-1], freqs_y[-1], freqs_y[0]]

    fig, ax = plt.subplots(figsize=(9, 9))
    ax.imshow(log_magnitude, cmap="inferno", extent=extent, origin="upper")
    ax.set_xlabel("freq_x (cyc/px, 0 deg == along columns)")
    ax.set_ylabel("freq_y (cyc/px)")
    ax.set_title(f"{label}: annotated FFT log-magnitude (DC at center)")

    max_freq = min(freqs_x.max(), -freqs_y.min())
    for wavelength_m in wavelengths_m:
        radius_cyc_px = gsd_m / wavelength_m
        if radius_cyc_px > max_freq:
            continue
        circle = plt.Circle((0, 0), radius_cyc_px, fill=False, color="cyan", linewidth=0.8, alpha=0.7)
        ax.add_patch(circle)
        ax.annotate(f"{wavelength_m}m", (radius_cyc_px * 0.707, radius_cyc_px * 0.707), color="cyan", fontsize=8)
    for angle_deg in range(0, 360, 30):
        angle_rad = math.radians(angle_deg)
        ax.plot(
            [0, max_freq * math.cos(angle_rad)], [0, max_freq * math.sin(angle_rad)],
            color="cyan", linewidth=0.4, alpha=0.4,
        )
    fig.tight_layout()
    return fig


annotated_fft_plot(combined_hillshade[y0:y1, x0:x1], config.dem_target_gsd_m, "combined A+B hillshade")
annotated_fft_plot(gradient_first_bilinear[y0:y1, x0:x1], config.dem_target_gsd_m, "Option A (bilinear) hillshade")
annotated_fft_plot(raw_ortho[y0:y1, x0:x1], config.dem_target_gsd_m, "raw ortho (pre-shade) -- known-clean reference")

# %% [markdown]
# ## User's direct read of the annotated FFT: energy concentrated along the Cartesian axes
#
# X axis peak ~330m, Y axis peaks ~290m and ~380m -- **not** the ~952m this notebook had been
# chasing (that guess was wrong; scrap it). All three sit close to the native ~237m grid spacing
# (within ~1.2-1.6x), and critically they're aligned to the *working grid's own* axes, not any
# rotated native-grid direction.
#
# That axis-alignment is itself a strong mechanistic clue: `rasterio.warp.reproject`'s bilinear/cubic
# kernels are *separable* (applied independently per output axis), so any ringing they introduce is
# inherently aligned to whichever grid is the *destination* of a same-CRS, non-rotated resampling step
# -- exactly what the final "intermediate -> working" upsample is in every variant tried so far
# (~2.4x, same ratio regardless of architecture). That step is common to all of them, which is
# plausibly why none of the earlier variations (kernel choice, native-noise cleanup, oversample+
# average) fully removed it -- they didn't change *that* step's fundamental separable-kernel
# characteristics. Test directly: measure power at the user's exact frequencies/directions (not a
# radially-averaged band, which would dilute an axis-concentrated signal with the quiet angles off
# axis), and try `Resampling.lanczos` (designed to minimize this kind of ringing) plus a small blur
# sized to the *correct* ~300-400m scale (not the ~950m one mistakenly targeted earlier).

# %%
def power_at_freq_and_angle(arr2d, target_freq_cyc_px, target_angle_deg, angle_tol_deg=15, freq_tol_frac=0.15):
    crop = arr2d.astype(np.float64)
    crop = crop - crop.mean()
    window = np.outer(np.hanning(crop.shape[0]), np.hanning(crop.shape[1]))
    spectrum = np.fft.fftshift(np.fft.fft2(crop * window))
    power = np.abs(spectrum) ** 2
    freqs_y = np.fft.fftshift(np.fft.fftfreq(crop.shape[0], d=1.0))
    freqs_x = np.fft.fftshift(np.fft.fftfreq(crop.shape[1], d=1.0))
    fy, fx = np.meshgrid(freqs_y, freqs_x, indexing="ij")
    freq_radius = np.sqrt(fy**2 + fx**2)
    freq_angle_deg = np.degrees(np.arctan2(fy, fx))
    angle_diff = np.abs(((freq_angle_deg - target_angle_deg) + 180) % 360 - 180)
    angle_diff_antipodal = np.abs(((freq_angle_deg - target_angle_deg + 180) + 180) % 360 - 180)
    in_angle = (angle_diff <= angle_tol_deg) | (angle_diff_antipodal <= angle_tol_deg)
    in_freq = np.abs(freq_radius - target_freq_cyc_px) <= freq_tol_frac * target_freq_cyc_px
    band = in_angle & in_freq
    return power[band].mean() if np.any(band) else float("nan")


x_freq = config.dem_target_gsd_m / 330.0  # 0 deg == along columns (x)
y_freq_a = config.dem_target_gsd_m / 290.0  # 90 deg == along rows (y)
y_freq_b = config.dem_target_gsd_m / 380.0

for label, hs in {
    "current (cubic elevation)": variant_hillshades["cubic (current default)"],
    "Option A (bilinear)": gradient_first_bilinear,
    "combined A+B": combined_hillshade,
    "raw ortho (clean reference)": raw_ortho,
}.items():
    crop = hs[y0:y1, x0:x1]
    px = power_at_freq_and_angle(crop, x_freq, 0)
    pya = power_at_freq_and_angle(crop, y_freq_a, 90)
    pyb = power_at_freq_and_angle(crop, y_freq_b, 90)
    print(f"{label:32s}  X@330m={px:10.2f}  Y@290m={pya:10.2f}  Y@380m={pyb:10.2f}")

# %% [markdown]
# ### Try `Resampling.lanczos` for the final upsample, and a correctly-sized blur

# %%
gradient_first_lanczos = np.full((lunaserv_result.height, lunaserv_result.width), np.nan, dtype="float64")
reproject(
    source=intermediate_hillshade,
    destination=gradient_first_lanczos,
    src_transform=intermediate_transform,
    src_crs=local_crs,
    dst_transform=working_transform,
    dst_crs=local_crs,
    resampling=Resampling.lanczos,
)

blurred_bilinear = gaussian_blur_2d(gradient_first_bilinear, sigma_px=1.2)  # ~120m, sized to the ~300-400m finding

for label, hs in {
    "Option A (lanczos final upsample)": gradient_first_lanczos,
    "Option A (bilinear) + small blur sigma=1.2px": blurred_bilinear,
}.items():
    crop = hs[y0:y1, x0:x1]
    px = power_at_freq_and_angle(crop, x_freq, 0)
    pya = power_at_freq_and_angle(crop, y_freq_a, 90)
    pyb = power_at_freq_and_angle(crop, y_freq_b, 90)
    print(f"{label:38s}  X@330m={px:10.2f}  Y@290m={pya:10.2f}  Y@380m={pyb:10.2f}")

# %%
plot_crop_grid(
    {
        "Option A (bilinear)": gradient_first_bilinear[y0:y1, x0:x1],
        "Option A (lanczos)": gradient_first_lanczos[y0:y1, x0:x1],
        "Option A (bilinear) + blur 1.2px": blurred_bilinear[y0:y1, x0:x1],
        # raw_ortho is uint8 (0-255), not the [0,1] hillshade scale -- normalize for a like-for-like panel.
        "raw ortho (clean reference)": raw_ortho[y0:y1, x0:x1].astype(np.float64) / 255.0,
    },
    config.dem_target_gsd_m,
)

annotated_fft_plot(gradient_first_lanczos[y0:y1, x0:x1], config.dem_target_gsd_m, "Option A (lanczos) hillshade")
annotated_fft_plot(blurred_bilinear[y0:y1, x0:x1], config.dem_target_gsd_m, "Option A (bilinear) + blur 1.2px hillshade")

# %% [markdown]
# ## Correction: the dominant peak is really ~950m (X) / ~1200m (Y), not ~330m
#
# User's closer re-inspection: the ~290-380m peaks were a smaller, better-separated-from-DC signal;
# the actually-dominant one causing visible crosshatch is back at ~950m on the X axis and ~1200m on
# the Y axis -- different per axis, not a single isotropic wavelength. That asymmetry is itself a
# clue: the native DEM fetch is anisotropic in real meters (128 pixels per *degree* in both lon and
# lat, but a degree of longitude covers less real ground than a degree of latitude away from the
# equator) -- check the exact numbers for this camera's own latitude, and re-measure every candidate
# at the corrected frequencies.

# %%
native_ns_spacing_m = (native_maxlat - native_minlat) / native_height * (config.moon_radius_m * math.pi / 180.0)
native_ew_spacing_m = (
    (native_maxlon - native_minlon) / native_width * (config.moon_radius_m * math.pi / 180.0) * math.cos(math.radians(center_lat))
)
print(f"native pixel spacing: north-south ~{native_ns_spacing_m:.1f} m, east-west ~{native_ew_spacing_m:.1f} m (ratio {native_ns_spacing_m / native_ew_spacing_m:.3f})")
print(f"observed wavelength ratio Y/X: {1200 / 950:.3f}")

x_freq2 = config.dem_target_gsd_m / 950.0
y_freq2 = config.dem_target_gsd_m / 1200.0
for label, hs in {
    "current (cubic elevation)": variant_hillshades["cubic (current default)"],
    "Option A (bilinear)": gradient_first_bilinear,
    "Option A (lanczos)": gradient_first_lanczos,
    "Option A (bilinear) + blur 1.2px": blurred_bilinear,
    "combined A+B": combined_hillshade,
}.items():
    crop = hs[y0:y1, x0:x1]
    px = power_at_freq_and_angle(crop, x_freq2, 0)
    py = power_at_freq_and_angle(crop, y_freq2, 90)
    print(f"{label:38s}  X@950m={px:10.2f}  Y@1200m={py:10.2f}")

# %%
plot_crop_grid(
    {
        "current (cubic elevation)": variant_hillshades["cubic (current default)"][y0:y1, x0:x1],
        "Option A (bilinear) + blur 1.2px": blurred_bilinear[y0:y1, x0:x1],
    },
    config.dem_target_gsd_m,
)
annotated_fft_plot(
    variant_hillshades["cubic (current default)"][y0:y1, x0:x1],
    config.dem_target_gsd_m,
    "current (cubic elevation) hillshade",
    wavelengths_m=(300, 500, 750, 950, 1200, 1500, 2500, 5000),
)
annotated_fft_plot(
    blurred_bilinear[y0:y1, x0:x1],
    config.dem_target_gsd_m,
    "Option A (bilinear) + blur 1.2px hillshade",
    wavelengths_m=(300, 500, 750, 950, 1200, 1500, 2500, 5000),
)

# %% [markdown]
# ## Correctly-sized blur: sigma matched to the real ~950-1200m period, not ~1.2px
#
# `blurred_bilinear`'s sigma=1.2px (~120m) was tuned against the wrong target. Sweep sigma up to a
# scale actually comparable to ~950-1200m (~9.5-12 working px) and re-measure at the corrected
# frequencies.

# %%
correctly_sized_blurs = {}
for sigma_px in (2.0, 3.0, 4.0, 5.0):
    blurred = gaussian_blur_2d(gradient_first_bilinear, sigma_px)
    label = f"Option A (bilinear) + blur sigma={sigma_px}px"
    correctly_sized_blurs[label] = blurred
    px = power_at_freq_and_angle(blurred[y0:y1, x0:x1], x_freq2, 0)
    py = power_at_freq_and_angle(blurred[y0:y1, x0:x1], y_freq2, 90)
    print(f"{label:42s}  X@950m={px:10.2f}  Y@1200m={py:10.2f}")

# %%
plot_crop_grid({label: hs[y0:y1, x0:x1] for label, hs in correctly_sized_blurs.items()}, config.dem_target_gsd_m)

# %% [markdown]
# ## Root cause attempt: GDAL's approximate-transformer tolerance
#
# `rasterio.warp.reproject` has a `tolerance` parameter (default 0.125), which maps directly to
# GDAL's `gdalwarp -et` -- by default GDAL does **not** compute an exact source coordinate for every
# destination pixel; it transforms only 3 points per output scanline (start/middle/end), linearly
# approximates the rest, and only falls back to an exact computation if that approximation would
# exceed the tolerance (in source pixels). This is a genuine, well-documented GDAL performance
# optimization (https://gdal.org/en/stable/programs/gdalwarp.html) -- and for a genuinely nonlinear
# transform (unprojected lon/lat -> Orthographic, not a simple affine map, especially away from the
# tangent point), a per-scanline linear approximation is a very plausible source of a small, periodic
# geometric ripple. `-et 0` forces an exact, per-pixel transform. Test directly: `tolerance=0` on
# both reprojection stages (native -> intermediate, intermediate -> working), no blur at all.

# %%
exact_intermediate_radius_path = config.output_dir / "dem_radius_intermediate_exact.tif"
lunaserv.reproject_dem_to_local_grid(
    native_path,
    native_bbox_deg,
    native_width,
    native_height,
    lunaserv_result.bbox,
    intermediate_width,
    intermediate_height,
    center_lon,
    center_lat,
    config.moon_radius_m,
    exact_intermediate_radius_path,
    resampling=Resampling.bilinear,
    tolerance=0.0,
)
with rasterio.open(exact_intermediate_radius_path) as src:
    exact_intermediate_elevation = src.read(1).astype(np.float64) - config.moon_radius_m

exact_intermediate_hillshade = LightSource(azdeg=azimuth_deg, altdeg=elevation_deg).hillshade(
    exact_intermediate_elevation, dx=intermediate_gsd_m, dy=intermediate_gsd_m
)

exact_final_hillshade = np.full((lunaserv_result.height, lunaserv_result.width), np.nan, dtype="float64")
reproject(
    source=exact_intermediate_hillshade,
    destination=exact_final_hillshade,
    src_transform=intermediate_transform,
    src_crs=local_crs,
    dst_transform=working_transform,
    dst_crs=local_crs,
    resampling=Resampling.bilinear,
    tolerance=0.0,
)

px = power_at_freq_and_angle(exact_final_hillshade[y0:y1, x0:x1], x_freq2, 0)
py = power_at_freq_and_angle(exact_final_hillshade[y0:y1, x0:x1], y_freq2, 90)
print(f"Option A (bilinear, tolerance=0, no blur)          X@950m={px:10.2f}  Y@1200m={py:10.2f}")
print(f"(compare: Option A (bilinear, default tolerance=0.125): X@950m=326.65  Y@1200m=522.33)")

# %%
plot_crop_grid(
    {
        "Option A (bilinear), default tolerance=0.125": gradient_first_bilinear[y0:y1, x0:x1],
        "Option A (bilinear), tolerance=0 (exact transform)": exact_final_hillshade[y0:y1, x0:x1],
    },
    config.dem_target_gsd_m,
)
annotated_fft_plot(
    exact_final_hillshade[y0:y1, x0:x1],
    config.dem_target_gsd_m,
    "Option A (bilinear, tolerance=0) hillshade",
    wavelengths_m=(300, 500, 750, 950, 1200, 1500, 2500, 5000),
)

# %% [markdown]
# ## Redoing the native-tile check properly: per-axis, not an isotropic average
#
# The earlier "is this already in the raw native tile?" check (which found nothing, -0.7 dB) used a
# single *averaged* native pixel spacing (~237m) for a radially-symmetric frequency band. But the
# native grid is genuinely anisotropic (185.7m east-west vs. 236.9m north-south at this latitude) --
# a radially-symmetric check on an anisotropic array doesn't correctly test either axis. Redo it
# properly: check the native array's own column-direction (east-west) power at the frequency
# equivalent to a 950m wavelength using the *correct* 185.7m spacing, and its row-direction
# (north-south) power at 1200m using the *correct* 236.9m spacing -- this is the test that actually
# settles whether the artifact is inherited from the source data or introduced by reprojection.

# %%
native_x_freq = native_ew_spacing_m / 950.0
native_y_freq = native_ns_spacing_m / 1200.0
native_crop = native_radius[ny0:ny1, nx0:nx1].astype(np.float64)
native_px = power_at_freq_and_angle(native_crop, native_x_freq, 0)
native_py = power_at_freq_and_angle(native_crop, native_y_freq, 90)
native_db_x = db_above_trend_at_freq(native_crop, native_x_freq)
native_db_y = db_above_trend_at_freq(native_crop, native_y_freq)
print(f"raw native tile, correct per-axis check: X (950m, {native_ew_spacing_m:.1f}m/px) power={native_px:.2f}  {native_db_x:+.1f} dB above trend")
print(f"raw native tile, correct per-axis check: Y (1200m, {native_ns_spacing_m:.1f}m/px) power={native_py:.2f}  {native_db_y:+.1f} dB above trend")

# %%
annotated_fft_plot(
    native_crop,
    native_gsd_m_approx,
    "raw native DEM tile (per-axis check)",
    wavelengths_m=(300, 500, 750, 950, 1200, 1500, 2500, 5000),
)

# %% [markdown]
# ## Notch filter: remove just this one periodic component, not a broad cutoff
#
# Now that the artifact is confirmed baked into the raw native tile at a precisely-known frequency
# and direction per axis, filter it out *at the source* (the native array, before any reprojection)
# with a narrow, smooth (Gaussian-shaped, not a hard cutoff -- avoids ringing) attenuation centered
# on exactly that frequency, leaving every other frequency (including genuine terrain detail)
# untouched. Applied to the native array directly, not a downstream product, so no rotation/
# frequency-warping from reprojection has to be accounted for. Not windowed before the FFT (unlike
# `periodicity_report`'s analysis-only windowing) -- this is real filtering, not spectral analysis,
# so the actual data has to be preserved, not tapered.

# %%
def notch_filter_2d(arr2d, notches, freq_tol_frac=0.15):
    """`notches`: list of (freq_y, freq_x) points in cycles/pixel to attenuate -- each point's
    mirror (-freq_y, -freq_x) must be included explicitly too (real-signal FFTs are Hermitian-
    symmetric; only touching one side would introduce a spurious imaginary component)."""
    h, w = arr2d.shape
    spectrum = np.fft.fft2(arr2d.astype(np.float64))
    freqs_y = np.fft.fftfreq(h, d=1.0)
    freqs_x = np.fft.fftfreq(w, d=1.0)
    fy, fx = np.meshgrid(freqs_y, freqs_x, indexing="ij")
    attenuation = np.ones((h, w))
    for ny, nx in notches:
        freq_dist = np.sqrt((fy - ny) ** 2 + (fx - nx) ** 2)
        sigma = freq_tol_frac * math.hypot(ny, nx)
        attenuation *= 1 - np.exp(-(freq_dist**2) / (2 * sigma**2))
    return np.fft.ifft2(spectrum * attenuation).real


notches = [
    (0.0, native_x_freq), (0.0, -native_x_freq),  # X (east-west) component + mirror
    (native_y_freq, 0.0), (-native_y_freq, 0.0),  # Y (north-south) component + mirror
]
native_radius_notched = notch_filter_2d(native_radius.astype(np.float64), notches)

notched_check = power_at_freq_and_angle(native_radius_notched[ny0:ny1, nx0:nx1], native_x_freq, 0)
notched_check_y = power_at_freq_and_angle(native_radius_notched[ny0:ny1, nx0:nx1], native_y_freq, 90)
print(f"native tile after notch filter: X power={notched_check:.2f} (was {native_px:.2f}), Y power={notched_check_y:.2f} (was {native_py:.2f})")

# %%
notched_radius_path = config.output_dir / "dem_radius_native_notched.tif"
minlon, minlat, maxlon, maxlat = native_bbox_deg
notched_transform = transform_from_bounds(minlon, minlat, maxlon, maxlat, native_width, native_height)
notched_native_crs = f"+proj=longlat +R={config.moon_radius_m} +no_defs"
with rasterio.open(
    notched_radius_path, "w", driver="GTiff", height=native_height, width=native_width, count=1,
    dtype="float32", crs=notched_native_crs, transform=notched_transform,
) as dst:
    dst.write(native_radius_notched.astype("float32"), 1)

notched_intermediate_radius_path = config.output_dir / "dem_radius_intermediate_notched.tif"
lunaserv.reproject_dem_to_local_grid(
    notched_radius_path,
    native_bbox_deg,
    native_width,
    native_height,
    lunaserv_result.bbox,
    intermediate_width,
    intermediate_height,
    center_lon,
    center_lat,
    config.moon_radius_m,
    notched_intermediate_radius_path,
    resampling=Resampling.bilinear,
)
with rasterio.open(notched_intermediate_radius_path) as src:
    notched_intermediate_elevation = src.read(1).astype(np.float64) - config.moon_radius_m

notched_intermediate_hillshade = LightSource(azdeg=azimuth_deg, altdeg=elevation_deg).hillshade(
    notched_intermediate_elevation, dx=intermediate_gsd_m, dy=intermediate_gsd_m
)

notched_final_hillshade = np.full((lunaserv_result.height, lunaserv_result.width), np.nan, dtype="float64")
reproject(
    source=notched_intermediate_hillshade,
    destination=notched_final_hillshade,
    src_transform=intermediate_transform,
    src_crs=local_crs,
    dst_transform=working_transform,
    dst_crs=local_crs,
    resampling=Resampling.bilinear,
)

px_notched = power_at_freq_and_angle(notched_final_hillshade[y0:y1, x0:x1], x_freq2, 0)
py_notched = power_at_freq_and_angle(notched_final_hillshade[y0:y1, x0:x1], y_freq2, 90)
print(f"Option A (bilinear) + native notch filter    X@950m={px_notched:10.2f}  Y@1200m={py_notched:10.2f}")
print(f"(compare: Option A (bilinear), no filter:     X@950m=326.65  Y@1200m=522.33)")
print(f"(compare: Option A (bilinear) + blur sigma=3: X@950m=9.07    Y@1200m=50.70)")

# %%
plot_crop_grid(
    {
        "Option A (bilinear), no filter": gradient_first_bilinear[y0:y1, x0:x1],
        "Option A (bilinear) + native notch filter": notched_final_hillshade[y0:y1, x0:x1],
        "Option A (bilinear) + blur sigma=3 (for comparison)": correctly_sized_blurs["Option A (bilinear) + blur sigma=3.0px"][y0:y1, x0:x1],
    },
    config.dem_target_gsd_m,
)
annotated_fft_plot(
    notched_final_hillshade[y0:y1, x0:x1],
    config.dem_target_gsd_m,
    "Option A (bilinear) + native notch filter hillshade",
    wavelengths_m=(300, 500, 750, 950, 1200, 1500, 2500, 5000),
)

# %% [markdown]
# ## Does fetching native at a lower ppd (closer to Lunaserv's own true backing resolution) help?
#
# A standalone sweep (`scratch/pyramid_sweep.py`, not part of this notebook) tested two hypotheses
# for the ~950m/1200m artifact across native fetches at ppd=32..256: a *fixed real-world wavelength*
# (consistent with a single fixed backing pyramid tier) and a *fixed number of native pixels*
# (consistent with a resampling-scale artifact tied to whatever we request). Neither held uniformly,
# but both signatures were weakest around ppd~64-96, and the fixed-pixel-count signature grew
# monotonically for anything requested finer than that -- consistent with Lunaserv's own server doing
# *its own* internal resampling from a true backing resolution somewhere around there (~250-370m,
# coarser than the 128 ppd/237m the layer's abstract claims), the same mistake this project already
# fixed on its own end, just one layer further upstream. Test directly: run the full Option A
# pipeline (bilinear, no blur/notch) at a few native ppd values around that sweet spot, no source
# fetch changes beyond `config.dem_native_ppd`.

# %%
ppd_sweep_results = {}
for test_ppd in (64.0, 80.0, 96.0, 112.0, 128.0):
    ppd_config = dataclasses.replace(config, dem_native_ppd=test_ppd)
    test_native_path, test_native_bbox_deg, test_native_width, test_native_height = lunaserv.fetch_dem_native(
        camera, ppd_config
    )
    test_intermediate_radius_path = config.output_dir / f"dem_radius_intermediate_ppd{test_ppd:.0f}.tif"
    lunaserv.reproject_dem_to_local_grid(
        test_native_path,
        test_native_bbox_deg,
        test_native_width,
        test_native_height,
        lunaserv_result.bbox,
        intermediate_width,
        intermediate_height,
        center_lon,
        center_lat,
        config.moon_radius_m,
        test_intermediate_radius_path,
        resampling=Resampling.bilinear,
    )
    with rasterio.open(test_intermediate_radius_path) as src:
        test_intermediate_elevation = src.read(1).astype(np.float64) - config.moon_radius_m
    test_intermediate_hillshade = LightSource(azdeg=azimuth_deg, altdeg=elevation_deg).hillshade(
        test_intermediate_elevation, dx=intermediate_gsd_m, dy=intermediate_gsd_m
    )
    test_final_hillshade = np.full((lunaserv_result.height, lunaserv_result.width), np.nan, dtype="float64")
    reproject(
        source=test_intermediate_hillshade,
        destination=test_final_hillshade,
        src_transform=intermediate_transform,
        src_crs=local_crs,
        dst_transform=working_transform,
        dst_crs=local_crs,
        resampling=Resampling.bilinear,
    )
    ppd_sweep_results[f"ppd={test_ppd:.0f}"] = test_final_hillshade
    px = power_at_freq_and_angle(test_final_hillshade[y0:y1, x0:x1], x_freq2, 0)
    py = power_at_freq_and_angle(test_final_hillshade[y0:y1, x0:x1], y_freq2, 90)
    print(f"native ppd={test_ppd:5.0f}  X@950m={px:10.2f}  Y@1200m={py:10.2f}")

# %%
plot_crop_grid({label: hs[y0:y1, x0:x1] for label, hs in ppd_sweep_results.items()}, config.dem_target_gsd_m)

# %% [markdown]
# ## Summary printout

# %%
print("Artifact presence in raw hillshade (pre-blend, pre-sat_sim): see the two hillshade panels above.")
print("If stripes appear there already, the cause is upstream of shade_ortho's blending step and of sat_sim.")
print("Check the FFT peak list above for a consistent off-DC direction/period across reruns/crops.")
print("Check the DEM diff histogram/unique-value list above for ~0.125m-spaced clustering (quantization signature).")
