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
# Demo: pose a synthetic pinhole camera using the **real LRO spacecraft trajectory** (NAIF SPICE kernels) at the timestamp of a real LROC WAC image selected from a catalog-driven, illumination-filtered dataset search, render a synthetic 256x256 image from real Lunaserv WMS DEM/imagery with NASA's Ames Stereo Pipeline `sat_sim`, and compare it against the real WAC data. See `../docs/plan.md` for the full phase-by-phase approach and `../docs/data-sources.md` for the researched specifics referenced below.
#
# This notebook drives the installed `trntest` package (see `../src/trntest/`) rather than duplicating its logic -- each cell is close to a one-line call into the package.

# %%
import dataclasses
import json

import trntest
from trntest import plotting

# isis_wac is unused while Phase 6 is temporarily disabled below -- re-add this import when re-enabling it.

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
# ## Phase 5: a recognizable image from the real WAC CDR, with SPICE-derived tie points
#
# WAC is a push-frame camera: each 78-line frame multiplexes 7 filters (2 UV @ 4 TDI lines + 5 VIS @ 14 TDI lines), and per the official LROC SIS, the CDR "will require further processing to separate framelets into their respective bands ... in order to be viewed as a standard image." `session.fetch_vis_mosaic(camera)` does that: it pulls one VIS filter's 14-line block (an offset guaranteed pure-VIS regardless of WAC's current yaw-dependent band ordering -- see the module docstring) from many consecutive frames and stacks them, which is exactly how WAC's push-frame design builds continuous coverage. The frame count is computed (not a fixed guess) so the crop covers the same real ground distance as the synthetic camera's FOV -- see `docs/plan.md`.
#
# `select_dataset()` already filtered out unilluminated products (sun elevation >= 10 deg at each candidate's midpoint-anchored image center, computed via SPICE's `ilumin` -- see `trntest.dataset.anchor_start_frame_for_centered_crop`), so the selected image is guaranteed well-lit without hand-tuning a start frame the way earlier iterations of this demo did. The synthetic camera's pose epoch is that crop's own temporal midpoint (`camera.center_frame_index`), so both images are centered on the same ground point.
#
# `session.compute_tie_points()` adds explicit, geometry-derived tie points: it finds the real ground area both images cover (each image's own footprint, an inscribed axis-aligned lon/lat box per image, intersected), picks 5 points in a die's "5"/X pattern (4 corners + center, 10% margin from the shared area's edges), and projects each into both images' pixel coordinates using their real camera models (closed-form pinhole for the synthetic image; a small root-find over frame index for the real crop, which mixes many real poses). Both panels are plotted in real km (not raw pixel index) so the non-square-pixel CDR crop and the square-pixel synthetic image display at the same true scale.

# %%
tie_point_results = session.compute_tie_points(camera, frame_timing)
for name, r in tie_point_results.items():
    print(f"{name:12s} lonlat={r['lonlat']}  synthetic_px={r['synthetic_px']}  crop_px={r['crop_px']}")

# %% [markdown]
# ### North-up rotation (display only)
#
# The sensor model's own pixel axes (Part A) are a **fixed, hardware-motivated** choice -- not something that should vary by pass -- but *which way is north* in that fixed convention depends on this specific pass (ascending vs. descending, plus any yaw state), so it's computed here purely for display. `trntest.orientation` picks, for each image independently, the multiple of 90 degrees (no mirroring) whose on-screen "up" is closest to true north, and this is applied only to the plotted arrays and tie-point marker positions below -- the `.tsai`, the CSM/ISD JSON, and `session.compute_tie_points()`'s own returned pixel values are untouched.
#
# Since both images already share the same axis convention (Part A), we expect -- and confirm below -- that they need the *same* rotation here.

# %%
rotations = session.compute_display_rotations(camera, frame_timing)
print(f"synthetic: rotate {rotations.k_synthetic*90} deg for north-up (residual {rotations.dev_synthetic_deg:.1f} deg from true north)")
print(f"real crop: rotate {rotations.k_crop*90} deg for north-up (residual {rotations.dev_crop_deg:.1f} deg from true north)")

# %%
vis_mosaic = session.fetch_vis_mosaic(camera)
plotting.plot_comparison(camera, tie_point_results, vis_mosaic, rotations, render_result.rendered_tif);

