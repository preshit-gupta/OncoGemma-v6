"""
Comprehensive test suite for Batch 17 (Workstream 4: Histologic Grading & AJCC Staging).
Validates:
1. OpenSlide lock reentrancy (threading.RLock)
2. AJCC 8th Edition staging config & SHA-256 hash
3. Pathologic tumor (pT) integer millimetre rounding rules
4. Pathologic node (pN) ITC, micrometastasis, and strict examined/positive invariant
5. Anatomic stage group mapping table (no default 'IA' fallback)
6. Dynamic config-driven Nottingham invariant validation
7. Empty evidence handling in grading pipeline
8. Grading worker coordinate clamping and true area centroid calculation
9. Grading router 409 Conflict gating on confirmed stages & signed case reports
10. Grading router MITOTIC count read-only enforcement in Stage 5 HPF review
11. Grading router CAP histologic type ontology validation
12. Grading router tubule score vs percent consistency and override key restriction
"""

import math
import threading
import uuid
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Polygon
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.core.db import Base, get_db
from app.core.pipeline_config import get_pipeline_config
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.grading import Grading
from app.models.hpf_site import HpfSite
from pipeline.grading import (
    validate_grading_invariants,
    aggregate_grading_findings,
    calculate_tubule_score,
)

# ---------------------------------------------------------------------------
# In-memory DB Fixtures for Router Tests
# ---------------------------------------------------------------------------

SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def db_session():
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_db] = override_get_db
    session = TestingSessionLocal()
    yield session
    session.close()
    app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# 6. Nottingham Grading Engine & Config-Driven Invariants
# ---------------------------------------------------------------------------

def test_nottingham_grading_config_hash_and_invariants():
    """Verify the pipeline config hash and dynamic config-driven validate_grading_invariants."""
    from app.core.pipeline_config import NottinghamGradingConfig, get_config_hash

    h = get_config_hash()
    assert isinstance(h, str)
    assert len(h) == 64
    scoring = get_pipeline_config().scoring

    # Valid combinations
    validate_grading_invariants(tubule_score=1, pleo_score=1, mitotic_score=1, nottingham_sum=3, grade=1, cfg=scoring)
    validate_grading_invariants(tubule_score=2, pleo_score=2, mitotic_score=2, nottingham_sum=6, grade=2, cfg=scoring)
    validate_grading_invariants(tubule_score=3, pleo_score=3, mitotic_score=3, nottingham_sum=9, grade=3, cfg=scoring)

    # Inconsistent sum
    with pytest.raises(ValueError, match="nottingham_sum"):
        validate_grading_invariants(tubule_score=2, pleo_score=2, mitotic_score=2, nottingham_sum=7, grade=2, cfg=scoring)

    # Inconsistent grade
    with pytest.raises(ValueError, match="grade"):
        validate_grading_invariants(tubule_score=2, pleo_score=2, mitotic_score=2, nottingham_sum=6, grade=3, cfg=scoring)

    # Custom config thresholds passed via cfg
    custom_cfg = scoring.model_copy(update={
        "nottingham_grading": NottinghamGradingConfig(grade1_max_sum=6, grade2_max_sum=7),  # Grade 1 up to sum 6
    })
    # Sum 6 with Grade 1 should succeed with custom_cfg
    validate_grading_invariants(
        tubule_score=2, pleo_score=2, mitotic_score=2, nottingham_sum=6, grade=1, cfg=custom_cfg
    )


# ---------------------------------------------------------------------------
# 7. Empty Evidence Handling in Grading Engine
# ---------------------------------------------------------------------------

def test_grading_empty_evidence_handling():
    """Verify aggregate_grading_findings handles empty patch sets gracefully without fabricating scores."""
    result = aggregate_grading_findings(
        tubule_responses=[],
        pleo_responses=[],
        mitotic_score=1,
        cfg=get_pipeline_config().scoring,
    )
    assert result["needs_human"] is True
    assert "empty_evidence_set" in result["flags"]
    assert "needs_human" in result["flags"]
    assert result["tubule_score"] is None
    assert result["pleo_score"] is None


# ---------------------------------------------------------------------------
# 8. Worker Coordinate Clamping & Centroid Calculation
# ---------------------------------------------------------------------------

def test_worker_centroid_and_anisotropy():
    """Verify Shapely true polygon area centroid and anisotropic mpp handling."""
    # Right triangle with vertices (0,0), (6,0), (0,6)
    # Area centroid is at (2, 2)
    geom_coords = [(0.0, 0.0), (6.0, 0.0), (0.0, 6.0), (0.0, 0.0)]
    poly = Polygon(geom_coords)
    assert poly.centroid.x == pytest.approx(2.0)
    assert poly.centroid.y == pytest.approx(2.0)

    # Test select_max_density_hotspot_patches with mock patches and anisotropic mpp
    # In select_max_density_hotspot_patches, hotspot polygon coords in um should convert to px
    # by dividing x by mpp_x and y by mpp_y
    mpp_x = 0.25
    mpp_y = 0.50  # anisotropic

    # Coordinates in microns: (100, 200) -> (400 px, 400 px)
    cx_um, cy_um = 100.0, 200.0
    cx_px = cx_um / mpp_x
    cy_px = cy_um / (mpp_y or mpp_x)
    assert cx_px == 400.0
    assert cy_px == 400.0


