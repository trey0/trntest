from pathlib import Path

import pytest

from trntest.config import TrntestConfig, load_config


def test_defaults_when_nothing_resolved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRNTEST_CONFIG", raising=False)
    monkeypatch.delenv("TRNTEST_CACHE_ROOT", raising=False)
    monkeypatch.delenv("TRNTEST_OUTPUT_DIR", raising=False)

    config = load_config()

    assert config == TrntestConfig()


def test_explicit_path_wins_over_everything(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / "env.toml"
    env_file.write_text("dem_target_gsd_m = 10.0\n")
    monkeypatch.setenv("TRNTEST_CONFIG", str(env_file))
    (tmp_path / "trntest.toml").write_text("dem_target_gsd_m = 20.0\n")

    explicit_file = tmp_path / "explicit.toml"
    explicit_file.write_text("dem_target_gsd_m = 30.0\n")

    config = load_config(explicit_file)

    assert config.dem_target_gsd_m == 30.0


def test_env_var_config_path_wins_over_cwd_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / "env.toml"
    env_file.write_text("dem_target_gsd_m = 10.0\n")
    monkeypatch.setenv("TRNTEST_CONFIG", str(env_file))
    (tmp_path / "trntest.toml").write_text("dem_target_gsd_m = 20.0\n")

    config = load_config()

    assert config.dem_target_gsd_m == 10.0


def test_cwd_trntest_toml_used_when_no_explicit_or_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRNTEST_CONFIG", raising=False)
    (tmp_path / "trntest.toml").write_text('dem_target_gsd_m = 20.0\ncache_root = "/tmp/cache"\n')

    config = load_config()

    assert config.dem_target_gsd_m == 20.0
    assert config.cache_root == Path("/tmp/cache")


def test_cache_root_env_var_overrides_resolved_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRNTEST_CONFIG", raising=False)
    monkeypatch.setenv("TRNTEST_CACHE_ROOT", "/override/cache")

    config = load_config()

    assert config.cache_root == Path("/override/cache")


def test_output_dir_env_var_overrides_resolved_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRNTEST_CONFIG", raising=False)
    monkeypatch.setenv("TRNTEST_OUTPUT_DIR", "/override/output")

    config = load_config()

    assert config.output_dir == Path("/override/output")


def test_unknown_key_raises(tmp_path):
    bad_file = tmp_path / "bad.toml"
    bad_file.write_text("not_a_real_field = 1\n")

    with pytest.raises(ValueError, match="not_a_real_field"):
        load_config(bad_file)


def test_moon_radius_m_derived_from_km():
    config = TrntestConfig(moon_radius_km=1000.0)
    assert config.moon_radius_m == 1_000_000.0
