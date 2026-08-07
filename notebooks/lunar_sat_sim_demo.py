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
# # SPICE-posed synthetic lunar satellite imagery
#
# Demo: pose a synthetic pinhole camera using the **real LRO spacecraft trajectory** (NAIF SPICE kernels) at the timestamp of a real LROC WAC image selected from a catalog-driven, illumination-filtered dataset search, then generate two candidate images that could stand in for real spacecraft imagery in terrain-relative navigation (TRN) testing: a synthetic 256x256 render from real Lunaserv WMS DEM/imagery via NASA's Ames Stereo Pipeline `sat_sim` (Phase 5), and the same real footprint's actual WAC image processed through ISIS3's own EDR-to-calibrated-cube pipeline (`isis_wac.py`, Phase 6). Validates each candidate's geometry against a common reference -- a real hillshade-based basemap -- two ways: a raw, north-up-rotated quality check, and a true pixel-for-pixel geo-registered overlay via a genuine CSM camera model (`mapproject`). Phase 7 then compares the two candidates directly against each other, with explicit SPICE-derived tie points. See `../docs/plan.md` for the full phase-by-phase approach and `../docs/data-sources.md` for the researched specifics referenced below.
#
# This notebook drives the installed `trntest` package (see `../src/trntest/`) rather than duplicating its logic -- each cell is close to a one-line call into the package.

# %%
import dataclasses
import json

import trntest
from trntest import isis_wac, plotting

session = trntest.Session()
session.config.output_dir.mkdir(parents=True, exist_ok=True)

# %% [markdown]
# ## Phase 2: catalog-driven image selection + SPICE-derived camera pose
#
# `session.select_dataset()` queries the real LROC catalog (via the PDS Geosciences Node ODE REST API) for a multi-orbit window with favorable illumination geometry and good WAC data availability, and returns a throttled, illumination-filtered image list (see `trntest.dataset`/`trntest.illumination`). `session.generate_dataset(images, limit=1)` then poses a pinhole camera for the first selected image, using LRO's real position/orientation (from NAIF SPICE kernels, in the `MOON_ME` frame) at that instant, and runs the rest of the pipeline (DEM/ortho fetch, `sat_sim` render) in one call.

# %%
images = session.select_dataset(max_search_days=7)
images

# %%
results = session.generate_dataset(images, limit=1)
result = results[0]

camera = result.camera
frame_timing = result.frame_timing
session = trntest.Session(config=result.config)

print(json.dumps(dataclasses.asdict(camera), indent=2, default=str))

# %% [markdown]
# Sanity check: altitude and sub-spacecraft location should be consistent with LRO's known Fourth Extended Science Mission frozen elliptical orbit (low periapsis over the south pole).

# %%
print(f"Slant range to surface: {camera.slant_range_km:.2f} km")
print(f"Off-nadir angle: {camera.off_nadir_deg:.2f} deg")
print(f"Ground footprint center (lon, lat): {camera.footprint_lonlat_deg['center']}")

# %% [markdown]
# ## Phase 3: DEM + ortho from Lunaserv WMS
#
# Fetch `luna_wac_normalized_reflectance` (visible mosaic, despeckled and blended with a real-sun-lit hillshade -- see `docs/data-sources.md`) and `luna_wac_dtm_numeric_meters_absolute` (GLD100 DEM, converted from planetocentric radius to elevation) for the footprint above, through the local cache.

# %%
lunaserv_result = result.lunaserv_result
print(json.dumps(dataclasses.asdict(lunaserv_result), indent=2, default=str))

# %%
plotting.plot_dem_ortho(lunaserv_result);

# %% [markdown]
# ### Camera footprint over the DEM
#
# Plot the SPICE-derived camera's ground footprint (corners + center) on top of the ortho mosaic, to visually confirm the pose lands where expected.

# %%
plotting.plot_camera_footprint(lunaserv_result, camera);

# %% [markdown]
# ## Phase 4: render with `sat_sim` + the real camera pose
#
# `session.generate_dataset()` already called `run_sat_sim(camera, lunaserv_result)` above, which:
# 1. Calls `sat_sim --camera-list` with the Phase 2 `.tsai` camera against the Phase 3 DEM/ortho, producing the synthetic 256x256 image.
# 2. Calls ASP's `cam_gen` to convert that exact camera to a CSM Frame model-state JSON (the "ISD" sidecar) -- `--save-as-csm` is a no-op in `--camera-list` mode, see `docs/data-sources.md`.

