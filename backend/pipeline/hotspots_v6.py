"""Stage 3 Hotspot Windows, Chebyshev Selection & Validation (SPEC-05 §5).

Implements:
1. Regular candidate window lattice over tumor bounding box.
2. Tissue and tumor coverage fraction filters.
3. Scoring and ranking under Arm H1 (mean calibrated tumor probability).
4. Pure Chebyshev distance greedy selection with zero padding (AC3, AC5).
5. Polygon mitre-buffered overlap validation (AC4).

No hardcoded clinical or geometric constants: all configuration is passed in via
HotspotsConfig (configs/specimen_profiles.yaml) and SafetyConfig (configs/safety.yaml).
"""
from dataclasses import dataclass
import math
from typing import Sequence

import numpy as np
from shapely.geometry import Polygon

from pipeline.tile_grid import TileGrid

UM2_PER_MM2 = 1e6


@dataclass(frozen=True)
class HotspotWindow:
    """An axis-aligned square hotspot window (SPEC-05 §5.1, §5.3)."""

    id: str
    cx: float
    cy: float
    window_um: float
    rank: int | None
    rank_score: float
    score_kind: str
    tumor_fraction: float | None  # None for candidates that are not lattice windows (SPEC-05 §5.1)
    prescan_expected: float | None = None
    source: str = "model"
    excluded: bool = False
    exclude_reason: str | None = None
    candidate_id: str | None = None  # the candidate this window was selected from

    @property
    def polygon_um(self) -> list[list[float]]:
        """Closed 5-vertex polygon ring: [x, y] in micrometers."""
        half = self.window_um / 2.0
        x0, y0 = self.cx - half, self.cy - half
        x1, y1 = self.cx + half, self.cy + half
        return [
            [x0, y0],
            [x1, y0],
            [x1, y1],
            [x0, y1],
            [x0, y0],
        ]

    @property
    def area_mm2(self) -> float:
        return (self.window_um * self.window_um) / UM2_PER_MM2

    def to_dict(self) -> dict:
        """Serializes to the Hotspot schema in docs/contracts/triage_v6.md."""
        return {
            "id": self.id,
            "polygon_um": self.polygon_um,
            "window_um": self.window_um,
            "rank": self.rank,
            "rank_score": self.rank_score,
            "score_kind": self.score_kind,
            "tumor_fraction": self.tumor_fraction,
            "prescan_expected": self.prescan_expected,
            "source": self.source,
            "excluded": self.excluded,
            "exclude_reason": self.exclude_reason,
            "area_mm2": self.area_mm2,
        }


def generate_candidate_lattice(
    tumor_bbox_um: tuple[float, float, float, float],
    step_um: float,
) -> list[tuple[float, float]]:
    """Generates (cx, cy) window centers on a regular lattice with step_um.

    Candidate windows are centred on a regular lattice with step w/4 (lattice_step_um)
    over the bounding box of the tumour mask (SPEC-05 §5.1).

    Args:
        tumor_bbox_um: (x_min, y_min, x_max, y_max) in micrometers.
        step_um: lattice step (w / 4 from HotspotsConfig).

    Returns:
        List of (cx, cy) coordinates.
    """
    x_min, y_min, x_max, y_max = tumor_bbox_um
    if step_um <= 0:
        raise ValueError(f"lattice step must be positive, got {step_um}")
    if x_max < x_min or y_max < y_min:
        return []

    # Generate centers spanning the bounding box at step_um intervals
    xs = np.arange(x_min, x_max + (step_um * 0.5), step_um, dtype=np.float64)
    ys = np.arange(y_min, y_max + (step_um * 0.5), step_um, dtype=np.float64)

    if xs.size == 0 or ys.size == 0:
        return []

    grid_x, grid_y = np.meshgrid(xs, ys)
    return list(zip(grid_x.ravel().tolist(), grid_y.ravel().tolist()))


