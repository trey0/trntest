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
# Generate two candidate images that could stand in for real spacecraft imagery in
# terrain-relative navigation (TRN) testing, for a real LROC WAC image selected ahead of time by
# `data_set_selection.ipynb`: a synthetic 256x256 render from real Lunaserv WMS DEM/imagery via
# NASA's Ames Stereo Pipeline `sat_sim` (Phase 5), and the same real footprint's actual WAC image
# processed through ISIS3's own EDR-to-calibrated-cube pipeline (`isis_wac.py`, Phase 6). Both are
# posed using the **real LRO spacecraft trajectory** (NAIF SPICE kernels) at the timestamp of that
# real image. Validates each candidate's geometry against a common reference -- a real
# hillshade-based basemap -- two ways: a raw, north-up-rotated quality check, and a true
# pixel-for-pixel geo-registered overlay (the synthetic render via a CSM camera model and ASP's
# `mapproject`; the real WAC crop via ISIS's own native Pushframe camera model and `cam2map`,
# after `mapproject`'s CSM route turned out to have a real geometric bug for this sensor type --
# see Phase 6's notes). Phase 7 then compares the two candidates directly against each other, with
# explicit tie points. See `../docs/plan.md` for the full phase-by-phase approach and
# `../docs/data-sources.md` for the researched specifics referenced below.
#
# This notebook drives the installed `trntest` package (see `../src/trntest/`) rather than
# duplicating its logic -- each cell is close to a one-line call into the package.
#
# **Which image**: this notebook reads `dataset_manifest.csv`, a small file checked into this
# repo alongside this notebook, produced by `data_set_selection.ipynb`'s last cell (that notebook
# also picked/prints the same `edr_product` id below -- see it for the catalog search parameters
# used). This notebook has no runtime dependency on `data_set_selection.ipynb` itself -- to render
# a different real image, rerun that notebook and commit its updated `dataset_manifest.csv`.

# %%
import dataclasses
import json

import trntest
from trntest import isis_wac, plotting, tie_points

images = trntest.read_manifest("dataset_manifest.csv")
print(f"Rendering EDR product: {images.iloc[0]['edr_product']} (from dataset_manifest.csv, see data_set_selection.ipynb)")

session = trntest.Session()
session.config.output_dir.mkdir(parents=True, exist_ok=True)

# %% [markdown]
# ## Phase 2: generate the selected image + SPICE-derived camera pose
#
# `session.generate_dataset(images, limit=1)` poses a pinhole camera for the first (and, per
# `data_set_selection.ipynb`, only relevant) row of the manifest above, using LRO's real
# position/orientation (from NAIF SPICE kernels, in the `MOON_ME` frame) at that instant, and runs
# the rest of the pipeline (DEM/ortho fetch, `sat_sim` render) in one call. The DEM/ortho fetch AOI
# is sized to cover both the synthetic camera's own footprint *and* the real WAC crop's footprint
# (`GenerationResult.crop_footprint`, reused directly in Phase 6 below) in one request, so nothing
# displayed later runs past the edge of what was actually fetched here.

# %%
results = session.generate_dataset(images, limit=1)
result = results[0]

camera = result.camera
frame_timing = result.frame_timing
crop_footprint = result.crop_footprint
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
# ### Tie points (display only)
#
# `session.select_tie_points()` finds the real ground area both the synthetic render and the real WAC crop cover (each image's own footprint, an inscribed axis-aligned lon/lat box per image, intersected -- an approximate estimate, used only to pick plausible candidate points, not to place them), picks 5 points in a die's "5"/X pattern (4 corners + center, 10% margin from the shared area's edges), and projects each into the synthetic image's exact pixel coordinates (closed-form pinhole, the same fixed pose that rendered it). The real WAC crop's own pixel coordinates (`crop_px`) aren't resolved here yet -- that needs the real crop cube to exist first (Phase 6, below), and now goes through a genuine ISIS `campt` ground-to-image query against it, not a hand-rolled approximation (see `tie_points.py`'s module docstring). Reused throughout Phases 5-7 below: on the render panels of 5A/6A (marking the same 5 real ground points on each candidate's own raw image), and for the direct comparison in Phase 7.

# %%
tie_point_results = session.select_tie_points(camera, frame_timing)
for name, r in tie_point_results.items():
    print(f"{name:12s} lonlat={r['lonlat']}  synthetic_px={r['synthetic_px']}")

