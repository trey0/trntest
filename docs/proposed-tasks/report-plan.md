# Design: per-image Jupyter/HTML reports

**Status: all four planned pages exist.** `notebooks/report_template.py` + `TrnTestReport`
(`src/trntest/trn_products.py`) + `trntest.report.generate_report` produce one entry's report as
part of normal dataset population (report is a fourth product type, wired into
`TrnTestDataSet.populate()`/`populate_via_workers()`); `TrnTestDataSet.write_index()` writes the
dataset-wide `status.csv`, `reports/overview_table.html`, `reports/index.html` (the nav bar --
persistent, over a content iframe), and `reports/overview_map.png` (all by default, the last one
skippable via `write_index(write_overview_map=False)` -- see `docs/batch-generation.md` for why that
matters at scale). Report content is a title (dataset name, entry index, entry id), a one-line
summary (orbit, center, sun elevation/azimuth), and `reproject`'s overlay-toggle/full-resolution zoom
blink against the basemap (`TrnTestReport._generate_impl` self-ensures `reproject`). **The nav bar
cannot be viewed through JupyterLab's own server at all** (a real, structural CSP limitation, not a
bug to fix — see "Nav bar" below) — view it via `scripts/serve_reports.sh` instead. Only "richer
problem flags" in "Future work" below is still not started.

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

The nav bar/status table/overview map are a different shape — one file summarizing every entry, not
one entry's own artifact — so they're a separate step, `TrnTestDataSet.write_index()`, run once (not
per worker) after `populate()`/`populate_via_workers()`'s task-queue loop, controlled by their own
`write_index: bool = True` parameter. It writes `<dataset_folder>/status.csv` (`status()` plus a
`problems` column from `trntest.report.problem_flags`), `<dataset_folder>/reports/overview_table.html`
(`report.write_overview_table_html` — a plain-HTML table linking to each entry's own report where it
exists — no styling/JS, fine if a link is momentarily broken because that entry's report doesn't
exist yet), `<dataset_folder>/reports/index.html` (`report.write_index_html` — the persistent nav bar
described in "Nav bar" below, defaulting its content iframe to the overview table), and
`<dataset_folder>/reports/overview_map.png` (`overview_map.write_overview_map`, its own
`write_overview_map: bool = True` parameter — see `docs/batch-generation.md` for why that one in
particular is worth turning off during incremental population at scale).

`problem_flags(entry)` is deliberately narrow for this pass: cheap, zero-fetch heuristics on
`entry.row` only (currently just low sun elevation — deep-shadow risk). Its threshold
(`LOW_SUN_ELEVATION_DEG_THRESHOLD` in `report.py`) is a first guess, not a validated cutoff — tune
it once a real batch run shows what's actually worth flagging. A footprint-geometry outlier check
was considered and dropped for now: it would need `entry.camera` (a real SPICE/camera rebuild, not
persisted anywhere cheap to re-read), which would make `problem_flags` itself re-do that work for
every entry on every call rather than staying cheap/pure-Python — worth adding once there's a cheap
place to read footprint size from instead of rebuilding the camera. (The overview map's own FOV
polygons already pay this same cost deliberately, for a different, presentation-only purpose — see
"Overview map" below — but `problem_flags` stays a separate, cheap, pure-manifest check.)

## On-disk layout

```
<dataset_folder>/reports/
  index.html              # TrnTestDataSet.write_index() -- persistent nav bar + content iframe
  overview_table.html     # TrnTestDataSet.write_index() -- one row per entry, status/problems
  overview_map.png        # TrnTestDataSet.write_index() -- overview_map.write_overview_map()
  map.html                # ditto -- thin <img> wrapper, the nav bar's actual "Map" link target
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

## Viewing reports

**The nav bar (`reports/index.html`) cannot be viewed through JupyterLab's own server at all** --
use `scripts/serve_reports.sh` instead (a plain `python3 -m http.server`, see "Nav bar" below for
why). Single-page views (an individual `report.html`, `overview_table.html`, `overview_map.png` on
their own, with no nav bar) still work fine through JupyterLab, via either mechanism below.

**Through JupyterLab** (single pages only, not the nav bar): browse to the file in JupyterLab's file
browser (`<dataset_folder>` is under `output/`, e.g. `output/trn_dataset/reports/overview_table.html`)
and open it. Double-clicking opens it fine (JupyterLab's built-in HTML viewer fetches it via the
contents API), and links to other single pages now work too (they didn't always -- clicking a link
used to 403, "Blocking request from unknown origin": Jupyter Server's Referer-based anti-CSRF check
on `/files/...` GETs, which can't use its usual "token-authenticated requests skip this" bypass since
this server runs with no token/password, and the built-in HTML viewer renders content in a sandboxed
`srcdoc` iframe that sends no Referer on an outgoing link click. Fixed by `docker/Dockerfile`'s
`--ServerApp.allow_origin='*'`, added specifically for this).

**Through `scripts/serve_reports.sh`** (needed for the nav bar, works for everything else too):
`scripts/serve_reports.sh [port] [dataset_folder]` runs a plain, CSP-free static file server over
one dataset's `reports/` folder, entirely separate from JupyterLab's own server on its own port.
Tunnel that port the same way as JupyterLab's (`ssh -L <port>:localhost:<port> <this-host>`) and open
`http://localhost:<port>/reports/index.html`.

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

