"""Unit and property-based tests for Stage 3 Hotspots v6 (SPEC-05 §5, WP-6.3).

Acceptance Criteria:
- AC3: Chebyshev distance non-overlap verified via Hypothesis over >= 10,000 iterations.
- AC5: Purity constraint — zero padding with low-threshold, unconfirmed or fabricated candidates.
- Mitre buffered overlap validation.
"""
import math
import numpy as np
import pytest
from hypothesis import given, settings, strategies as st, HealthCheck

from pipeline.hotspots_v6 import (
    HotspotWindow,
    generate_candidate_lattice,
    select_hotspots,
    validate_hotspots_mitre_overlap,
    compute_window_tumor_metrics_from_prob_map,
    score_lattice_windows,
    select_verified_hotspots,
)
from app.core.pipeline_config import HotspotsConfig
from pipeline.tissue_mask import TissueMask


# ---------------------------------------------------------------------------
# AC3: Hypothesis 10,000 iterations Chebyshev distance property test
# ---------------------------------------------------------------------------
@st.composite
def candidate_window_lists(draw):
    """Generates random lists of HotspotWindow candidates."""
    n_cands = draw(st.integers(min_value=0, max_value=40))
    cands = []
    for i in range(n_cands):
        # Coordinates in a 10 mm x 10 mm region
        cx = draw(st.floats(min_value=0.0, max_value=10000.0, allow_nan=False, allow_infinity=False))
        cy = draw(st.floats(min_value=0.0, max_value=10000.0, allow_nan=False, allow_infinity=False))
        score = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False))
        tf = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False))
        cands.append(
            HotspotWindow(
                id=f"cand_{i+1:02d}",
                cx=cx,
                cy=cy,
                window_um=600.0,
                rank=None,
                rank_score=score,
                score_kind="mean_p_tumor",
                tumor_fraction=tf,
            )
        )
    return cands


