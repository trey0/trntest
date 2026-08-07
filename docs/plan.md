# trntest — architecture & status

## What this is

Generates a synthetic lunar satellite image (with a CSM/"ISD" JSON camera sidecar) using NASA's
Ames Stereo Pipeline (ASP) `sat_sim` tool, fed by real DEM + visible imagery pulled live from the
Lunaserv WMS server — with the camera's pose derived from the **real LRO spacecraft trajectory**
(NAIF SPICE kernels) at the time of a real LROC WAC image, so the synthetic 256x256 frame
approximates the FOV of a real WAC swath and can be compared against it. This is a demo/exercise in
AI-assisted coding on a real geospatial engineering task.

All heavy tooling/build/test happens inside a Docker container (Ubuntu 24.04), built from the
checked-in `docker/Dockerfile`, so it's reproducible off this host.

## Status

The demo runs end-to-end and is stable: real LRO SPICE trajectory → posed synthetic camera →
`sat_sim` render + CSM/ISD sidecar → compared (with explicit SPICE-derived tie points) against a
properly band-separated, correctly-sized, correctly-posed crop of real WAC data. Packaged as an
installable library (`src/trntest/`) with config, tests, and style tooling.

The **live default path is catalog-driven, not a single hardcoded product**: `select_dataset()`
queries the real LROC catalog for a favorable multi-orbit window and returns a list of real,
illuminated WAC images; `generate_dataset()` renders the chosen one(s) through the same pipeline
described above. There is no current dependency on any one specific EDR product or framelet index —
see `docs/data-sources.md` for the couple of specific products still used as regression-test
fixtures, and `docs/history.md` if you're curious how the demo evolved from a single hand-picked
product to this.

## Architecture (`src/trntest/`)

| Module | Responsibility |
|---|---|
| `config.py` | `TrntestConfig`/`load_config()` — endpoints, paths, product IDs, tunables. TOML file + `TRNTEST_*` env var overrides. |
| `cache.py` | Local-mirror disk caching for all external fetches (NAIF, Lunaserv, LROC) — see `docs/caching.md`. |
| `spice_kernels.py` | Selects/downloads the minimal SPICE kernel set for a date, furnishes it (`fetch_and_furnish`, `furnish_spk_range`). |
| `camera.py` | Poses the synthetic camera from real SPICE trajectory/orientation data; `build_camera()`, `FrameTiming`/`fetch_frame_timing()` (EDR label parsing), sensor-axis convention (`boresight_rotation_k`). |
| `illumination.py` | Sun/orbit geometry via real SPICE functions — sun elevation/azimuth, sub-solar point, ascending-node search (`gfposc`). |
| `catalog.py` | PDS ODE REST API client — lists real EDR/CDR products by time range, matches EDR↔CDR pairs. |
| `dataset.py` | Public multi-image API: `select_dataset()` (catalog-driven selection), `generate_dataset()` (renders selected images through the single-image pipeline). |
| `lunaserv.py` | Fetches DEM + ortho imagery from Lunaserv WMS for a camera's footprint, in a per-camera local Orthographic CRS (`IAU2000:30166`, real Moon radius) centered on that footprint — genuinely isotropic meter pixels, unlike Lunaserv's native geographic grid. Despeckles the ortho and blends in a real-sun-lit hillshade (`sat_sim` applies no illumination model of its own). |
| `render.py` | Runs `sat_sim`/`cam_gen` to produce the rendered `.tif` + CSM/ISD JSON sidecar. `run_mapproject` reprojects the render back onto the map through that same CSM sidecar, for geo-aligned overlay display. |
| `wac.py` | Extracts a band-separated, along-track-stacked VIS mosaic from a real WAC CDR product. |
| `isis_wac.py` | Alternative to `wac.py`: reprojects a real WAC EDR through ISIS3's own pipeline (`lrowac2isis`/`spiceinit`/`lrowaccal`/`framestitch`) instead of manual framelet-stacking, then reprojects the result onto the DEM via ALE's `isd_generate` + ASP's `mapproject` -- must run against the stitched (interleaved) cube, not a lone even/odd parity (see the open items below). |
| `tie_points.py` | SPICE-derived ground tie points, projected into both images' pixel coordinates, for the comparison figure. |
| `orientation.py` | Notebook-display-only north-up rotation (does not touch the sensor model). |
| `plotting.py` | Comparison-figure plotting. `plot_overlay` displays two geo-aligned rasters (e.g. a `mapproject` output over `LunaservResult.ortho`) via `rioxarray`, using each file's own real coordinates rather than pixel indices. |
| `session.py` | `Session` facade — thin one-line delegators so notebook cells don't repeat `config=...`. |

