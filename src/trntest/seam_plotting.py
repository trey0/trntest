"""Figures and tables for `seam_probes` results: the index notebooks (`reflectance_seams.ipynb`) and
the per-probe reports rendered from `notebooks/seam_probe_template.py`.

Every figure is a single full-width panel, so a notebook viewer shows it at a usable size. Each
`plot_*` function closes its figure before returning it, so a bare last-expression call displays it
exactly once.
"""

import html
from collections.abc import Callable, Mapping

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from IPython.display import HTML, Markdown, display
from matplotlib.colors import ListedColormap

from trntest import seam_probes
from trntest.seam_probes import ProbeResult, SeamThresholds

_NAN_COLOR = "red"
_SEAM_COLORS = [f"C{i}" for i in range(10)]  # matplotlib's default color cycle
_WIDTH_IN = 12


def _stretch(array: np.ndarray) -> tuple[float, float]:
    finite = array[np.isfinite(array)]
    if finite.size == 0:
        return 0.0, 1.0
    lo, hi = np.percentile(finite, [1, 99])
    return float(lo), float(hi)


def _show_with_nan(ax, array: np.ndarray, extent=None, cmap="gray", vmin=None, vmax=None, aspect="equal"):
    if vmin is None or vmax is None:
        vmin, vmax = _stretch(array)
    image = ax.imshow(array, cmap=cmap, vmin=vmin, vmax=vmax, extent=extent, interpolation="nearest", aspect=aspect)
    nan = np.isnan(array)
    if nan.any():
        ax.imshow(
            np.where(nan, 1.0, np.nan),
            cmap=ListedColormap([_NAN_COLOR]),
            vmin=0,
            vmax=1,
            extent=extent,
            interpolation="nearest",
            aspect=aspect,
        )
    return image


def _value_label(result: ProbeResult) -> str:
    return f"{result.value_name} ({result.value_units})"


def _gradient_units(result: ProbeResult) -> str:
    return "per px" if result.value_units == "unitless" else f"{result.value_units} / px"


def _label_image_axes(ax):
    ax.set_xlabel("column (px; east to the right)")
    ax.set_ylabel("row (px; north up)")


def _draw_seams(ax, result: ProbeResult, rows: slice, cols: slice, margin: float = 0.06):
    # Only near the panel's edges, like crop marks: a line drawn over the seam itself would hide a
    # 1-px artifact on it.
    for i, (name, distance) in enumerate(result.distances_px.items()):
        color = _SEAM_COLORS[i % len(_SEAM_COLORS)]
        window = distance[rows, cols]
        if not (np.nanmin(window) < 0 < np.nanmax(window)):
            continue
        h, w = window.shape
        my, mx = max(2, round(margin * h)), max(2, round(margin * w))
        interior = np.zeros(window.shape, dtype=bool)
        interior[my:-my, mx:-mx] = True
        window = np.where(interior, np.nan, window)
        y0, x0 = rows.start or 0, cols.start or 0
        yy, xx = np.mgrid[y0 : y0 + h, x0 : x0 + w]
        ax.contour(xx, yy, window, levels=[0], colors=[color], linewidths=2.5)
        ax.plot([], [], color=color, linewidth=2.5, label=name)
    if result.distances_px:
        ax.legend(loc="upper right", fontsize=9, framealpha=0.8)


def _finish(fig):
    plt.close(fig)
    return fig


