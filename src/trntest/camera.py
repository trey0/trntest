"""Build a `.tsai` Pinhole camera file posed from the real LRO SPICE trajectory, at the timestamp
of a chosen LROC WAC EDR framelet -- so a small synthetic image rendered from this camera
approximates the FOV of that part of the real swath.

`config.target_frame_index` (default 440, of this repo's original single-demo product's 538
framelets) is the START of the along-track crop; the actual pose epoch is that crop's own temporal
midpoint (see `build_camera`). This default was chosen empirically for ONE specific product, whose
early framelets are in shadow -- see docs/plan.md (Phase 2) for that history -- and is NOT a
general illumination-avoidance strategy: different EDR products have different lit spans depending
on their own orbit geometry, and hand-tuning a start offset per product doesn't scale. The demo
notebook no longer uses this default directly -- see `trntest.dataset.select_dataset`/
`generate_dataset`, which anchor each product's crop at a stable, product-relative point (its own
temporal midpoint, not a hand-picked offset) and simply exclude whole products that aren't
illuminated there, rather than searching within a product for a better offset.

See docs/plan.md (Phase 2) and docs/data-sources.md for the single-demo background.
"""

import dataclasses
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import numpy as np
import spiceypy as spice

from trntest import cache, spice_kernels
from trntest.config import DEFAULT_MOON_RADIUS_KM, TrntestConfig, load_config

PDS_NS = {
    "pds": "http://pds.nasa.gov/pds4/pds/v1",
    "lro": "http://pds.nasa.gov/pds4/mission/lro/v1",
    "img": "http://pds.nasa.gov/pds4/img/v1",
}

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


@dataclasses.dataclass(frozen=True)
class FrameTiming:
    """Per-frame acquisition timing, parsed from the EDR product's PDS4 label (start time,
    spacecraft clock, interframe delay, frame count) -- see `fetch_frame_timing`. This is the only
    role EDR data plays anywhere in this pipeline; no EDR pixel data is ever read. The actual image
    pixel data used for visual comparison against the synthetic render comes entirely from the CDR
    counterpart of the same acquisition -- see `trntest.wac.fetch_vis_mosaic`."""

    start_time: datetime
    sclk_start: str
    interframe_delay_s: float
    nframes: int


@dataclasses.dataclass(frozen=True)
class Camera:
    """SPICE-derived pose and geometry for the synthetic camera, as returned by `build_camera`."""

    et: float
    center_frame_index: float
    camera_center_moon_me_m: list
    r_cam_to_me: list
    slant_range_km: float
    off_nadir_deg: float
    focal_length_px: float
    footprint_lonlat_deg: dict[str, tuple[float, float] | None]
    cross_track_width_km: float
    km_per_frame: float
    n_frames_for_square_crop: int
    tsai_path: Path


def fetch_frame_timing(config: TrntestConfig | None = None) -> FrameTiming:
    """Fetch and parse the chosen EDR product's PDS4 XML label for its frame timing. See
    `FrameTiming`'s docstring for why this is named for timing, not "EDR", despite reading from the
    EDR product's label."""
    config = config or load_config()
    label_path = cache.fetch_lroc_file(
        config.lroc_edr_dataset,
        config.edr_volume,
        config.edr_subdir,
        config.edr_doy,
        config.edr_product,
        "xml",
        cache_root=config.cache_root,
        base_url=config.lroc_base_url,
    )
    root = ET.parse(label_path).getroot()

    start_str = _required_text(root, ".//pds:Time_Coordinates/pds:start_date_time")
    start_time = datetime.strptime(start_str, "%Y-%m-%dT%H:%M:%S.%f%z")
    sclk_start = _required_text(root, ".//lro:spacecraft_clock_start_count")
    delay_ms = float(_required_text(root, ".//lro:interframe_delay"))
    nframes = int(_required_text(root, ".//lro:nframes"))

    return FrameTiming(start_time, sclk_start, delay_ms / 1000.0, nframes)


def _required_text(root: ET.Element, xpath: str) -> str:
    """`root.find(xpath).text`, asserting the element and its text are present -- if the EDR
    label's schema is ever missing one of these fields, fail loudly here rather than with a
    confusing `AttributeError`/`TypeError` downstream."""
    element = root.find(xpath, PDS_NS)
    assert element is not None, f"EDR label missing expected element: {xpath}"
    assert element.text is not None, f"EDR label element has no text: {xpath}"
    return element.text


