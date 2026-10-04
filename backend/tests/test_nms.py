"""
Global cross-tile NMS in micrometres (SPEC-06 §5.7): once, after the decisions, by p_b ?? p_a.
"""
import pytest
from pipeline.detect import apply_global_nms


def test_global_nms_suppression():
    candidates = [
        # Candidate 1: high confidence
        {"id": "m1", "centroid_um": [500.0, 500.0], "p_a": 0.90},
        # Candidate 2: within 5.0 um of m1 -> should be suppressed
        {"id": "m2", "centroid_um": [503.0, 504.0], "p_a": 0.70},
        # Candidate 3: 15.0 um away from m1 -> should be kept
        {"id": "m3", "centroid_um": [515.0, 500.0], "p_a": 0.85},
        # Candidate 4: within 4.0 um of m3 -> should be suppressed
        {"id": "m4", "centroid_um": [517.0, 502.0], "p_a": 0.60},
    ]

    survivors = apply_global_nms(candidates, nms_radius_um=7.5)

    assert len(survivors) == 2
    survivor_ids = [c["id"] for c in survivors]
    assert "m1" in survivor_ids
    assert "m3" in survivor_ids
    assert "m2" not in survivor_ids
    assert "m4" not in survivor_ids


def test_global_nms_empty():
    assert apply_global_nms([], nms_radius_um=7.5) == []


def test_dividing_cell_counts_once():
    """SPEC-06 §3 dividing-cell rule: the two daughter chromosome groups of one synthetic telophase
    figure, detected 6 µm apart, yield one detection at r_nms = 7.5 µm."""
    telophase = [
        {"id": "upper_group", "centroid_um": [1000.0, 997.0], "p_a": 0.93, "final_decision": "mitosis"},
        {"id": "lower_group", "centroid_um": [1000.0, 1003.0], "p_a": 0.88, "final_decision": "mitosis"},
    ]
    survivors = apply_global_nms(telophase, nms_radius_um=7.5)
    assert [c["id"] for c in survivors] == ["upper_group"]


def test_order_is_p_b_then_p_a_with_no_rank_by_decision():
    """A 'not_mitosis' with the higher probability suppresses a 'mitosis' next to it: decisions do not rank."""
    candidates = [
        {"id": "low_mitosis", "centroid_um": [0.0, 0.0], "p_a": 0.95, "p_b": 0.40, "final_decision": "mitosis"},
        {"id": "high_other", "centroid_um": [3.0, 0.0], "p_a": 0.80, "p_b": 0.90, "final_decision": "not_mitosis"},
    ]
    assert [c["id"] for c in apply_global_nms(candidates, nms_radius_um=7.5)] == ["high_other"]


def test_a_candidate_without_a_model_probability_is_refused():
    with pytest.raises(ValueError, match="p_a"):
        apply_global_nms([{"id": "x", "centroid_um": [0.0, 0.0], "p_a": None, "p_b": None}], nms_radius_um=7.5)
