# Report generation: per-entry HTML reports + dataset-wide site

A small, templated HTML report per dataset entry, cheap enough to regenerate for every entry, plus
a four-page site tying them together (a nav bar, an overview map, an overview table, and the
per-entry reports themselves) — an alternative to `notebooks/image_generation.py`'s single-entry,
read-top-to-bottom demo notebook for looking at *many* entries side by side.

## Mechanism

Reuses tooling this repo already depends on (`jupytext`, `papermill`, `nbconvert`), plus one small
piece of custom code (`trntest.report.render_template`) for the one job none of those three tools
does:

1. **Template**: `notebooks/report_template.py`, a `.py:percent`-format text notebook with `{{ name
   }}` placeholders inline in the code (e.g. `load_entry("{{ dataset_folder }}", "{{ entry_index
   }}")`) — plain text substitution, not papermill's parameter-injection mechanism (papermill's own
   `parameters`-tagged-cell mechanism injects a *new* cell with overridden values *after* the tagged
   cell, so code inside that cell never sees the override — incompatible with a compact one-line
   `entry = trntest.report.load_entry(...)` using literal values). The title heading is filled in
   the same way (`dataset_name`/`entry_index`/`product_id`, all cheap to compute from the manifest
   before the notebook ever runs).

   Deliberately compact: no explanatory markdown beyond a one-line title, every cell a single call
   into `src/trntest/report.py` (`load_entry`/`summary`/`reproject_overlay`/`reproject_zoom_blink`),
   named so the call itself reads as documentation. The template needs exactly one import
   (`import trntest` — `report` is registered in `trntest/__init__.py` alongside `plotting`); the
   `import`+`load_entry` line is merged onto one line with the following `summary(entry)` call into
   a single cell (needs `# noqa: E702, I001  # fmt: skip`, since `ruff format`/`ruff check` otherwise
   disallow a semicolon-joined import-then-statement).

   Not paired/committed as a notebook: the template's `{{ }}` text isn't valid parameter defaults, so
   it can't be executed directly the way every other notebook here is (`scripts/run_notebook.sh`) —
   there's no `.ipynb` twin to keep in sync. `render_template(text, params)`
   (`src/trntest/report.py`) is a two-line regex substitution (`\{\{\s*(\w+)\s*\}\}`, raising
   `KeyError` on an unresolved placeholder) — reaches into markdown text too (used for the title);
   `summary()`'s per-entry text goes through `IPython.display.Markdown(f"...")` instead, since it
   depends on `entry.camera` at run time, not a static value.
2. **Per-entry execution**: `trntest.report.generate_report(dataset_folder, entry_index,
   report_dir)` runs the substitution above (writes `<report_dir>/report.py`) → `jupytext --to
   notebook` → `papermill` (executes in place) — all via `subprocess_utils.run_quiet`, in-process, no
   `docker compose run` wrapper (the caller already runs inside the container).
   `notebooks/report_template.py` itself is only ever read, never written to or executed.
   `TrnTestReport._generate_impl` (`src/trntest/trn_products.py`) is the normal caller — see "Report
   as a product type" below; `scripts/generate_report.sh <entry_index> [dataset_folder]
   [report_dir]` calls the same function for manual single-entry regeneration. `entry_index` (the
   entry's position in the dataset, `TrnTestEntry.index`) is the report's primary — and only —
   lookup key; `TrnTestDataSet.__getitem__` also supports lookup by EDR product id, but `load_entry`
   doesn't expose that (one lookup mode is enough for this template's own use).
3. **HTML export**: `jupyter nbconvert --to html <report_dir>/report.ipynb
   --ExtractOutputPreprocessor.enabled=True --NbConvertApp.output_files_dir=images
   --TemplateExporter.exclude_input_prompt=True --TemplateExporter.exclude_output_prompt=True` right
   after. Code cells stay visible (the point is to see the code, not just its output) but the
   `In[N]:`/`Out[N]:` execution-count gutters are suppressed. `ExtractOutputPreprocessor` (nbconvert's
   own built-in mechanism) pulls each cell's *figure* output out of the executed notebook's
   embedded-base64 output into a real file under `images/`, rewriting the cell's `<img>` tag to that
   relative path — but only for a plain-`Figure`-returning cell; an `IPython.display.HTML` object's
   base64 payload (the report's own GIF-returning cells) isn't image output in nbconvert's sense, so
   it stays inlined in the HTML instead.

## Report as a product type

`TrnTestReport` (`src/trntest/trn_products.py`) is a `TrnTestProduct` alongside `TrnTestCropImage`/
`TrnTestHillshadeImage`/`TrnTestReprojectImage` — `entry.report`, `"report"` in `PRODUCT_TYPES`
(default-on, unlike opt-in `reproject`). Not a bolt-on: `task_state`/`truncate`/`status` already
treat any `images_by_type` entry generically, so plugging `report` in gets its task-queue lifecycle,
failure tracking, and backfilling of already-populated entries for free — including under
`populate_via_workers()`, where it parallelizes across entries the same way crop/hillshade do.
`TrnTestReport._generate_impl` self-ensures its one dependency (`entry.reproject.generate()`, a
no-op once done) rather than relying on callers passing `product_types` in a particular order, the
same pattern `TrnTestReprojectImage` uses for its own dependency on `entry.crop_result`.

The nav bar/overview table/overview map are a different shape — one file summarizing every entry,
not one entry's own artifact — so they're a separate step, `TrnTestDataSet.write_index()`, run once
(not per worker) after `populate()`/`populate_via_workers()`'s task-queue loop. It writes
`<dataset_folder>/status.csv` (`status()` plus a `problems` column from
`trntest.report.problem_flags`), `<dataset_folder>/reports/overview_table.html`
(`report.write_overview_table_html`), `<dataset_folder>/reports/index.html` (`report.write_index_html`
— the nav bar, see below), and `<dataset_folder>/reports/overview_map.png`
(`overview_map.write_overview_map`) — the last two are each individually skippable
(`write_index(write_overview_map=False)`; `write_overview_map` has its own real per-entry SPICE
cost — see `docs/batch-generation.md` for why that matters at scale).

`problem_flags(entry)` is deliberately narrow: cheap, zero-fetch heuristics on `entry.row` only
(currently just low sun elevation — deep-shadow risk, `LOW_SUN_ELEVATION_DEG_THRESHOLD` in
`report.py`, a first guess not a validated cutoff). A footprint-geometry outlier check was
considered and dropped: it would need `entry.camera` (a real SPICE/camera rebuild, not persisted
anywhere cheap to re-read), which would make `problem_flags` re-do that work on every
`write_index()` call rather than staying cheap/pure-Python. (The overview map's own FOV polygons pay
this same cost deliberately, for a different, presentation-only purpose, generated on demand rather
than on every `write_index()` call — see "Overview map" below.)

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
                             # never has anything to extract; kept anyway since nbconvert creates it
                             # unconditionally, and a future Figure-returning cell would populate it
<dataset_folder>/status.csv   # TrnTestDataSet.write_index() -- one row per entry
```

