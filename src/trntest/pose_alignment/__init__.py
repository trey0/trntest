"""Camera-pose-alignment tooling: feature-match a map-projected WAC crop against the basemap and fit
a correction, at two levels of rigor. `tie_point_matching.py` works entirely in 2D map/image space
(fit-and-apply, no camera model); `wac_camera_model.py` (a hand-rolled WAC Pushframe forward
projector, built because ISIS's `jigsaw` bundle adjuster has a confirmed bug for this camera) and
`control_network.py` (converts `tie_point_matching`'s 2D matches into 3D ISIS control points) are the
projection-aware next step. See `docs/pose-alignment.md` for the full investigation and status.

**On the back burner, not wired into the main pipeline** -- validated standalone tooling, not dead
code. `notebooks/pose_alignment_spike.py` exercises `tie_point_matching.py` end-to-end.

No re-exports here -- import the specific submodule needed (`from trntest.pose_alignment import
tie_point_matching`), matching this project's style elsewhere (e.g. `dem_ortho.py`'s sibling
data-source modules).
"""
