"""Validates `hapke.real_geometry_photometric_angles`'s real, DEM-aware `incidence_deg` field
against Ames Stereo Pipeline's own independently ray-traced incidence angle -- extracted via
`sfs_validation.run_sfs_lambertian_incidence`'s Lambertian-mode inversion trick (`sfs
--reflectance-type 0` with a uniform albedo=1, so its raw `sim-intensity` output is exactly
`exposure * cos(incidence)`, with no Hapke-model dependence at all to confound the comparison).

This is this project's first genuine **DEM-aware** ground-truth check -- Phase 70/73
(`docs/history.md`) found no ISIS tool that gives one: `phocube`'s `LOCAL*` backplanes were
confirmed broken/implausible on this project's own real candidates, and real `campt`'s plain angles
were confirmed to stay ellipsoid-normal-based even with a real DEM shape model attached. `sfs`'s own
independent ray-DEM intersection sidesteps both entirely.

Marked `@pytest.mark.heavy` for the same reasons `test_lunaserv_campt_validation.py` is: needs live
network access (the real candidate's own DEM/ortho/SPICE fetch) and a real ISIS+ASP toolchain
(`gdal_translate`, `csminit`, `usgscsm`, `sfs`) -- not mocked, a real subprocess pipeline against a
real rendered image's own real CSM camera.
"""

from pathlib import Path

import numpy as np
import pytest
import rasterio

import trntest
from trntest import hapke, illumination, sfs_validation

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.heavy
def test_real_geometry_incidence_matches_sfs_lambertian_inversion_across_the_whole_frame():
    # Same minimal setup `test_lunaserv_campt_validation.py`/`notebooks/sfs_validation.py` use -- the
    # project's own real, frozen default candidate.
    session = trntest.Session()
    images = trntest.read_manifest(_REPO_ROOT / "notebooks" / "dataset_manifest.csv")
    dataset = trntest.TrnTestDataSet.create(session.config.output_dir / "trn_dataset", images, session.config)
    entry = dataset[0]
    camera = entry.camera
    dem_ortho_result = entry.dem_ortho_result
    config = entry.per_image_config

    result = sfs_validation.run_sfs_lambertian_incidence(camera, dem_ortho_result, config)
    incidence_sfs_deg = sfs_validation.incidence_deg_from_lambertian_sim_intensity(
        result.sim_intensity_tif, result.exposure
    )
    valid = np.isfinite(incidence_sfs_deg)
    assert valid.sum() > 0, "sfs produced no real-coverage pixels -- something upstream is broken"

    with rasterio.open(dem_ortho_result.dem) as src:
        dem = src.read(1)

    center = camera.footprint_lonlat_deg["center"]
    assert center is not None, "candidate's own boresight must intersect the Moon"
    azimuth_deg, elevation_deg = illumination.sun_azimuth_elevation_deg(*center, camera.et)
    incidence_ours_deg, _emission_deg, _phase_deg = hapke.real_geometry_photometric_angles(
        dem, dem_ortho_result.bbox, camera, azimuth_deg, elevation_deg, config.dem_target_gsd_m
    )

    diff_deg = np.abs(incidence_sfs_deg[valid] - incidence_ours_deg[valid])
    print(
        f"incidence diff over {valid.sum()} real-coverage pixels: "
        f"mean={diff_deg.mean():.4f} deg, max={diff_deg.max():.4f} deg"
    )

    # Expected residual budget (not zero, but tiny): `sfs`'s own ray-DEM-intersection normal and
    # `_terrain_photometric_angles`'s `np.gradient`-based one are two genuinely independent
    # discretizations of the same exact terrain embedding (Phase 76's relief-displacement fix --
    # docs/history.md's dated entry), expected to disagree only at floating-point/interpolation noise
    # level, not real geometric error. Observed live (2026-08-23, docs/history.md): mean|diff|
    # ~0.0005 deg, max|diff| ~0.0005 deg across the whole real-coverage region (not just a handful of
    # sample points) -- before the relief-displacement fix this was ~0.024/~0.51 deg; these thresholds
    # leave a real margin above the current result without masking a genuine regression back toward
    # that old, larger residual.
    assert diff_deg.mean() < 0.005
    assert diff_deg.max() < 0.01
