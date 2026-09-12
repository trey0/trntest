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

Step 1 of the sequencing below has now been spiked end-to-end against a real candidate
(`M1327218454CE`, 13.6° sun elevation — the lowest, and thus best cast-shadow-risk validation case,
in the current `notebooks/dataset_manifest.csv`) via `src/scratch/isis_shadow_spike.py` (disposable,
not committed). Findings below are updated in place; unresolved items stay flagged.

- **`demprep` on a local-AOI Orthographic cube**: **confirmed working.** Runs cleanly on a small
  2424x2437px local-Orthographic DEM cube with no error — silently skips pole padding (as its docs
  imply for non-Simple-Cylindrical input) and attaches the `ShapeModelStatistics` table blob
  (`MinimumRadius`/`MaximumRadius` fields) `shadow` needs. No special handling required.
- **Elevation → radius conversion**: **confirmed working**, plain `elevation_m + MOON_RADIUS_M`, no
  negative-radius failures on this candidate's real elevation range (DEM radius ~1,732,274 to
  ~1,740,602 m, comfortably positive).
- **Getting the DEM GeoTIFF into an ISIS cube at all**: **confirmed working, with one gap.**
  `gdal_translate -of ISIS3` produces a genuinely valid ISIS `Mapping` group directly from this
  project's local-Orthographic PROJ4 string — `TargetName=MOON`, `ProjectionName=Orthographic`,
  correct `CenterLongitude`/`CenterLatitude`/`EquatorialRadius`/`PolarRadius`, no manual label
  construction needed for those fields. But it omits `PixelResolution`/`UpperLeftCornerX`/
  `UpperLeftCornerY` — `demprep` fails outright without them (`**ERROR** PVL Keyword
  [PixelResolution] does not exist in [Group = Mapping]`). Fix: three `editlab options=addkey
  grpname=Mapping` calls, computed from the same `bbox`/resolution `dem_ortho.fetch_dem` already
  returns (no re-derivation) — `PixelResolution = (maxx-minx)/width`, `UpperLeftCornerX = minx`,
  `UpperLeftCornerY = maxy` (row 0 = north, matching this codebase's usual convention). Confirmed
  sufficient: `demprep` and `shadow` both then ran cleanly.
- **Kernel reuse for `SUNPOSITIONSOURCE=TIME`**: **confirmed working, with one gotcha.**
  `de421.bsp` (SPK) and `pck00010.tpc` (the plain **text** PCK) — both already in
  `spice_kernels.ALWAYS_KERNELS`, no new fetch — are sufficient; `time=` takes
  `spice.et2utc(camera.et, "ISOC", 3)` directly. The gotcha: passing `moon_pa_de421_1900_2050.bpc`
  (the **binary** PCK, also already cached) as `PCK=` instead crashes `shadow` outright
  (`SIGABRT`, `SPICE(FRAMEDATANOTFOUND) ... required to compute the orientation of the body-fixed
  frame IAU_MOON`) — that binary PCK only furnishes the `MOON_PA` frame, which needs the
  `moon_assoc_me.tf`/`moon_080317.tf` frame-kernel association this project's own Python/`spiceypy`
  side furnishes alongside it, but `shadow`'s single `PCK=` parameter has no slot for a second file.
  `shadow` wants plain `IAU_MOON`, which the text PCK provides directly — use that one, not the
  binary one, despite the binary one being the "more precise" kernel elsewhere in this codebase.
- **`PRESET`/`PRECISION` choice**: left at `shadow`'s own defaults (`PRESET=BALANCED` implicitly,
  `PRECISION=1.0`) for this spike — worked without tuning. No measured need yet to move off the
  default.
