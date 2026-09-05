"""SFS-validation-only comparison plots (`sfs_validation.py`'s own notebook cells) -- split out of
`plotting.py` since neither figure is needed by the generator-comparison/report path, only the
independent ASP `sfs` forward-render cross-check.
"""

import matplotlib.pyplot as plt
import numpy as np

from trntest.plotting import cellsize_m, normalize_to_median, open_raster_dataarray, robust_median


def plot_sfs_comparison(real_wac_path, ours_path, sfs_sim_intensity_path, title: str | None = None):
    """WAC crop vs. this project's own Hapke hillshade vs. ASP `sfs`'s independent forward-render
    (`sfs_validation.run_sfs_forward_render`), each independently normalized to its own median.

    :param real_wac_path: WAC crop raster path.
    :param ours_path: This project's Hapke hillshade raster path.
    :param sfs_sim_intensity_path: `sfs`'s forward-render intensity raster path, already
        coverage-masked (`sfs_validation.mask_sfs_uncovered`).
    :param title: Optional figure title.
    :returns: The `Figure`.
    """
    # Each panel independently normalized to its own valid-pixel median = 1.0 (`plotting.robust_median`/
    # `plotting.normalize_to_median`, the same brightness-normalization technique
    # `plotting.compute_brightness_matched_diff` uses), not `ours`/`sim` matched to `real`'s absolute
    # level -- a shared vmax (the largest of the three sides' own post-normalization percentile) then
    # protects whichever panel needed the biggest correction from oversaturating, not just `real`.
    # `sfs_sim_intensity_path` must already be coverage-masked -- `sfs`'s own literal-`0.0` "outside
    # camera coverage" convention would otherwise dominate the median and wash out the normalization
    # entirely, the same failure mode `compute_brightness_matched_diff`'s own docstring warns a
    # mismatched-extent raster can cause.
    real = open_raster_dataarray(real_wac_path)
    tolerance = cellsize_m(real) / 2.0
    ours = open_raster_dataarray(ours_path).reindex_like(real, method="nearest", tolerance=tolerance)
    sim = open_raster_dataarray(sfs_sim_intensity_path).reindex_like(real, method="nearest", tolerance=tolerance)

    real_norm = normalize_to_median(real, robust_median(real.values))
    ours_norm = normalize_to_median(ours, robust_median(ours.values))
    sim_norm = normalize_to_median(sim, robust_median(sim.values))
    vmax = max(
        np.nanpercentile(real_norm.values, 99.5),
        np.nanpercentile(ours_norm.values, 99.5),
        np.nanpercentile(sim_norm.values, 99.5),
    )

    fig, axes = plt.subplots(1, 3, figsize=(15, 6))
    axes[0].imshow(real_norm.values, cmap="gray", vmin=0, vmax=vmax)
    axes[0].set_title("Real WAC (cam2map)")
    axes[1].imshow(ours_norm.values, cmap="gray", vmin=0, vmax=vmax)
    axes[1].set_title("Our Hapke hillshade\n(median-normalized)")
    axes[2].imshow(sim_norm.values, cmap="gray", vmin=0, vmax=vmax)
    axes[2].set_title("ASP sfs forward-render\n(median-normalized)")
    for ax in axes:
        ax.axis("off")
    fig.tight_layout()
    if title:
        fig.suptitle(title, y=1.05)
    return fig


def plot_incidence_validation(incidence_sfs_deg: np.ndarray, incidence_ours_deg: np.ndarray, title: str | None = None):
    """3-panel comparison for `sfs_validation`'s Lambertian-mode incidence cross-check: `sfs`'s own
    independently ray-traced incidence field, `hapke.real_geometry_photometric_angles`'s own
    field, and their difference.

    :param incidence_sfs_deg: `sfs`'s incidence field, degrees, NaN outside camera coverage
        (see `incidence_deg_from_lambertian_sim_intensity`).
    :param incidence_ours_deg: This project's incidence field, degrees, same shape.
    :param title: Optional figure title.
    :returns: The `Figure`.
    """
    # Both plain arrays, not raster paths, since both are already in-memory by the time a caller has
    # something to compare.
    diff_deg = incidence_sfs_deg - incidence_ours_deg
    vmin = float(np.nanmin([np.nanmin(incidence_sfs_deg), np.nanmin(incidence_ours_deg)]))
    vmax = float(np.nanmax([np.nanmax(incidence_sfs_deg), np.nanmax(incidence_ours_deg)]))

    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    im0 = axes[0].imshow(incidence_sfs_deg, cmap="viridis", vmin=vmin, vmax=vmax)
    axes[0].set_title("incidence, from sfs\n(Lambertian-mode inversion)")
    fig.colorbar(im0, ax=axes[0], fraction=0.046)
    im1 = axes[1].imshow(incidence_ours_deg, cmap="viridis", vmin=vmin, vmax=vmax)
    axes[1].set_title("incidence, ours\n(real_geometry_photometric_angles)")
    fig.colorbar(im1, ax=axes[1], fraction=0.046)
    im2 = axes[2].imshow(diff_deg, cmap="RdBu_r", vmin=-0.5, vmax=0.5)
    axes[2].set_title("sfs - ours (deg)")
    fig.colorbar(im2, ax=axes[2], fraction=0.046)
    for ax in axes:
        ax.axis("off")
    fig.tight_layout()
    if title:
        fig.suptitle(title, y=1.05)
    return fig
