# Dataset selection: maneuver detection (for TRN-OD dataset selection)

Architecture detail alongside `../README.md`'s higher-level map of `dataset_selection.py`/
`candidate_window.images_for_window()`'s orbit-search and candidate-filtering logic.

For TRN-based orbit-determination testing (image-matching as the OD input), a propulsive maneuver
between two dataset images corrupts the OD solve — but there's no known public source for LRO's
flight-dynamics team's own maneuver log (a "small forces file"/SFF equivalent). Two things fill that
gap:

- **Literature: Mesarch, "Long-Term Orbit Operations for the Lunar Reconnaissance Orbiter,"
  AAS-23-234 (2023)**, NTRS
  [20230010952](https://ntrs.nasa.gov/api/citations/20230010952/downloads/Mesarch_LROLongTermOrbit_Preprint_20230727.pdf).
  Key facts from it:
  - LRO's orbit has had **zero stationkeeping maneuvers of any kind since SK34 on 2015-05-04** —
    the mission stopped maintaining a frozen orbit in 2016 and has let it drift ever since (Figure
    19 explicitly labels 2016–2023 "No Maintenance"). Any date from 2016 onward is free of
    stationkeeping/frozen-orbit-reset burns by construction.
  - Table 2 lists every dedicated Eclipse Phasing Maneuver (EPM) date; there's a gap from
    2019-06-24 to 2021-05-03 — no EPM anywhere in H2 2019.
  - The only maneuver type that can still occur in the post-2016 "drift" era is a reaction-wheel
    momentum unload: small (Figure 15: ~0.05–0.3 m/s), every 2-4 weeks, ~302 total since launch.
    Three special phasing maneuvers (Chandrayaan/LCROSS/GRAIL coordination) and the three Frozen
    Orbit Reset burns (2013-04-29, 2014-04-03, 2015-05-04, each 2.7-5.7 m/s) are all pre-2016 and
    don't recur.
  - Real stationkeeping burns (pre-2016) were ~5.5 m/s each, in posigrade/retrograde pairs ~3 hours
    apart, roughly every 28 days.
- **`maneuver_detection.py`**: momentum unloads, though small, turn out to be directly detectable
  in LRO's own public reconstructed-orbit SPK — no SFF needed. Method: sample state vectors (r, v)
  at fine, uniform cadence, compute specific angular momentum `h = r x v` and specific orbital
  energy `eps = v^2/2 - GM/r`, and flag persistent step changes across all four channels jointly
  (quadrature sum of each channel's own MAD-normalized z-score) — a real burn shifts them to a new
  baseline and stays there, unlike gravity-driven periodic oscillation. Chosen over the classical
  elements (a, e, i) an earlier version of this tool used: `Delta h = r x Delta v` is *exact* (not a
  linearized rate) and has a clean, phase-INDEPENDENT null only on the radial impulse component —
  unlike inclination alone, whose Gauss-equation sensitivity is modulated by
  `cos(argument of latitude)` and vanishes at node crossings, which a momentum unload has no reason
  to avoid. This mattered concretely: Mesarch et al. note momentum unloads were flown "in the +/-
  orbit normal direction to minimize the along-track perturbative effects" early in the mission —
  i.e. designed to be invisible to a semi-major-axis-only check. See the module's own docstring for
  the full derivation, including the one honest residual gap it still has (a purely-radial impulse,
  weakly observable across LRO's whole near-circular orbit, not just near apsis — detection still
  works, but the reconstructed radial component isn't trusted, via an explicit `rcond` cutoff on the
  impulse-reconstruction least-squares solve).

  Validated against the literature above: run over H2 2019 (encompasses this repo's fixture EDR,
  `M1329714703CE`, 2019-11-30), it finds 11 candidates, 11–30 days apart, matching the paper's
  momentum-unload cadence almost exactly, in a window the paper independently confirms had no EPM or
  stationkeeping burn (`combined_z` stays under ~20 for all of them). **Notably, several are
  normal-direction-dominant, up to ~2.1 m/s total** — several times larger than the ~0.07–0.25 m/s
  a semi-major-axis-only estimate reports for the same dates, since that estimate is blind to
  exactly the component driving them. Cross-checked against a short 2010 window (pre-frozen-orbit):
  real stationkeeping pairs there are unmistakable (`combined_z` in the hundreds, ~5.2–5.6 m/s,
  tangential-dominant, alternating sign, ~2h38m apart — matching the paper's "~3 hours" and 2-burn
  posigrade/retrograde description almost exactly), cleanly separated from momentum-unload-scale
  candidates in the same window. Wired into `dataset_selection.add_maneuver_flags` (flags a whole
  orbit-level table at once) — not into `candidate_window.images_for_window()`'s own per-candidate filtering;
  also usable standalone (`find_maneuver_candidates(start_dt, end_dt, config)`) for vetting a
  candidate date range by hand.
