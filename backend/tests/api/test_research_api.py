"""Tests for the Research API (SPEC-08 §3–7; docs/contracts/research_v1.md; WP-9.1).

Validates:
- Role-based access control (viewer 403, pathologist 403 on labels-qa, researcher/admin full)
- Runs dashboard, filtering, detail, headline metrics, and a measurement-validity gate that is
  computed from the manifest and the decision records (never assumed)
- Items listing with manifest ground truth, and hierarchical decision tree reconstruction
- Endpoints whose data no run stores yet answer with an explicit 404, never with invented values
- Run comparisons (manifest mismatch 400, not comparable 400, slices and flips from the runs)
- Issue register, evidence tracking, and the resolution rule (422 resolution_requires_run)
- Pathologist annotation queue and payload schema validation (422 invalid_payload)
- Label QA workflow and required reason enforcement (422 reason_required)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pandas as pd
import pytest
from app.core.db import Base, get_db
from app.main import app
from app.models.case import Case
from app.models.decision_record import DecisionRecord
from app.models.research import (
    AnnotationTaskModel,
    GTAnnotation,
    QAItemModel,
    RunMetric,
)
from app.models.stage_execution import StageExecution
from app.models.user import User
from app.models.validation import ValidationItem, ValidationRun
from eval.harness.runs import sha256_file
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

USER_IDS = {
    "viewer": str(uuid.uuid4()),
    "pathologist": str(uuid.uuid4()),
    "researcher": str(uuid.uuid4()),
    "admin": str(uuid.uuid4()),
}


def _headers(role: str) -> dict[str, str]:
    return {"X-Test-Role": role, "X-Test-User-Id": USER_IDS[role]}


VIEWER = _headers("viewer")
PATHOLOGIST = _headers("pathologist")
RESEARCHER = _headers("researcher")
ADMIN = _headers("admin")

# slide_id, patient_id, split, gt_grade, gt_total, gt_tubule, gt_pleo, gt_mitoses, scanner
MANIFEST_ROWS = [
    ("slide-01", "TCGA-01", "val", 1, 4, 1, 1, 2, "Aperio"),
    ("slide-02", "TCGA-02", "val", 2, 6, 2, 2, 2, "Aperio"),
    ("slide-03", "TCGA-03", "val", 2, 7, 2, 3, 2, "Hamamatsu"),
    ("slide-04", "TCGA-04", "val", 3, 8, 3, 3, 2, "Hamamatsu"),
    ("slide-05", "TCGA-05", "test", 2, 6, 2, 2, 2, "Aperio"),
    ("slide-06", "TCGA-06", "train", 1, 5, 2, 2, 1, "Aperio"),
]
# what each arm predicted for the four val slides: grade, total, tubule, pleo, mitoses
PRED_BEFORE = {
    "slide-01": (1, 4, 1, 1, 2),
    "slide-02": (2, 6, 2, 2, 2),
    "slide-03": (3, 8, 2, 3, 2),  # wrong grade
    "slide-04": (3, 8, 3, 3, 2),
}
PRED_AFTER = {**PRED_BEFORE, "slide-03": (2, 7, 2, 3, 2)}  # fixes slide-03


def write_manifest(path, rows=MANIFEST_ROWS) -> tuple[str, str]:
    frame = pd.DataFrame(
        rows,
        columns=["slide_id", "patient_id", "split", "gt_grade", "gt_total", "gt_tubule", "gt_pleo",
                 "gt_mitoses", "scanner"],
    )
    frame["dataset"] = "tcga_brca_dx"
    frame["native_mag"] = 40
    frame.to_parquet(path)
    return str(path), sha256_file(path)


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
    for role, uid in USER_IDS.items():
        session.add(User(id=uuid.UUID(uid), email=f"{role}@example.org", role=role, status="active"))
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


def make_run(db: Session, name: str, manifest: tuple[str, str], *, split: str = "val", **kw) -> ValidationRun:
    uri, sha = manifest
    fields = dict(
        id=uuid.uuid4(),
        name=name,
        dataset="tcga_brca_dx",
        split=split,
        arm=None,
        stages=["triage", "mitosis", "grading"],
        mode="auto",
        concurrency=2,
        status="completed",
        is_locked_test=split == "test",
        manifest_uri=uri,
        manifest_sha256=sha,
        config_hash="c0" * 32,
        registry_sha256="e0" * 32,
        splits_lock_sha256="a0" * 32,
        created_by="eval_runner",
        created_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    fields.update(kw)
    run = ValidationRun(**fields)
    db.add(run)
    db.flush()
    return run


def add_item(db: Session, run: ValidationRun, slide_id: str, patient_id: str, pred, *, with_decision: bool = True,
             producer_kind: str = "model"):
    """A succeeded item with its case and (unless ``with_decision`` is False) a two-node decision tree."""
    case = Case(id=uuid.uuid4(), created_by="eval_runner", status="done", specimen_type="resection",
                created_at=datetime.now(timezone.utc))
    db.add(case)
    grade, total, tubule, pleo, mitoses = pred
    item = ValidationItem(
        run_id=run.id, slide_id=slide_id, patient_id=patient_id, status="succeeded", case_id=case.id,
        prediction={"grading": {"grade": grade, "total": total, "tubule": tubule, "pleo": pleo, "mitoses": mitoses}},
        runtime_s=45.2, cost_usd=0.04,
    )
    db.add(item)
    db.flush()
    if not with_decision:
        return item, None, None
    execution = StageExecution(id=uuid.uuid4(), case_id=case.id, stage="grading", status="done", run_mode="eval",
                               run_id=run.id)
    db.add(execution)
    db.flush()
    root = DecisionRecord(
        id=uuid.uuid4(), case_id=case.id, stage_execution_id=execution.id, run_id=run.id, stage="grading",
        task="nottingham_grade", entity_type="slide", entity_id=slide_id, producer_kind=producer_kind,
        producer_id="grading_ensemble", producer_version="v2.1", status="ok", input_sha256="sha_grading_input",
        input_spec={"tubule": 2, "pleo": 2, "mitoses": 2}, output={"grade": grade, "total": total},
        latency_ms=120, run_mode="eval", config_hash="cfg_hash_1", supersedes_id=None,
    )
    db.add(root)
    db.flush()
    child = DecisionRecord(
        id=uuid.uuid4(), case_id=case.id, stage_execution_id=execution.id, run_id=run.id, stage="mitosis",
        task="mitosis_counting", entity_type="hotspot", entity_id="hotspot-01", producer_kind=producer_kind,
        producer_id="kongnet_mitosis", producer_version="v1.4", status="ok", input_sha256="sha_mitosis_input",
        input_spec={"hpf_area_mm2": 0.238}, output={"count": 8, "score": 2}, latency_ms=850, run_mode="eval",
        config_hash="cfg_hash_1", supersedes_id=root.id,
    )
    db.add(child)
    db.flush()
    return item, root, child


def add_arm(db: Session, run: ValidationRun, preds: dict, **kw):
    made = []
    for slide_id, pred in preds.items():
        patient = next(r[1] for r in MANIFEST_ROWS if r[0] == slide_id)
        made.append(add_item(db, run, slide_id, patient, pred, **kw))
    return made


@pytest.fixture
def manifest(tmp_path):
    return write_manifest(tmp_path / "manifest.parquet")


@pytest.fixture
def sample_runs(db_session: Session, manifest, tmp_path):
    run_val = make_run(db_session, "TCGA-BRCA Validated Arm P2", manifest, arm="P2:ordinal_morph")
    run_after = make_run(db_session, "TCGA-BRCA Validated Arm P3", manifest, arm="P3:vlm_prompt")
    run_test = make_run(db_session, "TCGA-BRCA Test Arm P2", manifest, split="test", arm="P2:ordinal_morph")
    other_manifest = write_manifest(tmp_path / "other.parquet", MANIFEST_ROWS[:5])
    run_other = make_run(db_session, "Other Manifest Arm", other_manifest, arm="P1:vlm_prompt")

    db_session.add_all([
        RunMetric(run_id=run_val.id, metric_id="ns_g", value=0.58, ci_low=0.49, ci_high=0.67, n=104, status="final"),
        RunMetric(run_id=run_val.id, metric_id="ns_m", value=0.71, ci_low=0.66, ci_high=0.76, n=30, status="final"),
    ])
    item1, dr_root, dr_child = add_arm(db_session, run_val, PRED_BEFORE)[0]
    add_arm(db_session, run_after, PRED_AFTER)
    db_session.commit()
    return {
        "val": run_val, "after": run_after, "test": run_test, "other": run_other,
        "item": item1, "dr_root": dr_root, "dr_child": dr_child,
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
    assert len(data["items"]) == 3

    # Limit and pagination
    res_page = client.get("/api/v1/research/runs?limit=1", headers=RESEARCHER)
    assert res_page.status_code == 200
    page_data = res_page.json()
    assert len(page_data["items"]) == 1
    assert page_data["next_cursor"] is not None
    res_next = client.get(f"/api/v1/research/runs?limit=1&cursor={page_data['next_cursor']}", headers=RESEARCHER)
    assert res_next.status_code == 200
    assert res_next.json()["items"][0]["id"] != page_data["items"][0]["id"]


def test_invalid_cursor_is_rejected_not_reset_to_the_first_page(client: TestClient, sample_runs):
    run_id = str(sample_runs["val"].id)
    for url in ("/api/v1/research/runs", f"/api/v1/research/runs/{run_id}/items", "/api/v1/research/annotation-tasks"):
        for cursor in ("not-base64!", "bm90LWEtbnVtYmVy", "LTE="):  # garbage, b64 of "not-a-number", b64 of "-1"
            res = client.get(f"{url}?cursor={cursor}", headers=RESEARCHER)
            assert res.status_code == 422, (url, cursor)
            assert res.json()["detail"] == "invalid_cursor"


def test_get_run_detail(client: TestClient, sample_runs):
    run_id = str(sample_runs["val"].id)
    res = client.get(f"/api/v1/research/runs/{run_id}", headers=RESEARCHER)
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == run_id
    assert data["name"] == "TCGA-BRCA Validated Arm P2"
    assert data["headline"]["ns_g"]["value"] == 0.58
    assert data["headline"]["ns_g"]["status"] == "final"
    assert data["headline"]["ns_m"]["value"] == 0.71
    assert data["gate"] == {"valid": True, "int_prov": 1.0, "int_fall": 0, "disjoint": True}
    # tcga_brca_dx is research-scoped in eval/datasets/registry.yaml
    assert data["license_scopes"] == ["research"]


def test_license_scope_of_an_unregistered_dataset_is_pending(client: TestClient, db_session: Session, manifest):
    run = make_run(db_session, "Unregistered", manifest, dataset="not_in_registry")
    db_session.commit()
    res = client.get(f"/api/v1/research/runs/{run.id}", headers=RESEARCHER)
    assert res.json()["license_scopes"] == ["pending"]


def test_get_run_detail_not_found(client: TestClient):
    fake_id = str(uuid.uuid4())
    res = client.get(f"/api/v1/research/runs/{fake_id}", headers=RESEARCHER)
    assert res.status_code == 404
    assert res.json()["detail"] == "run_not_found"


def test_run_name_shared_by_two_runs_is_ambiguous(client: TestClient, db_session: Session, manifest):
    make_run(db_session, "twin", manifest)
    make_run(db_session, "twin", manifest)
    db_session.commit()
    res = client.get("/api/v1/research/runs/twin", headers=RESEARCHER)
    assert res.status_code == 409
    assert res.json()["detail"] == "run_name_ambiguous"


# --- the gate is computed, never assumed --------------------------------------


def test_gate_fails_and_headline_is_invalid_without_full_provenance(client: TestClient, db_session: Session, manifest):
    run = make_run(db_session, "no provenance", manifest)
    db_session.add(RunMetric(run_id=run.id, metric_id="ns_g", value=0.6, ci_low=0.5, ci_high=0.7, n=10, status="final"))
    add_item(db_session, run, "slide-01", "TCGA-01", PRED_BEFORE["slide-01"])
    add_item(db_session, run, "slide-02", "TCGA-02", PRED_BEFORE["slide-02"], with_decision=False)
    db_session.commit()

    data = client.get(f"/api/v1/research/runs/{run.id}", headers=RESEARCHER).json()
    assert data["gate"]["int_prov"] == 0.5
    assert data["gate"]["valid"] is False
    assert data["headline"]["ns_g"]["status"] == "invalid"  # SPEC-08 AC4
    assert data["headline"]["ns_g"]["value"] == 0.6


def test_gate_fails_on_a_fallback_decision(client: TestClient, db_session: Session, manifest):
    run = make_run(db_session, "with fallback", manifest)
    add_arm(db_session, run, {"slide-01": PRED_BEFORE["slide-01"]}, producer_kind="fallback")
    db_session.commit()
    gate = client.get(f"/api/v1/research/runs/{run.id}", headers=RESEARCHER).json()["gate"]
    assert gate["int_fall"] == 2
    assert gate["valid"] is False


def test_gate_is_not_valid_for_a_run_with_no_succeeded_item(client: TestClient, sample_runs):
    gate = client.get(f"/api/v1/research/runs/{sample_runs['test'].id}", headers=RESEARCHER).json()["gate"]
    assert gate["int_prov"] == 0.0
    assert gate["valid"] is False


def test_gate_fails_when_a_patient_is_in_two_splits(client: TestClient, db_session: Session, tmp_path):
    leaky = write_manifest(tmp_path / "leaky.parquet", MANIFEST_ROWS + [("slide-07", "TCGA-01", "test", 1, 4, 1, 1, 2, "Aperio")])
    run = make_run(db_session, "leaky", leaky)
    add_arm(db_session, run, PRED_BEFORE)
    db_session.commit()
    gate = client.get(f"/api/v1/research/runs/{run.id}", headers=RESEARCHER).json()["gate"]
    assert gate["disjoint"] is False
    assert gate["valid"] is False


def test_gate_fails_closed_when_the_manifest_cannot_be_read(client: TestClient, db_session: Session, manifest):
    run = make_run(db_session, "gone", ("/no/such/manifest.parquet", manifest[1]))
    add_arm(db_session, run, PRED_BEFORE)
    db_session.commit()
    gate = client.get(f"/api/v1/research/runs/{run.id}", headers=RESEARCHER).json()["gate"]
    assert gate["disjoint"] is False
    assert gate["valid"] is False


def test_gate_requires_the_recorded_hashes(client: TestClient, db_session: Session, manifest):
    run = make_run(db_session, "no lock", manifest, splits_lock_sha256=None)
    add_arm(db_session, run, PRED_BEFORE)
    db_session.commit()
    gate = client.get(f"/api/v1/research/runs/{run.id}", headers=RESEARCHER).json()["gate"]
    assert gate["valid"] is False


# =============================================================================
# 3. Items & Decision Tree Tests (AC3)
# =============================================================================


def test_list_run_items_and_detail_tree(client: TestClient, sample_runs):
    run_id = str(sample_runs["val"].id)
    res = client.get(f"/api/v1/research/runs/{run_id}/items", headers=RESEARCHER)
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 4
    item = data["items"][0]
    assert item["slide_id"] == "slide-01"
    assert item["pred"]["grade"] == 1
    assert item["pred"]["total"] == 4

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


def test_items_carry_the_manifest_ground_truth_and_sum_error(client: TestClient, sample_runs):
    run_id = str(sample_runs["val"].id)
    items = {i["slide_id"]: i for i in client.get(f"/api/v1/research/runs/{run_id}/items", headers=RESEARCHER).json()["items"]}
    wrong = items["slide-03"]
    assert wrong["gt"] == {"grade": 2, "total": 7, "tubule": 2, "pleo": 3, "mitoses": 2, "histotype": None}
    assert wrong["pred"]["grade"] == 3
    assert wrong["sum_error"] == 1
    assert items["slide-01"]["sum_error"] == 0


def test_items_of_an_unreadable_manifest_are_503_not_empty_truth(client: TestClient, db_session: Session, manifest):
    run = make_run(db_session, "gone", ("/no/such/manifest.parquet", manifest[1]))
    add_arm(db_session, run, PRED_BEFORE)
    db_session.commit()
    res = client.get(f"/api/v1/research/runs/{run.id}/items", headers=RESEARCHER)
    assert res.status_code == 503
    assert res.json()["detail"] == "manifest_unavailable"


def test_item_tree_holds_only_this_runs_decisions(client: TestClient, db_session: Session, sample_runs):
    item = sample_runs["item"]
    other_run = sample_runs["after"]
    execution = db_session.scalars(select(StageExecution).where(StageExecution.case_id == item.case_id)).first()
    db_session.add(DecisionRecord(
        id=uuid.uuid4(), case_id=item.case_id, stage_execution_id=execution.id, run_id=other_run.id,
        stage="grading", task="nottingham_grade", entity_type="slide", entity_id="slide-01",
        producer_kind="model", producer_id="other", producer_version="v0", status="ok", input_sha256="x",
        input_spec={}, output={}, latency_ms=1, run_mode="eval", config_hash="cfg_hash_1",
    ))
    db_session.commit()
    res = client.get(f"/api/v1/research/runs/{sample_runs['val'].id}/items/slide-01", headers=RESEARCHER)
    assert [d["producer_id"] for d in res.json()["decisions"]] == ["grading_ensemble"]


def test_get_run_metrics_is_the_metrics_document(client: TestClient, sample_runs):
    res = client.get(f"/api/v1/research/runs/{sample_runs['val'].id}/metrics", headers=RESEARCHER)
    assert res.status_code == 200
    doc = res.json()
    assert doc["metrics_schema_version"] == 1
    assert doc["headline"]["ns_g"]["n"] == 4
    assert doc["counts"]["int_fall"] == 0
    assert "curves" in doc and "unavailable" in doc


def test_metrics_not_ready_for_an_unfinished_run(client: TestClient, db_session: Session, manifest):
    run = make_run(db_session, "running", manifest, status="running")
    db_session.commit()
    res = client.get(f"/api/v1/research/runs/{run.id}/metrics", headers=RESEARCHER)
    assert res.status_code == 404
    assert res.json()["detail"] == "metrics_not_ready"


# =============================================================================
# 4. Mitosis Error Cards & What-If Curves (AC6)
# =============================================================================


def test_mitosis_curves_are_not_invented(client: TestClient, sample_runs):
    val_run_id = str(sample_runs["val"].id)
    for query in ("", "?tau_a=0.55&tau_b=0.65"):
        res = client.get(f"/api/v1/research/runs/{val_run_id}/curves/mitosis{query}", headers=RESEARCHER)
        assert res.status_code == 404
        assert res.json()["detail"] == "curves_not_available"


def test_mitosis_curves_what_if_strictly_forbidden_on_test_split(client: TestClient, sample_runs):
    test_run_id = str(sample_runs["test"].id)
    # What-if on test split must raise 400 not_val_split
    res = client.get(
        f"/api/v1/research/runs/{test_run_id}/curves/mitosis?tau_a=0.50",
        headers=RESEARCHER,
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "not_val_split"


def test_mitosis_error_cards_are_not_an_empty_page(client: TestClient, sample_runs):
    res = client.get(f"/api/v1/research/runs/{sample_runs['val'].id}/errors/mitosis?kind=fp", headers=RESEARCHER)
    assert res.status_code == 404
    assert res.json()["detail"] == "mitosis_errors_not_available"
    assert client.get(f"/api/v1/research/runs/{uuid.uuid4()}/errors/mitosis", headers=RESEARCHER).status_code == 404


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


def test_compare_runs_of_different_splits_is_not_comparable(client: TestClient, sample_runs):
    res = client.get(
        f"/api/v1/research/compare?a={sample_runs['val'].id}&b={sample_runs['test'].id}", headers=RESEARCHER
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "runs_not_comparable"


def test_compare_runs_same_manifest_succeeds_with_real_slices_and_flips(client: TestClient, sample_runs):
    res = client.get(
        f"/api/v1/research/compare?a={sample_runs['val'].id}&b={sample_runs['after'].id}", headers=RESEARCHER
    )
    assert res.status_code == 200
    data = res.json()
    assert data["manifest_sha256"] == sample_runs["val"].manifest_sha256
    (row,) = data["metrics"]
    assert row["a"]["n"] == row["b"]["n"] == 4
    assert row["delta"] > 0  # the P3 arm grades slide-03 correctly
    assert row["delta_low"] <= row["delta"] <= row["delta_high"]
    assert {(s["slice"], s["key"]) for s in data["slices"]} == {
        ("scanner", "Aperio"), ("scanner", "Hamamatsu"), ("native_mag", "40"),
    }
    assert data["flips"] == [{"slide_id": "slide-03", "component": "grade", "a_correct": False, "b_correct": True}]


def test_compare_does_not_fall_back_to_invented_intervals(client: TestClient, db_session: Session, manifest):
    """Runs whose items are still active cannot be compared; the answer is an error, not ±0.05."""
    a = make_run(db_session, "a", manifest)
    b = make_run(db_session, "b", manifest)
    add_arm(db_session, a, PRED_BEFORE)
    add_arm(db_session, b, PRED_AFTER)
    db_session.add(ValidationItem(run_id=b.id, slide_id="slide-02-x", patient_id="TCGA-02", status="running"))
    db_session.commit()
    res = client.get(f"/api/v1/research/compare?a={a.id}&b={b.id}", headers=RESEARCHER)
    assert res.status_code == 409
    assert res.json()["detail"] == "run_not_finished"


# =============================================================================
# 6. Issue Register & Resolution Gate (AC5)
# =============================================================================


def _new_issue(client: TestClient, evidence_run, metric="ns_g"):
    payload = {
        "title": "Low tubule formation precision on pleomorphic cases",
        "category": "model",
        "severity": "high",
        "metric_impact": {"metric": metric, "est_delta": -0.07},
        "evidence": [{"run_id": str(evidence_run.id), "slide_id": "slide-03"}],
        "spec_ref": "SPEC-00 §4.2",
    }
    res = client.post("/api/v1/research/issues", json=payload, headers=RESEARCHER)
    assert res.status_code == 201
    return res.json()


def test_issue_lifecycle_and_resolution_gate(client: TestClient, sample_runs):
    issue = _new_issue(client, sample_runs["val"])
    issue_id = issue["id"]
    assert issue["status"] == "open"
    assert issue["created_by"] == USER_IDS["researcher"]

    # Patching status to resolved WITHOUT resolved_in must return 422 resolution_requires_run
    res_fail1 = client.patch(f"/api/v1/research/issues/{issue_id}", json={"status": "resolved"}, headers=RESEARCHER)
    assert res_fail1.status_code == 422
    assert res_fail1.json()["detail"] == "resolution_requires_run"

    # ... with a nonexistent run
    res_fail2 = client.patch(
        f"/api/v1/research/issues/{issue_id}",
        json={"status": "resolved", "resolved_in": "nonexistent_run_xyz"},
        headers=RESEARCHER,
    )
    assert res_fail2.status_code == 422
    assert res_fail2.json()["detail"] == "resolution_requires_run"

    # ... with the run that is also the evidence (no paired comparison is possible)
    res_fail3 = client.patch(
        f"/api/v1/research/issues/{issue_id}",
        json={"status": "resolved", "resolved_in": "TCGA-BRCA Validated Arm P2"},
        headers=RESEARCHER,
    )
    assert res_fail3.status_code == 422
    assert res_fail3.json()["detail"] == "resolution_requires_run"

    # A run that improves the metric over the evidence run resolves the issue
    res_ok = client.patch(
        f"/api/v1/research/issues/{issue_id}",
        json={
            "status": "resolved",
            "resolved_in": "TCGA-BRCA Validated Arm P3",
            "resolution_note": "Resolved by hyperparameter tuning on ordinal loss.",
        },
        headers=RESEARCHER,
    )
    assert res_ok.status_code == 200
    updated = res_ok.json()
    assert updated["status"] == "resolved"
    assert updated["resolved_in"] == "TCGA-BRCA Validated Arm P3"
    assert updated["resolution_note"] == "Resolved by hyperparameter tuning on ordinal loss."


def test_resolution_is_refused_when_the_metric_got_worse(client: TestClient, sample_runs):
    issue = _new_issue(client, sample_runs["after"])  # evidence: the better run
    res = client.patch(
        f"/api/v1/research/issues/{issue['id']}",
        json={"status": "resolved", "resolved_in": "TCGA-BRCA Validated Arm P2"},  # the worse run
        headers=RESEARCHER,
    )
    assert res.status_code == 422
    assert res.json()["detail"] == "resolution_requires_run"


def test_resolution_needs_a_comparable_metric(client: TestClient, sample_runs):
    issue = _new_issue(client, sample_runs["val"], metric="ns_m_f1")  # not computable from a whole-slide run
    res = client.patch(
        f"/api/v1/research/issues/{issue['id']}",
        json={"status": "resolved", "resolved_in": "TCGA-BRCA Validated Arm P3"},
        headers=RESEARCHER,
    )
    assert res.status_code == 422
    assert res.json()["detail"] == "resolution_requires_run"


def test_resolved_in_is_refused_unless_the_issue_is_resolved(client: TestClient, sample_runs):
    issue = _new_issue(client, sample_runs["val"])
    res = client.patch(
        f"/api/v1/research/issues/{issue['id']}",
        json={"status": "triaged", "resolved_in": "TCGA-BRCA Validated Arm P3"},
        headers=RESEARCHER,
    )
    assert res.status_code == 422
    assert res.json()["detail"] == "resolved_in_requires_resolved_status"


def test_issue_owner_must_be_a_user_and_a_malformed_issue_id_is_404(client: TestClient, sample_runs):
    issue = _new_issue(client, sample_runs["val"])
    url = f"/api/v1/research/issues/{issue['id']}"
    assert client.patch(url, json={"owner": "not-a-uuid"}, headers=RESEARCHER).json()["detail"] == "owner_not_found"
    assert client.patch(url, json={"owner": str(uuid.uuid4())}, headers=RESEARCHER).status_code == 422
    ok = client.patch(url, json={"owner": USER_IDS["pathologist"]}, headers=RESEARCHER)
    assert ok.status_code == 200 and ok.json()["owner"] == USER_IDS["pathologist"]
    cleared = client.patch(url, json={"owner": ""}, headers=RESEARCHER)
    assert cleared.json()["owner"] is None
    res = client.patch("/api/v1/research/issues/not-a-uuid", json={"status": "triaged"}, headers=RESEARCHER)
    assert res.status_code == 404
    assert res.json()["detail"] == "issue_not_found"


# =============================================================================
# 7. Pathologist Annotation Tasks (AC7)
# =============================================================================


def _mitosis_task(**kw) -> AnnotationTaskModel:
    fields = dict(
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
    fields.update(kw)
    return AnnotationTaskModel(**fields)


def test_annotation_task_workflow(client: TestClient, db_session: Session):
    db_session.add(_mitosis_task())
    db_session.commit()

    # List tasks
    res_list = client.get("/api/v1/research/annotation-tasks", headers=PATHOLOGIST)
    assert res_list.status_code == 200
    data = res_list.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == "task_mitosis_01"
    assert data["items"][0]["regions_um"] == [[[100.0, 100.0], [200.0, 100.0], [200.0, 200.0], [100.0, 200.0]]]
    assert data["items"][0]["my_annotation"] is None

    # Submit invalid payloads -> 422 invalid_payload
    for bad in (
        {"invalid_key": "data"},
        {"points": [{"x_um": "1", "y_um": 2, "class": "MF"}]},
        {"points": [{"x_um": 1, "y_um": 2, "class": "other"}]},
        {"points": [{"x_um": True, "y_um": 2, "class": "MF"}]},
    ):
        res_bad = client.post(
            "/api/v1/research/annotations",
            json={"task_id": "task_mitosis_01", "payload": bad, "status": "submitted"},
            headers=PATHOLOGIST,
        )
        assert res_bad.status_code == 422, bad
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

    mine = client.get("/api/v1/research/annotation-tasks", headers=PATHOLOGIST).json()["items"][0]["my_annotation"]
    assert mine["status"] == "submitted"
    assert len(mine["payload"]["points"]) == 2


def test_annotation_is_per_dataset_and_unknown_task_is_404(client: TestClient, db_session: Session):
    db_session.add_all([_mitosis_task(), _mitosis_task(id="task_other_dataset", dataset="bcnb")])
    db_session.commit()
    body = {"task_id": "task_mitosis_01", "payload": {"points": []}, "status": "draft"}
    assert client.post("/api/v1/research/annotations", json=body, headers=PATHOLOGIST).status_code == 201
    items = {t["id"]: t for t in client.get("/api/v1/research/annotation-tasks", headers=PATHOLOGIST).json()["items"]}
    assert items["task_mitosis_01"]["my_annotation"] is not None
    assert items["task_other_dataset"]["my_annotation"] is None  # same slide id, other dataset
    missing = client.post("/api/v1/research/annotations", json={**body, "task_id": "nope"}, headers=PATHOLOGIST)
    assert missing.status_code == 404


def test_malformed_stored_region_fails_loudly(db_session: Session):
    from app.routers.research import _polygons

    with pytest.raises(ValueError, match="malformed region"):
        _polygons(_mitosis_task(regions_um=[[1, 2, 3]]))


# =============================================================================
# 8. Label QA Queue & Reason Enforcement (AC7)
# =============================================================================


def test_label_qa_workflow_and_reason_enforcement(client: TestClient, db_session: Session):
    qa_item = QAItemModel(
        patient_id="TCGA-BRCA-001",
        dataset="tcga_brca_dx",
        protocol_version="labels-qa-v1",
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


def test_label_qa_review_is_recorded_with_the_items_dataset_and_protocol(client: TestClient, db_session: Session):
    db_session.add(QAItemModel(
        patient_id="TCGA-BRCA-002", dataset="some_dataset", protocol_version="labels-qa-v9",
        report_text_url="u", regex_data={}, llm_data={}, status="pending",
    ))
    db_session.commit()
    res = client.post(
        "/api/v1/research/labels-qa/TCGA-BRCA-002",
        json={"action": "edit", "values": {"grade": 2}, "reason": "report says 2"},
        headers=RESEARCHER,
    )
    assert res.status_code == 200
    (ann,) = db_session.scalars(select(GTAnnotation).where(GTAnnotation.task == "label_qa")).all()
    assert (ann.dataset, ann.protocol_version, ann.slide_id) == ("some_dataset", "labels-qa-v9", "TCGA-BRCA-002")
    assert ann.payload == {"action": "edit", "values": {"grade": 2}, "reason": "report says 2"}
    assert str(ann.annotator_id) == USER_IDS["researcher"]
    assert client.post("/api/v1/research/labels-qa/missing", json={"action": "accept"}, headers=RESEARCHER).status_code == 404
