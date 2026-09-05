# Design: split `isis_wac.py`, remove its circular imports

**Status: not started.** First step of a broader source-code reorganization — see
[`open-items.md`](open-items.md)'s "Source code reorganization" section for the remaining
steps and the target-naming table covering all of them.

## Context

`isis_wac.py` (1402 lines) mixes two concerns: running the ISIS pipeline
(`lrowac2isis`→`spiceinit`→`lrowaccal`→`framestitch`→`crop`→`cam2map`, plus CSM ISD generation) and
answering ground-truth ground↔image queries against an already-processed cube via ISIS's `campt`.
The test suite already reflects this seam — `tests/test_isis_wac_ground_to_image.py` covers only the
second concern.

The file also sits at the center of three module-level circular-import problems:

- `camera.py`'s `build_camera()` needs `isis_wac.run_pipeline`/`ground_point_at_pixel` for a real
  boresight correction — genuine, not incidental — so it can't import `isis_wac` normally; today it
  does `from trntest import isis_wac` inside the function body
  (`camera.py:567`, `# noqa: PLC0415 -- circular otherwise`).
- `lunaserv.py`'s Hapke-calibration path needs `isis_wac.ensure_isisdata` — same pattern, same
  workaround (`lunaserv.py:1095`).
- `isis_wac.py` and `wac_camera_model.py` import each other at module scope with no workaround at
  all. It doesn't crash today only because neither does a name-specific `from trntest.X import Y` on
  the other — both bind the whole module and defer attribute access to inside function bodies. This
  is fragile: changing either import to name-specific form would break module load order.

Tracing what each direction actually needs at runtime (not just in a type annotation) shows the
cycles are mostly unnecessary:

- `isis_wac.py`'s imports of `Camera`/`FrameTiming` (from `camera.py`) and `PoseCorrection` (from
  `wac_camera_model.py`) are **type annotations only** — never constructed, never `isinstance`-checked.
- `isis_wac.py`'s import of `DemOrthoResult` (from `lunaserv.py`) is the same — annotation only.
- `wac_camera_model.py`'s import of `isis_wac` is a **real runtime call**
  (`isis_wac.ephemeris_time_at_pixel`, in `calibrate_et_per_crop_line`).
- `isis_wac.py`'s import of `lunaserv` for `local_orthographic_crs`/`geographic_crs` (in
  `sample_local_dem_patch`) is also a **real runtime call**, not annotation-only.

## The split

New module **`isis_campt.py`** — everything that queries an already-processed cube via ISIS's `campt`
(ground-truth ground↔image lookups) or generates the CSM ISD such a query needs:

- `GroundToImageModel`, `resolve_ground_to_image_model`
- `ground_to_image_pixel`, `ground_to_image_pixels_batch`, `image_to_ground_points_batch`
- `campt_photometric_angles`, `ground_point_at_pixel`, `ephemeris_time_at_pixel`, `cube_serial_number`
- `IsdGenerateResult`, `run_isd_generate`, `run_isd_generate_for_crop` (grouped here because
  `resolve_ground_to_image_model` depends on `run_isd_generate`, not because ISD generation is
  itself a `campt` call)
