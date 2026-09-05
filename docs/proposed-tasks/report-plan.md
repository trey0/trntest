# Design: per-image Jupyter/HTML reports

**Status: report is now a fourth product type, wired into `TrnTestDataSet.populate()`/
`populate_via_workers()`.** `notebooks/report_template.py` + `TrnTestReport`
(`src/trntest/trn_products.py`) + `trntest.report.generate_report` produce one entry's report as
part of normal dataset population; `TrnTestDataSet.write_index()` writes a dataset-wide
`status.csv` and `reports/index.html` nav bar. Report content is a title (dataset name, entry index,
entry id), a one-line summary (orbit, center, sun elevation/azimuth), and `reproject`'s
overlay-toggle/full-resolution zoom blink against the basemap (`TrnTestReport._generate_impl`
self-ensures `reproject`). The rest of "Future work" below (nav bar, overview map, richer problem
flags) is still not started.

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
   }}` placeholders inline in the code (e.g. `load_entry("{{ dataset_folder }}", "{{ entry_index
   }}")`) — plain text substitution, not papermill's parameter-injection mechanism. Papermill's own
   `parameters`-tagged-cell mechanism can't produce this: it injects a *new* cell with the
   overridden values immediately *after* the tagged cell, so code inside the tagged cell itself
   never sees the override — only cells strictly after it do. That rules out a one-line `entry =
   trntest.report.load_entry("<path>", "<index>")` with literal values, the compact style wanted
   here, hence `{{ }}` substitution instead. The title heading is filled in the same way
   (`dataset_name`/`entry_index`/`product_id`, all cheap to compute from the manifest before the
   notebook ever runs — see `generate_report`'s own comment on why this stays plain substitution
   rather than a Python call in the template).

   Deliberately compact, per the user's request: the template carries no explanatory markdown
   beyond a one-line title — every cell is a single call into `src/trntest/report.py`
   (`load_entry`/`summary`/`reproject_overlay`/`reproject_zoom_blink`), named so the call itself
   reads as the documentation. Anything more than a trivial call lives in `report.py`, not the
   notebook, and the notebook needs exactly one import (`import trntest` — `report` is registered in
   `trntest/__init__.py` alongside `plotting`); the `import`+`load_entry` line is merged onto one
   line with the following `summary(entry)` call into a single cell, per the user's explicit request
   for the most compact form (needs `# noqa: E702, I001  # fmt: skip` to satisfy `trntest-lint`,
   since this repo's `ruff format`/`ruff check` otherwise disallow a semicolon-joined
   import-then-statement).

   Not paired/committed as a notebook: the template's `{{ }}` text isn't valid parameter defaults,
   so it can't be executed directly the way every other notebook here is
   (`scripts/run_notebook.sh`) — there's no `.ipynb` twin to keep in sync. `render_template(text,
   params)` (`src/trntest/report.py`) is a two-line regex substitution (`\{\{\s*(\w+)\s*\}\}`,
   raising `KeyError` on an unresolved placeholder) — reaches into markdown text too if a template
   ever needs that (this one does, for the title; `summary()`'s per-entry text goes through
   `IPython.display.Markdown(f"...")` instead, since it depends on `entry.camera` at run time, not a
   static value like `dataset_folder`/`entry_index`).
2. **Per-entry execution**: `trntest.report.generate_report(dataset_folder, entry_index,
   report_dir)` runs the substitution above (writes `<report_dir>/report.py`) → `jupytext --to
   notebook` (→ `<report_dir>/report.ipynb`) → `papermill` (executes that notebook in place) — all
   via `subprocess_utils.run_quiet`, in-process, no `docker compose run` wrapper (the caller already
   runs inside the container). `notebooks/report_template.py` itself is only ever read, never
   written to or executed. `TrnTestReport._generate_impl` (`src/trntest/trn_products.py`) is the
   normal caller — see "Report as a product type" below; `scripts/generate_report.sh <entry_index>
   [dataset_folder] [report_dir]` calls the same function for manual single-entry regeneration.
   `entry_index` is the entry's position in the dataset (`TrnTestEntry.index`), the report's primary
   (and only) lookup key — `TrnTestDataSet.__getitem__` also supports lookup by EDR product id, but
   `load_entry` doesn't expose that: one lookup mode is enough for this template's own use.
3. **HTML export**: `jupyter nbconvert --to html <report_dir>/report.ipynb
   --ExtractOutputPreprocessor.enabled=True --NbConvertApp.output_files_dir=images
   --TemplateExporter.exclude_input_prompt=True --TemplateExporter.exclude_output_prompt=True`
   right after. Code cells stay visible (no `--no-input`, since the user asked to see the code, not
   just its output) but the `In[N]:`/`Out[N]:` execution-count gutters are suppressed. Template
   cells display figures the normal way (`trntest.report.reproject_overlay(entry)`, a bare last
   expression, no `fig.savefig`/`plt.close`) — `ExtractOutputPreprocessor` (nbconvert's own built-in
   mechanism, already on by default for the markdown/RST/LaTeX/asciidoc exporters, just not HTML's)
   pulls each cell's image out of the executed notebook's embedded-base64 output and writes it to a
   file under `output_files_dir` (`images/`), rewriting the cell's `<img>` tag to that relative
   path — one CLI flag, no custom nbconvert template or preprocessor needed. (In practice this
   applies only to a plain-`Figure`-returning cell, not the two GIF-returning ones below — an
   `IPython.display.HTML` object's base64 payload isn't image output in nbconvert's sense, so it
   stays inlined in the HTML rather than extracted to `images/`.)

## Report as a product type

`TrnTestReport` (`src/trntest/trn_products.py`) is a `TrnTestProduct` alongside `TrnTestCropImage`/
`TrnTestHillshadeImage`/`TrnTestReprojectImage` — `entry.report`, `"report"` in `PRODUCT_TYPES`
(default-on, unlike opt-in `reproject`). This isn't a bolt-on: `task_state`/`truncate`/`status`
already treat any `images_by_type` entry generically, so plugging `report` in there gets its
task-queue lifecycle, failure tracking, and backfilling of already-populated entries for free —
including under `populate_via_workers()`, where it parallelizes across entries the same way
crop/hillshade already do. `TrnTestReport._generate_impl` self-ensures its one dependency
(`entry.reproject.generate()`, a no-op once done) rather than relying on callers passing
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
    images/                 # currently always empty -- every current template cell returns an
                             # IPython.display.HTML GIF, not a plain Figure, so ExtractOutputPreprocessor
                             # (see "Mechanism" above) never has anything to extract; kept as a real
                             # subfolder anyway since nbconvert creates it unconditionally, and a future
                             # Figure-returning cell would populate it (report_N_0.png naming, cell
                             # index-based, not chosen by report.py)
<dataset_folder>/status.csv   # TrnTestDataSet.write_index() -- one row per entry
```

