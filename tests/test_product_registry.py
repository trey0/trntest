from pathlib import Path

import pytest

from trntest import dem_ortho, isis_wac, product_registry, render

# -- atomic_publish / atomic_publish_path -----------------------------------------------------------


def test_atomic_publish_writes_to_temp_then_renames_to_dest(tmp_path):
    dest = tmp_path / "sub" / "out.txt"

    with product_registry.atomic_publish(dest) as tmp:
        assert tmp.parent == dest.parent
        assert tmp != dest
        tmp.write_text("hello")
        # Not visible at dest until the context manager exits cleanly.
        assert not dest.exists()

    assert dest.read_text() == "hello"


def test_atomic_publish_leaves_no_temp_file_after_success(tmp_path):
    dest = tmp_path / "out.txt"

    with product_registry.atomic_publish(dest) as tmp:
        tmp.write_text("data")

    assert list(dest.parent.glob(f"{dest.stem}.tmp.*")) == []


def test_atomic_publish_cleans_up_and_reraises_on_exception(tmp_path):
    dest = tmp_path / "out.txt"

    with pytest.raises(ValueError, match="boom"):
        with product_registry.atomic_publish(dest) as tmp:
            tmp.write_text("partial")
            raise ValueError("boom")

    assert not dest.exists()
    assert list(dest.parent.glob(f"{dest.stem}.tmp.*")) == []


def test_atomic_publish_creates_parent_directory(tmp_path):
    dest = tmp_path / "a" / "b" / "c" / "out.txt"

    with product_registry.atomic_publish(dest) as tmp:
        tmp.write_text("nested")

    assert dest.read_text() == "nested"


def test_atomic_publish_path_preserves_dest_suffix_on_the_temp_path(tmp_path):
    # Confirmed live (heavy-suite regression): a real ISIS `framestitch TO=<path>` call silently
    # never wrote anything at a `.tmp`-suffixed path, only at one ending in `.cub` -- the temp path
    # must keep dest's real suffix, not a generic one, or callers like this go silently unwritten.
    dest = tmp_path / "out.cub"

    with product_registry.atomic_publish_path(dest) as tmp:
        assert tmp.suffix == ".cub"
        tmp.write_text("subprocess output")


def test_atomic_publish_path_yields_a_path_that_does_not_exist(tmp_path):
    dest = tmp_path / "out.cub"

    with product_registry.atomic_publish_path(dest) as tmp:
        assert tmp.parent == dest.parent
        assert not tmp.exists()
        tmp.write_text("subprocess output")

    assert dest.read_text() == "subprocess output"


def test_atomic_publish_path_cleans_up_on_exception_even_if_never_created(tmp_path):
    dest = tmp_path / "out.cub"

    with pytest.raises(RuntimeError, match="subprocess failed"):
        with product_registry.atomic_publish_path(dest):
            raise RuntimeError("subprocess failed")

    assert not dest.exists()
    assert list(dest.parent.glob(f"{dest.stem}.tmp.*")) == []


def test_atomic_publish_path_cleans_up_temp_output_on_exception(tmp_path):
    dest = tmp_path / "out.cub"

    with pytest.raises(RuntimeError):
        with product_registry.atomic_publish_path(dest) as tmp:
            tmp.write_text("half-written")
            raise RuntimeError("boom")

    assert not dest.exists()
    assert list(dest.parent.glob(f"{dest.stem}.tmp.*")) == []


def test_atomic_publish_prefix_writes_via_the_tools_own_suffix_then_renames_to_dest(tmp_path):
    dest = tmp_path / "dem_filled-tile-0.tif"

    with product_registry.atomic_publish_prefix(dest, "-tile-0.tif") as tmp_prefix:
        assert not Path(str(tmp_prefix) + "-tile-0.tif").exists()
        # Mirrors what dem_mosaic itself does: append its own fixed suffix to the given prefix.
        Path(str(tmp_prefix) + "-tile-0.tif").write_text("hole-filled dem")
        assert not dest.exists()

    assert dest.read_text() == "hole-filled dem"


def test_atomic_publish_prefix_leaves_no_temp_output_after_success(tmp_path):
    dest = tmp_path / "run-cam.tif"

    with product_registry.atomic_publish_prefix(dest, "-cam.tif") as tmp_prefix:
        Path(str(tmp_prefix) + "-cam.tif").write_text("rendered")

    assert list(dest.parent.glob(f"{dest.stem}.tmp.*")) == []


