"""Tie-point-based pose/registration correction for a map-projected raster (e.g. the real-WAC
Phase 6B overlay, `isis_wac.run_cam2map_for_crop`'s output) against a trusted reference raster
already in the same map projection (e.g. `dem_ortho_result.ortho`) -- feature-match the two rasters,
fit a 2D correction from the matches, and apply it to the source raster's own georeferencing.

This exists because the real-WAC overlay is visibly not perfectly aligned with the basemap (small,
"not huge" per direct user observation) -- see `docs/plan.md`'s open items ("camera-pose alignment")
for the full research trail this module is the result of, including why the two most obvious
approaches don't apply here: ASP's own `bundle_adjust`/`pc_align`/`image_align` route corrects
cameras via ASP's `mapproject`, which is the CSM/Pushframe path already abandoned elsewhere in this
project for a confirmed severe bug; and ISIS's own `jigsaw` + `findfeatures` (the architecturally
"right" tool, real USGS-documented practice for single-image space resection against a basemap) hits
a real, unresolved blocker where its control-point-construction step discards every match regardless
of `TARGET=`/`GEOMTYPE=` settings, likely because the basemap here is a plain GDAL-exported GeoTIFF,
not something ISIS itself map-projected.

This module works entirely in 2D image/map space instead, sidestepping both: match two already
map-projected, same-CRS rasters directly (no camera model, no ISIS control network), fit a 2D
correction, and apply it to the source raster's own affine transform. **Validated, but not wired
into the main pipeline**: direct user visual inspection of the homography-corrected blink overlay
confirmed the correspondences this module finds are real, not RANSAC accepting noise (see
`docs/history.md`'s dated entries) -- concluded as the deliberate stopping point for this 2D
approach, with the next real step being a proper projection-informed (camera-model) alignment
rather than further refinement here, so this stays a standalone tool, not wired into
`image_generation.py`'s main pipeline. `notebooks/pose_alignment_spike.py` exercises this module
end-to-end against the current default dataset candidate.

Requires `opencv-python-headless` for SIFT/RANSAC (`cv2`) -- not needed anywhere else in this
project, added as a real dependency specifically for this module."""

import functools
from pathlib import Path

import affine
import cv2
import lightglue
import lightglue.utils
import numpy as np
import rasterio
import rasterio.warp
import rasterio.windows
import torch

from trntest.lunaserv import pad_bbox

# ISIS's own nodata/special-pixel sentinel convention for float32 rasters this project already
# reads without `masked=True` in a few places (e.g. `plotting.valid_pixel_mask`'s own threshold) --
# matched here rather than reinvented, since `isis_wac.run_cam2map_for_crop`'s own output uses it.
_FLOAT_NODATA_MAGNITUDE_THRESHOLD = 1e30
# The concrete sentinel value itself (float32 min), used as a fallback when a raster's own GDAL
# nodata tag isn't set -- both `apply_correction` and `downsample_to_gsd` need an explicit nodata
# value to pass to `rasterio.warp.reproject` so resampling doesn't blend real data with padding.
_ISIS_FLOAT_NODATA = -3.4028235e38

# cv2.BFMatcher.knnMatch(k=2)'s own fixed neighbor count -- a descriptor with fewer than this many
# candidate neighbors can't be ratio-tested at all, not an independently tunable minimum.
_KNN_NEIGHBORS_FOR_RATIO_TEST = 2
# Minimum point correspondences each RANSAC geometric model needs to be fit at all (a property of
# the models themselves, not a chosen tuning knob): 4 for a homography, 8 for the fundamental
# matrix's 8-point algorithm.
_MIN_POINTS_FOR_HOMOGRAPHY = 4
_MIN_POINTS_FOR_FUNDAMENTAL_MATRIX = 8