def compute_window_tumor_metrics_from_grid(
    cx: float,
    cy: float,
    window_um: float,
    tile_grid: TileGrid,
    p_tumor_cal: np.ndarray,
    tau_tumor: float,
) -> tuple[float, float]:
    """Computes area-weighted tumor fraction and mean calibrated tumor probability.

    Args:
        cx, cy: Window center in micrometers.
        window_um: Window side length in micrometers.
        tile_grid: Slide TileGrid instance.
        p_tumor_cal: Calibrated tumor probability per tile in tile_grid (shape: [n_tiles]).
        tau_tumor: Calibrated tumor classification threshold from config.

    Returns:
        tuple (tumor_fraction, mean_p_tumor)
    """
    half = window_um / 2.0
    wx0, wy0 = cx - half, cy - half
    wx1, wy1 = cx + half, cy + half

    tile_size = tile_grid.tile_um
    tx0 = tile_grid.x_um
    ty0 = tile_grid.y_um
    tx1 = tx0 + tile_size
    ty1 = ty0 + tile_size

    # Intersection of each tile with window W
    ix0 = np.maximum(wx0, tx0)
    iy0 = np.maximum(wy0, ty0)
    ix1 = np.minimum(wx1, tx1)
    iy1 = np.minimum(wy1, ty1)

    inter_w = np.maximum(0.0, ix1 - ix0)
    inter_h = np.maximum(0.0, iy1 - iy0)
    inter_area = inter_w * inter_h

    intersecting = inter_area > 0.0
    if not np.any(intersecting):
        return 0.0, 0.0

    areas = inter_area[intersecting]
    probs = p_tumor_cal[intersecting]
    is_tumor = probs >= tau_tumor

    window_area = window_um * window_um
    tumor_area = np.sum(areas[is_tumor])
    tumor_fraction = float(tumor_area / window_area)

    total_tiled_area = np.sum(areas)
    if total_tiled_area > 0.0:
        mean_p_tumor = float(np.sum(areas * probs) / total_tiled_area)
    else:
        mean_p_tumor = 0.0

    return tumor_fraction, mean_p_tumor


def compute_window_tumor_metrics_from_prob_map(
    cx: float,
    cy: float,
    window_um: float,
    prob_grid: np.ndarray,
    origin_um: tuple[float, float],
    stride_um: float,
    tau_tumor: float,
) -> tuple[float, float]:
    """Computes tumor fraction and mean calibrated probability from a 2D probability grid.

    Used when working directly with a 2D probability array (e.g. synthetic test grids).
    """
    half = window_um / 2.0
    wx0, wy0 = cx - half, cy - half
    wx1, wy1 = cx + half, cy + half

    ox, oy = origin_um
    ny, nx = prob_grid.shape

    # Bounding index range in prob_grid
    col_min = max(0, int(math.floor((wx0 - ox) / stride_um)))
    col_max = min(nx, int(math.ceil((wx1 - ox) / stride_um)))
    row_min = max(0, int(math.floor((wy0 - oy) / stride_um)))
    row_max = min(ny, int(math.ceil((wy1 - oy) / stride_um)))

    if col_min >= col_max or row_min >= row_max:
        return 0.0, 0.0

    sub_probs = prob_grid[row_min:row_max, col_min:col_max]
    sub_cols = np.arange(col_min, col_max)
    sub_rows = np.arange(row_min, row_max)
    grid_c, grid_r = np.meshgrid(sub_cols, sub_rows)

    tx0 = ox + grid_c * stride_um
    ty0 = oy + grid_r * stride_um
    tx1 = tx0 + stride_um
    ty1 = ty0 + stride_um

    ix0 = np.maximum(wx0, tx0)
    iy0 = np.maximum(wy0, ty0)
    ix1 = np.minimum(wx1, tx1)
    iy1 = np.minimum(wy1, ty1)

    inter_w = np.maximum(0.0, ix1 - ix0)
    inter_h = np.maximum(0.0, iy1 - iy0)
    inter_area = inter_w * inter_h

    valid = (~np.isnan(sub_probs)) & (inter_area > 0.0)
    if not np.any(valid):
        return 0.0, 0.0

    areas = inter_area[valid]
    probs = sub_probs[valid]
    is_tumor = probs >= tau_tumor

    window_area = window_um * window_um
    tumor_area = np.sum(areas[is_tumor])
    tumor_fraction = float(tumor_area / window_area)

    total_area = np.sum(areas)
    mean_p = float(np.sum(areas * probs) / total_area) if total_area > 0 else 0.0
    return tumor_fraction, mean_p