# %% [markdown]
# ## Phase 5: does the synthetic render's geometry check out?
#
# This demo's actual goal is generating synthetic images that could stand in for real spacecraft imagery in terrain-relative navigation (TRN) testing -- so the real question for each candidate TRN test image (the synthetic render here; the real, ISIS-processed WAC crop in Phase 6) is whether its geometry genuinely matches reality, not just whether it looks plausible. Both phases check this two ways against the same reference -- the hillshade-based ortho basemap (`lunaserv_result.ortho`), Phase 3's own best available geometry reference:
#
# - **A: raw image quality.** The render's own unprojected pixels, rotated north-up and scaled to real km, next to a plain crop of the basemap covering the same real footprint, both marked with the same 5 tie points -- a quick, ad hoc look at whether the render's content and rough position/scale make sense (`plotting.plot_render_vs_basemap`).
# - **B: pixel-for-pixel alignment.** The render reprojected onto the map through its own real camera model (`mapproject`) and overlaid directly on the basemap -- true geo-registration, not just visual similarity, as an auto-blinking animated GIF that alternates the overlay against the basemap (`plotting.plot_overlay_toggle`).
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
# `render.run_mapproject`'s `--ref-map` (see `render.run_mapproject_image`) reprojects the synthetic render through the exact CSM/ISD sidecar `cam_gen` already produced for it (`render_result.csm_json`) -- the geometric inverse of `sat_sim`'s own forward DEM+camera-to-image render, through that same camera model -- onto the same DEM the render came from, so the result shares an exact pixel grid with `lunaserv_result.ortho` with no separate alignment step. `plotting.plot_overlay_toggle()` displays both with `rioxarray`, using each file's own real geographic coordinates rather than pixel indices, as an animated GIF that automatically blinks the overlay on and off.

# %%
synthetic_mapproj_tif = session.run_mapproject(render_result, lunaserv_result)
plotting.plot_overlay_toggle(
    lunaserv_result.ortho,
    synthetic_mapproj_tif,
    title="Phase 5B: synthetic render (mapprojected) over hillshade-based basemap",
)

# %% [markdown]
# ## Phase 6: does the real, ISIS-processed WAC crop's geometry check out?
#
# The same two-style geometry check as Phase 5, for the other TRN test image candidate: a roughly square center crop of the product's real WAC EDR, processed through ISIS3's own pipeline instead of `sat_sim`. WAC is a push-frame camera: each 78-line frame multiplexes 7 filters (2 UV @ 4 TDI lines + 5 VIS @ 14 TDI lines), and getting a usable, calibrated image out of the raw EDR takes real processing. `isis_wac.run_pipeline()` steps the product's real EDR through ISIS3's own pipeline (`lrowac2isis` -> `spiceinit web=yes` -> `lrowaccal` -> `framestitch`) -- calibrated, band-separated, and framelet-interleaved through a genuine camera-model-aware toolchain, not a hand-derived byte offset. `flip` (`camera.reverse_crop_along_track`, the same real SPICE-derived per-pass yaw-state signal used throughout this notebook, not a fixed per-product constant) is passed explicitly now, not derived from a `Camera` object -- `session.generate_dataset()` above already ran this exact pipeline once internally, to pose `camera`'s own boresight at this crop's real, ISIS-determined center (see `camera.build_camera`'s docstring); `run_pipeline` is idempotent, so this call just reuses that same stitched cube rather than redoing the work.
#
# `isis_wac.crop_for_camera()` then crops `stitched` (via ISIS's own `crop` app) down to the real-footprint frame range (`camera.center_frame_index` +/- half of `camera.n_frames_for_square_crop`) -- a single, real "WAC crop" cube that both 6A (raw display, right below) and 6B (mapprojected, further below) derive from, the same way 5A/5B both already derive from one synthetic render with no special-casing. `crop_footprint` (from Phase 2's `result.crop_footprint` -- `tie_points.crop_footprint_corners_for_camera`'s independent ray-trace of that same crop's real ground footprint, computed inside `generate_dataset()` since it's also needed there to size the DEM/ortho fetch) is what `plot_render_vs_basemap` crops the matching basemap area to.
#
# Unlike 5B, 6B does *not* go through ASP's `mapproject` via a CSM/ISD sidecar. That path was tried extensively and abandoned: investigation traced its bad geometry to a real bug in `usgscsm`'s `UsgsAstroPushFrameSensorModel::groundToImage` (the function `mapproject` calls once per output pixel) -- an unbracketed secant search over framelet index that's unreliable for Pushframe images, badly so for a short crop. Confirmed independent of any ISD authoring choice (varying every plausible ISD field made no difference), confirmed via a chained-iteration round-trip test that never converges to a stable answer for the crop, and confirmed even the long-trusted *full-cube* reference used throughout this notebook's earlier validation is itself measurably affected (~0.2-0.4 correlation against ISIS's own native reprojection, despite the two using literally the same source pixels). `isis_wac.run_cam2map_for_crop()` instead uses ISIS's own native camera model via `cam2map` -- confirmed self-consistent regardless of crop size (crop vs. full cube agree to 0.9999986 correlation) and independently checked against `pyproj`'s own math to sub-micrometer precision. See `docs/history.md`'s dated entry for the full investigation.

# %%
stitched = isis_wac.run_pipeline(camera.reverse_crop_along_track, frame_timing, session.config)
wac_crop = isis_wac.crop_for_camera(stitched, camera, session.config)

