from unittest import mock

import numpy as np

from trntest import wac
from trntest.config import TrntestConfig


def _synthetic_cdr_bytes(n_frames: int) -> bytes:
    header = b"\x00" * wac.PDS3_HEADER_BYTES
    f_idx = np.arange(n_frames).reshape(-1, 1, 1)
    l_idx = np.arange(wac.LINES_PER_FRAME).reshape(1, -1, 1)
    s_idx = np.arange(wac.SAMPLES).reshape(1, 1, -1)
    frames = (f_idx * 10000 + l_idx * 100 + s_idx).astype("<f4")
    return header + frames.tobytes(), frames


def test_fetch_vis_mosaic_extracts_correct_vis_block(tmp_path):
    n_frames = 2
    payload, frames = _synthetic_cdr_bytes(n_frames)
    img_path = tmp_path / "fake.IMG"
    img_path.write_bytes(payload)

    config = TrntestConfig()
    with mock.patch("trntest.wac.cache.fetch_lroc_file", return_value=img_path):
        mosaic = wac.fetch_vis_mosaic(start_frame=0, n_frames=n_frames, config=config)

    assert mosaic.shape == (n_frames * wac.VIS_BLOCK_HEIGHT, wac.SAMPLES)
    for f in range(n_frames):
        expected_block = frames[f, wac.VIS_BLOCK_OFFSET : wac.VIS_BLOCK_OFFSET + wac.VIS_BLOCK_HEIGHT, :]
        actual_block = mosaic[f * wac.VIS_BLOCK_HEIGHT : (f + 1) * wac.VIS_BLOCK_HEIGHT, :]
        np.testing.assert_allclose(actual_block, expected_block)


def test_fetch_vis_mosaic_respects_start_frame_offset(tmp_path):
    n_total_frames = 3
    payload, frames = _synthetic_cdr_bytes(n_total_frames)
    img_path = tmp_path / "fake.IMG"
    img_path.write_bytes(payload)

    config = TrntestConfig()
    with mock.patch("trntest.wac.cache.fetch_lroc_file", return_value=img_path):
        mosaic = wac.fetch_vis_mosaic(start_frame=1, n_frames=1, config=config)

    expected_block = frames[1, wac.VIS_BLOCK_OFFSET : wac.VIS_BLOCK_OFFSET + wac.VIS_BLOCK_HEIGHT, :]
    np.testing.assert_allclose(mosaic, expected_block)
