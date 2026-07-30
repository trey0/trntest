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
TARGET_GSD_M = 100.0  # match Lunaserv's GLD100/WAC ~100 m/px source resolution


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


def ray_sphere_intersect_range(origin_km: np.ndarray, direction_unit: np.ndarray, moon_radius_km: float = 1737.4) -> float:
    """Distance along `direction_unit` from `origin_km` to the Moon's mean sphere; None if it misses."""
    b = 2 * np.dot(origin_km, direction_unit)
    c = np.dot(origin_km, origin_km) - moon_radius_km**2
    disc = b * b - 4 * c
    if disc < 0:
        return None
    t = (-b - np.sqrt(disc)) / 2  # near intersection
    return t


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
    et = frame_et(edr, TARGET_FRAME_INDEX)

    c_meters, r_cam_to_me, slant_range_km, off_nadir_deg = camera_pose_moon_me(et)
    fu = fv = (slant_range_km * 1000.0) / TARGET_GSD_M
    cu = cv = IMAGE_SIZE / 2.0

    write_tsai(output_tsai_path, c_meters, r_cam_to_me, fu, fv, cu, cv)

    footprint = footprint_lonlat(c_meters / 1000.0, r_cam_to_me, fu, fv, cu, cv, IMAGE_SIZE)

    return {
        "et": et,
        "camera_center_moon_me_m": c_meters.tolist(),
        "slant_range_km": slant_range_km,
        "off_nadir_deg": off_nadir_deg,
        "focal_length_px": fu,
        "footprint_lonlat_deg": footprint,
    }


if __name__ == "__main__":
    import json
    import os

    os.makedirs("/workspace/output", exist_ok=True)
    info = build("/workspace/output/camera_frame440.tsai")
    print(json.dumps(info, indent=2, default=str))
