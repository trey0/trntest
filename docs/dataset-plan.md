# Design: `TrnTestDataSet` / `TrnTestEntry` / `TrnTestImage`

**Status: designed, not yet implemented.** This is a design doc for a future session to pick up —
see `docs/plan.md`'s "Known open items" for the pointer. Everything below was worked out and
confirmed with the user in a planning-only session (no code changes made); implementing it is the
next step, not something already done.

## Context

Today, generated pipeline output is ad hoc: `dataset.generate_dataset()` writes a flat
`output_dir/dataset/<product_id>/` tree for the synthetic side, and `isis_wac.py`'s real-WAC
pipeline is entirely notebook-driven, writing to a separate `scratch_dir/isis_wac/<edr_product>/`
tree. There's no reusable object representing "one generated image" or "a collection of them,"
and no way to generate a batch of images incrementally/resumably. The goal, since this project
expects multiple data sets going forward: each tied to one self-contained folder (manifest + typed
subfolders), with an API that makes iterating and plotting easy, and that can populate an empty
folder efficiently (a real, filesystem-based task queue, multi-worker-safe) rather than requiring
everything up front.

**Two-level object model** (refined after an initial one-level draft, in review with the user):
each manifest row is a `TrnTestEntry`, holding the state shared across all of that row's generated
products (camera, frame timing, DEM/ortho, etc.). Each product type within an entry (`entry.crop`,
`entry.hillshade`, later `entry.reproject`) is a `TrnTestImage` — a small base class implementing
the genuinely shared logic (`plot_vs_basemap()`, `plot_overlay()`, idempotent `generate()`,
`exists()`) once, with per-type subclasses supplying only the type-specific pieces (which raster,
which rotation, which real-world dimensions, which footprint, how to produce a mapprojected
version of themselves). This is real code reuse, not just a naming convenience — see "Class
design" below.

