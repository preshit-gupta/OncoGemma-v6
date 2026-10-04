"""
Stage 4 HPF placement (SPEC-06 §5.8): one field per hotspot window, its disk inside the window.

Owner decision (2026-10-04): a hotspot window holds at most one HPF and the whole disk lies inside
it. A 600 µm window and a 262 µm radius leave the centre a 76 µm square, so the choice inside a
window is small; the windows themselves (SPEC-05 §5, non-overlapping) decide where the fields go.

Inside each window, candidate centres lie on a ``centre_step_um`` lattice anchored at the window's
centre. A centre is eligible when its disk is inside the slide and the window, at least
``min_tissue_fraction`` tissue and ``min_tumor_fraction`` tumour (specimen profile), and at least
``min_separation_um`` from every placed field. The eligible centre with the most counted candidates
in its disk wins (then the higher tumour fraction, then the one nearer the window centre). Windows
are taken by priority, so with more windows than ``count`` the best-ranked ones get the fields.
Fewer than ``count`` fields is a result (``hpf_count_lt_10``, pipeline/scoring.py).
"""
import math
from typing import Any, Dict, List, Tuple

from shapely.geometry import Point, Polygon

from app.core.pipeline_config import MitosisHpfConfig
from pipeline.mitosis_gate import TumorGate
from pipeline.tissue_mask import TissueMask

# A polygon has at least three vertices.
MIN_POLYGON_VERTICES = 3
# Slack for floating point when a disk exactly fits its window or two fields exactly touch.
FIT_TOLERANCE_UM = 1e-6
# Decimals of the tumour fraction used to rank centres within a window.
TIE_DECIMALS = 3


def window_centres(polygon_um: List[List[float]], radius_um: float, step_um: float) -> List[Tuple[float, float]]:
    """Lattice points (``step_um``, anchored at the window's centroid) where a ``radius_um`` disk lies inside the polygon."""
    if len(polygon_um) < MIN_POLYGON_VERTICES:
        raise ValueError(f"a hotspot polygon needs at least {MIN_POLYGON_VERTICES} vertices, got {len(polygon_um)}")
    polygon = Polygon(polygon_um)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    eroded = polygon.buffer(-(radius_um - FIT_TOLERANCE_UM))  # centres whose disk is inside the polygon
    if eroded.is_empty:
        return []
    anchor = polygon.centroid
    x0, y0, x1, y1 = eroded.bounds
    kx0, kx1 = math.floor((x0 - anchor.x) / step_um), math.ceil((x1 - anchor.x) / step_um)
    ky0, ky1 = math.floor((y0 - anchor.y) / step_um), math.ceil((y1 - anchor.y) / step_um)
    centres = []
    for ky in range(ky0, ky1 + 1):
        for kx in range(kx0, kx1 + 1):
            x, y = anchor.x + kx * step_um, anchor.y + ky * step_um
            if eroded.covers(Point(x, y)):
                centres.append((x, y))
    return centres


def place_hpfs(
    candidates: List[Dict[str, Any]],
    hotspots: List[Tuple[List[List[float]], float]],
    *,
    tissue: TissueMask,
    tumor: TumorGate,
    slide_dimensions_um: Tuple[float, float],
    cfg: MitosisHpfConfig,
    min_tissue_fraction: float,
    min_tumor_fraction: float,
) -> List[Dict[str, Any]]:
    """
    Stage 4's HPFs: at most one per hotspot window, by window priority, at most ``cfg.count``.

    ``hotspots`` are (polygon_um, priority) pairs; ``candidates`` carry ``centroid_um`` and ``counted``.
    Each field reports its ``count`` of counted candidates, ``tissue_coverage`` and ``tumor_fraction``.
    """
    if not hotspots:
        raise ValueError("HPFs are placed inside confirmed hotspots; there are none")
    r = cfg.radius_um
    width_um, height_um = slide_dimensions_um
    counted = [tuple(c["centroid_um"]) for c in candidates if c["counted"]]

    placed: List[Dict[str, Any]] = []
    for polygon, _priority in sorted(hotspots, key=lambda h: h[1], reverse=True):
        if len(placed) >= cfg.count:
            break
        anchor = Polygon(polygon).centroid
        best = None
        for cx, cy in window_centres(polygon, r, cfg.centre_step_um):
            if cx - r < 0.0 or cy - r < 0.0 or cx + r > width_um or cy + r > height_um:
                continue
            if any(math.dist((cx, cy), h["center_um"]) < cfg.min_separation_um - FIT_TOLERANCE_UM for h in placed):
                continue
            tissue_coverage = tissue.fraction_in_disk_um(cx, cy, r)
            if tissue_coverage < min_tissue_fraction:
                continue
            tumor_fraction = tumor.tumor_fraction_in_disk(cx, cy, r)
            if tumor_fraction < min_tumor_fraction:
                continue
            n = sum(1 for p in counted if math.dist(p, (cx, cy)) <= r)
            # Fractions are compared at TIE_DECIMALS so area-sampling noise never outranks the window centre.
            key = (n, round(tumor_fraction, TIE_DECIMALS), -math.dist((cx, cy), (anchor.x, anchor.y)), -cy, -cx)
            if best is None or key > best[0]:
                best = (key, cx, cy, n, tissue_coverage, tumor_fraction)
        if best is not None:
            _, cx, cy, n, tissue_coverage, tumor_fraction = best
            placed.append({
                "seq": len(placed) + 1,
                "center_um": [float(cx), float(cy)],
                "radius_um": float(r),
                "count": n,
                "tissue_coverage": float(tissue_coverage),
                "tumor_fraction": float(tumor_fraction),
                "source": "model",
            })
    return placed
