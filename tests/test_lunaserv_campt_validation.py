"""Validates `hapke._terrain_photometric_angles`'s ellipsoid limit (`dem` all zero) against real
ISIS `campt` ground truth -- independent confirmation, via a completely different geometry engine,
that Phase 71's normal-tilt fix (unconditional since Phase 72 -- no opt-out parameter any more) is
correct and not, e.g., double-counting the curvature correction already applied to `ground`'s own
position. See
`_terrain_photometric_angles`'s own docstring and `docs/history.md`'s Phase 70/71 entries for the
full rationale -- this file is the concrete `heavy` test those entries point to.

Marked `@pytest.mark.heavy`: needs live network access (the real candidate's own DEM/ortho/SPICE
fetch) *and* a real ISIS toolchain (`gdal_translate`, `csminit`, `campt`, and -- unlike every other
ISIS-touching test in this project -- an actually-installed `usgscsm` CSM plugin, added to
`docker/Dockerfile`'s `isis` conda env specifically for this test in Phase 71). Every other
`campt`-touching test in this project (`test_isis_campt.py`) mocks the subprocess call;
this one really shells out, against a real rendered image's own real CSM camera.
"""

from pathlib import Path

import numpy as np
import pytest

import trntest
from trntest import hapke, illumination, isis_campt, render, tie_points
from trntest.subprocess_utils import run_quiet

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.heavy
def test_terrain_photometric_angles_ellipsoid_limit_matches_real_campt_ground_truth(tmp_path):
    # Same minimal setup `notebooks/hapke_hillshade.py`/`image_generation.py` use -- the project's
    # own real, frozen default candidate, not a synthetic fixture (this test's whole point is to
    # validate against ISIS's own real geometry engine, so it needs a real render with a real camera
    # model attached).
    session = trntest.Session()
    images = trntest.read_manifest(_REPO_ROOT / "notebooks" / "dataset_manifest.csv")
    dataset = trntest.TrnTestDataSet.create(session.config.output_dir / "trn_dataset", images, session.config)
    entry = dataset[0]
    camera = entry.camera
    dem_ortho_result = entry.dem_ortho_result

    center = camera.footprint_lonlat_deg["center"]
    assert center is not None, "candidate's own boresight must intersect the Moon"
    center_lon_deg, center_lat_deg = center

    # Real render + its own real CSM camera state -- `cam_gen`'s conversion never populates the sun
    # position (ASP's own tools don't need it), so `render.patch_sun_position` fills it in from the
    # same real ephemeris `illumination.sun_azimuth_elevation_deg` itself uses (Phase 70/71).
    render_result = render.run_sat_sim(camera, dem_ortho_result, entry.per_image_config)
    render.patch_sun_position(render_result.csm_json, camera.et)

    # Attach that real camera model to an ISIS cube -- `gdal_translate -of ISIS3` then
    # `csminit from=... state=...` (not `isd=` -- see `render.patch_sun_position`'s own docstring for
    # why `state=` is the parameter that actually wants a `cam_gen`-style CSM state file). No
    # `shapemodel=` argument -- ellipsoid mode, matching this test's `dem=zeros` comparison.
    csm_cub_path = tmp_path / "campt_validation.cub"
    run_quiet(["gdal_translate", "-of", "ISIS3", str(render_result.rendered_tif), str(csm_cub_path)])
    run_quiet(["csminit", f"from={csm_cub_path}", f"state={render_result.csm_json}"])

    # 5 sparse sample points (die's-5 pattern, this project's own established convention for
    # control-point-style validation -- see `tie_points.die5_points`/`select_tie_points`), inscribed
    # within the *camera's own real footprint* (not the DEM/ortho fetch's own padded AOI, which is
    # deliberately larger than the actual rendered image -- confirmed live: a first attempt using that
    # padded bbox picked points genuinely outside the render's own FOV, which real `campt` correctly
    # refused to project). Mirrors `select_tie_points`'s exact approach for a single footprint.
    synthetic_corners_m = tie_points._footprint_to_local_m(camera.footprint_lonlat_deg, center_lon_deg, center_lat_deg)
    inscribed_m = tie_points.inscribed_bbox(synthetic_corners_m, synthetic_corners_m["center"])
    points_m = tie_points.die5_points(inscribed_m, synthetic_corners_m["center"])
    points_lonlat = tie_points._local_m_to_lonlat(points_m, center_lon_deg, center_lat_deg)

    # Our own function, at the ellipsoid limit (flat `dem`), over the exact same bbox/grid as the
    # real DEM/ortho fetch -- production cellsize (`config.dem_target_gsd_m`), not a coarser
    # synthetic one, so this exercises `np.gradient`'s real discretization error at the same
    # resolution the docstring's own residual figure was measured at. Calls the *public*
    # `real_geometry_photometric_angles` (not the private `_terrain_photometric_angles`) -- since
    # Phase 77 (docs/history.md's dated entry) that function is fully MOON_ME-native and needs no
    # local-frame camera-position conversion at all, so this exercises the real end-to-end path
    # exactly as every other caller uses it, not a hand-built intermediate.
    azimuth_deg, elevation_deg = illumination.sun_azimuth_elevation_deg(center_lon_deg, center_lat_deg, camera.et)
    flat_dem = np.zeros((dem_ortho_result.height, dem_ortho_result.width))
    incidence_deg, emission_deg, phase_deg = hapke.real_geometry_photometric_angles(
        flat_dem,
        dem_ortho_result.bbox,
        camera,
        azimuth_deg,
        elevation_deg,
        cellsize_m=entry.per_image_config.dem_target_gsd_m,
        along_track_correction=False,
    )

    minx, miny, maxx, maxy = dem_ortho_result.bbox
    width, height = dem_ortho_result.width, dem_ortho_result.height
    diffs_deg = []
    for name, (x, y) in points_m.items():
        col = int(round((x - minx) / (maxx - minx) * width - 0.5))
        row = int(round((maxy - y) / (maxy - miny) * height - 0.5))
        lon_deg, lat_deg = points_lonlat[name]

        real = isis_campt.campt_photometric_angles(csm_cub_path, lon_deg, lat_deg)
        assert real is not None, f"real campt failed to project sample point {name!r} into the render"
        phase_real_deg, incidence_real_deg, emission_real_deg = real

        diffs_deg.append(abs(incidence_deg[row, col] - incidence_real_deg))
        diffs_deg.append(abs(emission_deg[row, col] - emission_real_deg))
        diffs_deg.append(abs(phase_deg[row, col] - phase_real_deg))

    print(f"max |diff| against real campt: {max(diffs_deg):.6f} deg (of {len(diffs_deg)} angle comparisons)")

    # Expected residual budget (not zero): `np.gradient`'s central-difference discretization error at
    # production resolution (docstring's own ~0.0017 deg figure), plus treating the sun as one
    # scene-wide direction rather than a true per-point vector (bounded by footprint-size/sun-distance
    # -- a small fraction of a degree at this project's real candidate footprint sizes). A residual
    # meaningfully above this means something real is being missed -- investigate, don't loosen this.
    # Observed live (Phase 71, docs/history.md): max |diff| ~0.018 deg across 15 angle comparisons (5
    # points x phase/incidence/emission) -- 0.05 leaves a real margin without masking a real bug.
    assert max(diffs_deg) < 0.05
