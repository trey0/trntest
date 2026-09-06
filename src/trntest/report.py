"""Per-entry report helpers -- keeps notebooks/report_template.py's cells to one-liners, plus the
pipeline that renders that template (`generate_report`, used by `TrnTestReport._generate_impl`) and
the dataset-wide nav bar/overview table (`write_index_html`/`write_overview_table_html`, used by
`TrnTestDataSet.write_index`)."""

import json
import re
from pathlib import Path

from IPython.display import Markdown, display

from trntest import illumination
from trntest.config import load_config
from trntest.session import Session
from trntest.subprocess_utils import run_quiet
from trntest.trn_dataset import TrnTestDataSet, TrnTestEntry

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "notebooks" / "report_template.py"

LOW_SUN_ELEVATION_DEG_THRESHOLD = 10.0  # first-guess placeholder -- tune once a real batch run
# shows what's actually worth flagging.

NAV_SYNC_MESSAGE_SOURCE = "trntest-report"  # shared between generate_report's injected postMessage
# call and write_index_html's own listener for it -- see both docstrings.


def render_template(text: str, params: dict[str, str]) -> str:
    """Substitute `{{ name }}` placeholders in `text` with `params[name]`.

    :param text: Template text.
    :param params: Placeholder values, keyed by name.
    :returns: The substituted text.
    :raises KeyError: If a placeholder has no matching entry in `params`.
    """
    # Used to fill in `notebooks/report_template.py`'s own placeholders before jupytext/papermill
    # ever see it; see `scripts/generate_report.sh`.
    return _PLACEHOLDER_RE.sub(lambda m: params[m.group(1)], text)


def load_entry(dataset_folder: str, entry_index: int) -> TrnTestEntry:
    """Open a dataset and look up one entry by its positional index.

    :param dataset_folder: Dataset folder path.
    :param entry_index: The entry's position in the dataset (0-based) -- `TrnTestDataSet.images` is
        reset to a dense `0..n-1` index at construction, so this is always a stable short id
        regardless of how the entry was originally looked up (see `TrnTestEntry.index`). Searching by
        EDR product ID instead would also be possible (`TrnTestDataSet.__getitem__` already supports
        it), but isn't exposed here -- one lookup mode is enough for this template's own use.
    :returns: The entry.
    """
    session = Session()
    return TrnTestDataSet.open(dataset_folder, session.config)[int(entry_index)]


def summary(entry: TrnTestEntry) -> None:
    """Display a one-line Markdown summary of `entry` (product ID, orbit, center, sun geometry).

    Sun azimuth/elevation are computed fresh via `illumination.sun_azimuth_elevation_deg` at
    `entry.camera`'s own footprint center/epoch -- the same call `hapke_shade_ortho` itself uses for
    the real render's lighting -- rather than read from the manifest's own `sun_elevation_deg`
    column, which uses a different (ellipsoid-normal) method and has no azimuth counterpart; mixing
    the two would show numerically inconsistent elevations side by side.
    """
    row = entry.row
    center = entry.camera.footprint_lonlat_deg["center"]
    assert center is not None, "camera's nadir footprint center must be a real ground point"
    azimuth_deg, elevation_deg = illumination.sun_azimuth_elevation_deg(*center, entry.camera.et)
    display(
        Markdown(
            f"**{entry.product_id}** -- orbit {row['orbit_number']}, "
            f"center ({row['center_lat_deg']:.3f}, {row['center_lon_deg']:.3f}), "
            f"sun elevation {elevation_deg:.1f} deg, azimuth {azimuth_deg:.1f} deg"
        )
    )


def reproject_overlay(entry: TrnTestEntry):
    """Display `entry`'s reproject render as an overlay-toggle GIF over the basemap.

    `margin_frac` is set to roughly half `plot_overlay`'s own default (0.3 -> 0.15) so more of the
    report's fixed page width goes to the overlay itself rather than basemap padding.

    :returns: An `IPython.display.HTML` object -- bare last expression, no trailing `;`, same
        requirement as `TrnTestImage.plot_overlay`.
    """
    return entry.reproject.plot_overlay(margin_frac=0.15)


def reproject_zoom_blink(entry: TrnTestEntry):
    """Display a full-resolution zoom blink between `entry`'s reproject render and the basemap.

    :returns: An `IPython.display.HTML` object -- same bare-last-expression requirement as
        `reproject_overlay`.
    """
    return entry.reproject.plot_zoom_blink_over()


