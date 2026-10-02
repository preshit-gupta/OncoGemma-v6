"""The Stage 3 tile grid (SPEC-05 §3).

The grid is global and anchored at the slide origin: tile ``(i, j)`` covers
``[i*t, (i+1)*t) x [j*t, (j+1)*t)`` µm, ``i`` the column and ``j`` the row. It spans the whole
slide, so a partial tile at the right or bottom edge is a candidate too (its area off the slide
counts as non-tissue, and its pixels there are read as white). Every tile with enough tissue is
included: there is no cap, so the heatmap is defined over all tissue (S3-COV = 1.0).
"""
import math
from dataclasses import dataclass

import numpy as np

from pipeline.tissue_mask import TissueMask

GRID_SCHEMA_VERSION = 1


class EmptyTileGridError(ValueError):
    """No tile of the grid has enough tissue."""


def grid_version(tile_um: float) -> str:
    """``grid224_v1`` for 224 µm tiles. Part of the embedding cache key."""
    return f"grid{tile_um:g}_v{GRID_SCHEMA_VERSION}"


@dataclass(frozen=True)
class TileGrid:
    tile_um: float
    n_cols: int
    n_rows: int
    # Included tiles in row-major order: column, row and exact tissue fraction of each.
    i: np.ndarray
    j: np.ndarray
    tissue_fraction: np.ndarray

    @property
    def version(self) -> str:
        return grid_version(self.tile_um)

    @property
    def n_tiles(self) -> int:
        return int(self.i.size)

    @property
    def x_um(self) -> np.ndarray:
        return self.i.astype(np.float64) * self.tile_um

    @property
    def y_um(self) -> np.ndarray:
        return self.j.astype(np.float64) * self.tile_um

    def tile_ids(self) -> list[str]:
        return [f"t_{i}_{j}" for i, j in zip(self.i.tolist(), self.j.tolist())]


def tissue_tile_grid(mask: TissueMask, extent_um: tuple[float, float], tile_um: float, min_fraction: float) -> TileGrid:
    """Every tile of the slide's grid whose tissue fraction is at least ``min_fraction``.

    ``extent_um`` is the slide's (width, height); the grid has ``ceil(extent / tile_um)`` tiles
    along each axis. Raises EmptyTileGridError when no tile qualifies.
    """
    width_um, height_um = extent_um
    if not (tile_um > 0 and math.isfinite(tile_um)):
        raise ValueError(f"tile_um must be a positive finite number, got {tile_um!r}")
    if not (width_um > 0 and height_um > 0):
        raise ValueError(f"the slide extent must be positive, got {extent_um!r}")
    n_cols, n_rows = math.ceil(width_um / tile_um), math.ceil(height_um / tile_um)
    cols, rows = np.meshgrid(np.arange(n_cols), np.arange(n_rows))
    x0, y0 = cols * tile_um, rows * tile_um
    fractions = mask.fractions_of_boxes_um(x0, y0, x0 + tile_um, y0 + tile_um)
    rows_in, cols_in = np.nonzero(fractions >= min_fraction)
    if rows_in.size == 0:
        raise EmptyTileGridError(f"no {tile_um:g} µm tile has {min_fraction:.0%} tissue in the registered mask")
    return TileGrid(
        tile_um=float(tile_um),
        n_cols=int(n_cols),
        n_rows=int(n_rows),
        i=cols_in.astype(np.int32),
        j=rows_in.astype(np.int32),
        tissue_fraction=fractions[rows_in, cols_in].astype(np.float32),
    )
