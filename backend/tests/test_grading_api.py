"""Stage 5 review API = the grading_v6 contract (docs/contracts/grading_v6.md; WP-8.6)."""
import copy
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.db import Base, get_db
from app.core.gcs import upload_blob_from_bytes
from app.main import app
from app.models.case import Case
from app.models.grading import Grading
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from pipeline.grading import MACHINE_SCHEMA, sample_blob

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
BASE = "/api/v1/stages/grading"


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def setup_db_override():
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.pop(get_db, None)
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    return TestClient(app)


def tubule(sid, pct, area, present=True, failed=False):
    return {"id": sid, "center_um": [1000.0, 1000.0], "size_um": 512.0, "mpp": 1.0, "stratum": 0, "hotspot_id": "hs_01",
            "tumor_area_um2": area, "estimate": None if failed else {"tumor_present": present, "tubule_percent": pct},
            "rationale": None if failed else "r", "record_id": str(uuid.uuid4())}


def field(sid, score, failed=False, check=None):
    check = score if check is None else check
    return {"id": sid, "center_um": [1000.0, 1000.0], "size_um": 128.0, "mpp": 0.25, "stratum": 0, "hotspot_id": "hs_01",
            "tumor_area_um2": 16384.0, "estimate": None if failed else {"pleomorphism_score": score}, "nuclei": None,
            "verification": {"producer": "medgemma", "pleomorphism_score": check,
                             "agrees": None if failed else check == score, "record_id": str(uuid.uuid4())},
            "rationale": None if failed else "r", "record_id": str(uuid.uuid4())}


def v6_machine(samples, fields, mitotic_score=2, histotype="IDC-NST", flags=()):
    return {
        "schema": MACHINE_SCHEMA, "case_id": "x", "slide_id": "y",
        "frame": {"hotspot_ids": ["hs_01"], "n_candidates": 100},
        "tubule": {"samples": samples, "requested": len(samples), "estimator": "T1:gemini_referee@tubule@v1"},
        "pleomorphism": {"fields": fields, "requested": len(fields), "aggregation": "mode",
                         "estimator": "P1:gemini_referee@pleo@v1"},
        "histotype": {"type": histotype, "rationale": "cohesive nests", "record_id": str(uuid.uuid4())} if histotype else None,
        "histotype_estimator": "H1:gemini_referee@histologic_type@v1",
        "mitotic": {"score": mitotic_score, "count_total": 12, "n_hpf": 10, "area_mm2": 2.157, "per_mm2": 5.56, "flags": []},
        "shortfall": {"tubule": 0, "pleo": 0}, "flags": list(flags), "model_versions": {}, "generated_at": "2026-10-04T00:00:00Z",
    }


DEFAULT_SAMPLES = [tubule("t_01", 40, 200000.0), tubule("t_02", 10, 100000.0)]
DEFAULT_FIELDS = [field("p_01", 2), field("p_02", 3), field("p_03", 3)]


def seed(machine=None, status="awaiting_review", histotype="IDC-NST"):
    db = TestingSessionLocal()
    case_id = uuid.uuid4()
    machine = machine or v6_machine(copy.deepcopy(DEFAULT_SAMPLES), copy.deepcopy(DEFAULT_FIELDS))
    db.add_all([
        Case(id=case_id, created_by="t", status="open"),
        Slide(id=uuid.uuid4(), case_id=case_id, gcs_uri_original="gs://raw/x.svs", mpp_x=0.25, mpp_y=0.25,
              width_px=80000, height_px=60000),
        StageExecution(id=uuid.uuid4(), case_id=case_id, stage="grading", attempt=1, status=status,
                       config_hash="abc", run_mode="clinical", model_versions={"gemini_referee": "v"}),
        Grading(case_id=case_id, histologic_type=histotype, type_confirmed_by="unconfirmed", machine=machine, overrides={}),
    ])
    db.commit()
    db.close()
    return str(case_id)


def grading_row(case_id):
    db = TestingSessionLocal()
    try:
        return db.get(Grading, uuid.UUID(case_id))
    finally:
        db.close()


def post(client, path, body):
    return client.post(f"{BASE}/{path}", json=body)