`reports/` is a subfolder of the dataset folder itself, alongside `crop`/`hillshade`/`reproject`/
`_work`. `TrnTestDataSet.create()` creates it up front the same way it does the other
product-type subfolders.

## Viewing reports

**The nav bar (`reports/index.html`) cannot be viewed through JupyterLab's own server at all —
use `scripts/serve_reports.sh` instead.** Jupyter Server's `AuthenticatedFileHandler` (the handler
behind every `/files/...` response) unconditionally gives served HTML an opaque origin (`sandbox
allow-scripts`, no `allow-same-origin`) plus `frame-ancestors 'self'` — and an opaque origin can
never satisfy `'self'`, so no page Jupyter serves can ever embed another page Jupyter serves in an
iframe or frame, regardless of how the embedding page is structured. No server config fixes this —
the `sandbox` token is hardcoded onto that one handler. `scripts/serve_reports.sh [port]
[dataset_folder]` runs a plain `python3 -m http.server` over one dataset's `reports/` folder,
entirely separate from JupyterLab, with no CSP at all — the nav bar's iframe design works normally
there. Tunnel that port the same way as JupyterLab's own
(`ssh -L <port>:localhost:<port> <this-host>`) and open `http://localhost:<port>/reports/index.html`.

Single-page views (one `report.html`, `overview_table.html`, `overview_map.png`/`map.html` alone, no
nav bar) work fine through JupyterLab too — browse to the file and open it, or use
`scripts/serve_reports.sh` for those as well. Links between single pages work either way; that was
its own, separate, now-fixed issue (Jupyter Server's Referer-based anti-CSRF check on `/files/...`
GETs 403'd a link clicked from JupyterLab's sandboxed `srcdoc` HTML viewer, since it sends no
Referer — fixed by `docker/Dockerfile`'s `--ServerApp.allow_origin='*'`, a real server-side fix,
unlike the nav-bar embedding restriction above).

## Nav bar

`report.write_index_html` (`src/trntest/report.py`) writes `reports/index.html`: a single document
with a fixed nav `<div>` (CSS flexbox, `flex: 0 0 auto`) above one content `<iframe>`
(`flex: 1 1 auto`) — not a `<frameset>`, purely a styling choice (both forms hit the same CSP wall
above regardless). Layout, left to right: the dataset name (bold), `Map`/`Table` links
(content-frame-targeted via plain `<a target="content">`, no JS needed), Prev/Next buttons (adjacent
to each other, each disabled at its own end of the entry range), then a plain number `<input>` (not
a `<select>` — a dropdown with one `<option>` per entry doesn't scale to a many-hundred-entry
dataset) that doubles as the current-entry display, labeled "Entry index ... (max N)" — not "of N+1"
— since entries are 0-indexed throughout (`TrnTestEntry.index`, report titles, URLs) and stating the
actual maximum valid value avoids the off-by-one reading a count invites. The map link points at
`map.html` (a thin `<img>` wrapper `overview_map.write_overview_map` also writes), not
`overview_map.png` directly — linking straight to the raw image makes some browsers (confirmed in
Firefox) treat it as a standalone image document, shrunk to a thumbnail with an unreliable
click-to-zoom.

