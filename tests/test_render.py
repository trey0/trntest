import json

import pytest

from trntest import render

_FAKE_CSM_STATE = {
    "m_modelName": "USGS_ASTRO_FRAME_SENSOR_MODEL",
    "m_focalLength": 242.32354117560124,  # (fu + fv) / 2, cam_gen's isotropic average
    "m_iTransL": [0.0, 0.0, 1.0],
    "m_iTransS": [0.0, 1.0, 0.0],
    "m_transX": [0.0, 0.0, 1.0],
    "m_transY": [0.0, 1.0, 0.0],
    "m_ccdCenter": [133.25956719715256, 128.0],
}


def _write_fake_csm_json(path) -> None:
    with open(path, "w") as f:
        f.write("USGS_ASTRO_FRAME_SENSOR_MODEL\n")
        json.dump(_FAKE_CSM_STATE, f)


def test_correct_csm_focal_length_anisotropy_restores_per_axis_scale(tmp_path):
    csm_path = tmp_path / "fake.json"
    _write_fake_csm_json(csm_path)
    fu, fv = 235.24707571465046, 249.40000663655198

    render._correct_csm_focal_length_anisotropy(csm_path, fu, fv)

    with open(csm_path) as f:
        header = f.readline()
        state = json.load(f)

    assert header == "USGS_ASTRO_FRAME_SENSOR_MODEL\n"
    assert state["m_focalLength"] == fu
    # Sample axis (m_iTransS/m_transX) is untouched -- already correct once m_focalLength == fu.
    assert state["m_iTransS"] == [0.0, 1.0, 0.0]
    assert state["m_transX"] == [0.0, 0.0, 1.0]
    # Line axis (m_iTransL/m_transY) rescaled to the real fv/fu ratio (and its reciprocal).
    assert state["m_iTransL"] == [0.0, 0.0, fv / fu]
    assert state["m_transY"] == [0.0, fu / fv, 0.0]
    # Sign of an already-negative slot is preserved, not flipped, by the correction.
    assert all(v >= 0 for v in state["m_iTransL"])


def test_correct_csm_focal_length_anisotropy_preserves_sign(tmp_path):
    csm_path = tmp_path / "fake.json"
    state = dict(_FAKE_CSM_STATE)
    state["m_iTransL"] = [0.0, 0.0, -1.0]  # a flipped axis convention
    state["m_transY"] = [0.0, -1.0, 0.0]
    with open(csm_path, "w") as f:
        f.write("USGS_ASTRO_FRAME_SENSOR_MODEL\n")
        json.dump(state, f)
    fu, fv = 235.25, 249.40

    render._correct_csm_focal_length_anisotropy(csm_path, fu, fv)

    with open(csm_path) as f:
        f.readline()
        result = json.load(f)
    assert result["m_iTransL"] == pytest.approx([0.0, 0.0, -(fv / fu)])
    assert result["m_transY"] == pytest.approx([0.0, -(fu / fv), 0.0])


def test_correct_csm_focal_length_anisotropy_noop_when_symmetric(tmp_path):
    csm_path = tmp_path / "fake.json"
    _write_fake_csm_json(csm_path)
    before = csm_path.read_text()

    render._correct_csm_focal_length_anisotropy(csm_path, 235.25, 235.25)

    assert csm_path.read_text() == before