def to_uint8_for_matching(raster_path, percentile: float = 99.9) -> tuple[np.ndarray, np.ndarray]:
    """Reads band 1 of `raster_path` and returns `(uint8_image, valid_mask)`, ready for OpenCV
    feature matching -- OpenCV's feature detectors need 8-bit input (confirmed empirically: its TIFF
    reader can't even load a raw float32 GeoTIFF, e.g. `isis_wac.run_cam2map_for_crop`'s calibrated
    I/F output). A plain 0/`percentile`-th-percentile linear stretch over the *valid* pixels only
    (matching `plotting`'s own stretch convention elsewhere in this project) -- computing the
    percentile over the whole array, including large invalid/nodata regions (e.g. a mapprojected
    crop's own padding outside its real rotated footprint), would be a no-op here since nodata reads
    as a huge-magnitude sentinel, but is avoided on principle since it's the wrong statistic
    regardless of this particular sentinel's value. Invalid pixels come back as `0` in the returned
    image and `False` in `valid_mask`."""
    with rasterio.open(raster_path) as src:
        data = src.read(1).astype("float64")
    valid = np.isfinite(data) & (np.abs(data) < _FLOAT_NODATA_MAGNITUDE_THRESHOLD)
    vmax = np.percentile(data[valid], percentile)
    scaled = np.zeros_like(data)
    scaled[valid] = np.clip(data[valid] / vmax * 255, 0, 255)
    return scaled.astype("uint8"), valid


def crop_to_footprint(reference_path, footprint_source_path, out_path, pad_fraction: float = 0.15) -> Path:
    """Crops `reference_path` (e.g. the basemap ortho, typically much larger than the area actually
    being compared) down to `footprint_source_path`'s own real valid-data bounding box, padded by
    `pad_fraction` (via `lunaserv.pad_bbox`, reused rather than reinvented -- same "pad generously"
    convention this project already uses for WMS fetch AOIs). Matching the two rasters' real extent
    like this matters for feature matching specifically: an unmatched, much-larger reference frame
    gives OpenCV's matcher a far larger, mostly-irrelevant search space to false-match against,
    confirmed empirically to hurt match quality, not just waste compute."""
    with rasterio.open(footprint_source_path) as src:
        data = src.read(1)
        valid = np.isfinite(data) & (np.abs(data.astype("float64")) < _FLOAT_NODATA_MAGNITUDE_THRESHOLD)
        rows = np.where(valid.any(axis=1))[0]
        cols = np.where(valid.any(axis=0))[0]
        r0, r1, c0, c1 = rows.min(), rows.max(), cols.min(), cols.max()
        minx, maxy = src.transform * (c0, r0)
        maxx, miny = src.transform * (c1, r1)

    padded = pad_bbox((minx, miny, maxx, maxy), pad_fraction)
    with rasterio.open(reference_path) as src:
        window = rasterio.windows.from_bounds(*padded, transform=src.transform)
        data = src.read(1, window=window)
        out_transform = src.window_transform(window)
        profile = src.profile.copy()
        profile.update(height=data.shape[0], width=data.shape[1], transform=out_transform)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(data, 1)
    return out_path


def native_wac_gsd_m(camera) -> float:
    """Estimates the WAC crop's own real native ground sample distance -- i.e. before
    `isis_wac.run_cam2map_for_crop`'s `PIXRES=map` forces its output onto the basemap's ~100 m/px
    working grid (see that function's docstring) -- directly from `camera`'s already-ray-traced
    cross-track/along-track ground geometry (`Camera.cross_track_width_km`/`km_per_frame`, computed
    by `camera.compute_n_frames_for_square_crop`), rather than an extra ISIS `cam2map PIXRES=camera`
    probe call. WAC is a pushframe sensor with genuinely anisotropic native resolution -- 704
    cross-track samples vs. 14 along-track TDI lines per VIS framelet, covering different real
    ground extents -- so this returns the *coarser* of the two axes: downsampling the map-projected
    (isotropic) product any finer than that would still be interpolating detail that was never
    actually resolved in that direction. Confirmed against a direct measurement (`cam2map
    PIXRES=camera`, no map override) on this project's current default candidate: this function
    returns 211 m/px (cross-track; along-track was 151 m/px) vs. ISIS's own camera model reporting
    184 m/px for the same crop -- same order of magnitude and, being the coarser estimate, on the
    conservative side for choosing a downsample target, not a substitute for the exact figure if one
    is ever needed elsewhere."""
    cross_track_gsd_m = camera.cross_track_width_km * 1000.0 / 704.0
    along_track_gsd_m = camera.km_per_frame * 1000.0 / 14.0
    return max(cross_track_gsd_m, along_track_gsd_m)


