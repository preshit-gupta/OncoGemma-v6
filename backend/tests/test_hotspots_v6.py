"""Unit and property-based tests for Stage 3 HPF sites (SPEC-05 §5, WP-6.3, WP-6.5).

Acceptance Criteria:
- AC3: circle non-overlap (centre distance >= d + gap) verified via Hypothesis over >= 10,000 iterations.
- AC5: Purity constraint — zero padding with low-threshold, unconfirmed or fabricated candidates.
- Circle overlap validation; frames may overlap.
"""
import math
import numpy as np
import pytest
from hypothesis import given, settings, strategies as st, HealthCheck

from pipeline.hotspots_v6 import (
    HotspotWindow,
    circles_overlapping,
    disk_tumor_metrics,
    generate_candidate_lattice,
    select_hotspots,
    score_lattice_windows,
    select_verified_hotspots,
)
from app.core.pipeline_config import HotspotsConfig
from pipeline.tissue_mask import TissueMask


# ---------------------------------------------------------------------------
# AC3: Hypothesis 10,000 iterations circle non-overlap property test
# ---------------------------------------------------------------------------
@st.composite
def candidate_site_lists(draw):
    """Generates random lists of HotspotWindow (HPF site) candidates."""
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
                hpf_diameter_um=500.0,
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
    cands=candidate_site_lists(),
    k_max=st.integers(min_value=1, max_value=20),
    d=st.floats(min_value=100.0, max_value=1200.0, allow_nan=False, allow_infinity=False),
    gap=st.floats(min_value=0.0, max_value=200.0, allow_nan=False, allow_infinity=False),
)
def test_hypothesis_circle_non_overlap_ac3(cands, k_max, d, gap):
    """AC3: Every pair of selected circles must satisfy the centre distance >= d + gap."""
    selected = select_hotspots(cands, k_max=k_max, d=d, gap=gap)

    # 1. Count never exceeds k_max
    assert len(selected) <= k_max

    # 2. Sequential ranking
    for idx, s in enumerate(selected):
        assert s.rank == idx + 1
        assert s.id == f"hs_{idx + 1:02d}"

    # 3. Strict non-overlap invariant for every pair
    min_dist = d + gap
    for i, a in enumerate(selected):
        for b in selected[i + 1:]:
            dist = math.dist((a.cx, a.cy), (b.cx, b.cy))
            # Floating point tolerance
            assert dist >= (min_dist - 1e-5), (
                f"Circle overlap violation between {a.id} ({a.cx}, {a.cy}) and "
                f"{b.id} ({b.cx}, {b.cy}): dist={dist} < {min_dist}"
            )


# ---------------------------------------------------------------------------
# AC5: Purity constraint — zero padding
# ---------------------------------------------------------------------------
def test_ac5_fewer_than_kmax_returns_exact_k():
    """If fewer than k_max sites qualify, return exactly K < k_max without padding."""
    # Create 3 well-separated candidates
    cands = [
        HotspotWindow("c1", 1000.0, 1000.0, 500.0, None, 0.95, "mean_p_tumor", 0.90),
        HotspotWindow("c2", 3000.0, 3000.0, 500.0, None, 0.90, "mean_p_tumor", 0.85),
        HotspotWindow("c3", 5000.0, 5000.0, 500.0, None, 0.85, "mean_p_tumor", 0.80),
    ]

    selected = select_hotspots(cands, k_max=10, d=500.0, gap=0.0)

    # Must return exactly 3, never padded to 10
    assert len(selected) == 3
    assert [s.rank for s in selected] == [1, 2, 3]
    assert [s.id for s in selected] == ["hs_01", "hs_02", "hs_03"]


def test_ac5_zero_qualifying_candidates_returns_empty():
    """If 0 candidates qualify, returns empty list without fabrication."""
    selected = select_hotspots([], k_max=10, d=500.0, gap=0.0)
    assert selected == []


def test_ac5_greedy_ranking_and_tie_breaking():
    """Greedy selection picks highest score first, breaking ties with tumor_fraction."""
    # Two candidates at same location (only one can be chosen)
    c1 = HotspotWindow("c1", 1000.0, 1000.0, 500.0, None, 0.80, "mean_p_tumor", 0.60)
    c2 = HotspotWindow("c2", 1000.0, 1000.0, 500.0, None, 0.80, "mean_p_tumor", 0.85)

    selected = select_hotspots([c1, c2], k_max=5, d=500.0, gap=0.0)
    assert len(selected) == 1
    # c2 wins due to higher tumor_fraction
    assert selected[0].tumor_fraction == 0.85