`notebooks/lunar_sat_sim_demo.ipynb` drives all of the above end to end — see `README.md` to run it,
and AGENTS.md's "Working conventions" for how to validate changes against it.

## Known open items (resolve as encountered, record findings in `docs/data-sources.md`)

- Whether `--save-as-csm` state JSON is an acceptable stand-in for a literal ISD file for whatever
  comes after this demo.
- Confirm the lunar frame kernel defining `MOON_ME` loads correctly so SPICE can output that frame
  directly; sanity-check against the known GLD100/LOLA convention.
- **Resolved**: Lunaserv's native geographic projection is fine for `sat_sim`'s forward render, but
  turned out to break the `mapproject --ref-map` round-trip (anisotropic degree-pixels away from the
  equator, not preserved by `--ref-map`) — fixed by requesting a per-camera local Orthographic CRS
  directly from Lunaserv (`IAU2000:30166`, still a single WMS fetch, no separate `gdalwarp` step).
  See `docs/data-sources.md`'s Lunaserv WMS section and `docs/history.md`'s dated entry.
- **Resolved**: whether a real WAC swath can be reprojected onto the DEM via a genuine ISIS/CSM
  camera model (`mapproject`), as a principled alternative to `wac.py`'s manual framelet-stacking.
  The previously-reported "severe framelet-boundary striping" (`docs/history.md` Phase 12,
  `docs/data-sources.md`'s "ISIS3/CSM spike" section) turned out to be mostly a methodological
  artifact, not a fundamental CSM Pushframe limitation: mapprojecting a lone even/odd parity cube
  (each only ~50% populated — WAC alternates which nominal frame slot gets real data) leaves
  `mapproject` to resample across that sparsity, producing the smearing. Mapprojecting the properly
  interleaved *stitched* cube instead resolves the vast majority of it (31% valid coverage/no
  recognizable terrain → 81% valid coverage/real craters throughout, same real product). `isis_wac.py`
  now implements the full chain: EDR fetch → `lrowac2isis` → `spiceinit web=yes` → `lrowaccal` →
  `framestitch` → `isd_generate` → `mapproject` (via `render.run_mapproject_image`, shared with the
  synthetic render's own mapproject step). `notebooks/lunar_sat_sim_demo.py`'s Phase 5B/6A/6B
  demonstrate this: 5B overlays the mapprojected real WAC on the hillshade base, 6A is the
  side-by-side comparison against the synthetic render, 6B is the synthetic render's own
  mapprojected overlay (5B/6B share `plotting.plot_overlay`). See `docs/data-sources.md`'s "ISIS3/CSM
  spike" section and `docs/history.md`'s dated entry for the full investigation;
  `notebooks/wac_isis_spike.py` remains the step-by-step version for isolating pipeline stages.
- `geopandas` (added alongside `rioxarray` for `plotting.plot_overlay`) now has a concrete caller:
  `plot_overlay(show_overlay_outline=True)` traces the overlay raster's real (non-NaN) footprint and
  draws it as a vector boundary. A vector *data* layer (e.g. the Robbins crater database) on top of
  this raster overlay is still a possible future extension, not yet implemented.

## Development history

See `docs/history.md` for the phase-by-phase narrative — what was tried, what broke, and how each
design decision (framelet timing, sensor axis convention, catalog-driven selection, the perf fixes
in the sweep) was actually reached. Background/curiosity reading, not required before making a
change; this file and `docs/data-sources.md` describe current behavior.
