"""Integration and contract tests for Stage 3 Triage API v6 (WP-6.3, SPEC-05 §5).

Acceptance Criteria:
- AC4: Overlapping edit operations rejected with 422 and collision pairs.
- Confirmation overlap rejection with 409.
- DecisionRecord creation for human edits with supersedes_id.
- TriageStageV6 contract compliance.
"""
import json
import uuid
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models
from app.main import app
from app.core.db import Base, get_db
from app.models.case import Case
from app.models.stage_execution import StageExecution
from app.models.hotspot import Hotspot
from app.models.decision_record import DecisionRecord
from app.core.tasks import Task, ProducerKind

CONTRACT_KEYS = {
    "case_id", "stage_execution_id", "status", "slide", "heatmap", "tumor_threshold",
    "hotspots", "machine_hotspots", "flags", "provenance",
}


@pytest.fixture
def client_and_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    db = TestingSessionLocal()
    yield client, db
    db.close()
    app.dependency_overrides.clear()


def test_triage_edits_overlap_rejection_ac4(client_and_db):
    """AC4: Overlapping candidate windows are rejected with HTTP 422 and colliding IDs."""
    client, db = client_and_db
    case_id = str(uuid.uuid4())
    exec_id = uuid.uuid4()

    c = Case(id=case_id, created_by="pathologist_1", status="processing")
    se = StageExecution(
        id=exec_id,
        case_id=case_id,
        stage="triage",
        attempt=1,
        status="awaiting_review",
        input_ref={},
        output_ref=f"gs://og-artifacts-local/cases/{case_id}/triage/output.json",
    )
    db.add(c)
    db.add(se)
    db.commit()

    # Model produced one hotspot at (1000, 1000) to (1600, 1600)
    mock_output = {
        "heatmap_png_uri": "/artifacts/heatmap.png",
        "prob_grid_uri": "/artifacts/probs.npy",
        "grid": {"origin_um": [0, 0], "stride_um": [224.0, 224.0], "nx": 20, "ny": 20},
        "hotspots": [
            {
                "id": "hs_01",
                "polygon_um": [[1000, 1000], [1600, 1000], [1600, 1600], [1000, 1600], [1000, 1000]],
                "area_mm2": 0.36,
                "prob_mean": 0.92,
                "prob_max": 0.98,
                "source": "model",
                "excluded": False,
            }
        ],
    }
    mock_bytes = json.dumps(mock_output).encode("utf-8")

    with patch("app.routers.triage.download_blob_as_bytes", return_value=mock_bytes), \
         patch("app.services.stages.download_blob_as_bytes", return_value=mock_bytes):

        # Pathologist tries to add a user ROI overlapping hs_01 at (1500, 1500) to (2100, 2100)
        overlapping_edits = [
            {
                "op": "add",
                "id": "user_overlap_01",
                "polygon_um": [[1500, 1500], [2100, 1500], [2100, 2100], [1500, 2100], [1500, 1500]],
                "area_mm2": 0.36,
            }
        ]

        res = client.post(
            "/api/v1/stages/triage/edits",
            json={"case_id": case_id, "edits": overlapping_edits},
        )

        assert res.status_code == 422
        body = res.json()
        assert body.get("error") == "hotspot_overlap"
        assert ["hs_01", "user_overlap_01"] in body.get("ids", []) or [
            "user_overlap_01",
            "hs_01",
        ] in body.get("ids", [])


def test_triage_confirm_overlap_rejection(client_and_db):
    """Overlapping candidates during confirmation return HTTP 409."""
    client, db = client_and_db
    case_id = str(uuid.uuid4())
    exec_id = uuid.uuid4()

    c = Case(id=case_id, created_by="pathologist_1", status="processing")
    se = StageExecution(
        id=exec_id,
        case_id=case_id,
        stage="triage",
        attempt=1,
        status="awaiting_review",
        input_ref={},
        output_ref=f"gs://og-artifacts-local/cases/{case_id}/triage/output.json",
        # Stored review edits have overlapping hotspots
        review_edits=[
            {
                "op": "add",
                "id": "user_overlap_01",
                "polygon_um": [[1000, 1000], [1600, 1000], [1600, 1600], [1000, 1600], [1000, 1000]],
                "area_mm2": 0.36,
            },
            {
                "op": "add",
                "id": "user_overlap_02",
                "polygon_um": [[1200, 1200], [1800, 1200], [1800, 1800], [1200, 1800], [1200, 1200]],
                "area_mm2": 0.36,
            },
        ],
    )
    db.add(c)
    db.add(se)
    db.commit()

    mock_output = {"hotspots": []}
    mock_bytes = json.dumps(mock_output).encode("utf-8")

    with patch("app.routers.triage.download_blob_as_bytes", return_value=mock_bytes), \
         patch("app.services.stages.download_blob_as_bytes", return_value=mock_bytes):

        res = client.post(
            "/api/v1/stages/triage/confirm",
            json={"case_id": case_id, "no_invasive_tumor": False},
        )

        assert res.status_code == 409
        body = res.json()
        assert body.get("error") == "hotspot_overlap"
        assert len(body.get("ids", [])) >= 1


