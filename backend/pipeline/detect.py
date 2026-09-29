"""
OncoGemma Stage 4 - hotspot tiling and physical micrometer cross-tile NMS.
The detector itself runs through the model gateway (worker/mitosis.py).
"""
import math
from typing import List, Tuple, Dict, Any
import numpy as np
from shapely.geometry import Polygon, box

from pipeline.tissue_mask import TissueMask

# A polygon has at least three vertices.
MIN_POLYGON_VERTICES = 3


def apply_global_nms(
    candidates: List[Dict[str, Any]],
    nms_radius_um: float = 20.0
) -> List[Dict[str, Any]]:
    """
    Applies greedy Non-Maximum Suppression across candidate mitotic figures in physical micrometer space.
    Suppresses lower-confidence detections within nms_radius_um (MIDOG challenge standard: 15-20 um cell diameter).
    """
    if not candidates:
        return []

    def _cand_priority(c: Dict[str, Any]) -> Tuple[int, float]:
        lbl = c.get("label", "unreviewed")
        rank = 2 if lbl == "mitosis" else (1 if lbl == "unreviewed" else 0)
        conf = float(c.get("ver_conf") if c.get("ver_conf") is not None else c.get("det_conf", 0.0))
        return (rank, conf)

    sorted_cands = sorted(candidates, key=_cand_priority, reverse=True)

    kept: List[Dict[str, Any]] = []
    kept_coords: List[Tuple[float, float]] = []

    for cand in sorted_cands:
        cx, cy = cand["centroid_um"]
        suppress = False
        for kx, ky in kept_coords:
            dist = math.hypot(cx - kx, cy - ky)
            if dist < nms_radius_um:
                suppress = True
                break
        if not suppress:
            kept.append(cand)
            kept_coords.append((cx, cy))

    return kept


def enumerate_hotspot_tiles(
    hotspot_polygon_um: List[List[float]],
    *,
    tile_um: float,
    stride_um: float,
    tissue: TissueMask,
    min_tissue_fraction: float,
) -> List[Dict[str, Any]]:
    """
    Tiles of side ``tile_um`` on a ``stride_um`` grid that starts at the hotspot's bounding box,
    keeping those that touch the hotspot polygon and are at least ``min_tissue_fraction`` tissue
    according to the registered tissue mask. A tile may overhang the slide edge (it is read with
    white padding). Returns dicts with the tile's ``origin_um`` and ``size_um``.
    """
    if len(hotspot_polygon_um) < MIN_POLYGON_VERTICES:
        raise ValueError(f"a hotspot polygon needs at least {MIN_POLYGON_VERTICES} vertices, got {len(hotspot_polygon_um)}")
    if not tile_um > 0 or not stride_um > 0:
        raise ValueError(f"tile_um and stride_um must be positive, got {tile_um} and {stride_um}")

    polygon = Polygon(hotspot_polygon_um)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)

    min_x_um, min_y_um, max_x_um, max_y_um = polygon.bounds
    slide_w_um, slide_h_um = tissue.extent_um
    xs = np.arange(max(0.0, min_x_um), max_x_um + 0.0, stride_um)
    ys = np.arange(max(0.0, min_y_um), max_y_um + 0.0, stride_um)
    xs = xs[xs < slide_w_um]
    ys = ys[ys < slide_h_um]
    if xs.size == 0 or ys.size == 0:
        return []
    grid_x, grid_y = np.meshgrid(xs, ys)
    grid_x, grid_y = grid_x.ravel(), grid_y.ravel()
    fractions = tissue.fractions_of_boxes_um(grid_x, grid_y, grid_x + tile_um, grid_y + tile_um)

    tiles = []
    for x, y, fraction in zip(grid_x, grid_y, fractions):
        if fraction < min_tissue_fraction:
            continue
        if not polygon.intersects(box(x, y, x + tile_um, y + tile_um)):
            continue
        tiles.append({
            "origin_um": [float(x), float(y)],
            "size_um": [float(tile_um), float(tile_um)],
        })
    return tiles


def hotspot_geometry(hotspot_polygon_um: List[List[float]]):
    """The hotspot polygon as a valid shapely geometry (SPEC-06 §5.1 sweep region)."""
    if len(hotspot_polygon_um) < MIN_POLYGON_VERTICES:
        raise ValueError(f"a hotspot polygon needs at least {MIN_POLYGON_VERTICES} vertices, got {len(hotspot_polygon_um)}")
    polygon = Polygon(hotspot_polygon_um)
    return polygon if polygon.is_valid else polygon.buffer(0)


def hotspot_region_um(
    geometry, margin_um: float, extent_um: Tuple[float, float]
) -> Tuple[float, float, float, float]:
    """The hotspot's bounding box grown by ``margin_um`` and clipped to the slide: (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = geometry.bounds
    width_um, height_um = extent_um
    return max(0.0, x0 - margin_um), max(0.0, y0 - margin_um), min(width_um, x1 + margin_um), min(height_um, y1 + margin_um)
