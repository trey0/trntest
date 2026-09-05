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
# # Dataset selection: which orbits/epochs make good TRN-OD test data?
#
# Picks *orbits*, not single images -- which multi-day spans of consecutive orbits make good,
# maneuver-free test data for TRN-based orbit determination. A different scope than
# `image_generation.py`'s single-EDR demo (a selected dataset here has 100+ entries), so the two
# don't share a dataset folder or wire together; `dataset_manifest.csv` is untouched.
#
# Consecutive images in a selected span need to be maneuver-free in between (see
# `docs/data-sources.md`'s "LRO maneuver detection" section and `src/trntest/maneuver_detection.py`).
#
# The actual selection logic lives in `src/trntest/dataset_selection.py` (orbit-level statistics,
# candidate enumeration, the greedy diversity-selection algorithm) and `src/trntest/plotting.py`
# (both plots below) -- this notebook is just that pipeline's tunable parameters plus the calls
# themselves, so it stays a scannable narrative rather than the place the actual logic lives.
#
# ## The illuminated node
#
# Every LRO orbit has an ascending node and a descending node (where the ground track crosses the
# lunar equator going north/south), ~180 deg apart in longitude. Because they're on opposite sides
# of the Moon, only one of the two is typically sunlit -- we call that one **the illuminated node**,
# picked as whichever of the pair has the higher sun elevation (an arbitrary but reasonable
# tie-break; the rare case where neither/both are lit just means LRO is near the terminator on both
# passes, not a time of particular interest for this dataset anyway).
#
# For each orbit, `dataset_selection.find_orbits` collects:
#
# 1. Solar hour angle at the sub-satellite point under the illuminated node (degrees: -90 =
#    sunrise, 0 = local noon, +90 = sunset).
# 2. Longitude of the illuminated node.
# 3. Number of "acceptable" WAC EDRs in the orbit -- typical nadir mapping-mode acquisitions
#    (low emission angle) with sun elevation above a tunable minimum.
# 4. Whether the orbit contains a propulsive maneuver.
#
# The period of interest here is all of 2019.

# %%
from datetime import datetime

from trntest import TrnTestDataSet, dataset_selection, plotting
from trntest.config import load_config

config = load_config()

PERIOD_START = datetime(2019, 1, 1)
PERIOD_END = datetime(2020, 1, 1)  # exclusive -- orbits must complete strictly before this

MIN_SUN_ELEVATION_DEG = 15.0  # statistic (3)'s "acceptable" sun-elevation floor
MAX_EMISSION_ANGLE_DEG = 15.0  # statistic (3)'s nadir/"typical mapping mode" cutoff
DATASET_LENGTH_ORBITS = 24  # ~2 days
MIN_EDR_COUNT_PER_ORBIT = 3  # an orbit needs at least this many acceptable EDRs to count as acceptable
MIN_CENTER_LONGITUDE_SEPARATION_DEG = 12.0  # how far apart selected datasets must be in center longitude
N_DATASETS = 20  # how many datasets to select

# %% [markdown]
# ## Find every orbit and its illuminated-node statistics (1) and (2)
#
# Furnishes a full year of SPK/CK coverage, so it's the slow one on a cold cache (several minutes --
# one-time cost, cached afterward per `docs/caching.md`).

# %%
orbits_df = dataset_selection.find_orbits(PERIOD_START, PERIOD_END, config)
orbits_df.head()

# %% [markdown]
# ## Acceptable EDR count per orbit (3)

# %%
orbits_df = dataset_selection.add_acceptable_edr_counts(
    orbits_df, PERIOD_START, PERIOD_END, config, MIN_SUN_ELEVATION_DEG, MAX_EMISSION_ANGLE_DEG
)
orbits_df["acceptable_edr_count"].describe()

# %% [markdown]
# ## Maneuver flag (4)

# %%
orbits_df = dataset_selection.add_maneuver_flags(orbits_df, PERIOD_START, PERIOD_END, config)

# %% [markdown]
# ## Sun elevation vs. acceptable EDR count
#
# How much sun elevation at the illuminated node actually buys you, in terms of acceptable-EDR
# yield.

# %%
plotting.plot_sun_elevation_vs_edr_count(orbits_df, PERIOD_START, PERIOD_END)

