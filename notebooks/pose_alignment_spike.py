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
# # Camera-pose alignment spike: tie-point correction for the real-WAC overlay
#
# `image_generation.ipynb`'s Phase 6B overlay (the real, ISIS-processed WAC crop mapprojected onto
# the basemap) is visibly not perfectly aligned with it -- small, "not huge" per direct user
# observation, but real. This notebook is the checked-in, reproducible form of the investigation
# into that (see `docs/plan.md`'s open items, "camera-pose alignment", for the full research trail:
# why ASP's `bundle_adjust`/`pc_align`/`image_align` and ISIS's own `jigsaw`+`findfeatures` routes
# both hit real blockers, and `docs/history.md`'s dated entries for how this notebook's own approach
# was arrived at). **Validated, but not wired into the main pipeline** -- direct user visual
# inspection of the homography-corrected blink overlay below confirmed the correspondences this
# notebook finds are real, not RANSAC accepting noise (see `docs/history.md`'s dated entries),
# concluded as the deliberate stopping point for this 2D approach; the next real step is a proper
# projection-informed (camera-model) alignment, not further refinement here, so this stays a
# standalone tool, not wired into `image_generation.py`'s main pipeline.
#
# The approach, implemented in `src/trntest/pose_alignment.py`: feature-match the already
# map-projected WAC crop directly against the basemap (both already in the same map projection, no
# camera model needed for the matching step itself; downsampled first to the WAC crop's own real
# native resolution -- see the "Crop the basemap" section below, and `docs/history.md`'s Phase 53
# entry for why), fit a 2D correction from the matches via RANSAC, apply it to the WAC raster's own
# georeferencing, and compare the result against the basemap via the *existing*, unmodified
# `plotting.plot_overlay_toggle` blink comparator.
#
# Three correction models are fit from the same match set and compared directly: `fit_similarity_correction`
# (translation + rotation + uniform scale, 4 DOF), `fit_affine_correction` (adds independent x/y
# scale and shear, 6 DOF), and `fit_homography_correction` (full projective, 8 DOF). None is
# asserted as physically "correct" -- a real camera pose error has 6 degrees of freedom before even
# accounting for this being a pushframe sensor's extended-exposure capture, and the mapping from
# those onto a 2D map-space distortion isn't simple or one-to-one, so there's no first-principles
# case that any fixed DOF count is exactly right; richer models were left for later specifically
# until there were enough well-distributed inliers to support them without overfitting (Phase 53's
# downsampling fix raised that count from 53 to 91). See `fit_similarity_correction`'s own docstring
# for the full rationale.

# %%
import warnings

import numpy as np
import rasterio
from scipy.spatial.transform import Rotation

import trntest
from trntest import control_network, isis_wac, plotting, pose_alignment, tie_points, wac_camera_model

images = trntest.read_manifest("dataset_manifest.csv")
session = trntest.Session()
dataset = trntest.TrnTestDataSet.create(session.config.output_dir / "trn_dataset", images, session.config)
dataset.populate(limit=1)
entry = dataset[0]

# The real public function TrnTestCropImage._mapprojected_path() itself calls -- used directly here
# rather than that private method, which isn't meant for callers outside the class.
wac_path = isis_wac.run_cam2map_for_crop(entry.crop_result, entry.dem_ortho_result, entry.per_image_config)
basemap_path = entry.dem_ortho_result.ortho
print("WAC mapprojected crop:", wac_path)
print("Basemap ortho:", basemap_path)