def downsample_to_gsd(
    raster_path,
    target_gsd_m: float,
    out_path,
    resampling: rasterio.warp.Resampling = rasterio.warp.Resampling.average,
) -> Path:
    """Resamples `raster_path` (band 1) onto a coarser grid at `target_gsd_m` m/px, same CRS and
    origin -- used to bring a map-projected product's pixel grid back down toward its own real
    native resolution (see `native_wac_gsd_m`) before feature matching, instead of matching SIFT
    keypoints on a grid that's been interpolated finer than the sensor actually resolved (confirmed,
    via a direct `cam2map PIXRES=camera` probe, to be a real ~1.8x linear oversampling on this
    project's own default candidate -- see `docs/history.md`'s dated entry).

    `resampling=Resampling.average` (not the default nearest/bilinear) is the deliberate choice for
    genuinely *shrinking* real imagery -- it approximates what a coarser-GSD sensor would actually
    have integrated over each output pixel, rather than just picking or blending between a few
    existing samples the way nearest/bilinear do. This is a real accuracy difference for downsampling
    specifically (unlike `apply_correction`'s bilinear resample, which resamples at essentially the
    same scale, where the choice matters far less).

    Raises `ValueError` if `target_gsd_m` isn't actually coarser than the source's own resolution --
    this function downsamples, it doesn't upsample."""
    with rasterio.open(raster_path) as src:
        src_res = src.res[0]
        if target_gsd_m <= src_res:
            raise ValueError(f"target_gsd_m ({target_gsd_m:.1f}) must exceed the source's own {src_res:.1f} m/px")
        scale = target_gsd_m / src_res
        new_width = max(1, round(src.width / scale))
        new_height = max(1, round(src.height / scale))
        dst_transform = src.transform * affine.Affine.scale(scale, scale)

        data = src.read(1)
        # `_ISIS_FLOAT_NODATA` is only a valid fallback for float rasters (e.g. `wac_path`'s
        # calibrated I/F cube) -- the basemap ortho this function is also used on
        # (`lunaserv.despeckle_and_shade_ortho`'s output) is `uint8` with no real nodata concept, and
        # that huge-magnitude sentinel isn't representable in its dtype at all (confirmed live: GDAL
        # raises rather than silently truncating it).
        nodata = src.nodata
        if nodata is None and np.issubdtype(data.dtype, np.floating):
            nodata = _ISIS_FLOAT_NODATA
        out = np.full((new_height, new_width), nodata if nodata is not None else 0, dtype=data.dtype)
        rasterio.warp.reproject(
            source=data,
            destination=out,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=dst_transform,
            dst_crs=src.crs,
            src_nodata=nodata,
            dst_nodata=nodata,
            resampling=resampling,
        )
        profile = src.profile.copy()
        profile.update(height=new_height, width=new_width, transform=dst_transform)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(out, 1)
    return out_path


