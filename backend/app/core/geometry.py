"""Server-side geometry validation for hotspot ROI edits (SPEC-03 §5.3.2, SPEC-05 §6).

Rules:
- Closed polygon with >= min_vertices (3) and <= max_vertices (64).
- Within slide bounds (non-negative, <= width/height if available).
- Area within configured bounds (>= min_area_mm2).
- Valid non-self-intersecting boundary.
- No overlap between active hotspots.
"""
from typing import Sequence
from fastapi import HTTPException, status
import numpy as np
from shapely.geometry import Polygon

from app.core.pipeline_config import get_pipeline_config


def compute_polygon_area_mm2(coords: Sequence[Sequence[float]]) -> float:
    """Computes polygon area in square millimeters from micrometer coordinates [[x, y], ...]."""
    if not coords or len(coords) < 3:
        return 0.0
    pts = np.array(coords, dtype=float)
    x = pts[:, 0]
    y = pts[:, 1]
    area_um2 = 0.5 * np.abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))
    return round(float(area_um2 / 1e6), 4)


def validate_polygon_geometry(
    coords: Sequence[Sequence[float]],
    min_area_mm2: float | None = None,
    slide_bounds_um: tuple[float, float] | None = None,
) -> float:
    """
    Validates polygon geometry against architectural safety constraints (SPEC-03 §5.3.2).
    Returns calculated area in mm².
    """
    geom_cfg = get_pipeline_config().safety.geometry

    if not coords or len(coords) < geom_cfg.min_vertices:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Polygon must have at least {geom_cfg.min_vertices} vertices, got {len(coords) if coords else 0}."
        )

    if len(coords) > geom_cfg.max_vertices:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Polygon vertex count {len(coords)} exceeds the maximum allowed limit of {geom_cfg.max_vertices} vertices."
        )

    # Validate coordinate values and slide bounds
    for idx, pt in enumerate(coords):
        if len(pt) < 2:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Vertex {idx} is invalid: expected 2D coordinate [x, y], got {pt}."
            )
        x, y = float(pt[0]), float(pt[1])
        if x < 0.0 or y < 0.0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Vertex {idx} coordinates ({x}, {y}) cannot be negative."
            )
        if slide_bounds_um is not None:
            try:
                max_x, max_y = float(slide_bounds_um[0]), float(slide_bounds_um[1])
                if x > max_x or y > max_y:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Vertex {idx} ({x}, {y}) is outside slide bounds ({max_x:.1f}, {max_y:.1f}) µm."
                    )
            except (TypeError, ValueError):
                pass

    # Validate simplicity / non-self-intersection
    try:
        poly = Polygon(coords)
        if not poly.is_valid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Polygon geometry is invalid or self-intersecting."
            )
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid polygon geometry: {e}"
        )

    calc_area_mm2 = compute_polygon_area_mm2(coords)
    if min_area_mm2 is not None and calc_area_mm2 < min_area_mm2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Polygon area {calc_area_mm2:.4f} mm² is below the minimum required area of {min_area_mm2} mm²."
        )

    return calc_area_mm2


def validate_hotspots_non_overlapping(hotspots: Sequence[dict]) -> None:
    """
    Validates that active hotspots do not overlap with each other (SPEC-03 §5.3.2, SPEC-05 §6).
    """
    geom_cfg = get_pipeline_config().safety.geometry
    active = [h for h in hotspots if not h.get("excluded", False) and h.get("polygon_um")]

    shapely_polys: list[tuple[str, Polygon]] = []
    for h in active:
        hid = str(h.get("id", "unnamed"))
        poly_coords = h["polygon_um"]
        if len(poly_coords) >= geom_cfg.min_vertices:
            try:
                poly = Polygon(poly_coords)
                if poly.is_valid:
                    shapely_polys.append((hid, poly))
            except Exception:
                pass

    for i in range(len(shapely_polys)):
        id_a, poly_a = shapely_polys[i]
        for j in range(i + 1, len(shapely_polys)):
            id_b, poly_b = shapely_polys[j]
            if poly_a.intersects(poly_b):
                inter = poly_a.intersection(poly_b)
                if inter.area > geom_cfg.overlap_iou_threshold:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Hotspots cannot overlap: '{id_a}' and '{id_b}' intersect with overlap area {inter.area / 1e6:.4f} mm²."
                    )
