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
# ### {{ dataset_name }} -- entry {{ entry_index }}: {{ product_id }}

# %%
import trntest; entry = trntest.report.load_entry("{{ dataset_folder }}", "{{ entry_index }}")  # noqa: E702, I001  # fmt: skip
trntest.report.summary(entry)

# %%
trntest.report.reproject_overlay(entry)

# %%
trntest.report.reproject_zoom_blink(entry)
