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
| `illumination.py` | Sun/orbit geometry via real SPICE functions — sun elevation, sub-solar point, ascending-node search (`gfposc`). |
| `catalog.py` | PDS ODE REST API client — lists real EDR/CDR products by time range, matches EDR↔CDR pairs. |
| `dataset.py` | Public multi-image API: `select_dataset()` (catalog-driven selection), `generate_dataset()` (renders selected images through the single-image pipeline). |
| `lunaserv.py` | Fetches DEM + ortho imagery from Lunaserv WMS for a camera's footprint; antimeridian-safe. |
| `render.py` | Runs `sat_sim`/`cam_gen` to produce the rendered `.tif` + CSM/ISD JSON sidecar. |
| `wac.py` | Extracts a band-separated, along-track-stacked VIS mosaic from a real WAC CDR product. |
| `tie_points.py` | SPICE-derived ground tie points, projected into both images' pixel coordinates, for the comparison figure. |
| `orientation.py` | Notebook-display-only north-up rotation (does not touch the sensor model). |
| `plotting.py` | Comparison-figure plotting. |
| `session.py` | `Session` facade — thin one-line delegators so notebook cells don't repeat `config=...`. |

`notebooks/lunar_sat_sim_demo.ipynb` drives all of the above end to end — see `README.md` to run it,
and AGENTS.md's "Working conventions" for how to validate changes against it.

## Known open items (resolve as encountered, record findings in `docs/data-sources.md`)

- Whether `--save-as-csm` state JSON is an acceptable stand-in for a literal ISD file for whatever
  comes after this demo.
- Confirm the lunar frame kernel defining `MOON_ME` loads correctly so SPICE can output that frame
  directly; sanity-check against the known GLD100/LOLA convention.
- Whether Lunaserv's native projection is directly usable by `sat_sim` or a reprojection step is
  actually required after all.
- **Open spike, not yet resolved**: whether a real WAC swath can be reprojected onto the DEM via a
  genuine ISIS/CSM camera model (`mapproject`) + `sat_sim`, as a principled alternative to `wac.py`'s
  manual framelet-stacking. The pipeline works end-to-end on real data, but hits a real, unresolved
  blocker (severe framelet-boundary striping in `mapproject`'s output, confirmed on two products,
  not an illumination/AOI artifact) — see `docs/history.md` Phase 12 and `docs/data-sources.md`'s
  "ISIS3/CSM spike" section before re-investigating or re-deriving any of this. Real (not just
  docs-only) spike code now exists on branch `spike/wac-isis-framestitch` — `src/trntest/isis_wac.py`
  and `notebooks/wac_isis_spike.py` step through EDR fetch → `lrowac2isis` → `spiceinit web=yes` →
  `lrowaccal` → `framestitch` with inline images at each step, chasing the working hypothesis that
  the striping is introduced at `framestitch` — scope currently stops there (no `isd_generate`/
  `mapproject`/`sat_sim` yet). Not merged to `main`: this is unproven and adds a heavy new
  toolchain (ISIS/ALE, via a `micromamba`-managed env in `docker/Dockerfile`, alongside ASP).

## Development history

See `docs/history.md` for the phase-by-phase narrative — what was tried, what broke, and how each
design decision (framelet timing, sensor axis convention, catalog-driven selection, the perf fixes
in the sweep) was actually reached. Background/curiosity reading, not required before making a
change; this file and `docs/data-sources.md` describe current behavior.
