"""The tumour's invasive front on the triage tile raster (WP-6.6, SPEC-05 §5.2 arm H1P, D22).

The front is where tumour tissue meets non-tumour tissue of the same section. A tumour edge that only
meets glass (the section border) is not front. Distances are Euclidean, from tile centres to the
nearest non-tumour tissue tile centre.
"""
import numpy as np
from scipy import ndimage


class FrontDistance:
    """Distance to the invasive front on a tile raster, readable at any slide position."""

    def __init__(self, distance_um: np.ndarray, tile_um: float):
        self.distance_um = distance_um
        self.tile_um = float(tile_um)
        self.has_front = bool(np.isfinite(distance_um).any())

    def at_um(self, x_um, y_um) -> np.ndarray:
        """Distance at each position, bilinear between tile centres (edge values beyond the outer centres). ``inf`` without a front."""
        x = np.asarray(x_um, dtype=np.float64)
        if not self.has_front:
            return np.full(x.shape, np.inf)
        coords = [np.asarray(y_um, dtype=np.float64) / self.tile_um - 0.5, x / self.tile_um - 0.5]
        return ndimage.map_coordinates(self.distance_um, coords, order=1, mode="nearest")


def front_distance(p_raster: np.ndarray, is_tumor_raster: np.ndarray, tile_um: float) -> FrontDistance:
    """``front_distance_um`` for every position of the tile raster.

    ``p_raster`` is NaN off tissue. The section is the tissue with its holes filled, so fat and lumina inside
    it count as non-tumour tissue and the glass beyond its border does not. A section with no non-tumour
    tissue has no front: every distance is ``inf``.
    """
    tissue = ~np.isnan(p_raster)
    tumor = np.asarray(is_tumor_raster, dtype=bool) & tissue
    section = ndimage.binary_fill_holes(tissue)
    non_tumor = section & ~tumor
    if not non_tumor.any():
        return FrontDistance(np.full(p_raster.shape, np.inf), tile_um)
    # The transform measures the distance of each nonzero cell to the nearest zero cell: non-tumour tissue is zero.
    distance = ndimage.distance_transform_edt(~non_tumor, sampling=tile_um)
    return FrontDistance(distance, tile_um)
