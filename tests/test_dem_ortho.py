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
