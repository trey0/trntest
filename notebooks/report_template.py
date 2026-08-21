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
# # Per-entry report

# %%
import trntest

entry = trntest.report.load_entry("{{ dataset_folder }}", "{{ edr_product }}")

# %%
trntest.report.summary(entry)

# %%
trntest.report.hillshade(entry)