def _sobel_edges(image: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Sobel gradient-magnitude, percentile-normalized over valid pixels only (see
    `to_uint8_for_matching` for why: normalizing over the whole array lets a large invalid/padding
    region's own sharp valid/invalid boundary dominate the stretch and wash out real content
    contrast) -- confirmed empirically necessary here: without masking, WAC keypoint counts came out
    an order of magnitude lower (848 vs. 40000+) due to exactly this. Matches `findfeatures`'
    `FILTER=SOBEL` option, used for the same reason: the WAC crop and the basemap are different
    sensors/processing pipelines with different tone curves, and edge/gradient content is far more
    consistent across that gap than raw intensity is."""
    gx = cv2.Sobel(image, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(image, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    vmax = np.percentile(mag[valid], 99.5)
    out = np.clip(mag / vmax * 255, 0, 255).astype("uint8")
    out[~valid] = 0
    return out


def match_features(
    from_image: np.ndarray,
    from_valid: np.ndarray,
    to_image: np.ndarray,
    to_valid: np.ndarray,
    ratio: float = 0.8,
    ransac_reproj_threshold_px: float = 3.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Matches `from_image` against `to_image` (both `to_uint8_for_matching`'s output) via SIFT on
    Sobel-filtered versions of each (see `_sobel_edges`), a mutual (both-directions) Lowe's ratio
    test, and two RANSAC geometric-consistency passes (homography, then epipolar/fundamental
    matrix) -- the same pipeline ISIS's own `findfeatures` uses internally (confirmed by matching its
    reported match counts closely: 47 vs. its own 46 on the same real image pair), reimplemented
    directly in OpenCV because `findfeatures` doesn't expose raw matched pixel coordinates, only
    summary counts, and (separately, see the module docstring) its own control-point-construction
    step doesn't work with this project's plain-GeoTIFF basemap regardless.

    Returns `(from_points_px, to_points_px)`, same-length arrays of matched `(x, y)` pixel
    coordinates in each input image's own pixel space -- convert to real map coordinates via each
    raster's own `rasterio` transform before comparing the two (they're different crops with
    different origins, so raw pixel coordinates aren't directly comparable -- see
    `pixel_points_to_map`)."""
    sift = cv2.SIFT_create()  # type: ignore[attr-defined]  # real at runtime; cv2's bundled stubs miss it
    from_edges = _sobel_edges(from_image, from_valid)
    to_edges = _sobel_edges(to_image, to_valid)
    kp1, des1 = sift.detectAndCompute(from_edges, None)
    kp2, des2 = sift.detectAndCompute(to_edges, None)

    bf = cv2.BFMatcher(cv2.NORM_L2, crossCheck=False)
    matches_12 = bf.knnMatch(des1, des2, k=2)
    matches_21 = bf.knnMatch(des2, des1, k=2)

    def _ratio_test(matches, ratio):
        good = {}
        for pair in matches:
            if len(pair) < _KNN_NEIGHBORS_FOR_RATIO_TEST:
                continue
            m, n = pair
            if m.distance < ratio * n.distance:
                good[(m.queryIdx, m.trainIdx)] = m
        return good

    good_12 = _ratio_test(matches_12, ratio)
    good_21 = _ratio_test(matches_21, ratio)
    symmetric = [m for (q, t), m in good_12.items() if (t, q) in good_21]

    pts1 = np.array([kp1[m.queryIdx].pt for m in symmetric], dtype=np.float32)
    pts2 = np.array([kp2[m.trainIdx].pt for m in symmetric], dtype=np.float32)
    if len(pts1) < _MIN_POINTS_FOR_HOMOGRAPHY:
        return pts1, pts2

    _, mask_h = cv2.findHomography(pts1, pts2, cv2.RANSAC, ransac_reproj_threshold_px)
    inliers_h = mask_h.ravel().astype(bool)
    pts1_h, pts2_h = pts1[inliers_h], pts2[inliers_h]
    if len(pts1_h) < _MIN_POINTS_FOR_FUNDAMENTAL_MATRIX:
        return pts1_h, pts2_h

    _, mask_f = cv2.findFundamentalMat(pts1_h, pts2_h, cv2.FM_RANSAC, ransac_reproj_threshold_px, 0.99)
    if mask_f is None:
        return pts1_h, pts2_h
    inliers_f = mask_f.ravel().astype(bool)
    return pts1_h[inliers_f], pts2_h[inliers_f]


@functools.cache
def _lightglue_models() -> tuple[lightglue.DISK, lightglue.LightGlue]:
    """Constructs (and, via `functools.cache`, memoizes process-wide) the DISK extractor + LightGlue
    matcher -- both load real pretrained weights over the network on first use (see
    docs/data-sources.md's "LightGlue tie-point matching" section), so this avoids
    re-downloading/re-initializing them on every `match_features_lightglue` call within one process.

    DISK, not SuperPoint (LightGlue's more commonly-used pairing): SuperPoint's inference code and
    pretrained weights carry a proprietary-style notice, not a standard permissive license -- see
    docs/data-sources.md for the full reasoning behind this choice."""
    return lightglue.DISK(max_num_keypoints=2048).eval(), lightglue.LightGlue(features="disk").eval()


def match_features_lightglue(
    from_image: np.ndarray,
    from_valid: np.ndarray,
    to_image: np.ndarray,
    to_valid: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Matches `from_image` against `to_image` (both `to_uint8_for_matching`'s output) via DISK
    (deep-learned local features) + LightGlue (a learned, attention-based matcher) instead of
    `match_features`'s classical SIFT+ratio-test+RANSAC pipeline -- tried specifically to push match
    count/quality higher for more challenging future EDRs (shadowed terrain, low texture) than SIFT
    can reliably deliver. Unlike `match_features`, this doesn't run the raw uint8 images through
    `_sobel_edges` first: DISK/LightGlue are deep features trained directly on natural RGB/grayscale
    imagery (not edge maps), and are already designed to be more robust to cross-sensor appearance
    changes than classical descriptors, so feeding them the same edge-filtered input `match_features`
    needs would be fighting what they were actually trained on, not helping them -- an empirical
    question worth revisiting if match quality doesn't hold up in practice, not a settled one.

    Also unlike `match_features`, this doesn't run its own homography/fundamental-matrix RANSAC
    verification pass afterward -- LightGlue is specifically designed to output high-precision
    matches directly (its own `filter_threshold`, not a separate geometric check, is what LightGlue's
    own paper reports doing that job), and every caller of this function's output already runs its
    own RANSAC when fitting a correction (`fit_similarity_correction`/`fit_affine_correction`/
    `fit_homography_correction`), so a redundant geometric-verification pass here would just be
    duplicated work, not additional safety.

    Returns `(from_points_px, to_points_px)`, same contract as `match_features` (same-length arrays
    of matched `(x, y)` pixel coordinates in each input image's own pixel space) -- a drop-in
    alternative anywhere `match_features` is used."""
    extractor, matcher = _lightglue_models()

    with torch.no_grad():
        feats0 = extractor.extract(lightglue.utils.numpy_image_to_torch(from_image))
        feats1 = extractor.extract(lightglue.utils.numpy_image_to_torch(to_image))
        matches01 = matcher({"image0": feats0, "image1": feats1})

    feats0, feats1, matches01 = (lightglue.utils.rbd(x) for x in (feats0, feats1, matches01))
    matches = matches01["matches"].numpy()
    pts0 = feats0["keypoints"].numpy()[matches[:, 0]]
    pts1 = feats1["keypoints"].numpy()[matches[:, 1]]

    # Defensive valid-pixel filter, same concern `_sobel_edges`'s own masking addresses for SIFT: a
    # keypoint can still land right at a nodata/padding boundary even though the network was fed
    # already-zeroed invalid pixels.
    rows0 = np.clip(pts0[:, 1].round().astype(int), 0, from_valid.shape[0] - 1)
    cols0 = np.clip(pts0[:, 0].round().astype(int), 0, from_valid.shape[1] - 1)
    rows1 = np.clip(pts1[:, 1].round().astype(int), 0, to_valid.shape[0] - 1)
    cols1 = np.clip(pts1[:, 0].round().astype(int), 0, to_valid.shape[1] - 1)
    valid = from_valid[rows0, cols0] & to_valid[rows1, cols1]
    return pts0[valid].astype(np.float32), pts1[valid].astype(np.float32)


def pixel_points_to_map(points_px: np.ndarray, transform: rasterio.Affine) -> np.ndarray:
    """Converts `(x, y)` pixel coordinates (as returned by `match_features`) to real map coordinates
    via `transform` (a raster's own `rasterio` affine transform) -- the necessary step before
    comparing points from two different rasters, since two independently-cropped rasters don't share
    a pixel origin even when they share a CRS/scale."""
    xs, ys = transform * (points_px[:, 0], points_px[:, 1])
    return np.stack([xs, ys], axis=1).astype("float64")


def fit_similarity_correction(
    from_points_map: np.ndarray, to_points_map: np.ndarray, ransac_threshold_m: float = 300.0
) -> tuple[affine.Affine, np.ndarray, np.ndarray]:
    """Fits a similarity transform (translation + rotation + uniform scale -- 4 degrees of freedom,
    via `cv2.estimateAffinePartial2D`'s own internal RANSAC) that maps `from_points_map` onto
    `to_points_map`, both real map-coordinate arrays (see `pixel_points_to_map`). Similarity, not a
    full 8-DOF homography (`match_features`'s own internal RANSAC passes use homography/epipolar
    models, appropriate *there* for match verification, not for this fit): deliberately the
    *simplest* plausible correction model to start from, not asserted as the physically "correct"
    one -- a real camera pose error has 6 degrees of freedom before even accounting for this being a
    pushframe sensor's extended-exposure capture (potentially more, if pose drifts during the
    exposure), and the mapping from those onto a 2D map-space distortion isn't simple or
    one-to-one, so there's no first-principles case that similarity (or any fixed DOF count) is
    exactly right. Start simple for interpretability; escalate to a richer model (`fit_affine_correction`,
    `fit_homography_correction`) only if there are enough independent, well-distributed tie points to
    support it without just overfitting noise -- an empirical question, not one this function decides
    (Phase 53's native-resolution downsampling raised the default candidate's inlier count from 53 to
    91, motivating the first real attempt at those richer models -- see `docs/history.md`'s dated
    entry for what that comparison found).

    Returns `(correction, inlier_mask, residuals_m)`: `correction` is an `affine.Affine` mapping
    `from_points_map`-space coordinates onto the fitted `to_points_map`-space location (apply it to
    a raster's own transform via `apply_correction`); `inlier_mask` marks which input points RANSAC
    accepted; `residuals_m` is each point's real distance (meters, or whatever unit the input map
    coordinates are in) from where the fitted transform predicts it should land, for *all* input
    points including outliers (so callers can inspect how bad the rejected points really are, not
    just how good the accepted ones are)."""
    src = from_points_map.astype("float32")
    dst = to_points_map.astype("float32")
    matrix, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=ransac_threshold_m)
    correction = affine.Affine(*matrix[0], *matrix[1])
    predicted = (matrix[:, :2] @ from_points_map.T).T + matrix[:, 2]
    residuals_m = np.linalg.norm(predicted - to_points_map, axis=1)
    return correction, inliers.ravel().astype(bool), residuals_m


def fit_affine_correction(
    from_points_map: np.ndarray, to_points_map: np.ndarray, ransac_threshold_m: float = 300.0
) -> tuple[affine.Affine, np.ndarray, np.ndarray]:
    """Fits a full affine transform (6 degrees of freedom -- translation, rotation, independent x/y
    scale, and shear, via `cv2.estimateAffine2D`'s own internal RANSAC) mapping `from_points_map`
    onto `to_points_map`. A strictly richer model than `fit_similarity_correction`'s 4-DOF fit (same
    inputs/RANSAC threshold convention, same `affine.Affine` return type, so it's a drop-in
    alternative -- `apply_correction` composes with either identically, since it never inspects which
    of an `affine.Affine`'s 6 possible degrees of freedom are actually non-trivial). See
    `fit_similarity_correction`'s docstring for why *some* fixed model is used at all despite none
    being asserted as physically "correct"."""
    src = from_points_map.astype("float32")
    dst = to_points_map.astype("float32")
    matrix, inliers = cv2.estimateAffine2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=ransac_threshold_m)
    correction = affine.Affine(*matrix[0], *matrix[1])
    predicted = (matrix[:, :2] @ from_points_map.T).T + matrix[:, 2]
    residuals_m = np.linalg.norm(predicted - to_points_map, axis=1)
    return correction, inliers.ravel().astype(bool), residuals_m


def fit_homography_correction(
    from_points_map: np.ndarray, to_points_map: np.ndarray, ransac_threshold_m: float = 300.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Fits a full projective homography (8 degrees of freedom, via `cv2.findHomography`'s own
    internal RANSAC) mapping `from_points_map` onto `to_points_map` -- the richest of this module's
    three correction models (see `fit_similarity_correction`'s docstring for why none of them is
    asserted as the physically "correct" one). Unlike `fit_similarity_correction`/
    `fit_affine_correction`, a homography's bottom row isn't `[0, 0, 1]` (it's projective, not
    affine), so it isn't representable as an `affine.Affine` at all -- apply it via
    `apply_homography_correction`, not `apply_correction`.

    Returns `(homography, inlier_mask, residuals_m)`: `homography` is the raw 3x3 matrix
    `cv2.findHomography` returns, mapping a homogeneous `[x, y, 1]` map coordinate to another
    homogeneous `[x', y', w]` one (`predicted = (homography @ [x, y, 1]) / w`, matching the
    convention `apply_homography_correction`'s own pixel-space composition relies on)."""
    src = from_points_map.astype("float64")
    dst = to_points_map.astype("float64")
    homography, mask = cv2.findHomography(src, dst, cv2.RANSAC, ransac_threshold_m)
    src_h = np.hstack([src, np.ones((len(src), 1))])
    projected = (homography @ src_h.T).T
    predicted = projected[:, :2] / projected[:, 2:3]
    residuals_m = np.linalg.norm(predicted - dst, axis=1)
    return homography, mask.ravel().astype(bool), residuals_m


def apply_correction(src_raster_path, correction: affine.Affine, out_path) -> Path:
    """Applies `correction` (from `fit_similarity_correction`) to `src_raster_path`'s own real
    georeferencing and resamples it back onto its own original pixel grid -- so the output drops
    into `plotting.plot_overlay`/`plot_overlay_toggle` exactly like the uncorrected raster did, no
    further plumbing changes needed. Composes `correction` with the source's existing transform
    (`correction * src_transform`, i.e. "first go from pixel to the original, possibly-wrong map
    position, then apply the fitted correction") and resamples via `rasterio.warp.reproject`, not a
    bare metadata edit: a metadata-only fix would be valid for a translation-only correction (the
    pixel grid stays rectilinear), but a real fitted rotation/scale component would leave the raster's
    own affine transform non-rectilinear in a way `plotting.py`'s `rioxarray`/`xarray`-based display
    path isn't set up to handle (it assumes a plain north-up grid throughout this project, matching
    every other raster this pipeline produces) -- reprojecting onto the original grid keeps that
    assumption true regardless of what the fitted correction turns out to contain."""
    with rasterio.open(src_raster_path) as src:
        data = src.read(1)
        src_transform = src.transform
        src_crs = src.crs
        nodata = src.nodata if src.nodata is not None else _ISIS_FLOAT_NODATA
        corrected_src_transform = correction * src_transform

        out = np.full_like(data, nodata)
        rasterio.warp.reproject(
            source=data,
            destination=out,
            src_transform=corrected_src_transform,
            src_crs=src_crs,
            dst_transform=src_transform,
            dst_crs=src_crs,
            src_nodata=nodata,
            dst_nodata=nodata,
            resampling=rasterio.warp.Resampling.bilinear,
        )
        profile = src.profile.copy()
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(out, 1)
    return out_path


def apply_homography_correction(src_raster_path, homography: np.ndarray, out_path) -> Path:
    """Applies `homography` (from `fit_homography_correction`) to `src_raster_path`'s own real
    georeferencing and resamples back onto its original pixel grid -- the homography counterpart to
    `apply_correction` (which only handles `affine.Affine` corrections, since it composes two affines
    directly). A homography can't be composed with a raster's affine transform that way, and
    `rasterio.warp.reproject` has no projective-transform mode -- instead, this builds the single
    pixel-space projective matrix equivalent to "pixel -> map (`src_transform`) -> corrected map
    (`homography`) -> map (`src_transform`'s own inverse)" and warps directly via
    `cv2.warpPerspective`, which does understand a full 3x3 projective matrix. `src_transform`'s own
    2x3 affine coefficients are lifted to a 3x3 homogeneous matrix (bottom row `[0, 0, 1]`) purely to
    make that composition well-defined -- the *result*, `pixel_homography`, is genuinely projective in
    general, unlike either of its two affine ends.

    Same output semantics as `apply_correction`: the corrected raster lands back on its own original
    pixel grid, so it drops straight into `plotting.plot_overlay`/`plot_overlay_toggle` unchanged."""
    with rasterio.open(src_raster_path) as src:
        data = src.read(1)
        src_transform = src.transform
        nodata = src.nodata if src.nodata is not None else _ISIS_FLOAT_NODATA
        profile = src.profile.copy()

    src_matrix = np.array(
        [
            [src_transform.a, src_transform.b, src_transform.c],
            [src_transform.d, src_transform.e, src_transform.f],
            [0.0, 0.0, 1.0],
        ]
    )
    pixel_homography = np.linalg.inv(src_matrix) @ homography @ src_matrix
    out = cv2.warpPerspective(
        data,
        pixel_homography,
        (data.shape[1], data.shape[0]),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=float(nodata),
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(out, 1)
    return out_path
