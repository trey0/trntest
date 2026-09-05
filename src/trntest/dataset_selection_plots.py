"""Dataset-selection scatter plots (`notebooks/select_datasets.py`'s own notebook cells) -- split
out of `plotting.py` since `dataset_selection.py`'s orbit-level candidate geometry is the only
reason this file depends on `illumination.py` at all, unlike the generator-comparison figures that
module otherwise holds.
"""

from datetime import datetime

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from trntest import illumination


def plot_sun_elevation_vs_edr_count(
    orbits_df: pd.DataFrame,
    period_start: datetime,
    period_end: datetime,
    sun_elev_bin_width_deg: float = 10.0,
    figsize: tuple[float, float] = (10, 7),
) -> None:
    """2D histogram (`notebooks/select_datasets.py`): how much sun elevation at the illuminated node
    buys you, in terms of acceptable-EDR yield.

    :param orbits_df: `dataset_selection.find_orbits`'s rows.
    :param period_start: Period start (for the title).
    :param period_end: Period end (for the title).
    :param sun_elev_bin_width_deg: Sun-elevation bin width, degrees (0-90).
    :param figsize: Figure size.
    """
    # x = sun-elevation bin, y = exact acceptable-EDR count (`orbits_df["acceptable_edr_count"]`, one
    # bin per integer value), colored by orbit count per cell.
    sun_elev_bins = np.arange(0, 91, sun_elev_bin_width_deg)
    max_edr_count = int(orbits_df["acceptable_edr_count"].max())
    edr_count_bins = np.arange(-0.5, max_edr_count + 1.5, 1)  # one bin per integer count, 0..max_edr_count

    fig, ax = plt.subplots(figsize=figsize)
    _, _, _, hist_image = ax.hist2d(
        orbits_df["illum_sun_elev_deg"],
        orbits_df["acceptable_edr_count"],
        bins=[sun_elev_bins, edr_count_bins],
        cmap="viridis",
    )
    fig.colorbar(hist_image, ax=ax, label="orbit count")

    ax.set_xticks(sun_elev_bins)
    ax.set_yticks(np.arange(0, max_edr_count + 1, 1))
    ax.set_xlabel("sun elevation at illuminated node (deg)")
    ax.set_ylabel("acceptable WAC EDR count")
    ax.set_title(f"Sun elevation vs. acceptable EDR count, {period_start.date()}–{period_end.date()}")
    fig.tight_layout()