# %% [markdown]
# `isis_wac.resolve_ground_to_image_model()` decides which camera-model authority the real crop's own tie points should be queried against: try a CSM ISD sidecar first (`isd_generate`, same tool 5B's `mapproject` uses), and only fall back to the crop's native, SPICE-embedded camera model if the ISD resolves to a Pushframe sensor model -- the class `mapproject`'s `groundToImage` is known unreliable for (see 6B's notes above). For WAC-VIS this always takes the fallback branch, but the decision itself is real (derived from the ISD's own `name_model`), not hardcoded. `tie_points.resolve_crop_pixels()` then queries each tie point's real pixel location in `wac_crop` via ISIS's own `campt` -- a genuine ground-to-image lookup through a validated tool, replacing the deprecated SPICE-only approximation `select_tie_points` used to compute this same value with (see `tie_points.py`'s module docstring for the measured discrepancy this fixes). A die5 point the real camera doesn't actually see (the approximate footprint used to pick candidate points can be off enough for this to happen for real, e.g. near the poles) is dropped with a printed warning rather than breaking the run.

# %%
ground_to_image_model = isis_wac.resolve_ground_to_image_model(stitched, wac_crop, session.config)
tie_point_results = tie_points.resolve_crop_pixels(tie_point_results, ground_to_image_model)
for name, r in tie_point_results.items():
    print(f"{name:12s} crop_px={r['crop_px']}")

# %%
crop_width_km = camera.cross_track_width_km
crop_height_km = camera.n_frames_for_square_crop * camera.km_per_frame
plotting.plot_render_vs_basemap(
    plotting.read_raster_band(wac_crop.cub_path),
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
# `isis_wac.run_cam2map_for_crop()` reprojects `wac_crop` onto the map via ISIS's own native `cam2map`, using a map file cloned from `lunaserv_result`'s own local Orthographic projection (`isis_wac._orthographic_map_pvl`) so the output lands in the same real-world coordinate system as `lunaserv_result.ortho` -- the real-WAC counterpart to 5B's `mapproject`, sharing `plotting.plot_overlay_toggle` (no special-casing needed since `wac_crop` -- unlike the old full `stitched` cube -- already covers just the real footprint being compared).

# %%
wac_mapproj_tif = isis_wac.run_cam2map_for_crop(wac_crop, lunaserv_result, session.config)
plotting.plot_overlay_toggle(
    lunaserv_result.ortho,
    wac_mapproj_tif,
    title="Phase 6B: real ISIS-processed WAC (mapprojected) over hillshade-based basemap",
)

# %% [markdown]
# ## Phase 7: synthetic render vs. real WAC, side by side
#
# Conceptually, just 5A's and 6A's own render panels (synthetic, real WAC crop -- same tie points, same north-up rotation, computed above) put together directly, for an easier side-by-side look at the two TRN test image candidates themselves, rather than each against the basemap. `plotting.plot_isis_comparison()` additionally brightness-matches the two panels (a single multiplicative scale, since the ISIS cube's calibrated I/F and the render's texture-brightness values are on entirely different numeric scales to begin with -- see its docstring) and interpolates across the real crop's small, known dead-pixel gaps for display -- both real quality-of-life improvements on top of what 5A/6A already show, not new geometry content.

# %%
plotting.plot_isis_comparison(camera, tie_point_results, render_result.rendered_tif, wac_crop.cub_path, rotations);

# %% [markdown]
# ## Summary
#
# - Rendered a real, illuminated LROC WAC EDR picked by `data_set_selection.ipynb`'s catalog-driven, multi-orbit dataset search (`trntest.dataset.select_dataset`, via the PDS ODE REST API and SPICE-derived orbit/illumination geometry), then computed LRO's true position/orientation at that image's timestamp directly in the Moon's `MOON_ME` frame via `spiceypy`, using a minimal, selectively-cached SPICE kernel set (see `docs/caching.md`).
# - Built a `.tsai` Pinhole camera from that pose and rendered a synthetic 256x256 image with ASP's `sat_sim`, fed by real DEM/imagery pulled live from Lunaserv WMS for the camera's own computed ground footprint. Produced a CSM/"ISD" JSON sidecar for it (`cam_gen`), and cross-validated the whole pose pipeline: `cam_gen` independently recovered the same sub-spacecraft geodetic position from the `.tsai`'s raw ECEF pose that the original SPICE computation produced.
# - Validated the synthetic render's geometry against the hillshade-based basemap two ways (Phase 5): a raw, north-up-rotated quality check (5A), and a true pixel-for-pixel geo-registered overlay via `mapproject` through the render's own CSM sidecar (5B).
# - Processed the same real footprint's WAC EDR through ISIS3's own pipeline (`isis_wac.run_pipeline`) -- a genuine camera-model-based real-WAC product (EDR fetch through calibration and framelet interleaving) -- cropped it to the real footprint being compared (`isis_wac.crop_for_camera`), and validated that single crop's geometry against the same basemap the same two ways (Phase 6): a raw quality check (6A), and a `cam2map` overlay through ISIS's own native Pushframe camera model (6B) -- not ASP's `mapproject`/CSM, after finding a real bug in `usgscsm`'s `groundToImage` for Pushframe sensors.
# - Compared the two TRN test image candidates directly against each other, with explicit tie points (Phase 7).
