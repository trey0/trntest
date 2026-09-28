"""Seam probes on the WAC_EMP reflectance mosaic at every point where three or four of its tiles meet.
`notebooks/reflectance_seams.ipynb` shows the same health table, linking to per-probe reports; run it when one
of these fails.

Marked `@pytest.mark.heavy`: reads ten ~2 GB WAC_EMP tiles (fetched on first use, cached after).
"""

import pytest

from trntest import seam_probes

_SOURCE = seam_probes.SOURCES["reflectance"]
_PROBES = {probe.name: probe for probe in _SOURCE.probes}
_POLAR_SEAMS = ("lat +60", "lat -60")
# Corners where `wac_emp_edge_correction` leaves a streak over the +-60 deg gradient limit (see
# `docs/proposed-tasks/open-items.md`). Strict: a corner that starts passing fails the run until its
# marker is removed here.
_KNOWN_POLAR_GRADIENT_FAILURES = {"60N_090E", "60N_180E", "60N_270E", "60S_000E", "60S_090E", "60S_180E", "60S_270E"}


@pytest.fixture(scope="module")
def results():
    return {result.grid.probe.name: result for result in seam_probes.run_source(_SOURCE.name)}


def _failures(result) -> set[tuple[str, str]]:
    violations = seam_probes.threshold_violations(seam_probes.metrics_table([result]), _SOURCE.thresholds)
    return {(row.seam, metric) for _, row in violations.iterrows() for metric in row.failed.split(", ")}


def _expected_seams(probe: seam_probes.SeamProbe) -> set[str]:
    lon_seam = f"lon {probe.center_lon_deg:g}"
    if probe.center_lat_deg == 0:
        return {"lat 0", lon_seam}
    return {f"lat {probe.center_lat_deg:+g}", lon_seam}


@pytest.mark.heavy
@pytest.mark.parametrize("probe_name", list(_PROBES))
def test_probe_mosaics_expected_tiles_and_sees_its_seams(results, probe_name):
    result = results[probe_name]
    probe = result.grid.probe
    expected_tiles = 4 if probe.center_lat_deg == 0 else 3
    assert len(result.source_ids) == expected_tiles
    assert {p.seam.name for p in result.profiles} == _expected_seams(probe)


@pytest.mark.heavy
@pytest.mark.parametrize("probe_name", list(_PROBES))
def test_probe_seams_within_limits(results, probe_name):
    # Everything except the +-60 deg gradient check, which has its own test below.
    failures = {f for f in _failures(results[probe_name]) if not (f[0] in _POLAR_SEAMS and f[1] == "gradient_ratio")}
    assert not failures


@pytest.mark.heavy
@pytest.mark.parametrize(
    "probe_name",
    [
        pytest.param(
            name,
            marks=pytest.mark.xfail(strict=True, reason="+-60 deg edge correction inadequate at this corner")
            if name in _KNOWN_POLAR_GRADIENT_FAILURES
            else (),
        )
        for name, probe in _PROBES.items()
        if probe.center_lat_deg != 0
    ],
)
def test_polar_seam_gradient_within_limit(results, probe_name):
    assert not {f for f in _failures(results[probe_name]) if f[0] in _POLAR_SEAMS and f[1] == "gradient_ratio"}