# %%
render_result = result.render_result
plotting.plot_synthetic_render(render_result.rendered_tif);

# %% [markdown]
# ### The CSM / "ISD" JSON sidecar
#
# The state file's first line is a bare model-name string (not JSON) -- standard CSM "state string" convention; skip it before parsing.

# %%
model_name, csm_state = trntest.read_csm_state(render_result.csm_json)

print('Model name:', model_name)
for key in ['m_focalLength', 'm_nLines', 'm_nSamples', 'm_ccdCenter', 'm_currentParameterValue']:
    print(f'{key}: {csm_state[key]}')

# %% [markdown]
# `m_currentParameterValue`'s first 3 entries are the camera center (X, Y, Z, meters, MOON_ME frame) and the last 4 are the orientation quaternion -- matching the `C`/`R` we computed from SPICE in Phase 2.

# %% [markdown]
# ### North-up rotation (display only)
#
# The sensor model's own pixel axes are a **fixed, hardware-motivated** choice -- not something that should vary by pass (see `camera.boresight_rotation_k`'s docstring and `docs/data-sources.md`'s "Pass-dependent sensor axis convention") -- but *which way is north* in that fixed convention depends on this specific pass (ascending vs. descending, plus any yaw state), so it's computed here purely for display, and reused throughout Phases 5-7 below. `trntest.orientation` picks, for each image independently, the multiple of 90 degrees (no mirroring) whose on-screen "up" is closest to true north, and this is applied only to the plotted arrays below -- the `.tsai`, the CSM/ISD JSON, and any pixel coordinates computed elsewhere are untouched.
#
# Since the synthetic render and the real WAC crop already share the same fixed axis convention, we expect -- and confirm below -- that they need the *same* rotation here.

# %%
rotations = session.compute_display_rotations(camera, frame_timing)
print(f"synthetic: rotate {rotations.k_synthetic*90} deg for north-up (residual {rotations.dev_synthetic_deg:.1f} deg from true north)")
print(f"real crop: rotate {rotations.k_crop*90} deg for north-up (residual {rotations.dev_crop_deg:.1f} deg from true north)")

# %% [markdown]
# ### SPICE-derived tie points (display only)
#
# `session.compute_tie_points()` finds the real ground area both the synthetic render and the real WAC crop cover (each image's own footprint, an inscribed axis-aligned lon/lat box per image, intersected), picks 5 points in a die's "5"/X pattern (4 corners + center, 10% margin from the shared area's edges), and projects each into both images' pixel coordinates using their real camera models (closed-form pinhole for the synthetic image; a small root-find over frame index for the real crop, which mixes many real poses -- pure SPICE frame-index geometry, independent of which real-data pipeline actually produced the pixels). Computed once here and reused throughout Phases 5-7 below: on the render panels of 5A/6A (marking the same 5 real ground points on each candidate's own raw image), and for the direct comparison in Phase 7.

# %%
tie_point_results = session.compute_tie_points(camera, frame_timing)
for name, r in tie_point_results.items():
    print(f"{name:12s} lonlat={r['lonlat']}  synthetic_px={r['synthetic_px']}  crop_px={r['crop_px']}")

# %% [markdown]
# ## Phase 5: does the synthetic render's geometry check out?
#
# This demo's actual goal is generating synthetic images that could stand in for real spacecraft imagery in terrain-relative navigation (TRN) testing -- so the real question for each candidate TRN test image (the synthetic render here; the real, ISIS-processed WAC crop in Phase 6) is whether its geometry genuinely matches reality, not just whether it looks plausible. Both phases check this two ways against the same reference -- the hillshade-based ortho basemap (`lunaserv_result.ortho`), Phase 3's own best available geometry reference:
#
# - **A: raw image quality.** The render's own unprojected pixels, rotated north-up and scaled to real km, next to a plain crop of the basemap covering the same real footprint, both marked with the same 5 tie points -- a quick, ad hoc look at whether the render's content and rough position/scale make sense (`plotting.plot_render_vs_basemap`).
# - **B: pixel-for-pixel alignment.** The render reprojected onto the map through its own real camera model (`mapproject`) and overlaid directly on the basemap -- true geo-registration, not just visual similarity (`plotting.plot_overlay`).
#
# (Phase 7, below, is just 5A's and 6A's own render panels put together directly, for an easier side-by-side look at the two candidates themselves.)