def test_get_aggregates_from_machine_output(client):
    case_id = seed()
    body = client.get(f"{BASE}/{case_id}").json()
    # T% = (40 * 200000 + 10 * 100000) / 300000 = 30.0 -> score 2; P = mode(2, 3, 3) = 3; M = 2.
    assert body["tubule"]["percent"] == 30.0 and body["tubule"]["score"] == 2 and body["tubule"]["n_used"] == 2
    assert body["pleomorphism"]["score"] == 3 and body["pleomorphism"]["aggregation"] == "mode"
    assert body["total"] == 7 and body["grade"] == 2 and body["flags"] == ["near_grade_boundary"]
    assert body["mitotic"] == {"score": 2, "count_total": 12, "n_hpf": 10, "area_mm2": 2.157, "per_mm2": 5.56}
    assert body["histotype"] == {"type": "IDC-NST", "estimator": "H1:gemini_referee@histologic_type@v1",
                                 "rationale": "cohesive nests", "confirmed": False, "confirmed_by": None}
    assert body["tubule"]["samples"][0]["image_url"] == f"{BASE}/{case_id}/tubule/t_01/image"
    assert body["provenance"] == {"stage": "grading", "model_versions": {"gemini_referee": "v"}, "config_hash": "abc",
                                  "run_mode": "clinical"}


def test_a_v5_grading_is_not_found_on_every_route(client):
    v5 = {"case_id": "x", "patches": [], "aggregate": {}, "histologic_type": None}
    case_id = seed(machine=v5)
    assert client.get(f"{BASE}/{case_id}").json()["error"] == "not_found"
    for path, body in [
        ("review-sample", {"case_id": case_id, "kind": "pleo", "sample_id": "p_01", "value": {"pleomorphism_score": 1}}),
        ("override", {"case_id": case_id, "component": "pleo", "value": 1, "reason": "ten chars or more"}),
        ("histotype/confirm", {"case_id": case_id, "type": "ILC"}),
        ("confirm", {"case_id": case_id}),
    ]:
        resp = post(client, path, body)
        assert resp.status_code == 404 and resp.json()["error"] == "not_found", path


def test_unknown_case_is_not_found(client):
    resp = client.get(f"{BASE}/{uuid.uuid4()}")
    assert resp.status_code == 404 and resp.json()["error"] == "not_found"
    assert client.get(f"{BASE}/not-a-uuid").status_code == 404


def test_review_sample_reaggregates_and_never_touches_the_machine_output(client):
    case_id = seed()
    before = copy.deepcopy(grading_row(case_id).machine)
    body = post(client, "review-sample", {"case_id": case_id, "kind": "tubule", "sample_id": "t_02",
                                          "value": {"tumor_present": False}}).json()
    assert body["tubule"]["percent"] == 40.0 and body["tubule"]["n_used"] == 1
    review = body["tubule"]["samples"][1]["review"]
    assert review["tumor_present"] is False and review["by"] and review["at"]
    # A second review merges into the first.
    body = post(client, "review-sample", {"case_id": case_id, "kind": "tubule", "sample_id": "t_02",
                                          "value": {"tubule_percent": 90}}).json()
    assert body["tubule"]["samples"][1]["review"]["tumor_present"] is False
    row = grading_row(case_id)
    assert row.machine == before
    assert row.tubule_percent == 40.0 and row.overrides["reviews"]["tubule"]["t_02"]["tubule_percent"] == 90.0


def test_review_of_a_failed_estimate_clears_needs_human(client):
    machine = v6_machine([tubule("t_01", 40, 1.0e5), tubule("t_02", 0, 1.0e5, failed=True)], [field("p_01", 2)])
    case_id = seed(machine=machine)
    assert "needs_human" in client.get(f"{BASE}/{case_id}").json()["flags"]
    body = post(client, "review-sample", {"case_id": case_id, "kind": "tubule", "sample_id": "t_02",
                                          "value": {"tumor_present": True, "tubule_percent": 20}}).json()
    assert "needs_human" not in body["flags"] and body["tubule"]["percent"] == 30.0