# %% [markdown]
# ## Crop the basemap to the WAC's own footprint, and prepare both for matching
#
# The basemap ortho covers a much larger area than the WAC crop's own real footprint -- cropping to
# match (`pose_alignment.crop_to_footprint`, padded 15%) gives the feature matcher a far smaller,
# more relevant search space; confirmed empirically to matter for match quality, not just compute.
#
# Both `wac_path` and the basemap ortho are on the same ~100 m/px working grid (`config.
# DEFAULT_DEM_TARGET_GSD_M`) -- but that's genuinely native resolution for the basemap
# (`luna_wac_global`'s own ~100 m/px mosaic) and *not* for this WAC crop: `isis_wac.
# run_cam2map_for_crop`'s `PIXRES=map` forces its output onto that same 100 m/px grid regardless of
# the camera's own real resolution at this pose, which a direct `cam2map PIXRES=camera` probe found
# to be ~184 m/px for this candidate (~1.8x coarser -- see `docs/history.md`'s dated entry). Matching
# SIFT keypoints on the interpolated-not-actually-resolved 100 m/px grid risks treating resampling
# texture (and the `PATCHSIZE=1` warp-patch seam artifact still faintly visible there, per
# `docs/plan.md`'s open items) as real structure. `pose_alignment.native_wac_gsd_m` estimates the
# WAC crop's real native GSD from the camera's own already-computed ground geometry (no extra ISIS
# call), and `downsample_to_gsd` (area-averaging, the correct decimation filter -- see its own
# docstring for why nearest/bilinear would be wrong here) brings both rasters down to that scale
# before matching. `to_uint8_for_matching` then converts each to 8-bit (OpenCV's feature detectors
# can't even load a raw float32 GeoTIFF), stretching over valid pixels only.

# %%
basemap_cropped_path = pose_alignment.crop_to_footprint(
    basemap_path, wac_path, entry.per_image_config.output_dir / "alignment" / "basemap_cropped.tif"
)

target_gsd_m = pose_alignment.native_wac_gsd_m(entry.camera)
print(f"Downsampling to the WAC crop's estimated native resolution: {target_gsd_m:.0f} m/px")
wac_matching_path = pose_alignment.downsample_to_gsd(
    wac_path, target_gsd_m, entry.per_image_config.output_dir / "alignment" / "wac_matching_res.tif"
)
basemap_matching_path = pose_alignment.downsample_to_gsd(
    basemap_cropped_path, target_gsd_m, entry.per_image_config.output_dir / "alignment" / "basemap_matching_res.tif"
)

wac_image, wac_valid = pose_alignment.to_uint8_for_matching(wac_matching_path)
basemap_image, basemap_valid = pose_alignment.to_uint8_for_matching(basemap_matching_path)
print(f"WAC: {wac_image.shape}, valid {wac_valid.mean():.1%}")
print(f"Basemap: {basemap_image.shape}, valid {basemap_valid.mean():.1%}")

# %% [markdown]
# ## Feature matching
#
# SIFT on Sobel-filtered versions of each image (see `pose_alignment.match_features`'s docstring for
# why: raw-intensity matching across two different sensors/processing pipelines -- real calibrated
# WAC I/F vs. a synthetic basemap render -- is far less reliable than matching on edge/gradient
# content, which is more consistent across that gap), with a mutual ratio test and two RANSAC
# geometric-consistency passes (homography, then epipolar).

# %%
basemap_points_px, wac_points_px = pose_alignment.match_features(basemap_image, basemap_valid, wac_image, wac_valid)
print(f"{len(basemap_points_px)} matched points survived ratio/symmetry/RANSAC verification")

# %% [markdown]
# Converting both point sets to real map coordinates (`pose_alignment.pixel_points_to_map`) --
# necessary since the WAC crop and the cropped basemap are different windows with different pixel
# origins, even though they share a CRS/scale -- lets us look at the real-world offset each match
# implies. High scatter here (relative to the mean) is a sign the match set is a mix of real
# correspondences and false positives, not a single clean, trustworthy correction on its own.

# %%
with rasterio.open(basemap_matching_path) as src:
    basemap_transform = src.transform
with rasterio.open(wac_matching_path) as src:
    wac_transform = src.transform

basemap_points_map = pose_alignment.pixel_points_to_map(basemap_points_px, basemap_transform)
wac_points_map = pose_alignment.pixel_points_to_map(wac_points_px, wac_transform)

