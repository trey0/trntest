# Design: per-image Jupyter/HTML reports

**Status: report is now a fourth product type, wired into `TrnTestDataSet.populate()`/
`populate_via_workers()`.** `notebooks/report_template.py` + `TrnTestReport`
(`src/trntest/trn_products.py`) + `trntest.report.generate_report` produce one entry's report as
part of normal dataset population; `TrnTestDataSet.write_index()` writes a dataset-wide
`status.csv` and `reports/index.html` nav bar. Growing the report's own content (beyond the
hillshade render + a couple of manifest fields) is the remaining explicit follow-up.

## Context

`notebooks/image_generation.py` is the flagship demo notebook: one long, hand-curated, multi-phase
walkthrough of a *single* manifest entry (`../../README.md`'s `TrnTestEntry`), meant to be read
top-to-bottom in JupyterLab or on GitHub. There's no lightweight way to look at *many* entries side
by side, or to generate a shareable, standalone artifact for one entry without dragging in the
whole demo notebook's narrative.

The goal here is different: a small, templated report — one HTML page per entry — cheap enough to
regenerate for every entry in a dataset, viewable outside JupyterLab (a browser, no running
kernel), with images written to disk as files rather than embedded as base64 (avoids bloating the
HTML and lets the index page reference the same files directly).

## Mechanism

Reuses tooling this repo already depends on (`jupytext`, `papermill`, `nbconvert` — all already in
`pyproject.toml`/the Docker image), plus one small piece of custom code (`trntest.report.
render_template`) for the one job none of those three tools does:

1. **Template**: `notebooks/report_template.py`, a `.py:percent`-format text notebook with `{{ name
   }}` placeholders inline in the code (e.g. `load_entry("{{ dataset_folder }}", "{{ edr_product
   }}")`) — plain text substitution, not papermill's parameter-injection mechanism. Papermill's own
   `parameters`-tagged-cell mechanism can't produce this: it injects a *new* cell with the
   overridden values immediately *after* the tagged cell, so code inside the tagged cell itself
   never sees the override — only cells strictly after it do. That rules out a one-line `entry =
   trntest.report.load_entry("<path>", "<id>")` with literal values, the compact style wanted here,
   hence `{{ }}` substitution instead.

   Deliberately compact, per the user's request: the template carries no explanatory markdown
   beyond a one-line title — every cell is a single call into `src/trntest/report.py`
   (`load_entry`/`summary`/`hillshade`), named so the call itself reads as the documentation.
   Anything more than a trivial call lives in `report.py`, not the notebook, and the notebook needs
   exactly one import (`import trntest` — `report` is registered in `trntest/__init__.py` alongside
   `plotting`).

   Not paired/committed as a notebook: the template's `{{ }}` text isn't valid parameter defaults,
   so it can't be executed directly the way every other notebook here is
   (`scripts/run_notebook.sh`) — there's no `.ipynb` twin to keep in sync. `render_template(text,
   params)` (`src/trntest/report.py`) is a two-line regex substitution (`\{\{\s*(\w+)\s*\}\}`,
   raising `KeyError` on an unresolved placeholder) — reaches into markdown text too if a template
   ever needs that (this one doesn't; `summary()`'s per-entry text goes through
   `IPython.display.Markdown(f"...")` instead, since it's computed from `entry` at run time, not a
   static value like `dataset_folder`/`edr_product`).
2. **Per-entry execution**: `trntest.report.generate_report(dataset_folder, edr_product,
   report_dir)` runs the substitution above (writes `<report_dir>/report.py`) → `jupytext --to
   notebook` (→ `<report_dir>/report.ipynb`) → `papermill` (executes that notebook in place) — all
   via `subprocess_utils.run_quiet`, in-process, no `docker compose run` wrapper (the caller already
   runs inside the container). `notebooks/report_template.py` itself is only ever read, never
   written to or executed. `TrnTestReport._generate_impl` (`src/trntest/trn_products.py`) is the
   normal caller — see "Report as a product type" below; `scripts/generate_report.sh <edr_product>
   [dataset_folder] [report_dir]` calls the same function for manual single-entry regeneration.
3. **HTML export**: `jupyter nbconvert --to html <report_dir>/report.ipynb
   --ExtractOutputPreprocessor.enabled=True --NbConvertApp.output_files_dir=images
   --TemplateExporter.exclude_input_prompt=True --TemplateExporter.exclude_output_prompt=True`
   right after. Code cells stay visible (no `--no-input`, since the user asked to see the code, not
   just its output) but the `In[N]:`/`Out[N]:` execution-count gutters are suppressed. Template
   cells display figures the normal way (`trntest.report.hillshade(entry)`, no return value
   captured, no `fig.savefig`/`plt.close`) — `ExtractOutputPreprocessor` (nbconvert's own built-in
   mechanism, already on by default for the markdown/RST/LaTeX/asciidoc exporters, just not HTML's)
   pulls each cell's image out of the executed notebook's embedded-base64 output and writes it to a
   file under `output_files_dir` (`images/`), rewriting the cell's `<img>` tag to that relative
   path — one CLI flag, no custom nbconvert template or preprocessor needed.

## Report as a product type

`TrnTestReport` (`src/trntest/trn_products.py`) is a `TrnTestProduct` alongside `TrnTestCropImage`/
`TrnTestHillshadeImage`/`TrnTestReprojectImage` — `entry.report`, `"report"` in `PRODUCT_TYPES`
(default-on, unlike opt-in `reproject`). This isn't a bolt-on: `task_state`/`truncate`/`status`
already treat any `images_by_type` entry generically, so plugging `report` in there gets its
task-queue lifecycle, failure tracking, and backfilling of already-populated entries for free —
including under `populate_via_workers()`, where it parallelizes across entries the same way
crop/hillshade already do. `TrnTestReport._generate_impl` self-ensures its one dependency
(`entry.hillshade.generate()`, a no-op once done) rather than relying on callers passing
`product_types` in a particular order, the same pattern `TrnTestReprojectImage` already uses for
its own dependency on `entry.crop_result`.

The nav bar/status table are a different shape — one file summarizing every entry, not one entry's
own artifact — so they're a separate step, `TrnTestDataSet.write_index()`, run once (not per
worker) after `populate()`/`populate_via_workers()`'s task-queue loop, controlled by their own
`write_index: bool = True` parameter. It writes `<dataset_folder>/status.csv` (`status()` plus a
`problems` column from `trntest.report.problem_flags`) and `<dataset_folder>/reports/index.html`
(a plain-HTML table linking to each entry's own report where it exists — no styling/JS, fine if a
link is momentarily broken because that entry's report doesn't exist yet).

`problem_flags(entry)` is deliberately narrow for this pass: cheap, zero-fetch heuristics on
`entry.row` only (currently just low sun elevation — deep-shadow risk). Its threshold
(`LOW_SUN_ELEVATION_DEG_THRESHOLD` in `report.py`) is a first guess, not a validated cutoff — tune
it once a real batch run shows what's actually worth flagging. A footprint-geometry outlier check
was considered and dropped for now: it would need `entry.camera` (a real SPICE/camera rebuild, not
persisted anywhere cheap to re-read), which would make `write_index()` re-do that work for every
entry on every call rather than staying cheap/pure-Python — worth adding once there's a cheap place
to read footprint size from instead of rebuilding the camera.

## On-disk layout

```
<dataset_folder>/reports/
  index.html              # TrnTestDataSet.write_index() -- nav bar + status/problems table
  <edr_product>/
    report.py             # notebooks/report_template.py with {{ }} substituted -- kept for provenance
    report.ipynb           # jupytext-synced + papermill-executed -- also kept for debugging
    report.html             # nbconvert's HTML export -- the deliverable
    images/
      report_N_0.png          # nbconvert's own ExtractOutputPreprocessor naming (cell index-based),
                               # not chosen by report.py -- see "Mechanism" above
