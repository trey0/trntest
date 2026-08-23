"""End-to-end validation of the WAC_EMP-PDS ortho source (`lunaserv.fetch_dem_and_ortho`'s live
default, `ortho_source="wac_emp_pds"`) against the project's own real, frozen default candidate --
real fetch (the ~1.86GB 304ppd 643nm tile), real reproject onto the local Orthographic working grid,
real Hapke shading, real display stretch. See `docs/history.md`'s dated entry for the migration this
validates and `docs/data-sources.md`'s "WAC_EMP PDS4 archive" section for the data source itself.

Marked `@pytest.mark.heavy`: needs live network access (the real WAC_EMP tile, plus the candidate's
own DEM/SPICE fetch) and a real ISIS toolchain (`photomet` for the Hapke shading) -- not mocked, the
same class of test as `test_lunaserv_campt_validation.py`/`test_sfs_validation_lambertian_incidence.py`.
"""

from pathlib import Path

import numpy as np
import pytest
import rasterio

import trntest
from trntest import lunaserv

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.heavy
def test_fetch_dem_and_ortho_wac_emp_pds_default_produces_a_real_non_saturating_ortho():
    # Same minimal setup every other heavy test in this project uses -- the real, frozen default
    # candidate, not a synthetic fixture.
    session = trntest.Session()
    images = trntest.read_manifest(_REPO_ROOT / "notebooks" / "dataset_manifest.csv")
    dataset = trntest.TrnTestDataSet.create(session.config.output_dir / "trn_dataset", images, session.config)
    entry = dataset[0]

    dem_ortho_result = entry.dem_ortho_result
    assert dem_ortho_result.ortho.name.endswith("_wacemp.tif"), (
        "the live default ortho_source is 'wac_emp_pds' -- the resumed/fetched file must carry the "
        "_wacemp filename suffix (lunaserv.ortho_shaded_filename), not a stale pre-migration name"
    )

    with rasterio.open(dem_ortho_result.ortho) as src:
        shaded = src.read(1)

    assert shaded.dtype == np.uint8
    assert shaded.shape == (dem_ortho_result.height, dem_ortho_result.width)
    # Real physical reflectance (no embedded display stretch, unlike the deprecated Lunaserv WMS DN)
    # relit and stretched through DISPLAY_STRETCH_REFLECTANCE_MIN/MAX -- a real candidate's own
    # coverage should land well inside [0, 255], not pinned at either endpoint throughout.
    assert shaded.min() > 0
    assert shaded.max() < 255
    assert 10.0 < shaded.mean() < 245.0


@pytest.mark.heavy
def test_fetch_dem_and_ortho_wac_emp_pds_lambertian_fallback_is_not_all_black():
    # Regression test for a real bug caught live the first time this migration regenerated
    # notebooks/hapke_hillshade.ipynb (docs/history.md's dated entry): `shade_ortho` (the `hapke=False`
    # Lambertian fallback) is deliberately still DN-`[0, 255]`-only, unchanged by this migration -- but
    # without `despeckle_and_shade_ortho`'s own `stretch_reflectance_to_uint8` pre-stretch (added to
    # fix this), feeding it real WAC_EMP reflectance (~0.05-0.3) produced a fully black, all-zero
    # image (shade_ortho's own /255-then-*255 round trip truncates values this small to 0 under
    # `.astype(np.uint8)`).
    session = trntest.Session()
    images = trntest.read_manifest(_REPO_ROOT / "notebooks" / "dataset_manifest.csv")
    dataset = trntest.TrnTestDataSet.create(session.config.output_dir / "trn_dataset", images, session.config)
    entry = dataset[0]

    # extra_footprint_lonlat_deg=entry.crop_footprint matches entry.dem_ortho_result's own internal
    # call -- without it, this call's smaller camera-only-footprint AOI silently overwrites the
    # *shared* per-candidate dem_filled-tile-0.tif with a differently-sized DEM, corrupting
    # entry.dem_ortho_result's own (larger, crop-unioned) ortho's pairing for any later resumer in the
    # same process/session (the exact bug this same fix addressed in notebooks/hapke_hillshade.py --
    # see docs/history.md's Phase 78 entry -- caught here too by a heavy-suite ordering regression).
    dem_ortho_lambertian = lunaserv.fetch_dem_and_ortho(
        entry.camera, entry.per_image_config, extra_footprint_lonlat_deg=entry.crop_footprint, hapke=False
    )
    assert dem_ortho_lambertian.ortho.name.endswith("_wacemp.tif")

    with rasterio.open(dem_ortho_lambertian.ortho) as src:
        shaded = src.read(1)

    assert shaded.max() > 0, "Lambertian ortho is all-zero -- the display-stretch-before-shade_ortho fix regressed"
    assert (shaded > 0).mean() > 0.5, "Lambertian ortho is mostly black -- likely the same regression, not fully fixed"


@pytest.mark.heavy
def test_wac_emp_tile_id_for_bbox_resolves_the_real_default_candidate():
    # A narrower, faster-to-reason-about live check than the full fetch above: the real default
    # candidate's own footprint resolves to the exact tile this migration's own investigation
    # confirmed exists (docs/data-sources.md) -- catches a tile-grid regression without needing the
    # full ~1.86GB fetch this test alone doesn't trigger (`wac_emp_tile_id_for_bbox` is pure math).
    session = trntest.Session()
    images = trntest.read_manifest(_REPO_ROOT / "notebooks" / "dataset_manifest.csv")
    dataset = trntest.TrnTestDataSet.create(session.config.output_dir / "trn_dataset", images, session.config)
    entry = dataset[0]
    camera = entry.camera
    config = entry.per_image_config

    center = camera.footprint_lonlat_deg["center"]
    assert center is not None
    center_lon, center_lat = center
    bbox_unpadded = lunaserv.footprint_bbox_local_m(camera.footprint_lonlat_deg, center_lon, center_lat)
    bbox = lunaserv.pad_bbox(bbox_unpadded, config.dem_padding_fraction)

    tile_id = lunaserv.wac_emp_tile_id_for_bbox(bbox, center_lon, center_lat, lunaserv.MOON_RADIUS_M)
    assert tile_id == "WAC_EMP_643NM_E300N1350_304P"
