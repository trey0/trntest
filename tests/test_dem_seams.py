"""Seam probes on the DEM at every point where SLDEM2015's 512-ppd tile edges (every 30 deg of latitude
to +-60, every 45 deg of longitude) meet, for GLD100 alone and for the SLDEM2015 + GLD100 mosaic
with and without its +-60 deg seam treatment. `notebooks/dem_seams.ipynb` shows the same health
tables, linking to per-probe reports; run it when one of these fails.

Marked `@pytest.mark.heavy`: reads the ~10 GB Astropedia GLD100 file and the SLDEM2015 tiles
(fetched on first use, cached after).
"""

import functools

import pytest

from trntest import seam_probes

_SOURCES = ("dem_gld100", "dem_sldem2015_gld100_hardcut", "dem_sldem2015_gld100")
_PROBE_NAMES = [probe.name for probe in seam_probes.DEM_PROBES]
_AT_60 = {f"60{hemisphere}_{lon:03d}E" for hemisphere in "NS" for lon in range(0, 360, 45)}
# Strict: a probe that starts passing fails the run until its marker is removed here. See
# `notebooks/dem_seams.ipynb` and `docs/proposed-tasks/open-items.md`.
_KNOWN_FAILURES = {
    # GLD100's own seams, in the source file: at +-60 deg (a nodata row along parts of 60N and 60S,
    # and a one-row line in elevation), and one-column lines along 90 and 270 deg between them (only
    # 270 is strong enough to fail here).
    "dem_gld100": _AT_60 | {"30N_270E"},
    # The same +-60 deg rows of GLD100, now right against SLDEM2015's edge, plus the offset between
    # the two sources; `dem_sources.LatSeam` treats both.
    "dem_sldem2015_gld100_hardcut": _AT_60,
    "dem_sldem2015_gld100": set(),
}


@functools.cache
def _results(source: str) -> dict:
    return {result.grid.probe.name: result for result in seam_probes.run_source(source)}


def _expected_seams(probe: seam_probes.SeamProbe) -> set[str]:
    lon_seam = f"lon {probe.center_lon_deg:g}"
    if abs(probe.center_lat_deg) == 45:
        return {lon_seam}
    return {f"lat {probe.center_lat_deg:+g}" if probe.center_lat_deg else "lat 0", lon_seam}


@pytest.mark.heavy
@pytest.mark.parametrize("probe_name", _PROBE_NAMES)
def test_probe_sees_its_seams(probe_name):
    result = _results("dem_gld100")[probe_name]
    assert {p.seam.name for p in result.profiles} == _expected_seams(result.grid.probe)


@pytest.mark.heavy
@pytest.mark.parametrize(
    ("source", "probe_name"),
    [
        pytest.param(
            source,
            name,
            marks=pytest.mark.xfail(strict=True, reason="known seam defect") if name in _KNOWN_FAILURES[source] else (),
        )
        for source in _SOURCES
        for name in _PROBE_NAMES
    ],
)
def test_probe_seams_within_limits(source, probe_name):
    table = seam_probes.metrics_table([_results(source)[probe_name]])
    violations = seam_probes.threshold_violations(table, seam_probes.SOURCES[source].thresholds)
    assert violations.empty, violations[["seam", "failed"]].to_string()