## Future work

**Page inventory: all four planned pages now exist.** A nav bar (`reports/index.html`), an overview
map (`reports/overview_map.png`), an overview table (`reports/overview_table.html`), and one detailed
report per entry (`reports/<edr_product>/report.html`, "Current report content" above). Only "Richer
problem flags" below remains genuinely not-started.

- **Nav bar**: built, `report.write_index_html` (`src/trntest/report.py`) writes
  `reports/index.html` — a single document with a fixed nav `<div>` (CSS flexbox, not
  `position: absolute`/`fixed` — the first version used the latter and, missing a `<!DOCTYPE html>`,
  triggered quirks-mode `height: 100%` bugs that starved the content iframe down to showing only one
  table row at a time; flexbox plus an explicit doctype fixed it) over one content `<iframe>` (not a
  `<frameset>` split into separate nav/content pages as originally planned — see the CSP finding
  below for why that distinction turned out not to matter). Current layout, left to right: the
  dataset name (bold, prominent), `Map`/`Table` links (content-frame-targeted, no JS), Prev/Next
  buttons (adjacent to each other, not flanking anything, each disabled at its own end of the entry
  range), then a plain number `<input>` (not a `<select>` — a dropdown with one `<option>` per entry
  doesn't scale to a many-hundred-entry dataset the way a "type a number" box does) that doubles as
  the current-entry display, plus a `Go` button/Enter-key shortcut. Labeled "Entry index ... (max
  {last_index})", not "... of {n_entries}" — entries are 0-indexed throughout (matching
  `TrnTestEntry.index`/report titles/URLs), and stating the actual maximum valid value avoids the
  off-by-one reading a count invites ("indices 0..1 'of 2'" reads as inconsistent to anyone not
  already thinking in 0-based terms; "(max 1)" states the same fact without implying a count). The
  map link points at a new `overview_map.write_overview_map`-written
  `map.html` (a thin `<img>` wrapper), not `overview_map.png` directly — linking straight to the raw
  image made the browser treat it as a standalone image document, which (confirmed in Firefox) got
  shrunk to a thumbnail with an unreliable click-to-zoom.

  **Real, structural finding: this can never work through JupyterLab's own server, by design, no
  server config can fix it.** First guess (wrong, corrected after the user actually tried it and
  reported total failure — every link and button did nothing): that a single document sidesteps
  Jupyter's per-file CSP `sandbox` header (see "Viewing reports" above) since it only ever *writes*
  its own child iframe's `src`, never *reads* across the frame boundary, and a same-document DOM
  write should be unaffected by sandboxing. That reasoning addressed the wrong half of the problem.
  The real mechanism: `AuthenticatedFileHandler.content_security_policy`
  (`jupyter_server/base/handlers.py`) unconditionally appends `sandbox allow-scripts` (no
  `allow-same-origin`) to *every* file served via `/files/...`, deliberately, so served HTML can
  never impersonate the Jupyter server itself — confirmed by reading that handler's own source
  directly, not inferred. That gives `index.html` itself an opaque origin when loaded as a top-level
  page. Every file also carries `frame-ancestors 'self'` — and an opaque origin can never equal
  `'self'`, so **any attempt by a Jupyter-served page to embed another Jupyter-served page in an
  iframe or frame is blocked by the embedded page's own `frame-ancestors` check**, regardless of how
  the navigation was initiated (a plain `<a target="content">` click and a JS `.src =` write both hit
  it identically) and regardless of which document does the embedding (a `<frameset>` document would
  have hit the exact same wall, since it too would carry `sandbox` as a Jupyter-served file). Live
  reproduction: the user's own browser console showed
  `Content-Security-Policy: ... blocked ... (frame-ancestors) ... "frame-ancestors 'self'"` on the
  first real attempt to use it. No `allow_origin`-style server setting fixes this — the `sandbox`
  token is hardcoded in that one handler, appended after any `headers` config override, not
  conditional on anything.

  **Consequence — the nav bar cannot be viewed through JupyterLab at all.**
  `scripts/serve_reports.sh` (a plain `python3 -m http.server`, no CSP of any kind) serves the same
  files on their own port instead — the iframe design works completely normally there, confirmed via
  `curl` (no `Content-Security-Policy` header on any response). See "Viewing reports" above.

  **Fixed: the nav bar's "current entry" state now stays in sync however you got there.** Originally
  it lived only in `index.html`'s own in-memory JS, with no way to detect a navigation that happened
  *inside* the content iframe without going through the nav bar's own controls (e.g. clicking a row's
  link directly in the overview table) — the exact `postMessage`-based fix speculated here originally
  is what got built: `generate_report` (`src/trntest/report.py`) post-processes nbconvert's own
  `report.html` output (not a notebook cell — keeps `report_template.py`'s "every cell is a single
  call" convention untouched) to inject one `<script>` tag that calls
  `window.parent.postMessage({{source: "trntest-report", entryIndex: N}}, "*")` on load.
  `index.html`'s own `message` listener (`NAV_SYNC_MESSAGE_SOURCE`, shared between both ends) then
  calls the same `updateNavState` the nav bar's own Prev/Next/Go controls use, syncing the number
  box's value and Prev/Next's disabled state regardless of which page announced it or how it was
  reached. `postMessage` was the right tool specifically because it isn't blocked by the opaque-origin
  restriction above (unlike direct cross-frame property access, which would be).

  Prev/Next are now disabled at either end of the entry range (`updateNavState` sets
  `.disabled` from `i <= 0`/`i >= productIds.length - 1`); before this they had no such check.
- **Entry identifiers — add positional index alongside EDR id**: done. `TrnTestEntry.index`,
  `report.load_entry`/`generate_report`'s primary lookup key, shown in the per-entry report's own
  title alongside the EDR product id, the nav bar's jump-to-entry number box's value
  (space-constrained UI, where the position is more compact than the EDR product id), and the
  overview table's own product-id column (`write_overview_table_html`, `"{index}: {product_id}"`,
  both part of the same link) — since the nav bar's jump box takes an index, not a product id, the
  table needed some way to expose that lookup key too.
