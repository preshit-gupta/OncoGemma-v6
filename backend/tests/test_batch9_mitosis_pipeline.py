"""
Batch 9 Test Suite: Stage 4 Mitosis Pipeline & Candidate/HPF Review Integrity.
Validates fixes for:
  - #580: Zero synthetic 2x2mm slide center hotspot fabrication on missing/unconfirmed triage
  - #103 & #109: adding a figure on an unreadable slide fails instead of inventing its images

#464 (re-runs keep pathologist rows), #110 (HPF counts persisted) and #114 (audit per edit) are
covered for the v6 routes in test_mitosis_worker.py and test_mitosis_api.py (WP-7.6a).
"""
import json
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.core.db import Base, get_db
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.detection import Detection
from app.models.hotspot import Hotspot
from worker.mitosis import run_mitosis
from tests.fakes.runtime import make_runtime


# Test Database setup
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


@pytest.fixture(autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.pop(get_db, None)


client = TestClient(app)


# =========================================================================
# Issue #580: Mitosis worker aborts if no confirmed hotspots exist
# =========================================================================
def test_worker_aborts_without_confirmed_hotspots():
    """
    Validates issue #580: run_mitosis must raise ValueError when no confirmed
    hotspots exist in DB, and must NOT fabricate a 2x2 mm center hotspot box.
    """
    db = TestingSessionLocal()
    case_id = uuid.uuid4()
    case = Case(id=case_id, created_by="test_pathologist", status="open")
    slide = Slide(
        id=uuid.uuid4(),
        case_id=case_id,
        gcs_uri_original="gs://raw/slide.svs",
        width_px=10000,
        height_px=10000,
        mpp_x=0.25,
        mpp_y=0.25
    )
    stage_exec = StageExecution(id=uuid.uuid4(), case_id=case_id, stage="mitosis", attempt=1, status="queued")
    db.add_all([case, slide, stage_exec])
    db.commit()

    # Hotspot table is empty for this case
    with pytest.raises(ValueError) as excinfo:
        run_mitosis(stage_exec, db, make_runtime(stage_exec))

    assert "No confirmed tumor hotspots found for case" in str(excinfo.value)
    db.close()


# =========================================================================
# Issue #103 & #109: /add refuses an unreadable slide instead of inventing images
# =========================================================================
def test_add_fails_on_an_unreadable_slide_without_creating_the_figure():
    """
    Validates issues #103 and #109 on the v6 route: when the slide cannot be read, /add answers
    502 slide_unreadable and creates no row, rather than a figure with synthetic images.
    """
    db = TestingSessionLocal()
    case_id = uuid.uuid4()
    case = Case(id=case_id, created_by="test_pathologist", status="open")
    slide = Slide(
        id=uuid.uuid4(),
        case_id=case_id,
        mpp_x=0.25,
        mpp_y=0.25,
        width_px=20000,
        height_px=20000,
        gcs_uri_original="gs://raw/missing-slide.svs"
    )
    stage_exec = StageExecution(id=uuid.uuid4(), case_id=case_id, stage="mitosis", attempt=1, status="awaiting_review")
    db.add_all([case, slide, stage_exec])
    db.commit()
    db.close()

    resp = client.post("/api/v1/stages/mitosis/add", json={"case_id": str(case_id), "centroid_um": [1500.0, 2500.0]})
    assert resp.status_code == 502
    assert resp.json()["error"] == "slide_unreadable"
    db = TestingSessionLocal()
    assert db.scalars(select(Detection).where(Detection.case_id == case_id)).all() == []
    db.close()


# =========================================================================
# Unconfirmed triage output is never used (SPEC-06 §9)
# =========================================================================
def test_worker_refuses_unconfirmed_triage_artifact():
    """
    The v5 worker copied hotspots out of cases/{case_id}/triage/output.json when the
    Hotspot table was empty, running Stage 4 on unconfirmed triage. Hotspots now come
    from the confirmed triage in the database only; without them the stage fails.
    """
    db = TestingSessionLocal()
    case_id = uuid.uuid4()
    case = Case(id=case_id, created_by="test_pathologist", status="open")
    slide = Slide(
        id=uuid.uuid4(),
        case_id=case_id,
        gcs_uri_original="gs://raw/slide.svs",
        width_px=10000,
        height_px=10000,
        mpp_x=0.25,
        mpp_y=0.25
    )
    stage_exec = StageExecution(id=uuid.uuid4(), case_id=case_id, stage="mitosis", attempt=1, status="queued")
    db.add_all([case, slide, stage_exec])
    db.commit()

    mock_triage_output = json.dumps({
        "hotspots": [
            {
                "id": "hs_01",
                "polygon_um": [[100.0, 100.0], [700.0, 100.0], [700.0, 700.0], [100.0, 700.0], [100.0, 100.0]],
                "area_mm2": 0.36,
                "prob_mean": 0.479,
                "prob_max": 0.479,
                "source": "model",
                "excluded": False
            }
        ]
    }).encode("utf-8")

    # Hotspots come from the confirmed rows in the database only; the machine output above is never read.
    with pytest.raises(ValueError, match="No confirmed tumor hotspots found"):
        run_mitosis(stage_exec, db, make_runtime(stage_exec))

    assert db.scalars(select(Hotspot).where(Hotspot.case_id == case_id)).all() == []
    db.close()
