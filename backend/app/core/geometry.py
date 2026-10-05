"""Server-side geometry validation for hotspot ROI edits (SPEC-03 §5.3.2, SPEC-05 §5.5, §6).

Rules, with limits from configs/safety.yaml ``geometry``:
- Closed polygon with min_vertices..max_vertices vertices.
- Finite, non-negative coordinates within slide bounds (when the slide size is known).
- Simple (non-self-intersecting) boundary.
- Area within min_area_mm2..max_area_mm2.
- No overlap between active hotspots (mitre-buffered intersection check).
"""
import math
from typing import Any, Sequence

from fastapi import HTTPException, status
from shapely.geometry import Polygon

from app.core.pipeline_config import get_pipeline_config
from pipeline.hotspots_v6 import circles_overlapping

UM2_PER_MM2 = 1e6


class HotspotOverlapError(HTTPException):
    """Raised when active hotspots overlap (SPEC-05 §5.5, AC4)."""

    def __init__(self, ids: list[list[str]], status_code: int = status.HTTP_422_UNPROCESSABLE_CONTENT):
        self.ids = ids
        super().__init__(
            status_code=status_code,
            detail=f"Hotspots cannot overlap: {ids}",
        )


class ContractHTTPError(HTTPException):
    """A refusal that answers with a contract body ``{"error": <code>, ..., "detail": <text>}`` (main.py handler)."""

    def __init__(self, status_code: int, error: str, detail: str, **extra: Any):
        super().__init__(status_code=status_code, detail=detail)
        self.body = {"error": error, **extra, "detail": detail}


