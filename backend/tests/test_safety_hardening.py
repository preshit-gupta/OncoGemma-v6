"""
Tests for WP-4.3: Safety Hardening (SPEC-03 §5, AC6-AC10).
Covers:
- Route and database separation in prod vs test (AC6)
- Soft delete and 7-day retention purge
- Immutable audit events triggers in SQLite / DB
- Health check information leak prevention
- Prompt injection controls via render_prompt (AC7)
- Geometry safety and hotspot overlap rejection (AC8)
- Idempotency-Key enforcement, replay, and conflicts (AC9)
- Rate limiting middleware on session creation and mutating routes
"""
import json
import uuid
from enum import Enum
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models
from app.core.config import settings
from app.core.db import Base, get_db
from app.core.pipeline_config import get_pipeline_config
from app.main import create_app
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.hotspot import Hotspot
from app.models.audit import AuditEvent
from app.services.purge import purge_soft_deleted_cases
from app.core.geometry import validate_polygon_geometry, validate_hotspots_non_overlapping
from app.inference.errors import PromptVariableError
from app.inference.gateway import render_prompt
from app.auth.deps import CurrentUser, current_user


@pytest.fixture
def safety_test_env():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)

    # Install SQLite audit trigger
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS trg_audit_events_no_update
            BEFORE UPDATE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit_events is append-only: updates are prohibited');
            END;
        """))
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS trg_audit_events_no_delete
            BEFORE DELETE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit_events is append-only: deletions are prohibited');
            END;
        """))

    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    test_app = create_app("test")
    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[current_user] = lambda: CurrentUser(
        id="test_admin", email="admin@example.org", role="admin"
    )

    client = TestClient(test_app)
    db = TestingSessionLocal()

    yield client, db, engine, override_get_db

    db.close()
    Base.metadata.drop_all(bind=engine)


def test_route_separation_prod_vs_test():
    """AC6: Admin reset-database and bulk delete cases must only mount in test mode."""
    prod_app = create_app("prod")
    client_prod = TestClient(prod_app)

    # In prod, admin router is not mounted -> 404
    res_admin = client_prod.post("/api/v1/admin/reset-database")
    assert res_admin.status_code == 404

    # In prod, bulk DELETE /cases is not mounted -> 404 or 405
    res_bulk_del = client_prod.delete("/api/v1/cases")
    assert res_bulk_del.status_code in (404, 405)

    # In test, admin router and cases_test_router are mounted
    test_app = create_app("test")

    routes = [r.path for r in test_app.routes]
    assert "/api/v1/admin/reset-database" in routes
    assert "/api/v1/cases" in routes


def test_soft_delete_and_retention_purge(safety_test_env):
    """Soft delete marks case deleted_at, hides from list, and purges after retention threshold."""
    client, db, _, _ = safety_test_env

    # 1. Create a case with valid specimen_type
    case_id = uuid.uuid4()
    case = Case(
        id=case_id,
        status="open",
        created_by="pathologist_test",
        specimen_type="core_biopsy"
    )
    db.add(case)
    db.commit()

    # 2. Soft delete case via API (returns 204 No Content)
    resp = client.delete(f"/api/v1/cases/{case_id}")
    assert resp.status_code == 204

    # Verify deleted_at is populated in DB
    db.refresh(case)
    assert case.deleted_at is not None

    # Verify case is excluded from list_cases
    resp_list = client.get("/api/v1/cases")
    assert resp_list.status_code == 200
    assert not any(c["id"] == str(case_id) for c in resp_list.json())

    # Verify single case lookup returns 404
    resp_get = client.get(f"/api/v1/cases/{case_id}")
    assert resp_get.status_code == 404

    # Verify audit event was logged
    audit = db.scalars(
        select(AuditEvent).where(AuditEvent.case_id == str(case_id), AuditEvent.event_type == "case_deleted")
    ).first()
    assert audit is not None

    # 3. Purge service with retention (e.g. 7 days)
    # Freshly deleted case should not be purged
    purged = purge_soft_deleted_cases(db, retention_days=7)
    assert purged == 0
    assert db.get(Case, case_id) is not None

    # Age the deleted_at date past retention
    case.deleted_at = datetime.now(timezone.utc) - timedelta(days=8)
    db.commit()

    # Now purge should permanently remove the case
    purged_count = purge_soft_deleted_cases(db, retention_days=7)
    assert purged_count == 1
    db.expunge_all()
    assert db.scalars(select(Case).where(Case.id == case_id)).first() is None

    # Verify case_purged audit event
    purge_audit = db.scalars(
        select(AuditEvent).where(AuditEvent.case_id == str(case_id), AuditEvent.event_type == "case_purged")
    ).first()
    assert purge_audit is not None
    assert purge_audit.payload.get("retention_days") == 7


