# ---
# jupyter:
#   jupytext:
#     formats: notebooks//ipynb,notebooks//py:percent
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
# # Seams in the WAC_EMP reflectance mosaic
#
# `hillshade`'s texture is WAC_EMP reflectance, mosaicked from ten archive tiles onto each entry's
# local grid (`ortho_wac_emp.reproject_wac_emp_reflectance_to_local_grid`). The tiles meet along
# three kinds of seam:
#
# - **±60° latitude**: equirectangular tiles meet the polar-stereographic tile of that hemisphere.
# - **The equator**: north and south equirectangular tiles meet.
# - **0°, 90°, 180°, 270° longitude**: neighboring equirectangular tiles meet, between 60°S and 60°N.
#
# A seam can go wrong in several ways: a coverage gap (`NaN`), a step (the two sides disagree), or a
# line (a spike in brightness or texture right at the seam, with both sides otherwise matching).
# This notebook renders a synthetic 200 km square centered on every point where three or four tiles
# meet (`seam_probes.WAC_EMP_PROBES`), through the same code path the `hillshade` generator uses, and
# measures each seam crossing it.
#
# `tests/test_reflectance_seams.py` runs the same probes and checks the same numbers against the same
# limits (`seam_probes.wac_emp_thresholds`). When that test fails, this notebook shows why.

# %%
from IPython.display import display

from trntest import seam_plotting, seam_probes

SOURCE = "reflectance"
source = seam_probes.SOURCES[SOURCE]
results = seam_probes.run_source(SOURCE)
table = seam_probes.metrics_table(results)
health = seam_probes.probe_health_table(results, source.thresholds)

# %% [markdown]
# ## Metrics
#
# For each seam, the metrics come from a *seam-normal profile*: pixels binned by signed distance
# from the seam (1 px bins, positive to the north or east), away from the probe's other seam.
#
# - `nan_near`: `NaN` pixels within 3 px of the seam. A coverage gap along a seam is a bug.
# - `step`: the difference between the two sides, each extrapolated to the seam from a straight
#   line fit 6-30 px out, relative to the probe's median reflectance.
# - `spike`: the largest deviation of a near-seam bin (within 3 px) from its side's line,
#   relative to the median. A bright or dark line shows up here; so does the blended edge of a step.
# - `gradient_ratio`: the largest near-seam median gradient magnitude, divided by the reference
#   bins'. A line or a texture change raises it even when the mean doesn't move.
#
# Terrain alone moves every one of these. To show by how much, the same metrics are computed on
# *control lines*: lines parallel to each seam, 80-320 px away, inside a single tile. The plot shows
# them in grey behind each real seam, with the test's pass limit as a short bar.

# %%
seam_plotting.plot_metrics_vs_controls(table, source.thresholds)

# %% [markdown]
# ## Health by probe
#
# One row per probe: its worst value of each metric over its seams; `limit_use`, how close its
# closest metric is to that metric's limit (1.0 = at the limit; `gradient_ratio` counts from 1), and
# which seam and metric that is; and pass/fail, which is what the test checks.
#
# Each probe name links to a report notebook with its renders, profiles and straightened seam
# strips. The reports aren't committed: running this notebook writes them under
# `output/seam_probes/reflectance/`, so the links only work in a checkout where it has run.

# %%
links = seam_probes.report_links(seam_probes.write_probe_reports(SOURCE))
seam_plotting.show_health_table(health, links)

# %% [markdown]
# Every seam, for reference:

# %%
display(table[~table.control].drop(columns="control").round(4))
