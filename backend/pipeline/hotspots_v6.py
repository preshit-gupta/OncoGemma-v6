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
from typing import Callable, Sequence

import numpy as np
from shapely.geometry import Polygon

from app.core.pipeline_config import HotspotsConfig
from pipeline.tissue_mask import TissueMask

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


def _overlaps(lo: np.ndarray, hi: np.ndarray, n_cells: int, origin_um: float, cell_um: float) -> np.ndarray:
    """(n_windows, n_cells): length of each window interval [lo, hi) inside each cell along one axis."""
    edges = origin_um + np.arange(n_cells + 1, dtype=np.float64) * cell_um
    return np.clip(np.minimum(hi[:, None], edges[None, 1:]) - np.maximum(lo[:, None], edges[None, :-1]), 0.0, None)


def window_tumor_metrics(
    centers_um: np.ndarray,
    window_um: float,
    p_raster: np.ndarray,
    is_tumor_raster: np.ndarray,
    cell_um: float,
    origin_um: tuple[float, float] = (0.0, 0.0),
    chunk: int = 2048,
) -> tuple[np.ndarray, np.ndarray]:
    """Tumour fraction and mean calibrated tumour probability of square windows (SPEC-05 §5.1, §5.2 H1).

    The tumour fraction is the share of the window's area covered by tumour cells (area-weighted);
    the mean is ``p_tumor_cal`` weighted by the area of each tissue cell inside the window (NaN
    when the window holds no tissue cell). ``p_raster`` is NaN off tissue. Windows are computed in
    chunks, each as two separable overlap matrices.
    """
    centers = np.asarray(centers_um, dtype=np.float64).reshape(-1, 2)
    n_rows, n_cols = p_raster.shape
    tissue = ~np.isnan(p_raster)
    p = np.where(tissue, p_raster, 0.0)
    tumor = (np.asarray(is_tumor_raster, dtype=bool) & tissue).astype(np.float64)
    tissue_f = tissue.astype(np.float64)
    half = window_um / 2.0
    tumor_fraction = np.empty(len(centers))
    mean_p = np.empty(len(centers))
    for k in range(0, len(centers), chunk):
        cx, cy = centers[k:k + chunk, 0], centers[k:k + chunk, 1]
        ox = _overlaps(cx - half, cx + half, n_cols, origin_um[0], cell_um)
        oy = _overlaps(cy - half, cy + half, n_rows, origin_um[1], cell_um)
        tumor_area = np.einsum("wi,wi->w", oy @ tumor, ox)
        tissue_area = np.einsum("wi,wi->w", oy @ tissue_f, ox)
        p_area = np.einsum("wi,wi->w", oy @ p, ox)
        tumor_fraction[k:k + chunk] = tumor_area / (window_um * window_um)
        with np.errstate(invalid="ignore", divide="ignore"):
            mean_p[k:k + chunk] = np.where(tissue_area > 0.0, p_area / tissue_area, np.nan)
    return tumor_fraction, mean_p


def compute_window_tumor_metrics_from_prob_map(
    cx: float,
    cy: float,
    window_um: float,
    prob_grid: np.ndarray,
    origin_um: tuple[float, float],
    stride_um: float,
    tau_tumor: float,
) -> tuple[float, float]:
    """Tumour fraction and mean probability of one window, tumour being ``prob_grid >= tau_tumor``."""
    is_tumor = np.nan_to_num(prob_grid, nan=-1.0) >= tau_tumor
    tf, mean_p = window_tumor_metrics(np.array([[cx, cy]]), window_um, prob_grid.astype(np.float64), is_tumor, stride_um, origin_um)
    return float(tf[0]), float(mean_p[0])


