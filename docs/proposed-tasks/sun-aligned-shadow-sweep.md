# A pure-Python shadow mask via a sun-aligned sweep

Prototyped in `notebooks/sun_aligned_shadow_sweep.py`. Preferred over
`docs/proposed-tasks/standalone-shadow-mask-tool.md`'s C++ port: no new repo, no new build/CI, and
it reuses this project's own already-validated 3D geometry code directly. **Status: geometry and
sweep direction confirmed correct; disagrees with ISIS `shadow` on shadow extent by a wide margin,
still unexplained. Checked against real WAC imagery on the one question this investigation actually
cared about (the row-1016 streak): the real sensor data shows no anomaly there, agreeing with this
method and contradicting ISIS `shadow`** — see "Ground truth check" below.

The tool's real output is an *illumination* fraction (`1` = fully lit, `0` = fully shadowed), meant
to multiply directly against a hillshade layer (`final = hillshade * illumination_fraction`) — not a
"shadow fraction," which reads backwards on screen and in that multiplication.

## The core idea

Build a Cartesian frame where the sun sits at infinity along +x. In that frame, every sun ray is
parallel to the x-axis, so occlusion along one raster row (fixed y) reduces to: sweep from the
sun-facing edge inward, track the running max height seen so far, and any point below that running
max is shadowed. That's a single `np.maximum.accumulate` per row — no per-pixel ray/DEM intersection
search at all, unlike ISIS `shadow` or ASP's `isInShadow`.

Frame construction, all confirmed correct:

- `x̂` = sun direction (unit vector, body-fixed).
- `ẑ` = the local "up" vector at a reference point, Gram-Schmidt-orthogonalized against `x̂` (project
  out the component along `x̂`, renormalize). Reuses `hapke._local_enu_basis`'s existing `up` output —
  no new math.
- `ŷ = ẑ × x̂` (confirmed right-handed: `x̂ × ŷ = x̂ × (ẑ × x̂) = ẑ`).

## The one real subtlety: this needs true 3D geometry, not a flat local plane

A tempting shortcut is to treat the DEM as a flat `(east, north, height)` sheet and do the whole
thing as a 2D image rotation by the sun's azimuth, folding the sun's elevation into a per-column
`height - x·tan(elevation)` correction. That shortcut is wrong at this DEM's scale: the Moon's
curvature (sagitta) over half this DEM's own width is

```
half_width = 121,200 m
sagitta = half_width² / (2 × 1,737,400 m) ≈ 4,227 m
```

against this candidate's own elevation range of ~8,328 m — the curvature term is the same order of
magnitude as the terrain relief itself, not a negligible higher-order correction. Use real 3D
Cartesian positions throughout, not a flat tangent-plane approximation.

This project already has exactly that, validated: `hapke._terrain_photometric_angles` converts every
DEM grid point to real body-fixed Cartesian coordinates via `rasterio.warp.transform` between
`geo_utils.local_orthographic_crs` and `geo_utils.moon_geocentric_crs` — the same PROJ/GDAL datum
machinery this project's DEMs already trust elsewhere (cross-checked against ISIS `campt` and ASP
`sfs`, per that function's own docstring). Reuse that transform directly for step 1 below; no new
sphere/ellipsoid math needed.

## Implementation plan