raw_offsets_m = wac_points_map - basemap_points_map
raw_distances_m = np.linalg.norm(raw_offsets_m, axis=1)
print(f"Raw offset distance: mean {raw_distances_m.mean():.0f}m, std {raw_distances_m.std():.0f}m")
print(f"Range: {raw_distances_m.min():.0f}m - {raw_distances_m.max():.0f}m")

# %% [markdown]
# ## Fit the pose correction: similarity, full affine, and homography
#
# `fit_similarity_correction` maps the WAC's own (possibly-wrong) claimed positions onto the
# basemap's trusted ones, with its own internal RANSAC separating real, consistent correspondences
# from outliers -- report both, not just the inliers, since how bad the rejected points really are
# is itself useful information about match-set quality.
#
# Phase 53's native-resolution downsampling raised the default candidate's inlier count from 53 to
# 91 -- enough to make a first real attempt at the richer models `fit_similarity_correction`'s own
# docstring always left open: `fit_affine_correction` (6 DOF: independent x/y scale and shear, not
# just uniform scale) and `fit_homography_correction` (8 DOF: full projective). All three are fit
# from the *same* match set below for a direct, apples-to-apples comparison -- residuals are also
# reported in native WAC pixels (dividing by `target_gsd_m`), not just meters, since that's the unit
# that actually says whether a correction is doing better than pixel-level noise.

# %%
correction_similarity, inliers_similarity, residuals_similarity_m = pose_alignment.fit_similarity_correction(
    wac_points_map, basemap_points_map
)
correction_affine, inliers_affine, residuals_affine_m = pose_alignment.fit_affine_correction(
    wac_points_map, basemap_points_map
)
homography, inliers_homography, residuals_homography_m = pose_alignment.fit_homography_correction(
    wac_points_map, basemap_points_map
)

for name, inliers, residuals_m in [
    ("similarity (4 DOF)", inliers_similarity, residuals_similarity_m),
    ("affine (6 DOF)", inliers_affine, residuals_affine_m),
    ("homography (8 DOF)", inliers_homography, residuals_homography_m),
]:
    inlier_residuals_m = residuals_m[inliers]
    print(
        f"{name:20s}  inliers {inliers.sum():3d}/{len(inliers)}   "
        f"residual mean {inlier_residuals_m.mean():5.0f}m ({inlier_residuals_m.mean() / target_gsd_m:.2f}px)   "
        f"max {inlier_residuals_m.max():5.0f}m ({inlier_residuals_m.max() / target_gsd_m:.2f}px)"
    )

scale = np.sqrt(correction_similarity.a**2 + correction_similarity.d**2)
rotation_deg = np.degrees(np.arctan2(correction_similarity.d, correction_similarity.a))
print(
    f"\nSimilarity fit: scale {scale:.4f}  rotation {rotation_deg:.3f} deg  "
    f"translation ({correction_similarity.c:.1f}, {correction_similarity.f:.1f}) m"
)

# %% [markdown]
# ## Apply each correction and compare via the existing blink overlay
#
# `apply_correction` composes an `affine.Affine` fit (similarity or full affine -- both are affine,
# so it handles either identically) with the WAC raster's own georeferencing and resamples back onto
# its original grid; `apply_homography_correction` does the projective equivalent for the homography
# fit (see its own docstring for why a homography needs a different code path). All three corrected
# rasters drop straight into the *existing* `plotting.plot_overlay_toggle` blink comparator with no
# further plumbing changes. A blink comparator is the right tool for judging a shift this small (a
# few pixels): far more sensitive than a static side-by-side crop.

# %%
alignment_dir = entry.per_image_config.output_dir / "alignment"
corrected_similarity_path = pose_alignment.apply_correction(
    wac_path, correction_similarity, alignment_dir / "wac_corrected_similarity.tif"
)
corrected_affine_path = pose_alignment.apply_correction(
    wac_path, correction_affine, alignment_dir / "wac_corrected_affine.tif"
)
corrected_homography_path = pose_alignment.apply_homography_correction(
    wac_path, homography, alignment_dir / "wac_corrected_homography.tif"
)