@settings(
    max_examples=10000,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
@given(
    cands=candidate_window_lists(),
    k_max=st.integers(min_value=1, max_value=20),
    w=st.floats(min_value=100.0, max_value=1200.0, allow_nan=False, allow_infinity=False),
    gap=st.floats(min_value=0.0, max_value=200.0, allow_nan=False, allow_infinity=False),
)
def test_hypothesis_chebyshev_non_overlap_ac3(cands, k_max, w, gap):
    """AC3: Every pair of selected windows must satisfy the Chebyshev distance >= w + gap."""
    selected = select_hotspots(cands, k_max=k_max, w=w, gap=gap)

    # 1. Count never exceeds k_max
    assert len(selected) <= k_max

    # 2. Sequential ranking
    for idx, s in enumerate(selected):
        assert s.rank == idx + 1
        assert s.id == f"hs_{idx + 1:02d}"

    # 3. Strict Chebyshev non-overlap invariant for every pair
    min_dist = w + gap
    for i, a in enumerate(selected):
        for b in selected[i + 1:]:
            chebyshev_dist = max(abs(a.cx - b.cx), abs(a.cy - b.cy))
            # Floating point tolerance
            assert chebyshev_dist >= (min_dist - 1e-9), (
                f"Chebyshev overlap violation between {a.id} ({a.cx}, {a.cy}) and "
                f"{b.id} ({b.cx}, {b.cy}): dist={chebyshev_dist} < {min_dist}"
            )


# ---------------------------------------------------------------------------
# AC5: Purity constraint — zero padding
# ---------------------------------------------------------------------------
def test_ac5_fewer_than_kmax_returns_exact_k():
    """If fewer than k_max windows qualify, return exactly K < k_max without padding."""
    # Create 3 well-separated candidates
    cands = [
        HotspotWindow("c1", 1000.0, 1000.0, 600.0, None, 0.95, "mean_p_tumor", 0.90),
        HotspotWindow("c2", 3000.0, 3000.0, 600.0, None, 0.90, "mean_p_tumor", 0.85),
        HotspotWindow("c3", 5000.0, 5000.0, 600.0, None, 0.85, "mean_p_tumor", 0.80),
    ]

    selected = select_hotspots(cands, k_max=10, w=600.0, gap=0.0)

    # Must return exactly 3, never padded to 10
    assert len(selected) == 3
    assert [s.rank for s in selected] == [1, 2, 3]
    assert [s.id for s in selected] == ["hs_01", "hs_02", "hs_03"]


def test_ac5_zero_qualifying_candidates_returns_empty():
    """If 0 candidates qualify, returns empty list without fabrication."""
    selected = select_hotspots([], k_max=10, w=600.0, gap=0.0)
    assert selected == []


def test_ac5_greedy_ranking_and_tie_breaking():
    """Greedy selection picks highest score first, breaking ties with tumor_fraction."""
    # Two candidates at same location (only one can be chosen)
    c1 = HotspotWindow("c1", 1000.0, 1000.0, 600.0, None, 0.80, "mean_p_tumor", 0.60)
    c2 = HotspotWindow("c2", 1000.0, 1000.0, 600.0, None, 0.80, "mean_p_tumor", 0.85)

    selected = select_hotspots([c1, c2], k_max=5, w=600.0, gap=0.0)
    assert len(selected) == 1
    # c2 wins due to higher tumor_fraction
    assert selected[0].tumor_fraction == 0.85


# ---------------------------------------------------------------------------
# Lattice Generation & Metric Calculation
# ---------------------------------------------------------------------------
def test_candidate_lattice_step_size():
    """Lattice centers step by step_um (w/4) over bounding box."""
    bbox = (100.0, 200.0, 700.0, 800.0)
    step = 150.0  # 600 / 4
    centers = generate_candidate_lattice(bbox, step_um=step)

    assert len(centers) > 0
    xs = [c[0] for c in centers]
    ys = [c[1] for c in centers]

    assert min(xs) >= 100.0
    assert max(xs) <= 700.0 + 1e-6
    assert min(ys) >= 200.0
    assert max(ys) <= 800.0 + 1e-6


def test_candidate_lattice_empty_bbox():
    """Invalid or inverted bounding box returns empty list."""
    assert generate_candidate_lattice((500.0, 500.0, 100.0, 100.0), 150.0) == []
    with pytest.raises(ValueError):
        generate_candidate_lattice((0.0, 0.0, 100.0, 100.0), -10.0)


def test_window_tumor_metrics_from_prob_map():
    """Verifies area-weighted tumor fraction and mean calibrated probability."""
    # 10x10 grid with stride 100 um -> 1000 um x 1000 um
    grid = np.zeros((10, 10), dtype=np.float32)
    # 4x4 region of high tumor
    grid[3:7, 3:7] = 0.90

    # Window centered at (500, 500) of size 400 um covers exactly the 4x4 region
    tf, mean_p = compute_window_tumor_metrics_from_prob_map(
        cx=500.0,
        cy=500.0,
        window_um=400.0,
        prob_grid=grid,
        origin_um=(0.0, 0.0),
        stride_um=100.0,
        tau_tumor=0.50,
    )

    assert pytest.approx(tf, abs=1e-3) == 1.0
    assert pytest.approx(mean_p, abs=1e-3) == 0.90


# ---------------------------------------------------------------------------
# Mitre Buffer Overlap Validation (AC4)
# ---------------------------------------------------------------------------
def test_validate_hotspots_mitre_overlap_touching_boxes():
    """Boxes sharing an edge have 0.0 intersection area under gap=0."""
    h1 = {
        "id": "hs_01",
        "polygon_um": [[0.0, 0.0], [600.0, 0.0], [600.0, 600.0], [0.0, 600.0], [0.0, 0.0]],
    }
    h2 = {
        "id": "hs_02",
        "polygon_um": [[600.0, 0.0], [1200.0, 0.0], [1200.0, 600.0], [600.0, 600.0], [600.0, 0.0]],
    }

    # Under gap=0, touching boxes do NOT overlap (intersection area is a line, area 0)
    assert validate_hotspots_mitre_overlap([h1, h2], gap_um=0.0) == []

    # Under gap=10.0, buffered boxes expand by 5.0 each and overlap
    collisions = validate_hotspots_mitre_overlap([h1, h2], gap_um=10.0)
    assert collisions == [["hs_01", "hs_02"]]


def test_validate_hotspots_mitre_overlap_overlapping_boxes():
    """Overlapping boxes are detected and returned as collision pairs."""
    h1 = {
        "id": "hs_01",
        "polygon_um": [[0.0, 0.0], [600.0, 0.0], [600.0, 600.0], [0.0, 600.0], [0.0, 0.0]],
    }
    h2 = {
        "id": "hs_02",
        "polygon_um": [[500.0, 0.0], [1100.0, 0.0], [1100.0, 600.0], [500.0, 600.0], [500.0, 0.0]],
    }

    collisions = validate_hotspots_mitre_overlap([h1, h2], gap_um=0.0)
    assert collisions == [["hs_01", "hs_02"]]


# ---------------------------------------------------------------------------
# §5.1 lattice windows and §5.4 referee-in-the-loop selection
# ---------------------------------------------------------------------------
TILE_UM = 100.0
HS_CFG = HotspotsConfig(
    window_um=400.0, lattice_step_um=100.0, min_tissue_fraction=0.70, min_tumor_fraction=0.50, gap_um=0.0, k_max=3,
)


def _slide(n_tiles: int = 30):
    """A fully tissue slide of n_tiles x n_tiles 100 um tiles with p_tumor_cal 0.1 everywhere."""
    p = np.full((n_tiles, n_tiles), 0.1)
    mask = TissueMask(np.ones((n_tiles * 10, n_tiles * 10), dtype=bool), mpp=TILE_UM / 10)
    return p, mask, (n_tiles * TILE_UM, n_tiles * TILE_UM)


def test_lattice_windows_cover_the_tumour_and_obey_the_filters():
    p, mask, extent = _slide()
    p[10:16, 10:16] = 0.9  # a 600 um tumour block
    windows = score_lattice_windows(p, p >= 0.5, TILE_UM, mask, extent, HS_CFG)
    assert windows
    assert all(w.tumor_fraction >= HS_CFG.min_tumor_fraction for w in windows)
    best = max(windows, key=lambda w: w.rank_score)
    assert best.tumor_fraction == pytest.approx(1.0) and best.rank_score == pytest.approx(0.9)
    assert 1200.0 <= best.cx <= 1400.0 and 1200.0 <= best.cy <= 1400.0


def test_lattice_has_no_windows_without_tumour_or_tissue():
    p, mask, extent = _slide()
    assert score_lattice_windows(p, p >= 0.5, TILE_UM, mask, extent, HS_CFG) == []
    p[10:16, 10:16] = 0.9
    no_tissue = TissueMask(np.zeros((300, 300), dtype=bool), mpp=TILE_UM / 10)
    assert score_lattice_windows(p, p >= 0.5, TILE_UM, no_tissue, extent, HS_CFG) == []


def test_lattice_windows_stay_inside_the_slide():
    p, mask, extent = _slide()
    p[0:3, 0:3] = 0.9  # tumour in the corner: windows centred on its bbox would leave the slide
    windows = score_lattice_windows(p, p >= 0.5, TILE_UM, mask, extent, HS_CFG)
    half = HS_CFG.window_um / 2
    assert windows and all(w.cx - half >= 0 and w.cy - half >= 0 for w in windows)


def _ranked(n: int, spacing: float = 1000.0) -> list[HotspotWindow]:
    return [HotspotWindow(f"c{k}", k * spacing, 0.0, 400.0, None, 1.0 - k / 100, "mean_p_tumor", 0.9) for k in range(n)]


def test_rejected_windows_are_skipped_and_never_padded_back():
    verdicts = {"c0": False, "c1": True, "c2": False, "c3": True}
    selected, checked = select_verified_hotspots(_ranked(4), 3, 400.0, 0.0, lambda c: verdicts[c.id], 20)
    assert [s.candidate_id for s in selected] == ["c1", "c3"]
    assert [s.rank for s in selected] == [1, 2]
    assert [c.id for c, _ in checked] == ["c0", "c1", "c2", "c3"]


def test_referee_calls_are_capped_and_overlapping_windows_are_not_checked():
    cands = _ranked(3) + [HotspotWindow("dup", 10.0, 0.0, 400.0, None, 0.995, "mean_p_tumor", 0.9)]
    calls = []
    selected, _ = select_verified_hotspots(cands, 10, 400.0, 0.0, lambda c: calls.append(c.id) or True, 2)
    assert calls == ["c0", "c1"]  # "dup" overlaps c0; the cap stops after two calls
    assert [s.candidate_id for s in selected] == ["c0", "c1"]


def test_unverified_windows_are_used_only_without_any_verdict():
    selected, _ = select_verified_hotspots(_ranked(3), 3, 400.0, 0.0, lambda c: None, 20)
    assert [s.candidate_id for s in selected] == ["c0", "c1", "c2"]
    verdicts = {"c0": None, "c1": False, "c2": None}
    selected, _ = select_verified_hotspots(_ranked(3), 3, 400.0, 0.0, lambda c: verdicts[c.id], 20)
    assert selected == []
