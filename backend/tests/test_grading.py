import pytest
import uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

from app.core.pipeline_config import get_pipeline_config
from pipeline.grading import (
    weighted_median,
    weighted_mode,
    calculate_tubule_score,
    calculate_nottingham_grade,
    validate_grading_invariants,
    aggregate_grading_findings,
)
from app.models.case import Case
from app.models.grading import Grading
from app.core.db import Base


def cfg():
    return get_pipeline_config().scoring


def test_weighted_median_basic():
    # Simple unweighted equal cases
    vals = [10.0, 20.0, 30.0]
    weights = [1.0, 1.0, 1.0]
    assert weighted_median(vals, weights) == 20.0

    # Skewed weights pulling median towards higher values
    vals = [10.0, 20.0, 80.0]
    weights = [0.5, 0.5, 3.0]
    assert weighted_median(vals, weights) == 80.0

    # Single value
    assert weighted_median([45.0], [1.0]) == 45.0
    # Empty
    assert weighted_median([], []) == 0.0


def test_weighted_mode_and_tie_breaking():
    # Clear majority
    vals = [1, 2, 2, 3]
    weights = [1.0, 1.0, 1.0, 1.0]
    winning_score, disp = weighted_mode(vals, weights, tie_breaker=max)
    assert winning_score == 2
    assert disp == 0.5

    # Exact tie between Score 1 and Score 3: conservative tie_breaker resolves to Score 3 (worse grade)
    vals = [1, 3]
    weights = [1.5, 1.5]
    winning_score, disp = weighted_mode(vals, weights, tie_breaker=max)
    assert winning_score == 3
    assert disp == 0.5

    # Exact tie between Score 2 and Score 3: resolves to Score 3
    vals = [2, 3]
    weights = [1.0, 1.0]
    winning_score, _ = weighted_mode(vals, weights, tie_breaker=max)
    assert winning_score == 3


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


def test_aggregate_grading_findings_flow():
    tubule_responses = [
        {"tubule_percent": 25, "tumor_present": True, "confidence": "high"},
        {"tubule_percent": 20, "tumor_present": True, "confidence": "medium"},
        {"tubule_percent": 30, "tumor_present": True, "confidence": "medium"},
        {"tubule_percent": 0, "tumor_present": False, "confidence": "low"},  # Non-tumor patch filtered
    ] + [{"tubule_percent": 22, "tumor_present": True, "confidence": "medium"} for _ in range(8)]

    pleo_responses = [
        {"pleomorphism_score": 3, "rationale": "Marked nucleomegaly", "confidence": "high"},
        {"pleomorphism_score": 3, "rationale": "Prominent nucleoli", "confidence": "medium"},
        {"pleomorphism_score": 2, "rationale": "Moderate atypia", "confidence": "low"},
    ] + [{"pleomorphism_score": 3, "rationale": "Atypia", "confidence": "medium"} for _ in range(8)]

    result = aggregate_grading_findings(
        tubule_responses=tubule_responses,
        pleo_responses=pleo_responses,
        mitotic_score=3,
        cfg=cfg()
    )

    assert result["tubule_score"] == 2  # ~22% tubule -> Score 2
    assert result["pleo_score"] == 3    # Score 3 dominant
    assert result["mitotic_score"] == 3
    assert result["nottingham_sum"] == 8 # 2 + 3 + 3 = 8
    assert result["grade"] == 3         # Sum 8 -> Grade 3
    assert result["flags"] == []        # >8 tumor patches, low dispersion


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
    """
    Verify Finding #357: Mitoses falling within overlapping HPF circles are counted once,
    preventing double-counting in Nottingham score calculation.
    """
    from pipeline.grading import calculate_mitotic_score_from_detections_and_hpfs

    # Two overlapping HPFs
    hpfs = [
        {"seq": 1, "center_um": [1000.0, 1000.0], "radius_um": 262.0},
        {"seq": 2, "center_um": [1200.0, 1000.0], "radius_um": 262.0}
    ]

    # One mitosis right in the intersection
    detections = [
        {"id": "m_overlap_01", "centroid_um": [1100.0, 1000.0], "counted": True}
    ]

    unique_total, score = calculate_mitotic_score_from_detections_and_hpfs(detections, hpfs, get_pipeline_config().mitosis.scoring)
    assert unique_total == 1  # Not 2! Counted once despite being in both HPF 1 and HPF 2
    assert score == 1
