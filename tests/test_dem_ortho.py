import json

from trntest import dem_ortho


def test_ortho_shaded_filename_no_hapke_ignores_other_flags():
    # Default `ortho_source` is now "wac_emp_pds" (2026-08-23, docs/history.md's dated entry) -- every
    # filename gets the `_wacemp` suffix unless a caller explicitly asks for the deprecated
    # "lunaserv_wms" source (see `test_ortho_shaded_filename_lunaserv_wms_matches_pre_migration_name`).
    assert dem_ortho.ortho_shaded_filename(False) == "ortho_shaded_wacemp.tif"
    no_hapke = dem_ortho.ortho_shaded_filename(False, along_track_correction=True, real_hapke_params=True)
    assert no_hapke == "ortho_shaded_wacemp.tif"


def test_ortho_shaded_filename_matches_todays_defaults():
    # All-defaults call must resolve to exactly the file `fetch_dem_and_ortho`'s own defaults would
    # produce. `_normaltilt` is a permanent, unconditional part of this filename since Phase 72 (no
    # parameter controls it any more -- see `ortho_shaded_filename`'s own docstring for why it's kept
    # anyway), so this is deliberately not the pre-Phase-70 filename (see
    # `test_ortho_shaded_filename_real_params_false_matches_pre_phase_69` below for that backward-
    # compat guarantee, which still applies to `real_hapke_params` specifically).
    assert dem_ortho.ortho_shaded_filename(True) == "ortho_shaded_hapke_atc_realparams_normaltilt_wacemp.tif"


def test_ortho_shaded_filename_lunaserv_wms_matches_pre_migration_name():
    # Backward-compat check: real cached files from before the WAC_EMP-PDS migration (2026-08-23) must
    # still resolve under `ortho_source="lunaserv_wms"` -- no `_wacemp` suffix, exact pre-migration name.
    assert dem_ortho.ortho_shaded_filename(False, ortho_source="lunaserv_wms") == "ortho_shaded.tif"
    assert dem_ortho.ortho_shaded_filename(True, ortho_source="lunaserv_wms") == (
        "ortho_shaded_hapke_atc_realparams_normaltilt.tif"
    )


def test_ortho_shaded_filename_real_params_false_matches_pre_phase_69():
    # Backward-compat check: existing cached files from before `real_hapke_params` existed (when
    # `hapke`/`along_track_correction` were the only toggles) must still resolve to the same name
    # under an explicit `real_hapke_params=False` -- plus the now-permanent `_normaltilt` suffix
    # (Phase 72), which every `hapke=True` filename gets regardless of any parameter now.
    assert dem_ortho.ortho_shaded_filename(True, real_hapke_params=False) == (
        "ortho_shaded_hapke_atc_normaltilt_wacemp.tif"
    )
    assert dem_ortho.ortho_shaded_filename(True, along_track_correction=False, real_hapke_params=False) == (
        "ortho_shaded_hapke_normaltilt_wacemp.tif"
    )


def test_ortho_shaded_filename_real_params_suffix():
    assert dem_ortho.ortho_shaded_filename(True, along_track_correction=True, real_hapke_params=True) == (
        "ortho_shaded_hapke_atc_realparams_normaltilt_wacemp.tif"
    )
    assert dem_ortho.ortho_shaded_filename(True, along_track_correction=False, real_hapke_params=True) == (
        "ortho_shaded_hapke_realparams_normaltilt_wacemp.tif"
    )


def test_ortho_shaded_filename_normaltilt_suffix_always_present_when_hapke_true():
    # No parameter controls this any more (Phase 72, see `ortho_shaded_filename`'s own docstring) --
    # `_normaltilt` is simply always appended whenever `hapke=True`, even with every other flag off.
    assert dem_ortho.ortho_shaded_filename(True, along_track_correction=False, real_hapke_params=False) == (
        "ortho_shaded_hapke_normaltilt_wacemp.tif"
    )


# -- dem_footprint_matches (principle-1 verification for the dem_filled single-answer artifact) ---


def test_dem_footprint_meta_path_swaps_the_tif_suffix(tmp_path):
    dem_path = tmp_path / dem_ortho.DEM_FILLED_FILENAME
    assert dem_ortho.dem_footprint_meta_path(dem_path) == tmp_path / "dem_filled-tile-0.footprint.json"


def test_dem_footprint_matches_true_when_no_sidecar_exists(tmp_path):
    """An already-on-disk `dem_filled` from before this check existed (no sidecar at all) is
    trusted as-is, not forced to refetch -- see `dem_footprint_matches`'s own docstring."""
    dem_path = tmp_path / dem_ortho.DEM_FILLED_FILENAME
    dem_path.write_text("fake dem bytes")

    assert dem_ortho.dem_footprint_matches(dem_path, {"center": (1.0, 2.0)}) is True
    assert dem_ortho.dem_footprint_matches(dem_path, None) is True


def test_dem_footprint_matches_true_for_an_identical_recorded_value(tmp_path):
    dem_path = tmp_path / dem_ortho.DEM_FILLED_FILENAME
    footprint = {"center": (1.5, -2.5), "ul": (0.5, -1.5)}
    dem_ortho.dem_footprint_meta_path(dem_path).write_text(json.dumps({"extra_footprint_lonlat_deg": footprint}))

    assert dem_ortho.dem_footprint_matches(dem_path, footprint) is True


def test_dem_footprint_matches_true_when_both_sides_are_none(tmp_path):
    dem_path = tmp_path / dem_ortho.DEM_FILLED_FILENAME
    dem_ortho.dem_footprint_meta_path(dem_path).write_text(json.dumps({"extra_footprint_lonlat_deg": None}))

    assert dem_ortho.dem_footprint_matches(dem_path, None) is True


def test_dem_footprint_matches_false_on_a_real_mismatch(tmp_path):
    dem_path = tmp_path / dem_ortho.DEM_FILLED_FILENAME
    recorded = {"center": (1.5, -2.5)}
    dem_ortho.dem_footprint_meta_path(dem_path).write_text(json.dumps({"extra_footprint_lonlat_deg": recorded}))

    assert dem_ortho.dem_footprint_matches(dem_path, {"center": (9.9, 9.9)}) is False
    assert dem_ortho.dem_footprint_matches(dem_path, None) is False
