# Design: per-image Jupyter/HTML reports

**Status: first minimal prototype built and hand-run repeatedly (this session); not yet the full
vision.** `notebooks/report_template.py` (real `{{ }}` substitution, see "Mechanism" below) +
`scripts/generate_report.sh`/`scripts/render_report_template.py` exist and produce one real
entry's report end to end, but only with the deliberately small scope described below. Growing the
report's content and adding the multi-entry index page are both explicit follow-ups, not started.

## Context

`notebooks/image_generation.py` is the flagship demo notebook: one long, hand-curated, multi-phase
walkthrough of a *single* manifest entry (`docs/plan.md`'s `TrnTestEntry`), meant to be
read top-to-bottom in JupyterLab or on GitHub. There's no lightweight way to look at *many* entries
side by side, or to generate a shareable, standalone artifact for one entry without dragging in the
whole demo notebook's narrative.

The goal here is different: a small, templated report — one HTML page per entry — cheap enough to
regenerate for every entry in a dataset, viewable outside JupyterLab (a browser, no running
kernel), with images written to disk as real files rather than embedded as base64 (avoids bloating
the HTML and lets a future index page reference the same files directly). Eventually, an index page
would list every entry's report (e.g. in an `<iframe>`) with fast next/prev navigation — not built
yet, see "Future work" below.

## Mechanism

Reuses tooling this repo already depends on (`jupytext`, `papermill`, `nbconvert` — all already in
`pyproject.toml`/the Docker image), plus one small piece of custom code (`trntest.report.
render_template`) for the one job none of those three tools actually do:

1. **Template**: `notebooks/report_template.py`, a `.py:percent`-format text notebook with real
   `{{ name }}` placeholders inline in the code (e.g. `load_entry("{{ dataset_folder }}",
   "{{ edr_product }}")`) — genuine text substitution, not papermill's parameter-injection
   mechanism. **This was a real pivot, not the original design**: the first version of this
   prototype used a `parameters`-tagged cell + `papermill -p name value` instead (papermill's own
   built-in mechanism for exactly this kind of thing, and the reason it was chosen over `{{ }}`
   substitution in the first place — no second templating layer to build). That version hit a real
   correctness wall confirmed live: papermill works by inserting a *new* cell with the overridden
   values immediately *after* the tagged cell, so any code living inside the tagged cell itself
   only ever sees its own written-in defaults, never the override — only cells strictly after the
   injected one do. That makes a true one-line `entry = trntest.report.load_entry("<path>",
   "<id>")` with literal values fundamentally impossible to override via papermill's mechanism
   (there's no way to get papermill to rewrite code already inside an existing cell), which is
   exactly the compact style wanted here — so this went back to `{{ }}` substitution after all,
   this time genuinely needed rather than avoidable complexity. **Deliberately compact, per the
   user's explicit request**: the template carries no explanatory markdown beyond a one-line title
   — every cell is a single call into `src/trntest/report.py` (`load_entry`/`summary`/
   `hillshade`), named so the call itself reads as the documentation (no separate
   `## Hillshade render`-style headers needed). Anything more than a trivial call lives in
   `report.py`, not the notebook, and the notebook needs exactly one import (`import trntest`) —
   `report` is registered in `trntest/__init__.py` alongside `plotting`, so `trntest.report.<fn>
   (...)` is reachable with no second import line. **Not paired/committed as a notebook**: because
   the template's own source contains `{{ }}` text, not valid parameter defaults, it can't be
   executed directly the way every other notebook here is (`scripts/run_notebook.sh`), so there's
   no real `notebooks/report_template.ipynb` twin to keep in sync or commit — the user's own call,
   given the alternative (a lint exception, or a second sibling script whose only job is producing
   a real-executed example twin) added complexity for little benefit on a file that's a template
   consumed by a script, not something meant to be read directly. `render_template(text, params)`
   itself (`src/trntest/report.py`) is a two-line regex substitution (`\{\{\s*(\w+)\s*\}\}`,
   raising `KeyError` on an unresolved placeholder) — genuinely reaches into markdown text too if a
   template ever needs that (this one doesn't -- `summary()`'s per-entry text still goes through
   `IPython.display.Markdown(f"...")` since it's computed from `entry`, not known until the
   notebook actually runs, not a substitutable static value like `dataset_folder`/`edr_product`).
2. **Per-entry execution**: `scripts/generate_report.sh <edr_product> [dataset_folder] [report_dir]`
   runs `scripts/render_report_template.py` (writes `<report_dir>/report.py`, the substituted
   template) → `jupytext --to notebook` (→ `<report_dir>/report.ipynb`) → `papermill` (executes
   that notebook in place, no `-p` needed anymore) inside Docker (same `docker compose run --rm
   demo` pattern as `scripts/run_notebook.sh`) — `notebooks/report_template.py` itself is only ever
   read, never written to or executed.
3. **HTML export**: `jupyter nbconvert --to html <report_dir>/report.ipynb
   --ExtractOutputPreprocessor.enabled=True --NbConvertApp.output_files_dir=images
   --TemplateExporter.exclude_input_prompt=True --TemplateExporter.exclude_output_prompt=True`
   right after. Code cells stay visible (no `--no-input`, dropped after the user asked to see the
   code, not just its output) but the `In[N]:`/`Out[N]:` execution-count gutters on either side are
   suppressed — cosmetic chrome unrelated to whether content shows, confirmed by checking the actual
   rendered `<div class="jp-InputPrompt">`/`jp-OutputPrompt` elements (absent) vs. the CSS rules
   that define them (always present, bundled in every export regardless of these flags — not a
   useful signal by itself). Template cells display figures the normal way
   (`trntest.report.hillshade(entry)`, no return value captured, no `fig.savefig`/`plt.close`) —
   `ExtractOutputPreprocessor` is nbconvert's own built-in mechanism for turning that into a real
   file: it pulls each cell's image *out* of the executed notebook's normal embedded-base64 output
   and writes it to a real file under `output_files_dir` (`images/`, overriding the default
   `<name>_files/`), rewriting the cell's `<img>` tag to that relative path. It's registered in
   every exporter's `default_preprocessors` list and already on by default for the
   markdown/RST/LaTeX/asciidoc exporters — just not HTML's, which is presumably a legacy default
   from when HTML pages were expected to be self-contained single files; turning it on with a plain
   CLI flag is enough, no custom nbconvert template or preprocessor needed.
   **First design considered and abandoned**: an earlier draft of this prototype had template cells
   `fig.savefig()` to a file directly and reference it via a hand-written Markdown `![...]()` cell,
   avoiding nbconvert's default embedding by never producing a rich-display output to embed in the
   first place. It worked, but was noticeably more cumbersome per figure (three things to keep in
   sync: the save call, the file path, and the markdown reference) — replaced with the
   `ExtractOutputPreprocessor` approach above once the user pointed out nbconvert likely already had
   a built-in mode for this and it was confirmed live (a throwaway test notebook, one
   `plt.plot()` cell, `--ExtractOutputPreprocessor.enabled=True --NbConvertApp.output_files_dir=images`
   → `images/report_0_1.png` + a rewritten `<img>` tag, no notebook-side code changes required).

## On-disk layout

```
<output_dir>/reports/<edr_product>/
  report.py          # notebooks/report_template.py with {{ }} substituted -- kept for provenance
  report.ipynb        # jupytext-synced + papermill-executed -- also kept for debugging
  report.html          # nbconvert's HTML export -- the actual deliverable
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
of those are real, already-implemented pieces (`plotting.py`, `tie_points.py`, `craters.py`) that
`notebooks/image_generation.py`'s Phases 5-8 already exercise per-entry — extending the report to
reuse them is expected to be straightforward once the papermill/nbconvert mechanism itself is
confirmed to work end to end, not a design risk.

Hand-run repeatedly against this session's already-populated `trn_dataset`, including entries
other than the default `M1327210646CE` (`image_generation.py`'s own default), confirming each
report renders that entry's own data -- via `scripts/generate_report.sh`.

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
  sanitizer (see `docs/plan.md`'s `plotting.py` row and `docs/history.md`'s Phases 33-35) — a
  standalone HTML page this project fully controls (not viewed through GitHub's sanitizer) has no
  such constraint, so a real `<input type="range">`/JS-driven alpha slider is available as a nicer
  future alternative for report pages specifically, without needing to touch the GIF mechanism
  `image_generation.py` still relies on.
