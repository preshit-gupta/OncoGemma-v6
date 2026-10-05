"""
Stage 4 review API: the mitosis_v6 routes, server-side recompute, error bodies and the review gate
(docs/contracts/mitosis_v6.md; SPEC-06 §5.6-5.8, AC8, AC10; WP-7.6a).
"""
import uuid

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.db import Base, get_db
from app.core.gcs import upload_blob_from_bytes
from app.main import app
from app.models.case import Case
from app.models.decision_record import DecisionRecord
from app.models.detection import Detection
from app.models.hotspot import Hotspot
from app.models.hpf_site import HpfSite
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from tests.fakes.stage2 import seed_stage2
from tests.test_mitosis_gate import save_tumor_mask

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)

SIDE_PX, MPP = 20000, 0.25  # 5000 µm square


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def setup_mitosis_test_db():
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.pop(get_db, None)


client = TestClient(app)
HEADERS = {"X-Test-Role": "pathologist"}


def model_candidate(case_id, cid, xy, p_a, final_decision="mitosis", review_label=None):
    return Detection(id=cid, case_id=case_id, hotspot_id="hs_01", centroid_um=list(xy), p_a=p_a, p_b=None, vlm=None,
                     in_tumor=True, final_decision=final_decision, decision_path="A", review_label=review_label,
                     record_ids=[str(uuid.uuid4())])


@pytest.fixture
def setup_test_case():
    db = TestingSessionLocal()
    case_id = uuid.uuid4()
    db.add(Case(id=case_id, created_by="pathologist_test", status="open"))
    db.add(Slide(id=uuid.uuid4(), case_id=case_id, gcs_uri_original="gs://raw/slide.svs", mpp_x=MPP, mpp_y=MPP,
                 width_px=SIDE_PX, height_px=SIDE_PX))
    db.add(StageExecution(id=uuid.uuid4(), case_id=case_id, stage="mitosis", attempt=1, status="awaiting_review",
                          model_versions={"kongnet_det_midog_1": "v2"}, config_hash="a" * 64))
    db.flush()
    exec_id = db.scalars(select(StageExecution).where(StageExecution.case_id == case_id)).one().id
    for i in range(1, 4):
        centre = [1000.0 * i, 1000.0 * i]
        db.add(HpfSite(case_id=case_id, seq=i, center_um=centre, radius_um=250.0, mitotic_count=0,
                       tissue_coverage=0.9, tumor_fraction=0.8, source="model"))
        db.add(Hotspot(id=f"hs_{i:02d}", case_id=case_id, stage_execution_id=exec_id, source="model", excluded=False,
                       center_um=centre, hpf_diameter_um=500.0, window_um=600.0, rank=i,
                       polygon_um=[[centre[0] - 300, centre[1] - 300], [centre[0] + 300, centre[1] - 300],
                                   [centre[0] + 300, centre[1] + 300], [centre[0] - 300, centre[1] + 300],
                                   [centre[0] - 300, centre[1] - 300]]))
    db.add_all([
        model_candidate(case_id, "m_0001", (1010.0, 1010.0), 0.92),                   # inside HPF 1
        model_candidate(case_id, "m_0002", (2020.0, 2020.0), 0.85),                   # inside HPF 2
        model_candidate(case_id, "m_0003", (3010.0, 3010.0), 0.80, "equivocal"),      # inside HPF 3, needs review
        model_candidate(case_id, "m_0004", (4500.0, 300.0), 0.79, "equivocal"),       # outside every HPF
    ])
    db.commit()
    db.close()
    return str(case_id)


def get(case_id):
    res = client.get(f"/api/v1/stages/mitosis/{case_id}", headers=HEADERS)
    assert res.status_code == 200, res.text
    return res.json()


def post(path, body):
    return client.post(f"/api/v1/stages/mitosis/{path}", json=body, headers=HEADERS)


def count_records(case_id):
    db = TestingSessionLocal()
    try:
        return db.scalars(select(DecisionRecord).where(
            DecisionRecord.case_id == uuid.UUID(case_id), DecisionRecord.task == "mitosis_count"
        ).order_by(DecisionRecord.created_at)).all()
    finally:
        db.close()


