import pandas as pd
import pytest

from trntest import trn_dataset, trn_products
from trntest.config import TrntestConfig


class _FakeImage(trn_products.TrnTestImage):
    """Minimal concrete `TrnTestImage` -- exercises the shared base-class logic (`exists`,
    `generate`'s idempotency, `_require_generated`) without any real generation/plotting."""

    def __init__(self, entry):
        super().__init__(entry)
        self.generate_impl_calls = 0

    @property
    def raster_path(self):
        return self.entry.dataset_folder / "fake.raster"

    @property
    def sidecar_json_path(self):
        return self.entry.dataset_folder / "fake.json"

    @property
    def rotation_k(self):
        return 0

    @property
    def width_km(self):
        return 1.0

    @property
    def height_km(self):
        return 1.0

    @property
    def footprint_lonlat_deg(self):
        return {}

    @property
    def render_label(self):
        return "Fake"

    @property
    def generator_name(self):
        return "fake"

    @property
    def tie_point_px_key(self):
        return "synthetic_px"

    def _generate_impl(self):
        self.generate_impl_calls += 1
        self.raster_path.parent.mkdir(parents=True, exist_ok=True)
        self.raster_path.write_text("raster")
        self.sidecar_json_path.write_text("{}")

    def _mapprojected_path(self):
        return self.entry.dataset_folder / "fake-mapproj.tif"


# -- Exact path naming ------------------------------------------------------------------------


def test_crop_and_hillshade_path_naming(tmp_path):
    folder = tmp_path / "ds"
    entry = trn_dataset.TrnTestEntry(
        pd.Series({"product_id": "M1327210646CE", "edr_product": "M1327210646CE"}), folder, TrntestConfig()
    )

    assert entry.crop.raster_path == folder / "crop" / "M1327210646CE_crop.cub"
    assert entry.crop.sidecar_json_path == folder / "crop" / "M1327210646CE_crop.json"
    assert entry.hillshade.raster_path == folder / "hillshade" / "M1327210646CE_hillshade.tif"
    assert entry.hillshade.sidecar_json_path == folder / "hillshade" / "M1327210646CE_hillshade.json"


def test_hillshade_and_reproject_mapprojected_path_are_generator_scoped(tmp_path, monkeypatch):
    # docs/history.md's Phase 79 entry: _work/<entry>/<generator>/<label>, not a flat
    # _work/<entry>/<label> -- hillshade and reproject must land in their own separate subfolders
    # even though they share this same inherited _mapprojected_path implementation.
    folder = tmp_path / "ds"
    row = pd.Series(
        {
            "product_id": "M1327210646CE",
            "edr_product": "M1327210646CE",
            "edr_volume": "LROLRC_0041C",
            "edr_subdir": "ESM4",
            "edr_doy": "2019334",
            "cdr_volume": "LROLRC_1041C",
            "cdr_product": "M1327210646CC",
            "start_frame": 440,
        }
    )
    entry = trn_dataset.TrnTestEntry(row, folder, TrntestConfig())
    work_dir = folder / "_work" / "M1327210646CE"

    captured_out_paths = []

    def fake_run_mapproject_image(image_path, camera_path, output_path, dem_ortho_result, config):
        captured_out_paths.append(output_path)
        return output_path

    monkeypatch.setattr(trn_products.render, "run_mapproject_image", fake_run_mapproject_image)
    monkeypatch.setattr(trn_dataset.TrnTestEntry, "dem_ortho_result", None)

    entry.hillshade._mapprojected_path()
    entry.reproject._mapprojected_path()

    assert captured_out_paths[0] == work_dir / "hillshade" / "M1327210646CE_hillshade-mapproj.tif"
    assert captured_out_paths[1] == work_dir / "reproject" / "M1327210646CE_reproject-mapproj.tif"


# -- TrnTestImage shared base-class logic (via the fake subclass) -----------------------------


def test_image_exists_false_before_generate(tmp_path):
    entry = trn_dataset.TrnTestEntry(pd.Series({"product_id": "P1", "edr_product": "P1"}), tmp_path, TrntestConfig())
    image = _FakeImage(entry)
    assert not image.exists()


def test_image_require_generated_raises_before_generate(tmp_path):
    entry = trn_dataset.TrnTestEntry(pd.Series({"product_id": "P1", "edr_product": "P1"}), tmp_path, TrntestConfig())
    image = _FakeImage(entry)
    with pytest.raises(FileNotFoundError):
        image._require_generated()


def test_image_generate_is_idempotent(tmp_path):
    entry = trn_dataset.TrnTestEntry(pd.Series({"product_id": "P1", "edr_product": "P1"}), tmp_path, TrntestConfig())
    image = _FakeImage(entry)

    first = image.generate()
    second = image.generate()

    assert first == second == image.raster_path
    assert image.exists()
    assert image.generate_impl_calls == 1
