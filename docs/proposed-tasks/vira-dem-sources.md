# Plan sketch: switch the default DEM to VIRA's multi-source selection

Status: **not started** — a plan sketch, written ahead of the work. Once the work is done, fold the
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
| 0–60°N, 60°S–0 | `SLDEM2015_256_*_FLOAT` (6 tiles, 120° lon each: `000_120`, `120_240`, `240_360`) | imbrium.mit.edu | PDS3 `.IMG`, simple cylindrical, 30720×15360 each | 256 ppd (~118.45 m) | float32 **km**, height above 1737.4 km; `CENTER_LONGITUDE = 180` |
| 60–90°S | `LDEM_60S_60MPP_ADJ` | pgda.gsfc.nasa.gov | GeoTIFF | 60 m | not yet checked |
| 80–90°S | `LDEM_80S_20MPP_ADJ` | pgda.gsfc.nasa.gov | GeoTIFF | 20 m | not yet checked |
| 87–90°S | `ldem_87s_5mpp` | pgda.gsfc.nasa.gov | GeoTIFF | 5 m | not yet checked |

Download size: ~21 GB total (SLDEM ~1.9 GB × 6, north polar ~0.5 GB, south 3.1 + 2.7 + 3.5 GB per
`Content-Length`). Coverage is asymmetric: the north pole is 120 m only, the south is nested 60/20/5 m.

## Seam inventory

What a mosaic of these sources can go wrong at. The existing WAC_EMP work already hit versions of
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
3. **Longitude tile seams (0°/360°, 120°, 240°).** Between SLDEM tiles. 0°/360° is also the global
   wrap, since SLDEM (like GLD100) uses a `CENTER_LONGITUDE = 180` frame: its raster edge is the prime
   meridian, not the antimeridian.
4. **Coordinate branch cut (±180°, or wherever each CRS puts it).** Not a data seam, a math one:
   `transform_bounds`/`reproject` normalize longitude and can silently land a read window a full
   circumference away (exactly `ortho_wac_emp.py`'s bug). Occurs anywhere the source CRS's
   `lon_0 ± 180` falls inside a footprint.
5. **Poles.** A footprint containing a pole has a degree-space bbox of `lon ∈ [-180, 180]`, so
   `dem_gld100.astropedia_coverage_bbox_deg`-style degree-bbox logic breaks. Source selection has to
   work from the footprint polygon in each source's own CRS.

Possible latent baseline bug worth checking first: GLD100's raster edge is also at 0°/360° (CM 180),
and `reproject_astropedia_elevation_to_local_grid` builds its read window with `transform_bounds`
from a degree bbox. A footprint straddling the prime meridian may already misbehave today.
Milestone 1 step 1 tests this against current code before anything changes.

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

**Harness.** `trntest.seam_probes` now exists for the WAC_EMP reflectance mosaic
(`reflectance_seams.ipynb`, `tests/test_reflectance_seams.py`): synthetic square AOIs, per-seam
profiles against control lines, and shared pass limits. The DEM side needs a `Renderer` and its own
`Seam` list; the planned `dem_seams.ipynb` should reuse the rest. `dem_ortho.fetch_dem` only reads
`camera.footprint_lonlat_deg`, so a probe can pass a
stub footprint (square, nadir-sized, centered on the point) without SPICE or an EDR. Add an optional
rotation of the square, since seams that are axis-aligned in the destination grid hide some artifacts.
Output under this worktree's `output/<name>/seam_probes/`. Once a probe is also worth rendering,
look for a real LRO pass near it with `TrnTestEntrySpice` (LRO is polar, so every latitude gets
crossed; longitude is the constraint).

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

## Milestones

Ordered by value. Milestone 1 is the near-term goal; Milestone 2 may wait a long time.

### Milestone 1: SLDEM2015 for ±60°

SLDEM2015 replaces GLD100 wherever it has data. Its posting is similar (~118 vs. 100 m), but it is
reported to be substantially better quality. GLD100 stays in use for 60–79°, so nothing that works
today regresses, and >79° still fails as it does now. This creates a **transitional** SLDEM↔GLD100
seam at ±60°. Unlike the eventual SLDEM↔LDEM seam, the two overlap (GLD100 runs to 79°), so the step
between them can be measured directly and feathered if needed.

The four-corners probe still comes first: at (±60°, 0°) two SLDEM tiles and GLD100 meet across the
wrap.

0. **Reconnaissance, SLDEM only.** Download the 6 tiles (~11 GB) with the resumable `curl -C -`
   pattern from `cache.fetch_astropedia_gld100`, plus a lock (`docs/environment.md` notes that
   fetch's concurrency race). Check each tile with `gdalinfo` against its `.LBL`: units (km),
   scale/offset (GDAL exposes PDS3 `SCALING_FACTOR`/`OFFSET` as metadata but does not apply them on
   read), nodata, exact extent. Record in a new `docs/data-sources/sldem2015.md`.
1. **Baseline probes on GLD100** at #1–#3 and #6, against current code. This settles the
   prime-meridian question and gives numbers to compare against.
2. **Source abstraction.** Generalize `dem_gld100.py` so a DEM is built from per-source reads:
   coverage test in the source's own CRS, fetch/cache, read-and-reproject one source onto the local
   grid (the pattern `ortho_wac_emp._reproject_one_wac_emp_tile_to_array` already uses, including its
   branch-cut fix), conversion to meters of elevation. Add a `TrntestConfig.dem_source`
   (`"gld100"` stays available for comparison, as `lunaserv_wms` was). Per
   `docs/intermediate-product-discipline.md`, the DEM source goes into `dem_filled_filename` so the
   two can't collide on one name. Design it for more than two sources, since Milestone 2 adds them.
3. **Merge.** Per-pixel precedence (SLDEM where valid, else GLD100), then `hole_fill_dem`. Start with
   a hard cut. Add feathering or offset correction only if step 4 measures a step that needs it — the
   WAC_EMP work showed each correction brings its own new edge cases.
4. **Probe the mosaic**, #1 first, then #2, #3, #6, #7. Fix, re-probe.
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
- **~21 GB of extra cache is fine.**

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
