"""Server-side geometry validation for hotspot ROI edits (SPEC-03 §5.3.2, SPEC-05 §6).

Rules, with limits from configs/safety.yaml ``geometry``:
- Closed polygon with min_vertices..max_vertices vertices.
- Finite, non-negative coordinates within slide bounds (when the slide size is known).
- Simple (non-self-intersecting) boundary.
- Area within min_area_mm2..max_area_mm2.
- No overlap between active hotspots.
"""
import math
from typing import Sequence

from fastapi import HTTPException, status
from shapely.geometry import Polygon

from app.core.pipeline_config import get_pipeline_config

UM2_PER_MM2 = 1e6


def _reject(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _open_ring(coords: Sequence[Sequence[float]]) -> list[Sequence[float]]:
    """Vertices without the repeated closing point, if the client sent one."""
    ring = list(coords)
    if len(ring) > 1 and list(ring[0]) == list(ring[-1]):
        ring = ring[:-1]
    return ring


def validate_polygon_geometry(
    coords: Sequence[Sequence[float]],
    slide_bounds_um: tuple[float, float] | None = None,
) -> float:
    """
    Validates polygon geometry against architectural safety constraints (SPEC-03 §5.3.2).
    Returns the polygon area in mm².
    """
    geom_cfg = get_pipeline_config().safety.geometry
    ring = _open_ring(coords or [])

    if len(ring) < geom_cfg.min_vertices:
        raise _reject(f"Polygon must have at least {geom_cfg.min_vertices} vertices, got {len(ring)}.")
    if len(ring) > geom_cfg.max_vertices:
        raise _reject(
            f"Polygon vertex count {len(ring)} exceeds the maximum allowed limit of {geom_cfg.max_vertices} vertices."
        )

    for idx, pt in enumerate(ring):
        if len(pt) != 2:
            raise _reject(f"Vertex {idx} is invalid: expected 2D coordinate [x, y], got {list(pt)}.")
        x, y = float(pt[0]), float(pt[1])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise _reject(f"Vertex {idx} coordinates ({x}, {y}) must be finite.")
        if x < 0.0 or y < 0.0:
            raise _reject(f"Vertex {idx} coordinates ({x}, {y}) cannot be negative.")
        if slide_bounds_um is not None:
            max_x, max_y = slide_bounds_um
            if x > max_x or y > max_y:
                raise _reject(f"Vertex {idx} ({x}, {y}) is outside slide bounds ({max_x:.1f}, {max_y:.1f}) µm.")

    poly = Polygon(ring)
    if not poly.is_valid:
        raise _reject("Polygon geometry is invalid or self-intersecting.")

    area_mm2 = poly.area / UM2_PER_MM2
    if not geom_cfg.min_area_mm2 <= area_mm2 <= geom_cfg.max_area_mm2:
        raise _reject(
            f"Polygon area {area_mm2:.4f} mm² is outside the allowed range "
            f"{geom_cfg.min_area_mm2}–{geom_cfg.max_area_mm2} mm²."
        )
    return area_mm2


def validate_hotspots_non_overlapping(hotspots: Sequence[dict]) -> None:
    """
    Validates that active hotspots do not overlap with each other (SPEC-03 §5.3.2, SPEC-05 §6).
    Every active hotspot must carry a valid polygon; one that does not is itself a 422.
    """
    geom_cfg = get_pipeline_config().safety.geometry
    polys: list[tuple[str, Polygon]] = []
    for h in hotspots:
        if h.get("excluded", False):
            continue
        hid = str(h.get("id"))
        ring = _open_ring(h.get("polygon_um") or [])
        if len(ring) < 3:
            raise _reject(f"Hotspot '{hid}' has no valid polygon.")
        poly = Polygon(ring)
        if not poly.is_valid:
            raise _reject(f"Hotspot '{hid}' polygon is invalid or self-intersecting.")
        polys.append((hid, poly))

    for i, (id_a, poly_a) in enumerate(polys):
        for id_b, poly_b in polys[i + 1:]:
            overlap_um2 = poly_a.intersection(poly_b).area
            if overlap_um2 > geom_cfg.max_overlap_area_um2:
                raise _reject(
                    f"Hotspots cannot overlap: '{id_a}' and '{id_b}' intersect with overlap area "
                    f"{overlap_um2 / UM2_PER_MM2:.4f} mm²."
                )
