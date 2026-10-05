"""One stage-confirm path for the API and the harness (SPEC-02 §5.3, §8)."""
import json
import shutil
import uuid
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.db import Base, get_db
from app.core.gcs import upload_blob_from_bytes
from app.core.pipeline_config import get_config_hash
from app.core.run_context import DecisionContext, RunMode
from app.main import app
from app.models.audit import AuditEvent
from app.models.case import Case
from app.models.detection import Detection
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.validation import ValidationRun
from app.services import stages as stage_service
from tests.fakes.runtime import make_runtime
from worker.ingest import run_ingest

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db):
    def override():
        yield db

    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def add_run(db) -> ValidationRun:
    run = ValidationRun(
        name="tcga-val", dataset="tcga_brca_dx", split="val", manifest_uri="gs://m/tcga.parquet",
        manifest_sha256="a" * 64, stages=["ingest", "preprocess", "qc", "triage", "mitosis", "grading"],
        mode="auto", concurrency=2, config_hash=get_config_hash(), registry_sha256="b" * 64,
        splits_lock_sha256="c" * 64, status="running", created_by="researcher@example.org",
    )
    db.add(run)
    db.flush()
    return run


def add_case(db, *, uri: str | None = None) -> tuple[Case, Slide]:
    case = Case(id=uuid.uuid4(), created_by="t", status="open", specimen_type="resection")
    slide = Slide(id=uuid.uuid4(), case_id=case.id, gcs_uri_original=uri or f"gs://raw/cases/{case.id}/s.svs")
    db.add_all([case, slide])
    db.flush()
    return case, slide


def add_execution(db, case, stage, status, *, run=None, attempt=1) -> StageExecution:
    execution = StageExecution(
        case_id=case.id, stage=stage, attempt=attempt, status=status,
        run_mode=RunMode.EVAL.value if run else RunMode.CLINICAL.value, run_id=run.id if run else None,
    )
    db.add(execution)
    db.commit()
    return execution


def write_triage_output(case, hotspots) -> None:
    upload_blob_from_bytes(
        settings.GCS_ARTIFACTS_BUCKET, f"cases/{case.id}/triage/output.json",
        json.dumps({"hotspots": hotspots, "hpf_diameter_um": 500.0, "frame_um": 600.0, "hpf_target": 10}).encode("utf-8"), "application/json",
    )


HOTSPOT = {"id": "hs_1", "center_um": [300.0, 300.0], "polygon_um": [[0, 0], [600, 0], [600, 600], [0, 600], [0, 0]],
           "hpf_diameter_um": 500.0, "window_um": 600.0, "source": "model", "excluded": False}


# --- queueing --------------------------------------------------------------------

def test_queued_stage_inherits_the_run_of_its_parent(db):
    run = add_run(db)
    case, _ = add_case(db)
    parent = add_execution(db, case, "qc", "done", run=run)
    add_execution(db, case, "triage", "failed", run=run)

    queued = stage_service.queue_stage(db, case.id, "triage", parent=parent)

    assert (queued.attempt, queued.status) == (2, "queued")
    assert queued.run_mode == "eval" and str(queued.run_id) == str(run.id)


def test_queued_stage_without_parent_is_clinical(db):
    case, _ = add_case(db)
    queued = stage_service.queue_stage(db, case.id, "ingest")
    assert (queued.attempt, queued.run_mode, queued.run_id) == (1, "clinical", None)


def test_unknown_stage_is_refused(db):
    case, _ = add_case(db)
    with pytest.raises(stage_service.InvalidStage):
        stage_service.queue_stage(db, case.id, "report")


def test_decision_context_carries_the_run_id(db):
    run = add_run(db)
    case, _ = add_case(db)
    execution = add_execution(db, case, "triage", "running", run=run)
    ctx = DecisionContext.for_stage_execution(execution, get_config_hash())
    assert ctx.run_mode is RunMode.EVAL and ctx.run_id == uuid.UUID(str(run.id))


# --- confirm ---------------------------------------------------------------------

def test_harness_confirm_of_triage_queues_mitosis_in_the_same_run(db):
    run = add_run(db)
    case, _ = add_case(db)
    triage = add_execution(db, case, "triage", "awaiting_review", run=run)
    write_triage_output(case, [HOTSPOT])

    result = stage_service.confirm_stage(db, case.id, "triage", f"harness:{run.id}", accept_fewer_hpfs=True)

    db.refresh(triage)
    assert triage.status == "confirmed" and triage.reviewed_by == f"harness:{run.id}"
    assert result.next_stage == "mitosis"
    mitosis = stage_service.latest_execution(db, case.id, "mitosis")
    assert mitosis.run_mode == "eval" and str(mitosis.run_id) == str(run.id)
    events = [e.event_type for e in db.scalars(select(AuditEvent).where(AuditEvent.case_id == str(case.id)))]
    assert events == ["stage_confirmed", "stage_started"]


