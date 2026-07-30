"""Build a recognizable single-band image from the real WAC CDR product, for comparison against
the synthetic sat_sim render (Phase 5).

Two things had to be fixed to get here (see docs/data-sources.md for the full story):

1. WAC is a "push-frame" camera: each 78-line frame multiplexes 7 filters (2 UV @ 4 TDI lines +
   5 VIS @ 14 TDI lines), and per the official LROC EDR/CDR SIS, "the WAC CDR file will require
   further processing to separate framelets into their respective bands ... in order to be viewed
   as a standard image." A raw multiplexed strip (what this script used to extract) is therefore
   never going to look like a picture -- it has to be de-interleaved first. This module extracts
   ONE filter's TDI-line block from each of many consecutive frames and stacks them vertically,
   which is exactly how WAC's push-frame design is meant to build up continuous coverage
   ("continuous color coverage ... such that each of the narrow framelets of each color band
   overlap" -- LROCSIS.PDF). The 14-line block at offset 22 is guaranteed to be a pure VIS band
   regardless of which of the two band orderings LRO is currently flying (the order reverses after
   each 180-degree yaw/solar-panel maneuver) -- see the offset comment below.

2. The chosen product's early frames (roughly 0-210 of 538) turned out to be in near-total shadow
   (I/F values at the noise floor, sometimes negative) -- this is presumably why the original
   frame-0 comparison looked like nothing. Scanning the product found a long, stable, well-lit
   stretch around frames 240-530; the target frame index (default 440) was moved there so the
   synthetic camera and this real-image comparison both land on visible terrain.

The crop uses the full 704-sample width and however many along-track frames cover that same real
ground distance -- i.e. a square patch of real ground, not a fixed pixel count. The frame count is
computed from the real WAC color-mode FOV and the actual orbital ground speed at this pose (see
`camera.compute_n_frames_for_square_crop`), not a magic constant -- so the resulting crop won't be
square in *pixels* (704 samples cross-track vs. a different line count along-track, since the two
axes have different native GSD), but is square in real km, matching the synthetic camera's FOV
(which is sized from the same real WAC FOV figure).
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
    start_frame: int | None = None, n_frames: int | None = None, config: TrntestConfig | None = None
) -> np.ndarray:
    """Stack one VIS filter's TDI-line block from `n_frames` consecutive frames starting at
    `start_frame`, producing a single continuous (n_frames * 14, 704) image. If `n_frames` isn't
    given, it's computed so the crop covers the same real ground distance as the 704-sample
    cross-track width -- see `camera.compute_n_frames_for_square_crop`."""
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
    return vis.reshape(n_frames * VIS_BLOCK_HEIGHT, SAMPLES)