@pytest.mark.parametrize("kind,value", [
    ("tubule", {}), ("tubule", {"tubule_percent": 120}), ("tubule", {"tumor_present": "yes"}),
    ("tubule", {"pleomorphism_score": 2}), ("pleo", {"pleomorphism_score": 4}), ("pleo", {"pleomorphism_score": True}),
    ("nuclei", {"pleomorphism_score": 2}),
])
def test_invalid_reviews_are_refused(client, kind, value):
    case_id = seed()
    sample = "t_01" if kind == "tubule" else "p_01"
    resp = post(client, "review-sample", {"case_id": case_id, "kind": kind, "sample_id": sample, "value": value})
    assert resp.status_code == 422 and resp.json()["error"] == "invalid_value"


def test_unknown_sample_and_locked_stage(client):
    case_id = seed()
    resp = post(client, "review-sample", {"case_id": case_id, "kind": "pleo", "sample_id": "p_99", "value": {"pleomorphism_score": 1}})
    assert resp.status_code == 404 and resp.json()["error"] == "sample_not_found"
    locked = seed(status="confirmed")
    for path, body in [
        ("review-sample", {"case_id": locked, "kind": "pleo", "sample_id": "p_01", "value": {"pleomorphism_score": 1}}),
        ("override", {"case_id": locked, "component": "pleo", "value": 1, "reason": "ten chars or more"}),
        ("histotype/confirm", {"case_id": locked, "type": "ILC"}),
    ]:
        resp = post(client, path, body)
        assert resp.status_code == 409 and resp.json()["error"] == "stage_locked", path


def test_override_needs_a_reason_and_a_valid_value_and_can_be_cleared(client):
    case_id = seed()
    short = post(client, "override", {"case_id": case_id, "component": "pleo", "value": 1, "reason": "too short"})
    assert short.status_code == 422 and short.json()["error"] == "reason_too_short"
    bad = post(client, "override", {"case_id": case_id, "component": "pleo", "value": 4, "reason": "a long enough reason"})
    assert bad.status_code == 422 and bad.json()["error"] == "invalid_value"
    body = post(client, "override", {"case_id": case_id, "component": "pleo", "value": 1, "reason": "uniform small nuclei"}).json()
    assert body["overrides"] == {"pleo_score": 1, "reasons": {"pleo": "uniform small nuclei"}}
    assert body["pleomorphism"]["score"] == 3  # the machine aggregate is still shown
    assert body["total"] == 5 and body["grade"] == 1
    assert grading_row(case_id).pleo_score == 1 and grading_row(case_id).grade == 1
    body = post(client, "override", {"case_id": case_id, "component": "pleo", "value": None, "reason": "back to the estimate"}).json()
    assert body["overrides"] == {"reasons": {}} and body["total"] == 7


def test_histotype_confirm_and_override(client):
    case_id = seed()
    bad = post(client, "histotype/confirm", {"case_id": case_id, "type": "carcinoid"})
    assert bad.status_code == 422 and bad.json()["error"] == "invalid_value"
    body = post(client, "histotype/confirm", {"case_id": case_id, "type": "ILC"}).json()
    assert body["histotype"]["type"] == "ILC" and body["histotype"]["confirmed"] is True and body["histotype"]["confirmed_by"]
    # An override changes the type and needs a new confirmation.
    body = post(client, "override", {"case_id": case_id, "component": "histotype", "value": "mucinous",
                                     "reason": "extracellular mucin pools"}).json()
    assert body["histotype"]["type"] == "mucinous" and body["histotype"]["confirmed"] is False
    assert body["overrides"]["histotype"] == "mucinous"


def test_confirm_gates_then_closes_the_case(client):
    case_id = seed()
    resp = post(client, "confirm", {"case_id": case_id})
    assert resp.status_code == 409 and resp.json()["error"] == "histotype_unconfirmed"
    post(client, "histotype/confirm", {"case_id": case_id, "type": "IDC-NST"})
    resp = post(client, "confirm", {"case_id": case_id})
    assert resp.status_code == 200 and resp.json() == {"status": "confirmed", "case_status": "done", "next_stage": None}
    db = TestingSessionLocal()
    assert db.get(Case, uuid.UUID(case_id)).status == "done"
    db.close()
    again = post(client, "confirm", {"case_id": case_id})
    assert again.status_code == 409 and again.json()["error"] == "not_awaiting_review"