def test_audit_events_immutability_trigger(safety_test_env):
    """Database triggers must block any UPDATE or DELETE operations on audit_events."""
    _, db, _, _ = safety_test_env

    audit = AuditEvent(
        case_id="case_audit_test",
        actor="pathologist_1",
        event_type="test_event",
        stage="triage",
        payload={"note": "original"}
    )
    db.add(audit)
    db.commit()
    db.refresh(audit)

    # Attempt UPDATE -> trigger raises error
    with pytest.raises(Exception) as exc_info:
        db.execute(text("UPDATE audit_events SET stage = 'tampered' WHERE id = :aid"), {"aid": str(audit.id)})
        db.commit()
    assert "append-only" in str(exc_info.value).lower()
    db.rollback()

    # Attempt DELETE -> trigger raises error
    with pytest.raises(Exception) as exc_info_del:
        db.execute(text("DELETE FROM audit_events WHERE id = :aid"), {"aid": str(audit.id)})
        db.commit()
    assert "append-only" in str(exc_info_del.value).lower()
    db.rollback()


def test_health_endpoint_no_information_leak(safety_test_env):
    """Health failure must return 503 without leaking raw DB connection strings, passwords or stack traces."""
    client, _, _, _ = safety_test_env

    with patch("app.main.engine.connect") as mock_conn:
        mock_conn.side_effect = RuntimeError("FATAL: connection failed postgresql://oncogemma:secret_pw@10.0.0.1/db")
        resp = client.get("/healthz")

    assert resp.status_code == 503
    data = resp.json()
    assert data["detail"] == "Database connection failed"
    assert "secret_pw" not in resp.text
    assert "10.0.0.1" not in resp.text
    assert "postgresql://" not in resp.text
    assert "RuntimeError" not in resp.text


def test_prompt_injection_render_prompt_str_rejection():
    """AC7: render_prompt must strictly reject raw str variables to prevent prompt injection."""
    class ValidSpecimenType(str, Enum):
        CORE_BIOPSY = "core_biopsy"

    template = "Analyze the specimen: {{specimen_type}} with magnification {{mag}}"

    # Reject raw str variable
    with pytest.raises(PromptVariableError) as exc_info:
        render_prompt(template, {"specimen_type": "malicious injection", "mag": 10})
    assert "disallowed type 'str'" in str(exc_info.value)

    # Allow validated Enum and numeric primitives
    rendered = render_prompt(template, {"specimen_type": ValidSpecimenType.CORE_BIOPSY, "mag": 10})
    assert "core_biopsy" in rendered
    assert "10" in rendered


def test_geometry_safety_bounds_and_overlap():
    """AC8: Polygon vertices must be 3-64, non-negative, valid simple polygon, non-overlapping hotspots."""
    # < 3 vertices
    with pytest.raises(HTTPException) as exc1:
        validate_polygon_geometry([[0.0, 0.0], [1.0, 1.0]])
    assert exc1.value.status_code == 422
    assert "at least 3 vertices" in exc1.value.detail

    # > 64 vertices
    coords_65 = [[float(i), float(i)] for i in range(65)]
    with pytest.raises(HTTPException) as exc2:
        validate_polygon_geometry(coords_65)
    assert exc2.value.status_code == 422
    assert "exceeds the maximum" in exc2.value.detail

    # Negative coordinates
    with pytest.raises(HTTPException) as exc3:
        validate_polygon_geometry([[-1.0, 0.0], [5.0, 0.0], [5.0, 5.0]])
    assert exc3.value.status_code == 422
    assert "negative" in exc3.value.detail

    # Slide bounds overflow
    with pytest.raises(HTTPException) as exc4:
        validate_polygon_geometry([[0.0, 0.0], [100.0, 0.0], [100.0, 100.0]], slide_bounds_um=(50.0, 50.0))
    assert exc4.value.status_code == 422
    assert "outside slide bounds" in exc4.value.detail

    # Self-intersecting bowtie polygon
    bowtie = [[0.0, 0.0], [10.0, 10.0], [0.0, 10.0], [10.0, 0.0]]
    with pytest.raises(HTTPException) as exc5:
        validate_polygon_geometry(bowtie)
    assert exc5.value.status_code == 422
    assert "self-intersecting" in exc5.value.detail

    # Non-overlapping hotspots: pass
    hs_disjoint = [
        {"id": "hs_1", "polygon_um": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]], "excluded": False},
        {"id": "hs_2", "polygon_um": [[20.0, 20.0], [30.0, 20.0], [30.0, 30.0], [20.0, 30.0]], "excluded": False},
    ]
    validate_hotspots_non_overlapping(hs_disjoint)

    # Overlapping hotspots: 422
    hs_overlap = [
        {"id": "hs_1", "polygon_um": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]], "excluded": False},
        {"id": "hs_2", "polygon_um": [[5.0, 5.0], [15.0, 5.0], [15.0, 15.0], [5.0, 15.0]], "excluded": False},
    ]
    with pytest.raises(HTTPException) as exc6:
        validate_hotspots_non_overlapping(hs_overlap)
    assert exc6.value.status_code == 422
    assert "Hotspots cannot overlap" in exc6.value.detail


