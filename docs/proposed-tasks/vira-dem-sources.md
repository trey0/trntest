# Plan sketch: switch the default DEM to VIRA's multi-source selection

Status: **Milestone 1 steps 0-4 done** (SLDEM2015 cached and checked; `TrntestConfig.dem_source =
"sldem2015_gld100"` available with its ±60° seam treated, passing all 42 seam probes in
`notebooks/dem_seams.ipynb`; not yet the default); step 5 next. Once the work is done, fold the
lasting facts into `docs/data-sources/` (one file per new source) and `dem_ortho.py`'s docstrings,
and delete this file.

## Goal

Replace the single-source GLD100 default (`dem_gld100.py`, ±79° only) with the latitude-banded set
NASA's VIRA project downloads
([`scripts/download_dems.sh`](https://github.com/nasa/vira/blob/main/scripts/download_dems.sh)),
following its choices unless there's a concrete reason not to. The first and most valuable step is
SLDEM2015 for ±60° (Milestone 1); the polar caps come later. Together they also close
`open-items.md`'s ">±79° fails outright" gap, and may bear on its "implausibly steep pixels" item
(GLD100 is WAC-stereo-derived; SLDEM2015 is LOLA+Kaguya-TC).

## VIRA's selection

| Band | Product | Host | Format | Posting | Encoding (from `.LBL`) |
|---|---|---|---|---|---|
| 60–90°N | `LDEM_60N_120M` | imbrium.mit.edu (LOLA GDR) | PDS3 `.IMG`, polar stereo, 15520² | 120 m | Int16 × 0.5 m, height above 1737.4 km sphere; label says "preliminary LOLA data" |
| 0–60°N, 60°S–0 | `SLDEM2015_256_*_FLOAT` (6 tiles, 120° lon each: `000_120`, `120_240`, `240_360`) | imbrium.mit.edu | PDS3 `.IMG`, simple cylindrical, 30720×15360 each | 256 ppd (~118.45 m) | float32 **km**, height above 1737.4 km; `CENTER_LONGITUDE = 180`. **We use the 512 ppd tiles instead** (deviation below). |
| 60–90°S | `LDEM_60S_60MPP_ADJ` | pgda.gsfc.nasa.gov | GeoTIFF | 60 m | not yet checked |
| 80–90°S | `LDEM_80S_20MPP_ADJ` | pgda.gsfc.nasa.gov | GeoTIFF | 20 m | not yet checked |
| 87–90°S | `ldem_87s_5mpp` | pgda.gsfc.nasa.gov | GeoTIFF | 5 m | not yet checked |

Download size per `Content-Length`: VIRA's set is ~21 GB (SLDEM ~1.9 GB × 6, north polar ~0.5 GB,
south 3.1 + 2.7 + 3.5 GB); with SLDEM2015 at 512 ppd instead (~1.4 GB × 32), ~55 GB. Coverage is asymmetric: the north pole is 120 m only, the south is nested 60/20/5 m.

## Seam inventory

General principles are in `docs/map-seams.md`. What a mosaic of these sources can go wrong at. The existing WAC_EMP work already hit versions of
the first three (`wac_emp_edge_correction.py`, `ortho_wac_emp.py`'s branch-cut fix, and the
`trntest2` three-tile corner gap in `open-items.md`).

1. **Latitude band seam, abutting (±60°).** SLDEM ↔ LDEM polar. Different source data (LOLA+TC vs.
   LOLA-only, and "preliminary" LOLA in the north), different posting, different projection. Risks:
   vertical offset, horizontal misregistration, edge-row defects like WAC_EMP's, a texture change
   (different effective smoothness) even with zero offset. The polar squares circumscribe the 60°
   circle, so their corners reach ~48° latitude — whether those corners hold valid data (i.e. whether
   there is an overlap zone to blend across) is unknown.
2. **Nested resolution seams (80°S, 87°S).** Overlapping, not abutting. Precedence is "finest valid
   wins"; the seam is the edge of the finer product's valid data. At our ~100 m target posting the
   20 m/5 m products must be *downsampled* — bilinear would alias, so each side of the seam would get
   different high-frequency content. Use `average` resampling for downsampling. A finer working
   grid (see "DEM working resolution" below) would move where downsampling vs. upsampling happens.
3. **SLDEM tile seams: every 45° of longitude, and ±30°/0° latitude** (512 ppd tiles are 45° × 30°). 0°/360° is also the global
   wrap, since SLDEM (like GLD100) uses a `CENTER_LONGITUDE = 180` frame: its raster edge is the prime
   meridian, not the antimeridian.
4. **Coordinate branch cut (±180°, or wherever each CRS puts it).** Not a data seam, a math one:
   `transform_bounds`/`reproject` normalize longitude and can silently land a read window a full
   circumference away (exactly `ortho_wac_emp.py`'s bug). Occurs anywhere the source CRS's
   `lon_0 ± 180` falls inside a footprint.
5. **Poles.** A footprint containing a pole has a degree-space bbox of `lon ∈ [-180, 180]`, so
   `dem_gld100.check_astropedia_coverage`-style degree-bbox logic breaks. Source selection has to
   work from the footprint polygon in each source's own CRS.

GLD100's raster edge is at 0°/360° (CM 180) too, and a footprint straddling it did misbehave (a
`NaN` strip up to ~0.5° wide just west of 0°); step 1 found and fixed it, along with a half-pixel
misregistration in every GLD100 read. SLDEM2015's tiles share GLD100's CRS (eqc, `lon_0=180`,
`R=1737400`), so the same read pattern applies to them.

## Test-first: seam probes, worst case first

Build the probe harness **before** the new sources, and run it against GLD100 first as a baseline.

**Probe points**, in this order:

| # | Point | Seams exercised | Why |
|---|---|---|---|
| 1 | (60°N, 0°) and (60°S, 0°) | band seam + SLDEM tile seam + global wrap | The real "four corners": three sources meet across the wrap. Structurally the same as the `trntest2` WAC_EMP corner gap. |
| 2 | (60°N, 180°) and (60°S, 180°) | band seam + branch cut | Branch cut without a data seam, so it's separable from #1. |
| 3 | (±60°, 120°) and (±60°, 240°) | band seam + ordinary tile seam | Control for #1 without the wrap. |
| 4 | (80°S, any), (87°S, any) | nested resolution seams | Different failure family: overlap/precedence/resampling. |
| 5 | 90°N, 90°S | pole | Degree-bbox breakdown. |
| 6 | (±45°, 0°), (0°, 0°) | tile seam + wrap, no band seam | Isolates the wrap. |
| 7 | Sampled points along each seam, every ~15° of longitude | band seam | #1–#3 only see a few longitudes; a vertical offset between SLDEM and LDEM may vary along the seam. |

Starting at #1 is the right optimization: if both seam types are handled there, most simpler cases
follow, and any interaction between them (e.g. the branch-cut fix changing which tile wins merge
precedence) only shows up there. #7 is still needed.

**Harness.** `trntest.seam_probes` has a DEM source (`dem_gld100`: `DEM_SEAMS`, `DEM_PROBES`,
`render_gld100_elevation`, `dem_thresholds`), run by `notebooks/dem_seams.ipynb` and
`tests/test_dem_seams.py`. It probes every point where four 512-ppd SLDEM2015 tiles meet (three at
±60°): ±60°, ±30° and 0° latitude × every 45° of longitude, plus #6's ±45° 0° wrap-only points. The mosaic will need its own renderer and source entry, reusing
`DEM_SEAMS`/`DEM_PROBES` so its numbers compare directly against the GLD100 baseline. Not done yet:
an optional rotation of the probe square (seams that are axis-aligned in the destination grid hide
some artifacts), and probes #4, #5 and #7 from the table above. Once a probe is also worth rendering, look for a
real LRO pass near it with `TrnTestEntrySpice` (LRO is polar, so every latitude gets crossed;
longitude is the constraint).

**Metrics per probe DEM** (all computed on the final local-ortho grid, with the seam curve
rasterized into that grid, since seams aren't axis-aligned there):

- **Pre-fill nodata count and location**, before `hole_fill_dem`. Any gap at a seam is a coverage or
  selection bug until proven otherwise; don't let `dem_mosaic --hole-fill-length 50` hide it.
- **Seam-band gradient ratio**: median gradient magnitude in a few-pixel band along the seam curve
  vs. matched bands a few pixels either side. The WAC_EMP notebook's row-to-row jump metric,
  generalized to a curved seam.
- **Step estimate**: mean elevation difference across the seam from short profiles normal to it
  (or, where sources overlap, the direct difference of the two sources on the overlap).
- **Slope outliers**: pixels > 60° (the `open-items.md` steep-pixel test), on vs. off the seam.
- **Low-sun hillshade** with the sun perpendicular to the seam, for eyeballing. A step shows as a
  bright or dark line.
- **Independent cross-check**: mosaic the same sources with ASP `dem_mosaic` (or plain `gdalwarp`)
  and compare, as the WAC_EMP notebook did. Where GLD100 overlaps (±60–79°), difference against it.

Keep the probe list and thresholds in a `@pytest.mark.heavy` test so regressions are caught later.

On elevation, step and spike turned out to be terrain-dominated: control lines reach ~280 m and
~330 m at 100 m posting, so they carry no pass limit (`seam_probes.dem_thresholds`). The gradient
ratio (controls ≤ 1.31) and `NaN` are what separate a seam from terrain. Slope outliers, the
`dem_mosaic`/`gdalwarp` cross-check and a curvature-based metric aren't implemented; add them if
the mosaic's seams need finer discrimination than the gradient ratio gives.

## Milestones

Ordered by value. Milestone 1 is the near-term goal; Milestone 2 may wait a long time.

### Milestone 1: SLDEM2015 for ±60°

SLDEM2015 replaces GLD100 wherever it has data. At 512 ppd its posting (~59 m) is finer than GLD100's
100 m and than the ~100 m DEM grid, and it is reported to be substantially better quality. GLD100 stays in use for 60–79°, so nothing that works
today regresses, and >79° still fails as it does now. This creates a **transitional** SLDEM↔GLD100
seam at ±60°. Unlike the eventual SLDEM↔LDEM seam, the two overlap (GLD100 runs to 79°), so the step
between them can be measured directly and feathered if needed.

The four-corners probe still comes first: at (±60°, 0°) two SLDEM tiles and GLD100 meet across the
wrap.

0. **Reconnaissance, SLDEM only.** *Done.* The 32 tiles at 512 ppd plus the data-quality map, via
   `cache.fetch_sldem2015_tile` (resumable, locked); facts in `docs/data-sources/sldem2015.md`:
   exact 45°×30° tiles in GLD100's own CRS, km above 1737.4 km.
1. **Baseline probes on GLD100.** *Done* (`dem_seams.ipynb`). Found and fixed: the 0° wrap gap and
   a ≤0.5 px misregistration of every GLD100 read (`dem_gld100.reproject_astropedia_elevation_to_local_grid`).
   Found, not fixed: GLD100's own seams, a one-row line along all of ±60° plus nodata stretches,
   and one-column lines along 90° and 270° between them (17 of 42 probes fail, strict xfails in
   `tests/test_dem_seams.py`). Every other probed meridian, ±30° and the equator are clean on
   GLD100: the baseline the mosaic's tile seams have to match. Within ±60° SLDEM replaces GLD100's
   90°/270° seams outright.
2. **Source abstraction.** *Done.* `dem_sources`: a `DemSource` is a set of Equidistant Cylindrical
   tiles (GLD100 one, SLDEM2015 32) with its own fetch and meters conversion, read through
   `geo_utils.read_eqc_raster_to_local_grid_array` (GLD100's wrap-safe, whole-pixel reader,
   generalized), averaging where the source is finer than the grid. `DEM_SOURCES` maps
   `TrntestConfig.dem_source` to sources in precedence order; `"gld100"` (default, output
   bit-identical to before) and `"sldem2015_gld100"`. A non-default source is in the DEM and
   shaded-ortho filenames. The hard-cut precedence merge is already in (`mosaic_elevation`): every
   probe corner within ±60° comes out with no `NaN`, SLDEM 2-11 m above GLD100 on median, 15-33 m RMS
   apart, ~0.6 s per 200 km grid once the tiles are in the OS cache.
3. **Merge at ±60°.** *Done* (`dem_sources.LatSeam`). SLDEM's own edge rows at 60° are clean; the
   seam comes from GLD100's first ~2 local pixels poleward of 60° (its own seam, plus the 60°N
   nodata row) and a regional SLDEM−GLD100 offset of 4-15 m that stays constant up to the seam. The
   treatment discards a band of ~0.007° equatorward / 0.015° poleward of 60° (~7 px), fills it by
   inverse-distance weighting, and blends SLDEM into GLD100 over the 0.1° equatorward of 60°. The
   band width came from a sweep: 3 px left 60°S 135°E at 1.46; ~7 px is the narrowest under the
   limit (worst 1.28); wider bands smooth it below the surrounding texture. The hard cut stays
   available as `dem_source = "sldem2015_gld100_hardcut"`, the inventory's pass without mitigations.
4. **Probe the mosaic.** *Done* for #1-#3 and #6 at all 42 points: SLDEM's tile seams (every 45°,
   ±30°, the equator) are clean as a hard cut; at ±60° the hard cut fails all 16 probes, the treated
   mosaic none, with no `NaN` anywhere. Still to do: #7 (more longitudes along ±60°). Not yet looked
   at: whether the ~3 km feather or the texture change (SLDEM's far sharper detail meeting GLD100's
   blur, visible in a low-sun hillshade, untreatable at the seam) shows up in rendered images.