def test_confirm_refuses_a_missing_component(client):
    case_id = seed(machine=v6_machine(copy.deepcopy(DEFAULT_SAMPLES), copy.deepcopy(DEFAULT_FIELDS), mitotic_score=None))
    post(client, "histotype/confirm", {"case_id": case_id, "type": "IDC-NST"})
    resp = post(client, "confirm", {"case_id": case_id})
    assert resp.status_code == 409 and resp.json() == {
        "error": "missing_component", "detail": "No score for mitotic", "components": ["mitotic"]}


def test_a_tied_pleomorphism_mode_takes_the_highest_score(client):
    """Owner decision 2026-10-04 (scoring.yaml grading.pleo_tie_break: max)."""
    case_id = seed(machine=v6_machine(copy.deepcopy(DEFAULT_SAMPLES), [field("p_01", 1), field("p_02", 2), field("p_03", 2), field("p_04", 1)]))
    assert client.get(f"{BASE}/{case_id}").json()["pleomorphism"]["score"] == 2


def test_sample_images_are_served_or_404(client):
    case_id = seed()
    missing = client.get(f"{BASE}/{case_id}/pleo/p_01/image")
    assert missing.status_code == 404 and missing.json()["error"] == "image_not_found"
    upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, sample_blob(case_id, "pleo", "p_01"), b"\x89PNG", "image/png")
    resp = client.get(f"{BASE}/{case_id}/pleo/p_01/image")
    assert resp.status_code == 200 and resp.content == b"\x89PNG" and resp.headers["content-type"] == "image/png"
    assert client.get(f"{BASE}/{case_id}/nuclei/p_01/image").status_code == 422


def test_v5_routes_are_gone(client):
    case_id = seed()
    for path in ("patches/review", "hpfs/review", "recompute", "type/confirm", f"{case_id}/type/confirm"):
        assert post(client, path, {"case_id": case_id}).status_code in (404, 405), path


def test_a_viewer_cannot_confirm_or_edit(client):
    """Ported from the v5 suite (test_batch8): confirmation needs stage:confirm (SPEC-03 §4.1)."""
    case_id = seed()
    for path, body in [("confirm", {"case_id": case_id}), ("histotype/confirm", {"case_id": case_id, "type": "ILC"}),
                       ("override", {"case_id": case_id, "component": "pleo", "value": 1, "reason": "ten chars or more"})]:
        resp = client.post(f"{BASE}/{path}", json=body, headers={"X-Test-Role": "viewer"})
        assert resp.status_code == 403, path


def test_every_estimator_type_can_be_confirmed(client):
    """Ported from the v5 suite (test_batch8): every type the estimator can propose is a valid confirmation."""
    from typing import get_args

    from app.inference.schemas import HistotypeVerdict

    for histotype in get_args(HistotypeVerdict.model_fields["type"].annotation):
        case_id = seed(histotype=histotype)
        resp = post(client, "histotype/confirm", {"case_id": case_id, "type": histotype})
        assert resp.status_code == 200 and resp.json()["histotype"]["type"] == histotype, histotype


def test_each_field_carries_the_verifiers_independent_score(client):
    """Owner decision 2026-10-04: a second model scores each field; a disagreement is shown, the grade is unchanged."""
    fields = [field("p_01", 2, check=3), field("p_02", 3), field("p_03", 3)]
    case_id = seed(machine=v6_machine(copy.deepcopy(DEFAULT_SAMPLES), fields))
    body = client.get(f"{BASE}/{case_id}").json()
    assert [f["verification"] for f in body["pleomorphism"]["fields"]] == [
        {"producer": "medgemma", "pleomorphism_score": 3, "agrees": False},
        {"producer": "medgemma", "pleomorphism_score": 3, "agrees": True},
        {"producer": "medgemma", "pleomorphism_score": 3, "agrees": True},
    ]
    assert body["pleomorphism"]["score"] == 3 and body["grade"] == 2  # from the estimates, as before