# %%
plotting.plot_overlay_toggle(basemap_path, wac_path, title="Uncorrected WAC over basemap")

# %%
plotting.plot_overlay_toggle(basemap_path, corrected_similarity_path, title="Similarity-corrected WAC over basemap")

# %%
plotting.plot_overlay_toggle(basemap_path, corrected_affine_path, title="Affine-corrected WAC over basemap")

# %%
plotting.plot_overlay_toggle(basemap_path, corrected_homography_path, title="Homography-corrected WAC over basemap")

# %% [markdown]
# ## A second matcher: LightGlue, compared directly against SIFT
#
# `pose_alignment.match_features_lightglue` swaps classical SIFT for a deep-learned local-feature
# extractor (DISK) + learned matcher (LightGlue) -- tried specifically to push match count/quality
# higher for more challenging future EDRs (shadowed terrain, low texture) than SIFT can reliably
# deliver. Same inputs (the native-GSD-downsampled `wac_image`/`basemap_image` from above), same
# downstream pipeline (map coordinates -> `fit_homography_correction`, the model Phase 54's direct
# user visual inspection validated as giving a real, non-noise improvement) -- only the matcher
# itself differs, for a direct, apples-to-apples comparison against the SIFT-based homography result
# already shown above.

# %%
basemap_points_px_lg, wac_points_px_lg = pose_alignment.match_features_lightglue(
    basemap_image, basemap_valid, wac_image, wac_valid
)
print(f"SIFT:      {len(basemap_points_px)} matched points")
print(f"LightGlue: {len(basemap_points_px_lg)} matched points")

basemap_points_map_lg = pose_alignment.pixel_points_to_map(basemap_points_px_lg, basemap_transform)
wac_points_map_lg = pose_alignment.pixel_points_to_map(wac_points_px_lg, wac_transform)

homography_lg, inliers_homography_lg, residuals_homography_lg_m = pose_alignment.fit_homography_correction(
    wac_points_map_lg, basemap_points_map_lg
)
inlier_residuals_lg_m = residuals_homography_lg_m[inliers_homography_lg]
print(
    f"\nSIFT homography:      inliers {inliers_homography.sum():3d}/{len(inliers_homography)}   "
    f"residual mean {residuals_homography_m[inliers_homography].mean():5.0f}m "
    f"({residuals_homography_m[inliers_homography].mean() / target_gsd_m:.2f}px)"
)
print(
    f"LightGlue homography: inliers {inliers_homography_lg.sum():3d}/{len(inliers_homography_lg)}   "
    f"residual mean {inlier_residuals_lg_m.mean():5.0f}m ({inlier_residuals_lg_m.mean() / target_gsd_m:.2f}px)"
)

# %%
corrected_homography_lg_path = pose_alignment.apply_homography_correction(
    wac_path, homography_lg, alignment_dir / "wac_corrected_homography_lightglue.tif"
)
plotting.plot_overlay_toggle(
    basemap_path, corrected_homography_lg_path, title="Homography-corrected WAC over basemap (LightGlue matches)"
)

