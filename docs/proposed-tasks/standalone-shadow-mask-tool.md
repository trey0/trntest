# A standalone shadow-mask tool, ported from ASP's `isInShadow`

Investigation only — nothing implemented. Recommended as a separate repo `trntest` would depend on
(a prebuilt binary release, fetched the same way `docker/Dockerfile` already fetches ASP), not a new
module here. Superseded in practice by
`docs/proposed-tasks/sun-aligned-shadow-sweep.md` — a pure-Python alternative that needs no new repo
or build system — kept here as the C++ path's own reference in case that tradeoff is revisited.

Context: `docs/proposed-tasks/gld100-banding-artifact.md`'s row-1016 streak question is still open,
and `notebooks/asp_sfs_shadow_spike.py` found ASP's `sfs --model-shadows` itself unusable against
this project's data (a "no data for this DEM" false negative in every camera representation tried).
This doc investigates a narrower alternative: pull just `sfs`'s own shadow ray-tracer out and run it
directly against a DEM, skipping `sfs`'s image/camera/exposure machinery entirely.

## What `isInShadow` actually is

Not a published ASP API — an internal free function in ASP's own source
(`NeoGeographyToolkit/StereoPipeline`, `src/asp/SfS/SfsImageProc.{h,cc}`, Apache-2.0). ASP's binary
release ships no headers at all (`/opt/StereoPipeline` in this project's Docker image has the
compiled `libVw*.so` files but nothing to compile against them with), so "use the API" in practice
means porting the function's source, not linking against a distributed one — a ~60-line, legally
reusable copy, not a dependency in the usual sense.

Signature:

```cpp
bool isInShadow(int col, int row, vw::Vector3 const& sunPos,
                vw::ImageView<double> const& dem, double max_dem_height,
                double gridx, double gridy,
                vw::cartography::GeoReference const& geo);
```

Algorithm: convert the grid point `(col, row)` to a body-fixed Cartesian position via the DEM's own
`GeoReference`/datum, then march a ray toward `sunPos` in fixed steps (half a grid cell), converting
each step back to lon/lat/height and comparing against the DEM's own (bilinearly interpolated)
surface. Returns true the first time the ray dips under the surface; false if it clears the DEM's
max height or exits the grid first. `areInShadow` (same file) loops this over every pixel and returns
a mask — the function to actually port. Confirmed via `SfsModel.cc`: this is exactly what
`--model-shadows` calls, unmodified — not an approximation of it.

No caching or precomputed shadow volume — a plain per-pixel ray-march every time. Closer to ISIS
`shadow`'s `PRESET=ACCURATE` than its cached `BALANCED` default.

## Why this, not the rest of ASP

`isInShadow`/`areInShadow`'s only dependencies are Vision Workbench's Image, Cartography, and Core
modules — not ISIS, not USGSCSM, not ASP's camera/session abstraction, which is what actually broke
every attempt in `asp_sfs_shadow_spike.py` (the WAC Pushframe camera type rejected outright; the CSM
ISD path already known-broken for Pushframe; the synthetic TSAI/CSM camera hitting the unexplained
"no data" bug). None of that machinery is reachable from this function at all.

Inputs a standalone tool would need:

- **A DEM as `vw::ImageView<double>`, elevation above datum** — this project's own
  `dem_ortho.fetch_dem` GeoTIFF works directly. No ISIS-style radius conversion (`isis_shadow_spike.py`'s
  `elevation_m + MOON_RADIUS_M` dance) — `isInShadow` builds Cartesian positions from
  `(lon, lat, dem(col,row))` via the datum itself.
- **A `GeoReference`**, read straight from the GeoTIFF (VW's Cartography module wraps GDAL, the same
  library `rasterio` uses) — this project's local-Orthographic Moon CRS should read the same way it
  already does for every other GDAL-based tool in this pipeline (not yet verified empirically).
- **`gridx`/`gridy`** — the DEM's own pixel size in meters. Requires a projected (metric) DEM grid,
  not geographic lon/lat — matches this project's own Orthographic DEM convention already.
- **`sunPos`**, a plain body-fixed Cartesian position in meters — exactly the vector
  `illumination.sun_azimuth_elevation_deg`'s own `spice.spkpos("SUN", et, "MOON_ME", "NONE", "MOON")`
  call already computes. No SPICE, no camera model, no ephemeris time needed inside the tool itself —
  the Python side hands it a plain XYZ vector.

## Getting Vision Workbench

Available prebuilt on conda-forge (`visionworkbench`, 3.7.2, linux-64/macOS) with headers and
libraries as one matched package. Prefer this over pairing a separately-cloned VW header tree against
the `.so` files already bundled in ASP's own binary release — those ship with no headers, so there's
no version to match against without guessing. VW's own dependencies (per the conda-forge feedstock's
recipe): Boost, GDAL, OpenBLAS, OpenCV, libjpeg-turbo, libpng, libtiff — all standard, all already
packaged.

## Proposed CLI

Something like:

```
shadowmask --dem in.tif --sun-x <m> --sun-y <m> --sun-z <m> --out mask.tif
```

No camera model, no image, no ISIS/CSM/SPICE dependency in the tool itself — a DEM in, a mask out.

## Licensing

Apache-2.0, NASA Ames-authored. Freely portable with attribution — keep the original license header
on the ported file.

## How `trntest` would use it

Shell out via `trntest.subprocess_utils.run_quiet`, the same pattern already used for every ASP/ISIS
tool call in this codebase. `docker/Dockerfile` would fetch a prebuilt Linux binary release by
version tag, mirroring how it already fetches ASP itself — not build the new tool from source as
part of this repo's own image.

## Validation path

Run against the DEM already fetched for `M1327218454CE` (`dem_ortho.fetch_dem`'s output — the same
file `isis_shadow_spike.py` and `asp_sfs_shadow_spike.py` both used) and compare row 1016 and row
1023 directly against ISIS `shadow`'s own mask, closing the question both of those left open.

## Open questions / risks

- Whether VW's `GeoReference`/`Datum` reads this project's Moon-datum Orthographic GeoTIFF the same
  way GDAL/`rasterio` does — likely, unverified.
- This is the same horizon-ray-march *style* ISIS `shadow` uses (self-occlusion test from each grid
  point toward the Sun), not a fundamentally different algorithm — a genuinely separate
  implementation/codebase, but a bug shared by "any horizon-march approach" wouldn't be ruled out by
  agreement between the two.
- No caching shortcut here, so the row-1023-type caching artifact (`gld100-banding-artifact.md`) is
  expected not to reproduce — that would confirm the caching theory again, not add new evidence.
- Pin a specific VW release (3.7.2) rather than tracking `visionworkbench/visionworkbench`'s
  unreleased history.

## Effort

Small. The core function is ~60 lines and directly portable. Remaining work: a CMake build against
conda-forge's `visionworkbench`, DEM I/O plus CLI parsing (~100-200 lines), and a release workflow
producing the prebuilt Linux binary `trntest`'s own Docker build would fetch.