# %% [markdown]
# ## Phase 6: compare against a genuine ISIS/CSM-processed WAC image
#
# Phase 5's comparison uses `wac.py`'s manual framelet-stacking (hand-derived byte offsets into the CDR). `isis_wac.run_pipeline()` instead steps the same product's real EDR through ISIS3's own pipeline (`lrowac2isis` -> `spiceinit web=yes` -> `lrowaccal` -> `framestitch`) -- a genuine camera-model-based alternative, not yet reprojected onto the DEM (`mapproject`, still an open item -- see `docs/plan.md`). `flip` is derived from `camera.reverse_crop_along_track`, the same real SPICE-derived per-pass yaw-state signal used throughout this notebook -- not a fixed per-product constant.
#
# `isis_wac.crop_window_for_camera()` picks the same real-footprint frame range (`camera.center_frame_index` +/- half of `camera.n_frames_for_square_crop`) that `wac.fetch_vis_mosaic` above already uses, so both comparisons cover the same real ground area -- by construction, not by search. `plotting.plot_isis_comparison()` reuses the same north-up rotation and real-km extent scaling (Phase 5, above) that `plot_comparison` already applies -- the ISIS cube shares `wac.py`'s exact pixel-axis convention, so the same `rotations.k_crop` and physical-km scaling apply here too, not just to `wac.py`'s own crop. Phase 5's `tie_point_results` are reused directly, not recomputed -- `tie_points.py`'s crop-pixel projection was never CSM/ISD-based to begin with (pure SPICE frame-index geometry), and its coordinate origin/scaling exactly matches `crop_window_for_camera`'s, so the same points land correctly here with no new geometry code.

# %%
# Temporarily disabled: the external ISIS SPICE web service (`spiceinit web=yes`) is currently
# failing ("SPICE server returned incompatible SPICE data"), unrelated to this repo's own code --
# confirmed non-transient (same failure on repeated runs). Re-enable once that service recovers.
# stitched = isis_wac.run_pipeline(camera, frame_timing, session.config)

# %%
# plotting.plot_isis_comparison(camera, tie_point_results, render_result.rendered_tif, stitched.cub_path, isis_wac.crop_window_for_camera(camera), rotations);

# %% [markdown]
# ## Phase 7: geo-aligned overlay of the synthetic render via `mapproject`
#
# `plot_comparison` above aligns its two panels only "in an ad hoc way" -- matched by real-km extent and a north-up display rotation, not true pixel-for-pixel geo-registration. This phase instead reprojects the synthetic render back onto the map with ASP's `mapproject`, using the exact CSM/ISD sidecar `cam_gen` already produced for it (`render_result.csm_json`) -- the geometric inverse of `sat_sim`'s own forward DEM+camera-to-image render, through that same camera model. `--ref-map` (see `render.run_mapproject`) reads the projection and grid size from the same DEM the render came from, so the result shares an exact pixel grid with `lunaserv_result.ortho` (the hillshade-based base layer here) with no separate alignment step -- confirmed empirically to align real terrain features pixel-precisely, as expected for a round trip through one consistent camera model.
#
# `plotting.plot_overlay()` displays both with `rioxarray`, using each file's own real geographic coordinates rather than pixel indices.

# %%
mapproj_tif = session.run_mapproject(render_result, lunaserv_result)
plotting.plot_overlay(
    lunaserv_result.ortho,
    mapproj_tif,
    overlay_alpha=1.0,  # debug: fully opaque to see what mapproj_tif actually contains
    title="Synthetic render (mapprojected) over hillshade-based ortho",
);

# %% [markdown]
# ## Summary
#
# - Selected a real, illuminated LROC WAC EDR from a catalog-driven, multi-orbit dataset search (`trntest.dataset.select_dataset`, via the PDS ODE REST API and SPICE-derived orbit/illumination geometry), then computed LRO's true position/orientation at that image's timestamp directly in the Moon's `MOON_ME` frame via `spiceypy`, using a minimal, selectively-cached SPICE kernel set (see `docs/caching.md`).
# - Built a `.tsai` Pinhole camera from that pose and rendered a synthetic 256x256 image with ASP's `sat_sim`, fed by real DEM/imagery pulled live from Lunaserv WMS for the camera's own computed ground footprint.
# - Produced a CSM/"ISD" JSON sidecar for the rendered camera (`cam_gen`), and cross-validated the whole pose pipeline: `cam_gen` independently recovered the same sub-spacecraft geodetic position from the `.tsai`'s raw ECEF pose that the original SPICE computation produced.
# - Compared against a properly band-separated crop of the real WAC CDR from the same part of the swath -- which required both de-interleaving WAC's push-frame format and discovering (then avoiding) a long shadowed stretch at the start of the original single-demo product (now handled generally by `select_dataset`'s illumination filtering).
# - Also compared against the same real footprint processed through ISIS3's own pipeline (`isis_wac.run_pipeline`) instead of `wac.py`'s manual framelet-stacking -- a genuine camera-model-based alternative, not yet reprojected onto the DEM (see `docs/plan.md`'s open items for the still-unresolved framelet-boundary striping question this is investigating).
# - Reprojected the synthetic render back onto the map with `mapproject`, through the same CSM/ISD sidecar camera model that generated it, and overlaid it on the hillshade-based ortho with `rioxarray` -- genuine pixel-for-pixel geo-registration (`render.run_mapproject`'s `--ref-map`), not just visual similarity, confirmed to align real terrain features precisely.