class InvalidSiteError(ContractHTTPError):
    """A pathologist's HPF site cannot be used (docs/contracts/triage_v6.md: ``invalid_site``)."""

    def __init__(self, site_id: str | None, reason: str, detail: str):
        super().__init__(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_site", detail, id=site_id, reason=reason)


class TooManySitesError(ContractHTTPError):
    """More active HPF sites than the target (``too_many_sites``)."""

    def __init__(self, hpf_target: int, n_active: int):
        super().__init__(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "too_many_sites",
            f"{n_active} active HPF sites exceed the target of {hpf_target}.", hpf_target=hpf_target,
        )


class PolygonValidationError(HTTPException):
    """Raised when an edited polygon violates geometric constraints (SPEC-05 §5.5)."""

    def __init__(
        self,
        reason: str,
        message: str,
        polygon_id: str | None = None,
        status_code: int = status.HTTP_422_UNPROCESSABLE_CONTENT,
    ):
        self.reason = reason
        self.polygon_id = polygon_id
        super().__init__(status_code=status_code, detail=message)


def _open_ring(coords: Sequence[Sequence[float]]) -> list[Sequence[float]]:
    """Vertices without the repeated closing point, if the client sent one."""
    ring = list(coords)
    if len(ring) > 1 and list(ring[0]) == list(ring[-1]):
        ring = ring[:-1]
    return ring


def validate_polygon_geometry(
    coords: Sequence[Sequence[float]],
    slide_bounds_um: tuple[float, float] | None = None,
    polygon_id: str | None = None,
) -> float:
    """
    Validates polygon geometry against architectural safety constraints (SPEC-03 §5.3.2, SPEC-05 §5.5).
    Returns the polygon area in mm².
    """
    geom_cfg = get_pipeline_config().safety.geometry
    raw_list = list(coords or [])
    if len(raw_list) < 3:
        raise PolygonValidationError(
            "too_few_vertices",
            f"Polygon must have at least {geom_cfg.min_vertices} vertices, got {len(raw_list)}.",
            polygon_id=polygon_id,
        )

    ring = _open_ring(raw_list)

    if len(ring) < geom_cfg.min_vertices:
        raise PolygonValidationError(
            "too_few_vertices",
            f"Polygon must have at least {geom_cfg.min_vertices} vertices, got {len(ring)}.",
            polygon_id=polygon_id,
        )
    if len(ring) > geom_cfg.max_vertices:
        raise PolygonValidationError(
            "too_many_vertices",
            f"Polygon vertex count {len(ring)} exceeds the maximum allowed limit of {geom_cfg.max_vertices} vertices.",
            polygon_id=polygon_id,
        )

    for idx, pt in enumerate(ring):
        if len(pt) != 2:
            raise PolygonValidationError(
                "out_of_bounds",
                f"Vertex {idx} is invalid: expected 2D coordinate [x, y], got {list(pt)}.",
                polygon_id=polygon_id,
            )
        x, y = float(pt[0]), float(pt[1])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise PolygonValidationError(
                "out_of_bounds",
                f"Vertex {idx} coordinates ({x}, {y}) must be finite.",
                polygon_id=polygon_id,
            )
        if x < 0.0 or y < 0.0:
            raise PolygonValidationError(
                "out_of_bounds",
                f"Vertex {idx} coordinates ({x}, {y}) cannot be negative.",
                polygon_id=polygon_id,
            )
        if slide_bounds_um is not None:
            max_x, max_y = slide_bounds_um
            if x > max_x or y > max_y:
                raise PolygonValidationError(
                    "out_of_bounds",
                    f"Vertex {idx} ({x}, {y}) is outside slide bounds ({max_x:.1f}, {max_y:.1f}) µm.",
                    polygon_id=polygon_id,
                )

    poly = Polygon(ring)
    if not poly.is_valid:
        msg = (
            f"Hotspot '{polygon_id}' polygon is invalid or self-intersecting."
            if polygon_id
            else "Polygon geometry is invalid or self-intersecting."
        )
        raise PolygonValidationError("self_intersecting", msg, polygon_id=polygon_id)

    area_mm2 = poly.area / UM2_PER_MM2
    if not (geom_cfg.min_area_mm2 <= area_mm2 <= geom_cfg.max_area_mm2):
        raise PolygonValidationError(
            "area_out_of_range",
            f"Polygon area {area_mm2:.4f} mm² is outside the allowed range {geom_cfg.min_area_mm2}–{geom_cfg.max_area_mm2} mm².",
            polygon_id=polygon_id,
        )

    return area_mm2


def validate_hotspots_non_overlapping(
    hotspots: Sequence[dict],
    gap_um: float = 0.0,
    status_code: int = status.HTTP_422_UNPROCESSABLE_CONTENT,
) -> None:
    """
    Validates that active hotspots do not overlap with each other (SPEC-03 §5.3.2, SPEC-05 §5.5, §6).
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
            raise PolygonValidationError(
                "too_few_vertices",
                f"Hotspot '{hid}' has no valid polygon.",
                polygon_id=hid,
                status_code=status_code,
            )
        poly = Polygon(ring)
        if not poly.is_valid:
            raise PolygonValidationError(
                "self_intersecting",
                f"Hotspot '{hid}' polygon is invalid or self-intersecting.",
                polygon_id=hid,
                status_code=status_code,
            )
        if gap_um > 0.0:
            poly = poly.buffer(gap_um / 2.0, join_style="mitre")
        polys.append((hid, poly))

    collisions: list[list[str]] = []
    for i, (id_a, poly_a) in enumerate(polys):
        for id_b, poly_b in polys[i + 1:]:
            inter_area = poly_a.intersection(poly_b).area
            if inter_area > geom_cfg.max_overlap_area_um2:
                collisions.append([id_a, id_b])

    if collisions:
        raise HotspotOverlapError(ids=collisions, status_code=status_code)


def validate_hpf_sites(
    hotspots: Sequence[dict],
    *,
    diameter_um: float,
    gap_um: float,
    hpf_target: int,
    slide_bounds_um: tuple[float, float] | None,
    status_code: int = status.HTTP_422_UNPROCESSABLE_CONTENT,
) -> None:
    """Server-side checks on the effective HPF sites (SPEC-05 §5.5, D22). Excluded sites are not checked.

    - Every circle lies inside the slide (when its size is known): ``InvalidSiteError`` ``out_of_bounds``.
    - Active circles do not overlap (``HotspotOverlapError``); their frames may.
    - No more than ``hpf_target`` sites are active (``TooManySitesError``).
    """
    active = [h for h in hotspots if not h.get("excluded", False)]
    radius = diameter_um / 2.0
    for h in active:
        centre = h.get("center_um")
        if centre is None or len(centre) != 2:
            raise InvalidSiteError(h.get("id"), "no_center", f"Site '{h.get('id')}' has no center_um.")
        x, y = float(centre[0]), float(centre[1])
        inside = math.isfinite(x) and math.isfinite(y) and x - radius >= 0.0 and y - radius >= 0.0
        if inside and slide_bounds_um is not None:
            inside = x + radius <= slide_bounds_um[0] and y + radius <= slide_bounds_um[1]
        if not inside:
            raise InvalidSiteError(
                h.get("id"), "out_of_bounds",
                f"The circle at ({x}, {y}) µm is not inside the slide.",
            )
    collisions = circles_overlapping(active, diameter_um, gap_um)
    if collisions:
        raise HotspotOverlapError(ids=collisions, status_code=status_code)
    if len(active) > hpf_target:
        raise TooManySitesError(hpf_target, len(active))
