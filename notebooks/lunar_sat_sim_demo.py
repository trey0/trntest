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
# ## Phase 5A: a recognizable image from the real WAC CDR, with SPICE-derived tie points
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
# ## Phase 5B: geo-aligned overlay of the real, ISIS-processed WAC image via `mapproject`
#
# The same real-vs-synthetic alignment question as Phase 5A, but via a true geo-aligned overlay instead of an ad hoc real-km/north-up comparison -- the WAC counterpart to Phase 6B below (they share `plotting.plot_overlay`). `isis_wac.run_pipeline()` steps the product's real EDR through ISIS3's own pipeline (`lrowac2isis` -> `spiceinit web=yes` -> `lrowaccal` -> `framestitch`), producing the same `stitched` cube Phase 6A's side-by-side comparison displays below (computed once, here, and reused there). `isis_wac.run_isd_generate()` then derives a CSM Pushframe ISD for it (ALE's `isd_generate`), and `isis_wac.run_mapproject()` reprojects it onto the DEM through that ISD -- the real-WAC counterpart to Phase 4's `cam_gen`+Phase 6B's `mapproject` for the synthetic render.
#
# **Must run against the stitched (interleaved) cube, not a lone even/odd parity in isolation** -- WAC only writes real pixel data to alternating nominal frame slots (confirmed empirically: each parity cube is ~50% populated, strictly alternating, not a same-frame split despite the name), and mapprojecting one parity alone previously produced severe venetian-blind-style smearing that had been (wrongly) attributed to a fundamental CSM Pushframe modeling limitation. See `isis_wac.run_mapproject`'s docstring and `docs/data-sources.md`'s "ISIS3/CSM spike" section for the full investigation.

# %%
stitched = isis_wac.run_pipeline(camera, frame_timing, session.config)

# %%
wac_isd = isis_wac.run_isd_generate(stitched, session.config)
wac_mapproj_tif = isis_wac.run_mapproject(stitched, wac_isd, lunaserv_result, session.config)
plotting.plot_overlay(
    lunaserv_result.ortho,
    wac_mapproj_tif,
    title="Real ISIS-processed WAC (mapprojected) over hillshade-based ortho",
);

# %% [markdown]
# ## Phase 6A: compare against a genuine ISIS/CSM-processed WAC image
#
# Phase 5A's comparison uses `wac.py`'s manual framelet-stacking (hand-derived byte offsets into the CDR). `isis_wac.run_pipeline()` (Phase 5B, above) instead steps the same product's real EDR through ISIS3's own pipeline -- a genuine camera-model-based alternative. `flip` is derived from `camera.reverse_crop_along_track`, the same real SPICE-derived per-pass yaw-state signal used throughout this notebook -- not a fixed per-product constant.
#
# `isis_wac.crop_window_for_camera()` picks the same real-footprint frame range (`camera.center_frame_index` +/- half of `camera.n_frames_for_square_crop`) that `wac.fetch_vis_mosaic` above already uses, so both comparisons cover the same real ground area -- by construction, not by search. `plotting.plot_isis_comparison()` reuses the same north-up rotation and real-km extent scaling (Phase 5A, above) that `plot_comparison` already applies -- the ISIS cube shares `wac.py`'s exact pixel-axis convention, so the same `rotations.k_crop` and physical-km scaling apply here too, not just to `wac.py`'s own crop. Phase 5A's `tie_point_results` are reused directly, not recomputed -- `tie_points.py`'s crop-pixel projection was never CSM/ISD-based to begin with (pure SPICE frame-index geometry), and its coordinate origin/scaling exactly matches `crop_window_for_camera`'s, so the same points land correctly here with no new geometry code.

# %%
plotting.plot_isis_comparison(camera, tie_point_results, render_result.rendered_tif, stitched.cub_path, isis_wac.crop_window_for_camera(camera), rotations);

# %% [markdown]
# ## Phase 6B: geo-aligned overlay of the synthetic render via `mapproject`
#
# The synthetic-render counterpart to Phase 5B, above -- both share `plotting.plot_overlay` (same CRS, same styling). `plot_comparison` (Phase 5A) aligns its two panels only "in an ad hoc way" -- matched by real-km extent and a north-up display rotation, not true pixel-for-pixel geo-registration. This phase instead reprojects the synthetic render back onto the map with ASP's `mapproject`, using the exact CSM/ISD sidecar `cam_gen` already produced for it (`render_result.csm_json`) -- the geometric inverse of `sat_sim`'s own forward DEM+camera-to-image render, through that same camera model. `--ref-map` (see `render.run_mapproject_image`) reads the projection and grid size from the same DEM the render came from, so the result shares an exact pixel grid with `lunaserv_result.ortho` (the hillshade-based base layer here) with no separate alignment step -- confirmed empirically to align real terrain features pixel-precisely, as expected for a round trip through one consistent camera model.
#
# `plotting.plot_overlay()` displays both with `rioxarray`, using each file's own real geographic coordinates rather than pixel indices.

# %%
synthetic_mapproj_tif = session.run_mapproject(render_result, lunaserv_result)
plotting.plot_overlay(
    lunaserv_result.ortho,
    synthetic_mapproj_tif,
    title="Synthetic render (mapprojected) over hillshade-based ortho",
);

# %% [markdown]
# ## Summary
#
# - Selected a real, illuminated LROC WAC EDR from a catalog-driven, multi-orbit dataset search (`trntest.dataset.select_dataset`, via the PDS ODE REST API and SPICE-derived orbit/illumination geometry), then computed LRO's true position/orientation at that image's timestamp directly in the Moon's `MOON_ME` frame via `spiceypy`, using a minimal, selectively-cached SPICE kernel set (see `docs/caching.md`).
# - Built a `.tsai` Pinhole camera from that pose and rendered a synthetic 256x256 image with ASP's `sat_sim`, fed by real DEM/imagery pulled live from Lunaserv WMS for the camera's own computed ground footprint.
# - Produced a CSM/"ISD" JSON sidecar for the rendered camera (`cam_gen`), and cross-validated the whole pose pipeline: `cam_gen` independently recovered the same sub-spacecraft geodetic position from the `.tsai`'s raw ECEF pose that the original SPICE computation produced.
# - Compared against a properly band-separated crop of the real WAC CDR from the same part of the swath -- which required both de-interleaving WAC's push-frame format and discovering (then avoiding) a long shadowed stretch at the start of the original single-demo product (now handled generally by `select_dataset`'s illumination filtering).
# - Also compared against the same real footprint processed through ISIS3's own pipeline (`isis_wac.run_pipeline`) instead of `wac.py`'s manual framelet-stacking -- a genuine camera-model-based alternative.
# - Reprojected both the synthetic render (`cam_gen`'s CSM sidecar) and the real, ISIS-processed WAC cube (ALE's `isd_generate`, against the stitched/interleaved cube -- see Phase 5B for why that distinction matters) back onto the map with `mapproject`, and overlaid each on the hillshade-based ortho with `rioxarray` -- genuine pixel-for-pixel geo-registration (`--ref-map`), not just visual similarity, confirmed to align real terrain features precisely in both cases.