- **Validation signal**: `M1327218454CE` (13.6° sun elevation, the lowest in the current manifest)
  used as the spike candidate. A first visual check — the `shadow` LRS mask overlaid on a plain
  Lambertian hillshade of the *same* DEM (no WAC-ortho fetch needed for this quick check) — already
  shows shadow concentrated on the down-sun (away from the 224° sun azimuth) side of crater rims and
  walls, not scattered noise: geometrically sensible at a glance. `stats` reports 17.9% of valid
  pixels flagged LRS for this candidate, a plausible fraction at 13.3° sun elevation. **Still open**:
  the doc's own recommended step 2 (comparing against a real WAC crop with visible cast shadows, the
  closest thing to ground truth available) hasn't been done yet — this was only checked against this
  project's own synthetic Lambertian shading, which has no cast-shadow occlusion of its own to
  cross-check against.
- **New observation, not previously flagged**: the shadow mask shows a faint horizontal streaking
  pattern (thin, roughly one-line-wide bands at irregular, roughly-tens-of-rows intervals), described
  by the user as visually looking like "exactly horizontal lines at a constant spacing" in the
  original 3-panel figure. Investigated at length, through several wrong hypotheses corrected in turn
  (kept below since the elimination is itself informative, and because the final explanation is the
  one the user directly disputed the visual premise of — see "Still unresolved" below):
  1. Confirmed real, not a display artifact — the same lines survive a strict 1:1-pixel-scale render
     (`interpolation="none"`, no resampling), ruling out matplotlib `imshow` downsampling/moire.
  2. Confirmed not from `hole_fill_dem` — pre- and post-hole-fill elevation rasters are byte-identical
     for this candidate (zero actual holes in this AOI).
  3. First guessed as a GLD100 mosaic seam (adjacent-WAC-orbit-strip stitching artifact), since the
     same row-level anomaly is present in the raw Astropedia file's own native grid, before any
     reprojection. **Wrong** — the large row-to-row gradient at each checked anomalous row turned out
     confined to a narrow column band (1-3.3% of the row's ~11,580px width, checked at 4 rows), each
     with a smooth, large, real-looking elevation rise over a handful of pixels.
  4. Revised to "real crater wall/rim profiles, each anomalous row being one specific rim crossing."
     **User pushed back on this being terrain** based on the shadow-mask visual alone (a fair
     objection — the elimination above was against the *raw GLD100 render*, which hadn't actually been
     shown yet). Rendering the raw GLD100 as a hillshade at the candidate's own real grazing sun
     geometry (224.44/13.32 deg az/el, matching what `shadow` itself used) and inspecting a tall 1:1
     ticked strip does show a faint band around native row ~1045 distinct from the row~1131 crater
     wall — but a direct numerical scan of every column's row-to-row elevation diff across rows
     1040-1055 found **no discrete jump at any single row** (median diff ~7m, statistically
     indistinguishable from an arbitrary control range checked for comparison, rows 700-715, median
     ~16m — if anything the "line" row's diffs are smaller, not larger). So this specific band is not
     a discrete per-row data anomaly either.
  5. Step 4's "no discrete jump" finding was a real measurement but of the wrong row: it checked
     native-GLD100-grid row numbers against a line actually observed in the *reprojected*-grid strip,
     wrongly treating the two grids' row indices as interchangeable (they aren't in general — the
     earlier crater-wall example's rows only happened to be close). Redone correctly, on the
     reprojected grid (the same grid the shadow-mask figures actually show), with a direct same-grid
     side-by-side crop (plain Lambertian hillshade of the same DEM, same low-sun geometry, vs.
     `shadow`'s own LRS mask): a thin bright line **spanning the crop's full column width** is clearly
     visible in the independently-computed hillshade (`matplotlib`'s `LightSource`, no ISIS involved
     at all) at reprojected row ~962, with a corresponding line in the LRS mask nearby — unlike the
     earlier crater-wall rows, this is not confined to a narrow column band. A full-image-height scan
     for this same signature (row-mean hillshade residual against a broad rolling-median baseline)
     finds **49 such peaks across the image's 2437 rows** (prominence >= 1.5 std of the residual),
     spacing varying but centered around a median of ~41 rows (range 17-111, std ~25 -- consistent
     with the earlier finding of no single exact FFT period, but now clearly *not* random/scattered
     either: 49 peaks in 2437 rows is far more regular than craters-by-chance would produce). **This
     is a real, recurring, full-width, small-amplitude (~0.1-1% relative brightness) row-level
     phenomenon in the elevation data itself** — confirmed independent of `shadow`/ISIS entirely,
     since the same peaks show up in a bare `matplotlib` hillshade of the raw elevation array. At
     ~41-row median spacing and 100 m/px, that's roughly a 4.1 km recurrence scale — plausible for a
     photogrammetric block-adjustment artifact in GLD100's own production (stitching many individual
     stereo blocks/orbit segments), though this still hasn't been confirmed as the specific mechanism,
     nor cross-checked against a second real candidate.
  6. **Root cause pinned down**: independently downloaded NASA's original PDS-archived source tile
     directly (`WAC_GLD100_P900N0000_100M.IMG`, the north polar Polar Stereographic 100 m/px tile
     covering this candidate's 73.5 deg N AOI, from
     `https://pds.lroc.im-ldi.com/data/LRO-L-LROC-5-RDR-V1.0/LROLRC_2001/DATA/SDP/WAC_GLD100/`, 693.6
     MB, 18622x18622px, confirmed via its own embedded PDS3 label — not the flat file this project
     actually fetches, which is Astropedia's separate full-Moon mosaic GeoTIFF built from all 10 such
     original PDS tiles). Running the identical full-height row-peak search on this
     independently-sourced file's own AOI window: **55 peaks across 2965 rows, median spacing 45.5
     rows** — closely matching Astropedia's reprojected mosaic (49 peaks across 2437 rows, median 41
     rows). Since this PDS tile was fetched straight from NASA's archive and never touched by
     Astropedia's mosaicking/repackaging or this project's own reprojection, this rules out both as
     the source: the banding is inherited directly from the original DLR/Scholten photogrammetric DTM
     production (GLD100, "69,000 WAC stereo models" block-adjusted together per
     `WAC_GLD100_README.TXT`), not introduced anywhere downstream. Most likely mechanism: a residual
     seam/bias between adjacent individually-adjusted stereo models or orbit passes in that original
     production pipeline — a plausible, physically-scaled match (~4.1-4.5 km recurrence at 100 m/px)
     for a WAC stereo-model/swath boundary. **Status: root cause pinned to GLD100's own upstream
     production, confirmed by independent re-fetch from a second, unrelated NASA source** — worth
     flagging to whoever maintains `docs/data-sources/astropedia-gld100.md` if this DEM source is used
     for anything precision-sensitive; not blocking for this task's own step 1 (`shadow` correctly
     reflects whatever the real input DEM says, artifact or not).
- **Penumbra-relevant geometries**: compute the occluder-to-receiver distance `D` (see above) for
  this project's real low-sun-elevation candidates before deciding whether the disk-sampling
  extension is worth building at all. Not yet done.

## Recommended sequencing

1. ~~Spike, outside the main pipeline...~~ **Done** — `src/scratch/isis_shadow_spike.py` (disposable,
   not committed), against `M1327218454CE`. All steps ran successfully end-to-end; see "Open
   questions" above for what each step needed in practice (the `editlab` Mapping-group patch, the
   text-vs-binary PCK gotcha). A first geometric sanity check (shadow mask overlaid on a plain
   hillshade of the same DEM) already looks right — shadow concentrated on crater rims' down-sun
   side, not noise.
2. Visually compare the resulting shadow layer against the same candidate's real WAC
   `crop`/`reproject` imagery, ideally one with visible real cast shadows — the closest thing to
   ground truth this project has, given `sat_sim` supplies no shadow reference of its own. **Not yet
   done** — step 1's check above only compared against this project's own synthetic Lambertian
   shading, which has no cast-shadow occlusion of its own to cross-check against.
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