def test_get_serves_the_v6_payload(setup_test_case):
    case_id = setup_test_case
    data = get(case_id)
    assert data["case_id"] == case_id and data["status"] == "awaiting_review"
    assert data["slide"] == {"width_px": SIDE_PX, "height_px": SIDE_PX, "mpp_x": MPP, "mpp_y": MPP}
    by_id = {c["id"]: c for c in data["candidates"]}
    assert set(by_id) == {"m_0001", "m_0002", "m_0003", "m_0004"}
    assert by_id["m_0001"]["counted"] is True and by_id["m_0003"]["counted"] is False
    assert by_id["m_0001"]["in_tumor"] is True and by_id["m_0001"]["decision_path"] == "A"
    assert by_id["m_0001"]["crop_url"] == f"/api/v1/stages/mitosis/{case_id}/candidates/m_0001/crop"
    assert by_id["m_0001"]["context_url"] == f"/api/v1/stages/mitosis/{case_id}/candidates/m_0001/context"
    assert [h["count"] for h in data["hpfs"]] == [1, 1, 0]
    assert all(h["tissue_coverage"] == 0.9 and h["tumor_fraction"] == 0.8 for h in data["hpfs"])
    summary = data["summary"]
    assert (summary["count_total"], summary["n_hpf"], summary["n_equivocal"]) == (2, 3, 1)  # m_0004 is outside the HPFs
    assert summary["flags"] == ["hpf_count_lt_10"] and summary["hpf_target"] == 10
    assert summary["area_mm2"] == 0.589 and summary["mitotic_score"] == 1  # 2 / 0.589 mm² = 3.40/mm² < 3.65
    assert [h["hotspot_id"] for h in data["hpfs"]] == ["hs_01", "hs_02", "hs_03"]
    assert data["hpfs"][0]["frame_um"][0] == [700.0, 700.0] and len(data["hpfs"][0]["frame_um"]) == 5
    # hpf_seq: the circle that contains the candidate, null outside every circle
    assert [by_id[k]["hpf_seq"] for k in ("m_0001", "m_0002", "m_0003", "m_0004")] == [1, 2, 3, None]
    assert data["provenance"] == {"stage": "mitosis", "model_versions": {"kongnet_det_midog_1": "v2"},
                                  "config_hash": "a" * 64, "run_mode": "clinical"}


def test_get_without_slide_geometry_is_not_found_instead_of_an_invented_size(setup_test_case):
    case_id = setup_test_case
    db = TestingSessionLocal()
    slide = db.scalars(select(Slide).where(Slide.case_id == uuid.UUID(case_id))).one()
    slide.width_px = None
    db.commit()
    db.close()
    res = client.get(f"/api/v1/stages/mitosis/{case_id}", headers=HEADERS)
    assert res.status_code == 404 and res.json()["error"] == "not_found"


def test_review_recomputes_on_the_server_and_writes_a_superseding_count_record(setup_test_case):
    case_id = setup_test_case
    data = post("review", {"case_id": case_id, "candidate_id": "m_0003", "review_label": "mitosis"}).json()
    assert data["summary"]["count_total"] == 3 and data["summary"]["n_equivocal"] == 0
    assert next(c for c in data["candidates"] if c["id"] == "m_0003")["counted"] is True

    data = post("review", {"case_id": case_id, "candidate_id": "m_0001", "review_label": "not_mitosis"}).json()
    assert data["summary"]["count_total"] == 2
    data = post("review", {"case_id": case_id, "candidate_id": "m_0001", "review_label": None}).json()
    assert data["summary"]["count_total"] == 3  # cleared: the model's decision counts again

    records = count_records(case_id)
    assert len(records) == 3
    assert records[0].supersedes_id is None and records[1].supersedes_id == records[0].id and records[2].supersedes_id == records[1].id
    assert records[-1].output == data["summary"]
    db = TestingSessionLocal()
    assert [h.mitotic_count for h in db.scalars(select(HpfSite).where(HpfSite.case_id == uuid.UUID(case_id)).order_by(HpfSite.seq))] == [1, 1, 1]
    db.close()


def test_review_of_an_unknown_candidate_is_candidate_not_found(setup_test_case):
    res = post("review", {"case_id": setup_test_case, "candidate_id": "m_9999", "review_label": "mitosis"})
    assert res.status_code == 404 and res.json()["error"] == "candidate_not_found"


def lock(case_id, status="confirmed"):
    db = TestingSessionLocal()
    db.scalars(select(StageExecution).where(StageExecution.case_id == uuid.UUID(case_id))).one().status = status
    db.commit()
    db.close()