def test_triage_valid_edits_decision_record_and_contract(client_and_db):
    """Valid non-overlapping edits record a DecisionRecord and return TriageStageV6 contract."""
    client, db = client_and_db
    case_uuid = uuid.uuid4()
    case_id = str(case_uuid)
    exec_id = uuid.uuid4()
    model_dr_id = uuid.uuid4()

    c = Case(id=case_uuid, created_by="pathologist_1", status="processing", specimen_type="resection")
    se = StageExecution(
        id=exec_id,
        case_id=case_uuid,
        stage="triage",
        attempt=1,
        status="awaiting_review",
        input_ref={},
        output_ref=f"gs://og-artifacts-local/cases/{case_id}/triage/output.json",
    )
    # Model's initial DecisionRecord
    model_dr = DecisionRecord(
        id=model_dr_id,
        case_id=case_uuid,
        stage_execution_id=exec_id,
        stage="triage",
        task=Task.HOTSPOT_SELECT.value,
        entity_type="hotspot",
        entity_id=str(exec_id),
        producer_kind="model",
        producer_id="hotspot_selector_v6",
        producer_version="1.0",
        input_sha256="0" * 64,
        input_spec={"dummy": True},
        status="ok",
        latency_ms=100,
        run_mode="eval",
        config_hash="0" * 64,
    )
    db.add(c)
    db.add(se)
    db.commit()

    db.add(model_dr)
    db.commit()

    mock_output = {
        "heatmap_png_uri": "/artifacts/heatmap.png",
        "prob_grid_uri": "/artifacts/probs.npy",
        "grid": {"origin_um": [0, 0], "stride_um": [224.0, 224.0], "nx": 30, "ny": 30},
        "flags": ["hotspots_limited_by_tissue"],
        "heatmap": {"tile_um": 224.0, "origin_um": [0.0, 0.0], "nx": 30, "ny": 30,
                    "value": "p_tumor_cal", "head_version": "1.0.0"},
        "tumor_threshold": 0.5,
        "hotspots": [
            {
                "id": "hs_01",
                "polygon_um": [[1000, 1000], [1600, 1000], [1600, 1600], [1000, 1600], [1000, 1000]],
                "area_mm2": 0.36,
                "prob_mean": 0.90,
                "prob_max": 0.95,
                "source": "model",
                "excluded": False,
            }
        ],
    }
    mock_bytes = json.dumps(mock_output).encode("utf-8")

    with patch("app.routers.triage.download_blob_as_bytes", return_value=mock_bytes), \
         patch("app.services.stages.download_blob_as_bytes", return_value=mock_bytes):

        # Add a separated user ROI at (3000, 3000) to (3600, 3600)
        valid_edits = [
            {
                "op": "add",
                "id": "user_clean_01",
                "polygon_um": [[3000, 3000], [3600, 3000], [3600, 3600], [3000, 3600], [3000, 3000]],
                "area_mm2": 0.36,
            }
        ]

        res_edits = client.post(
            "/api/v1/stages/triage/edits",
            json={"case_id": case_id, "edits": valid_edits},
        )

        assert res_edits.status_code == 200
        data = res_edits.json()

        # Check TriageStageV6 contract fields (docs/contracts/triage_v6.md)
        assert CONTRACT_KEYS <= data.keys()
        assert data["status"] == "awaiting_review"
        assert len(data["hotspots"]) == 2
        assert len(data["machine_hotspots"]) == 1
        assert data["flags"] == ["hotspots_limited_by_tissue"]
        assert data["provenance"]["stage"] == "triage"
        assert data["heatmap"]["nx"] == 30 and data["heatmap"]["png_url"].endswith("/heatmap")
        assert data["tumor_threshold"] == 0.5
        assert data["edits_count"] == 1

        # Check DecisionRecord in DB
        human_drs = db.scalars(
            select(DecisionRecord).where(
                DecisionRecord.case_id == uuid.UUID(case_id),
                DecisionRecord.task == Task.HUMAN_EDIT.value,
            )
        ).all()
        assert len(human_drs) == 1
        hdr = human_drs[0]
        assert hdr.producer_kind == ProducerKind.HUMAN.value
        assert str(hdr.supersedes_id) == str(model_dr_id)

        # Now test GET /api/v1/stages/triage/{case_id}
        res_get = client.get(f"/api/v1/stages/triage/{case_id}")
        assert res_get.status_code == 200
        get_data = res_get.json()
        assert CONTRACT_KEYS <= get_data.keys()
        assert get_data["hotspots"] == data["hotspots"]
        assert get_data["flags"] == ["hotspots_limited_by_tissue"]

        # Now confirm triage and check DB Hotspot rows
        res_confirm = client.post(
            "/api/v1/stages/triage/confirm",
            json={"case_id": case_id, "no_invasive_tumor": False},
        )
        assert res_confirm.status_code == 200

        db_hotspots = db.scalars(
            select(Hotspot).where(Hotspot.case_id == uuid.UUID(case_id))
        ).all()
        assert len(db_hotspots) == 2
        for hs in db_hotspots:
            assert hs.polygon_um is not None
            assert hs.area_mm2 is not None