# %% [markdown]
# ## Toward a proper projection-aware (3D) alignment: real ISIS control points
#
# Everything above corrects the WAC raster's own map-space georeferencing after the fact -- a 2D
# fix, not a camera-pose one. The next step (see `docs/plan.md`'s open items) is a real `jigsaw`
# bundle adjustment over the camera's actual exterior orientation (6 DOF: position + attitude,
# degree-0/frozen for a first pass), using these same matched tie points as control points.
#
# `jigsaw` needs, per tie point, the real *image-space* pixel it was observed at (in the original,
# pre-`cam2map` WAC crop cube -- the cube `jigsaw` will actually adjust) and a trusted 3D ground
# location -- not the map-projected pixel positions `match_features`/`match_features_lightglue`
# return. `control_network.resolve_control_points` converts between the two: see its own docstring
# for exactly how (a deterministic un-warp of `cam2map`'s own resampling on the WAC side, direct
# georeferencing on the basemap side) and why it's deliberately **ellipsoid-only for now, not real
# DEM elevation** -- this pipeline's entire existing ground<->image geometry
# (`isis_wac.run_spiceinit`'s `shape=ellipsoid`) already is, and feeding elevation-aware ground truth
# into a camera model that's still ellipsoid-only would conflate real camera-pose error with the
# ellipsoid-vs-real-terrain gap -- worst exactly at high-relief features like crater rims, which is
# where the parallax-like effect motivating this whole investigation was actually seen. A DEM-aware
# shape model is a deliberate, real follow-up, not attempted here.
#
# Using the LightGlue match set (more tie points to work with than SIFT's).

# %%
ground_to_image_model = isis_wac.resolve_ground_to_image_model(
    entry.stitched, entry.crop_result, entry.per_image_config
)
with rasterio.open(wac_path) as src:
    map_crs = src.crs

observed_pixels, ground_lonlat = control_network.resolve_control_points(
    wac_points_map_lg, basemap_points_map_lg, map_crs, ground_to_image_model, entry.per_image_config
)
print(f"{len(observed_pixels)} real ISIS control points resolved (from {len(wac_points_map_lg)} LightGlue matches)")
print(
    f"Observed pixel (sample, line) range: "
    f"({observed_pixels[:, 0].min():.0f}-{observed_pixels[:, 0].max():.0f}, "
    f"{observed_pixels[:, 1].min():.0f}-{observed_pixels[:, 1].max():.0f})"
)
print(
    f"Ground point (lon, lat) range: "
    f"({ground_lonlat[:, 0].min():.3f}-{ground_lonlat[:, 0].max():.3f}, "
    f"{ground_lonlat[:, 1].min():.3f}-{ground_lonlat[:, 1].max():.3f})"
)

# %% [markdown]
# ## The real fit: `jigsaw`'s hand-rolled fallback
#
# `jigsaw` itself hit a real, root-caused, unfixable bug in its PushFrame framelet search (see
# `docs/wac-jigsaw-investigation.md` for the full trail: a tautological, mathematically
# guaranteed-zero-error control network still produced ~350px `jigsaw` residuals). Pivoted to a
# hand-rolled Python ground-to-image forward projection (`src/trntest/wac_camera_model.py`) instead
# -- its optics chain is validated to exact (0.000px) agreement with real `campt` output, and its
# framelet search (`find_framelet_and_project`) is validated to 0.00m ground error round-tripped
# through `campt`'s trusted inverse.
#
# `fit_pose_correction` fits a single, frozen 6-DOF `PoseCorrection` (3 position, meters, MOON_ME;
# 3 rotation, composed on the camera side -- matching this project's own precedent that WAC-VIS's
# real boresight offset is frame-constant, not time-varying) against real control points, via
# `scipy.optimize.least_squares`. `calibrate_et_per_crop_line` derives the crop's own line-to-ET
# relationship from 2 real `campt` `EphemerisTime` queries, rather than hand-deriving
# `crop_window_for_camera`'s row-offset/flip bookkeeping.

# %%
ground_points_me_m = (
    np.array(
        [
            tie_points.lonlat_to_ground_km(lon_deg, lat_deg, entry.per_image_config.moon_radius_km)
            for lon_deg, lat_deg in ground_lonlat
        ]
    )
    * 1000.0
)

with warnings.catch_warnings():
    # NotGeoreferencedWarning is expected for an ISIS .cub at this pipeline stage (no geotransform
    # yet, not a bug) -- see plotting.read_raster_band's own docstring for this same suppression.
    warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
    with rasterio.open(entry.crop_result.cub_path) as src:
        n_lines = src.height