def score_lattice_windows(
    p_raster: np.ndarray,
    is_tumor_raster: np.ndarray,
    tile_um: float,
    tissue: TissueMask,
    extent_um: tuple[float, float],
    cfg: HotspotsConfig,
) -> list[HotspotWindow]:
    """The valid candidate windows of SPEC-05 §5.1, scored under arm H1 (§5.2).

    Windows of side ``cfg.window_um`` are centred on a lattice of step ``cfg.lattice_step_um``
    over the bounding box of the tumour mask (tile rasters from the slide origin). A window is
    valid when it lies inside the slide, its tissue fraction is at least ``cfg.min_tissue_fraction``
    and its tumour fraction at least ``cfg.min_tumor_fraction``. No tumour tile: no windows.
    """
    if cfg.ranking_arm != "H1":
        raise NotImplementedError(f"hotspot ranking arm {cfg.ranking_arm} needs the mitotic prescan (SPEC-05 §5.2)")
    rows, cols = np.nonzero(is_tumor_raster)
    if rows.size == 0:
        return []
    w = cfg.window_um
    bbox = (cols.min() * tile_um, rows.min() * tile_um, (cols.max() + 1) * tile_um, (rows.max() + 1) * tile_um)
    centers = np.array(generate_candidate_lattice(bbox, cfg.lattice_step_um), dtype=np.float64).reshape(-1, 2)
    width_um, height_um = extent_um
    half = w / 2.0
    inside = (
        (centers[:, 0] - half >= 0.0) & (centers[:, 0] + half <= width_um)
        & (centers[:, 1] - half >= 0.0) & (centers[:, 1] + half <= height_um)
    )
    centers = centers[inside]
    if len(centers) == 0:
        return []
    tissue_fraction = tissue.fractions_of_boxes_um(
        centers[:, 0] - half, centers[:, 1] - half, centers[:, 0] + half, centers[:, 1] + half
    )
    tumor_fraction, mean_p = window_tumor_metrics(centers, w, p_raster, is_tumor_raster, tile_um)
    valid = (tissue_fraction >= cfg.min_tissue_fraction) & (tumor_fraction >= cfg.min_tumor_fraction)
    return [
        HotspotWindow(
            id=f"win_{n:05d}",
            cx=float(centers[n, 0]),
            cy=float(centers[n, 1]),
            window_um=w,
            rank=None,
            rank_score=float(mean_p[n]),
            score_kind="mean_p_tumor",
            tumor_fraction=float(tumor_fraction[n]),
        )
        for n in np.flatnonzero(valid)
    ]


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


def select_verified_hotspots(
    cands: Sequence[HotspotWindow],
    k_max: int,
    w: float,
    gap: float,
    verify: Callable[[HotspotWindow], bool | None],
    max_checks: int,
) -> tuple[list[HotspotWindow], list[tuple[HotspotWindow, bool | None]]]:
    """Greedy selection (SPEC-05 §5.3) with the tumour referee in the loop (§5.4).

    Down the ranked list, each window that clears the Chebyshev rule against those already
    selected is put to ``verify`` (at most ``max_checks`` calls): True selects it, False removes it
    and selection continues; nothing rejected is ever added back. ``None`` (no verdict: an outage
    the fallback policy allows) is held; held windows are selected only when the referee neither
    confirmed nor rejected any window (SPEC-01 §3.6).

    Returns the selected windows (ranked, ``hs_NN``) and every (window, verdict) checked.
    """
    ranked = sorted(
        cands,
        key=lambda c: (c.rank_score, c.tumor_fraction is not None, c.tumor_fraction or 0.0),
        reverse=True,
    )
    confirmed: list[HotspotWindow] = []
    checked: list[tuple[HotspotWindow, bool | None]] = []
    for c in ranked:
        if len(confirmed) == k_max or len(checked) == max_checks:
            break
        if not all(max(abs(c.cx - s.cx), abs(c.cy - s.cy)) >= (w + gap) for s in confirmed):
            continue
        verdict = verify(c)
        checked.append((c, verdict))
        if verdict is True:
            confirmed.append(c)
    if confirmed:
        eligible = confirmed
    elif checked and all(v is None for _, v in checked):
        eligible = [c for c, _ in checked]
    else:
        eligible = []
    return select_hotspots(eligible, k_max=k_max, w=w, gap=gap), checked


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
