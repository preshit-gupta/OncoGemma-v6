"""
Unit tests for Pure Nottingham Mitotic Scoring Engine (v4.3).
"""
import pytest

from app.core.pipeline_config import get_pipeline_config
from pipeline.scoring import calculate_hpf_mitosis_counts, compute_nottingham_mitotic_score


def scoring():
    return get_pipeline_config().mitosis.scoring


def test_hpf_containment_counting():
    hpfs = [
        {"seq": 1, "center_um": [100.0, 100.0], "radius_um": 50.0, "count": 0},
        {"seq": 2, "center_um": [300.0, 300.0], "radius_um": 50.0, "count": 0},
    ]

    candidates = [
        # Inside HPF 1
        {"id": "m1", "centroid_um": [110.0, 110.0], "label": "mitosis"},
        {"id": "m2", "centroid_um": [130.0, 100.0], "label": "mitosis"},
        # Outside HPF 1 (rejected candidate inside HPF 1)
        {"id": "m3", "centroid_um": [105.0, 105.0], "label": "not_mitosis"},
        # Inside HPF 2
        {"id": "m4", "centroid_um": [310.0, 300.0], "label": "mitosis"},
        # Outside both HPFs
        {"id": "m5", "centroid_um": [500.0, 500.0], "label": "mitosis"}
    ]

    updated_hpfs, total_count = calculate_hpf_mitosis_counts(candidates, hpfs)

    assert updated_hpfs[0]["count"] == 2 # m1 and m2
    assert updated_hpfs[1]["count"] == 1 # m4
    assert total_count == 3 # m1, m2, m4


@pytest.mark.parametrize("count_total, n_hpf, expected_score", [
    (0, 10, 1),    # 0.00 / mm² -> Score 1
    (5, 10, 1),    # 2.32 / mm² -> Score 1
    (7, 10, 1),    # 3.25 / mm² -> Score 1
    (8, 10, 2),    # 3.71 / mm² (>= 3.65) -> Score 2
    (12, 10, 2),   # 5.56 / mm² -> Score 2
    (15, 10, 2),   # 6.95 / mm² (< 7.30) -> Score 2
    (16, 10, 3),   # 7.42 / mm² (>= 7.30) -> Score 3
    (25, 10, 3),   # 11.59 / mm² -> Score 3
    (50, 10, 3),   # 23.18 / mm² -> Score 3
])
def test_nottingham_scoring_boundaries(count_total, n_hpf, expected_score):
    summary = compute_nottingham_mitotic_score(count_total=count_total, n_hpf=n_hpf, radius_um=262.0, scoring=scoring())
    assert summary["mitotic_score"] == expected_score
    assert summary["count_total"] == count_total
    assert summary["area_mm2"] == 2.157


def test_scoring_with_fewer_hpfs():
    # Test area normalization for small tumor (5 HPFs instead of 10)
    summary = compute_nottingham_mitotic_score(count_total=8, n_hpf=5, radius_um=262.0, scoring=scoring())
    # Area = 5 * 0.21565 = 1.078 mm²
    # Density = 8 / 1.078 = 7.42 mitoses/mm² >= 7.30 -> Score 3
    assert summary["n_hpf"] == 5
    assert summary["area_mm2"] == 1.078
    assert summary["mitotic_score"] == 3
