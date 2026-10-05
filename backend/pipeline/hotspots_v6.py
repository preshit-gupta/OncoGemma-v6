"""Stage 3 HPF sites: circles on a lattice, disjoint greedy selection, circle validation (SPEC-05 §5, D22).

An HPF site is a circle ``hpf_diameter_um`` across; its frame is the square around it with
``frame_padding_um`` added on each side. Implements:
1. Regular candidate lattice over the tumour bounding box.
2. Tissue and tumour fractions of each candidate circle, and the mean calibrated tumour
   probability over it (arm H1), computed for all centres at once.
3. Greedy selection: circles never overlap (centre distance >= d + gap); frames may.
4. Overlap validation of circles.

No hardcoded clinical or geometric constants: all configuration is passed in via
HotspotsConfig (configs/specimen_profiles.yaml) and SafetyConfig (configs/safety.yaml).
"""
import math
from dataclasses import dataclass, replace
from typing import Callable, Sequence

import numpy as np

from app.core.pipeline_config import HotspotsConfig
from pipeline.mitosis_gate import DISK_SAMPLES_PER_RADIUS
from pipeline.tissue_mask import TissueMask, disk_strips
from pipeline.tumor_front import front_distance

UM2_PER_MM2 = 1e6
# Slack for floating point when two circles exactly touch or a lattice ends exactly on the bounding box.
TOUCH_TOLERANCE_UM = 1e-6


@dataclass(frozen=True)
class HotspotWindow:
    """An HPF site: a circle of diameter ``hpf_diameter_um`` at (cx, cy) in a padded square frame (SPEC-05 §5.1, §5.3)."""

    id: str
    cx: float
    cy: float
    hpf_diameter_um: float
    rank: int | None
    rank_score: float
    score_kind: str
    tumor_fraction: float | None  # over the circle; None for a site that is not a lattice candidate
    prescan_expected: float | None = None
    source: str = "model"
    excluded: bool = False
    exclude_reason: str | None = None
    candidate_id: str | None = None  # the candidate this site was selected from
    tissue_fraction: float | None = None  # over the circle
    frame_padding_um: float = 0.0
    at_periphery: bool | None = None  # arm H1P: the centre is within periphery_band_um of the invasive front
    front_distance_um: float | None = None  # distance of the centre to the front; None when the slide has none

    @property
    def window_um(self) -> float:
        """Side of the padded frame."""
        return self.hpf_diameter_um + 2.0 * self.frame_padding_um

    @property
    def polygon_um(self) -> list[list[float]]:
        """The frame as a closed 5-vertex ring: [x, y] in micrometers."""
        return frame_polygon_um(self.cx, self.cy, self.window_um)

    @property
    def area_mm2(self) -> float:
        """Area of the circle."""
        return math.pi * (self.hpf_diameter_um / 2.0) ** 2 / UM2_PER_MM2

    def to_dict(self) -> dict:
        """Serializes to the Hotspot schema in docs/contracts/triage_v6.md."""
        return {
            "id": self.id,
            "center_um": [self.cx, self.cy],
            "hpf_diameter_um": self.hpf_diameter_um,
            "polygon_um": self.polygon_um,
            "window_um": self.window_um,
            "rank": self.rank,
            "rank_score": self.rank_score,
            "score_kind": self.score_kind,
            "tissue_fraction": self.tissue_fraction,
            "tumor_fraction": self.tumor_fraction,
            "prescan_expected": self.prescan_expected,
            "source": self.source,
            "excluded": self.excluded,
            "exclude_reason": self.exclude_reason,
            "area_mm2": self.area_mm2,
            "at_periphery": self.at_periphery,
            "front_distance_um": self.front_distance_um,
        }


def frame_polygon_um(cx: float, cy: float, side_um: float) -> list[list[float]]:
    """The closed ring of the axis-aligned square of side ``side_um`` centred on (cx, cy)."""
    half = side_um / 2.0
    x0, y0, x1, y1 = cx - half, cy - half, cx + half, cy + half
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


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
    xs = np.arange(x_min, x_max + TOUCH_TOLERANCE_UM, step_um, dtype=np.float64)
    ys = np.arange(y_min, y_max + TOUCH_TOLERANCE_UM, step_um, dtype=np.float64)

    if xs.size == 0 or ys.size == 0:
        return []

    grid_x, grid_y = np.meshgrid(xs, ys)
    return list(zip(grid_x.ravel().tolist(), grid_y.ravel().tolist()))


