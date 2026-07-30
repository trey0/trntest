"""Build a `.tsai` Pinhole camera file posed from the real LRO SPICE trajectory, at the timestamp
of a chosen LROC WAC EDR framelet -- so a small synthetic image rendered from this camera
approximates the FOV of that part of the real swath.

Framelet index 440 (of 538) is used, not the start of the swath: frames 0-~210 of this particular
product are in near-total shadow (verified by inspecting the CDR -- see docs/data-sources.md), so
they wouldn't be recognizable in any decoding. Frame 440 falls within a long, stable, well-lit
stretch (frames ~240-530), matched against real WAC imagery in Phase 5.

See docs/plan.md (Phase 2) and docs/data-sources.md for the background and the specific EDR chosen.
"""
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import spiceypy as spice

from cache_utils import fetch_lroc_edr_file
from fetch_spice_kernels import fetch_and_furnish, LRO_ID

# The WAC EDR product chosen for this demo -- see docs/data-sources.md.
EDR_VOLUME = "LROLRC_0041C"
EDR_SUBDIR = "ESM4"
EDR_DOY = "2019334"
EDR_PRODUCT = "M1329714703CE"

PDS_NS = {
    "pds": "http://pds.nasa.gov/pds4/pds/v1",
    "lro": "http://pds.nasa.gov/pds4/mission/lro/v1",
    "img": "http://pds.nasa.gov/pds4/img/v1",
}

# Frame index (0-based) within the product's `nframes` framelets to pose the camera at.
# See module docstring: chosen to land in sunlit terrain, not the shadowed start of the swath.
TARGET_FRAME_INDEX = 440

IMAGE_SIZE = 256

# LROC EDR/CDR SIS: "The VIS optics have a cross-track FOV of 91.7 deg (monochrome) and 61.4 deg
# (color)." Color mode is what this product uses (INSTRUMENT_MODE_ID = COLOR) and only reads out
# the center 704 (of ~1024) columns -- this is that narrower FOV, not the full-detector one. (An
# attempt to read the real FOV straight out of the loaded WAC-VIS IK via `spice.getfov(-85621, ...)`
# returned a ~91.6 deg-derived pyramid instead -- that's the generic/monochrome-mode FOV entry, not
# usable for the color-mode crop, so the SIS's explicit color-mode figure is used directly.)
# Used both to size the synthetic camera's FOV and to size the real WAC CDR comparison crop
# (see compute_n_frames_for_square_crop) so the two cover the same real ground area.
WAC_VIS_COLOR_FOV_DEG = 61.4


@dataclass
class EdrInfo:
    start_time: datetime
    sclk_start: str
    interframe_delay_s: float
    nframes: int


def fetch_edr_label() -> EdrInfo:
    label_path = fetch_lroc_edr_file(EDR_VOLUME, EDR_SUBDIR, EDR_DOY, EDR_PRODUCT, "xml")
    root = ET.parse(label_path).getroot()

    start_str = root.find(".//pds:Time_Coordinates/pds:start_date_time", PDS_NS).text
    start_time = datetime.strptime(start_str, "%Y-%m-%dT%H:%M:%S.%f%z")
    sclk_start = root.find(".//lro:spacecraft_clock_start_count", PDS_NS).text
    delay_ms = float(root.find(".//lro:interframe_delay", PDS_NS).text)
    nframes = int(root.find(".//lro:nframes", PDS_NS).text)

    return EdrInfo(start_time, sclk_start, delay_ms / 1000.0, nframes)


def frame_et(edr: EdrInfo, frame_index: int) -> float:
    """SPICE ET (seconds) of a given framelet, computed from the product's start SCLK."""
    et0 = spice.scs2e(LRO_ID, edr.sclk_start)
    return et0 + frame_index * edr.interframe_delay_s


def camera_pose_moon_me(et: float):
    """Return (C_meters, R_cam_to_moon_me, slant_range_km) for the WAC VIS channel at time et."""
    state, _ = spice.spkezr("LRO", et, "MOON_ME", "NONE", "MOON")
    c_km = np.array(state[:3])
    r_cam_to_me = np.array(spice.pxform("LRO_LROCWAC_VIS", "MOON_ME", et))

    boresight_me = r_cam_to_me @ np.array([0.0, 0.0, 1.0])
    nadir = -c_km / np.linalg.norm(c_km)
    off_nadir_deg = np.degrees(np.arccos(np.clip(np.dot(boresight_me, nadir), -1, 1)))

    slant_range_km = ray_sphere_intersect_range(c_km, boresight_me)
    return c_km * 1000.0, r_cam_to_me, slant_range_km, off_nadir_deg