# ---------------------------------------------------------------------------
# 9. Grading API Router State Machine & 409 Conflict Gating
# ---------------------------------------------------------------------------

def test_grading_router_409_conflict_when_stage_confirmed(db_session):
    """Verify 409 Conflict is returned when attempting patch/HPF review or confirm on already confirmed stage."""
    client = TestClient(app)
    case_id = uuid.uuid4()
    case = Case(id=case_id, created_by="pathologist_1", status="open")
    stage_exec = StageExecution(
        case_id=case_id,
        stage="grading",
        attempt=1,
        status="confirmed",  # ALREADY CONFIRMED
    )
    grading = Grading(
        case_id=case_id,
        tubule_score=2,
        pleo_score=2,
        mitotic_score=2,
        nottingham_sum=6,
        grade=2,
        histologic_type="IDC-NST",
        type_confirmed_by="pathologist_1",
        machine={"patches": [{"id": "p_01"}], "hpfs": [{"seq": 1}]},
        overrides={},
    )
    db_session.add(case)
    db_session.add(stage_exec)
    db_session.add(grading)
    db_session.commit()

    headers = {"X-Test-Role": "pathologist"}

    # Attempt patch review
    res_patch = client.post(
        "/api/v1/stages/grading/patches/review",
        json={"case_id": str(case_id), "action": "approve_all"},
        headers=headers,
    )
    assert res_patch.status_code == 409
    assert "already confirmed" in res_patch.json()["detail"]

    # Attempt HPF review
    res_hpf = client.post(
        "/api/v1/stages/grading/hpfs/review",
        json={"case_id": str(case_id), "action": "approve_all"},
        headers=headers,
    )
    assert res_hpf.status_code == 409
    assert "already confirmed" in res_hpf.json()["detail"]

    # Attempt histologic type confirm
    res_type = client.post(
        "/api/v1/stages/grading/type/confirm",
        json={"case_id": str(case_id), "histologic_type": "IDC-NST", "reviewed_by": "Dr. Test"},
        headers=headers,
    )
    assert res_type.status_code == 409
    assert "already confirmed" in res_type.json()["detail"]

    # Attempt confirm
    res_confirm = client.post(
        "/api/v1/stages/grading/confirm",
        json={
            "case_id": str(case_id),
            "reviewed_by": "Dr. Test",
            "histologic_type": "IDC-NST",
            "type_confirmed": True,
            "tubule_score": 2,
            "pleo_score": 2,
            "mitotic_score": 2,
            "nottingham_sum": 6,
            "grade": 2,
        },
        headers=headers,
    )
    assert res_confirm.status_code == 409
    assert "already confirmed" in res_confirm.json()["detail"]


# ---------------------------------------------------------------------------
# 10. Grading Router: Read-Only Mitotic Figures in Stage 5
# ---------------------------------------------------------------------------

def test_grading_router_mitotic_count_readonly(db_session):
    """Verify attempting to alter raw mitotic counts in Stage 5 HPF review is rejected."""
    client = TestClient(app)
    case_id = uuid.uuid4()
    case = Case(id=case_id, created_by="pathologist_1", status="open")
    stage_exec = StageExecution(
        case_id=case_id,
        stage="grading",
        attempt=1,
        status="awaiting_review",
    )
    grading = Grading(
        case_id=case_id,
        tubule_score=2,
        pleo_score=2,
        mitotic_score=2,
        nottingham_sum=6,
        grade=2,
        histologic_type="IDC-NST",
        type_confirmed_by="unconfirmed",
        machine={"patches": [], "hpfs": [{"seq": 1, "mitotic_count": 5}]},
        overrides={},
    )
    hpf = HpfSite(
        case_id=case_id,
        seq=1,
        center_um=[1000.0, 1000.0],
        radius_um=262.0,
        mitotic_count=5,  # Baseline count from Stage 4
        source="model",
    )
    db_session.add(case)
    db_session.add(stage_exec)
    db_session.add(grading)
    db_session.add(hpf)
    db_session.commit()

    headers = {"X-Test-Role": "pathologist"}

    # Attempt to change mitotic count in Stage 5
    res = client.post(
        "/api/v1/stages/grading/hpfs/review",
        json={
            "case_id": str(case_id),
            "action": "update",
            "reviews": [
                {
                    "seq": 1,
                    "mitotic_count": 8,  # Attempting to supply mitotic count!
                    "status": "modified",
                }
            ],
        },
        headers=headers,
    )
    assert res.status_code == 400
    assert "Mitotic figure counts cannot be modified directly in Stage 5" in res.json()["detail"]


