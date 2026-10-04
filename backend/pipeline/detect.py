"""
OncoGemma Stage 4 - hotspot tiling, cross-tile NMS in micrometres and the candidates' review images.
The detector itself runs through the model gateway (worker/mitosis.py).
"""
import io
import math
from dataclasses import dataclass
from typing import List, Tuple, Dict, Any
import numpy as np
from PIL import Image
from shapely.geometry import Polygon, box

from app.core.pipeline_config import MitosisReviewCropsConfig
from pipeline.slide_io import SlideReader, read_region_at_mpp
from pipeline.tissue_mask import TissueMask

# A polygon has at least three vertices.
MIN_POLYGON_VERTICES = 3


def nms_priority(candidate: Dict[str, Any]) -> float:
    """The probability NMS orders by: the classifier's when there is one, otherwise the detector's (SPEC-06 §5.7)."""
    p = candidate.get("p_b") if candidate.get("p_b") is not None else candidate.get("p_a")
    if p is None:
        raise ValueError(f"candidate {candidate.get('id')} has neither p_b nor p_a; NMS needs a model probability")
    return float(p)


def apply_global_nms(
    candidates: List[Dict[str, Any]],
    *,
    nms_radius_um: float,
) -> List[Dict[str, Any]]:
    """
    Greedy non-maximum suppression in micrometres, run once after the decisions (SPEC-06 §5.7).

    Candidates are visited by ``p_b ?? p_a`` descending, with no rank by decision; one closer than
    ``nms_radius_um`` to a kept candidate is suppressed. The two chromosome groups of one dividing
    cell are closer than the radius, so they count once (§3).
    """
    kept: List[Dict[str, Any]] = []
    for cand in sorted(candidates, key=nms_priority, reverse=True):
        cx, cy = cand["centroid_um"]
        if all(math.hypot(cx - kx, cy - ky) >= nms_radius_um for kx, ky in (k["centroid_um"] for k in kept)):
            kept.append(cand)
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


@dataclass(frozen=True)
class ReviewCrops:
    """The two review images of a candidate (contract mitosis_v6), PNG, raw colour."""

    crop_png: bytes
    context_png: bytes


def review_crop_blob(case_id: str, candidate_id: str, kind: str) -> str:
    """Artifacts-bucket blob of a candidate's review image; ``kind`` is ``crop`` or ``context``."""
    if kind not in ("crop", "context"):
        raise ValueError(f"unknown review image kind {kind!r}")
    return f"cases/{case_id}/mitosis/crops/{candidate_id}_{kind}.png"


def candidate_review_crops(reader: SlideReader, cx_um: float, cy_um: float, cfg: MitosisReviewCropsConfig) -> ReviewCrops:
    """The crop (``crop_um`` at ``crop_mpp``) and context (``context_um`` at ``context_mpp``) centred on the
    candidate, as scanned. A view overhanging the slide edge is padded with white, so the candidate stays centred.
    They are independent of what any model was shown."""
    def png(size_um: float, mpp: float) -> bytes:
        region = read_region_at_mpp(reader, cx_um - size_um / 2, cy_um - size_um / 2, size_um, size_um, mpp)
        buffer = io.BytesIO()
        Image.fromarray(region.rgb).save(buffer, format="PNG")
        return buffer.getvalue()

    return ReviewCrops(png(cfg.crop_um, cfg.crop_mpp), png(cfg.context_um, cfg.context_mpp))
