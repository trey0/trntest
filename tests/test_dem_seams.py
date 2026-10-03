"""Seam probes on the GLD100 DEM at every point where SLDEM2015's 512-ppd tile edges (every 30 deg of
latitude to +-60, every 45 deg of longitude) meet. `notebooks/dem_seams.ipynb` shows the same health
table, linking to per-probe reports; run it when one of these fails.

Marked `@pytest.mark.heavy`: reads the ~10 GB Astropedia GLD100 file (fetched on first use, cached
after).
"""

import pytest

from trntest import seam_probes

_SOURCE = seam_probes.SOURCES["dem_gld100"]
_PROBES = {probe.name: probe for probe in _SOURCE.probes}
# GLD100's own seams, in the source file: at +-60 deg (a nodata row along parts of 60N and 60S, and a
# one-row line in elevation), and one-column lines along 90 and 270 deg between them (only 270 is
# strong enough to fail here). See `notebooks/dem_seams.ipynb` and
# `docs/proposed-tasks/open-items.md`. Strict: a probe that starts passing fails the run until its
# marker is removed here.
_KNOWN_GLD100_FAILURES = {
    *(f"60{hemisphere}_{lon:03d}E" for hemisphere in "NS" for lon in range(0, 360, 45)),
    "30N_270E",
}


@pytest.fixture(scope="module")
def results():
    return {result.grid.probe.name: result for result in seam_probes.run_source(_SOURCE.name)}


def _expected_seams(probe: seam_probes.SeamProbe) -> set[str]:
    lon_seam = f"lon {probe.center_lon_deg:g}"
    if abs(probe.center_lat_deg) == 45:
        return {lon_seam}
    return {f"lat {probe.center_lat_deg:+g}" if probe.center_lat_deg else "lat 0", lon_seam}


@pytest.mark.heavy
@pytest.mark.parametrize("probe_name", list(_PROBES))
def test_probe_sees_its_seams(results, probe_name):
    result = results[probe_name]
    assert {p.seam.name for p in result.profiles} == _expected_seams(result.grid.probe)


@pytest.mark.heavy
@pytest.mark.parametrize(
    "probe_name",
    [
        pytest.param(
            name,
            marks=pytest.mark.xfail(strict=True, reason="GLD100's own seam") if name in _KNOWN_GLD100_FAILURES else (),
        )
        for name in _PROBES
    ],
)
def test_probe_seams_within_limits(results, probe_name):
    violations = seam_probes.threshold_violations(seam_probes.metrics_table([results[probe_name]]), _SOURCE.thresholds)
    assert violations.empty, violations[["seam", "failed"]].to_string()