1. **Forward: scatter, don't warp.** Optionally upsample the original DEM 2-3x in its own grid first
   (a high-quality interpolator, e.g. spline via `scipy.ndimage.zoom`) — this is where the requested
   antialiasing resolution comes from, done before any rotation. Compute each point's real 3D
   position (`_terrain_photometric_angles`'s transform), then project onto `(x̂, ŷ, ẑ)` via a plain
   dot product — vectorized over the whole grid, no loops. Bin the resulting scattered `(X, Y, Z)`
   triples onto a new regular `(X, Y)` raster with `scipy.stats.binned_statistic_2d` (max), sized to
   the transformed DEM's own bounding box. **Bin size must be meaningfully larger than the upsampled
   grid's own spacing** (measured: ~2x is enough) — sizing it 1:1 seems natural but is a real
   correctness bug, not just a resolution choice: see "The bin-size aliasing artifact" below. Gaps
   where no source point lands are expected and fine — keep them as real nodata, not a false shadow
   signal.
2. **Sweep.** Per row (fixed `Y`), treat nodata as `-inf` for the running max (a gap can't occlude
   anything, and isn't itself a valid point to classify) so it never fabricates a false shadow;
   `np.maximum.accumulate` along `X` from the sun-facing edge inward, in `X`-descending order (the
   direction light actually travels). A point is shadowed iff its `Z` is less than the max
   accumulated *before* it. Same edge caveat as every other shadow tool tried so far: a point right at
   the sun-facing edge of the DEM is always marked lit, since nothing beyond the DEM's own extent is
   modeled — not new to this method.
3. **Backward, without a second resample.** `binned_statistic_2d` returns each input point's bin
   index — reuse it directly to gather the computed value back onto every original (upsampled) grid
   point, no separate inverse-warp needed. Then block-average down to the DEM's native resolution to
   get a fractional 0-1 *illumination* value (`1` = fully lit, `0` = fully shadowed — not a "shadow
   fraction"; this reads more naturally on screen and is the direct multiplier for a hillshade layer,
   `final = hillshade * illumination_fraction`), encoded as `uint8` (`0` → `0`, `1.0` → `255`).

## Performance

Plain NumPy/SciPy, no new framework needed. A `~2400×2400` DEM (this project's own typical size) at
3x upsampling is `~52M` cells; `np.maximum.accumulate` and `binned_statistic_2d` are both compiled,
vectorized, single-pass operations over arrays this size — comfortably sub-second. No case here for
Numba/GPU/C++.

## Risk specific to this method

Two resampling passes (upsample, then bin-and-gather) are exactly the kind of processing that could
introduce its own artifact — which matters here because that's the original question this whole
investigation is chasing. Before trusting any streak found (or not found) with this tool: check it at
strict 1:1 pixel scale the way the original streak investigation did, and confirm any pattern's
spacing/orientation doesn't track the chosen upsampling factor (2x vs 3x) — if it does, that's this
method's own artifact, not a DEM property.

## The bin-size aliasing artifact

Sizing bins to exactly match the upsampled grid's own spacing (the natural-seeming default) produces
a textbook rotated-lattice Moiré pattern, not gentle antialiasing noise. Measured: 77.7% of occupied
bins held *exactly one* source point at 1:1 sizing — so the "max" statistic per bin was really just
"whichever single point happened to land there," and the resulting illumination map showed an
obvious periodic checkerboard in bin occupancy, visible on screen as a "screen door" grid pattern in
every region of partial illumination (spotted directly by inspecting the output image, not found by
an automated check). This is a real bug, not the intended antialiasing behavior: the original design
called for ray-tracing at high resolution, aggregating at the bin, *then* downsampling to the final
resolution — 1:1 bin sizing skips the aggregation step entirely, so the final downsample was
averaging near-independent per-point noise, not a genuine antialiased signal.

Two tempting fixes turn out to be wrong or backwards:

- **Raising `UPSAMPLE_FACTOR` further doesn't help.** The aliasing ratio is scale-invariant when bin
  size is tied 1:1 to source spacing — confirmed empirically (occupancy statistics were identical at
  `UPSAMPLE_FACTOR=2` and `4`) before this fix was found. It also costs real memory for no benefit.
- **Blurring the final output would only hide the symptom.** It doesn't restore genuine aggregation,
  and would soften real shadow-boundary detail along with the artifact.

The actual fix: set the bin size to `BIN_SIZE_SAFETY_FACTOR` (2.0) times the upsampled grid's
spacing, decoupling it from a 1:1 tie to source resolution. This raises mean points-per-occupied-bin
from 1.22 to 4.10 and the count-exactly-1 fraction from 77.7% to ~0%, confirmed visually to remove
the screen-door pattern entirely (checked on the same crop, before/after) rather than merely soften
it. As a bonus, larger bins mean a *smaller* raster, so this is cheaper than the 1:1 version, not
more expensive.

## What this would and wouldn't prove

Same physical test as ISIS `shadow` and ASP's `isInShadow` (horizon self-occlusion via ray-marching)
reformulated for vectorized execution, not a different algorithm. It's a genuinely independent
implementation (built from scratch here, sharing no code with either), so agreement or disagreement
with ISIS `shadow` is still a real cross-check — but three tools agreeing wouldn't mean three
independent physical models agreed, just three implementations of the same underlying test.

## Self-shadow is a diagnostic, not part of the tool's real output

The sweep only tests occlusion by *other* terrain — it says nothing about a facet whose own local
slope faces away from the Sun (incidence >= 90 deg). That's fine for the actual use
(`hillshade * illumination_fraction`): any per-facet reflectance model already renders such a facet
black on its own, so marking it again in the shadow mask is redundant — multiplying by 0 or by 1
both land on the same already-black result. It only matters for comparing against ISIS `shadow`,
whose own LRS output conflates both cases into one mask — computed via
`hapke.real_geometry_photometric_angles`'s incidence angle (already validated against ISIS `campt`
and ASP `sfs`) and combined only for that comparison, kept separate from the tool's real output.

## Prototype results

Built and run against `M1327218454CE`'s DEM, `UPSAMPLE_FACTOR=2`, `BIN_SIZE_SAFETY_FACTOR=2.0`,
`binned_statistic_2d` with `statistic="max"`. Frame construction verified numerically (orthonormal,
right-handed). Numbers below are post-fix (see "The bin-size aliasing artifact"); the fix moved the
overall illuminated fraction up a few points (0.898 -> 0.914 including self-shadow) but changed
neither the row-1016 nor the ground-truth conclusions below.

- **The "lots of nodata" coverage (~42-50%) is exactly what was expected, but for a different reason
  than assumed going in.** First suspected an aliasing/lattice-alignment bug (coverage was identical
  at `UPSAMPLE_FACTOR=2` and `4`, and larger bins didn't raise it either) — checked with
  `scipy.ndimage.distance_transform_edt` before treating it as a bug: empty cells sit up to ~1200
  bins from the nearest real sample, ruling out a fine-grained gap a nearest-neighbor fill could
  safely patch. It's simply that this DEM's square footprint, rotated to align with the Sun
  direction, becomes a diamond inscribed in its own *larger* axis-aligned bounding box — confirmed
  against the closed-form area ratio for a rotated square, `1 / (|cos θ| + |sin θ|)²`, which is
  exactly 0.5 at 45°. The empty cells are the box's own corners, genuinely outside the DEM. This
  doesn't affect correctness: every real DEM point necessarily lands in its own populated bin, so no
  real point's classification is ever lost to a corner gap.
- **Direction/geometry confirmed correct, decisively, not just plausibly.** Cropped the identical
  region from both this method's mask and ISIS `shadow`'s own mask, over the same hillshade: the
  shadow patches land in the same places in both. No mirror/rotation bug.
- **A real discrepancy: this method marks noticeably less shadow than ISIS `shadow` does.** Overall
  illuminated fraction: ISIS 82.1% vs. this method (incl. self-shadow, for a fair comparison) 91.4%.
  Same crop: ISIS 83.5% vs. this method 94.4%. Neither a higher upsample factor (2x vs. 4x: no
  change) nor switching the bin statistic from mean to max moved this meaningfully, and fixing the
  bin-size aliasing artifact widened the gap slightly rather than closing it — ruling out bin
  resolution, bin-height aggregation, and that artifact as the cause. The patches are visibly smaller
  versions of the correct shape, not scattered noise. **This is not evidence the sweep is wrong**:
  ISIS `shadow` isn't ground truth either (its own precision stepping and `SUNEDGE`/`SOLARRADIUS`
  defaults are approximations too) — it's an unexplained disagreement between two implementations,
  not a known error in one of them.
- **Row 1016/1023 vs. ISIS alone is inconclusive.** Neither shows a distinct streak relative to its
  neighbors in this method (row 1016: 0.202 vs. neighbors' 0.202; row 1023: 0.213 vs. 0.213) — but
  given the general disagreement with ISIS in magnitude, a negative result here isn't strong evidence
  either way against ISIS alone; it may just lack the sensitivity to catch a marginal effect. Settled
  by real ground truth instead — see below.

## Ground truth check: real WAC imagery

ISIS `shadow` isn't ground truth (see above); real calibrated WAC imagery is the closest thing this
project has to it. Reprojected the real WAC crop (`entry.crop_result` -> `isis_wac.run_cam2map_for_crop`
-> `rasterio.warp.reproject` onto this notebook's exact DEM grid) and checked directly.

- **Row 1016, checked against what the sensor actually saw: no shadow feature there.** Real WAC
  brightness at row 1016 is 0.0153, identical to its immediate neighbors (also 0.0153) — visually
  confirmed too, ordinary terrain with no dark streak crossing it. This matches this method's own
  "no signal" finding and contradicts ISIS `shadow`'s claim of a streak there. `isis-shadow-masking.md`'s
  own step 2 ("compare against real WAC imagery") had never been done until now — this is that check,
  and it comes out against ISIS `shadow`, not against this method.
- **Where this method is confident, it tracks real brightness better than ISIS does.** Real WAC
  brightness where this method says confidently lit (`illumination_fraction > 0.9`): 0.0174. Where it
  says confidently shadowed (`< 0.1`): 0.0023 — a ~7.6x contrast. ISIS `shadow`'s own lit/shadowed
  split: 0.0184 vs. 0.0047 — only ~3.9x. This method's shadow calls, where it makes them, correlate
  more strongly with real observed darkness than ISIS `shadow`'s do (ISIS marks roughly twice as many
  pixels shadowed, diluting its own shadow-side mean with less-clearly-dark pixels).

This doesn't resolve the earlier magnitude discrepancy (this method still marks less area as shadow
overall than ISIS does) — but on the one question this whole investigation was chasing, real data
now sides with this method's "no anomaly at row 1016" over ISIS `shadow`'s own claim.

## Why this (and ISIS) likely undercount real shadow: DEM smoothing

The likely explanation for the magnitude gap against *true reality* (not the ISIS-vs-sweep gap,
which is separate — see below): GLD100's 100 m posting photogrammetrically smooths away real
terrain roughness (boulders, small ridges, crater-wall texture) below that scale. At this
candidate's shallow 13.3 deg sun elevation, that matters far more than it would at a higher sun
angle — shadow length is `height / tan(elevation)`, a ~4.2x multiplier here, so a real 5 m
obstruction invisible to the DEM would cast a ~21 m shadow, and a 20 m one, ~84 m, comparable to a
whole pixel. This is independently supported by this notebook's own "grazing sensitivity" finding
above: large regions sit within ~1 m of the exact lit/shadow threshold, meaning shadow determination
here is genuinely sensitive to sub-meter height information — exactly what a 100 m-posted DEM
cannot carry. `docs/data-sources/astropedia-gld100.md` already carries a version of this caveat from
an earlier, unrelated investigation ("worth a look before trusting this DEM for anything
sub-few-meter-precision at low sun angles"); this gives it a concrete, quantified case.

**Important nuance**: DEM smoothing explains why *any* tool working from this DEM — this method,
ISIS `shadow`, or a from-scratch ray-tracer — would undercount shadow relative to the real Moon. It
does *not* explain the separate ISIS-vs-sweep gap measured above, since both use the identical
GLD100 DEM and are equally blind to whatever it smooths away. That gap is still unresolved.

**Deliberately not fixing this with a tuning knob** (e.g. modeling the Sun as lower/larger than its
true geometry to inflate predicted shadow toward some target total). That would curve-fit a global
aggregate with no physical basis, and the real deficit almost certainly isn't spatially uniform —
rugged crater walls have more sub-resolution roughness to lose than smooth mare, so a global fudge
would get individual regions wrong even while the total looked better. Better to keep an honest,
DEM-limited result that's incomplete for a legible reason than a tuned one that's wrong for an
invisible reason. If finer terrain data is ever worth pursuing instead:
`docs/proposed-tasks/open-items.md` already points at NASA's VIRA project for LOLA-derived polar
DEMs down to 5 m/px, flagged there for a different reason (GLD100's own ±79 deg coverage limit) —
unverified whether that coverage actually extends down to this candidate's 73.5 deg N latitude.

## Next steps

- If still chasing the magnitude discrepancy vs. ISIS `shadow`: the cubic-spline-vs-linear upsample
  comparison done before the bin-size fix was found (no meaningful difference) should be re-checked
  now that the dominant source of "grey"/partial-illumination pixels turned out to be the aliasing
  artifact, not spline behavior — that earlier null result may not mean what it looked like it meant.
  Also worth checking whether ISIS `shadow`'s finer ray-marching precision resolves genuinely more
  grazing occlusion than a discrete bin comparison ever could, regardless of tuning.
- Re-validate against a second low-sun-elevation candidate — everything here, including the WAC
  ground-truth check and the bin-size fix, is one candidate (`M1327218454CE`).
