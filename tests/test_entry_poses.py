import json

import numpy as np
import pytest

from trntest import camera, entry_poses


class _FakeEntry:
    """A minimal stand-in for `TrnTestEntry` -- `entry_pose_record` only ever reads
    `tsai_path`/`camera_et`/`identifier`/`index`, so a real `TrnTestEntry` (with its real
    `per_image_config`/SPICE dependencies) isn't needed to test it."""

    def __init__(self, tsai_path, camera_et, identifier, index):
        self.tsai_path = tsai_path
        self.camera_et = camera_et
        self.identifier = identifier
        self.index = index


class _FakeDataset:
    def __init__(self, name):
        self.name = name


def _write_fake_tsai(path, c_meters=(1.0, 2.0, 3.0), r=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    r = np.eye(3) if r is None else r
    camera.write_tsai(path, np.array(c_meters), r, fu=1000.0, fv=1000.0, cu=500.0, cv=500.0)


def test_entry_pose_record_returns_none_when_no_tsai_yet(tmp_path):
    entry = _FakeEntry(tmp_path / "does_not_exist.tsai", camera_et=1.0, identifier="P1", index=0)

    assert entry_poses.entry_pose_record(_FakeDataset("ds"), entry) is None


def test_entry_pose_record_has_the_expected_shape(tmp_path):
    tsai_path = tmp_path / "camera_frame437.tsai"
    _write_fake_tsai(tsai_path, c_meters=(10.0, 20.0, 30.0))
    entry = _FakeEntry(tsai_path, camera_et=300000000.25, identifier="M1314334685CE", index=3)

    record = entry_poses.entry_pose_record(_FakeDataset("trntest1"), entry)

    assert record == {
        "header": {"stamp": {"sec": 300000000, "nanosec": 250000000}, "frame_id": "MOON_ME"},
        "pose": {
            "position": {"x": 10.0, "y": 20.0, "z": 30.0},
            "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
        },
        "dataset": "trntest1",
        "entry_identifier": "M1314334685CE",
        "entry_index": 3,
    }


def test_entry_pose_record_orientation_matches_a_nonidentity_rotation(tmp_path):
    tsai_path = tmp_path / "camera_frame1.tsai"
    r = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])  # +90deg about z
    _write_fake_tsai(tsai_path, r=r)
    entry = _FakeEntry(tsai_path, camera_et=0.0, identifier="P1", index=0)

    record = entry_poses.entry_pose_record(_FakeDataset("ds"), entry)

    expected = camera.r_cam_to_me_quaternion_xyzw(list(r.flatten()))
    got = record["pose"]["orientation"]
    assert (got["x"], got["y"], got["z"], got["w"]) == pytest.approx(expected)


def test_entry_pose_record_stamp_handles_a_negative_et(tmp_path):
    # Whole-seconds/nanosecond split must stay correct even for et < 0 (before the J2000 epoch --
    # not expected for any real entry in this project, but the split shouldn't silently break):
    # math.floor(-1.75) == -2, and the nanosecond remainder must still come out non-negative.
    tsai_path = tmp_path / "camera_frame1.tsai"
    _write_fake_tsai(tsai_path)
    entry = _FakeEntry(tsai_path, camera_et=-1.75, identifier="P1", index=0)

    record = entry_poses.entry_pose_record(_FakeDataset("ds"), entry)

    assert record["header"]["stamp"] == {"sec": -2, "nanosec": 250000000}


def test_write_entry_poses_writes_one_line_per_entry_with_a_tsai(tmp_path):
    entry1 = _FakeEntry(tmp_path / "e1.tsai", camera_et=100.0, identifier="P1", index=0)
    entry2 = _FakeEntry(tmp_path / "e2.tsai", camera_et=200.0, identifier="P2", index=1)
    _write_fake_tsai(entry1.tsai_path)
    _write_fake_tsai(entry2.tsai_path)
    dataset = _FakeDataset("ds")
    dataset.folder = tmp_path

    entry_poses.write_entry_poses(dataset, [entry1, entry2])

    lines = (tmp_path / entry_poses.ENTRY_POSES_FILENAME).read_text().splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert [r["entry_identifier"] for r in records] == ["P1", "P2"]


def test_write_entry_poses_skips_entries_with_no_tsai(tmp_path):
    entry1 = _FakeEntry(tmp_path / "e1.tsai", camera_et=100.0, identifier="P1", index=0)
    entry2 = _FakeEntry(tmp_path / "does_not_exist.tsai", camera_et=200.0, identifier="P2", index=1)
    _write_fake_tsai(entry1.tsai_path)
    dataset = _FakeDataset("ds")
    dataset.folder = tmp_path

    entry_poses.write_entry_poses(dataset, [entry1, entry2])

    lines = (tmp_path / entry_poses.ENTRY_POSES_FILENAME).read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["entry_identifier"] == "P1"


def test_write_entry_poses_writes_no_lines_when_nothing_has_a_tsai(tmp_path):
    entry = _FakeEntry(tmp_path / "does_not_exist.tsai", camera_et=100.0, identifier="P1", index=0)
    dataset = _FakeDataset("ds")
    dataset.folder = tmp_path

    entry_poses.write_entry_poses(dataset, [entry])

    assert (tmp_path / entry_poses.ENTRY_POSES_FILENAME).read_text() == ""


def test_write_entry_poses_also_writes_the_schema_file(tmp_path):
    entry = _FakeEntry(tmp_path / "e1.tsai", camera_et=100.0, identifier="P1", index=0)
    _write_fake_tsai(entry.tsai_path)
    dataset = _FakeDataset("ds")
    dataset.folder = tmp_path

    entry_poses.write_entry_poses(dataset, [entry])

    schema = json.loads((tmp_path / entry_poses.ENTRY_POSES_SCHEMA_FILENAME).read_text())
    assert schema == entry_poses.ENTRY_POSE_JSON_SCHEMA
    assert schema["required"] == ["header", "pose", "dataset", "entry_identifier", "entry_index"]
