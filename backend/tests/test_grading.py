import pytest
import uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

from app.core.pipeline_config import get_pipeline_config
from pipeline.grading import (
    aggregate_components,
    calculate_tubule_score,
    calculate_nottingham_grade,
    near_grade_boundary,
    pleomorphism_mode,
    stage5_result,
    tubule_percent_area_weighted,
    validate_grading_invariants,
)
from app.models.case import Case
from app.models.grading import Grading
from app.core.db import Base


def cfg():
    return get_pipeline_config().scoring


def test_tubule_percent_is_the_tumour_area_weighted_mean():
    samples = [
        {"tumor_present": True, "tubule_percent": 40, "tumor_area_um2": 200000.0},
        {"tumor_present": True, "tubule_percent": 10, "tumor_area_um2": 100000.0},
        {"tumor_present": False, "tubule_percent": 90, "tumor_area_um2": 262144.0},  # no tumour: left out
        {"tumor_present": True, "tubule_percent": None, "tumor_area_um2": 262144.0},  # failed estimate: left out
        {"tumor_present": True, "tubule_percent": 80, "tumor_area_um2": 0.0},  # no tumour area: no weight
    ]
    assert tubule_percent_area_weighted(samples) == (30.0, 2)
    assert tubule_percent_area_weighted(samples[2:]) == (None, 0)
    assert tubule_percent_area_weighted([]) == (None, 0)


def test_pleomorphism_mode_and_ties():
    assert pleomorphism_mode([3, 3, 2], "max") == (3, False)
    assert pleomorphism_mode([1, 2, 2, 1], "max") == (2, True)  # owner decision 2026-10-04
    assert pleomorphism_mode([1, 2, 2, 1], "min") == (1, True)
    assert pleomorphism_mode([1, 2, 3], "max") == (3, True)
    assert pleomorphism_mode([], "max") == (None, False)


def test_tubule_boundary_cutoffs():
    # Score 1: > 75.0%
    assert calculate_tubule_score(75.1, cfg()) == 1
    assert calculate_tubule_score(90.0, cfg()) == 1

    # Score 2: 10.0% - 75.0%
    assert calculate_tubule_score(75.0, cfg()) == 2
    assert calculate_tubule_score(50.0, cfg()) == 2
    assert calculate_tubule_score(10.0, cfg()) == 2

    # Score 3: < 10.0%
    assert calculate_tubule_score(9.9, cfg()) == 3
    assert calculate_tubule_score(0.0, cfg()) == 3


def test_exhaustive_27_grade_combinations():
    """
    Exhaustively test all 3 x 3 x 3 = 27 combinations of (Tubule, Pleomorphism, Mitosis).
    Verify that every combination calculates the correct Nottingham sum and Grade.
    """
    combos_tested = 0
    for t in [1, 2, 3]:
        for p in [1, 2, 3]:
            for m in [1, 2, 3]:
                nottingham_sum, grade = calculate_nottingham_grade(t, p, m, cfg())
                combos_tested += 1
                
                assert nottingham_sum == t + p + m
                if nottingham_sum in [3, 4, 5]:
                    assert grade == 1, f"Failed for {t},{p},{m}: sum={nottingham_sum}, expected Grade 1, got {grade}"
                elif nottingham_sum in [6, 7]:
                    assert grade == 2, f"Failed for {t},{p},{m}: sum={nottingham_sum}, expected Grade 2, got {grade}"
                elif nottingham_sum in [8, 9]:
                    assert grade == 3, f"Failed for {t},{p},{m}: sum={nottingham_sum}, expected Grade 3, got {grade}"
                else:
                    pytest.fail(f"Invalid sum {nottingham_sum} for {t},{p},{m}")

                # Invariant validator must pass for every valid combination
                validate_grading_invariants(t, p, m, nottingham_sum, grade, cfg())

    assert combos_tested == 27


def test_invariant_validation_failure():
    # Test invalid score values
    with pytest.raises(ValueError, match="Invariant Violation"):
        validate_grading_invariants(4, 2, 1, 7, 2, cfg())

    # Test sum mismatch
    with pytest.raises(ValueError, match="Invariant Violation"):
        validate_grading_invariants(1, 2, 2, 6, 2, cfg())  # 1+2+2 = 5 != 6

    # Test grade mismatch
    with pytest.raises(ValueError, match="Invariant Violation"):
        validate_grading_invariants(1, 1, 1, 3, 2, cfg())  # sum 3 must be Grade 1, not 2