- **Overview map**: built, `src/trntest/overview_map.py`
  (`plot_overview_map`/`write_overview_map`, registered in `trntest/__init__.py` alongside
  `plotting`/`report`) — called by `TrnTestDataSet.write_index()` by default (see
  `docs/batch-generation.md` for the real per-call cost this adds and how to skip it during
  incremental population), linked from the nav bar's "Overview map" link (see "Nav bar" above).
  Background: `luna_wac_global` (Lunaserv, see
  `docs/data-sources/lunaserv-wms.md` — deprecated for per-camera fetches in favor of `WAC_EMP` on
  image-quality grounds, but that concern doesn't apply to this map's own display), plain geographic
  lon/lat (`config.lunaserv_dem_srs`, reused as-is despite the DEM-flavored name — same fixed CRS),
  fetched once via the existing `cache.fetch_lunaserv_getmap` and cached like any other tile, at 40%
  opacity (raised from an initial 20%, which read too faint in practice). Day/night mask: computed
  directly from the sub-solar point (`illumination.sub_solar_lonlat_deg`, one SPICE call total) via
  the standard spherical solar-elevation law-of-cosines formula rather than a per-point SPICE
  `ilumin` call — cheap enough (pure `numpy`) to run at the backdrop's own full pixel resolution for
  a genuinely smooth terminator, not a coarse grid; white where sunlit, 80%-white (light grey) where
  in shadow, drawn as the base layer with the backdrop on top at its own opacity. Illumination
  reference time is `overview_map.dataset_midpoint_datetime` — halfway between the dataset's earliest
  `start_time` and latest `stop_time` — one global snapshot, not per-entry lighting. Live-verified
  against that same `sub_solar_lonlat_deg` call at the same epoch: the rendered terminator's longitude
  boundaries matched its ±90° antipodal boundary almost exactly. A 30°-spaced lon/lat grid is drawn
  for scale reference.

  Each entry's own FOV is a real footprint polygon (a straight-line quadrilateral through
  `entry.camera.footprint_lonlat_deg`'s 4 corners — fine at this whole-Moon zoom level, no need for
  the real geodesic edges), not just a center point — a real per-entry `Camera`/SPICE rebuild cost
  (the same one `report.problem_flags`'s own dropped footprint-geometry check avoided for a cheaper,
  more-frequently-run function), accepted here since this map is generated on demand, not on every
  `write_index()` call. Each entry's index label is anchored at its footprint's own bounding-box
  upper-right corner (`_upper_right_label_point`), not its center, so it doesn't collide with the
  polygon; drawn in `darkred` for better contrast against the backdrop than plain `red`. A footprint
  that straddles the +/-180° antimeridian is drawn correctly, not as a spurious line across the whole
  plot — plain matplotlib has no built-in geographic wraparound, so `_antimeridian_split_xy` applies
  this codebase's own existing per-edge unwrap-then-clip technique
  (`illumination.unwrap_relative_deg`, already used by `dataset_selection_plots._underline_segments`
  for the same reason) to each of the polygon's 4 edges, inserting a `nan` at any edge that crosses
  the seam so a single `ax.plot` call skips drawing across the break; verified against a synthetic
  seam-straddling footprint (none of `trn_dataset`'s own 2 real entries happen to cross it).
- **Richer problem flags**: crater-sharpness grading (`crater_depth.py`), a real tie-point pixel
  residual (not computed anywhere today — `tie_points.py` only produces ground-truth pixel
  *locations* for overlay plotting, no image-based comparison), and the footprint-geometry check
  described above, once each has a cheap-enough path (a persisted value or a lightweight query, not
  a fresh SPICE/camera rebuild or GLD100 fetch per entry per `write_index()` call).