Confirmed scope for the first implementation pass: **`crop` and `hillshade` generation only** —
`reproject` (a real WAC crop → intermediate map projection → `sat_sim` re-render onto the exact
synthetic camera's pixel grid) is new, untested ground technically, and explicitly deferred; the
folder/naming convention and class hierarchy account for it existing later (a future
`TrnTestReprojectImage(TrnTestImage)`, possibly even subclassing `TrnTestHillshadeImage` since it
would go through the same `sat_sim`-render-then-mapproject shape, just fed a different `--ortho`
source), but no generation code targets it yet. `image_generation.py` should get wired up to the new
code in the same pass that implements it (`data_set_selection.py`, the other notebook at the time
this was written, was later removed once `dataset_manifest.csv` was frozen -- see `docs/history.md`'s
dated entry).

## On-disk layout & naming

```
<dataset_folder>/                      # config.output_dir / "trn_dataset" by default — deliberately
                                        # NOT "dataset", to avoid colliding in meaning with
                                        # dataset.generate_dataset()'s existing flat layout at
                                        # output_dir/dataset/<product_id>/, which this doesn't replace
  manifest.csv                         # DATASET_COLUMNS, via existing dataset.write_manifest/read_manifest
  crop/<edr_product>_crop.cub          # isis_wac.crop_for_camera's cube, copied here
  crop/<edr_product>_crop.json         # isis_wac.run_isd_generate_for_crop's ISD — accurately scoped
                                        # to the crop, but not reprojection-reliable — see "Crop
                                        # sidecar: accurate, not just informational" below
  hillshade/<edr_product>_hillshade.tif   # render.RenderResult.rendered_tif, copied here (pure
  hillshade/<edr_product>_hillshade.json  #   relocation — see "Hillshade = pure relocation" below)
  reproject/                           # reserved, empty for now; naming convention only
  _work/<edr_product>/                 # per-entry intermediates (.tsai, DEM/ortho tiles, pre-copy
                                        # render output) — kept out of crop/hillshade so those only
                                        # ever hold the canonical named pair
                                        # (no .locks/ anymore — see "Task queue" below, superseded)
```

Filenames key on **`edr_product`** (matches the user's own literal example — `M1327210646CE` →
`crop/M1327210646CE_crop.{cub,json}` — and how `isis_wac.py`'s scratch dir already keys everything
for the real-WAC side); row lookup (`dataset[key]`) keys on **`product_id`** (matches
`generate_dataset()`'s existing per-image folder convention). In today's manifest these are always
equal, so this split is low-risk.

`isis_wac.py`'s own raw EDR-processing scratch (stitched cube, calibration intermediates — real
~10-20s of ISIS work) **stays in `config.scratch_dir/isis_wac/<edr_product>/`, shared/reused
across datasets** (confirmed with the user) rather than duplicated inside `_work/` — two datasets
referencing the same EDR product share that expensive work instead of redoing it.

**Superseded (2026-08-23, `docs/intermediate-product-plan.md`'s Phase 3)**: this decision was
deliberately reversed — the cross-dataset-reuse case above wasn't actually load-bearing (real
datasets are non-overlapping in `edr_product` by construction), while the shared, un-namespaced
`scratch_dir/isis_wac/<edr_product>/` path was a real, confirmed-live concurrency hazard
(`docs/environment.md`'s "Other sharp edges" section). `isis_wac._spike_dir` now returns
`_work/<entry>/isis/` instead — see `docs/history.md`'s dated entry for the full change.

## Hillshade = pure relocation, no pipeline change

Traced precisely: `sat_sim --ortho <dem_ortho_result.ortho>` already renders the hillshade-blended
texture today (hillshade gets baked into `DemOrthoResult.ortho` by
`lunaserv.despeckle_and_shade_ortho` *before* `sat_sim` ever runs) — so "hillshade base map data
reprojected using sat_sim" is a literal description of today's existing synthetic render
(`RenderResult.rendered_tif`/`csm_json`, i.e. `render.run_sat_sim`'s output). No changes to
`render.py`/`lunaserv.py` logic — `TrnTestHillshadeImage._generate_impl()` just calls the existing
`render.run_sat_sim` and copies its two output files into `hillshade/`.

## Class design (new module `src/trntest/trn_dataset.py`)

```python
class TrnTestDataSet:
    def __init__(self, folder: Path, images: pd.DataFrame, config: TrntestConfig): ...

    @classmethod
    def create(cls, folder, images: pd.DataFrame, config: TrntestConfig | None = None) -> "TrnTestDataSet":
        """Idempotent: (re)writes manifest.csv from `images`, ensures crop/reproject/hillshade/
        _work/.locks exist. Never touches already-generated product files."""

    @classmethod
    def open(cls, folder, config: TrntestConfig | None = None) -> "TrnTestDataSet":
        """Reads manifest.csv from an existing folder — no `images` needed. This is what makes
        "start from an empty folder with just a manifest" work."""

    def __len__(self) -> int: ...
    def __iter__(self) -> Iterator["TrnTestEntry"]: ...
    def __getitem__(self, key: int | str) -> "TrnTestEntry": ...   # int=positional, str=product_id

    def populate(self, product_types=("crop", "hillshade"), retry_failed: bool = False, limit: int | None = None) -> None: ...
    # limit, added post-implementation: stop after this call has done genuinely new work on `limit`
    # distinct entries, so a batch population can be split across multiple separate worker
    # invocations against the same folder -- see trn_dataset.py's own docstring for the exact
    # semantics (an entry already done/in-progress/failed doesn't consume the budget).
    def status(self, product_types=("crop", "hillshade")) -> pd.DataFrame: ...   # queue progress table


class TrnTestEntry:
    """One manifest row's worth of shared, cached, expensive-to-derive state -- computed once per
    entry (functools.cached_property throughout), reused by every product-type image below it."""

    def __init__(self, row: pd.Series, dataset_folder: Path, config: TrntestConfig): ...

    @property
    def edr_product(self) -> str: ...
    @property
    def product_id(self) -> str: ...

    per_image_config: TrntestConfig       # dataclasses.replace(...), same pattern generate_dataset() uses,
                                           # output_dir=dataset_folder/"_work"/edr_product
    frame_timing: FrameTiming             # camera.fetch_frame_timing(per_image_config)
    camera: Camera                        # camera.build_camera(per_image_config)
    stitched: isis_wac.FramestitchResult  # isis_wac.run_pipeline(...)
    crop_result: isis_wac.CropResult      # isis_wac.crop_for_camera(...) -- scratch-dir cube,
                                           # distinct from TrnTestCropImage.raster_path (dataset-folder copy)
    crop_footprint: dict                  # tie_points.crop_footprint_corners_for_camera(...)
    dem_ortho_result: DemOrthoResult       # loaded from already-generated files via new
                                           # lunaserv.result_from_files() when possible, else fetched fresh
    rotations: DisplayRotations           # orientation.compute_display_rotations(...)

    @property
    def crop(self) -> "TrnTestCropImage": ...            # cached_property
    @property
    def hillshade(self) -> "TrnTestHillshadeImage": ...  # cached_property
    # .reproject: reserved, not implemented yet

    @property
    def images_by_type(self) -> dict[str, "TrnTestImage"]:
        return {"crop": self.crop, "hillshade": self.hillshade}


class TrnTestImage(abc.ABC):
    """One product type of one entry. Owns the genuinely shared logic ONCE; subclasses supply only
    the small type-specific pieces. This is the actual code-reuse point behind this design."""

    def __init__(self, entry: TrnTestEntry): self.entry = entry

    # -- subclasses implement (small, type-specific) --
    @property
    @abc.abstractmethod
    def raster_path(self) -> Path: ...              # crop.cub / hillshade.tif -- dataset-folder copy
    @property
    @abc.abstractmethod
    def sidecar_json_path(self) -> Path: ...          # crop's ISD / hillshade's CSM json
    @property
    @abc.abstractmethod
    def rotation_k(self) -> int: ...                  # entry.rotations.k_crop / .k_synthetic
    @property
    @abc.abstractmethod
    def width_km(self) -> float: ...
    @property
    @abc.abstractmethod
    def height_km(self) -> float: ...
    @property
    @abc.abstractmethod
    def footprint_lonlat_deg(self) -> dict: ...
    @property
    @abc.abstractmethod
    def render_label(self) -> str: ...
    @property
    @abc.abstractmethod
    def tie_point_px_key(self) -> str: ...
    @abc.abstractmethod
    def _generate_impl(self) -> None: ...             # produce + copy raster_path/sidecar_json_path
    @abc.abstractmethod
    def _mapprojected_path(self) -> Path: ...          # type-specific mapproject/cam2map step

    # -- shared, implemented once --
    def exists(self) -> bool:
        return self.raster_path.exists() and self.sidecar_json_path.exists()

    def generate(self) -> Path:                        # idempotent template method
        if not self.exists():
            self._generate_impl()
        return self.raster_path

    def _require_generated(self) -> None:
        if not self.exists():
            raise FileNotFoundError(f"{self.render_label} not generated yet for "
                                     f"{self.entry.edr_product} -- call .generate() or dataset.populate() first")

    def plot_vs_basemap(self, tie_point_results=None, title=None):   # ~Phase 5A / 6A
        self._require_generated()
        plotting.plot_render_vs_basemap(
            plotting.read_raster_band(self.raster_path), self.rotation_k,
            self.width_km, self.height_km, self.footprint_lonlat_deg,
            self.entry.dem_ortho_result.ortho,
            title=title or f"{self.render_label} vs. hillshade-based basemap",
            render_label=self.render_label, tie_point_results=tie_point_results,
            render_px_key=self.tie_point_px_key,
        )

    def plot_overlay(self, title=None):                              # ~Phase 5B / 6B
        self._require_generated()
        plotting.plot_overlay(self.entry.dem_ortho_result.ortho, self._mapprojected_path(),
            title=title or f"{self.render_label} (mapprojected) over hillshade-based basemap")


class TrnTestCropImage(TrnTestImage):
    # raster_path        = .../crop/<edr_product>_crop.cub
    # sidecar_json_path  = .../crop/<edr_product>_crop.json
    # rotation_k         = entry.rotations.k_crop
    # width_km           = entry.camera.cross_track_width_km
    # height_km          = entry.camera.n_frames_for_square_crop * entry.camera.km_per_frame
    # footprint_lonlat_deg = entry.crop_footprint
    # render_label       = "Real WAC (ISIS-processed)"
    # tie_point_px_key   = "crop_px"
    # _generate_impl()   = isis_wac.run_isd_generate_for_crop(entry.crop_result, entry.camera,
    #                      entry.stitched.flip, entry.per_image_config)  -- NEW function, see
    #                      "Crop sidecar: accurate, not just informational" below; copy
    #                      entry.crop_result.cub_path -> raster_path, isd.json_path -> sidecar_json_path
    # _mapprojected_path() = isis_wac.run_cam2map_for_crop(entry.crop_result, entry.dem_ortho_result,
    #                        entry.per_image_config)  -- operates on the scratch-dir crop_result, not
    #                        raster_path, so cam2map's own intermediates don't spill into crop/


class TrnTestHillshadeImage(TrnTestImage):
    # raster_path        = .../hillshade/<edr_product>_hillshade.tif
    # sidecar_json_path  = .../hillshade/<edr_product>_hillshade.json
    # rotation_k         = entry.rotations.k_synthetic
    # width_km / height_km = entry.camera.cross_track_width_km (square by construction)
    # footprint_lonlat_deg = entry.camera.footprint_lonlat_deg
    # render_label       = "Synthetic (sat_sim, SPICE-posed)"
    # tie_point_px_key   = "synthetic_px"
    # _generate_impl()   = render.run_sat_sim(entry.camera, entry.dem_ortho_result, entry.per_image_config);
    #                      copy rendered_tif -> raster_path, csm_json -> sidecar_json_path
    # _mapprojected_path() = render.run_mapproject_image(raster_path, sidecar_json_path, <out>,
    #                        entry.dem_ortho_result, entry.per_image_config)
```

Tie points stay **out of scope for the class hierarchy in the first pass** (matches the current
notebook's own Phase-7-only usage) — `plot_vs_basemap()`'s `tie_point_results` param mirrors
`plotting.plot_render_vs_basemap`'s existing optional param; a notebook wanting them computes/
resolves manually using `entry.camera`/`entry.frame_timing`/`entry.stitched`/`entry.crop_result`
(all public).

## Crop sidecar: accurate, not just informational

**Revised after user pushback on an earlier draft.** An earlier draft of this design proposed just
copying the *full-cube* ISD (`run_isd_generate(entry.stitched, ...)`, the one
`resolve_ground_to_image_model` already produces today) next to the crop cube, documented as
"informational only." The user rejected that: on principle, the sidecar's own metadata (image
dimensions, time bounds) should accurately describe the crop it sits next to, using our best
understanding of CSM semantics — not describe a differently-sized cube. This is a distinct concern
from whether the ISD is *usable for reprojection* at all, which is separately, already known to be
unreliable (the confirmed `usgscsm` `UsgsAstroPushFrameSensorModel::groundToImage` bug — an
unbracketed secant search over framelet index — affects *any* Pushframe ISD, correctly-scoped or
not, even USGS's own unmodified tooling on an unmodified full cube). So: fix the sidecar's
*accuracy*, accept that it still can't be trusted for real ground↔image queries (that's a
sensor-model bug, not an ISD-authoring problem) — real ground↔image lookups keep going through
`resolve_ground_to_image_model`/`ground_to_image_pixel` regardless, unaffected by any of this.

**New function, `isis_wac.run_isd_generate_for_crop(crop: CropResult, camera: Camera, flip: bool,
config=None) -> IsdGenerateResult`** (distinct from the existing full-cube-only
`run_isd_generate`, whose docstring explicitly restricts it to the full stitched cube and is tied
to `resolve_ground_to_image_model`'s/`run_mapproject`'s different contract — not touched by this
work):
1. Runs `isd_generate -i` directly against `crop.cub_path` (the actual cropped cube ISIS's own
   `crop` app produced) rather than the full stitched cube — so the resulting JSON's own image
   dimensions/frame count are read from, and correctly reflect, the crop's real size.
2. **Patches the time-anchoring fields** — `crop`'s default `PROPSPICE=true` updates
   `ck_table_original_size` to the cropped line count but does **not** re-anchor the *start* time
   of the per-line pointing cache to the crop's new first line (a real, confirmed ISIS `crop`
   behavior — see `docs/data-sources.md`'s "`isd_generate -i` on an ISIS-`crop`ped Pushframe cube"
   entry for the original diagnosis). Compute `line_offset = crop_window_for_camera(camera).row_off`
   and `time_offset_s = (line_offset / VIS_BLOCK_HEIGHT) * isd["interframe_delay"]`, then shift
   `starting_ephemeris_time`/`ending_ephemeris_time`/`center_ephemeris_time` and
   `instrument_pointing.ck_table_start_time`/`ck_table_end_time` by it — reviving the exact,
   previously-validated formula from this repo's own history (confirmed there to bring a cropped
   ISD's self-consistency to 0.999 correlation — the patch itself was correct; the later-discovered
   `usgscsm` bug is a separate issue this patch was never responsible for and can't fix). This code
   was fully removed after that investigation moved on to the deeper bug — it needs to be
   re-implemented fresh, not literally revived from a dormant function (confirmed via grep: no
   trace of `time_offset_s`/`ck_table_start_time`/`starting_ephemeris_time` patching remains
   anywhere in the current `isis_wac.py`).
3. Also patches `framelet_order_reversed = flip`, same as `run_isd_generate` already does for the
   full-cube case (confirmed still necessary and independent of the timing fix).

**Docstring/`docs/data-sources.md` framing**: "this ISD accurately describes `crop.cub`'s own
dimensions and time bounds — but, like any Pushframe ISD in this codebase, `usgscsm`'s
`groundToImage` is not reliable enough to use it for actual reprojection; real ground↔image lookups
go through `resolve_ground_to_image_model`/`ground_to_image_pixel` instead." Not "informational
only, by design" — accurate, just not sufficient on its own for reprojection given a separate,
already-diagnosed sensor-model bug.

**Verification, since this is genuinely new logic, not a relocation**: during implementation,
confirm empirically (this repo's own "validate empirically" convention) that the patched JSON's
own stated image dimensions match `crop.cub_path`'s real raster dimensions, and that the patched
time fields land within the crop's own real acquisition window (cross-check against `campt` at the
crop's first/last line, the same technique `docs/history.md`'s original investigation used).

**Fallback, only if this proves out of scope during implementation**: copying the full-cube ISD
as a literal stopgap is acceptable *only* if explicitly labeled as a temporary stopgap with a
tracked TODO to replace it with the above — never characterized in code/docs as an accepted,
permanent "informational only" design choice, per the user's explicit direction.

## Task queue

**Superseded.** The filesystem-only design below (`.locks/<product_id>_<product_type>.lock`/
`.error`, atomic `os.O_CREAT|O_EXCL` claims) is what was originally implemented, and worked, but was
later replaced with the `huey` library (sqlite backend) to cut this project's own concurrency-
sensitive bookkeeping code and lean on a known-good queue implementation instead — see
`docs/history.md`'s dated entry for the migration and its rationale, and `src/trntest/tasks.py`'s
module docstring for the current design (one `huey` instance per worktree's `output_dir`, not
per-dataset-folder; `immediate=True` + `immediate_use_memory=False` so `populate()` keeps its
original synchronous, no-consumer-needed behavior while still persisting failures to real sqlite).
A second `huey` instance, `huey_parallel` (`immediate=False`), plus `TrnTestDataSet.
populate_via_workers()` were added later still, for real multi-worker batch population through a
managed `huey_consumer -k process` subprocess — see `docs/history.md`'s later dated entry.
Left below for historical context — nothing here describes current behavior.

Filesystem-only, no persisted job DB — task list is always `manifest rows × implemented product
types`, state always derived:
```python
PRODUCT_TYPES = ("crop", "hillshade")

def task_state(entry: TrnTestEntry, product_type: str) -> str: ...   # "done"|"in_progress"|"failed"|"pending"
def claim_task(dataset, product_id, product_type) -> bool: ...        # os.open(lock, O_CREAT|O_EXCL) — atomic
def mark_done(dataset, product_id, product_type) -> None: ...
def mark_failed(dataset, product_id, product_type, exc) -> None: ...  # writes .error, clears lock
def claim_next_task(dataset, product_types=PRODUCT_TYPES) -> tuple[str, str] | None: ...
def clear_lock(dataset, product_id, product_type) -> None: ...        # manual crash recovery
```
`done` = `entry.images_by_type[product_type].exists()`; `failed` = a `.error` sidecar exists;
`in_progress` = a `.lock` file exists; `pending` = none of the above. **Confirmed with the user**:
`populate()` itself stays sequential (`claim_next_task → image.generate() → mark_done/failed` in a
loop until nothing's claimable) — the claim primitive is already safe across **separate OS
processes** with zero extra code (multiple `docker compose run` invocations against the same
folder), which is the real "workers" story here, consistent with this repo's own documented rule
that SPICE/spiceypy state is process-global and unsafe across concurrent calls within one process.
No in-process `ProcessPoolExecutor`, no stale-lock PID-liveness reaping (workers are realistically
separate containers — a PID recorded in one means nothing to another; recovery is `clear_lock()` +
rerun).

## Notebook handoff (confirmed with the user)

`notebooks/dataset_manifest.csv` **stays exactly as it is today** — the small, git-tracked,
reproducibility anchor (unchanged `trntest.write_manifest`/`read_manifest`). Both notebooks should
call `TrnTestDataSet.create(session.config.output_dir/"trn_dataset", images, session.config)`:
`data_set_selection.py`'s last cell, as a cheap convenience/validation step after writing the CSV;
`image_generation.py`'s first cell, as its own authoritative entry point (reads the git-tracked CSV
first, so it still has **no runtime dependency on `data_set_selection.ipynb` having run** in this
environment — matching the property established when the flagship notebook was originally split).
`create()` is idempotent (safe to call from both), so this is not a race or a duplicate-work
concern.

**Update, later session**: `data_set_selection.py`/`.ipynb` was removed once nothing else depended
on re-running it (`dataset_manifest.csv` frozen as a static checked-in file instead) — see
`docs/history.md`'s dated entry. `image_generation.py`'s own `TrnTestDataSet.create()` call, as
described above, is unaffected -- it never had a runtime dependency on the other notebook to begin
with.

## Notebook rewiring

`image_generation.py`: `dataset = TrnTestDataSet.create(...)` → `dataset.populate()` → `entry =
dataset[0]` → `entry.hillshade.plot_vs_basemap(...)` / `entry.hillshade.plot_overlay()` /
`entry.crop.plot_vs_basemap(...)` / `entry.crop.plot_overlay()`, replacing today's Phase 2–6B cell
sequence. Phase 7 (`plot_isis_comparison`) and the CSM-json/sanity-check display cells stay manual
notebook cells, now reading off `entry.camera`/`entry.hillshade.raster_path`/
`entry.crop.raster_path`/`entry.rotations`/`entry.stitched`/`entry.crop_result` — genuine
display/prose logic stays in the notebook; only reusable generation/plotting logic moves into the
classes.

## Files to touch

| File | Change |
|---|---|
| `src/trntest/trn_dataset.py` | **New**: `TrnTestDataSet`, `TrnTestEntry`, `TrnTestImage` (abstract base), `TrnTestCropImage`, `TrnTestHillshadeImage`, queue primitives, `PRODUCT_TYPES`. |
| `src/trntest/isis_wac.py` | Add `run_isd_generate_for_crop(crop, camera, flip, config=None) -> IsdGenerateResult` — new function, real logic (crop-scoped `isd_generate` call + time-offset patch), not a relocation. `run_isd_generate`/`crop_window_for_camera`/`VIS_BLOCK_HEIGHT` unchanged, reused by it. |
| `src/trntest/lunaserv.py` | Add `result_from_files(ortho_path, dem_path) -> DemOrthoResult` (pure IO, no new pipeline logic). |
| `src/trntest/dataset.py` | Optional small refactor: extract shared `_per_image_config(row, config, output_dir)` helper used by both `generate_dataset()`'s loop (behavior-unchanged) and `TrnTestEntry.per_image_config`. |
| `src/trntest/__init__.py` | Export `TrnTestDataSet`, `TrnTestEntry`, `TrnTestImage` (queue primitives stay `trn_dataset.*`-only, not top-level). |
| `notebooks/data_set_selection.py`/`.ipynb` | Last cell also calls `TrnTestDataSet.create(...)`. |
| `notebooks/image_generation.py`/`.ipynb` | Phases 2–6B rewritten around `TrnTestDataSet`/`TrnTestEntry`/`TrnTestImage`; Phase 7 + sanity cells adapted to read off the new objects' public properties. |
| `docs/plan.md` | New `trn_dataset.py` architecture row; correct the stale "`run_isd_generate`... no longer used" claim (it's already called via `resolve_ground_to_image_model` today); describe the new dataset-folder flow. |
| `docs/data-sources.md` | Document the folder/naming convention and the crop-sidecar caveat as durable facts. |
| `docs/history.md` | New dated entry describing the feature and the design decisions actually adopted, once implemented. |
| `tests/test_trn_dataset.py` | **New** — see below. |

## Tests to write (`tests/test_trn_dataset.py`, `tmp_path` + fakes, no real SPICE/ASP/ISIS)

`create()` writes manifest+subfolders; `open()` needs only a manifest (no `images` arg); `create()`
called twice preserves existing product files and overwrites manifest text; `TrnTestDataSet.
__len__`/`__iter__`/`__getitem__` by index and by `product_id` (+ `KeyError`); exact path naming
against a fabricated `edr_product="M1327210646CE"` entry (the user's own example) for both
`entry.crop`/`entry.hillshade`; a fake `TrnTestImage` subclass (minimal, no real generation) to
test the shared base-class logic in isolation — `exists()`, `generate()`'s idempotency (doesn't
call `_generate_impl()` twice), `_require_generated()` raising before generation; `task_state`'s
four cases via fabricated on-disk files/locks/errors; `claim_task` atomicity (second claim on the
same task fails); `mark_done`/`mark_failed` lock/error bookkeeping; `iter_pending_tasks`/
`claim_next_task` skip done/in-progress/failed; `populate()` (with `TrnTestCropImage`/
`TrnTestHillshadeImage._generate_impl` monkeypatched to fakes that just touch expected files)
drives every task to done and `status()` reflects it; `populate()` marks failed and continues past
one row's exception; `retry_failed=True` clears errors and reruns.

## Verification plan (once implemented)

1. `trntest-lint --all`; `pytest` (new tests + confirm existing `test_dataset.py`/
   `test_isis_wac_ground_to_image.py`/`test_session.py` pass unmodified — proves the optional
   `dataset.py` refactor is behavior-preserving).
2. Real Docker run: `scripts/run_notebook.sh notebooks/data_set_selection.py` then
   `scripts/run_notebook.sh notebooks/image_generation.py`.
3. **Empirical visual check** (this repo's own stated convention — not just "it ran"): open the
   regenerated `image_generation.ipynb`, confirm hillshade-vs-basemap and crop-vs-basemap show
   real, sensibly-aligned lunar terrain (not blank/NaN), and both overlay plots' outlines land
   where expected on the basemap.
4. Re-run `scripts/run_notebook.sh notebooks/image_generation.py` a second time and check the
   timing report — the `populate()` cell should complete near-instantly (nothing left pending), a
   concrete resumability check.
5. **Crop ISD accuracy check** (new logic, not a relocation — see "Crop sidecar" section): confirm
   `crop/<id>_crop.json`'s stated image dimensions match `crop/<id>_crop.cub`'s real raster
   dimensions, and that its patched time fields fall within the crop's real acquisition window
   (cross-check via `campt` at the crop's first/last line, same technique the original investigation
   used).
