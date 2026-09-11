# Cast-shadow modeling via ISIS `shadow`

A feasibility assessment and plan, not yet implemented. Investigates using ISIS's `shadow`
application to add real terrain-occlusion shadowing to `hillshade`, a gap the current shading
pipeline explicitly leaves open, and specifically what it would take to model penumbra (soft
shadow edges) rather than a hard lit/shadowed boundary. **Verdict: feasible for hard shadows**;
penumbra is real physics but likely sub-pixel at this project's current resolution for most
geometries — see below.

## The gap this addresses

`hapke.shade_ortho`'s own comment is explicit about the limitation:

> This is still just local per-facet shading, not cast-shadow occlusion from other terrain, which
> remains out of scope.

Both shading paths `despeckle_and_shade_ortho` can select (`hapke_shade_ortho`'s Hapke BRDF,
`shade_ortho`'s Lambertian fallback) compute per-pixel reflectance from each facet's own local
surface normal and the sun direction. Neither traces a ray to the sun to check whether *other*
terrain blocks it first. A crater floor whose own facet geometrically faces the sun still renders
lit even if a crater wall between it and the sun would occlude it in reality. The goal is a more
realistic synthetic render overall, not a labeled mask — whether a given dark pixel is dark because
its own facet faces away from the sun or because something else blocks it doesn't matter; both
should just darken the render the same way.

## What ISIS `shadow` actually does

Confirmed from the [ISIS `shadow` docs](https://isis.astrogeology.usgs.gov/9.0.0/Application/presentation/Tabbed/shadow/shadow.html)
and its `shadow.xml` parameter list:

- Input: a DEM cube (radius-as-DN, prepared with `demprep`). Output: a same-grid cube of
  hillshade-with-shadow values. Per-pixel, either the ordinary hillshade value (lit) or ISIS's LRS
  special-pixel value (shadowed or facing away from the sun) — a hard binary result, not a
  continuous illumination fraction.
- Sun position comes from either `SUNPOSITIONSOURCE=MATCH` (another cube's own camera geometry) or
  `SUNPOSITIONSOURCE=TIME` (an explicit `YYYY-MM-DDTHH:MM:SS.SSS`, needing local PCK/SPK kernels for
  the target body). Real ray-traced occlusion against the DEM, not just azimuth/elevation like the
  older `shade` app.
- Cost/accuracy tradeoffs are explicit and tunable: `PRESET` (`NOSHADOW`/`BALANCED`/`ACCURATE`/
  `CUSTOM`), `PRECISION` (DEM pixels stepped per ray-intersection check, default 1.0),
  `SHADOWMAP`/`LIGHTCURTAIN` caching. The docs note caching introduces ~1-2 pixel shadow-boundary
  inaccuracy — a real, acknowledged limitation, not a bug to chase out.
- `demprep`'s own docs say it requires "a Planetocentric file in Simple Cylindrical projection" for
  its pole-padding step, but also that it "can be run on files with non-Equatorial projections" —
  just skipping padding and only attaching the `ShapeModelStatistics` table blob `shadow` needs.
  **Not yet confirmed**: whether that blob-only path actually runs cleanly on a small local-AOI cube
  in this project's Orthographic projection (see Open questions).

## Penumbra: what `shadow` offers, and whether it's worth more

`shadow` has two sun-geometry-related parameters that sound penumbra-adjacent but aren't:

- **`SUNEDGE`** (boolean, default `TRUE`): "Draw light ray to the highest point of the sun on the
  horizon, instead of to the center." The app's own algorithm description: "If SUNEDGE, adjust the
  sun's position to nearer the highest (on the horizon) edge of the sun."
- **`SOLARRADIUS`** (default `1.001211`, in solar radii): scales the sun's effective radius used in
  that single edge-offset calculation. "A larger number has the end effect of lessening shadows; a
  smaller number increases them."

Both just move **where the single hard boundary falls** — biasing the one traced ray toward the
sun's limb (the physically correct place to test for the true umbra edge, since a point is only
fully shadowed once the sun's *entire* disk is blocked) rather than its center. There's no disk
sampling and no fractional output: every pixel still resolves to fully lit or LRS. ISIS `shadow`
does not model penumbra in the sense of a soft, continuously-graded shadow edge — confirmed by
direct inspection of its docs (no mention of "penumbra"/"partial"/multi-ray sampling anywhere in
the application page).

**Real penumbra width, to judge whether it would even be visible here**: the Moon's solar angular
diameter is ~0.53° (~9.3 mrad), same order as Earth's (Earth-Moon distance from the Sun is
essentially the same as Earth's own). For an occluding edge a distance `D` from the shadowed point
along the sun's ray, the penumbra transition width is approximately:

```
w ≈ D × 0.0093   (full umbra-to-lit transition, meters, D in meters)
```

At `hillshade`'s ~100 m/px resolution (`docs/resolution-investigation.md`), the transition only
spans a full pixel once `D` exceeds roughly **10.75 km** — i.e. the occluding terrain would need to
be over 10 km from the point it's shadowing, along the sun's ray, for the softness to be visible at
all at this resolution. That's a real distance for typical crater-wall/floor geometry at moderate
sun elevation, but plausible for the **long, grazing shadows this project already flags as a risk
case** — `report.problem_flags`' existing low-sun-elevation ("deep shadow risk") check is exactly
the geometry where shadow-casting distances get long enough for this to matter. So penumbra
modeling isn't wasted effort, but it's a refinement worth targeting specifically at low-sun-angle
candidates, not a blanket win everywhere — worth computing `D` for this project's actual candidate
geometries (crater rim height vs. floor distance, at each candidate's real sun elevation) before
building it, to confirm it's not sub-pixel for every real candidate in the dataset too.

**How to actually build it, since `shadow` itself won't**: real soft shadows need multiple samples
across the sun's disk per pixel, not one ray. Two ways to get there from what's already available
here:

1. **Multiple `shadow` runs, one per disk sample point, averaged.** `SUNPOSITIONSOURCE=MATCH` reads
   sun position from a cube's own label rather than computing it — plausibly game-able by writing
   several synthetic match cubes, each labeled with a different point sampled across the sun's real
   angular disk (e.g. a stratified/Fibonacci disk pattern, 8-16 samples), running `shadow` once per
   sample, and averaging the resulting binary maps into a continuous visibility fraction. Reuses
   `shadow`'s presumably well-tested ray/DEM-intersection engine (including its caching), at the
   cost of N full ISIS subprocess calls per render instead of one, and depends on `MATCH`'s label
   format actually being hand-editable this way — not yet confirmed.
2. **A custom ray-march directly against the DEM array, in Python.** This project already has the
   DEM in memory as a plain array wherever `hapke_shade_ortho`/`shade_ortho` run, and already
   computes per-facet gradients/normals for Hapke shading — a horizon-based occlusion check (march
   along the sun azimuth from each pixel, comparing DEM height against line-of-sight height at a
   handful of elevation offsets spanning ±the sun's angular radius) could be built without any new
   ISIS dependency, with full control over the sampling pattern. Likely more practical to prototype
   and iterate on than option 1, but reimplements (probably more crudely) what `shadow`'s C++
   ray-intersection code already does for the hard-shadow case.

Given the sub-pixel finding above, the pragmatic path is: implement the plain hard-shadow case
first (single `shadow` call, as in the sequencing below), and only build either disk-sampling
extension if a direct check against this project's real candidate geometries shows penumbra width
actually clears a pixel for cases that matter.

## Where it fits in this pipeline

`dem_ortho.fetch_dem_and_ortho` already builds exactly the inputs `shadow` needs, on exactly the
grid it needs them on:

- The hole-filled DEM (`dem.dem`) is already reprojected onto the camera-centered local
  Orthographic CRS, the same grid `hapke_shade_ortho`/`shade_ortho` shade on — before `sat_sim` ever
  projects anything into camera space. `shadow` operates in this same map-grid domain, not camera
  space, so no additional reprojection step is needed to align it.
- The real sun geometry `hapke_shade_ortho` already derives from `camera`'s real acquisition
  ephemeris time (`_moon_me_direction_from_local_enu` et al.) is the same geometry `shadow`'s
  `TIME` mode would need — this project isn't deriving a second, independent sun position, just
  handing the existing one to a second tool.
- The natural insertion point is `hapke.despeckle_and_shade_ortho`, right after its existing shading
  step, before it writes `ortho_shaded_path`: darken the (already Hapke/Lambertian-shaded) ortho
  wherever `shadow` reports LRS, before that file is written. Because that one shaded ortho is what
  both `sat_sim` (the `hillshade` generator) and every display/overlay panel read, this gets the
  shadowing into `hillshade` for free, without a separate pipeline branch — and correctly leaves
  `crop` (real WAC pixels) and `reproject` (no relighting step at all, per
  `docs/generators/reproject.md`) untouched, matching the scope this task is about.

## Open questions (to verify before implementing)

- **`demprep` on a local-AOI Orthographic cube**: confirm it runs cleanly (even if it skips pole
  padding) rather than hard-failing outside Simple Cylindrical, as its docs' wording is ambiguous
  on this point.
- **Elevation → radius conversion**: Astropedia GLD100 (this project's live default DEM) stores
  elevation in meters directly, not planetocentric radius (`docs/data-sources/astropedia-gld100.md`).
  `shadow`/`demprep` need radius-as-DN. Converting is a plain `elevation_m + config.MOON_RADIUS_M`
  add — already the same constant this codebase uses elsewhere (`dem_gld100.py`,
  `crater_depth.py`) — but needs to happen before the DEM reaches ISIS.
  `demprep`'s docs also say it "will now cause this program to fail" on negative radius values,
  which shouldn't occur here but is worth a direct check.
- **Getting the DEM GeoTIFF into an ISIS cube at all**: needs some `gdal`/ISIS conversion step —
  `docs/external-tools.md` confirms GDAL's ISIS3 driver reads `.cub` natively but doesn't establish
  whether writing one from a GeoTIFF (rather than starting from an ISIS-native product) is
  supported cleanly the same way; may need `gdal_translate -of ISIS3` or an ISIS `demprep`-adjacent
  import app instead. Not yet checked directly.
- **Kernel reuse for `SUNPOSITIONSOURCE=TIME`**: `shadow`'s `TIME` mode needs local PCK/SPK for the
  target body — a real new requirement, since this project's existing ISIS integration
  (`isis_wac.py`'s `spiceinit web=yes`) deliberately avoids bulk local kernel downloads (see
  `docs/external-tools.md`'s "`$ISISDATA` size" section). But `spice_kernels.py`'s
  `ALWAYS_KERNELS` already fetches and caches `pck00010.tpc`, `moon_pa_de421_1900_2050.bpc`, and
  `de421.bsp` for this project's own Python-side (`spiceypy`) sun-geometry computation — the same
  NAIF binary kernel formats ISIS itself reads. Likely reusable directly via `shadow`'s `PCK=`/`SPK=`
  parameters with no new fetch, but not yet confirmed against a real ISIS run.
- **`PRESET`/`PRECISION` choice**: start with `BALANCED` (the app's own default) rather than
  `ACCURATE`, matching this repo's general practice of starting conservative and tightening only if
  measured to matter — the ~1-2px cache-based shadow-boundary inaccuracy the docs note is likely
  fine for shading applied at this project's ortho resolution (~100 m/px), but not yet measured here.
- **Validation signal**: worth checking whether a real WAC crop with visible, unambiguous cast
  shadows (a deep crater near low sun elevation) is available among existing candidates to use as
  ground truth for "does the shadowing land where a real shadow actually is," rather than only
  checking that geometry runs without error.
- **Penumbra-relevant geometries**: compute the occluder-to-receiver distance `D` (see above) for
  this project's real low-sun-elevation candidates before deciding whether the disk-sampling
  extension is worth building at all.

## Recommended sequencing

1. Spike, outside the main pipeline (a scratch script or notebook cell, not `hapke.py` yet): convert
   one real candidate's hole-filled DEM (elevation → radius, GeoTIFF → ISIS cube), run `demprep`,
   then `shadow` with `SUNPOSITIONSOURCE=TIME` against the entry's own real acquisition ET, reusing
   this project's already-cached PCK/SPK. Confirms the open questions above against real data before
   any pipeline code is written.
2. Visually compare the resulting shadow layer against the same candidate's real WAC
   `crop`/`reproject` imagery, ideally one with visible real cast shadows — the closest thing to
   ground truth this project has, given `sat_sim` supplies no shadow reference of its own.
3. If it looks right, wire it into `despeckle_and_shade_ortho` as a new opt-in flag (mirroring the
   existing `hapke`/`along_track_correction` pattern) rather than an unconditional default, so
   `hillshade` output before/after is easy to A/B — the same posture `along_track_correction`/
   `real_hapke_params` were introduced with.
4. Separately, run the `D` calculation above against real low-sun-elevation candidates to decide
   whether penumbra modeling is worth pursuing at all before spending time on either disk-sampling
   approach.
5. Add a subprocess-call test following `run_spiceinit`'s own testing pattern (mock the ISIS calls,
   not a real `shadow`/`demprep` run in CI), plus a real-data check in
   `notebooks/hapke_hillshade.py` alongside its existing shading-fallback comparisons.
6. Document the new flag in `docs/generators/hillshade.md`'s Processing section and the new tool
   facts (conversion steps, kernel reuse, `PRESET` choice) in `docs/external-tools.md`, then fold
   this file's still-relevant open questions into whichever of those two is the better home, and
   delete this file.
