"""The Stage 3 tile grid (SPEC-05 §3; WP-6.1)."""
import math

import numpy as np
import pytest

from pipeline.tile_grid import EmptyTileGridError, tissue_tile_grid
from pipeline.tissue_mask import TissueMask

TILE_UM = 224.0
MASK_MPP = 8.0


def brute_force(mask: TissueMask, extent_um, tile_um, min_fraction):
    width_um, height_um = extent_um
    kept = []
    for j in range(math.ceil(height_um / tile_um)):
        for i in range(math.ceil(width_um / tile_um)):
            x0, y0 = i * tile_um, j * tile_um
            fraction = mask.fraction_in_box_um(x0, y0, x0 + tile_um, y0 + tile_um)
            if fraction >= min_fraction:
                kept.append((i, j, fraction))
    return kept


def diagonal_band(width_um: float, height_um: float, band_um: float) -> TissueMask:
    """A thin diagonal core, the CNB case whose heatmap v5 left mostly empty (SPEC-05 §1.2)."""
    rows, cols = math.ceil(height_um / MASK_MPP), math.ceil(width_um / MASK_MPP)
    y, x = np.mgrid[0:rows, 0:cols] * MASK_MPP
    distance = np.abs(y - x * height_um / width_um) / math.hypot(1.0, height_um / width_um)
    return TissueMask(distance <= band_um / 2, MASK_MPP)


@pytest.mark.parametrize("min_fraction", [0.25, 0.01, 0.9])
def test_the_grid_is_every_tile_with_enough_tissue_on_a_thin_diagonal_core(min_fraction):
    extent = (5000.0, 3000.0)  # not a multiple of 224 µm: partial tiles on the right and bottom edges
    mask = diagonal_band(*extent, band_um=500.0)
    grid = tissue_tile_grid(mask, extent, TILE_UM, min_fraction)

    expected = brute_force(mask, extent, TILE_UM, min_fraction)
    assert [(int(i), int(j)) for i, j in zip(grid.i, grid.j)] == [(i, j) for i, j, _ in expected]
    np.testing.assert_allclose(grid.tissue_fraction, [f for _, _, f in expected], rtol=1e-6)
    assert (grid.n_cols, grid.n_rows) == (math.ceil(5000 / 224), math.ceil(3000 / 224))
    if min_fraction <= 0.25:
        # The band is covered along its whole length, the partial last column included.
        assert set(grid.i.tolist()) == set(range(grid.n_cols))
    assert grid.j.dtype == np.int32 and grid.tissue_fraction.dtype == np.float32


def test_tiles_are_anchored_at_the_slide_origin():
    mask = TissueMask(np.ones((100, 100), dtype=bool), MASK_MPP)
    grid = tissue_tile_grid(mask, (800.0, 800.0), TILE_UM, 0.25)
    np.testing.assert_array_equal(grid.x_um, grid.i * TILE_UM)
    np.testing.assert_array_equal(grid.y_um, grid.j * TILE_UM)
    assert grid.tile_ids()[:2] == ["t_0_0", "t_1_0"]
    assert grid.version == "grid224_v1"


def test_a_partial_edge_tile_counts_its_area_off_the_slide_as_glass():
    # 300 µm wide: column 1 is 76 µm of slide and 148 µm off it, so all-tissue gives it 76/224 = 0.34.
    mask = TissueMask(np.ones((56, 75), dtype=bool), 4.0)  # exactly the 300 x 224 µm slide
    extent = (300.0, 224.0)
    assert [int(i) for i in tissue_tile_grid(mask, extent, TILE_UM, 0.25).i] == [0, 1]
    assert [int(i) for i in tissue_tile_grid(mask, extent, TILE_UM, 0.5).i] == [0]


def test_there_is_no_cap_on_the_number_of_tiles():
    side_um = 60 * TILE_UM + 1.0  # 61 x 61 tiles = 3,721, more than v5's 2,048-patch budget
    mask = TissueMask(np.ones((math.ceil(side_um / MASK_MPP),) * 2, dtype=bool), MASK_MPP)
    grid = tissue_tile_grid(mask, (side_um, side_um), TILE_UM, 0.0)
    assert grid.n_tiles == 61 * 61 > 2048


def test_no_tissue_tile_raises():
    mask = TissueMask(np.zeros((50, 50), dtype=bool), MASK_MPP)
    with pytest.raises(EmptyTileGridError):
        tissue_tile_grid(mask, (400.0, 400.0), TILE_UM, 0.25)


@pytest.mark.parametrize("tile_um", [0.0, -224.0, float("nan")])
def test_a_bad_tile_size_raises(tile_um):
    mask = TissueMask(np.ones((50, 50), dtype=bool), MASK_MPP)
    with pytest.raises(ValueError, match="tile_um"):
        tissue_tile_grid(mask, (400.0, 400.0), tile_um, 0.25)
