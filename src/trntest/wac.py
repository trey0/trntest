"""Build a recognizable single-band image from the real WAC CDR product, for comparison against
the synthetic sat_sim render.

WAC is a "push-frame" camera: each 78-line frame multiplexes 7 filters (2 UV @ 4 TDI lines + 5 VIS
@ 14 TDI lines), and per the official LROC EDR/CDR SIS, "the WAC CDR file will require further
processing to separate framelets into their respective bands ... in order to be viewed as a
standard image" -- see docs/data-sources.md for the full byte-layout facts. This module extracts
ONE filter's TDI-line block (lines [22:36) -- always pure VIS regardless of yaw-dependent band
order, see the offset comment below) from each of many consecutive frames and stacks them
vertically, matching how WAC's push-frame design is meant to build continuous coverage.

The crop uses the full 704-sample width and however many along-track frames cover that same real
ground distance -- a square patch of real ground, not a fixed pixel count (see
`camera.compute_n_frames_for_square_crop`); the resulting crop isn't square in *pixels* (the two
axes have different native GSD) but is square in real km, matching the synthetic camera's FOV.

Frames are always read off disk in strict acquisition-time order (a PDS archival guarantee), but
which real-world direction that "forward in time" runs, expressed against WAC's fixed sample-axis
convention, is **pass-dependent** (see `camera.boresight_rotation_k`'s docstring and
docs/data-sources.md, "Pass-dependent sensor axis convention") -- the resulting mosaic's chirality
can come out mirrored, not just rotated, relative to the synthetic image. `fetch_vis_mosaic`
corrects for this by reversing the along-track frame order (`camera_pose.reverse_crop_along_track`)
whenever needed.
"""

import numpy as np

from trntest import cache, camera
from trntest.config import TrntestConfig, load_config

PDS3_HEADER_BYTES = 10560  # from the CDR label's Array_2D_Image/offset (EDR's was 7040)
SAMPLES = 704
LINES_PER_FRAME = 78  # 2 UV filters x 4 TDI lines + 5 VIS filters x 14 TDI lines
FRAME_BYTES = LINES_PER_FRAME * SAMPLES * 4  # float32

# Per the LROC SIS: "WAC band passes are arranged first UV then VIS ... but the order is reversed
# after LRO performs a 180 deg yaw maneuver." Either way, UV only ever occupies the first or last
# 8 lines of the 78-line frame -- lines [22:36) fall entirely within [8, 70), so they're always a
# genuine VIS filter block (which of the 5 VIS wavelengths depends on the yaw state, which we
# haven't determined -- irrelevant for just getting a recognizable picture).
VIS_BLOCK_OFFSET = 22
VIS_BLOCK_HEIGHT = 14

MISSING_CONSTANT = np.uint32(0xFF7FFFFB).view(np.float32)  # per the CDR label's Special_Constants


def fetch_vis_mosaic(
    camera_pose: camera.Camera,
    start_frame: int | None = None,
    n_frames: int | None = None,
    config: TrntestConfig | None = None,
) -> np.ndarray:
    """Stack one VIS filter's TDI-line block from `n_frames` consecutive frames starting at
    `start_frame`, producing a single continuous (n_frames * 14, 704) image. If `n_frames` isn't
    given, it's computed so the crop covers the same real ground distance as the 704-sample
    cross-track width -- see `camera.compute_n_frames_for_square_crop`.

    `camera_pose` (the already-built synthetic `Camera` for this same product/pose) determines
    whether frames must be stacked in *reverse* along-track order
    (`camera_pose.reverse_crop_along_track`): a real, pass-dependent mirror -- see that property's
    docstring and docs/data-sources.md, "Pass-dependent sensor axis convention" -- not
    optional/cosmetic, so this is a required argument, not a config knob."""
    config = config or load_config()
    if start_frame is None:
        start_frame = config.target_frame_index
    if n_frames is None:
        frame_timing = camera.fetch_frame_timing(config)
        n_frames = camera.compute_n_frames_for_square_crop(frame_timing, start_frame, config)[
            "n_frames_for_square_crop"
        ]

    img_path = cache.fetch_lroc_file(
        config.lroc_cdr_dataset,
        config.cdr_volume,
        config.edr_subdir,
        config.edr_doy,
        config.cdr_product,
        "IMG",
        cache_root=config.cache_root,
        base_url=config.lroc_base_url,
    )
    byte_start = PDS3_HEADER_BYTES + start_frame * FRAME_BYTES
    with open(img_path, "rb") as f:
        f.seek(byte_start)
        data = np.fromfile(f, dtype="<f4", count=n_frames * LINES_PER_FRAME * SAMPLES)
    frames = data.reshape(n_frames, LINES_PER_FRAME, SAMPLES)
    vis = frames[:, VIS_BLOCK_OFFSET : VIS_BLOCK_OFFSET + VIS_BLOCK_HEIGHT, :]
    if camera_pose.reverse_crop_along_track:
        vis = vis[::-1]
    return vis.reshape(n_frames * VIS_BLOCK_HEIGHT, SAMPLES)