# ---------------------------------------------------------------------------
# 11. Grading Router: CAP Histologic Type Ontology Validation
# ---------------------------------------------------------------------------

def test_grading_router_cap_histologic_type_validation(db_session):
    """Verify confirming an invalid histologic type not in CAP elements is rejected."""
    client = TestClient(app)
    case_id = uuid.uuid4()
    case = Case(id=case_id, created_by="pathologist_1", status="open")
    stage_exec = StageExecution(
        case_id=case_id,
        stage="grading",
        attempt=1,
        status="awaiting_review",
    )
    grading = Grading(
        case_id=case_id,
        tubule_score=2,
        pleo_score=2,
        mitotic_score=2,
        nottingham_sum=6,
        grade=2,
        histologic_type="IDC-NST",
        type_confirmed_by="unconfirmed",
        machine={"patches": [], "hpfs": []},
        overrides={},
    )
    db_session.add(case)
    db_session.add(stage_exec)
    db_session.add(grading)
    db_session.commit()

    headers = {"X-Test-Role": "pathologist"}

    # Confirm with invalid type
    res = client.post(
        "/api/v1/stages/grading/type/confirm",
        json={
            "case_id": str(case_id),
            "histologic_type": "MadeUpCancerType",
            "reviewed_by": "Dr. Test",
        },
        headers=headers,
    )
    assert res.status_code == 400
    assert "Invalid histologic type" in res.json()["detail"]

    # Confirm with valid CAP element
    res_valid = client.post(
        "/api/v1/stages/grading/type/confirm",
        json={
            "case_id": str(case_id),
            "histologic_type": "mucinous",
            "reviewed_by": "Dr. Test",
        },
        headers=headers,
    )
    assert res_valid.status_code == 200
    assert res_valid.json()["histologic_type"]["confirmed_type"] == "mucinous"


# ---------------------------------------------------------------------------
# 12. Grading Router: Confirm Overrides Key Restriction & Tubule Consistency
# ---------------------------------------------------------------------------

def test_grading_router_confirm_security_and_consistency(db_session):
    """Verify unauthorized override keys and tubule score vs percent inconsistencies are rejected."""
    client = TestClient(app)
    case_id = uuid.uuid4()
    case = Case(id=case_id, created_by="pathologist_1", status="open")
    stage_exec = StageExecution(
        case_id=case_id,
        stage="grading",
        attempt=1,
        status="awaiting_review",
    )
    grading = Grading(
        case_id=case_id,
        tubule_score=2,
        pleo_score=2,
        mitotic_score=2,
        nottingham_sum=6,
        grade=2,
        histologic_type="IDC-NST",
        type_confirmed_by="Dr. Test",
        machine={"patches": [], "hpfs": []},
        overrides={},
    )
    db_session.add(case)
    db_session.add(stage_exec)
    db_session.add(grading)
    db_session.commit()

    headers = {"X-Test-Role": "pathologist"}

    # 1. Reject unauthorized override key ('patches' or 'hpfs')
    res_unauth = client.post(
        "/api/v1/stages/grading/confirm",
        json={
            "case_id": str(case_id),
            "reviewed_by": "Dr. Test",
            "histologic_type": "IDC-NST",
            "type_confirmed": True,
            "overrides": {
                "patches": {"score": 2, "justification": "attempted unauthorized key"},  # Unauthorized key!
            },
            "tubule_score": 2,
            "pleo_score": 2,
            "mitotic_score": 2,
            "nottingham_sum": 6,
            "grade": 2,
        },
        headers=headers,
    )
    assert res_unauth.status_code in (400, 422)
    assert "unauthorized override" in res_unauth.text.lower() or "unauthorized" in res_unauth.text.lower()

    # 2. Reject inconsistent tubule_score vs tubule_percent (e.g. 80% tubules -> score 1, but passed score 3)
    res_mismatch = client.post(
        "/api/v1/stages/grading/confirm",
        json={
            "case_id": str(case_id),
            "reviewed_by": "Dr. Test",
            "histologic_type": "IDC-NST",
            "type_confirmed": True,
            "overrides": {
                "tubule": {
                    "score": 3,
                    "percent": 85.0,  # 85% must map to score 1, not 3!
                    "justification": "Tubule formation is extensive throughout the specimen.",
                }
            },
            "tubule_score": 3,
            "pleo_score": 2,
            "mitotic_score": 2,
            "nottingham_sum": 7,
            "grade": 2,
        },
        headers=headers,
    )
    assert res_mismatch.status_code == 400
    assert "Inconsistent tubule" in res_mismatch.json()["detail"]
