"""WAC-VIS sensor frame-geometry constants -- true of the physical camera regardless of which
extraction/comparison method reads it. Deliberately dependency-free.
"""

SAMPLES = 704
# Per-frame TDI line count for one VIS filter block (`sumMode=1`, no TDI summing applied) -- the
# stitched-cube line-height scale factor `isis_wac.py`'s ISIS pipeline and `wac_camera_model.py`'s
# hand-rolled projector both use to convert a frame-index-based quantity to a line count.
VIS_BLOCK_HEIGHT = 14