def _zoom_window(result: ProbeResult, half_px: int = 150) -> tuple[slice, slice]:
    h, w = result.array.shape
    return slice(h // 2 - half_px, h // 2 + half_px), slice(w // 2 - half_px, w // 2 + half_px)


def plot_probe_render(result: ProbeResult):
    """The whole rendered AOI (north up), seams marked at the panel edges, `NaN` pixels in red, and
    the `plot_probe_zoom` window outlined in yellow.

    :param result: `seam_probes.run_probe`'s output.
    :returns: The `Figure`.
    """
    h, w = result.array.shape
    fig, ax = plt.subplots(figsize=(_WIDTH_IN, _WIDTH_IN * 0.9), constrained_layout=True)
    image = _show_with_nan(ax, result.array)
    fig.colorbar(image, ax=ax, shrink=0.8, label=_value_label(result) + " (1st-99th percentile stretch)")
    _draw_seams(ax, result, slice(0, h), slice(0, w))
    rows, cols = _zoom_window(result)
    ax.add_patch(
        plt.Rectangle(
            (cols.start - 0.5, rows.start - 0.5),
            cols.stop - cols.start,
            rows.stop - rows.start,
            fill=False,
            edgecolor="yellow",
        )
    )
    _label_image_axes(ax)
    ax.set_title(f"{result.grid.probe.name}: {w}x{h} px at {result.grid.gsd_m:g} m/px, {result.nan_count} NaN px")
    return _finish(fig)


def plot_probe_zoom(result: ProbeResult):
    """A 300x300 px window on the AOI center, where the seams meet, at the full render's stretch.

    :param result: `seam_probes.run_probe`'s output.
    :returns: The `Figure`.
    """
    rows, cols = _zoom_window(result)
    vmin, vmax = _stretch(result.array)
    fig, ax = plt.subplots(figsize=(_WIDTH_IN, _WIDTH_IN * 0.9), constrained_layout=True)
    extent = (cols.start - 0.5, cols.stop - 0.5, rows.stop - 0.5, rows.start - 0.5)
    image = _show_with_nan(ax, result.array[rows, cols], extent=extent, vmin=vmin, vmax=vmax)
    fig.colorbar(image, ax=ax, shrink=0.8, label=_value_label(result) + " (same stretch as the full render)")
    _draw_seams(ax, result, rows, cols)
    _label_image_axes(ax)
    ax.set_title(f"{result.grid.probe.name}: center zoom ({rows.stop - rows.start} px)")
    return _finish(fig)


def _profile_and_metrics(result: ProbeResult, seam_name: str):
    profile = next(p for p in result.profiles if p.seam.name == seam_name)
    metrics = next(m for m in result.metrics if m.seam == seam_name and not m.control)
    return profile, metrics


def _mark_bands(ax, seam: seam_probes.Seam):
    near = seam_probes.NEAR_SEAM_PX
    lo, hi = seam_probes.REFERENCE_PX
    ax.axvspan(-near, near, color="orange", alpha=0.15, label=f"at seam (within {near} px)")
    for sign in (-1, 1):
        ax.axvspan(sign * lo, sign * hi, color="0.5", alpha=0.08, label="reference bins" if sign < 0 else None)
    ax.axvline(0, color="orange", linewidth=0.8)
    negative, positive = seam.sides
    ax.set_xlabel(f"signed distance from seam (px; - = {negative}, + = {positive})")


def plot_seam_profile(result: ProbeResult, seam_name: str):
    """Median value per 1-px distance bin across one seam, with its interquartile range, each side's
    trend fit to its reference bins (dashed, extrapolated to the seam) and the `NaN` fraction (red, if
    any), with `step_rel`/`spike_rel`/`nan_near` defined in the title.

    :param result: `seam_probes.run_probe`'s output.
    :param seam_name: One of `result.distances_px`'s keys.
    :returns: The `Figure`.
    """
    profile, m = _profile_and_metrics(result, seam_name)
    negative, positive = profile.seam.sides
    d = profile.distance_px
    fig, ax = plt.subplots(figsize=(_WIDTH_IN, 5.5), constrained_layout=True)
    _mark_bands(ax, profile.seam)
    ax.fill_between(d, profile.p25, profile.p75, color="tab:blue", alpha=0.2, label="interquartile range")
    ax.plot(d, profile.median, color="tab:blue", marker=".", label="median")
    trend_s, trend_n = seam_probes.reference_trends(profile)
    for trend, sign in ((trend_s, -1), (trend_n, 1)):
        x = np.linspace(0, sign * seam_probes.REFERENCE_PX[1], 20)
        ax.plot(x, trend(x), color="black", linestyle="--", linewidth=1, label="side trend" if sign < 0 else None)
    ax.set_ylabel(_value_label(result))
    ax.set_title(
        f"{seam_name}: profile across the seam\n"
        f"step_rel = {m.step_rel:+.3f}: ({positive} trend - {negative} trend) at the seam / probe median\n"
        f"spike_rel = {m.spike_rel:.3f}: largest |at-seam median - its own side's trend| / probe median\n"
        f"nan_near = {m.nan_near}: NaN pixels within {seam_probes.NEAR_SEAM_PX} px of the seam",
        loc="left",
        fontsize=10,
    )
    ax.legend(loc="upper left", fontsize=9)
    if np.nanmax(profile.nan_fraction) > 0:
        ax_nan = ax.twinx()
        ax_nan.plot(d, profile.nan_fraction, color=_NAN_COLOR, linewidth=1)
        ax_nan.set_ylabel("NaN fraction of the bin's pixels", color=_NAN_COLOR)
        ax_nan.set_ylim(0, max(0.05, float(np.nanmax(profile.nan_fraction)) * 1.1))
    return _finish(fig)


def plot_seam_gradient(result: ProbeResult, seam_name: str):
    """Median gradient magnitude per 1-px distance bin across one seam, with `gradient_ratio` defined
    in the title. A line or a texture change at the seam shows up as a peak.

    :param result: `seam_probes.run_probe`'s output.
    :param seam_name: One of `result.distances_px`'s keys.
    :returns: The `Figure`.
    """
    profile, m = _profile_and_metrics(result, seam_name)
    fig, ax = plt.subplots(figsize=(_WIDTH_IN, 4.5), constrained_layout=True)
    _mark_bands(ax, profile.seam)
    ax.plot(profile.distance_px, profile.gradient_median, color="tab:green", marker=".", label="median |gradient|")
    ax.set_ylabel(f"median |gradient| ({_gradient_units(result)})")
    ax.set_title(
        f"{seam_name}: gradient magnitude across the seam\n"
        f"gradient_ratio = {m.gradient_ratio:.2f}: largest at-seam median |gradient| / median over the reference bins",
        loc="left",
        fontsize=10,
    )
    ax.legend(loc="upper left", fontsize=9)
    return _finish(fig)


def plot_seam_strip(result: ProbeResult, seam_name: str):
    """The band around one seam, straightened so the seam runs down the middle, each cell's mean
    divided by its row's median (`seam_probes.seam_strip`). Shows where along the seam an artifact
    sits, which the profiles average away.

    :param result: `seam_probes.run_probe`'s output.
    :param seam_name: One of `result.distances_px`'s keys.
    :returns: The `Figure`.
    """
    strip = seam_probes.seam_strip(result, seam_name)
    contrast = strip.contrast()
    spread = float(np.nanpercentile(np.abs(contrast - 1), 99)) if np.isfinite(contrast).any() else 0.1
    d = strip.distance_px
    negative, positive = strip.seam.sides
    bin_px = strip.along_px[1] - strip.along_px[0] if len(strip.along_px) > 1 else 1
    fig, ax = plt.subplots(figsize=(_WIDTH_IN, 11), constrained_layout=True)
    extent = (d[0] - 0.5, d[-1] + 0.5, strip.along_px[-1] + bin_px / 2, strip.along_px[0] - bin_px / 2)
    image = _show_with_nan(ax, contrast, extent=extent, cmap="RdBu_r", vmin=1 - spread, vmax=1 + spread, aspect="auto")
    fig.colorbar(image, ax=ax, shrink=0.6, label=f"{result.value_name} / median of its row (unitless)")
    ax.invert_yaxis()
    ax.axvline(0, color="black", linewidth=0.5, linestyle=":")
    ax.set_title(
        f"{seam_name}: straightened strip ({bin_px:g} px rows, 1 px columns, NaN in red)", loc="left", fontsize=10
    )
    ax.set_xlabel(f"signed distance from seam (px; - = {negative}, + = {positive})")
    ax.set_ylabel(f"distance along seam from AOI center (px; + = {strip.seam.along_direction})")
    return _finish(fig)


def show_seam_diagnostics(result: ProbeResult) -> None:
    """For each seam in `result`: a heading, then `plot_seam_profile`, `plot_seam_gradient` and
    `plot_seam_strip`, one under another.

    :param result: `seam_probes.run_probe`'s output.
    """
    for profile in result.profiles:
        name = profile.seam.name
        display(Markdown(f"#### {name}"))
        for plot in (plot_seam_profile, plot_seam_gradient, plot_seam_strip):
            display(plot(result, name))


def _over_limits(row, limits: SeamThresholds) -> dict[str, bool]:
    return {
        "nan_near": row.nan_near > limits.nan_near_max,
        "step_rel": not abs(row.step_rel) <= limits.abs_step_rel_max,
        "spike_rel": not row.spike_rel <= limits.spike_rel_max,
        "gradient_ratio": not row.gradient_ratio <= limits.gradient_ratio_max,
    }


def show_probe_summary(result: ProbeResult, thresholds: Callable[[str], SeamThresholds]) -> None:
    """The probe's center, `NaN` count and tiles, then its seams' metrics with their limits, over-limit
    values in bold red.

    :param result: `seam_probes.run_probe`'s output.
    :param thresholds: Seam name -> limits.
    """
    probe = result.grid.probe
    display(
        Markdown(
            f"**{probe.name}**: center ({probe.center_lat_deg:g} N, {probe.center_lon_deg:g} E), "
            f"{result.nan_count} NaN px, tiles: {', '.join(f'`{t}`' for t in result.source_ids)}"
        )
    )
    table = seam_probes.metrics_table([result])
    real = table[~table.control].drop(columns=["probe", "control"]).reset_index(drop=True)
    limits = [thresholds(seam) for seam in real.seam]
    over = [_over_limits(row, lim) for row, lim in zip(real.itertuples(), limits, strict=True)]
    real["limits (nan / |step| / spike / gradient)"] = [
        f"{t.nan_near_max} / {t.abs_step_rel_max:g} / {t.spike_rel_max:g} / {t.gradient_ratio_max:g}" for t in limits
    ]

    def highlight(column: pd.Series) -> list[str]:
        flags = [o.get(str(column.name), False) for o in over]
        return ["color: red; font-weight: bold" if flag else "" for flag in flags]

    display(real.style.apply(highlight).format(precision=4).hide(axis="index"))


def show_health_table(health: pd.DataFrame, links: Mapping[str, str] | None = None) -> None:
    """`seam_probes.probe_health_table`'s output as an HTML table, `FAIL` rows in red, each probe name
    linking to its report notebook if `links` has one.

    :param health: `seam_probes.probe_health_table`'s output.
    :param links: Probe name -> relative link (`seam_probes.report_links`).
    """
    links = links or {}
    shown = health.copy()
    shown["probe"] = [
        f'<a href="{html.escape(links[p])}">{html.escape(p)}</a>' if p in links else html.escape(p) for p in shown.probe
    ]

    def color_failures(row: pd.Series) -> list[str]:
        return ["color: red; font-weight: bold" if row.status == "FAIL" else ""] * len(row)

    styled = shown.style.apply(color_failures, axis=1).format(precision=3).hide(axis="index")
    display(HTML(styled.to_html(escape=False)))


def plot_metrics_vs_controls(table: pd.DataFrame, thresholds: Callable[[str], SeamThresholds] | None = None):
    """Each metric per probe, one panel under another: control lines as grey dots, real seams as colored
    markers, pass limits as short bars (if `thresholds` is given).

    :param table: `seam_probes.metrics_table`'s output.
    :param thresholds: Seam name -> limits, e.g. `seam_probes.wac_emp_thresholds`.
    :returns: The `Figure`.
    """
    probes = list(dict.fromkeys(table.probe))
    x_of = {p: i for i, p in enumerate(probes)}
    real = table[~table.control]
    seam_names = list(dict.fromkeys(real.seam))
    color_of = {s: _SEAM_COLORS[i % len(_SEAM_COLORS)] for i, s in enumerate(seam_names)}
    panels = (
        ("|step_rel| (unitless)", lambda t: t.step_rel.abs(), "abs_step_rel_max"),
        ("spike_rel (unitless)", lambda t: t.spike_rel, "spike_rel_max"),
        ("gradient_ratio (unitless)", lambda t: t.gradient_ratio, "gradient_ratio_max"),
    )
    fig, axes = plt.subplots(
        len(panels), 1, figsize=(_WIDTH_IN, 3.2 * len(panels)), sharex=True, constrained_layout=True
    )
    controls = table[table.control]
    rng = np.random.default_rng(0)
    for ax, (label, value, limit_field) in zip(axes, panels, strict=True):
        jitter = rng.uniform(-0.15, 0.15, len(controls))
        ax.scatter(controls.probe.map(x_of) + jitter, value(controls), color="0.6", s=10, label="control lines")
        for seam in seam_names:
            rows = real[real.seam == seam]
            ax.scatter(rows.probe.map(x_of), value(rows), color=color_of[seam], s=60, marker="D", label=seam)
            if thresholds is not None:
                limit = getattr(thresholds(seam), limit_field)
                for x in rows.probe.map(x_of):
                    ax.hlines(limit, x - 0.3, x + 0.3, color=color_of[seam], linewidth=1)
        ax.set_ylabel(label)
    axes[0].legend(ncol=4, fontsize=8, loc="lower left", bbox_to_anchor=(0, 1.02))
    axes[-1].set_xticks(range(len(probes)))
    axes[-1].set_xticklabels(probes, rotation=45)
    axes[-1].set_xlabel("probe")
    return _finish(fig)
