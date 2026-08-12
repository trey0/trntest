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
# # Dataset selection: picking a real, illuminated LROC WAC EDR to test with
#
# Queries the real LROC catalog (via the PDS Geosciences Node ODE REST API) for a multi-orbit
# window with favorable illumination geometry and good WAC data availability, and picks one real
# EDR product to use as this demo's test image -- see `../docs/plan.md`/`trntest.dataset` for the
# full selection approach (SPICE-derived orbit/illumination geometry, throttling, per-orbit
# coverage).
#
# This notebook's only job is selection: it writes the full candidate table to a small, checked-in
# CSV (`dataset_manifest.csv`, alongside this notebook) and stops -- no DEM/ortho fetch, no
# `sat_sim` render. `../notebooks/image_generation.ipynb` reads that checked-in file and does the
# rest, so the two notebooks never need to run in the same session: rerun this one and commit an
# updated `dataset_manifest.csv` to change which real image the demo renders.

# %%
import trntest

session = trntest.Session()

# %% [markdown]
# ## Catalog-driven selection
#
# `session.select_dataset()` returns a throttled, illumination-filtered list of real EDR
# candidates (one row per image) for the chosen search window, including everything needed to
# relocate and pose each one later: `edr_volume`/`edr_subdir`/`edr_doy`/`edr_product`,
# `cdr_volume`/`cdr_product`, and `start_frame`.

# %%
images = session.select_dataset(max_search_days=7)
images

# %% [markdown]
# ## Selected image + checked-in manifest
#
# `image_generation.ipynb` only ever renders the first row of the manifest
# (`generate_dataset(images, limit=1)`), so that's the "selected" image below. Writing the *full*
# candidate table (not just this one row) to `dataset_manifest.csv` also documents what the rest
# of this search window looked like.

# %%
selected = images.iloc[0]
print(f"Selected EDR product: {selected['edr_product']}")
print(
    f"  edr_volume={selected['edr_volume']} edr_subdir={selected['edr_subdir']} "
    f"edr_doy={selected['edr_doy']} start_frame={selected['start_frame']}"
)

manifest_path = "dataset_manifest.csv"
trntest.write_manifest(images, manifest_path)
print(f"Wrote {len(images)} candidate row(s) to {manifest_path}")