def select_hotspots(
    cands: Sequence[HotspotWindow],
    k_max: int,
    w: float,
    gap: float,
) -> list[HotspotWindow]:
    """Greedy hotspot selection with hard Chebyshev distance non-overlap (SPEC-05 §5.3).

    For two square windows of side w with centers (cx_a, cy_a) and (cx_b, cy_b),
    they do not overlap with safety margin gap if and only if:
        max(|cx_a - cx_b|, |cy_a - cy_b|) >= w + gap

    Purity constraint (AC5):
        If fewer than k_max windows qualify, returns exactly K < k_max.
        Never pads with lower-threshold or unconfirmed windows.

    Args:
        cands: Eligible candidate windows.
        k_max: Maximum number of hotspots to select.
        w: Window side length in micrometers.
        gap: Safety margin gap in micrometers.

    Returns:
        List of selected HotspotWindow objects sorted by rank.
    """
    selected: list[HotspotWindow] = []
    # Rank descending by score, breaking ties by tumor_fraction; a window without one ranks
    # after those with one at the same score.
    sorted_cands = sorted(
        cands,
        key=lambda c: (c.rank_score, c.tumor_fraction is not None, c.tumor_fraction or 0.0),
        reverse=True,
    )

    for c in sorted_cands:
        # Chebyshev non-overlap: exact for equal axis-aligned squares
        if all(max(abs(c.cx - s.cx), abs(c.cy - s.cy)) >= (w + gap) for s in selected):
            rank_idx = len(selected) + 1
            ranked_window = HotspotWindow(
                id=f"hs_{rank_idx:02d}",
                cx=c.cx,
                cy=c.cy,
                window_um=w,
                rank=rank_idx,
                rank_score=c.rank_score,
                score_kind=c.score_kind,
                tumor_fraction=c.tumor_fraction,
                prescan_expected=c.prescan_expected,
                source="model",
                excluded=False,
                exclude_reason=None,
                candidate_id=c.id,
            )
            selected.append(ranked_window)
            if len(selected) == k_max:
                break

    return selected


def validate_hotspots_mitre_overlap(
    hotspots: Sequence[dict],
    gap_um: float = 0.0,
) -> list[list[str]]:
    """Checks for polygon collisions using mitre-buffered intersection (SPEC-05 §5.5).

    Formula:
        a.buffer(gap/2, join_style="mitre").intersection(b.buffer(gap/2, join_style="mitre")).area == 0

    Args:
        hotspots: Sequence of hotspot dictionaries containing 'id', 'polygon_um', and optional 'excluded'.
        gap_um: Safety gap in micrometers.

    Returns:
        List of colliding pairs [[id_a, id_b], ...]. If empty, no collisions exist.

    Raises:
        ValueError: an active hotspot has fewer than 3 vertices or an invalid polygon.
    """
    polys: list[tuple[str, Polygon]] = []
    for h in hotspots:
        if h.get("excluded", False):
            continue
        hid = str(h.get("id"))
        coords = h.get("polygon_um") or []
        ring = list(coords)
        if len(ring) > 1 and list(ring[0]) == list(ring[-1]):
            ring = ring[:-1]
        if len(ring) < 3:
            raise ValueError(f"hotspot {hid!r} has fewer than 3 vertices")
        poly = Polygon(ring)
        if not poly.is_valid:
            raise ValueError(f"hotspot {hid!r} has an invalid or self-intersecting polygon")
        if gap_um > 0.0:
            poly = poly.buffer(gap_um / 2.0, join_style="mitre")
        polys.append((hid, poly))

    collisions: list[list[str]] = []
    for i, (id_a, poly_a) in enumerate(polys):
        for id_b, poly_b in polys[i + 1:]:
            inter = poly_a.intersection(poly_b)
            if inter.area > 0.0:
                collisions.append([id_a, id_b])

    return collisions