`reports/` is a subfolder of the dataset folder itself, alongside `crop`/`hillshade`/`reproject`/
`_work` (moved here from an earlier prototype that kept `<output_dir>/reports/` as a sibling of
`<output_dir>/trn_dataset/` — nesting it under the dataset folder is the more consistent layout now
that report generation is dataset-native). `TrnTestDataSet.create()` creates it up front the same
way it does the other product-type subfolders.

## Viewing reports in JupyterLab

Browse to `<dataset_folder>/reports/index.html` in JupyterLab's file browser (`<dataset_folder>` is
under `output/`, e.g. `output/trn_dataset/reports/index.html`) and click through from there. Double-
clicking `index.html` opens it fine (JupyterLab's built-in HTML viewer fetches it via the contents
API), but clicking one of its links to a specific entry's `report.html` used to 403 ("Blocking
request from unknown origin") — Jupyter Server's Referer-based anti-CSRF check on `/files/...` GETs,
which can't use its usual "token-authenticated requests skip this" bypass since this server runs with
no token/password, and the built-in HTML viewer renders content in a sandboxed `srcdoc` iframe that
sends no Referer on an outgoing link click. Fixed by `docker/Dockerfile`'s
`--ServerApp.allow_origin='*'` (added specifically for this) rather than a client-side workaround, so
every notebook/report page's normal in-browser links Just Work regardless of how the page was opened.