@pytest.mark.strict_idempotency
def test_idempotency_key_enforcement_and_replay(safety_test_env):
    """AC9: Confirm endpoints require Idempotency-Key; replaying cached responses and detecting conflicts."""
    client, db, _, override_get_db = safety_test_env

    case_id = uuid.uuid4()
    case = Case(id=case_id, status="open", created_by="pathologist_test", specimen_type="core_biopsy")
    stage_exec = StageExecution(
        id=uuid.uuid4(),
        case_id=case_id,
        stage="triage",
        attempt=1,
        status="awaiting_review",
        output_ref=""
    )
    db.add_all([case, stage_exec])
    db.commit()

    # Mock download_blob_as_bytes to return 1 active hotspot
    mock_hs_data = {
        "hotspots": [
            {
                "id": "hs_01",
                "center_um": [300.0, 300.0],
                "polygon_um": [[0.0, 0.0], [600.0, 0.0], [600.0, 600.0], [0.0, 600.0], [0.0, 0.0]],
                "area_mm2": 0.196,
                "prob_mean": 0.9,
                "prob_max": 0.95
            }
        ],
        "hpf_diameter_um": 500.0, "frame_um": 600.0, "hpf_target": 10,
    }

    with patch("app.services.stages.download_blob_as_bytes", return_value=json.dumps(mock_hs_data).encode("utf-8")), \
         patch("app.core.cloud_tasks.dispatch_stage_task"):

        # 1. Missing Idempotency-Key header -> 400 Bad Request
        resp_missing = client.post(
            "/api/v1/stages/triage/confirm",
            json={"case_id": str(case_id), "no_invasive_tumor": False}
        )
        assert resp_missing.status_code == 400
        assert "Idempotency-Key" in resp_missing.text

        # 2. First call with Idempotency-Key -> 200 OK
        idem_key = f"key-{uuid.uuid4()}"
        resp1 = client.post(
            "/api/v1/stages/triage/confirm",
            json={"case_id": str(case_id), "no_invasive_tumor": False, "accept_fewer_hpfs": True},
            headers={"Idempotency-Key": idem_key}
        )
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "confirmed"

        # 3. Exact replay with same Idempotency-Key -> returns cached response with Idempotent-Replay header
        resp_replay = client.post(
            "/api/v1/stages/triage/confirm",
            json={"case_id": str(case_id), "no_invasive_tumor": False, "accept_fewer_hpfs": True},
            headers={"Idempotency-Key": idem_key}
        )
        assert resp_replay.status_code == 200
        assert resp_replay.headers.get("Idempotent-Replay") == "true"
        assert resp_replay.json()["status"] == "confirmed"

        # 4. Same Idempotency-Key with different payload -> 422 Unprocessable Entity
        resp_conflict = client.post(
            "/api/v1/stages/triage/confirm",
            json={"case_id": str(case_id), "no_invasive_tumor": True},
            headers={"Idempotency-Key": idem_key}
        )
        assert resp_conflict.status_code == 422
        assert "different payload" in resp_conflict.text


def test_rate_limits_enforced(safety_test_env):
    """RateLimitMiddleware blocks requests exceeding IP or user sliding window limits."""
    client, _, _, _ = safety_test_env

    # 1. Test IP rate limiting on /api/v1/auth/session (configured limit: 10/min)
    headers = {"X-Forwarded-For": "192.168.1.50"}

    for _ in range(10):
        # We don't care about auth failure status (e.g. 400/401), just that it passes the rate limiter
        resp = client.post("/api/v1/auth/session", json={}, headers=headers)
        assert resp.status_code != 429

    # 11th request must be blocked by rate limiter with 429
    resp_blocked = client.post("/api/v1/auth/session", json={}, headers=headers)
    assert resp_blocked.status_code == 429
    assert "Rate limit exceeded" in resp_blocked.text


def test_signed_upload_max_size_enforcement(safety_test_env):
    """Upload URL generation must enforce safety.signed_upload.max_bytes configuration."""
    client, db, _, _ = safety_test_env

    case_id = uuid.uuid4()
    case = Case(id=case_id, status="open", created_by="pathologist_test", specimen_type="core_biopsy")
    db.add(case)
    db.commit()

    max_bytes = get_pipeline_config().safety.signed_upload.max_bytes

    # Attempt to request upload url for file exceeding max_bytes
    resp = client.post(
        f"/api/v1/cases/{case_id}/slide/upload-url",
        json={"filename": "large_slide.svs", "size_bytes": max_bytes + 1}
    )
    assert resp.status_code == 400
    assert "exceeds maximum allowed size" in resp.text
