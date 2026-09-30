"""Tests for the Research API (SPEC-08 §3–7; docs/contracts/research_v1.md; WP-9.1).

Validates:
- Role-based access control (viewer 403, pathologist 403 on labels-qa, researcher/admin full)
- Runs dashboard, filtering, detail, headline metrics, gate integrity
- Items listing and hierarchical decision tree reconstruction
- Mitosis error cards and what-if split gate (400 not_val_split on test splits)
- Run comparisons and manifest mismatch rejection (400 manifest_mismatch)
- Issue register, evidence tracking, and resolution gate (422 resolution_requires_run)
- Pathologist annotation queue and payload schema validation (422 invalid_payload)
- Label QA workflow and required reason enforcement (422 reason_required)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_db
from app.main import app
from app.models.case import Case
from app.models.decision_record import DecisionRecord
from app.models.research import (
    AnnotationTaskModel,
    GTAnnotation,
    Issue,
    QAItemModel,
    RunMetric,
)
from app.models.stage_execution import StageExecution
from app.models.user import User
from app.models.validation import ValidationItem, ValidationRun
from app.routers.research import _to_uuid

VIEWER = {"X-Test-Role": "viewer", "X-Test-User-Id": "viewer-1"}
PATHOLOGIST = {"X-Test-Role": "pathologist", "X-Test-User-Id": "path-1"}
RESEARCHER = {"X-Test-Role": "researcher", "X-Test-User-Id": "researcher-1"}
ADMIN = {"X-Test-Role": "admin", "X-Test-User-Id": "admin-1"}


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()

    # Seed test users for foreign key references
    for uid_str, role in [
        ("viewer-1", "viewer"),
        ("path-1", "pathologist"),
        ("researcher-1", "researcher"),
        ("admin-1", "admin"),
    ]:
        u_uuid = _to_uuid(uid_str)
        user = User(
            id=u_uuid,
            email=f"{uid_str}@example.org",
            role=role,
            status="active",
        )
        session.add(user)
    session.commit()

    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture
def client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def sample_runs(db_session: Session):
    run_val = ValidationRun(
        id=uuid.uuid4(),
        name="TCGA-BRCA Validated Arm P2",
        dataset="tcga_brca_dx",
        split="val",
        arm="P2:ordinal_morph",
        stages=["s3_triage", "s4_mitosis", "s5_grading"],
        mode="auto",
        concurrency=2,
        status="completed",
        is_locked_test=False,
        manifest_uri="gs://og-test/manifest.csv",
        manifest_sha256="manifest_hash_123",
        config_hash="config_hash_123",
        registry_sha256="registry_hash_123",
        splits_lock_sha256="lock_hash_123",
        created_by="eval_runner",
        created_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    run_test = ValidationRun(
        id=uuid.uuid4(),
        name="TCGA-BRCA Test Arm P2",
        dataset="tcga_brca_dx",
        split="test",
        arm="P2:ordinal_morph",
        stages=["s3_triage", "s4_mitosis", "s5_grading"],
        mode="auto",
        concurrency=2,
        status="completed",
        is_locked_test=True,
        manifest_uri="gs://og-test/manifest.csv",
        manifest_sha256="manifest_hash_123",
        config_hash="config_hash_123",
        registry_sha256="registry_hash_123",
        splits_lock_sha256="lock_hash_123",
        created_by="eval_runner",
        created_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    run_diff_manifest = ValidationRun(
        id=uuid.uuid4(),
        name="Other Manifest Arm",
        dataset="tcga_brca_dx",
        split="val",
        arm="P1:vlm_prompt",
        stages=["s3_triage", "s5_grading"],
        mode="auto",
        concurrency=1,
        status="completed",
        is_locked_test=False,
        manifest_uri="gs://og-test/other_manifest.csv",
        manifest_sha256="other_manifest_hash_456",
        config_hash="config_hash_123",
        registry_sha256="registry_hash_123",
        splits_lock_sha256="lock_hash_123",
        created_by="eval_runner",
        created_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add_all([run_val, run_test, run_diff_manifest])
    db_session.flush()

    # Add headline metrics for run_val
    m_ns_g = RunMetric(
        run_id=run_val.id,
        metric_id="ns_g",
        slice_key=None,
        value=0.58,
        ci_low=0.49,
        ci_high=0.67,
        n=104,
        status="final",
    )
    m_ns_m = RunMetric(
        run_id=run_val.id,
        metric_id="ns_m",
        slice_key=None,
        value=0.71,
        ci_low=0.66,
        ci_high=0.76,
        n=30,
        status="final",
    )
    db_session.add_all([m_ns_g, m_ns_m])
    db_session.flush()

    # Add items to run_val
    case1 = Case(
        id=uuid.uuid4(),
        created_by="eval_runner",
        status="done",
        specimen_type="resection",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(case1)

    item1 = ValidationItem(
        run_id=run_val.id,
        slide_id="slide-01",
        patient_id="TCGA-01",
        status="succeeded",
        case_id=case1.id,
        prediction={"grading": {"grade": 2, "total": 6, "tubule": 2, "pleo": 2, "mitoses": 2}},
        runtime_s=45.2,
        cost_usd=0.04,
    )
    db_session.add(item1)
    db_session.flush()

    # Add stage execution and decision records attached to case1
    stage_exec = StageExecution(
        id=uuid.uuid4(),
        case_id=case1.id,
        stage="grading",
        status="done",
        run_mode="eval",
        run_id=run_val.id,
    )
    db_session.add(stage_exec)
    db_session.flush()

    dr_root = DecisionRecord(
        id=uuid.uuid4(),
        case_id=case1.id,
        stage_execution_id=stage_exec.id,
        run_id=run_val.id,
        stage="grading",
        task="nottingham_grade",
        entity_type="slide",
        entity_id="slide-01",
        producer_kind="model",
        producer_id="grading_ensemble",
        producer_version="v2.1",
        status="ok",
        input_sha256="sha_grading_input",
        input_spec={"tubule": 2, "pleo": 2, "mitoses": 2},
        output={"grade": 2, "total": 6},
        latency_ms=120,
        run_mode="eval",
        config_hash="cfg_hash_1",
        supersedes_id=None,
    )
    db_session.add(dr_root)
    db_session.flush()

    dr_child = DecisionRecord(
        id=uuid.uuid4(),
        case_id=case1.id,
        stage_execution_id=stage_exec.id,
        run_id=run_val.id,
        stage="mitosis",
        task="mitosis_counting",
        entity_type="hotspot",
        entity_id="hotspot-01",
        producer_kind="model",
        producer_id="kongnet_mitosis",
        producer_version="v1.4",
        status="ok",
        input_sha256="sha_mitosis_input",
        input_spec={"hpf_area_mm2": 0.238},
        output={"count": 8, "score": 2},
        latency_ms=850,
        run_mode="eval",
        config_hash="cfg_hash_1",
        supersedes_id=dr_root.id,
    )
    db_session.add(dr_child)
    db_session.flush()
    db_session.commit()

    return {
        "val": run_val,
        "test": run_test,
        "other": run_diff_manifest,
        "item": item1,
        "dr_root": dr_root,
        "dr_child": dr_child,
    }


# =============================================================================
# 1. RBAC Tests (AC1)
# =============================================================================


def test_viewer_is_forbidden_on_all_research_endpoints(client: TestClient, sample_runs):
    run_id = str(sample_runs["val"].id)
    assert client.get("/api/v1/research/runs", headers=VIEWER).status_code == 403
    assert client.get(f"/api/v1/research/runs/{run_id}", headers=VIEWER).status_code == 403
    assert client.get(f"/api/v1/research/runs/{run_id}/items", headers=VIEWER).status_code == 403
    assert client.get("/api/v1/research/issues", headers=VIEWER).status_code == 403
    assert client.get("/api/v1/research/annotation-tasks", headers=VIEWER).status_code == 403
    assert client.get("/api/v1/research/labels-qa", headers=VIEWER).status_code == 403


def test_pathologist_can_access_runs_and_annotate_but_not_label_qa(client: TestClient, sample_runs):
    run_id = str(sample_runs["val"].id)
    # Allowed
    assert client.get("/api/v1/research/runs", headers=PATHOLOGIST).status_code == 200
    assert client.get(f"/api/v1/research/runs/{run_id}", headers=PATHOLOGIST).status_code == 200
    assert client.get("/api/v1/research/issues", headers=PATHOLOGIST).status_code == 200
    assert client.get("/api/v1/research/annotation-tasks", headers=PATHOLOGIST).status_code == 200
    # Forbidden on label QA (pathologist lacks labels:qa per configs/auth.yaml)
    assert client.get("/api/v1/research/labels-qa", headers=PATHOLOGIST).status_code == 403


def test_researcher_has_full_access(client: TestClient, sample_runs):
    run_id = str(sample_runs["val"].id)
    assert client.get("/api/v1/research/runs", headers=RESEARCHER).status_code == 200
    assert client.get(f"/api/v1/research/runs/{run_id}", headers=RESEARCHER).status_code == 200
    assert client.get("/api/v1/research/issues", headers=RESEARCHER).status_code == 200
    assert client.get("/api/v1/research/annotation-tasks", headers=RESEARCHER).status_code == 200
    assert client.get("/api/v1/research/labels-qa", headers=RESEARCHER).status_code == 200


# =============================================================================
# 2. Runs Dashboard & Detail Tests (AC2)
# =============================================================================


def test_list_runs_with_filtering_and_pagination(client: TestClient, sample_runs):
    # Filter by split
    res = client.get("/api/v1/research/runs?split=val", headers=RESEARCHER)
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert all(item["split"] == "val" for item in data["items"])
    assert len(data["items"]) == 2

    # Limit and pagination
    res_page = client.get("/api/v1/research/runs?limit=1", headers=RESEARCHER)
    assert res_page.status_code == 200
    page_data = res_page.json()
    assert len(page_data["items"]) == 1
    assert page_data["next_cursor"] is not None


def test_get_run_detail(client: TestClient, sample_runs):
    run_id = str(sample_runs["val"].id)
    res = client.get(f"/api/v1/research/runs/{run_id}", headers=RESEARCHER)
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == run_id
    assert data["name"] == "TCGA-BRCA Validated Arm P2"
    assert data["headline"]["ns_g"]["value"] == 0.58
    assert data["headline"]["ns_m"]["value"] == 0.71
    assert data["gate"]["valid"] is True
    assert data["gate"]["int_fall"] == 0
    assert "commercial_ok" in data["license_scopes"]


def test_get_run_detail_not_found(client: TestClient):
    fake_id = str(uuid.uuid4())
    res = client.get(f"/api/v1/research/runs/{fake_id}", headers=RESEARCHER)
    assert res.status_code == 404
    assert res.json()["detail"] == "run_not_found"


# =============================================================================
# 3. Items & Decision Tree Tests (AC3)
# =============================================================================


def test_list_run_items_and_detail_tree(client: TestClient, sample_runs):
    run_id = str(sample_runs["val"].id)
    res = client.get(f"/api/v1/research/runs/{run_id}/items", headers=RESEARCHER)
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["slide_id"] == "slide-01"
    assert item["pred"]["grade"] == 2
    assert item["pred"]["total"] == 6

    # Get single item detail with reconstructed tree
    res_item = client.get(f"/api/v1/research/runs/{run_id}/items/slide-01", headers=RESEARCHER)
    assert res_item.status_code == 200
    tree_data = res_item.json()
    assert tree_data["item"]["slide_id"] == "slide-01"
    decisions = tree_data["decisions"]
    assert len(decisions) == 1
    root = decisions[0]
    assert root["task"] == "nottingham_grade"
    assert root["entity_type"] == "slide"
    assert len(root["children"]) == 1
    child = root["children"][0]
    assert child["task"] == "mitosis_counting"
    assert child["entity_type"] == "hotspot"


# =============================================================================
# 4. Mitosis Error Cards & What-If Curves (AC6)
# =============================================================================


def test_mitosis_curves_and_what_if_val_split(client: TestClient, sample_runs):
    val_run_id = str(sample_runs["val"].id)
    # Standard curves without what-if
    res = client.get(f"/api/v1/research/runs/{val_run_id}/curves/mitosis", headers=RESEARCHER)
    assert res.status_code == 200
    data = res.json()
    assert "pr" in data
    assert len(data["pr"]["thresholds"]) > 0
    assert data["what_if"] is None

    # What-if on validation split succeeds
    res_what_if = client.get(
        f"/api/v1/research/runs/{val_run_id}/curves/mitosis?tau_a=0.55&tau_b=0.65",
        headers=RESEARCHER,
    )
    assert res_what_if.status_code == 200
    what_if_data = res_what_if.json()
    assert what_if_data["what_if"] is not None
    assert "f1" in what_if_data["what_if"]
    assert "precision" in what_if_data["what_if"]
    assert "recall" in what_if_data["what_if"]


def test_mitosis_curves_what_if_strictly_forbidden_on_test_split(client: TestClient, sample_runs):
    test_run_id = str(sample_runs["test"].id)
    # What-if on test split must raise 400 not_val_split
    res = client.get(
        f"/api/v1/research/runs/{test_run_id}/curves/mitosis?tau_a=0.50",
        headers=RESEARCHER,
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "not_val_split"


# =============================================================================
# 5. Run Comparison & Manifest Mismatch (AC4)
# =============================================================================


def test_compare_runs_manifest_mismatch_returns_400(client: TestClient, sample_runs):
    run_val_id = str(sample_runs["val"].id)
    run_other_id = str(sample_runs["other"].id)

    # These two runs have different manifest_sha256
    res = client.get(f"/api/v1/research/compare?a={run_val_id}&b={run_other_id}", headers=RESEARCHER)
    assert res.status_code == 400
    assert res.json()["detail"] == "manifest_mismatch"


def test_compare_runs_same_manifest_succeeds(client: TestClient, sample_runs):
    run_val_id = str(sample_runs["val"].id)
    run_test_id = str(sample_runs["test"].id)

    res = client.get(f"/api/v1/research/compare?a={run_val_id}&b={run_test_id}", headers=RESEARCHER)
    assert res.status_code == 200
    data = res.json()
    assert data["manifest_sha256"] == "manifest_hash_123"
    assert len(data["metrics"]) > 0


# =============================================================================
# 6. Issue Register & Resolution Gate (AC5)
# =============================================================================


def test_issue_lifecycle_and_resolution_gate(client: TestClient, sample_runs):
    run_val_id = str(sample_runs["val"].id)

    # 1. Create issue
    payload = {
        "title": "Low tubule formation precision on pleomorphic cases",
        "category": "model",
        "severity": "high",
        "metric_impact": {"metric": "ns_g", "est_delta": -0.07},
        "evidence": [{"run_id": run_val_id, "slide_id": "slide-01"}],
        "spec_ref": "SPEC-00 §4.2",
    }
    res_create = client.post("/api/v1/research/issues", json=payload, headers=RESEARCHER)
    assert res_create.status_code == 201
    issue = res_create.json()
    issue_id = issue["id"]
    assert issue["status"] == "open"

    # 2. Patching status to resolved WITHOUT resolved_in must return 422 resolution_requires_run
    res_fail1 = client.patch(
        f"/api/v1/research/issues/{issue_id}",
        json={"status": "resolved"},
        headers=RESEARCHER,
    )
    assert res_fail1.status_code == 422
    assert res_fail1.json()["detail"] == "resolution_requires_run"

    # 3. Patching status to resolved with nonexistent run must return 422 resolution_requires_run
    res_fail2 = client.patch(
        f"/api/v1/research/issues/{issue_id}",
        json={"status": "resolved", "resolved_in": "nonexistent_run_xyz"},
        headers=RESEARCHER,
    )
    assert res_fail2.status_code == 422
    assert res_fail2.json()["detail"] == "resolution_requires_run"

    # 4. Patching status to resolved with valid validation run succeeds
    res_ok = client.patch(
        f"/api/v1/research/issues/{issue_id}",
        json={
            "status": "resolved",
            "resolved_in": "TCGA-BRCA Validated Arm P2",
            "resolution_note": "Resolved by hyperparameter tuning on ordinal loss.",
        },
        headers=RESEARCHER,
    )
    assert res_ok.status_code == 200
    updated = res_ok.json()
    assert updated["status"] == "resolved"
    assert updated["resolved_in"] == "TCGA-BRCA Validated Arm P2"
    assert updated["resolution_note"] == "Resolved by hyperparameter tuning on ordinal loss."


# =============================================================================
# 7. Pathologist Annotation Tasks (AC7)
# =============================================================================


def test_annotation_task_workflow(client: TestClient, db_session: Session):
    task = AnnotationTaskModel(
        id="task_mitosis_01",
        dataset="tcga_brca_dx",
        slide_id="slide-01",
        kind="mitosis_points",
        regions_um=[[100.0, 100.0, 200.0, 200.0]],
        blind=False,
        definition_md="Mark all mitotic figures and imposters",
        protocol_version="v6.0",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(task)
    db_session.commit()

    # List tasks
    res_list = client.get("/api/v1/research/annotation-tasks", headers=PATHOLOGIST)
    assert res_list.status_code == 200
    data = res_list.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == "task_mitosis_01"

    # Submit invalid payload (points missing or wrong structure) -> 422 invalid_payload
    bad_payload = {
        "task_id": "task_mitosis_01",
        "payload": {"invalid_key": "data"},
        "status": "submitted",
    }
    res_bad = client.post("/api/v1/research/annotations", json=bad_payload, headers=PATHOLOGIST)
    assert res_bad.status_code == 422
    assert res_bad.json()["detail"] == "invalid_payload"

    # Submit valid payload -> 201
    good_payload = {
        "task_id": "task_mitosis_01",
        "payload": {
            "points": [
                {"x_um": 120.5, "y_um": 150.2, "class": "MF"},
                {"x_um": 180.0, "y_um": 110.0, "class": "imposter"},
            ]
        },
        "status": "submitted",
    }
    res_good = client.post("/api/v1/research/annotations", json=good_payload, headers=PATHOLOGIST)
    assert res_good.status_code == 201
    assert res_good.json()["status"] == "submitted"


# =============================================================================
# 8. Label QA Queue & Reason Enforcement (AC7)
# =============================================================================


def test_label_qa_workflow_and_reason_enforcement(client: TestClient, db_session: Session):
    qa_item = QAItemModel(
        patient_id="TCGA-BRCA-001",
        report_text_url="https://storage.googleapis.com/reports/001.txt",
        regex_data={"grade": 2, "tubule": 2, "pleo": 2, "mitoses": 2},
        llm_data={"grade": 3, "tubule": 3, "pleo": 3, "mitoses": 2},
        status="pending",
    )
    db_session.add(qa_item)
    db_session.commit()

    # Pathologist gets 403
    assert client.get("/api/v1/research/labels-qa", headers=PATHOLOGIST).status_code == 403

    # Researcher gets 200
    res_list = client.get("/api/v1/research/labels-qa", headers=RESEARCHER)
    assert res_list.status_code == 200
    assert len(res_list.json()) == 1

    # Edit without reason returns 422 reason_required
    res_edit_no_reason = client.post(
        "/api/v1/research/labels-qa/TCGA-BRCA-001",
        json={"action": "edit", "values": {"grade": 3}},
        headers=RESEARCHER,
    )
    assert res_edit_no_reason.status_code == 422
    assert res_edit_no_reason.json()["detail"] == "reason_required"

    # Exclude without reason returns 422 reason_required
    res_ex_no_reason = client.post(
        "/api/v1/research/labels-qa/TCGA-BRCA-001",
        json={"action": "exclude"},
        headers=RESEARCHER,
    )
    assert res_ex_no_reason.status_code == 422
    assert res_ex_no_reason.json()["detail"] == "reason_required"

    # Accept without reason succeeds
    res_accept = client.post(
        "/api/v1/research/labels-qa/TCGA-BRCA-001",
        json={"action": "accept"},
        headers=RESEARCHER,
    )
    assert res_accept.status_code == 200
    assert res_accept.json()["status"] == "accepted"

    # Edit with reason succeeds
    res_edit_ok = client.post(
        "/api/v1/research/labels-qa/TCGA-BRCA-001",
        json={
            "action": "edit",
            "values": {"grade": 3},
            "reason": "Synoptic report explicitly states Grade 3",
        },
        headers=RESEARCHER,
    )
    assert res_edit_ok.status_code == 200
    assert res_edit_ok.json()["status"] == "edited"
