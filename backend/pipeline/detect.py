"""
OncoGemma Stage 4 - hotspot tiling and physical micrometer cross-tile NMS.
The detector itself runs through the model gateway (worker/mitosis.py).
"""
import math
from typing import List, Tuple, Dict, Any, Optional
import numpy as np


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
    tile_size_px: int = 1024,
    mpp: float | None = None,
    stride_px: int = 960,
    tissue_mask: Optional[np.ndarray] = None,
    slide_dimensions_um: Optional[Tuple[float, float]] = None,
    min_tissue_ratio: float = 0.20
) -> List[Dict[str, Any]]:
    """
    Generates 40x tile coordinates covering a hotspot polygon.
    Filters out tiles with less than min_tissue_ratio tissue coverage when tissue_mask is provided.
    Returns list of dicts with tile bounding box in pixels and base micrometers.
    """
    if not hotspot_polygon_um:
        return []

    if mpp is None or mpp <= 0:
        raise ValueError(f"Valid positive MPP is required for tile enumeration, got: {mpp}")

    xs = [p[0] for p in hotspot_polygon_um]
    ys = [p[1] for p in hotspot_polygon_um]
    min_x_um, max_x_um = min(xs), max(xs)
    min_y_um, max_y_um = min(ys), max(ys)

    tile_size_um = tile_size_px * mpp
    stride_um = stride_px * mpp

    mh, mw = (tissue_mask.shape if tissue_mask is not None else (0, 0))
    slide_w_um, slide_h_um = (slide_dimensions_um if slide_dimensions_um is not None else (float("inf"), float("inf")))

    # Prepare polygon geometry for intersection testing (#121)
    poly_geom = None
    if len(hotspot_polygon_um) >= 3:
        try:
            from shapely.geometry import Polygon, box
            poly_geom = Polygon(hotspot_polygon_um)
            if not poly_geom.is_valid:
                poly_geom = poly_geom.buffer(0)
        except Exception:
            poly_geom = None

    tiles = []
    curr_y = max(0.0, min_y_um)
    while curr_y <= max_y_um and curr_y < slide_h_um:
        curr_x = max(0.0, min_x_um)
        while curr_x <= max_x_um and curr_x < slide_w_um:
            # Check tile intersection with hotspot polygon (#121)
            if poly_geom is not None:
                from shapely.geometry import box
                tile_box = box(curr_x, curr_y, curr_x + tile_size_um, curr_y + tile_size_um)
                if not poly_geom.intersects(tile_box):
                    curr_x += stride_um
                    continue

            # Check tissue coverage if tissue_mask is available
            include_tile = True
            if tissue_mask is not None and mh > 0 and mw > 0 and slide_dimensions_um is not None:
                mx0 = max(0, min(mw - 1, int(round(curr_x / max(slide_w_um, 1.0) * (mw - 1)))))
                mx1 = max(0, min(mw - 1, int(round((curr_x + tile_size_um) / max(slide_w_um, 1.0) * (mw - 1)))))
                my0 = max(0, min(mh - 1, int(round(curr_y / max(slide_h_um, 1.0) * (mh - 1)))))
                my1 = max(0, min(mh - 1, int(round((curr_y + tile_size_um) / max(slide_h_um, 1.0) * (mh - 1)))))
                sub = tissue_mask[min(my0, my1):max(my0, my1) + 1, min(mx0, mx1):max(mx0, mx1) + 1]
                if sub.size > 0:
                    cov = (sub > 0).sum() / sub.size
                    if cov < min_tissue_ratio:
                        include_tile = False

            if include_tile:
                tiles.append({
                    "origin_um": [float(curr_x), float(curr_y)],
                    "size_um": [float(tile_size_um), float(tile_size_um)],
                    "origin_px": [int(curr_x / mpp), int(curr_y / mpp)],
                    "size_px": [tile_size_px, tile_size_px],
                })
            curr_x += stride_um
        curr_y += stride_um

    return tiles