def test_grade_needs_all_three_components_and_flags_the_boundary():
    assert aggregate_components(2, 3, None, cfg()) == {"total": None, "grade": None, "near_grade_boundary": False}
    assert aggregate_components(2, 3, 3, cfg()) == {"total": 8, "grade": 3, "near_grade_boundary": True}
    assert aggregate_components(1, 1, 1, cfg()) == {"total": 3, "grade": 1, "near_grade_boundary": False}
    assert [t for t in range(3, 10) if near_grade_boundary(t, cfg())] == [5, 6, 7, 8]


def test_stage5_result_uses_reviews_and_overrides_but_never_confidences():
    machine = {
        "tubule": {"samples": [
            {"id": "t_01", "tumor_area_um2": 1.0e5, "estimate": {"tumor_present": True, "tubule_percent": 22, "confidence": "low"}},
            {"id": "t_02", "tumor_area_um2": 1.0e5, "estimate": None},
        ]},
        "pleomorphism": {"fields": [
            {"id": "p_01", "estimate": {"pleomorphism_score": 3}},
            {"id": "p_02", "estimate": {"pleomorphism_score": 2}},
        ]},
        "mitotic": {"score": 3, "flags": ["hpf_count_lt_10"]},
        "flags": [],
    }
    result = stage5_result(machine, {}, cfg())
    assert (result["tubule_percent"], result["tubule_score"], result["pleo_score"], result["pleo_tie"]) == (22.0, 2, 3, True)
    assert result["total"] == 8 and result["grade"] == 3
    assert result["flags"] == ["needs_human", "near_grade_boundary", "hpf_count_lt_10"]  # t_02 failed, unreviewed

    overrides = {"reviews": {"tubule": {"t_02": {"tumor_present": True, "tubule_percent": 2}},
                             "pleo": {"p_01": {"pleomorphism_score": 2}}},
                 "tubule_score": 1, "reasons": {"tubule": "glands throughout"}}
    result = stage5_result(machine, overrides, cfg())
    assert result["tubule_percent"] == 12.0 and result["tubule_score"] == 2 and result["effective_tubule_score"] == 1
    assert result["pleo_score"] == 2 and result["total"] == 6 and result["flags"] == ["near_grade_boundary", "hpf_count_lt_10"]


def test_database_check_constraint_enforcement():
    """
    Test that SQLite and PostgreSQL enforce the Nottingham Grade CHECK constraint.
    """
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=test_engine)
    Session = sessionmaker(bind=test_engine)
    session = Session()

    case_id = uuid.uuid4()
    case = Case(id=case_id, created_by="test_user", status="open")
    session.add(case)
    session.commit()

    # 1. Insert valid row (T=2, P=3, M=3 -> Sum=8, Grade=3)
    valid_grading = Grading(
        case_id=case_id,
        tubule_percent=22.0,
        tubule_score=2,
        pleo_score=3,
        mitotic_score=3,
        nottingham_sum=8,
        grade=3,
        histologic_type="IDC-NST",
        type_confirmed_by="Dr. Pathologist",
        machine={},
        overrides={}
    )
    session.add(valid_grading)
    session.commit()

    # 2. Attempt to update with inconsistent grade (T=1, P=1, M=1 -> Sum=3, but Grade=3)
    valid_grading.tubule_score = 1
    valid_grading.pleo_score = 1
    valid_grading.mitotic_score = 1
    valid_grading.nottingham_sum = 3
    valid_grading.grade = 3  # Inconsistent! Must fail CHECK constraint

    with pytest.raises(IntegrityError):
        session.commit()

    session.rollback()
    session.close()


def test_mitotic_score_no_double_counting_in_overlapping_hpfs():
    """Finding #357: a figure inside two overlapping HPFs counts once (pipeline/scoring.py, the single implementation)."""
    from pipeline.scoring import summarize_stage4

    hpfs = [
        {"seq": 1, "center_um": [1000.0, 1000.0], "radius_um": 262.0},
        {"seq": 2, "center_um": [1200.0, 1000.0], "radius_um": 262.0}
    ]
    detections = [{"id": "m_overlap_01", "centroid_um": [1100.0, 1000.0], "counted": True,
                   "final_decision": "mitosis", "review_label": None}]
    cfg_m = get_pipeline_config().mitosis
    _, summary = summarize_stage4(detections, hpfs, scoring=cfg_m.scoring, hpf_count=cfg_m.hpf.count)
    assert summary["count_total"] == 1  # Not 2! Counted once despite being in both HPF 1 and HPF 2
    assert summary["mitotic_score"] == 1
