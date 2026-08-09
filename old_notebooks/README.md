# Archived investigation notebooks

Throwaway diagnostic notebooks, kept for their real diagnostic value (the plots/output already
captured, and the reasoning trail) but **not maintained going forward** — unlike the two real,
tracked notebook pairs in `../notebooks/`, these won't be kept in sync with the pipeline as it
changes, and `trntest-lint`'s notebook pairing/sync checks don't scan this directory (see
`src/trntest/_lint.py` — it only checks paths starting with `notebooks/`).

For each notebook, the **`.ipynb` is the source of truth** — it carries the real, already-executed
plots and output from when it was actually run, not just code. The paired `.py` was derived *from*
that `.ipynb` (`jupytext --to py:percent <notebook>.ipynb`), the reverse of this repo's normal
`.py`-is-source-of-truth convention for `../notebooks/`. Neither half will be re-executed or
re-synced; treat both as a frozen record, not a live notebook.

Full narrative for both: `docs/history.md`'s Phase 26 entry (the DEM stripe/crosshatch artifact
investigation — root cause, the false leads and why each one was ruled out, and the final fix).

## `stripe_debug.py` / `.ipynb`

The primary diagnostic notebook for the whole investigation. Built an FFT/periodicity-based toolkit
(`periodicity_report`, `power_at_freq_and_angle`, `db_above_trend_at_freq`, `annotated_fft_plot`) to
measure and visualize periodic artifacts in the synthetic render's hillshade, quantitatively rather
than by eye. Chronicles the full arc against Lunaserv's DTM WMS layer: the original near-Nyquist
server-side resampling artifact and its fix (native-CRS fetch + local reprojection), the *second*,
axis-aligned crosshatch that survived that fix, and everything tried against it (resampling kernel
choices, GDAL's approximate-transformer `tolerance`, computing the hillshade near-native-resolution
before the final upsample, a frequency-targeted notch filter, a live ppd sweep) before it was
confirmed baked into Lunaserv's own native tile and not fixable client-side.

## `astropedia_check.py` / `.ipynb`

The notebook that settled the question of whether USGS Astropedia's flat-file GLD100 distribution
is a clean alternative DEM source. Pulls the real AOI directly from
`Lunar_LRO_WAC_GLD100_DTM_79S79N_100m_v1.1.tif` (verified genuine 100 m/px, 79°N–79°S coverage),
reprojects it into the same per-camera local Orthographic working grid as everything else in
`stripe_debug.py`, and computes a directly-comparable hillshade. This is where the user directly
confirmed (both by inspecting the real plots and via the frequency-domain numbers) that Astropedia's
data has none of Lunaserv's artifact — the basis for the actual fix now in `src/trntest/lunaserv.py`
(`fetch_dem_astropedia`/`reproject_astropedia_elevation_to_local_grid`).