# %%
plotting.plot_render_vs_basemap(
    plotting.read_raster_band(render_result.rendered_tif),
    rotations.k_synthetic,
    camera.cross_track_width_km,
    camera.cross_track_width_km,
    camera.footprint_lonlat_deg,
    lunaserv_result.ortho,
    title="Phase 5A: synthetic render vs. hillshade-based basemap",
    render_label="Synthetic (sat_sim, SPICE-posed)",
    tie_point_results=tie_point_results,
    render_px_key="synthetic_px",
);

# %% [markdown]
# `render.run_mapproject`'s `--ref-map` (see `render.run_mapproject_image`) reprojects the synthetic render through the exact CSM/ISD sidecar `cam_gen` already produced for it (`render_result.csm_json`) -- the geometric inverse of `sat_sim`'s own forward DEM+camera-to-image render, through that same camera model -- onto the same DEM the render came from, so the result shares an exact pixel grid with `lunaserv_result.ortho` with no separate alignment step. `plotting.plot_overlay()` displays both with `rioxarray`, using each file's own real geographic coordinates rather than pixel indices.

# %%
synthetic_mapproj_tif = session.run_mapproject(render_result, lunaserv_result)
plotting.plot_overlay(
    lunaserv_result.ortho,
    synthetic_mapproj_tif,
    title="Phase 5B: synthetic render (mapprojected) over hillshade-based basemap",
);

# %% [markdown]
# ## Phase 6: does the real, ISIS-processed WAC crop's geometry check out?
#
# The same two-style geometry check as Phase 5, for the other TRN test image candidate: a roughly square center crop of the product's real WAC EDR, processed through ISIS3's own pipeline instead of `sat_sim`. WAC is a push-frame camera: each 78-line frame multiplexes 7 filters (2 UV @ 4 TDI lines + 5 VIS @ 14 TDI lines), and getting a usable, calibrated image out of the raw EDR takes real processing. `isis_wac.run_pipeline()` steps the product's real EDR through ISIS3's own pipeline (`lrowac2isis` -> `spiceinit web=yes` -> `lrowaccal` -> `framestitch`) -- calibrated, band-separated, and framelet-interleaved through a genuine camera-model-aware toolchain, not a hand-derived byte offset. `flip` is derived from `camera.reverse_crop_along_track`, the same real SPICE-derived per-pass yaw-state signal used throughout this notebook -- not a fixed per-product constant. The resulting `stitched` cube is reused for both 6A and 6B below.

# %%
stitched = isis_wac.run_pipeline(camera, frame_timing, session.config)

# %% [markdown]
# `isis_wac.crop_window_for_camera()` picks the real-footprint frame range (`camera.center_frame_index` +/- half of `camera.n_frames_for_square_crop`) this crop covers. `session.crop_footprint_corners()` independently ray-traces that same crop's real ground footprint (corners + center) from real SPICE geometry -- not assumed identical to the synthetic camera's own footprint -- for `plot_render_vs_basemap` to crop the matching basemap area.

# %%
crop_footprint = session.crop_footprint_corners(frame_timing, camera)
crop_width_km = camera.cross_track_width_km
crop_height_km = camera.n_frames_for_square_crop * camera.km_per_frame
plotting.plot_render_vs_basemap(
    plotting.read_raster_band(stitched.cub_path, window=isis_wac.crop_window_for_camera(camera)),
    rotations.k_crop,
    crop_width_km,
    crop_height_km,
    crop_footprint,
    lunaserv_result.ortho,
    title="Phase 6A: real ISIS-processed WAC crop vs. hillshade-based basemap",
    render_label="Real WAC (ISIS-processed)",
    tie_point_results=tie_point_results,
    render_px_key="crop_px",
);