class RasterIntegral:
    """Exact box integrals of a piecewise-constant raster: ``values[row, col]`` on cells of ``cell_um`` from the slide origin.

    Boxes may be fractional and extend past the raster; the part off it contributes nothing.
    """

    def __init__(self, values: np.ndarray, cell_um: float):
        cells = np.asarray(values, dtype=np.float64)
        self.cell_um = float(cell_um)
        self.n_rows, self.n_cols = cells.shape
        self._cells = cells
        self._area = np.zeros((self.n_rows + 1, self.n_cols + 1))
        self._area[1:, 1:] = cells.cumsum(axis=0).cumsum(axis=1)
        self._above = np.zeros((self.n_rows + 1, self.n_cols))
        self._above[1:] = cells.cumsum(axis=0)
        self._left = np.zeros((self.n_rows, self.n_cols + 1))
        self._left[:, 1:] = cells.cumsum(axis=1)

    def _cumulative(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        x = np.clip(x, 0.0, self.n_cols)
        y = np.clip(y, 0.0, self.n_rows)
        ix = np.minimum(np.floor(x).astype(np.int64), self.n_cols - 1)
        iy = np.minimum(np.floor(y).astype(np.int64), self.n_rows - 1)
        ax, ay = x - ix, y - iy
        return self._area[iy, ix] + ax * self._above[iy, ix] + ay * self._left[iy, ix] + ax * ay * self._cells[iy, ix]

    def box_sums_um(self, x0, y0, x1, y1) -> np.ndarray:
        """Integral of the raster over each box, in value x cell² (cells counted fractionally)."""
        x0, y0, x1, y1 = (np.asarray(v, dtype=np.float64) / self.cell_um for v in (x0, y0, x1, y1))
        return self._cumulative(x1, y1) - self._cumulative(x0, y1) - self._cumulative(x1, y0) + self._cumulative(x0, y0)

    def disk_sums_um(self, cx: np.ndarray, cy: np.ndarray, r: float) -> np.ndarray:
        """Integral over each disk of radius ``r``, in value x µm² (the disk cut into strips, see ``disk_strips``)."""
        dy, half, height = disk_strips(r)
        total = np.zeros(np.shape(cx))
        for dy_k, half_k in zip(dy, half):
            total += self.box_sums_um(cx - half_k, cy + dy_k - height / 2.0, cx + half_k, cy + dy_k + height / 2.0)
        return total * (self.cell_um ** 2)


def sampled_disk_fractions(mask: np.ndarray, cell_um: float, cx, cy, r: float, chunk: int = 2048) -> np.ndarray:
    """``TumorGate.tumor_fraction_in_disk`` of many disks at once: the share of the disk's sample points that fall in a True cell.

    The sample points are the gate's: a lattice of ``DISK_SAMPLES_PER_RADIUS`` points per radius, those
    inside the disk; points off the mask are False. Each lattice row is counted per cell column with
    integer arithmetic instead of looking up every point, so the result equals the per-disk function.
    """
    cx = np.asarray(cx, dtype=np.float64).ravel()
    cy = np.asarray(cy, dtype=np.float64).ravel()
    n = DISK_SAMPLES_PER_RADIUS
    pitch = r / n
    offsets = (np.arange(-n, n) + 0.5) * pitch                       # lattice offsets, index m = 0 .. 2n-1
    half_width = np.sqrt(np.maximum(r * r - offsets ** 2, 0.0))
    row_inside = (offsets[:, None] ** 2 + offsets[None, :] ** 2) <= r * r
    m_lo = np.argmax(row_inside, axis=1)                              # first inside point of each row
    m_hi = (2 * n - 1) - np.argmax(row_inside[:, ::-1], axis=1)       # last inside point of each row
    n_inside = int(row_inside.sum())
    n_rows, n_cols = mask.shape
    max_cols = int(np.ceil(2.0 * r / cell_um)) + 1                    # cell columns a disk can touch
    out = np.empty(cx.shape)
    for k in range(0, len(cx), chunk):
        x, y = cx[k:k + chunk, None], cy[k:k + chunk, None]
        rows = np.floor((y + offsets[None, :]) / cell_um).astype(np.int64)        # (centres, lattice rows)
        row_ok = (rows >= 0) & (rows < n_rows)
        first_col = np.floor((x - r) / cell_um).astype(np.int64)
        hits = np.zeros(rows.shape)
        for t in range(max_cols):
            col = first_col + t
            # lattice indices m with x + (m - n + 0.5) * pitch inside [col, col + 1) cells, within the row's inside range
            lo = np.ceil((col * cell_um - x) / pitch + n - 0.5)
            hi = np.ceil(((col + 1) * cell_um - x) / pitch + n - 0.5)
            count = np.clip(hi, m_lo[None, :], m_hi[None, :] + 1) - np.clip(lo, m_lo[None, :], m_hi[None, :] + 1)
            ok = row_ok & (col >= 0) & (col < n_cols)
            cell = np.zeros(rows.shape, dtype=bool)
            cell[ok] = mask[rows[ok], np.broadcast_to(col, rows.shape)[ok]]
            hits += np.where(cell, count, 0.0)
        out[k:k + chunk] = hits.sum(axis=1) / n_inside
    return out


def disk_tumor_metrics(
    centers_um: np.ndarray,
    radius_um: float,
    p_raster: np.ndarray,
    is_tumor_raster: np.ndarray,
    cell_um: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Tumour fraction and mean calibrated tumour probability of disks (SPEC-05 §5.1, §5.2 H1).

    The tumour fraction is the share of the disk covered by tumour tiles (the undilated mask), measured
    as ``TumorGate.tumor_fraction_in_disk`` does so that Stages 3 and 4 report one number; the mean is ``p_tumor_cal`` weighted by the area of each tissue tile inside the
    disk (NaN when the disk holds no tissue tile). ``p_raster`` is NaN off tissue. All centres at once.
    """
    centers = np.asarray(centers_um, dtype=np.float64).reshape(-1, 2)
    tissue = ~np.isnan(p_raster)
    tissue_area = RasterIntegral(tissue, cell_um)
    p_area = RasterIntegral(np.where(tissue, p_raster, 0.0), cell_um)
    cx, cy = centers[:, 0], centers[:, 1]
    tumor_fraction = sampled_disk_fractions(np.asarray(is_tumor_raster, dtype=bool) & tissue, cell_um, cx, cy, radius_um)
    tissue_um2 = tissue_area.disk_sums_um(cx, cy, radius_um)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean_p = np.where(tissue_um2 > 0.0, p_area.disk_sums_um(cx, cy, radius_um) / tissue_um2, np.nan)
    return tumor_fraction, mean_p


def score_lattice_windows(
    p_raster: np.ndarray,
    is_tumor_raster: np.ndarray,
    tile_um: float,
    tissue: TissueMask,
    extent_um: tuple[float, float],
    cfg: HotspotsConfig,
) -> list[HotspotWindow]:
    """The valid candidate sites of SPEC-05 §5.1, scored under arm H1 or H1P (§5.2).

    Circles of diameter ``cfg.hpf_diameter_um`` are centred on a lattice of step ``cfg.lattice_step_um``
    over the bounding box of the tumour mask (tile rasters from the slide origin). A site is valid when
    its circle lies inside the slide, its tissue fraction is at least ``cfg.min_tissue_fraction`` and its
    tumour fraction at least ``cfg.min_tumor_fraction``. No tumour tile: no sites.

    Under H1P each site also carries its centre's distance to the invasive front and whether that is within
    ``cfg.periphery_band_um`` (``_by_rank`` puts those first).
    """
    if cfg.ranking_arm not in ("H1", "H1P"):
        raise NotImplementedError(f"hotspot ranking arm {cfg.ranking_arm} needs the mitotic prescan (SPEC-05 §5.2)")
    rows, cols = np.nonzero(is_tumor_raster)
    if rows.size == 0:
        return []
    r = cfg.hpf_radius_um
    bbox = (cols.min() * tile_um, rows.min() * tile_um, (cols.max() + 1) * tile_um, (rows.max() + 1) * tile_um)
    centers = np.array(generate_candidate_lattice(bbox, cfg.lattice_step_um), dtype=np.float64).reshape(-1, 2)
    width_um, height_um = extent_um
    inside = (
        (centers[:, 0] - r >= 0.0) & (centers[:, 0] + r <= width_um)
        & (centers[:, 1] - r >= 0.0) & (centers[:, 1] + r <= height_um)
    )
    centers = centers[inside]
    if len(centers) == 0:
        return []
    tumor_fraction, mean_p = disk_tumor_metrics(centers, r, p_raster, is_tumor_raster, tile_um)
    # The tissue fraction (the costlier measurement) only for the centres that are tumour enough.
    tissue_fraction = np.zeros(len(centers))
    plausible = (tumor_fraction >= cfg.min_tumor_fraction) & ~np.isnan(mean_p)
    tissue_fraction[plausible] = tissue.fractions_in_disks_um(centers[plausible, 0], centers[plausible, 1], r)
    valid = plausible & (tissue_fraction >= cfg.min_tissue_fraction)
    periphery = cfg.ranking_arm == "H1P"
    if periphery:
        if cfg.periphery_band_um is None:
            raise ValueError("ranking_arm H1P needs periphery_band_um")
        front_um = front_distance(p_raster, is_tumor_raster, tile_um).at_um(centers[:, 0], centers[:, 1])
    return [
        HotspotWindow(
            id=f"win_{n:05d}",
            cx=float(centers[n, 0]),
            cy=float(centers[n, 1]),
            hpf_diameter_um=cfg.hpf_diameter_um,
            rank=None,
            rank_score=float(mean_p[n]),
            score_kind="periphery_then_tumor" if periphery else "mean_p_tumor",
            tumor_fraction=float(tumor_fraction[n]),
            tissue_fraction=float(tissue_fraction[n]),
            frame_padding_um=cfg.frame_padding_um,
            at_periphery=bool(front_um[n] <= cfg.periphery_band_um) if periphery else None,
            front_distance_um=(float(front_um[n]) if np.isfinite(front_um[n]) else None) if periphery else None,
        )
        for n in np.flatnonzero(valid)
    ]


def _clear_of(c: HotspotWindow, selected: Sequence[HotspotWindow], d: float, gap: float) -> bool:
    """True when circle ``c`` is at least ``gap`` from every selected circle (centre distance >= d + gap)."""
    return all(math.dist((c.cx, c.cy), (s.cx, s.cy)) >= d + gap - TOUCH_TOLERANCE_UM for s in selected)


def _by_rank(cands: Sequence[HotspotWindow]) -> list[HotspotWindow]:
    """Rank descending: periphery sites first (H1P; null counts as not), then by score, ties by tumour fraction; a site without one ranks after those with one."""
    return sorted(
        cands,
        key=lambda c: (c.at_periphery is True, c.rank_score, c.tumor_fraction is not None, c.tumor_fraction or 0.0),
        reverse=True,
    )


def _ranked(c: HotspotWindow, rank_idx: int) -> HotspotWindow:
    return replace(c, id=f"hs_{rank_idx:02d}", rank=rank_idx, source="model", excluded=False, exclude_reason=None, candidate_id=c.id)


def select_hotspots(
    cands: Sequence[HotspotWindow],
    k_max: int,
    d: float,
    gap: float,
) -> list[HotspotWindow]:
    """Greedy site selection with hard Euclidean non-overlap of the circles (SPEC-05 §5.3, D22).

    Two circles of diameter d do not overlap, with ``gap`` between their edges, if and only if their
    centres are at least d + gap apart. The frames around them may overlap.

    Purity constraint (AC5): if fewer than k_max sites qualify, returns exactly K < k_max.
    Never pads with lower-threshold or unconfirmed sites.

    Returns the selected sites, ranked (``hs_NN``).
    """
    selected: list[HotspotWindow] = []
    for c in _by_rank(cands):
        if _clear_of(c, selected, d, gap):
            selected.append(_ranked(c, len(selected) + 1))
            if len(selected) == k_max:
                break
    return selected


def select_verified_hotspots(
    cands: Sequence[HotspotWindow],
    k_max: int,
    d: float,
    gap: float,
    verify: Callable[[HotspotWindow], bool | None],
    max_checks: int,
) -> tuple[list[HotspotWindow], list[tuple[HotspotWindow, bool | None]]]:
    """Greedy selection (SPEC-05 §5.3) with the tumour referee in the loop (§5.4).

    Down the ranked list, each site whose circle clears those already selected is put to ``verify``
    (at most ``max_checks`` calls): True selects it, False removes it and selection continues; nothing
    rejected is ever added back. ``None`` (no verdict: an outage the fallback policy allows) is held;
    held sites are selected only when the referee neither confirmed nor rejected any site (SPEC-01 §3.6).

    Returns the selected sites (ranked, ``hs_NN``) and every (site, verdict) checked.
    """
    confirmed: list[HotspotWindow] = []
    checked: list[tuple[HotspotWindow, bool | None]] = []
    for c in _by_rank(cands):
        if len(confirmed) == k_max or len(checked) == max_checks:
            break
        if not _clear_of(c, confirmed, d, gap):
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
    return select_hotspots(eligible, k_max=k_max, d=d, gap=gap), checked


def circles_overlapping(
    sites: Sequence[dict],
    diameter_um: float,
    gap_um: float = 0.0,
) -> list[list[str]]:
    """Pairs of active sites whose circles are closer than ``gap_um`` (SPEC-05 §5.5, D22); frames may overlap.

    Args:
        sites: dictionaries with 'id', 'center_um' and optional 'excluded'.

    Returns:
        List of colliding pairs [[id_a, id_b], ...]. If empty, no collisions exist.

    Raises:
        ValueError: an active site has no centre.
    """
    active = []
    for h in sites:
        if h.get("excluded", False):
            continue
        centre = h.get("center_um")
        if centre is None or len(centre) != 2:
            raise ValueError(f"site {h.get('id')!r} has no center_um")
        active.append((str(h.get("id")), float(centre[0]), float(centre[1])))
    return [
        [id_a, id_b]
        for i, (id_a, xa, ya) in enumerate(active)
        for id_b, xb, yb in active[i + 1:]
        if math.dist((xa, ya), (xb, yb)) < diameter_um + gap_um - TOUCH_TOLERANCE_UM
    ]
