import numpy as np
import pytest
from scipy.spatial.transform import Rotation

import trntest
from trntest import camera, entry_poses


def test_edr_tsai_filename_matches_build_camera_default():
    assert camera.edr_tsai_filename(437) == "camera_frame437.tsai"


def test_read_tsai_pose_round_trips_write_tsai(tmp_path):
    c_meters = np.array([-1698401.228507, 631765.268248, -129853.740639])
    r_cam_to_me = np.array(
        [
            [-0.350489956, 0.158335022, 0.923085485],
            [-0.934651694, -0.122124750, -0.333933759],
            [0.059858175, -0.979803841, 0.190791595],
        ]
    )
    path = tmp_path / "camera_frame437.tsai"
    camera.write_tsai(path, c_meters, r_cam_to_me, fu=1342.5, fv=1342.5, cu=658.0, cv=658.0)

    got_c, got_r_flat = camera.read_tsai_pose(path)

    np.testing.assert_allclose(got_c, c_meters, atol=1e-5)
    np.testing.assert_allclose(np.array(got_r_flat).reshape(3, 3), r_cam_to_me, atol=1e-8)


def test_read_tsai_pose_matches_a_real_production_tsai_file(tmp_path):
    # A real .tsai written by a real populated trntest1 entry (M1314334685CE) -- confirms
    # read_tsai_pose's line-parsing against the exact on-disk format, not just write_tsai's own
    # round-trip.
    path = tmp_path / "camera_frame437.tsai"
    path.write_text(
        "VERSION_4\n"
        "PINHOLE\n"
        "fu = 1342.522354\n"
        "fv = 1342.522354\n"
        "cu = 658.0\n"
        "cv = 658.0\n"
        "u_direction = 1  0  0\n"
        "v_direction = 0  1  0\n"
        "w_direction = 0  0  1\n"
        "C = -1698401.228507 631765.268248 -129853.740639\n"
        "R = -0.350489956 0.158335022 0.923085485 -0.934651694 -0.122124750 -0.333933759 "
        "0.059858175 -0.979803841 0.190791595\n"
        "pitch = 1\n"
        "NULL\n"
    )

    c_meters, r_flat = camera.read_tsai_pose(path)

    assert c_meters == pytest.approx([-1698401.228507, 631765.268248, -129853.740639])
    assert len(r_flat) == 9
    assert r_flat[0] == pytest.approx(-0.350489956)


def test_read_tsai_pose_missing_c_raises(tmp_path):
    path = tmp_path / "broken.tsai"
    path.write_text("VERSION_4\nPINHOLE\nfu = 1.0\nR = 1 0 0 0 1 0 0 0 1\nNULL\n")
    with pytest.raises(AssertionError, match="'C'"):
        camera.read_tsai_pose(path)


def test_read_tsai_pose_missing_r_raises(tmp_path):
    path = tmp_path / "broken.tsai"
    path.write_text("VERSION_4\nPINHOLE\nfu = 1.0\nC = 1 2 3\nNULL\n")
    with pytest.raises(AssertionError, match="'R'"):
        camera.read_tsai_pose(path)


def test_read_tsai_pose_wrong_length_raises(tmp_path):
    path = tmp_path / "broken.tsai"
    path.write_text("VERSION_4\nPINHOLE\nC = 1 2\nR = 1 0 0 0 1 0 0 0 1\nNULL\n")
    with pytest.raises(AssertionError, match="expected 3"):
        camera.read_tsai_pose(path)


def test_r_cam_to_me_quaternion_xyzw_identity_is_zero_zero_zero_one():
    identity_flat = list(np.eye(3).flatten())
    x, y, z, w = camera.r_cam_to_me_quaternion_xyzw(identity_flat)
    assert (x, y, z, w) == pytest.approx((0.0, 0.0, 0.0, 1.0))


def test_r_cam_to_me_quaternion_xyzw_is_unit_length():
    r = np.array(
        [
            [-0.350489956, 0.158335022, 0.923085485],
            [-0.934651694, -0.122124750, -0.333933759],
            [0.059858175, -0.979803841, 0.190791595],
        ]
    )
    x, y, z, w = camera.r_cam_to_me_quaternion_xyzw(list(r.flatten()))
    assert x * x + y * y + z * z + w * w == pytest.approx(1.0, abs=1e-9)


def test_r_cam_to_me_quaternion_xyzw_round_trips_back_to_the_same_rotation():
    r = np.array(
        [
            [-0.350489956, 0.158335022, 0.923085485],
            [-0.934651694, -0.122124750, -0.333933759],
            [0.059858175, -0.979803841, 0.190791595],
        ]
    )
    quat = camera.r_cam_to_me_quaternion_xyzw(list(r.flatten()))
    recovered = Rotation.from_quat(quat).as_matrix()
    np.testing.assert_allclose(recovered, r, atol=1e-8)


def test_center_frame_index_for_square_crop_is_the_midpoint():
    crop_info = {"n_frames_for_square_crop": 70}
    assert camera.center_frame_index_for_square_crop(100, crop_info) == 135.0


def test_center_frame_index_for_square_crop_handles_odd_n_frames():
    crop_info = {"n_frames_for_square_crop": 7}
    assert camera.center_frame_index_for_square_crop(0, crop_info) == 3.5


@pytest.mark.heavy
def test_edr_entry_camera_et_and_tsai_path_match_a_real_built_camera():
    """Regression check against real SPICE/ISIS for the two ISIS-free accessors
    `TrnTestEntryEdr.camera_et`/`tsai_path` gained so `TrnTestDataSet.write_entry_poses()` can read
    a pose back without rebuilding `camera` -- both must agree exactly with what a real
    `build_camera` call actually produces, not just with each other."""
    images = trntest.read_manifest("notebooks/dataset_manifest.csv")
    session = trntest.Session()
    dataset = trntest.TrnTestDataSet.create(session.config.output_dir / "trn_dataset", images, session.config)
    # write_index=False: see test_wac_camera_model.py's own identical comment -- avoids a full
    # dataset-wide overview-map Camera rebuild unrelated to this test.
    dataset.populate(limit=1, write_index=False)
    entry = dataset[0]

    assert entry.camera_et == entry.camera.et
    assert entry.tsai_path == entry.camera.tsai_path
    assert entry.tsai_path.exists()

    record = entry_poses.entry_pose_record(dataset, entry)
    assert record is not None
    assert record["header"]["stamp"]["sec"] == int(entry.camera.et // 1)
    np.testing.assert_allclose(
        [record["pose"]["position"]["x"], record["pose"]["position"]["y"], record["pose"]["position"]["z"]],
        entry.camera.camera_center_moon_me_m,
        atol=1e-3,
    )
    expected_quat = camera.r_cam_to_me_quaternion_xyzw(list(np.array(entry.camera.r_cam_to_me).flatten()))
    got_quat = (
        record["pose"]["orientation"]["x"],
        record["pose"]["orientation"]["y"],
        record["pose"]["orientation"]["z"],
        record["pose"]["orientation"]["w"],
    )
    np.testing.assert_allclose(got_quat, expected_quat, atol=1e-8)