The nav bar's "current entry" state (for Prev/Next and the number box) lives in `index.html`'s own
in-memory JS, kept in sync regardless of how a report was reached — including a link clicked
*inside* the content iframe (e.g. the overview table's own per-entry links), not just the nav bar's
own controls — via `postMessage`: `generate_report` post-processes nbconvert's own `report.html`
output (a string-replace injecting one `<script>` tag, not a notebook cell, so
`report_template.py`'s own "every cell is a single call" convention stays untouched) to call
`window.parent.postMessage({source: "trntest-report", entryIndex: N}, "*")` on load.
`index.html`'s own `message` listener (`NAV_SYNC_MESSAGE_SOURCE`, shared between both ends) calls the
same `updateNavState` the nav bar's own controls use. `postMessage` specifically, not direct
cross-frame property access, because it isn't blocked by the opaque-origin restriction above.

Known limitation: this state lives only in that one page load — reloading `index.html` itself resets
it to "none selected" (both buttons then enabled; either one goes to entry 0).

## Overview map

`src/trntest/overview_map.py` (`plot_overview_map`/`write_overview_map`, registered in
`trntest/__init__.py` alongside `plotting`/`report`) — called by `TrnTestDataSet.write_index()` by
default (see `docs/batch-generation.md` for the real per-call cost this adds and how to skip it
during incremental population), linked from the nav bar's "Map" link.

Background: `luna_wac_global` (Lunaserv — deprecated for per-camera fetches in favor of `WAC_EMP` on
image-quality grounds, but that concern doesn't apply to this map's own low-opacity display), plain
geographic lon/lat (`config.lunaserv_dem_srs`, reused as-is despite the DEM-flavored name — same
fixed CRS), fetched once via the existing `cache.fetch_lunaserv_getmap` and cached like any other
tile, at 40% opacity. Day/night mask: computed directly from the sub-solar point
(`illumination.sub_solar_lonlat_deg`, one SPICE call total) via the standard spherical
solar-elevation law-of-cosines formula rather than a per-point SPICE `ilumin` call — cheap enough
(pure `numpy`) to run at the backdrop's own full pixel resolution for a smooth terminator; white
where sunlit, 80%-white (light grey) where in shadow, drawn as the base layer with the backdrop on
top at its own opacity. Illumination reference time is `overview_map.dataset_midpoint_datetime` —
halfway between the dataset's earliest `start_time` and latest `stop_time` — one global snapshot,
not per-entry lighting. A 30°-spaced lon/lat grid is drawn for scale reference.

Each entry's own FOV is a real footprint polygon (a straight-line quadrilateral through
`entry.camera.footprint_lonlat_deg`'s 4 corners — fine at whole-Moon zoom, no need for the real
geodesic edges), not just a center point — a real per-entry `Camera`/SPICE rebuild cost, accepted
here since this map is generated on demand rather than the more-frequently-run `problem_flags`.
Each entry's index label is anchored at its footprint's own bounding-box upper-right corner
(`_upper_right_label_point`), not its center, so it doesn't collide with the polygon; drawn in
`darkred` for contrast against the backdrop. A footprint that straddles the ±180° antimeridian is
drawn correctly (plain matplotlib has no built-in geographic wraparound): `_antimeridian_split_xy`
applies this codebase's existing per-edge unwrap-then-clip technique
(`illumination.unwrap_relative_deg`, also used by
`dataset_selection_plots._underline_segments`) to each of the polygon's 4 edges, inserting a `nan`
at any edge that crosses the seam so a single `ax.plot` call skips drawing across the break.

## Current report content

A title (dataset name, entry index, entry id), a one-line summary (orbit, center, sun
elevation/azimuth via `illumination.sun_azimuth_elevation_deg` at `entry.camera`'s own footprint
center/epoch — the same call the real render's own relighting uses, rather than the manifest's
`sun_elevation_deg` column, which uses a different method and has no azimuth counterpart), and
`reproject`'s overlay-toggle (`entry.reproject.plot_overlay(margin_frac=0.15)` — half
`plot_overlay`'s own 0.3 default, so more of the report's fixed page width goes to the overlay
itself than basemap padding) and full-resolution zoom blink against the basemap — the `reproject`
equivalents of `image_generation.py`'s Phase 6B/6C, deliberately just these two, not the full
Phase 5-8 sweep. No tie points, craters, or `crop` product in the report yet.

## Open work

- **Richer problem flags** (crater-sharpness grading, a real tie-point pixel residual, the
  footprint-geometry check described under "Report as a product type") — see
  `docs/proposed-tasks/open-items.md`.
- **No nav-bar link to the four pages from anywhere outside the site itself** — a user has to know
  to browse to `reports/index.html` directly (via `scripts/serve_reports.sh`); nothing in
  `README.md`'s own workflow points there yet beyond a status-section mention.