## Current report content

Per the user's explicit ask to keep the first pass simple so the mechanism itself could be iterated
on quickly, the very first version of the template rendered one image (the hillshade render) and a
couple of manifest fields, nothing else. Grown twice since:

- Added `report.reproject_overlay(entry)` (`entry.reproject.plot_overlay(margin_frac=0.15)` — half
  `plot_overlay`'s own 0.3 default, so more of the report's fixed page width goes to the overlay
  itself than basemap padding) and `report.reproject_zoom_blink(entry)`
  (`entry.reproject.plot_zoom_blink_over()`), the `reproject` equivalents of `image_generation.py`'s
  Phase 6B/6C — deliberately `reproject`, not `crop`, and deliberately just these two, not the full
  Phase 5-8 sweep at once, to keep the page light until real batch runs show what's actually worth
  checking. `plot_overlay`/`plot_overlay_toggle` already exposed `margin_frac` as a display-only
  parameter, no `_render_overlay_figure` axis-limit surgery needed.
- Per further user feedback: dropped the plain hillshade-render cell entirely (the two `reproject`
  blink plots above are more informative on their own, and this also removed `TrnTestReport`'s only
  reason to self-ensure `hillshade` as a dependency — it now self-ensures `reproject` alone).
  `summary()` now reports sun azimuth alongside elevation, both computed fresh via
  `illumination.sun_azimuth_elevation_deg` at `entry.camera`'s own footprint center/epoch (the same
  call the real render's own relighting uses) rather than the manifest's `sun_elevation_deg` column,
  which uses a different method and has no azimuth counterpart. The title heading dropped to `###`
  (saves vertical space) and reads `{dataset_name} -- entry {entry_index}: {product_id}` — informative
  enough to identify the entry standalone, and matches the entry-identifier scheme "Future work"
  below once asked for (`entry_index` is now `load_entry`'s primary, and only, lookup key). The
  template's first two cells (`import`+`load_entry`, then `summary`) are merged into one, with the
  first two statements joined on one line, per explicit request for a maximally compact top cell.

No tie points, craters, or `crop` product in the report; those, plus the rest of "Future work"
below, remain open.

## Future work (not started)

The site described below is the target shape; only the flat `write_index()` table and the single
per-entry detail report (both described above) are actually built so far.

- **Page inventory**: four pages total — a nav bar, an overview map, an overview table, and one
  detailed report per entry (the existing report from "Current report content" above).
  The overview table is close to what `write_index_html` already produces (see "Report as a
  product type" above); splitting the nav bar into its own page, described next, is the main change
  needed there.
- **Nav bar**: a persistent top frame (`<frameset>`/iframe, not per-page embedded navigation) so
  switching between the overview map, overview table, or any entry's detail report never re-renders
  the nav bar itself. It needs: links that open the overview map or overview table in the content
  frame, a compact way to jump straight to any entry's report, and prev/next buttons that step
  through entries one at a time for systematic review. Screen space is tight in a top strip, which
  is why the entry identifier below matters here specifically.
- **Entry identifiers — add positional index alongside EDR id**: done for the per-entry report
  (`TrnTestEntry.index`, `report.load_entry`/`generate_report`'s primary lookup key, shown in the
  report's own title alongside the EDR product id). Still to use it as the standard short reference
  in the nav bar once that's built (space-constrained UI, where the position is more compact than
  the EDR product id) — the EDR product id remains fine wherever there's room (e.g. the overview
  table).
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
- **Richer problem flags**: crater-sharpness grading (`crater_depth.py`), a real tie-point pixel
  residual (not computed anywhere today — `tie_points.py` only produces ground-truth pixel
  *locations* for overlay plotting, no image-based comparison), and the footprint-geometry check
  described above, once each has a cheap-enough path (a persisted value or a lightweight query, not
  a fresh SPICE/camera rebuild or GLD100 fetch per entry per `write_index()` call).