def rotation_about_boresight(k: int) -> np.ndarray:
    """Proper (handedness-preserving) rotation by 90*k degrees about the boresight (Z) axis. Unlike
    swapping two axes while holding the third fixed (a reflection), this keeps the boresight
    direction unchanged and just relabels which of +X/+Y/-X/-Y maps to the image's px/py axes."""
    theta = np.radians(90.0 * k)
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


# Fixed (NOT pass- or attitude-dependent) sensor-model axis convention. Applying
# rotation_about_boresight(1) to the raw WAC-VIS camera frame makes the synthetic image's px
# (column) align with the raw frame's +Y and py (row) align with -X. This is one of exactly two
# boresight rotations (the other being k=3) that align px with cross-track and py with along-track
# -- matching both WAC's and NAC's real archived-image layout (samples/columns = cross-track,
# lines/rows = along-track; see docs/data-sources.md for why this isn't actually a WAC-vs-NAC
# choice, both agree). k=1 (not k=3) is picked specifically so that increasing py (row, downward)
# matches the same temporal sense as the real archived WAC image's row axis: consecutive-frame
# ground motion measures as dominantly -X in the raw WAC-VIS frame (see
# compute_n_frames_for_square_crop's use of km_per_frame), i.e. "forward in time" is -X, which is
# exactly where k=1 sends py. This is a hardware/data-format property, fixed once here -- it does
# not vary with which pass, ascending/descending, or yaw state this particular render happens to be.
SENSOR_MODEL_BORESIGHT_ROTATION_K = 1


def ray_sphere_intersect_range(origin_km: np.ndarray, direction_unit: np.ndarray, moon_radius_km: float = 1737.4) -> float:
    """Distance along `direction_unit` from `origin_km` to the Moon's mean sphere; None if it misses."""
    b = 2 * np.dot(origin_km, direction_unit)
    c = np.dot(origin_km, origin_km) - moon_radius_km**2
    disc = b * b - 4 * c
    if disc < 0:
        return None
    t = (-b - np.sqrt(disc)) / 2  # near intersection
    return t


def ground_chord_km(p1_km: np.ndarray, p2_km: np.ndarray) -> float:
    """Straight-line (chord) distance between two MOON_ME points, in km -- fine at these short
    (tens of km) distances; no need for a full geodesic/haversine calculation."""
    return float(np.linalg.norm(p1_km - p2_km))


def boresight_ground_point_km(c_km: np.ndarray, r_cam_to_me: np.ndarray) -> np.ndarray:
    boresight_me = r_cam_to_me @ np.array([0.0, 0.0, 1.0])
    t = ray_sphere_intersect_range(c_km, boresight_me)
    return c_km + t * boresight_me


def cross_track_width_km(c_km: np.ndarray, r_cam_to_me: np.ndarray, half_angle_rad: float) -> float:
    """Real cross-track ground width at this exact pose: ray-trace the +/- half-angle rays along
    the camera's x (cross-track) axis to the Moon's surface and return the distance between them."""
    left_me = r_cam_to_me @ np.array([-np.sin(half_angle_rad), 0.0, np.cos(half_angle_rad)])
    right_me = r_cam_to_me @ np.array([np.sin(half_angle_rad), 0.0, np.cos(half_angle_rad)])
    left_ground = c_km + ray_sphere_intersect_range(c_km, left_me) * left_me
    right_ground = c_km + ray_sphere_intersect_range(c_km, right_me) * right_me
    return ground_chord_km(left_ground, right_ground)


def km_per_frame(edr: EdrInfo, frame_index: int, n: int = 10) -> float:
    """Real ground advance per framelet, smoothed over `n` frames (boresight ground point at
    frame_index vs. frame_index + n, divided by n)."""
    c0_m, r0, _, _ = camera_pose_moon_me(frame_et(edr, frame_index))
    c1_m, r1, _, _ = camera_pose_moon_me(frame_et(edr, frame_index + n))
    ground0 = boresight_ground_point_km(c0_m / 1000.0, r0)
    ground1 = boresight_ground_point_km(c1_m / 1000.0, r1)
    return ground_chord_km(ground0, ground1) / n


def compute_n_frames_for_square_crop(edr: EdrInfo, frame_index: int = TARGET_FRAME_INDEX) -> dict:
    """How many consecutive frames of the real WAC CDR (full 704-sample width) are needed so the
    along-track distance covered matches the real cross-track swath width -- i.e. a square crop
    of real ground, per the demo's objective (see docs/plan.md). Self-contained: furnishes SPICE
    kernels and computes the pose itself, so callers only need an EdrInfo."""
    fetch_and_furnish(edr.start_time)
    et = frame_et(edr, frame_index)
    c_meters, r_cam_to_me, _, _ = camera_pose_moon_me(et)
    half_angle_rad = np.radians(WAC_VIS_COLOR_FOV_DEG / 2.0)
    w_cross_km = cross_track_width_km(c_meters / 1000.0, r_cam_to_me, half_angle_rad)
    per_frame_km = km_per_frame(edr, frame_index)
    n_frames = max(1, round(w_cross_km / per_frame_km))
    return {
        "cross_track_width_km": w_cross_km,
        "km_per_frame": per_frame_km,
        "n_frames_for_square_crop": n_frames,
    }