5. **Quality check: is SLDEM actually better here?** Same entries, both sources:
   - the steep-pixel count (`open-items.md`: 38 of 207 `trntest1` DEMs have pixels > 60°; start with
     `M1314424588CE`)
   - low-sun hillshade side by side, and each against the real WAC image
   - effective resolution of each (see "DEM working resolution" below)
6. **End-to-end.** Render a few entries near each seam type through all three generators; confirm
   `image_generation.ipynb`'s geometry checks don't regress. Check DEM↔ortho registration: GLD100 is
   WAC-derived and so co-registered with WAC_EMP by construction; SLDEM (LOLA-controlled) may not be,
   which would show up as shading offset from texture. `wac_resample.py` map-projects the real WAC
   crop onto the DEM's own pixel lattice, so a DEM grid change also changes that output grid —
   re-check it here. It terrain-corrects with ISIS's own `LRO_LOLA_LDEM` shape model (~237 m), not
   this DEM, so the real and synthetic images already use different terrain; note whether the
   switch narrows or widens that gap.
7. **Switch the default**, regenerate what needs it, update docs.

### Milestone 2: polar caps

Replace GLD100 at 60–79° and fill >79°, retiring GLD100 from the default path.

- **South first** (60/20/5 m nested products): reconnaissance, then the working-resolution question
  below, then the nested-seam probes (#4) and the pole (#5). The 5 m product is where a finer DEM
  grid can be tested at all.
- **North** (`LDEM_60N_120M`): VIRA's choice, used unless something concrete argues otherwise. Its
  label says "preliminary LOLA data"; whether that matters is an open question, not a known problem.
  If reconnaissance turns up a newer or finer north product, compare the two before choosing.
- Re-run the ±60° probes: the seam becomes SLDEM↔LDEM, which abuts rather than overlaps (unless the
  polar squares' corners below 60° hold valid data — check).

When both milestones are done, fold this plan away.

## Decisions (user, 2026-09-27)

- **Priority: SLDEM at low latitudes first** (Milestone 1). The polar caps are well down the list.
- **Deviations from VIRA are allowed** when there's a concrete reason. No specific doubt about the
  "preliminary" north polar data; its value is an open question.
- **Keep the fine south products.** A DEM finer than the output image may still improve it, and a
  DEM's effective resolution can be much coarser than its nominal posting. "DEM working
  resolution" below covers both.
- **~21 GB of extra cache is fine.** Later (2026-10-03): SLDEM2015 at 512 ppd (~45 GB) rather than
  VIRA's 256 ppd; the 256 ppd tiles were deleted once the 512 ones checked out.

## DEM working resolution vs. output GSD

Today `dem_target_gsd_m` (~100 m) sets both the DEM working grid and, in effect, the detail behind
the ~100 m output image. Decouple them and measure whether a finer DEM grid helps.

- **Effective resolution.** For each source, estimate how much real detail it carries: e.g. a
  radially averaged power spectrum on a flat-ish patch (where does it fall to the noise/interpolation
  floor?). SLDEM vs. GLD100 at ±60° is part of Milestone 1's quality check; later, compare
  SLDEM/LDEM downsampled against the 20 m/5 m products where they overlap. LOLA-only
  products are interpolated between orbit tracks, so expect effective resolution to vary with
  latitude (track spacing shrinks toward the poles).
- **Does a finer grid change the render?** Render the same entry with the DEM grid at ~100, 50 and
  20 m (inside the 80°S/87°S products, where the source supports it), output image fixed at ~100 m.
  Compare against each other and against the real WAC image. Expect the biggest effect at low sun:
  cast shadows (`cast_shadow.py`; see the GLD100 doc's low-sun caveat) and shading normals. Also
  watch `sat_sim`'s ray-intersection speckle, which depends on DEM precision.
- **Cost.** DEM pixel count grows with the square of 1/GSD. Time `cast_shadow`'s pure-Python sweep,
  `sat_sim` and `hole_fill_dem` at each grid size.
- **Seams.** A working grid finer than a source's posting means upsampling that source, which can
  make the texture change at a seam more visible, not less. Re-run the seam probes at the chosen
  working GSD.

Outcome: a per-source (or per-latitude) working GSD, or one global value, and the downsampling
method from the working grid to the output image.
