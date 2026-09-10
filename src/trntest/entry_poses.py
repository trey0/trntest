"""A ROS-inspired JSONL record per dataset entry, describing that entry's 6-DOF camera-frame
pose -- position and quaternion attitude, `MOON_ME` -- read straight from its already-written
`.tsai` file (`camera.read_tsai_pose`), not rebuilt via `entry.camera`. See
`TrnTestDataSet.write_entry_poses`'s docstring (the public entry point) for why that split
matters. Kept in a separate module (like `report.py`/`overview_map.py`) rather than inline on
`TrnTestDataSet` itself, given `ENTRY_POSE_JSON_SCHEMA`'s size.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING

from trntest import camera as camera_module

if TYPE_CHECKING:
    from trntest.trn_dataset import TrnTestDataSet, TrnTestEntry

ENTRY_POSES_FILENAME = "entry_poses.jsonl"
ENTRY_POSES_SCHEMA_FILENAME = "entry_poses.schema.json"

ENTRY_POSE_JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://trntest.local/schemas/entry_pose.schema.json",
    "title": "TrnTest entry pose record",
    "description": (
        "One JSON Lines (JSONL) record per dataset entry -- each line of entry_poses.jsonl is a "
        "separate instance of this schema, not a JSON array. Describes the 6-DOF camera-frame "
        "pose associated with that entry's .tsai Pinhole camera model (see "
        "camera.write_tsai/read_tsai_pose), loosely modeled on a ROS geometry_msgs/PoseStamped "
        "message plus a few TrnTest-specific identifying fields."
    ),
    "type": "object",
    "additionalProperties": False,
    "required": ["header", "pose", "dataset", "entry_identifier", "entry_index"],
    "properties": {
        "header": {
            "type": "object",
            "description": "Modeled on ROS's std_msgs/Header.",
            "additionalProperties": False,
            "required": ["stamp", "frame_id"],
            "properties": {
                "stamp": {
                    "type": "object",
                    "description": (
                        "This pose's epoch, as SPICE ephemeris time (ET) -- TDB (Barycentric "
                        "Dynamical Time) seconds past the J2000 epoch (2000-001T12:00:00 TDB), "
                        "split into whole seconds and nanoseconds the way ROS 2's "
                        "builtin_interfaces/Time does. Not Unix time."
                    ),
                    "additionalProperties": False,
                    "required": ["sec", "nanosec"],
                    "properties": {
                        "sec": {
                            "type": "integer",
                            "description": (
                                "Whole TDB seconds past J2000 (may be negative for an epoch "
                                "before J2000 -- not applicable to any real entry in this "
                                "project)."
                            ),
                        },
                        "nanosec": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 999999999,
                            "description": "Nanosecond remainder -- always non-negative regardless of sec's sign.",
                        },
                    },
                },
                "frame_id": {
                    "type": "string",
                    "const": "MOON_ME",
                    "description": (
                        "The reference frame position/orientation are expressed in -- this "
                        "project's SPICE body-fixed Moon Mean Earth/rotation-axis frame. Always "
                        '"MOON_ME" today; kept as a string (not assumed) in case a future frame '
                        "is ever added."
                    ),
                },
            },
        },
        "pose": {
            "type": "object",
            "description": (
                "This entry's camera-frame 6-DOF pose at header.stamp, exactly as recorded in "
                "its .tsai file's C (position) and R (rotation) fields. Modeled on ROS's "
                "geometry_msgs/Pose."
            ),
            "additionalProperties": False,
            "required": ["position", "orientation"],
            "properties": {
                "position": {
                    "type": "object",
                    "description": "Camera center, meters, in the header.frame_id frame.",
                    "additionalProperties": False,
                    "required": ["x", "y", "z"],
                    "properties": {
                        "x": {"type": "number", "description": "Meters."},
                        "y": {"type": "number", "description": "Meters."},
                        "z": {"type": "number", "description": "Meters."},
                    },
                },
                "orientation": {
                    "type": "object",
                    "description": (
                        "Unit quaternion rotating a vector from the camera frame into "
                        "header.frame_id (camera-to-MOON_ME), scalar-last per ROS's "
                        "geometry_msgs/Quaternion convention (x, y, z, w)."
                    ),
                    "additionalProperties": False,
                    "required": ["x", "y", "z", "w"],
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                        "w": {"type": "number"},
                    },
                },
            },
        },
        "dataset": {
            "type": "string",
            "description": "This dataset's short name (TrnTestDataSet.name -- its folder's basename).",
        },
        "entry_identifier": {
            "type": "string",
            "description": (
                "This entry's TrnTestEntry.identifier -- the real WAC EDR product id for an "
                'entry_kind="edr" dataset, or a compact UTC timestamp for entry_kind="spice".'
            ),
        },
        "entry_index": {
            "type": "integer",
            "minimum": 0,
            "description": "This entry's dense 0..n-1 positional index in the dataset (TrnTestEntry.index).",
        },
    },
}


def entry_pose_record(dataset: TrnTestDataSet, entry: TrnTestEntry) -> dict | None:
    """One JSONL record for `entry` (matching `ENTRY_POSE_JSON_SCHEMA`), or `None` if it has no
    `.tsai` file on disk yet (an as-yet-unpopulated entry -- not an error, just nothing to report
    yet)."""
    if not entry.tsai_path.exists():
        return None
    c_meters, r_flat = camera_module.read_tsai_pose(entry.tsai_path)
    qx, qy, qz, qw = camera_module.r_cam_to_me_quaternion_xyzw(r_flat)
    et = entry.camera_et
    sec = math.floor(et)
    nanosec = round((et - sec) * 1e9)
    return {
        "header": {"stamp": {"sec": sec, "nanosec": nanosec}, "frame_id": "MOON_ME"},
        "pose": {
            "position": {"x": c_meters[0], "y": c_meters[1], "z": c_meters[2]},
            "orientation": {"x": qx, "y": qy, "z": qz, "w": qw},
        },
        "dataset": dataset.name,
        "entry_identifier": entry.identifier,
        "entry_index": entry.index,
    }


def write_entry_poses(dataset: TrnTestDataSet, entries: TrnTestEntry | list[TrnTestEntry] | None = None) -> None:
    """Writes `<dataset.folder>/entry_poses.jsonl` (one record per entry, from `entry_pose_record`
    -- skipping any entry with no `.tsai` yet) and its companion `entry_poses.schema.json`
    (`ENTRY_POSE_JSON_SCHEMA`, always rewritten alongside so the two never drift apart). See
    `TrnTestDataSet.write_entry_poses`'s docstring, the public entry point, for the full
    rationale."""
    # mkdir here rather than relying on create() having already run -- same reasoning as
    # write_index()'s mkdir, since a caller can construct a TrnTestDataSet directly (e.g. this
    # project's tests).
    dataset.folder.mkdir(parents=True, exist_ok=True)
    target_entries = list(dataset) if entries is None else entries if isinstance(entries, list) else [entries]
    records = [r for r in (entry_pose_record(dataset, entry) for entry in target_entries) if r is not None]
    lines = "".join(json.dumps(record) + "\n" for record in records)
    (dataset.folder / ENTRY_POSES_FILENAME).write_text(lines)
    (dataset.folder / ENTRY_POSES_SCHEMA_FILENAME).write_text(json.dumps(ENTRY_POSE_JSON_SCHEMA, indent=2) + "\n")