def test_a_stale_copy_cannot_confirm_a_stage_twice(db):
    """Two controllers on one run (a laptop and an eval worker) both saw triage awaiting review.
    The second confirm reads the locked row afresh and is refused, so mitosis is queued once."""
    run = add_run(db)
    case, _ = add_case(db)
    add_execution(db, case, "triage", "awaiting_review", run=run)
    write_triage_output(case, [HOTSPOT])
    seen = stage_service.latest_execution(db, case.id, "triage")  # db now holds a copy (kept referenced)
    assert seen.status == "awaiting_review"

    other = Session()
    try:
        stage_service.confirm_stage(other, case.id, "triage", "worker", accept_fewer_hpfs=True)
    finally:
        other.close()

    with pytest.raises(stage_service.StageConflict):
        stage_service.confirm_stage(db, case.id, "triage", "laptop", accept_fewer_hpfs=True)
    db.rollback()
    attempts = db.scalars(select(StageExecution.attempt).where(
        StageExecution.case_id == case.id, StageExecution.stage == "mitosis")).all()
    assert attempts == [1]


def test_triage_without_active_hotspots_needs_the_no_tumour_flag(db):
    case, _ = add_case(db)
    add_execution(db, case, "triage", "awaiting_review")
    write_triage_output(case, [{**HOTSPOT, "excluded": True}])

    with pytest.raises(stage_service.ReviewGateError) as refused:
        stage_service.confirm_stage(db, case.id, "triage", "u1")
    assert refused.value.status_code == 422

    result = stage_service.confirm_stage(db, case.id, "triage", "u1", no_invasive_tumor=True)
    assert result.next_stage is None and db.get(Case, case.id).status == "done"


def test_missing_triage_output_aborts_the_confirmation(db):
    case, _ = add_case(db)
    triage = add_execution(db, case, "triage", "awaiting_review")
    with pytest.raises(stage_service.StageOutputUnavailable):
        stage_service.confirm_stage(db, case.id, "triage", "u1")
    db.refresh(triage)
    assert triage.status == "awaiting_review"


def test_grading_is_not_confirmed_without_its_scores(db, client):
    case, _ = add_case(db)
    add_execution(db, case, "grading", "awaiting_review")
    with pytest.raises(stage_service.StageConflict):
        stage_service.confirm_stage(db, case.id, "grading", "u1")
    resp = client.post(f"/api/v1/cases/{case.id}/stages/grading/approve")
    assert resp.status_code == 409 and "/api/v1/stages/grading/confirm" in resp.json()["detail"]
    assert stage_service.latest_execution(db, case.id, "grading").status == "awaiting_review"


def test_approve_applies_the_mitosis_review_gate(db, client):
    """/approve and /stages/mitosis/confirm are one path: neither skips the equivocal-candidate gate (SPEC-06 §5.6)."""
    from app.models.hpf_site import HpfSite

    case, _ = add_case(db)
    add_execution(db, case, "mitosis", "awaiting_review")
    db.add(HpfSite(case_id=case.id, seq=1, center_um=[10.0, 10.0], radius_um=262.0, mitotic_count=0))
    db.add(Detection(id="m_1", case_id=case.id, centroid_um=[10.0, 10.0], p_a=0.9, final_decision="equivocal", decision_path="A"))
    db.commit()

    for resp in (
        client.post(f"/api/v1/cases/{case.id}/stages/mitosis/approve"),
        client.post("/api/v1/stages/mitosis/confirm", json={"case_id": str(case.id)}),
    ):
        assert resp.status_code == 409 and "review label" in resp.json()["detail"]
    assert stage_service.latest_execution(db, case.id, "mitosis").status == "awaiting_review"
    assert stage_service.latest_execution(db, case.id, "grading") is None


def test_mitosis_confirm_queues_grading_in_the_run(db):
    run = add_run(db)
    case, _ = add_case(db)
    add_execution(db, case, "mitosis", "awaiting_review", run=run)
    db.add(Detection(id="m_1", case_id=case.id, centroid_um=[10.0, 10.0], p_a=0.9, final_decision="mitosis", decision_path="A"))
    db.commit()

    result = stage_service.confirm_stage(db, case.id, "mitosis", f"harness:{run.id}")

    assert result.next_execution is not None
    grading = stage_service.latest_execution(db, case.id, "grading")
    assert grading.status == "queued" and str(grading.run_id) == str(run.id)