def test_a_site_serialises_to_the_contract_with_its_frame():
    site = HotspotWindow("c1", 1000.0, 2000.0, 500.0, None, 0.8, "mean_p_tumor", 0.9, tissue_fraction=0.95, frame_padding_um=50.0)
    d = select_hotspots([site], 1, 500.0, 0.0)[0].to_dict()
    assert d["center_um"] == [1000.0, 2000.0] and d["hpf_diameter_um"] == 500.0 and d["window_um"] == 600.0
    assert d["polygon_um"] == [[700.0, 1700.0], [1300.0, 1700.0], [1300.0, 2300.0], [700.0, 2300.0], [700.0, 1700.0]]
    assert d["area_mm2"] == pytest.approx(math.pi * 0.25 ** 2)
    assert d["tissue_fraction"] == 0.95 and d["tumor_fraction"] == 0.9


# ---------------------------------------------------------------------------
# Lattice Generation & Metric Calculation
# ---------------------------------------------------------------------------
def test_candidate_lattice_step_size():
    """Lattice centers step by step_um (d/4) over bounding box."""
    bbox = (100.0, 200.0, 700.0, 800.0)
    step = 125.0  # 500 / 4
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
    assert generate_candidate_lattice((500.0, 500.0, 100.0, 100.0), 125.0) == []
    with pytest.raises(ValueError):
        generate_candidate_lattice((0.0, 0.0, 100.0, 100.0), -10.0)


def test_disk_metrics_are_area_weighted_over_the_circle():
    """A circle inside a 4x4 tumour block of 100 µm tiles is wholly tumour with its mean probability."""
    p = np.zeros((10, 10))
    p[3:7, 3:7] = 0.90
    tf, mean_p = disk_tumor_metrics(np.array([[500.0, 500.0]]), 200.0, p, p >= 0.5, 100.0)
    assert tf[0] == pytest.approx(1.0, abs=1e-3)
    assert mean_p[0] == pytest.approx(0.90, abs=1e-3)
    # A circle straddling the block's edge: half its area is tumour, and the mean is over its tissue tiles.
    tf, mean_p = disk_tumor_metrics(np.array([[300.0, 500.0]]), 100.0, p, p >= 0.5, 100.0)
    assert tf[0] == pytest.approx(0.5, abs=0.02)
    assert mean_p[0] == pytest.approx(0.45, abs=0.05)


# ---------------------------------------------------------------------------
# Circle overlap validation (AC4): circles may touch, frames may overlap
# ---------------------------------------------------------------------------
def test_circles_touching_do_not_overlap_but_a_gap_separates_them():
    h1 = {"id": "hs_01", "center_um": [0.0, 0.0]}
    h2 = {"id": "hs_02", "center_um": [500.0, 0.0]}  # edges touch
    assert circles_overlapping([h1, h2], 500.0, 0.0) == []
    assert circles_overlapping([h1, h2], 500.0, 10.0) == [["hs_01", "hs_02"]]


def test_overlapping_circles_are_detected_and_overlapping_frames_are_not():
    h1 = {"id": "hs_01", "center_um": [0.0, 0.0]}
    h2 = {"id": "hs_02", "center_um": [550.0, 0.0]}  # 600 µm frames overlap by 50 µm; the circles are 50 µm apart
    assert circles_overlapping([h1, h2], 500.0, 0.0) == []
    assert circles_overlapping([h1, {"id": "hs_03", "center_um": [400.0, 0.0]}], 500.0, 0.0) == [["hs_01", "hs_03"]]
    assert circles_overlapping([h1, {**h2, "center_um": [100.0, 0.0], "excluded": True}], 500.0, 0.0) == []


# ---------------------------------------------------------------------------
# §5.1 lattice sites and §5.4 referee-in-the-loop selection
# ---------------------------------------------------------------------------
TILE_UM = 100.0
HS_CFG = HotspotsConfig(
    hpf_diameter_um=400.0, frame_padding_um=50.0, lattice_step_um=100.0,
    min_tissue_fraction=0.70, min_tumor_fraction=0.50, gap_um=0.0, k_max=3,
)