def frame_et(frame_timing: FrameTiming, frame_index: float) -> float:
    """SPICE ET (seconds) of a given framelet, computed from the product's start SCLK."""
    et0 = spice.scs2e(spice_kernels.LRO_ID, frame_timing.sclk_start)
    return et0 + frame_index * frame_timing.interframe_delay_s


def camera_pose_moon_me(et: float):
    """Return (C_meters, R_cam_to_moon_me, slant_range_km) for the WAC VIS channel at time et."""
    state, _ = spice.spkezr("LRO", et, "MOON_ME", "NONE", "MOON")
    c_km = np.array(state[:3])
    r_cam_to_me = np.array(spice.pxform("LRO_LROCWAC_VIS", "MOON_ME", et))

    boresight_me = r_cam_to_me @ np.array([0.0, 0.0, 1.0])
    nadir = -c_km / np.linalg.norm(c_km)
    off_nadir_deg = np.degrees(np.arccos(np.clip(np.dot(boresight_me, nadir), -1, 1)))

    slant_range_km = ray_sphere_intersect_range(c_km, boresight_me)
    # A camera actually looking at the Moon (as this demo's near-nadir poses always are) always
    # hits the sphere along its own boresight; None here would mean the boresight points away from
    # the Moon entirely, which would indicate a real upstream pose bug worth failing loudly on.
    assert slant_range_km is not None, "camera boresight does not intersect the Moon -- pose is not looking at it"
    return c_km * 1000.0, r_cam_to_me, slant_range_km, off_nadir_deg


def rotation_about_boresight(k: int) -> np.ndarray:
    """Proper (handedness-preserving) rotation by 90*k degrees about the boresight (Z) axis. Unlike
    swapping two axes while holding the third fixed (a reflection), this keeps the boresight
    direction unchanged and just relabels which of +X/+Y/-X/-Y maps to the image's px/py axes."""
    theta = np.radians(90.0 * k)
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def ray_sphere_intersect_range(
    origin_km: np.ndarray, direction_unit: np.ndarray, moon_radius_km: float = DEFAULT_MOON_RADIUS_KM
) -> float | None:
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
    assert t is not None, "camera boresight does not intersect the Moon -- pose is not looking at it"
    return c_km + t * boresight_me


def cross_track_width_km(c_km: np.ndarray, r_cam_to_me: np.ndarray, half_angle_rad: float) -> float:
    """Real cross-track ground width at this exact pose: ray-trace the +/- half-angle rays along
    the camera's x (cross-track) axis to the Moon's surface and return the distance between them."""
    left_me = r_cam_to_me @ np.array([-np.sin(half_angle_rad), 0.0, np.cos(half_angle_rad)])
    right_me = r_cam_to_me @ np.array([np.sin(half_angle_rad), 0.0, np.cos(half_angle_rad)])
    t_left = ray_sphere_intersect_range(c_km, left_me)
    t_right = ray_sphere_intersect_range(c_km, right_me)
    assert t_left is not None and t_right is not None, (
        "cross-track FOV edge ray does not intersect the Moon -- half_angle_rad too wide for this altitude"
    )
    left_ground = c_km + t_left * left_me
    right_ground = c_km + t_right * right_me
    return ground_chord_km(left_ground, right_ground)


def km_per_frame(frame_timing: FrameTiming, frame_index: int, n: int = 10) -> float:
    """Real ground advance per framelet, smoothed over `n` frames (boresight ground point at
    frame_index vs. frame_index + n, divided by n)."""
    c0_m, r0, _, _ = camera_pose_moon_me(frame_et(frame_timing, frame_index))
    c1_m, r1, _, _ = camera_pose_moon_me(frame_et(frame_timing, frame_index + n))
    ground0 = boresight_ground_point_km(c0_m / 1000.0, r0)
    ground1 = boresight_ground_point_km(c1_m / 1000.0, r1)
    return ground_chord_km(ground0, ground1) / n


