"""
Unit and Integration Tests for Audit Logging:
- #215, #708: Audit pagination tiebreaking by id.desc() and case_id normalization
- #189, #708: 404 Not Found for nonexistent and malformed case IDs on audit endpoints
- #644: stage_started audit event emission on stage approval
"""

import uuid
import pytest
from datetime import datetime, timezone
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.core.db import Base, get_db
from app.models.case import Case
from app.models.slide import Slide
from app.models.audit import AuditEvent
from app.models.stage_execution import StageExecution

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


client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.pop(get_db, None)


def test_unknown_case_id_audit_returns_404():
    """Verify audit endpoints return 404 for unknown or malformed case IDs (#189, #708)."""
    fake_case_id = str(uuid.uuid4())
    headers = {"X-Test-Role": "pathologist"}

    # GET audit events for unknown case
    r_audit = client.get(f"/api/v1/cases/{fake_case_id}/audit", headers=headers)
    assert r_audit.status_code == 404

    # Malformed non-UUID case string must return 404 (#189, #708)
    r_malformed = client.get("/api/v1/cases/not-a-valid-uuid/audit", headers=headers)
    assert r_malformed.status_code == 404


def test_audit_pagination_tiebreaker_order():
    """Verify audit query uses (created_at.desc(), id.desc()) for deterministic paging (#215)."""
    db = TestingSessionLocal()
    case_uid = uuid.uuid4()
    case_id_str = str(case_uid)

    try:
        case = Case(id=case_uid, created_by="test_admin", status="open")
        db.add(case)
        db.commit()

        # Insert multiple audit events sharing identical created_at timestamp
        fixed_time = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
        events = []
        for i in range(5):
            e = AuditEvent(
                case_id=case_id_str,
                actor="test_user",
                event_type=f"event_type_{i}",
                stage="triage",
                created_at=fixed_time
            )
            db.add(e)
            events.append(e)
        db.commit()

        # Fetch page 1
        resp = client.get(f"/api/v1/cases/{case_id_str}/audit?page=1&page_size=3")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["events"]) == 3

        # IDs must be strictly monotonically decreasing due to id.desc() tiebreak (#215)
        ids_p1 = [e["id"] for e in data["events"]]
        assert ids_p1 == sorted(ids_p1, reverse=True)

        # Fetch page 2
        resp2 = client.get(f"/api/v1/cases/{case_id_str}/audit?page=2&page_size=3")
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert len(data2["events"]) == 2
        ids_p2 = [e["id"] for e in data2["events"]]
        assert ids_p2 == sorted(ids_p2, reverse=True)

        # No duplicate IDs across pages
        assert set(ids_p1).isdisjoint(set(ids_p2))

    finally:
        db.close()


def test_stage_started_audit_event_emission():
    """Verify approve_case_stage emits both stage_confirmed and stage_started audit events (#644).

    Approving triage confirms it through the stage service, which reads the triage output.
    """
    import json
    from app.core.config import settings
    from app.core.gcs import upload_blob_from_bytes

    db = TestingSessionLocal()
    case_uid = uuid.uuid4()
    slide_uid = uuid.uuid4()

    try:
        case = Case(id=case_uid, created_by="test_doc", status="in_review")
        db.add(case)
        slide = Slide(id=slide_uid, case_id=case_uid, gcs_uri_original="gs://bucket/test.svs")
        db.add(slide)
        stage_exec = StageExecution(case_id=case_uid, stage="triage", attempt=1, status="awaiting_review")
        db.add(stage_exec)
        db.commit()
        hotspot = {"id": "hs_1", "center_um": [300.0, 300.0], "polygon_um": [[0, 0], [600, 0], [600, 600], [0, 600], [0, 0]], "source": "model", "excluded": False}
        upload_blob_from_bytes(
            settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_uid}/triage/output.json",
            json.dumps({"hotspots": [{**hotspot, "id": f"hs_{k}", "center_um": [300.0 + 700.0 * k, 300.0]} for k in range(10)], "hpf_diameter_um": 500.0, "frame_um": 600.0, "hpf_target": 10}).encode("utf-8"), "application/json",
        )

        with patch("app.routers.cases.dispatch_stage_task"):
            resp = client.post(
                f"/api/v1/cases/{case_uid}/stages/triage/approve",
                json={"review_comment": "Verified triage heatmap quality"},
                headers={"X-Test-Role": "pathologist"}
            )
        assert resp.status_code == 202
        res_data = resp.json()
        assert res_data["status"] == "approved"
        assert res_data["approved_stage"] == "triage"
        assert res_data["next_stage"] == "mitosis"

        # Check emitted audit events
        audit_events = db.scalars(
            select(AuditEvent)
            .where(AuditEvent.case_id == str(case_uid))
            .order_by(AuditEvent.id.asc())
        ).all()

        event_types = [e.event_type for e in audit_events]
        assert "stage_confirmed" in event_types
        assert "stage_started" in event_types

        started_evt = next(e for e in audit_events if e.event_type == "stage_started")
        assert started_evt.stage == "mitosis"
        assert started_evt.payload["triggered_by_approval_of"] == "triage"

    finally:
        db.close()