@pytest.mark.parametrize("path,body", [
    ("review", {"candidate_id": "m_0001", "review_label": "not_mitosis"}),
    ("add", {"centroid_um": [1000.0, 1000.0]}),
])
def test_edits_on_a_confirmed_stage_are_stage_locked(setup_test_case, path, body):
    lock(setup_test_case)
    res = post(path, {"case_id": setup_test_case, **body})
    assert res.status_code == 409 and res.json()["error"] == "stage_locked"


def test_add_outside_the_slide_is_out_of_bounds(setup_test_case):
    res = post("add", {"case_id": setup_test_case, "centroid_um": [6000.0, 100.0]})
    assert res.status_code == 422 and res.json()["error"] == "out_of_bounds"


def test_add_on_an_existing_candidate_labels_it_instead_of_adding_a_second_row(setup_test_case):
    case_id = setup_test_case
    data = post("add", {"case_id": case_id, "centroid_um": [3012.0, 3011.0]}).json()
    assert len(data["candidates"]) == 4
    m3 = next(c for c in data["candidates"] if c["id"] == "m_0003")
    assert m3["review_label"] == "mitosis" and m3["counted"] is True


def test_confirm_needs_every_equivocal_candidate_in_an_hpf_reviewed(setup_test_case):
    case_id = setup_test_case
    res = post("confirm", {"case_id": case_id})
    assert res.status_code == 409
    assert res.json()["error"] == "equivocal_unreviewed" and res.json()["ids"] == ["m_0003"]  # m_0004 is outside the HPFs

    post("review", {"case_id": case_id, "candidate_id": "m_0003", "review_label": "not_mitosis"})
    res = post("confirm", {"case_id": case_id})
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "confirmed", "next_stage": "grading"}


def test_confirm_when_not_awaiting_review(setup_test_case):
    lock(setup_test_case, "running")
    res = post("confirm", {"case_id": setup_test_case})
    assert res.status_code == 409 and res.json()["error"] == "not_awaiting_review"


def test_confirm_mitosis_does_not_clobber_completed_grading(setup_test_case):
    case_id = setup_test_case
    case_uid = uuid.UUID(case_id)
    db = TestingSessionLocal()
    db.add(StageExecution(case_id=case_uid, stage="grading", attempt=1, status="done"))
    db.commit()
    db.close()
    post("review", {"case_id": case_id, "candidate_id": "m_0003", "review_label": "not_mitosis"})

    assert post("confirm", {"case_id": case_id}).status_code == 200
    db = TestingSessionLocal()
    g_exec = db.scalars(select(StageExecution).where(StageExecution.case_id == case_uid, StageExecution.stage == "grading")).first()
    assert g_exec.status == "done"  # Preserved, not clobbered to queued
    db.close()


@pytest.mark.parametrize("path", ["recompute", "add_candidate", "bulk_action", "re_place_hpfs", "replace-hpfs"])
def test_the_v5_routes_are_gone(setup_test_case, path):
    assert post(path, {"case_id": setup_test_case}).status_code in (404, 405)


def test_candidate_images_are_served_when_stored_and_404_otherwise(setup_test_case):
    case_id = setup_test_case
    res = client.get(f"/api/v1/stages/mitosis/{case_id}/candidates/m_0001/crop", headers=HEADERS)
    assert res.status_code == 404 and res.json()["error"] == "image_not_found"
    upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/mitosis/crops/m_0001_context.png", b"\x89PNG-context", "image/png")
    res = client.get(f"/api/v1/stages/mitosis/{case_id}/candidates/m_0001/context", headers=HEADERS)
    assert res.status_code == 200 and res.content == b"\x89PNG-context" and res.headers["content-type"] == "image/png"
    assert client.get(f"/api/v1/stages/mitosis/{case_id}/candidates/m_0001/other", headers=HEADERS).status_code == 422


def test_a_counted_candidate_outside_every_circle_has_no_hpf_and_is_not_in_the_total(setup_test_case):
    case_id = setup_test_case
    db = TestingSessionLocal()
    db.get(Detection, ("m_0004", uuid.UUID(case_id))).final_decision = "mitosis"  # now counted, at (4500, 300): outside the circles
    db.commit()
    db.close()
    data = get(case_id)
    m4 = next(c for c in data["candidates"] if c["id"] == "m_0004")
    assert m4["counted"] is True and m4["hpf_seq"] is None
    assert data["summary"]["count_total"] == 2  # m_0001 and m_0002 only
