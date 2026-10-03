# Map seams

A statement of principles for handling seams: anywhere a mosaicked product (DEM, reflectance,
imagery) joins data from different source tiles or products, or crosses a coordinate system's own
discontinuity. Not tied to any one data source; source-specific facts live in `docs/data-sources/`,
and open seam defects in [`proposed-tasks/open-items.md`](proposed-tasks/open-items.md).

## Principles

1. **Find the root cause before choosing a fix.** Every seam artifact is different, and two that
   look alike can have unrelated causes: an edge row in the archive, two sides in subtly different
   map projections, a read window misplaced by our own code. A correct fix often depends on knowing
   which. Use best effort to find out why an artifact exists, including the product's labels and
   documentation and the raw source pixels, before reaching for a generic correction.

2. **Tell coordinate seams from data seams.** A coordinate seam is our code's fault: a branch cut, a
   window or transform computed in the wrong frame. A data seam is in the source itself: an edge
   row, a product assembled from separately made parts. Check the raw file to tell them apart.

3. **Compute read windows in a CRS centered on the area of interest,** never in a source CRS whose
   branch cut might fall inside it (`transform_bounds` across a cut silently returns a whole-world
   window clipped at whatever it happened to sample). A source whose own raster edges meet inside the
   area is read as one window per edge.

4. **Read whole-pixel windows only.** Floor the near edges and ceil the far ones. GDAL reads a
   fractional window as the nearest whole pixels while `window_transform` keeps the fraction, which
   misregisters the data by up to half a pixel.

5. **Warp each source onto the destination grid separately, then merge with an explicit precedence
   rule.** Don't concatenate source arrays into one: adjacent tiles, or a global raster's own two
   edges, can overlap or fall short by a fraction of a pixel.

6. **Decide coverage from the destination grid's own boundary**, not from a degree-space box padded
   independently. Degree boxes also break down around the poles.

7. **Where sources overlap and we choose where the seam goes, keep it a margin away from both
   sources' edges**, where artifacts concentrate, and use the overlap around it for a stronger
   agreement check: compare the two sources directly there, rather than extrapolating each side
   toward a seam the way an abutting pair forces.

8. **Where sources of different resolution meet, downsample by averaging, not bilinear**, so both
   sides keep comparable texture.

9. **Report and mitigate what can't be fully corrected.**
   - *Mitigate*: render something plausible rather than something arbitrary. Reject data known to
     be bad near a tile edge and fill the gap from what's around it, rather than leaving black,
     white or nodata pixels in the product.
   - *Report*: tell the user, through as many of these as fit. The minimum is the seam probe
     notebooks and their heavy tests (principle 10). Generating a product that crosses a seam with a
     known uncorrectable defect can also log a caution. For products where it's worth the cost, an
     opt-in mask of invalid or filled pixels, off by default (as `config.crop_map_write_fill_mask`
     does for the crop's map projection, alongside its always-on `FILLED_PERCENT` tag).
   - Start from a hard cut, and add a mitigation only where the probes measure a need for it: each
     one brings its own edge cases.

10. **Test seams with probes, and keep the inventory.** `trntest.seam_probes` renders a synthetic
    area of interest at every point where a source's tiles meet, and measures each seam against
    control lines inside one tile; start from the corners where several seams meet, which is where
    interactions show up. Each source's probe notebook (`notebooks/*_seams.py`) is its seam
    inventory, and its heavy test (`tests/test_*_seams.py`) marks known defects as strict expected
    failures, so a fix has to remove the marker. The inventory runs two passes, without and with
    mitigations, so it records which mitigation is enabled at which seam and why.

11. **Don't let hole filling hide a seam.** Measure coverage before any fill, and treat a gap at a
    seam as a bug until shown otherwise.
