import affine
import numpy as np
import pytest
import rasterio
import rasterio.transform

from trntest import pose_alignment


def _write_raster(path, data, transform, crs="+proj=ortho +lon_0=0 +lat_0=0 +R=1737400 +units=m +no_defs", nodata=None):
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype=str(data.dtype),
        crs=crs,
        transform=transform,
        nodata=nodata,
    ) as dst:
        dst.write(data, 1)


def _synthetic_texture(size, seed=0):
    """A blobby, SIFT-friendly synthetic texture (sum of random 2D Gaussians) -- real, findable
    structure, not noise, so feature matching on it is reliable and deterministic given a fixed
    seed."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    img = np.zeros((size, size), dtype="float64")
    for _ in range(40):
        cx, cy = rng.uniform(0, size, 2)
        sigma = rng.uniform(4, 12)
        amp = rng.uniform(50, 200)
        img += amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma**2))
    img = np.clip(img, 0, 255)
    return img.astype("uint8")


def test_to_uint8_for_matching_marks_invalid_pixels(tmp_path):
    data = np.full((20, 20), 0.05, dtype="float32")
    data[:5, :5] = -3.4028235e38  # ISIS-style huge-magnitude nodata sentinel
    path = tmp_path / "raster.tif"
    _write_raster(path, data, rasterio.transform.from_origin(0, 20, 1, 1))

    image, valid = pose_alignment.to_uint8_for_matching(path)

    assert image.dtype == np.uint8
    assert not valid[:5, :5].any()
    assert valid[5:, 5:].all()
    assert (image[:5, :5] == 0).all()
    assert image[5:, 5:].max() > 0


def test_crop_to_footprint_crops_to_padded_bounds(tmp_path):
    # A large reference raster covering (0,0) to (100,100) in map units.
    reference = np.full((100, 100), 10, dtype="uint8")
    reference_path = tmp_path / "reference.tif"
    _write_raster(reference_path, reference, rasterio.transform.from_origin(0, 100, 1, 1))

    # A footprint-source raster whose real valid data only covers a known 20x20 sub-region.
    footprint = np.full((100, 100), -3.4028235e38, dtype="float32")
    footprint[40:60, 30:50] = 0.1  # valid region: rows 40-60, cols 30-50 -> map y in [40,60], x in [30,50]
    footprint_path = tmp_path / "footprint.tif"
    _write_raster(footprint_path, footprint, rasterio.transform.from_origin(0, 100, 1, 1))

    out_path = pose_alignment.crop_to_footprint(
        reference_path, footprint_path, tmp_path / "cropped.tif", pad_fraction=0.1
    )

    with rasterio.open(out_path) as src:
        bounds = src.bounds
    # Real footprint bbox is (30, 40, 50, 60) in (minx, miny, maxx, maxy) map coords; padded 10%
    # of a 20x20 box adds 2 units on each side.
    assert bounds.left == pytest.approx(28, abs=1)
    assert bounds.right == pytest.approx(52, abs=1)
    assert bounds.bottom == pytest.approx(38, abs=1)
    assert bounds.top == pytest.approx(62, abs=1)


def test_match_features_recovers_a_known_pixel_shift():
    texture = _synthetic_texture(300)
    to_image = texture
    to_valid = np.ones_like(to_image, dtype=bool)
    # from_image is a genuine 200x200 crop of to_image, offset by a known (dx, dy).
    dx, dy = 35, 20
    from_image = texture[dy : dy + 200, dx : dx + 200]
    from_valid = np.ones_like(from_image, dtype=bool)

    from_points, to_points = pose_alignment.match_features(from_image, from_valid, to_image, to_valid)

    assert len(from_points) >= 4
    implied_shift = np.median(to_points - from_points, axis=0)
    assert implied_shift[0] == pytest.approx(dx, abs=2)
    assert implied_shift[1] == pytest.approx(dy, abs=2)


def test_pixel_points_to_map_applies_the_affine_transform():
    transform = rasterio.transform.from_origin(100, 200, 2, 2)  # pixel (0,0) -> map (100, 200)
    points_px = np.array([[0.0, 0.0], [5.0, 5.0]])

    points_map = pose_alignment.pixel_points_to_map(points_px, transform)

    assert points_map[0] == pytest.approx([100, 200])
    assert points_map[1] == pytest.approx([110, 190])  # +5px*2 east, -5px*2 north (y decreases downward)


def test_fit_similarity_correction_recovers_a_known_transform_and_flags_outliers():
    rng = np.random.default_rng(1)
    from_points = rng.uniform(0, 1000, (30, 2))
    true_transform = affine.Affine.translation(50, -30) * affine.Affine.rotation(5) * affine.Affine.scale(1.02)
    to_points = np.array([true_transform * tuple(p) for p in from_points])

    # Add clear outliers that don't follow the true transform at all.
    to_points_with_outliers = to_points.copy()
    to_points_with_outliers[:5] += rng.uniform(500, 1000, (5, 2))

    correction, inliers, residuals_m = pose_alignment.fit_similarity_correction(
        from_points, to_points_with_outliers, ransac_threshold_m=10.0
    )

    assert not inliers[:5].any()  # the injected outliers are correctly rejected
    assert inliers[5:].all()  # the genuine matches are correctly accepted
    assert residuals_m[5:].max() < 5.0  # tight residuals on the real matches
    assert residuals_m[:5].min() > 100.0  # outliers have real, large residuals, not silently ignored
    # Recovered transform's translation/scale should be close to the true one.
    assert (correction.c, correction.f) == pytest.approx((50, -30), abs=1.0)


class _FakeCamera:
    def __init__(self, cross_track_width_km, km_per_frame):
        self.cross_track_width_km = cross_track_width_km
        self.km_per_frame = km_per_frame


def test_native_wac_gsd_m_returns_the_coarser_axis():
    # Cross-track: 70.4 km / 704 samples = 100 m/px. Along-track: 2.1 km / 14 lines = 150 m/px.
    camera = _FakeCamera(cross_track_width_km=70.4, km_per_frame=2.1)

    assert pose_alignment.native_wac_gsd_m(camera) == pytest.approx(150.0)


def test_downsample_to_gsd_halves_dimensions_and_preserves_bright_region_mean(tmp_path):
    # A 40x40, 1 m/px raster: bright (100) in the left half, dark (0) in the right half.
    data = np.zeros((40, 40), dtype="float32")
    data[:, :20] = 100.0
    transform = rasterio.transform.from_origin(0, 40, 1, 1)
    src_path = tmp_path / "src.tif"
    _write_raster(src_path, data, transform, nodata=-3.4028235e38)

    out_path = pose_alignment.downsample_to_gsd(src_path, target_gsd_m=2.0, out_path=tmp_path / "down.tif")

    with rasterio.open(out_path) as src:
        out = src.read(1)
        assert src.res[0] == pytest.approx(2.0)
    assert out.shape == (20, 20)
    # Area-averaging a uniform 100/0 half-and-half raster should reproduce the same clean split,
    # not blur it into a gradient or a single-value block the way a coarser/wrong filter might.
    assert out[:, :10] == pytest.approx(100.0)
    assert out[:, 10:] == pytest.approx(0.0)


def test_downsample_to_gsd_handles_uint8_raster_with_no_nodata(tmp_path):
    # The basemap ortho this function is also called on (lunaserv.despeckle_and_shade_ortho's
    # output) is uint8 with no nodata tag -- confirmed live to crash if a float sentinel fallback is
    # blindly applied regardless of dtype (GDAL can't represent -3.4e38 in a uint8 buffer).
    data = np.zeros((40, 40), dtype="uint8")
    data[:, :20] = 200
    transform = rasterio.transform.from_origin(0, 40, 1, 1)
    src_path = tmp_path / "src.tif"
    _write_raster(src_path, data, transform, nodata=None)

    out_path = pose_alignment.downsample_to_gsd(src_path, target_gsd_m=2.0, out_path=tmp_path / "down.tif")

    with rasterio.open(out_path) as src:
        out = src.read(1)
    assert out.shape == (20, 20)
    assert out[:, :10] == pytest.approx(200.0, abs=1)
    assert out[:, 10:] == pytest.approx(0.0, abs=1)


def test_downsample_to_gsd_rejects_upsampling(tmp_path):
    data = np.zeros((10, 10), dtype="float32")
    src_path = tmp_path / "src.tif"
    _write_raster(src_path, data, rasterio.transform.from_origin(0, 10, 2, 2))

    with pytest.raises(ValueError):
        pose_alignment.downsample_to_gsd(src_path, target_gsd_m=1.0, out_path=tmp_path / "up.tif")


def test_apply_correction_shifts_a_known_marker_pixel(tmp_path):
    data = np.zeros((50, 50), dtype="float32")
    data[25, 25] = 1.0  # a single bright marker pixel
    transform = rasterio.transform.from_origin(0, 50, 1, 1)
    src_path = tmp_path / "src.tif"
    _write_raster(src_path, data, transform, nodata=0.0)

    correction = affine.Affine.translation(10, -5)  # +10 east, -5 north (map units == pixels here)
    out_path = pose_alignment.apply_correction(src_path, correction, tmp_path / "corrected.tif")

    with rasterio.open(out_path) as src:
        out = src.read(1)
    marker_row, marker_col = np.unravel_index(np.argmax(out), out.shape)
    # The marker's real map position should have moved by the correction; converting the shifted
    # marker's new pixel location back through the (unchanged) output transform confirms it landed
    # at map (25+10, 25-5) = (35, 20) -> pixel (col=35, row=30) in the original 0..50 grid.
    assert (marker_col, marker_row) == (35, 30)
