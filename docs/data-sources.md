# Data sources reference

Index of the external *data* this project depends on — endpoints, formats, coverage, and known
gotchas, one file per source under `docs/data-sources/`. Consult before writing new code against any
of these; update the relevant file (not just code comments) when a concrete choice changes. For
external *tools/libraries* (ASP, ISIS, `usgscsm`, LightGlue) see `docs/external-tools.md` instead —
those are dependencies too, but they don't hold data of their own.

| Data type | Source | Example uses | Rationale |
|---|---|---|---|
| DEM (elevation) | [Astropedia GLD100](data-sources/astropedia-gld100.md) | `sat_sim` ray-intersection geometry; crater depth grading | Live default — real elevation directly, ±79° coverage. Lunaserv's DTM layer is deprecated: a real, unfixable crosshatch artifact and a coarser-than-advertised effective resolution. |
| Ortho/texture (visible reflectance) | [WAC_EMP PDS4](data-sources/wac-emp-pds4.md) | `sat_sim` ortho texture; Hapke relighting input | Live default — real physical reflectance (I/F) straight from its authoritative PDS4 archive. Lunaserv's WMS ortho layer carries an uncorrected affine display stretch that breaks ratio-based relighting. |
| WMS imagery/DEM (legacy) | [Lunaserv WMS](data-sources/lunaserv-wms.md) | ortho fallback (`ortho_source="lunaserv_wms"`); historical DEM path, kept for comparison | Superseded by the two rows above for the live default path; kept reachable for A/B comparison and as the still-current source for a few one-off diagnostics (noise characterization, NoData convention). |
| Vector craters | [Robbins crater database](data-sources/robbins-craters.md) | crater overlay rendering; depth/sharpness grading (see `docs/crater-grading.md`) | The only public global lunar crater database at this size/precision (~1.3M craters, D≥1km). |
| SPICE kernels (NAIF, deprecated path) | [LRO SPICE kernels (NAIF)](data-sources/spice-kernels-naif.md) | spacecraft trajectory/pointing for camera pose | The canonical, official kernel archive. Kept as a fallback WAC CK source (`wac_ck_source="naif_metakernel"`), confirmed numerically equivalent to the live default. |
| SPICE kernels (ISIS-resolved, live default) | [ISIS's own LRO kernel database](data-sources/spice-kernels-isis.md) | live-default WAC CK pointing source | Matches ISIS's own real-world kernel resolution by construction — more principled and immune to future NAIF/USGS drift than a hand-picked kernel-flavor list. |
| Raw camera imagery | [LROC WAC EDR/CDR products](data-sources/lroc-wac-edr-cdr.md) | real instrument imagery + framelet timing; real-WAC (`isis_wac.py`) pipeline input | The only real (non-synthetic) source of WAC imagery this project has, and the ground truth candidate images are validated against. |

## Related docs

- [`docs/external-tools.md`](external-tools.md) — external tool/library behavior (ASP, ISIS, `usgscsm`, LightGlue), as opposed to data.
- [`docs/crater-grading.md`](crater-grading.md) — how the Robbins database + a DEM are combined to grade crater sharpness.
- [`docs/image-pipeline.md`](image-pipeline.md) — how the synthetic camera is posed/sized against a real WAC crop.
- [`docs/dataset-selection.md`](dataset-selection.md) — orbit/candidate selection, including maneuver detection.
- [`docs/intermediate-product-discipline.md`](intermediate-product-discipline.md) — `TrnTestDataSet`'s on-disk layout.