def generate_report(dataset_folder: str, entry_index: int, report_dir: Path) -> None:
    """Renders `notebooks/report_template.py` for one entry: substitutes its `{{ }}` placeholders,
    jupytext-syncs the result to a notebook, papermill-executes it, then nbconvert-exports it to
    HTML with figures written to `images/` as real files rather than embedded base64 (see the
    `ExtractOutputPreprocessor` comment below for how).

    Runs in-process, no `docker compose run` wrapper -- callers (`TrnTestReport._generate_impl`,
    `scripts/generate_report.sh`) already run inside the container.

    :param dataset_folder: Dataset folder path, passed through to the template's own `load_entry`
        call.
    :param entry_index: Entry's positional index in the dataset, passed through to the template's
        own `load_entry` call.
    :param report_dir: Output folder -- `report.py`/`report.ipynb`/`report.html`/`images/` are
        written here.
    """
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_py = report_dir / "report.py"
    report_ipynb = report_dir / "report.ipynb"
    # product_id is looked up here (cheap -- just a manifest row, no SPICE) rather than in the
    # template itself, so the page's own title can be filled in via plain `{{ }}` substitution like
    # every other static value, instead of a Python call the "no explanatory markdown beyond a
    # one-line title" convention (report-plan.md) would otherwise rule out.
    dataset = TrnTestDataSet.open(dataset_folder, load_config())
    params = {
        "dataset_folder": dataset_folder,
        "entry_index": str(entry_index),
        "dataset_name": dataset.name,
        "product_id": dataset[entry_index].product_id,
    }
    report_py.write_text(render_template(_TEMPLATE_PATH.read_text(), params))
    run_quiet(["jupytext", "--to", "notebook", str(report_py), "--output", str(report_ipynb)])
    run_quiet(
        [
            "papermill",
            str(report_ipynb),
            str(report_ipynb),
            "--cwd",
            str(_REPO_ROOT / "notebooks"),
            "--log-output",
            "--no-progress-bar",
        ]
    )
    # ExtractOutputPreprocessor is nbconvert's own built-in mechanism for pulling each cell's
    # displayed figure out of the executed notebook's embedded-base64 output into a real file under
    # output_files_dir, rewriting the cell's <img> tag to that relative path -- not custom code.
    run_quiet(
        [
            "jupyter",
            "nbconvert",
            "--to",
            "html",
            str(report_ipynb),
            "--output",
            "report.html",
            "--ExtractOutputPreprocessor.enabled=True",
            "--NbConvertApp.output_files_dir=images",
            "--TemplateExporter.exclude_input_prompt=True",
            "--TemplateExporter.exclude_output_prompt=True",
        ]
    )
    # Post-processes nbconvert's own output (not a notebook cell -- keeps report_template.py's own
    # "every cell is a single call" convention untouched) to announce this entry's index to the nav
    # bar (write_index_html) via postMessage on load, so Prev/Next/the entry number box stay in sync
    # even when this page was reached by clicking a link inside the content iframe (e.g. the overview
    # table's own per-entry links) rather than through the nav bar's own controls. postMessage works
    # regardless of the two frames' origins, unlike direct property access -- see write_index_html's
    # own docstring for the opaque-origin restriction this specifically sidesteps.
    report_html = report_dir / "report.html"
    sync_script = (
        f"<script>if (window.parent !== window) {{ window.parent.postMessage("
        f'{{source: "{NAV_SYNC_MESSAGE_SOURCE}", entryIndex: {entry_index}}}, "*"); }}</script>'
    )
    report_html.write_text(report_html.read_text().replace("</body>", f"{sync_script}</body>"))


def problem_flags(entry: TrnTestEntry) -> list[str]:
    """Cheap, zero-fetch heuristic checks on `entry.row` -- a first pass at flagging entries worth
    a closer look, not an authoritative quality signal.

    Silently skips a check whose manifest column isn't present (`entry.row.get`, not `[]`) rather
    than raising -- a real manifest always has `candidate_window.DATASET_COLUMNS`, but a hand-built or
    minimal one (e.g. in tests) may not, and a missing column here just means "nothing to flag,"
    not a bug.
    """
    flags = []
    sun_elevation = entry.row.get("sun_elevation_deg")
    if sun_elevation is not None and sun_elevation < LOW_SUN_ELEVATION_DEG_THRESHOLD:
        flags.append(f"low sun elevation ({sun_elevation:.1f}\N{DEGREE SIGN}) -- deep shadow risk")
    return flags


def write_overview_table_html(dataset: TrnTestDataSet, status_df) -> None:
    """Writes `<dataset.folder>/reports/overview_table.html`: one row per entry, linking to its own
    `reports/<edr_product>/report.html` where it already exists, alongside `status_df`'s other
    columns. The product-id column doubles as the entry-index column (`{entry.index}:
    {product_id}`, both part of the same link) -- the nav bar's own jump-to-entry box takes an
    index, not a product id, so this is the table's own way of exposing that lookup key. Loaded
    into `index.html`'s content frame by default (see `write_index_html`). Deliberately plain -- no
    styling/JS beyond that, just a table; fine if a link is momentarily broken because that entry's
    report doesn't exist yet.
    """
    header_cells = "".join(f"<th>{col}</th>" for col in status_df.columns)
    rows_html = []
    for _, row in status_df.iterrows():
        entry = dataset[row["product_id"]]
        label = f"{entry.index}: {row['product_id']}"
        if entry.report.exists():
            product_cell = f'<a href="{entry.edr_product}/report.html">{label}</a>'
        else:
            product_cell = f"{label} (no report yet)"
        other_cells = "".join(f"<td>{row[col]}</td>" for col in status_df.columns[1:])
        rows_html.append(f"<tr><td>{product_cell}</td>{other_cells}</tr>")
    name = dataset.name
    html = (
        f"<html><head><title>{name} reports</title></head><body>"
        f"<h1>{name} reports</h1>"
        f'<table border="1" cellpadding="4"><tr>{header_cells}</tr>{"".join(rows_html)}</table>'
        "</body></html>"
    )
    (dataset.folder / "reports" / "overview_table.html").write_text(html)


