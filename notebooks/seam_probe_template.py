# ---
# jupyter:
#   jupytext:
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.19.5
#   kernelspec:
#     display_name: Python 3 (ipykernel)
#     language: python
#     name: python3
# ---

# %% [markdown]
# ### Seam probe {{ probe_name }} ({{ source }})
#
# One probe AOI from the `{{ source }}` seam index notebook, which explains the metrics. Rendered by
# `seam_probes.write_probe_reports` from `notebooks/seam_probe_template.py`.

# %%
from trntest import seam_plotting, seam_probes; result = seam_probes.load_probe("{{ source }}", "{{ probe_name }}")  # noqa: E702, I001  # fmt: skip
seam_plotting.show_probe_summary(result, seam_probes.SOURCES["{{ source }}"].thresholds)

# %% [markdown]
# The whole AOI. Colored ticks at the edges mark where each seam crosses; the seam itself is left
# undrawn so a one-pixel artifact on it stays visible. Red pixels are `NaN`. The yellow box is the
# zoom below.

# %%
seam_plotting.plot_probe_render(result)

# %% [markdown]
# The center, where the seams meet, at full resolution.

# %%
seam_plotting.plot_probe_zoom(result)

# %% [markdown]
# For each seam: the profile across it (median, interquartile range, each side's reference trend
# dashed, `NaN` fraction in red if any; the orange band is "at the seam"), the gradient-magnitude
# profile (a line or texture change shows as a peak), and the straightened strip (the ±40 px band
# with the seam down the middle, each row divided by its median), which shows where along the seam
# an artifact sits.

# %%
seam_plotting.show_seam_diagnostics(result)
