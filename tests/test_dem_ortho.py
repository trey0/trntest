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


# -- dem_filled_filename (closes the DEM filename-collision gap -- see dem_ortho.py's own comment) -


def test_dem_filled_filename_none_matches_the_legacy_bare_name():
    # Backward-compat: TrnTestEntrySpice (no crop footprint to union in) and any pre-existing cached
    # DEM fetched with extra_footprint_lonlat_deg=None must still resolve to the exact same name as
    # before this suffix existed.
    assert dem_ortho.dem_filled_filename(None) == dem_ortho.DEM_FILLED_FILENAME == "dem_filled-tile-0.tif"


def test_dem_filled_filename_ends_in_tile_0_tif():
    # hole_fill_dem's own dem_mosaic convention relies on this exact ending -- see its docstring.
    name = dem_ortho.dem_filled_filename({"center": (1.5, -2.5)})
    assert name.endswith("-tile-0.tif")


def test_dem_filled_filename_is_deterministic_for_an_identical_value():
    footprint = {"center": (1.5, -2.5), "ul": (0.5, -1.5)}
    assert dem_ortho.dem_filled_filename(footprint) == dem_ortho.dem_filled_filename(dict(footprint))


def test_dem_filled_filename_differs_for_different_footprints():
    # The actual fix: two different footprints must resolve to two different files, so they can no
    # longer silently collide on one shared name (the real historical bug documented in
    # test_wac_emp_ortho_source.py's own comment on
    # test_fetch_dem_and_ortho_wac_emp_pds_lambertian_fallback_is_not_all_black).
    a = dem_ortho.dem_filled_filename({"center": (1.5, -2.5)})
    b = dem_ortho.dem_filled_filename({"center": (9.9, 9.9)})
    assert a != b


def test_dem_filled_filename_none_differs_from_a_real_footprint():
    assert dem_ortho.dem_filled_filename(None) != dem_ortho.dem_filled_filename({"center": (1.5, -2.5)})
