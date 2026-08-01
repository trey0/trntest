from datetime import UTC, datetime
from unittest import mock

from trntest import camera
from trntest.config import TrntestConfig
from trntest.session import Session


def test_session_uses_provided_config():
    config = TrntestConfig(target_frame_index=123)
    session = Session(config=config)
    assert session.config is config


def test_session_defaults_config_via_load_config():
    with mock.patch("trntest.session.load_config") as mock_load:
        mock_load.return_value = TrntestConfig()
        session = Session()
        mock_load.assert_called_once()
        assert session.config == TrntestConfig()


def test_build_camera_delegates_with_config():
    config = TrntestConfig()
    session = Session(config=config)
    with mock.patch("trntest.session.camera.build_camera") as mock_fn:
        mock_fn.return_value = "camera-sentinel"
        result = session.build_camera(output_tsai_path="/tmp/x.tsai")
    mock_fn.assert_called_once_with(config=config, output_tsai_path="/tmp/x.tsai")
    assert result == "camera-sentinel"


def test_fetch_dem_and_ortho_delegates_with_config():
    config = TrntestConfig()
    session = Session(config=config)
    with mock.patch("trntest.session.lunaserv.fetch_dem_and_ortho") as mock_fn:
        mock_fn.return_value = "lunaserv-sentinel"
        result = session.fetch_dem_and_ortho("camera-sentinel")
    mock_fn.assert_called_once_with("camera-sentinel", config=config)
    assert result == "lunaserv-sentinel"


def test_run_sat_sim_delegates_with_config():
    config = TrntestConfig()
    session = Session(config=config)
    with mock.patch("trntest.session.render.run_sat_sim") as mock_fn:
        mock_fn.return_value = "render-sentinel"
        result = session.run_sat_sim("camera-sentinel", "lunaserv-sentinel")
    mock_fn.assert_called_once_with("camera-sentinel", "lunaserv-sentinel", config=config)
    assert result == "render-sentinel"


def test_fetch_frame_timing_delegates_with_config():
    config = TrntestConfig()
    session = Session(config=config)
    with mock.patch("trntest.session.camera.fetch_frame_timing") as mock_fn:
        mock_fn.return_value = "timing-sentinel"
        result = session.fetch_frame_timing()
    mock_fn.assert_called_once_with(config=config)
    assert result == "timing-sentinel"


def test_compute_tie_points_delegates_with_swapped_arg_order():
    """Session standardizes on (camera, frame_timing), but the underlying free function keeps its
    original (frame_timing, camera) order -- this test pins that translation."""
    config = TrntestConfig()
    session = Session(config=config)
    with mock.patch("trntest.session.tie_points.compute_tie_points") as mock_fn:
        mock_fn.return_value = "tie-points-sentinel"
        result = session.compute_tie_points("camera-sentinel", "timing-sentinel")
    mock_fn.assert_called_once_with("timing-sentinel", "camera-sentinel", config=config)
    assert result == "tie-points-sentinel"


def test_compute_display_rotations_delegates_with_config():
    config = TrntestConfig()
    session = Session(config=config)
    with mock.patch("trntest.session.orientation.compute_display_rotations") as mock_fn:
        mock_fn.return_value = "rotations-sentinel"
        result = session.compute_display_rotations("camera-sentinel", "timing-sentinel")
    mock_fn.assert_called_once_with("camera-sentinel", "timing-sentinel", config=config)
    assert result == "rotations-sentinel"


def test_fetch_vis_mosaic_delegates_with_config():
    config = TrntestConfig()
    session = Session(config=config)
    with mock.patch("trntest.session.wac.fetch_vis_mosaic") as mock_fn:
        mock_fn.return_value = "mosaic-sentinel"
        result = session.fetch_vis_mosaic("camera-sentinel")
    mock_fn.assert_called_once_with("camera-sentinel", config=config)
    assert result == "mosaic-sentinel"


def test_fetch_and_furnish_delegates_with_config():
    config = TrntestConfig()
    session = Session(config=config)
    target_dt = datetime(2019, 11, 30, tzinfo=UTC)
    with mock.patch("trntest.session.spice_kernels.fetch_and_furnish") as mock_fn:
        mock_fn.return_value = ["a", "b"]
        result = session.fetch_and_furnish(target_dt)
    mock_fn.assert_called_once_with(target_dt, config=config)
    assert result == ["a", "b"]


def test_methods_inherit_docstrings():
    assert Session.build_camera.__doc__ is not None
    assert Session.build_camera.__doc__ == camera.build_camera.__doc__