# --- retry -----------------------------------------------------------------------

def test_retry_adds_an_attempt_in_the_same_run_and_keeps_the_old_one(db):
    run = add_run(db)
    case, _ = add_case(db)
    failed = add_execution(db, case, "mitosis", "failed", run=run)

    retried = stage_service.retry_stage(db, case.id, "mitosis", f"harness:{run.id}")

    assert retried.attempt == 2 and retried.run_mode == "eval" and str(retried.run_id) == str(run.id)
    db.refresh(failed)
    assert failed.status == "failed"


def test_retry_route_uses_the_service(db, client):
    case, _ = add_case(db)
    add_execution(db, case, "qc", "queued")
    resp = client.post(f"/api/v1/cases/{case.id}/stages/qc/retry")
    assert resp.status_code == 409 and "cannot be retried" in resp.json()["detail"]
    assert client.post(f"/api/v1/cases/{case.id}/stages/report/retry").status_code == 400


# --- ingest of a dataset slide ---------------------------------------------------

def write_tiff(path) -> str:
    import tifffile

    with tifffile.TiffWriter(str(path)) as tif:
        tif.write(np.full((64, 64, 3), 150, dtype=np.uint8), description="dataset slide")
    return str(path)


def ingest(db, execution, slide_path, mpp_props=None):
    slide = MagicMock()
    slide.dimensions = (1024, 1024)
    slide.properties = {"openslide.vendor": "aperio", **(mpp_props or {})}
    with patch("app.core.slide_source.download_blob_to_filename", side_effect=lambda b, k, dest: shutil.copyfile(slide_path, dest)), \
         patch("openslide.OpenSlide", return_value=slide), \
         patch("worker.ingest.generate_dzi_pyramid", return_value="pyramid.dzi"), \
         patch("worker.ingest.upload_dzi_tree_to_gcs"), \
         patch("worker.ingest.upload_blob_from_bytes"), \
         patch("worker.ingest.strip_label_and_macro_images", return_value=True) as strip, \
         patch("worker.ingest.upload_blob_from_file") as rewrite:
        run_ingest(execution, db, make_runtime(execution))
    return strip, rewrite


def test_ingest_reads_a_dataset_uri_in_place_and_keeps_the_documented_mpp(db, tmp_path):
    run = add_run(db)
    case, slide = add_case(db, uri="gs://og-datasets/tcga-brca/TCGA-A1.svs")
    slide.mpp_x = slide.mpp_y = 0.2527
    slide.mpp_source = "dataset_doc"
    execution = add_execution(db, case, "ingest", "running", run=run)
    execution.input_ref = {"slide_id": str(slide.id)}
    db.commit()

    strip, rewrite = ingest(db, execution, write_tiff(tmp_path / "s.tif"), {"openslide.mpp-x": "0.5", "openslide.mpp-y": "0.5"})

    strip.assert_not_called()
    rewrite.assert_not_called()
    db.refresh(slide)
    assert (slide.mpp_x, slide.mpp_y, slide.mpp_source, slide.status) == (0.2527, 0.2527, "dataset_doc", "ready")
    preprocess = stage_service.latest_execution(db, case.id, "preprocess")
    assert preprocess.run_mode == "eval" and str(preprocess.run_id) == str(run.id)


def test_ingest_still_de_identifies_app_uploads(db, tmp_path):
    case, slide = add_case(db)
    slide.gcs_uri_original = f"gs://{settings.GCS_RAW_BUCKET}/cases/{case.id}/upload.svs"
    execution = add_execution(db, case, "ingest", "running")
    execution.input_ref = {"slide_id": str(slide.id)}
    db.commit()

    strip, rewrite = ingest(db, execution, write_tiff(tmp_path / "s.tif"), {"openslide.mpp-x": "0.5", "openslide.mpp-y": "0.5"})

    strip.assert_called_once()
    rewrite.assert_called_once()
    db.refresh(slide)
    assert (slide.mpp_x, slide.mpp_source) == (0.5, "file")


def test_ingest_refuses_a_slide_without_a_gs_uri(db):
    case, slide = add_case(db, uri="/local/slide.svs")
    execution = add_execution(db, case, "ingest", "running")
    execution.input_ref = {"slide_id": str(slide.id)}
    db.commit()
    with pytest.raises(ValueError, match="no gs:// source URI"):
        run_ingest(execution, db, make_runtime(execution))