def write_index_html(dataset: TrnTestDataSet, status_df) -> None:
    """Writes `<dataset.folder>/reports/index.html`: the persistent nav bar (dataset name, links to
    the map/table, prev/next, a jump-to-entry number box) over a content `<iframe>` that defaults to
    `overview_table.html` -- also (re)written here via `write_overview_table_html`, so one
    `write_index_html` call refreshes both files. See `TrnTestDataSet.write_index`.

    A single document with a nav `<div>` (CSS flexbox, `flex: 0 0 auto`) above one content `<iframe>`
    (`flex: 1 1 auto`), not a `<frameset>` split into separate nav/content pages -- avoids any
    cross-frame scripting between sibling frames (which Jupyter's own CSP `sandbox` header, applied
    per file served via `/files/...`, would give distinct opaque origins and block): the nav bar's
    script only ever sets its *own* child iframe's `src` attribute, a same-document DOM operation,
    never reads or reaches into the iframe's own `window`/`document`. (This page cannot actually be
    *viewed* through Jupyter at all regardless, for a different, more fundamental reason -- see
    `docs/proposed-tasks/report-plan.md`'s "Nav bar" section and `scripts/serve_reports.sh`.)

    A number box (not a dropdown) is the jump-to-entry control -- a `<select>` with one `<option>`
    per entry doesn't scale to a many-hundred-entry dataset the way a plain "type a number" box does.
    It also doubles as the current-entry display, kept in sync by `updateNavState`, which also
    disables Prev/Next at either end of the entry range.

    The nav bar's "current entry" state lives only in this page's own in-memory JS (`current`) --
    reloading `index.html` itself resets it to "none selected" (both buttons enabled; either one
    goes to entry 0). It's kept in sync with navigation that happens *inside* the content iframe
    without going through the nav bar's own controls (e.g. clicking a row's link directly in the
    overview table) via a `message` listener: each per-entry report page
    (`generate_report`'s own postMessage injection, `NAV_SYNC_MESSAGE_SOURCE`) announces its own
    index on load, regardless of how it was navigated to.
    """
    write_overview_table_html(dataset, status_df)
    product_ids_json = json.dumps(list(dataset.images["product_id"]))
    name = dataset.name
    n_entries = len(dataset.images)
    last_index = n_entries - 1
    html = f"""<!DOCTYPE html>
<html>
<head>
<title>{name} reports</title>
<style>
  html, body {{ margin: 0; padding: 0; height: 100%; }}
  body {{ display: flex; flex-direction: column; }}
  #navbar {{
    flex: 0 0 auto; box-sizing: border-box; display: flex; align-items: center; gap: 10px;
    background: #eee; border-bottom: 1px solid #ccc; padding: 6px 8px;
    font-family: sans-serif; font-size: 13px; white-space: nowrap; overflow-x: auto;
  }}
  #dsname {{ font-weight: bold; font-size: 15px; }}
  #entryInput {{ width: 4em; }}
  #content {{ flex: 1 1 auto; width: 100%; border: none; }}
</style>
</head>
<body>
<div id="navbar">
  <span id="dsname">{name}</span>
  <a href="map.html" target="content">Map</a>
  <a href="overview_table.html" target="content">Table</a>
  |
  <button id="prevBtn" onclick="step(-1)">&laquo; Prev</button>
  <button id="nextBtn" onclick="step(1)">Next &raquo;</button>
  |
  Entry index
  <input id="entryInput" type="number" min="0" max="{last_index}"
         onkeydown="if (event.key === 'Enter') goToInput()">
  (max {last_index})
  <button onclick="goToInput()">Go</button>
</div>
<iframe id="content" name="content" src="overview_table.html"></iframe>
<script>
  const productIds = {product_ids_json};
  let current = null;
  function updateNavState(i) {{
    current = i;
    document.getElementById('entryInput').value = i;
    document.getElementById('prevBtn').disabled = (i <= 0);
    document.getElementById('nextBtn').disabled = (i >= productIds.length - 1);
  }}
  function goToIndex(i) {{
    if (i < 0 || i >= productIds.length) return;
    updateNavState(i);
    document.getElementById('content').src = productIds[i] + '/report.html';
  }}
  function goToInput() {{
    const i = parseInt(document.getElementById('entryInput').value, 10);
    if (!isNaN(i)) goToIndex(i);
  }}
  function step(delta) {{
    goToIndex(current === null ? 0 : current + delta);
  }}
  window.addEventListener('message', function (event) {{
    if (event.data && event.data.source === {json.dumps(NAV_SYNC_MESSAGE_SOURCE)}) {{
      updateNavState(event.data.entryIndex);
    }}
  }});
</script>
</body>
</html>"""
    (dataset.folder / "reports" / "index.html").write_text(html)