<dataset_folder>/status.csv   # TrnTestDataSet.write_index() -- one row per entry
```

`reports/` is a subfolder of the dataset folder itself, alongside `crop`/`hillshade`/`reproject`/
`_work` (moved here from an earlier prototype that kept `<output_dir>/reports/` as a sibling of
`<output_dir>/trn_dataset/` — nesting it under the dataset folder is the more consistent layout now
that report generation is dataset-native). `TrnTestDataSet.create()` creates it up front the same
way it does the other product-type subfolders.

## First-pass report content (deliberately minimal)

Per the user's explicit ask to keep this pass simple so the mechanism itself could be iterated on
quickly: the template renders **one image (the hillshade render, via the existing generic
`plotting.plot_raster`) and a couple of manifest fields** — orbit number, center lat/lon — nothing
else. No overlay/basemap comparison, no tie points, no craters, no `crop`/`reproject` products. All
of those are already-implemented pieces (`plotting.py`, `tie_points.py`, `craters.py`) that
`notebooks/image_generation.py`'s Phases 5-8 already exercise per-entry — extending the report to
reuse them is expected to be straightforward now that the pipeline is confirmed to work end to end
via real `populate()` runs against the current `trn_dataset`, not a design risk.

## Future work (not started)

The site described below is the target shape; only the flat `write_index()` table and the single
per-entry detail report (both described above) are actually built so far.

- **Page inventory**: four pages total — a nav bar, an overview map, an overview table, and one
  detailed report per entry (the existing minimal report from "First-pass report content" above).
  The overview table is close to what `write_index_html` already produces (see "Report as a
  product type" above); splitting the nav bar into its own page, described next, is the main change
  needed there.
- **Nav bar**: a persistent top frame (`<frameset>`/iframe, not per-page embedded navigation) so
  switching between the overview map, overview table, or any entry's detail report never re-renders
  the nav bar itself. It needs: links that open the overview map or overview table in the content
  frame, a compact way to jump straight to any entry's report, and prev/next buttons that step
  through entries one at a time for systematic review. Screen space is tight in a top strip, which
  is why the entry identifier below matters here specifically.
- **Entry identifiers — add positional index alongside EDR id**: `TrnTestDataSet.__getitem__`
  already accepts either a `product_id` string or an integer position (`self.images.iloc[key]`, see
  `trn_dataset.py`) since `images` is reset to a dense `0..n-1` index at construction — so the
  position is already a stable, ready-to-use short id, nothing new to build there. Use it as the
  standard short reference in space-constrained UI (the nav bar particularly); the EDR product id
  remains fine wherever there's room (e.g. the overview table).
- **Overview map**: a ground-track-style plot — one label per entry at its center lat/lon, with a
  vector overlay of each entry's hillshade/reproject FOV footprint (`camera.footprint_lonlat_deg`/
  `tie_points.crop_footprint_corners_for_camera` already compute these per entry) and its index
  number labeled next to the footprint. Background: a global ortho layer (Lunaserv's
  `luna_wac_global`, see `docs/data-sources/lunaserv-wms.md` — deprecated for per-camera fetches in
  favor of `WAC_EMP` on image-quality grounds, but that concern doesn't apply to a low-opacity
  overview backdrop) at ~20% opacity, layered over a day/night mask: white where sunlit, ~80% white
  (i.e. a light grey) where in shadow. The illumination reference time is fixed at the dataset's
  temporal midpoint — one global snapshot, not per-entry lighting; `illumination.sun_elevation_deg
  (ground_km, et)` already gives sun elevation for an arbitrary ground point/time and is the natural
  building block for a coarse lon/lat grid's day/night classification.
- **Dataset short name**: `write_index_html` already reads `self.folder.name` informally as the
  dataset's display name. That may already be sufficient — no strong need for a new stored field if
  a `TrnTestDataSet.name` property just returning `self.folder.name` signals "this is the standard
  identifier" clearly enough. Used in the `<title>` of every page above (nav bar, overview map,
  overview table, and each entry's detail report) either way.
- **Grow per-entry report content — next increment**: the current minimal report (hillshade + a
  couple of manifest fields) wasn't far off. Add the `reproject` equivalents of
  `image_generation.py`'s Phase 6B/6C — `entry.reproject.plot_overlay(...)` (basemap + overlay
  toggle) and `entry.reproject.plot_zoom_blink_over()` (full-resolution zoom blink) — deliberately
  `reproject`, not `crop`, and deliberately just these two, not the full Phase 5-8 sweep at once, to
  keep the page light until real batch runs show what's actually worth checking.

  Also shrink Phase 6B's overlay margin, roughly by half, so more of the report's fixed page width
  goes to the overlay itself. That margin isn't a `plot_overlay` display parameter today — it falls
  out of `config.dem_padding_fraction` (0.3, `dem_ortho.fetch_dem_and_ortho`'s AOI pad around the
  image footprint before fetching the basemap), and `plotting._render_overlay_figure` always shows
  the *entire* fetched basemap extent. Shrinking `dem_padding_fraction` itself isn't the right lever
  here — that basemap is `entry.dem_ortho_result`, shared with hillshade's own render/relighting
  input, so a smaller fetch AOI would affect more than the report's display. This needs a
  display-only crop instead: tighten `_render_overlay_figure`'s axis limits around the overlay
  footprint before rendering, without touching the underlying fetched raster or
  `dem_padding_fraction` — likely a new optional parameter threaded through `plot_overlay_toggle`/
  `TrnTestImage.plot_overlay`, used only by the report template's call.
- **Richer problem flags**: crater-sharpness grading (`crater_depth.py`), a real tie-point pixel
  residual (not computed anywhere today — `tie_points.py` only produces ground-truth pixel
  *locations* for overlay plotting, no image-based comparison), and the footprint-geometry check
  described above, once each has a cheap-enough path (a persisted value or a lightweight query, not
  a fresh SPICE/camera rebuild or GLD100 fetch per entry per `write_index()` call).
