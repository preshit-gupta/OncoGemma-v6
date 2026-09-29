"""DeepZoom tiles read through read_region_at_mpp (SPEC-04 §3.1, §3.4): the same tiles OpenSlide's generator makes."""
import math

import numpy as np
import openslide
import pytest
from openslide.deepzoom import DeepZoomGenerator

from pipeline.errors import RegionOutOfBoundsError
from pipeline.slide_io import (
    DZI_TILE_PX,
    SlideReader,
    dzi_level_dimensions,
    dzi_max_level,
    read_dzi_tile,
)
from tests.fakes.tiff import tissue_rgb, write_pyramid_tiff

MPP = 0.5
WIDTH_PX, HEIGHT_PX = 1300, 700  # not multiples of the tile size, so every level has edge tiles


@pytest.fixture(scope="module")
def slide_path(tmp_path_factory):
    base = tissue_rgb(WIDTH_PX, HEIGHT_PX, MPP, squares_um=[(120.0, 80.0, 60.0), (400.0, 200.0, 40.0)])
    return write_pyramid_tiff(tmp_path_factory.mktemp("dzi") / "slide.tif", base, MPP, levels=3)


@pytest.fixture(scope="module")
def generator(slide_path):
    return DeepZoomGenerator(openslide.OpenSlide(str(slide_path)), tile_size=DZI_TILE_PX, overlap=0, limit_bounds=False)


def test_levels_and_dimensions_are_openslides(generator):
    assert dzi_max_level(WIDTH_PX, HEIGHT_PX) == generator.level_count - 1
    for level in range(generator.level_count):
        assert dzi_level_dimensions(WIDTH_PX, HEIGHT_PX, level) == generator.level_dimensions[level]


def test_every_tile_of_every_level_has_the_size_openslide_gives_it(slide_path, generator):
    """``get_tile_dimensions`` is OpenSlide's stated tile size (its ``get_tile`` is a pixel short on one level)."""
    with SlideReader(str(slide_path), MPP, MPP, "tiff") as reader:
        for level in range(generator.level_count):
            cols, rows = generator.level_tiles[level]
            for col in range(cols):
                for row in range(rows):
                    expected = generator.get_tile_dimensions(level, (col, row))  # (width, height)
                    tile = read_dzi_tile(reader, level, col, row)
                    assert (tile.rgb.shape[1], tile.rgb.shape[0]) == expected, (level, col, row)


@pytest.mark.parametrize("level_offset", [0, 2, 4])
def test_a_tile_shows_the_same_picture_as_openslides(slide_path, generator, level_offset):
    """Tiles agree with OpenSlide's up to resampling (its box average against LANCZOS)."""
    level = generator.level_count - 1 - level_offset
    cols, rows = generator.level_tiles[level]
    with SlideReader(str(slide_path), MPP, MPP, "tiff") as reader:
        for col, row in {(0, 0), (cols - 1, 0), (0, rows - 1), (cols - 1, rows - 1)}:
            ours = read_dzi_tile(reader, level, col, row).rgb.astype(int)
            theirs = np.array(generator.get_tile(level, (col, row)).convert("RGB")).astype(int)
            assert ours.shape == theirs.shape
            assert np.abs(ours - theirs).mean() < 8, (level, col, row)


def test_a_tile_is_read_at_its_levels_resolution(slide_path):
    with SlideReader(str(slide_path), MPP, MPP, "tiff") as reader:
        region = read_dzi_tile(reader, dzi_max_level(WIDTH_PX, HEIGHT_PX) - 2, 0, 0)
    assert region.target_mpp == MPP * 4 and not region.upsampled


def test_a_normalized_tile_goes_through_the_stain_transform(slide_path):
    class Halve:
        profile_id = None

        def apply(self, rgb):
            return (rgb // 2).astype(np.uint8)

    with SlideReader(str(slide_path), MPP, MPP, "tiff") as reader:
        level = dzi_max_level(WIDTH_PX, HEIGHT_PX) - 1
        raw = read_dzi_tile(reader, level, 0, 0)
        normalized = read_dzi_tile(reader, level, 0, 0, color="normalized", stain=Halve())
    assert np.array_equal(normalized.rgb, raw.rgb // 2) and normalized.color == "normalized"


@pytest.mark.parametrize("level, col, row", [(-1, 0, 0), (99, 0, 0), (0, 1, 0), (0, 0, 1), (5, -1, 0), (11, 99, 99)])
def test_tiles_outside_the_pyramid_are_refused(slide_path, level, col, row):
    with SlideReader(str(slide_path), MPP, MPP, "tiff") as reader:
        with pytest.raises(RegionOutOfBoundsError):
            read_dzi_tile(reader, level, col, row)


def test_an_anisotropic_slide_still_yields_openslide_sized_tiles(slide_path, generator):
    """mpp_y differs a little from mpp_x: the read is a fraction of a pixel off, and the tile is fitted to size."""
    with SlideReader(str(slide_path), MPP, MPP * 1.01, "tiff") as reader:
        level = generator.level_count - 3
        cols, rows = generator.level_tiles[level]
        for col in range(cols):
            for row in range(rows):
                tile = read_dzi_tile(reader, level, col, row)
                assert (tile.rgb.shape[1], tile.rgb.shape[0]) == generator.get_tile_dimensions(level, (col, row))