def test_atomic_publish_prefix_cleans_up_temp_output_on_exception(tmp_path):
    dest = tmp_path / "dem_filled-tile-0.tif"

    with pytest.raises(RuntimeError, match="boom"):
        with product_registry.atomic_publish_prefix(dest, "-tile-0.tif") as tmp_prefix:
            Path(str(tmp_prefix) + "-tile-0.tif").write_text("partial")
            raise RuntimeError("boom")

    assert not dest.exists()
    assert list(dest.parent.glob(f"{dest.stem}.tmp.*")) == []


def test_atomic_publish_prefix_cleans_up_on_exception_even_if_tool_never_wrote_anything(tmp_path):
    dest = tmp_path / "dem_filled-tile-0.tif"

    with pytest.raises(RuntimeError, match="subprocess failed"):
        with product_registry.atomic_publish_prefix(dest, "-tile-0.tif"):
            raise RuntimeError("subprocess failed")

    assert not dest.exists()


def test_writes_product_registers_and_returns_function_unchanged():
    def my_writer():
        return "wrote"

    decorated = product_registry.writes_product("test_label_a")(my_writer)

    assert decorated is my_writer
    assert decorated() == "wrote"
    assert product_registry.writer_of("test_label_a") is my_writer


def test_writes_product_raises_on_duplicate_label():
    def first():
        pass

    def second():
        pass

    product_registry.writes_product("test_label_b")(first)

    with pytest.raises(product_registry.ProductRegistryError, match="test_label_b"):
        product_registry.writes_product("test_label_b")(second)

    # The original registration is untouched by the failed second attempt.
    assert product_registry.writer_of("test_label_b") is first


def test_writer_of_returns_none_for_unregistered_label():
    assert product_registry.writer_of("test_label_never_registered") is None


def test_reads_product_accepts_multiple_registrants():
    def reader_one():
        pass

    def reader_two():
        pass

    product_registry.reads_product("test_label_c")(reader_one)
    product_registry.reads_product("test_label_c")(reader_two)

    assert product_registry.readers_of("test_label_c") == [reader_one, reader_two]


def test_reads_product_returns_function_unchanged():
    def reader():
        return 42

    decorated = product_registry.reads_product("test_label_d")(reader)

    assert decorated is reader
    assert decorated() == 42


def test_readers_of_returns_empty_list_for_unregistered_label():
    assert product_registry.readers_of("test_label_never_registered") == []


def test_deletes_product_accepts_multiple_registrants():
    def deleter_one():
        pass

    def deleter_two():
        pass

    product_registry.deletes_product("test_label_e")(deleter_one)
    product_registry.deletes_product("test_label_e")(deleter_two)

    assert product_registry.deleters_of("test_label_e") == [deleter_one, deleter_two]


def test_deletes_product_returns_function_unchanged():
    def deleter():
        return "deleted"

    decorated = product_registry.deletes_product("test_label_f")(deleter)

    assert decorated is deleter
    assert decorated() == "deleted"


def test_deleters_of_returns_empty_list_for_unregistered_label():
    assert product_registry.deleters_of("test_label_never_registered") == []


def test_real_pipeline_writers_are_registered():
    # docs/history.md's Phase 79 entry: the real writer functions decorated so far are
    # legibly registered under their own label -- importing the modules is enough to trigger
    # registration (decorators run at module-import/definition time).
    assert product_registry.writer_of("dem_filled") is dem_ortho.fetch_dem
    assert product_registry.writer_of("ortho_shaded") is dem_ortho.fetch_and_shade_ortho
    assert product_registry.writer_of("isis_stitched_cube") is isis_wac.run_framestitch
    assert product_registry.writer_of("isis_crop_cube") is isis_wac.crop_for_camera
    assert product_registry.writer_of("crop_cam2map") is isis_wac.run_cam2map_for_crop
    assert product_registry.writer_of("sat_sim_render") is render.run_sat_sim


def test_decorator_syntax_works_end_to_end():
    @product_registry.writes_product("test_label_g")
    def writer():
        return "written"

    @product_registry.reads_product("test_label_g")
    def reader():
        return "read"

    assert writer() == "written"
    assert reader() == "read"
    assert product_registry.writer_of("test_label_g") is writer
    assert product_registry.readers_of("test_label_g") == [reader]
