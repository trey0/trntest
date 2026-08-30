"""Per-entry report helpers -- keeps notebooks/report_template.py's cells to one-liners.
See docs/proposed-tasks/report-plan.md."""

import re

from IPython.display import Markdown, display

from trntest.plotting import plot_raster
from trntest.session import Session
from trntest.trn_dataset import TrnTestDataSet, TrnTestEntry

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


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