# %% [markdown]
# `isis_wac.run_isd_generate()` derives a CSM Pushframe ISD for `stitched` (ALE's `isd_generate`), and `isis_wac.run_mapproject()` reprojects it onto the DEM through that ISD -- the real-WAC counterpart to 5B's `mapproject`, sharing `plotting.plot_overlay` (same CRS, same styling).
#
# **Must run against the stitched (interleaved) cube, not a lone even/odd parity in isolation** -- WAC only writes real pixel data to alternating nominal frame slots (confirmed empirically: each parity cube is ~50% populated, strictly alternating, not a same-frame split despite the name), and mapprojecting one parity alone previously produced severe venetian-blind-style smearing that had been (wrongly) attributed to a fundamental CSM Pushframe modeling limitation. See `isis_wac.run_mapproject`'s docstring and `docs/data-sources.md`'s "ISIS3/CSM spike" section for the full investigation.
#
# `run_mapproject` reprojects `stitched` in full -- the *entire* real swath (258 frames), not just the square crop 6A/Phase 7 compare -- so `zoom_footprint_lonlat_deg=crop_footprint` restricts the displayed extent to that same crop's own footprint (computed above for 6A), for a fair side-by-side against 6A rather than a long strip against a small square.

# %%
wac_isd = isis_wac.run_isd_generate(stitched, session.config)
wac_mapproj_tif = isis_wac.run_mapproject(stitched, wac_isd, lunaserv_result, session.config)
plotting.plot_overlay(
    lunaserv_result.ortho,
    wac_mapproj_tif,
    title="Phase 6B: real ISIS-processed WAC (mapprojected) over hillshade-based basemap",
    zoom_footprint_lonlat_deg=crop_footprint,
);

# %% [markdown]
# ## Phase 7: synthetic render vs. real WAC, side by side
#
# Conceptually, just 5A's and 6A's own render panels (synthetic, real WAC crop -- same tie points, same north-up rotation, computed above) put together directly, for an easier side-by-side look at the two TRN test image candidates themselves, rather than each against the basemap. `plotting.plot_isis_comparison()` additionally brightness-matches the two panels (a single multiplicative scale, since the ISIS cube's calibrated I/F and the render's texture-brightness values are on entirely different numeric scales to begin with -- see its docstring) and interpolates across the real crop's small, known dead-pixel gaps for display -- both real quality-of-life improvements on top of what 5A/6A already show, not new geometry content.

# %%
plotting.plot_isis_comparison(camera, tie_point_results, render_result.rendered_tif, stitched.cub_path, isis_wac.crop_window_for_camera(camera), rotations);

# %% [markdown]
# ## Summary
#
# - Selected a real, illuminated LROC WAC EDR from a catalog-driven, multi-orbit dataset search (`trntest.dataset.select_dataset`, via the PDS ODE REST API and SPICE-derived orbit/illumination geometry), then computed LRO's true position/orientation at that image's timestamp directly in the Moon's `MOON_ME` frame via `spiceypy`, using a minimal, selectively-cached SPICE kernel set (see `docs/caching.md`).
# - Built a `.tsai` Pinhole camera from that pose and rendered a synthetic 256x256 image with ASP's `sat_sim`, fed by real DEM/imagery pulled live from Lunaserv WMS for the camera's own computed ground footprint. Produced a CSM/"ISD" JSON sidecar for it (`cam_gen`), and cross-validated the whole pose pipeline: `cam_gen` independently recovered the same sub-spacecraft geodetic position from the `.tsai`'s raw ECEF pose that the original SPICE computation produced.
# - Validated the synthetic render's geometry against the hillshade-based basemap two ways (Phase 5): a raw, north-up-rotated quality check (5A), and a true pixel-for-pixel geo-registered overlay via `mapproject` through the render's own CSM sidecar (5B).
# - Processed the same real footprint's WAC EDR through ISIS3's own pipeline (`isis_wac.run_pipeline`) -- a genuine camera-model-based real-WAC product (EDR fetch through calibration and framelet interleaving) -- and validated its geometry against the same basemap the same two ways (Phase 6): a raw quality check (6A), and a `mapproject` overlay through ALE's `isd_generate` ISD, against the stitched/interleaved cube specifically (6B).
# - Compared the two TRN test image candidates directly against each other, with explicit SPICE-derived tie points (Phase 7).