- `run_mapproject` (isis_wac.py's own CSM-ISD reprojection, not `render.run_mapproject`) — moves here
  too, alongside the ISD family it depends on

`isis_wac.py` keeps the pipeline itself:

- `ensure_isisdata`, `ensure_lunar_shape_model`
- `EdrFetchResult`, `fetch_edr_img`
- `Lrowac2IsisResult`, `run_lrowac2isis`
- `SpiceinitResult`, `run_spiceinit`, `resolve_wac_ck_kernels` + its private helpers
- `LrowaccalResult`, `run_lrowaccal`
- `FramestitchResult`, `run_framestitch`, `run_pipeline`
- `crop_window_for_camera`, `CropResult`, `crop_for_camera`
- `_orthographic_map_pvl`, `run_cam2map_for_crop`, `_table_extra_label`
- `apply_pose_correction_to_crop`, `attach_dem_shape_model`
- `sample_lunar_dem_radii_batch`, `sample_local_dem_patch` (these use `mappt`, a different tool for a
  different question — DEM elevation sampling, not camera ground-truth — and have no dependency on
  anything moving to `isis_campt.py`)

`run_mapproject` (isis_wac.py:642) is currently marked **Deprecated** in its own docstring — it has
no call sites outside this file, superseded by `run_cam2map_for_crop`'s ISIS-native reprojection.
Keep it (moved to `isis_campt.py`, not deleted): the reason it's unused is a specific, named upstream
bug (`usgscsm`'s Pushframe `groundToImage` has an unreliable secant search over framelet index — see
`docs/external-tools.md`'s "ISIS Pushframe pipeline" section), not a rejection of the CSM approach
itself. If that bug is ever fixed upstream, this is the more architecturally direct path (reprojects
via a portable CSM ISD rather than ISIS's own native camera model). Rewrite its docstring's framing
accordingly when it moves — replace "**Deprecated** -- `run_cam2map_for_crop` is the accurate path
now" with something like: "Not used today: depends on `usgscsm`'s Pushframe `groundToImage`, which
has an unreliable secant search over framelet index (see docs/external-tools.md). Preferable to
`run_cam2map_for_crop` once that's fixed upstream — reprojects through a portable CSM ISD rather than
ISIS's own native camera model." — stating the real blocker and the condition under which this
becomes the preferred path, not just "deprecated."

Result: `isis_campt.py` ends up roughly 500-550 lines; `isis_wac.py` drops from 1402 to roughly
900-950 lines.

`isis_campt.py` depends on `isis_wac.py` (for `CropResult`, `FramestitchResult`,
`crop_window_for_camera`, and the `SAMPLES`/`VIS_BLOCK_HEIGHT` constants `isis_wac.py` already
re-exports — currently from `wac.py`, see `open-items.md`'s `wac.py` entry for where those constants
land instead) — one-directional, no cycle.

## Fixing the cycles

1. Add `from __future__ import annotations` (PEP 563) to `isis_wac.py` and `isis_campt.py` — without
   it, guarding an import behind `if TYPE_CHECKING:` while a plain (non-string) annotation still
   references that name raises `NameError` at function-definition time. Neither file uses this import
   today, and the mypy config already supports it (`requires-python = ">=3.11"`).
2. In `isis_wac.py` and `isis_campt.py`, move the `Camera`/`FrameTiming` import (from `camera.py`) and
   the `PoseCorrection` import (from `wac_camera_model.py`) behind `if TYPE_CHECKING:`. Same for
   `DemOrthoResult` (from `lunaserv.py`) in `isis_wac.py` — this removes one of `isis_wac.py`'s two
   reasons to import `lunaserv`, but not the other (see step 5).
3. `camera.py`: delete the lazy `from trntest import isis_wac` inside `build_camera()` and its
   `# noqa: PLC0415` comment; import `isis_wac` and `isis_campt` normally at the top of the file.
4. `wac_camera_model.py`: change `from trntest import isis_wac` to `from trntest import isis_campt`
   (its one real call, `ephemeris_time_at_pixel`, is moving there).
5. `lunaserv.py`: leave its lazy `from trntest import isis_wac` (for `ensure_isisdata`) as is. The
   `isis_wac`↔`lunaserv` cycle isn't fully resolved by this task — `isis_wac.sample_local_dem_patch`
   still needs a real `lunaserv.local_orthographic_crs`/`geographic_crs` call. It resolves as a
   byproduct of the `lunaserv.py` split (next task): once `geographic_crs`/`local_orthographic_crs`
   move to a dependency-free `geo_utils.py`, `isis_wac.py` imports that instead of `lunaserv`, and
   `lunaserv.py`'s own `isis_wac` import can become a normal top-level one too.

## Call-site updates

Every other module that reaches into `isis_wac.py` for something moving to `isis_campt.py` needs a
second import added alongside its existing `isis_wac` one:

- **`tie_points.py`**: `ground_point_at_pixel` moves; `run_pipeline`, `crop_for_camera`, `CropResult`,
  `apply_pose_correction_to_crop` stay.
- **`control_network.py`**: `GroundToImageModel`, `cube_serial_number`, and its use of the
  `ground_to_image_pixels_batch`/`image_to_ground_points_batch` family move; `sample_lunar_dem_radii_batch`
  stays.
- **`trn_dataset.py`** (line 593): `isis_wac.run_isd_generate_for_crop` → `isis_campt.run_isd_generate_for_crop`.
- **`sfs_validation.py`**: only uses `ensure_lunar_shape_model` (stays put) and mentions
  `resolve_ground_to_image_model` in a comment — update the comment reference.
- **`spice_kernels.py`**: only uses `resolve_wac_ck_kernels` (stays put) — unaffected.

Don't treat this list as exhaustive by itself — `trntest-lint`'s mypy pass and the existing test suite
will surface any missed reference (an `AttributeError` on `isis_wac.<moved name>` or a mypy unresolved
name), so verify against those rather than re-deriving the call-site list from scratch.

## Test updates

`tests/test_isis_wac_ground_to_image.py` tests exactly the functions moving to `isis_campt.py` —
rename it to `tests/test_isis_campt.py` and repoint its `from trntest import isis_wac, wac_camera_model`
import. `tests/test_isis_wac_parsing.py` and `tests/test_isis_wac_dem.py` test functions that stay in
`isis_wac.py` and don't need renaming.

## Verification

1. `trntest-lint --all` — catches unresolved names/imports from the `TYPE_CHECKING` guards and the
   moved functions.
2. `pytest` (fast suite) plus `scripts/run_heavy_tests.sh` for the ISIS-touching heavy tests
   (`test_isis_wac_ground_to_image.py`/`test_isis_campt.py` in particular).
3. `scripts/run_notebook.sh notebooks/image_generation.py` and `notebooks/wac_isis.py` end to end —
   both exercise `camera.build_camera()` (the code path the removed `noqa` lazy import used to guard)
   and the ISIS pipeline directly.