# %% [markdown]
# ## Picking multiple datasets
#
# A **dataset** is `DATASET_LENGTH_ORBITS` consecutive orbits. It's **acceptable** if every orbit in
# it is acceptable (no maneuver, and at least `MIN_EDR_COUNT_PER_ORBIT` acceptable EDRs) and it
# contains no illuminated-node flip -- the no-flip rule is what makes "center" statistics (the
# circular mean of the first and last orbit's value) actually behave like an average of nearby
# values, rather than splitting across two ~180-degree-apart node longitudes.
#
# Among acceptable datasets, we want to pick exactly `N_DATASETS`, each at least
# `MIN_CENTER_LONGITUDE_SEPARATION_DEG` apart in center longitude from every other one chosen (and
# not sharing any orbits with one already chosen), and jointly as diverse as possible in center
# solar hour angle. "Diverse" is the standard greedy farthest-point/max-min criterion: each new pick
# maximizes its own minimum hour-angle distance to every dataset already chosen. The very first pick
# has nothing to be far from yet, so it's seeded separately: the single most robust acceptable
# dataset (highest minimum per-orbit EDR count across the window). Raises rather than silently
# returning fewer than `N_DATASETS` if the exclusion constraints exhaust the candidate pool first.

# %%
candidates_df = dataset_selection.enumerate_candidate_datasets(
    orbits_df, DATASET_LENGTH_ORBITS, MIN_EDR_COUNT_PER_ORBIT
)

# %%
selected_datasets = dataset_selection.select_diverse_datasets(
    candidates_df, MIN_CENTER_LONGITUDE_SEPARATION_DEG, N_DATASETS
)
selected_datasets[["start_utc", "end_utc", "center_lon_deg", "center_hour_angle_deg", "min_edr_count"]]

# %% [markdown]
# ## The plot
#
# One marker per orbit, colored by acceptable-EDR count; a red X for any orbit with a maneuver; an
# "underline" under each selected dataset's span (black for picks 1-10, grey for 11-20). See
# `plotting.plot_illuminated_node_scatter`'s docstring for the full rationale.

# %%
plotting.plot_illuminated_node_scatter(orbits_df, PERIOD_START, PERIOD_END, selected_datasets)

# %% [markdown]
# ## Resolving one selected dataset into an image list
#
# `selected_datasets` is orbit-level -- a start/end UTC window, no images yet. `dataset_selection.
# resolve_orbit_sequence` turns exactly one selected row into a `TrnTestDataSet`-ready images table
# (`dataset.DATASET_COLUMNS`) -- the same per-candidate EDR-label fetch + SPICE pose
# `dataset.images_for_window` always uses, just windowed to this one selected span, and only after a
# cheap catalog-metadata pre-filter narrows the raw candidate list first.
#
# Resolves only `selected_datasets.iloc[0]` here, not all `N_DATASETS` picks -- resolving the rest
# is a `for` loop away.

# %%
orbit_sequence = selected_datasets.iloc[0]
images = dataset_selection.resolve_orbit_sequence(orbit_sequence, config, MIN_SUN_ELEVATION_DEG, MAX_EMISSION_ANGLE_DEG)
images

# %% [markdown]
# ## Dataset folder (no rendering yet)
#
# `TrnTestDataSet.create()` sets up (or reuses) a self-contained dataset folder -- `manifest.csv`
# (the resolved images above) plus empty `crop`/`hillshade`/`reproject` subfolders, ready for
# `dataset.populate()` later (see `README.md`). Stops here -- no rendering in this
# notebook.
#
# Uses its own `orbit_sequence_dataset` folder, separate from `image_generation.py`'s
# `trn_dataset` -- a different dataset, at a different scale. Also writes `orbit_sequence.csv`
# alongside `manifest.csv`: the one selected-orbit-window row this dataset's images were resolved
# from, kept for debugging/provenance per the design in `README.md`.

# %%
dataset_folder = config.output_dir / "orbit_sequence_dataset"
trn_dataset = TrnTestDataSet.create(dataset_folder, images, config)
orbit_sequence.to_frame().T.to_csv(dataset_folder / "orbit_sequence.csv", index=False)
print(f"Dataset folder ready at {dataset_folder} ({len(trn_dataset)} images)")
