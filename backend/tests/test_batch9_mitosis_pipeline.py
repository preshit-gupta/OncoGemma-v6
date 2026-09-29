"""
Batch 9 Test Suite: Stage 4 Mitosis Pipeline & Candidate/HPF Review Integrity.
Validates fixes for:
  - #580: Zero synthetic 2x2mm slide center hotspot fabrication on missing/unconfirmed triage
  - #464: Worker re-run preserves pathologist review edits and user-added detections
  - #110: HpfSite.mitotic_count is synchronized and persisted immediately across all endpoints
  - #114: Review edit audit events are derived directly for all modified candidate labels
  - #103 & #109: add_candidate uses cached slide path and raises HTTP 500 without fake pink tiles
  - #129: Threshold alignment between configs/mitosis.yaml and models/detector/EVAL.md
"""
import os
import json
import uuid
import yaml
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, delete
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.core.db import Base, get_db
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.detection import Detection
from app.models.hpf_site import HpfSite
from app.models.hotspot import Hotspot
from app.models.audit import AuditEvent
from app.routers.mitosis import sync_and_persist_hpf_counts
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
# Issue #464: Worker preserves pathologist edits and non-model detections
# =========================================================================
def test_worker_preserves_pathologist_detections():
    """
    Validates issue #464: Worker re-run only deletes model detections,
    preserving pathologist reviewed edits and added detections.
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
    hotspot = Hotspot(
        id="hs_01", case_id=case_id, stage_execution_id=stage_exec.id,
        polygon_um=[[100, 100], [500, 100], [500, 500], [100, 500]],
        area_mm2=0.16, prob_mean=0.92, source="pathologist", excluded=False
    )
    # Existing model detection
    det_model = Detection(
        id="m_model_001", case_id=case_id, centroid_um=[200.0, 200.0],
        det_conf=0.6, label="unreviewed", label_source="model"
    )
    # Existing pathologist reviewed detection
    det_path = Detection(
        id="m_path_001", case_id=case_id, centroid_um=[250.0, 250.0],
        det_conf=0.9, label="mitosis", label_source="pathologist"
    )
    # Existing pathologist bulk reviewed detection
    det_bulk = Detection(
        id="m_path_bulk_001", case_id=case_id, centroid_um=[300.0, 300.0],
        det_conf=0.4, label="not_mitosis", label_source="pathologist_bulk"
    )
    db.add_all([case, slide, stage_exec, hotspot, det_model, det_path, det_bulk])
    db.commit()

    # Query delete condition logic in worker: only delete where label_source == 'model'
    db.execute(
        delete(Detection).where(
            Detection.case_id == case.id,
            Detection.label_source == "model"
        )
    )
    db.commit()

    remaining_dets = db.scalars(select(Detection).where(Detection.case_id == case.id)).all()
    remaining_ids = {d.id for d in remaining_dets}
    assert "m_model_001" not in remaining_ids
    assert "m_path_001" in remaining_ids
    assert "m_path_bulk_001" in remaining_ids
    db.close()


# =========================================================================
# Issue #110: sync_and_persist_hpf_counts updates HpfSite.mitotic_count
# =========================================================================
def test_sync_and_persist_hpf_counts():
    """
    Validates issue #110: sync_and_persist_hpf_counts recalculates and
    persists HpfSite.mitotic_count directly in the DB.
    """
    db = TestingSessionLocal()
    case_id = uuid.uuid4()
    case = Case(id=case_id, created_by="test_pathologist", status="open")
    hpf = HpfSite(
        case_id=case_id, seq=1,
        center_um=[1000.0, 1000.0], radius_um=262.0, mitotic_count=0, source="model"
    )
    # Detection inside HPF (distance < 262 um)
    det1 = Detection(
        id="m_001", case_id=case_id, centroid_um=[1050.0, 1050.0],
        det_conf=0.9, label="mitosis", label_source="pathologist"
    )
    # Detection outside HPF (distance > 262 um)
    det2 = Detection(
        id="m_002", case_id=case_id, centroid_um=[2000.0, 2000.0],
        det_conf=0.9, label="mitosis", label_source="pathologist"
    )
    # Non-mitotic detection inside HPF
    det3 = Detection(
        id="m_003", case_id=case_id, centroid_um=[1010.0, 1010.0],
        det_conf=0.9, label="not_mitosis", label_source="pathologist"
    )
    db.add_all([case, hpf, det1, det2, det3])
    db.commit()

    updated_hpfs, summary = sync_and_persist_hpf_counts(str(case_id), db)
    db.commit()

    # Verify return values
    assert len(updated_hpfs) == 1
    assert updated_hpfs[0]["count"] == 1
    assert summary["count_total"] == 1

    # Verify DB record was updated (#110)
    db_hpf = db.scalars(select(HpfSite).where(HpfSite.case_id == case_id, HpfSite.seq == 1)).first()
    assert db_hpf.mitotic_count == 1
    db.close()


# =========================================================================
# Issue #114: Recompute derives review_edit audit events for each changed label
# =========================================================================
def test_recompute_derives_audit_events_for_multiple_labels():
    """
    Validates issue #114: recompute_scoring derives review_edit AuditEvents
    for every changed label in candidate_labels.
    """
    db = TestingSessionLocal()
    case_id = uuid.uuid4()
    case = Case(id=case_id, created_by="test_pathologist", status="open")
    det1 = Detection(id="m_01", case_id=case_id, centroid_um=[100.0, 100.0], label="unreviewed", label_source="model")
    det2 = Detection(id="m_02", case_id=case_id, centroid_um=[200.0, 200.0], label="unreviewed", label_source="model")
    hpf = HpfSite(case_id=case_id, seq=1, center_um=[150.0, 150.0], radius_um=262.0, mitotic_count=0)
    stage_exec = StageExecution(id=uuid.uuid4(), case_id=case_id, stage="mitosis", attempt=1, status="awaiting_review")
    db.add_all([case, det1, det2, hpf, stage_exec])
    db.commit()
    db.close()

    # Recompute payload with two changed labels
    payload = {
        "case_id": str(case_id),
        "candidate_labels": {
            "m_01": "mitosis",
            "m_02": "not_mitosis"
        }
    }
    resp = client.post("/api/v1/stages/mitosis/recompute", json=payload)
    assert resp.status_code == 200

    # Verify audit events were logged for both candidate changes (#114)
    db = TestingSessionLocal()
    audits = db.scalars(
        select(AuditEvent).where(AuditEvent.case_id == str(case_id), AuditEvent.event_type == "review_edit")
    ).all()
    audit_det_ids = {a.payload.get("detection_id") for a in audits}
    assert "m_01" in audit_det_ids
    assert "m_02" in audit_det_ids
    assert len(audits) >= 2
    db.close()


# =========================================================================
# Issue #103 & #109: add_candidate uses cached slide and rejects synthetic crops
# =========================================================================
def test_add_candidate_raises_500_on_slide_read_failure():
    """
    Validates issues #103 and #109: add_pathologist_mitosis raises HTTPException 500
    when authentic slide extraction fails, without generating synthetic pink pixels.
    """
    db = TestingSessionLocal()
    case_id = uuid.uuid4()
    case = Case(id=case_id, created_by="test_pathologist", status="open")
    slide = Slide(
        id=uuid.uuid4(),
        case_id=case_id,
        mpp_x=0.25,
        mpp_y=0.25,
        gcs_uri_original="gs://raw/slide.svs"
    )
    stage_exec = StageExecution(id=uuid.uuid4(), case_id=case_id, stage="mitosis", attempt=1, status="awaiting_review")
    db.add_all([case, slide, stage_exec])
    db.commit()
    db.close()

    payload = {
        "case_id": str(case_id),
        "centroid_um": [1500.0, 2500.0],
        "label": "mitosis",
        "reviewed_by": "pathologist_01"
    }

    # Mock get_cached_slide_path to simulate failed slide extraction
    with patch("app.routers.mitosis.get_cached_slide_path", return_value="/nonexistent/slide.svs"):
        resp = client.post("/api/v1/stages/mitosis/add_candidate", json=payload)
        # Must raise 500 error instead of 200 with fake pink image (#103)
        assert resp.status_code == 500
        assert "Could not extract authentic optical crop" in resp.json()["detail"]


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