def compute_n_frames_for_square_crop(
    frame_timing: FrameTiming, frame_index: int | None = None, config: TrntestConfig | None = None
) -> dict:
    """How many consecutive frames of the real WAC CDR (full 704-sample width) are needed so the
    along-track distance covered matches the real cross-track swath width -- i.e. a square crop
    of real ground, per the demo's objective (see docs/plan.md). Self-contained: furnishes SPICE
    kernels and computes the pose itself, so callers only need a FrameTiming."""
    config = config or load_config()
    if frame_index is None:
        frame_index = config.target_frame_index
    spice_kernels.fetch_and_furnish(frame_timing.start_time, config)
    et = frame_et(frame_timing, frame_index)
    c_meters, r_cam_to_me, _, _ = camera_pose_moon_me(et)
    half_angle_rad = np.radians(config.wac_vis_color_fov_deg / 2.0)
    w_cross_km = cross_track_width_km(c_meters / 1000.0, r_cam_to_me, half_angle_rad)
    per_frame_km = km_per_frame(frame_timing, frame_index)
    n_frames = max(1, round(w_cross_km / per_frame_km))
    return {
        "cross_track_width_km": w_cross_km,
        "km_per_frame": per_frame_km,
        "n_frames_for_square_crop": n_frames,
    }


def pixel_ray_cam(px: float, py: float, fu: float, fv: float, cu: float, cv: float) -> np.ndarray:
    v = np.array([(px - cu) / fu, (py - cv) / fv, 1.0])
    return v / np.linalg.norm(v)


def footprint_lonlat(
    c_km: np.ndarray, r_cam_to_me: np.ndarray, fu, fv, cu, cv, size: int
) -> dict[str, tuple[float, float] | None]:
    """Ground lon/lat (deg) of the image's 4 corners + center, via sphere intersection."""
    pts = {
        "center": (size / 2, size / 2),
        "top_left": (0, 0),
        "top_right": (size, 0),
        "bottom_left": (0, size),
        "bottom_right": (size, size),
    }
    out: dict[str, tuple[float, float] | None] = {}
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


def build_camera(config: TrntestConfig | None = None, output_tsai_path: str | Path | None = None) -> Camera:
    """Fetch the target frame's timing, pose the camera from real SPICE trajectory data, and write
    the resulting `.tsai` Pinhole camera file."""
    config = config or load_config()
    frame_timing = fetch_frame_timing(config)
    spice_kernels.fetch_and_furnish(frame_timing.start_time, config)

    # The real CDR comparison crop (Phase 5) spans n_frames frames *starting* at
    # target_frame_index, not centered on it -- so the synthetic camera's pose epoch must be the
    # crop's temporal midpoint, not its start, for the two images' centers to actually match.
    # The n_frames estimate itself barely changes over this short span, so using the start frame's
    # geometry for that estimate (not yet knowing the midpoint) isn't a meaningful circularity.
    crop_info = compute_n_frames_for_square_crop(frame_timing, config.target_frame_index, config)
    center_frame_index = config.target_frame_index + crop_info["n_frames_for_square_crop"] / 2.0
    et = frame_et(frame_timing, center_frame_index)

    c_meters, r_cam_to_me_raw, slant_range_km, off_nadir_deg = camera_pose_moon_me(et)
    # Apply the fixed sensor-model axis convention (see SENSOR_MODEL_BORESIGHT_ROTATION_K above) --
    # this only relabels px/py against the (unchanged) boresight, it doesn't move the camera.
    r_cam_to_me = r_cam_to_me_raw @ rotation_about_boresight(SENSOR_MODEL_BORESIGHT_ROTATION_K)

    half_angle_rad = np.radians(config.wac_vis_color_fov_deg / 2.0)
    fu = fv = (config.image_size / 2.0) / np.tan(half_angle_rad)
    cu = cv = config.image_size / 2.0

    if output_tsai_path is None:
        output_tsai_path = config.output_dir / f"camera_frame{config.target_frame_index}.tsai"
    output_tsai_path = Path(output_tsai_path)
    output_tsai_path.parent.mkdir(parents=True, exist_ok=True)
    write_tsai(output_tsai_path, c_meters, r_cam_to_me, fu, fv, cu, cv)

    footprint = footprint_lonlat(c_meters / 1000.0, r_cam_to_me, fu, fv, cu, cv, config.image_size)

    return Camera(
        et=et,
        center_frame_index=center_frame_index,
        camera_center_moon_me_m=c_meters.tolist(),
        r_cam_to_me=r_cam_to_me.tolist(),
        slant_range_km=slant_range_km,
        off_nadir_deg=off_nadir_deg,
        focal_length_px=fu,
        footprint_lonlat_deg=footprint,
        cross_track_width_km=crop_info["cross_track_width_km"],
        km_per_frame=crop_info["km_per_frame"],
        n_frames_for_square_crop=crop_info["n_frames_for_square_crop"],
        tsai_path=output_tsai_path,
    )