def pixel_ray_cam(px: float, py: float, fu: float, fv: float, cu: float, cv: float) -> np.ndarray:
    v = np.array([(px - cu) / fu, (py - cv) / fv, 1.0])
    return v / np.linalg.norm(v)


def footprint_lonlat(c_km: np.ndarray, r_cam_to_me: np.ndarray, fu, fv, cu, cv, size: int):
    """Ground lon/lat (deg) of the image's 4 corners + center, via sphere intersection."""
    pts = {
        "center": (size / 2, size / 2),
        "top_left": (0, 0),
        "top_right": (size, 0),
        "bottom_left": (0, size),
        "bottom_right": (size, size),
    }
    out = {}
    for name, (px, py) in pts.items():
        ray_cam = pixel_ray_cam(px, py, fu, fv, cu, cv)
        ray_me = r_cam_to_me @ ray_cam
        t = ray_sphere_intersect_range(c_km, ray_me)
        if t is None:
            out[name] = None
            continue
        ground = c_km + t * ray_me
        radius, lon, lat = spice.reclat(ground)
        out[name] = (np.degrees(lon), np.degrees(lat))
    return out


def write_tsai(path, c_meters, r_cam_to_me, fu, fv, cu, cv):
    lines = [
        "VERSION_4",
        "PINHOLE",
        f"fu = {fu}",
        f"fv = {fv}",
        f"cu = {cu}",
        f"cv = {cv}",
        "u_direction = 1  0  0",
        "v_direction = 0  1  0",
        "w_direction = 0  0  1",
        "C = " + " ".join(f"{x:.6f}" for x in c_meters),
        "R = " + " ".join(f"{x:.9f}" for x in r_cam_to_me.flatten()),
        "pitch = 1",
        "NULL",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def build(output_tsai_path):
    edr = fetch_edr_label()
    fetch_and_furnish(edr.start_time)

    # The real CDR comparison crop (Phase 5) spans n_frames frames *starting* at
    # TARGET_FRAME_INDEX, not centered on it -- so the synthetic camera's pose epoch must be the
    # crop's temporal midpoint, not its start, for the two images' centers to actually match.
    # The n_frames estimate itself barely changes over this short span, so using the start frame's
    # geometry for that estimate (not yet knowing the midpoint) isn't a meaningful circularity.
    crop_info = compute_n_frames_for_square_crop(edr, TARGET_FRAME_INDEX)
    center_frame_index = TARGET_FRAME_INDEX + crop_info["n_frames_for_square_crop"] / 2.0
    et = frame_et(edr, center_frame_index)

    c_meters, r_cam_to_me_raw, slant_range_km, off_nadir_deg = camera_pose_moon_me(et)
    # Apply the fixed sensor-model axis convention (see SENSOR_MODEL_BORESIGHT_ROTATION_K above) --
    # this only relabels px/py against the (unchanged) boresight, it doesn't move the camera.
    r_cam_to_me = r_cam_to_me_raw @ rotation_about_boresight(SENSOR_MODEL_BORESIGHT_ROTATION_K)

    half_angle_rad = np.radians(WAC_VIS_COLOR_FOV_DEG / 2.0)
    fu = fv = (IMAGE_SIZE / 2.0) / np.tan(half_angle_rad)
    cu = cv = IMAGE_SIZE / 2.0

    write_tsai(output_tsai_path, c_meters, r_cam_to_me, fu, fv, cu, cv)

    footprint = footprint_lonlat(c_meters / 1000.0, r_cam_to_me, fu, fv, cu, cv, IMAGE_SIZE)

    return {
        "et": et,
        "center_frame_index": center_frame_index,
        "camera_center_moon_me_m": c_meters.tolist(),
        "r_cam_to_me": r_cam_to_me.tolist(),
        "slant_range_km": slant_range_km,
        "off_nadir_deg": off_nadir_deg,
        "focal_length_px": fu,
        "footprint_lonlat_deg": footprint,
        **crop_info,
    }


if __name__ == "__main__":
    import json
    import os

    os.makedirs("/workspace/output", exist_ok=True)
    info = build("/workspace/output/camera_frame440.tsai")
    print(json.dumps(info, indent=2, default=str))