def _slide(n_tiles: int = 30):
    """A fully tissue slide of n_tiles x n_tiles 100 um tiles with p_tumor_cal 0.1 everywhere."""
    p = np.full((n_tiles, n_tiles), 0.1)
    mask = TissueMask(np.ones((n_tiles * 10, n_tiles * 10), dtype=bool), mpp=TILE_UM / 10)
    return p, mask, (n_tiles * TILE_UM, n_tiles * TILE_UM)


def test_lattice_sites_cover_the_tumour_and_obey_the_filters():
    p, mask, extent = _slide()
    p[10:16, 10:16] = 0.9  # a 600 um tumour block
    windows = score_lattice_windows(p, p >= 0.5, TILE_UM, mask, extent, HS_CFG)
    assert windows
    assert all(w.tumor_fraction >= HS_CFG.min_tumor_fraction and w.tissue_fraction >= HS_CFG.min_tissue_fraction for w in windows)
    best = max(windows, key=lambda w: w.rank_score)
    assert best.tumor_fraction == pytest.approx(1.0) and best.rank_score == pytest.approx(0.9)
    assert best.hpf_diameter_um == 400.0 and best.window_um == 500.0
    assert 1200.0 <= best.cx <= 1400.0 and 1200.0 <= best.cy <= 1400.0


def test_lattice_has_no_sites_without_tumour_or_tissue():
    p, mask, extent = _slide()
    assert score_lattice_windows(p, p >= 0.5, TILE_UM, mask, extent, HS_CFG) == []
    p[10:16, 10:16] = 0.9
    no_tissue = TissueMask(np.zeros((300, 300), dtype=bool), mpp=TILE_UM / 10)
    assert score_lattice_windows(p, p >= 0.5, TILE_UM, no_tissue, extent, HS_CFG) == []


def test_lattice_circles_stay_inside_the_slide():
    p, mask, extent = _slide()
    p[0:3, 0:3] = 0.9  # tumour in the corner: circles centred on its bbox would leave the slide
    windows = score_lattice_windows(p, p >= 0.5, TILE_UM, mask, extent, HS_CFG)
    r = HS_CFG.hpf_radius_um
    assert windows and all(w.cx - r >= 0 and w.cy - r >= 0 for w in windows)


def _ranked(n: int, spacing: float = 1000.0) -> list[HotspotWindow]:
    return [HotspotWindow(f"c{k}", k * spacing, 0.0, 400.0, None, 1.0 - k / 100, "mean_p_tumor", 0.9) for k in range(n)]


def test_rejected_sites_are_skipped_and_never_padded_back():
    verdicts = {"c0": False, "c1": True, "c2": False, "c3": True}
    selected, checked = select_verified_hotspots(_ranked(4), 3, 400.0, 0.0, lambda c: verdicts[c.id], 20)
    assert [s.candidate_id for s in selected] == ["c1", "c3"]
    assert [s.rank for s in selected] == [1, 2]
    assert [c.id for c, _ in checked] == ["c0", "c1", "c2", "c3"]


def test_referee_calls_are_capped_and_overlapping_sites_are_not_checked():
    cands = _ranked(3) + [HotspotWindow("dup", 10.0, 0.0, 400.0, None, 0.995, "mean_p_tumor", 0.9)]
    calls = []
    selected, _ = select_verified_hotspots(cands, 10, 400.0, 0.0, lambda c: calls.append(c.id) or True, 2)
    assert calls == ["c0", "c1"]  # "dup" overlaps c0; the cap stops after two calls
    assert [s.candidate_id for s in selected] == ["c0", "c1"]


def test_unverified_sites_are_used_only_without_any_verdict():
    selected, _ = select_verified_hotspots(_ranked(3), 3, 400.0, 0.0, lambda c: None, 20)
    assert [s.candidate_id for s in selected] == ["c0", "c1", "c2"]
    verdicts = {"c0": None, "c1": False, "c2": None}
    selected, _ = select_verified_hotspots(_ranked(3), 3, 400.0, 0.0, lambda c: verdicts[c.id], 20)
    assert selected == []
