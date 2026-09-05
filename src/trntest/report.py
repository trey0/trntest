"""Per-entry report helpers -- keeps notebooks/report_template.py's cells to one-liners, plus the
pipeline that renders that template (`generate_report`, used by `TrnTestReport._generate_impl`) and
the dataset-wide index (`write_index_html`, used by `TrnTestDataSet.write_index`)."""

import re
from pathlib import Path

from IPython.display import Markdown, display

from trntest.plotting import plot_raster
from trntest.session import Session
from trntest.subprocess_utils import run_quiet
from trntest.trn_dataset import TrnTestDataSet, TrnTestEntry

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "notebooks" / "report_template.py"

LOW_SUN_ELEVATION_DEG_THRESHOLD = 10.0  # first-guess placeholder -- tune once a real batch run
# shows what's actually worth flagging.


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


def load_entry(dataset_folder: str, edr_product: str) -> TrnTestEntry:
    """Open a dataset and look up one entry by product ID.

    :param dataset_folder: Dataset folder path.
    :param edr_product: EDR product ID.
    :returns: The entry.
    """
    session = Session()
    return TrnTestDataSet.open(dataset_folder, session.config)[edr_product]


def summary(entry: TrnTestEntry) -> None:
    """Display a one-line Markdown summary of `entry` (product ID, orbit, center, sun elevation)."""
    row = entry.row
    display(
        Markdown(
            f"**{entry.product_id}** -- orbit {row['orbit_number']}, "
            f"center ({row['center_lat_deg']:.3f}, {row['center_lon_deg']:.3f}), "
            f"sun elevation {row['sun_elevation_deg']:.1f} deg"
        )
    )


def hillshade(entry: TrnTestEntry) -> None:
    """Display `entry`'s hillshade raster."""
    plot_raster(entry.hillshade.raster_path)


def generate_report(dataset_folder: str, edr_product: str, report_dir: Path) -> None:
    """Renders `notebooks/report_template.py` for one entry: substitutes its `{{ }}` placeholders,
    jupytext-syncs the result to a notebook, papermill-executes it, then nbconvert-exports it to
    HTML with figures written to `images/` as real files rather than embedded base64 (see the
    `ExtractOutputPreprocessor` comment below for how).

    Runs in-process, no `docker compose run` wrapper -- callers (`TrnTestReport._generate_impl`,
    `scripts/generate_report.sh`) already run inside the container.

    :param dataset_folder: Dataset folder path, passed through to the template's own `load_entry`
        call.
    :param edr_product: EDR product ID.
    :param report_dir: Output folder -- `report.py`/`report.ipynb`/`report.html`/`images/` are
        written here.
    """
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_py = report_dir / "report.py"
    report_ipynb = report_dir / "report.ipynb"
    params = {"dataset_folder": dataset_folder, "edr_product": edr_product}
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


def problem_flags(entry: TrnTestEntry) -> list[str]:
    """Cheap, zero-fetch heuristic checks on `entry.row` -- a first pass at flagging entries worth
    a closer look, not an authoritative quality signal.

    Silently skips a check whose manifest column isn't present (`entry.row.get`, not `[]`) rather
    than raising -- a real manifest always has `dataset.DATASET_COLUMNS`, but a hand-built or
    minimal one (e.g. in tests) may not, and a missing column here just means "nothing to flag,"
    not a bug.
    """
    flags = []
    sun_elevation = entry.row.get("sun_elevation_deg")
    if sun_elevation is not None and sun_elevation < LOW_SUN_ELEVATION_DEG_THRESHOLD:
        flags.append(f"low sun elevation ({sun_elevation:.1f}\N{DEGREE SIGN}) -- deep shadow risk")
    return flags


def write_index_html(dataset: TrnTestDataSet, status_df) -> None:
    """Writes `<dataset.folder>/reports/index.html`: one row per entry, linking to its own
    `reports/<edr_product>/report.html` where it already exists, alongside `status_df`'s other
    columns. See `TrnTestDataSet.write_index`. Deliberately plain -- no styling/JS, just a table;
    fine if a link is momentarily broken because that entry's report doesn't exist yet.
    """
    header_cells = "".join(f"<th>{col}</th>" for col in status_df.columns)
    rows_html = []
    for _, row in status_df.iterrows():
        entry = dataset[row["product_id"]]
        if entry.report.exists():
            product_cell = f'<a href="{entry.edr_product}/report.html">{row["product_id"]}</a>'
        else:
            product_cell = f"{row['product_id']} (no report yet)"
        other_cells = "".join(f"<td>{row[col]}</td>" for col in status_df.columns[1:])
        rows_html.append(f"<tr><td>{product_cell}</td>{other_cells}</tr>")
    name = dataset.folder.name
    html = (
        f"<html><head><title>{name} reports</title></head><body>"
        f"<h1>{name} reports</h1>"
        f'<table border="1" cellpadding="4"><tr>{header_cells}</tr>{"".join(rows_html)}</table>'
        "</body></html>"
    )
    (dataset.folder / "reports" / "index.html").write_text(html)
