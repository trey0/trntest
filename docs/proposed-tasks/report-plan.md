# Design: per-image Jupyter/HTML reports

**Status: first minimal prototype built and hand-run repeatedly; not yet the full vision.**
`notebooks/report_template.py` ({{ }} substitution, see "Mechanism" below) +
`scripts/generate_report.sh`/`scripts/render_report_template.py` exist and produce one entry's
report end to end, but only with the deliberately small scope described below. Growing the
report's content and adding the multi-entry index page are both explicit follow-ups, not started.

## Context

`notebooks/image_generation.py` is the flagship demo notebook: one long, hand-curated, multi-phase
walkthrough of a *single* manifest entry (`docs/architecture.md`'s `TrnTestEntry`), meant to be read
top-to-bottom in JupyterLab or on GitHub. There's no lightweight way to look at *many* entries side
by side, or to generate a shareable, standalone artifact for one entry without dragging in the
whole demo notebook's narrative.

The goal here is different: a small, templated report — one HTML page per entry — cheap enough to
regenerate for every entry in a dataset, viewable outside JupyterLab (a browser, no running
kernel), with images written to disk as files rather than embedded as base64 (avoids bloating the
HTML and lets a future index page reference the same files directly). Eventually, an index page
would list every entry's report (e.g. in an `<iframe>`) with fast next/prev navigation — not built
yet, see "Future work" below.

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
2. **Per-entry execution**: `scripts/generate_report.sh <edr_product> [dataset_folder]
   [report_dir]` runs `scripts/render_report_template.py` (writes `<report_dir>/report.py`, the
   substituted template) → `jupytext --to notebook` (→ `<report_dir>/report.ipynb`) → `papermill`
   (executes that notebook in place, no `-p` needed anymore) inside Docker (same `docker compose
   run --rm demo` pattern as `scripts/run_notebook.sh`) — `notebooks/report_template.py` itself is
   only ever read, never written to or executed.
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

## On-disk layout

```
<output_dir>/reports/<edr_product>/
  report.py          # notebooks/report_template.py with {{ }} substituted -- kept for provenance
  report.ipynb        # jupytext-synced + papermill-executed -- also kept for debugging
  report.html          # nbconvert's HTML export -- the deliverable
  images/
    report_N_0.png       # nbconvert's own ExtractOutputPreprocessor naming (cell index-based),
                          # not chosen by report.py -- see "Mechanism" above
```

`<output_dir>/reports/` sits alongside `<output_dir>/trn_dataset/` (a separate top-level folder,
not nested inside it) — `output_dir` is already per-worktree-safe
(`docs/environment.md`'s "Multi-agent worktrees" section), so no extra namespacing was needed for
this to be concurrent-worktree-safe too.

## First-pass scope (deliberately minimal)

Per the user's explicit ask to keep this pass simple so the mechanism itself can be iterated on
quickly: the template renders **one image (the hillshade render, via the existing generic
`plotting.plot_raster`) and a couple of manifest fields** — orbit number, center lat/lon — nothing
else. No overlay/basemap comparison, no tie points, no craters, no `crop`/`reproject` products. All
of those are already-implemented pieces (`plotting.py`, `tie_points.py`, `craters.py`) that
`notebooks/image_generation.py`'s Phases 5-8 already exercise per-entry — extending the report to
reuse them is expected to be straightforward once the papermill/nbconvert mechanism itself is
confirmed to work end to end, not a design risk.

Hand-run against several entries in the current `trn_dataset` (not just the default
`M1327210646CE`, `image_generation.py`'s own default), confirming each report renders that entry's
own data — via `scripts/generate_report.sh`.

## Future work (not started)

- **Grow report content** to match `image_generation.py`'s Phase 5/6/8 comparisons (overlay
  toggle, tie points, crater layer) once the minimal version above is confirmed to read well.
- **Index page**: a separate static HTML page listing every entry in a dataset, loading each
  entry's `report.html` in an `<iframe>` with a sidebar/list for picking an entry and
  previous/next navigation for flipping quickly between them. Since this prototype's report pages
  are self-contained static HTML+images (no running kernel, no server needed to view them), the
  index page can be equally static — no new serving infrastructure implied.
- **Batch generation**: a loop over `TrnTestDataSet`'s entries calling `generate_report.sh`-style
  logic for each, mirroring `TrnTestDataSet.populate()`'s per-entry iteration (not its task-queue
  claiming machinery — reports are cheap/idempotent to regenerate, not worth the same
  resumability machinery `populate()` needs for expensive SPICE/`sat_sim`/ISIS calls).
- **Interactive comparisons without the GIF-blink workaround**: `plotting.plot_overlay_toggle`'s
  auto-looping animated GIF exists specifically to survive GitHub's `.ipynb` static-viewer
  sanitizer (see `docs/architecture.md`'s `plotting.py` row) — a standalone HTML page this project fully
  controls (not viewed through GitHub's sanitizer) has no such constraint, so an `<input
  type="range">`/JS-driven alpha slider is available as a nicer future alternative for report pages
  specifically, without needing to touch the GIF mechanism `image_generation.py` still relies on.