n_framelets = n_lines // wac_camera_model.FRAMELET_HEIGHT
et0, et_per_line = wac_camera_model.calibrate_et_per_crop_line(entry.crop_result.cub_path, n_lines)
print(f"crop: {n_framelets} framelets, et0={et0:.3f}, et_per_line={et_per_line:.6f}")

# %% [markdown]
# Baseline (uncorrected) residuals -- how far off the existing, uncorrected SPICE-derived pose
# already is, at each real control point -- for a direct before/after comparison against the fit.

# %%
baseline_residuals_px = []
for ground_pt, obs in zip(ground_points_me_m, observed_pixels, strict=True):
    predicted = wac_camera_model.find_framelet_and_project(ground_pt, n_framelets, et0, et_per_line)
    if predicted is not None:
        baseline_residuals_px.append((predicted[0] - obs[0], predicted[1] - obs[1]))
baseline_residuals_px = np.array(baseline_residuals_px)
baseline_norms = np.linalg.norm(baseline_residuals_px, axis=1)
print(
    f"Baseline (uncorrected): {len(baseline_residuals_px)}/{len(observed_pixels)} points resolved, "
    f"residual mean {baseline_norms.mean():.2f}px, max {baseline_norms.max():.2f}px"
)

# %%
fit = wac_camera_model.fit_pose_correction(ground_points_me_m, observed_pixels, n_framelets, et0, et_per_line)

fit_norms = np.linalg.norm(fit.residuals_px, axis=1)
# A control point that lands outside crop coverage under the fitted correction gets a fixed, large
# penalty residual (see fit_pose_correction's own docstring) -- filtered out here for the mean/max
# stats below since it's a sentinel, not a real pixel error; reported separately if it happens.
RESOLVED_RESIDUAL_THRESHOLD_PX = 100.0  # well below _UNRESOLVED_RESIDUAL_PX, well above any real fit
resolved = fit_norms < RESOLVED_RESIDUAL_THRESHOLD_PX
print(f"Fit success: {fit.success}")
print(f"delta_position_m (MOON_ME): {fit.correction.delta_position_m}")
rotvec_deg = np.degrees(Rotation.from_matrix(fit.correction.delta_rotation).as_rotvec())
print(f"delta_rotation (deg, camera-frame rotation vector): {rotvec_deg}")
print(
    f"Fitted: {resolved.sum()}/{len(observed_pixels)} points resolved, "
    f"residual mean {fit_norms[resolved].mean():.2f}px, max {fit_norms[resolved].max():.2f}px"
)
if not resolved.all():
    print(f"WARNING: {(~resolved).sum()} control point(s) unresolved at the fitted correction")

# %% [markdown]
# ## A direct visual before/after: the fitted correction, baked into a real corrected overlay
#
# `isis_wac.apply_pose_correction_to_crop` bakes `fit.correction` into a *copy* of the crop cube's
# cached `InstrumentPointing` (patching only its single, time-independent `ConstantRotation` matrix
# -- see that function's own docstring and `docs/corrected-overlay-cam2map-plan.md` for the full
# mechanism), so ISIS's own already-validated `cam2map` (`run_cam2map_for_crop`, completely
# unmodified) picks up the corrected pose automatically. This is the first real visual evidence of
# the fit above -- everything before this cell only checked pixel residuals numerically.

# %%
corrected_crop = isis_wac.apply_pose_correction_to_crop(entry.crop_result, fit.correction, entry.per_image_config)
corrected_cam2map_path = isis_wac.run_cam2map_for_crop(corrected_crop, entry.dem_ortho_result, entry.per_image_config)

# %%
plotting.plot_overlay_toggle(basemap_path, wac_path, title="Uncorrected WAC over basemap (real-fit comparison)")

# %%
plotting.plot_overlay_toggle(basemap_path, corrected_cam2map_path, title="Pose-corrected WAC over basemap")