def _underline_segments(
    start_lon: float, start_y: float, end_lon: float, end_y: float
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Segments for a line from `(start_lon, start_y)` to `(end_lon, end_y)`, broken at the +/-180
    wraparound rather than drawn as one line straight across the plot.

    :param start_lon: Start longitude, degrees.
    :param start_y: Start y-value.
    :param end_lon: End longitude, degrees.
    :param end_y: End y-value.
    :returns: One or two line segments, each `((x0, y0), (x1, y1))`.
    """
    # See `plot_illuminated_node_scatter`'s own trailing comment for why this exists.
    end_lon_unwrapped = illumination.unwrap_relative_deg(start_lon, end_lon)
    if end_lon_unwrapped == start_lon:
        return [((start_lon, start_y), (end_lon, end_y))]

    boundary = 180.0 if end_lon_unwrapped > start_lon else -180.0
    crosses = min(start_lon, end_lon_unwrapped) <= boundary <= max(start_lon, end_lon_unwrapped)
    if not crosses:
        return [((start_lon, start_y), (end_lon, end_y))]

    frac = (boundary - start_lon) / (end_lon_unwrapped - start_lon)
    y_at_boundary = start_y + frac * (end_y - start_y)
    return [
        ((start_lon, start_y), (boundary, y_at_boundary)),
        ((-boundary, y_at_boundary), (end_lon, end_y)),
    ]


def plot_illuminated_node_scatter(
    orbits_df: pd.DataFrame,
    period_start: datetime,
    period_end: datetime,
    selected_datasets: pd.DataFrame | None = None,
    figsize: tuple[float, float] = (20, 6),
    underline_offset_deg: float = 4.0,
    dataset_group_size: int = 10,
    dataset_group_colors: tuple[str, str] = ("#000000", "#808080"),
) -> None:
    """One marker per orbit (`notebooks/select_datasets.py`): x = illuminated-node longitude, y =
    solar hour angle at that node.

    :param orbits_df: `dataset_selection.find_orbits`'s rows.
    :param period_start: Period start (for the title).
    :param period_end: Period end (for the title).
    :param selected_datasets: `dataset_selection.select_diverse_datasets`'s output; if given, each
        selected dataset gets an "underline" marking its span.
    :param figsize: Figure size.
    :param underline_offset_deg: Vertical offset of the underline below the orbit markers.
    :param dataset_group_size: Number of `selected_datasets` picks (by pick order) in the first
        underline color group; the rest use the second.
    :param dataset_group_colors: `(first_group_color, rest_color)`.
    """
    # Circles colored by acceptable-EDR count (viridis -- perceptually uniform and varies in hue as
    # well as lightness, easier to read a value back off a marker's color than a ramp that only
    # varies in lightness, and its dark-purple low end is never invisible against the white figure
    # background either); a red X overrides the circle for any orbit flagged as containing a
    # maneuver. No connecting line between orbits -- markers stay individually resolvable at ~13
    # orbits/day, so consecutive orbits are already easy to associate visually without one.
    #
    # Each underline reads as marking a dataset's own span on the plot rather than sitting on top of
    # (and hiding) the markers themselves. Broken at the +/-180 wraparound via
    # `illumination.unwrap_relative_deg` (`_underline_segments`) -- draw in an unwrapped coordinate,
    # then clip/split wherever that crosses +/-180, rather than drawing one spurious line across the
    # whole plot. `dataset_group_colors` (black/medium-grey by default) was chosen to read
    # unambiguously at a glance and stay clearly distinct from viridis's own purple/teal/green/yellow
    # gamut and from the red maneuver X's (an earlier orange/magenta pairing was hard to tell apart).
    x = orbits_df["illum_lon_deg"].to_numpy()
    y = orbits_df["hour_angle_deg"].to_numpy()

    fig, ax = plt.subplots(figsize=figsize)

    no_maneuver = ~orbits_df["has_maneuver"].to_numpy()
    scatter = ax.scatter(
        x[no_maneuver],
        y[no_maneuver],
        c=orbits_df["acceptable_edr_count"].to_numpy()[no_maneuver],
        cmap="viridis",
        s=6,
        linewidths=0,
        zorder=2,
    )
    count_cbar = fig.colorbar(scatter, ax=ax, orientation="horizontal", pad=0.14, aspect=60, shrink=0.6)
    count_cbar.set_label("acceptable WAC EDR count (marker)")

    maneuver = orbits_df["has_maneuver"].to_numpy()
    ax.scatter(
        x[maneuver], y[maneuver], marker="x", color="#e34948", s=55, linewidths=1.8, zorder=4, label="maneuver in orbit"
    )

    if selected_datasets is not None:
        group_labeled = [False, False]
        n = len(selected_datasets)
        for i, row in selected_datasets.iterrows():
            start_orbit = orbits_df.iloc[row["start_idx"]]
            end_orbit = orbits_df.iloc[row["end_idx"]]
            group = 0 if i < dataset_group_size else 1
            label = None
            if not group_labeled[group]:
                label = f"dataset 1-{dataset_group_size}" if group == 0 else f"dataset {dataset_group_size + 1}-{n}"
                group_labeled[group] = True
            for (x0, y0), (x1, y1) in _underline_segments(
                start_orbit["illum_lon_deg"],
                start_orbit["hour_angle_deg"] - underline_offset_deg,
                end_orbit["illum_lon_deg"],
                end_orbit["hour_angle_deg"] - underline_offset_deg,
            ):
                ax.plot([x0, x1], [y0, y1], color=dataset_group_colors[group], linewidth=2.5, zorder=3, label=label)
                label = None  # only the first segment of the first dataset in each group gets a legend entry

    ax.set_xlim(-180, 180)
    ax.set_ylim(-90, 90)
    ax.set_xticks(np.arange(-180, 181, 45))
    ax.set_yticks(np.arange(-90, 91, 30))
    ax.set_xlabel("longitude of illuminated node (deg)")
    ax.set_ylabel("solar hour angle at illuminated node (deg)")
    ax.set_title(f"LRO orbits, {period_start.date()}–{period_end.date()}: illuminated-node geometry")
    ax.axhline(0, color="0.85", lw=0.8, zorder=0)
    ax.legend(loc="upper right")
    fig.tight_layout()
