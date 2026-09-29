import os
import uuid
import tempfile
import numpy as np
from unittest.mock import patch
from PIL import Image
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.core.config import settings
from app.core.db import get_db, Base, engine
from app.core.gcs import upload_blob_from_bytes, get_gcs_client
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.audit import AuditEvent
from worker.ingest import upload_dzi_tree_to_gcs, run_ingest
from worker.qc import run_qc
from tests.fakes.runtime import make_runtime

client = TestClient(app)


@pytest.fixture(autouse=True)
def schema():
    """API startup no longer builds tables (SPEC-01 §3.1); create_all lives in test fixtures."""
    Base.metadata.create_all(bind=engine)


def test_healthz_async_endpoint():
    """Verify /healthz returns 200 and healthy status (Issue #636)."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"


def test_slide_upload_invalid_extension():
    """Verify slide upload rejects non-WSI files with 400 (Issue #202)."""
    case_id = uuid.uuid4()
    with client:
        resp_case = client.post("/api/v1/cases", headers={"X-User-Role": "pathologist"})
        assert resp_case.status_code == 201
        created_id = resp_case.json()["id"]

        # Upload an illegal extension (.exe)
        files = {"file": ("malicious.exe", b"binarycontent", "application/octet-stream")}
        resp_upload = client.post(
            f"/api/v1/cases/{created_id}/slide/upload",
            files=files,
            headers={"X-User-Role": "pathologist"}
        )
        assert resp_upload.status_code == 400
        assert "Unsupported file extension" in resp_upload.json()["detail"]


def test_slide_finalize_cross_case_phi_isolation():
    """Verify slide finalize enforces strict URI prefix and blob existence (Issue #18)."""
    with patch("app.core.cloud_tasks.dispatch_stage_task"):
        with client:
            resp_case = client.post("/api/v1/cases", headers={"X-User-Role": "pathologist"})
            created_id = resp_case.json()["id"]

            # 1. Reject URI from other case
            other_case_id = str(uuid.uuid4())
            bad_uri = f"gs://{settings.GCS_RAW_BUCKET}/cases/{other_case_id}/slide.svs"
            resp_bad = client.post(
                f"/api/v1/cases/{created_id}/slide/finalize",
                json={"gcs_uri": bad_uri},
                headers={"X-User-Role": "pathologist"}
            )
            assert resp_bad.status_code == 400
            assert "Invalid gcs_uri" in resp_bad.json()["detail"]

            # 2. Reject non-existent blob even if under correct case
            fake_uri = f"gs://{settings.GCS_RAW_BUCKET}/cases/{created_id}/non_existent_slide.svs"
            resp_missing = client.post(
                f"/api/v1/cases/{created_id}/slide/finalize",
                json={"gcs_uri": fake_uri},
                headers={"X-User-Role": "pathologist"}
            )
            assert resp_missing.status_code == 404
            assert "Raw slide object does not exist" in resp_missing.json()["detail"]

            # 3. Accept valid URI when blob actually exists
            real_blob_name = f"cases/{created_id}/real_slide.svs"
            upload_blob_from_bytes(settings.GCS_RAW_BUCKET, real_blob_name, b"FAKE_SVS_HEADER", "application/octet-stream")
            valid_uri = f"gs://{settings.GCS_RAW_BUCKET}/{real_blob_name}"

            resp_ok = client.post(
                f"/api/v1/cases/{created_id}/slide/finalize",
                json={"gcs_uri": valid_uri, "client_sha256": "fake_sha256"},
                headers={"X-User-Role": "pathologist"}
            )
            assert resp_ok.status_code == 202
            assert resp_ok.json()["status"] == "queued"


def test_tile_bounds_check_immediate_404():
    """Verify tile requests outside valid level or coordinates return immediate 404 (Issue #200, #636)."""
    with patch("app.core.cloud_tasks.dispatch_stage_task"):
        with client:
            resp_case = client.post("/api/v1/cases", headers={"X-User-Role": "pathologist"})
            case_id = resp_case.json()["id"]

            # Finalize a slide
            blob_name = f"cases/{case_id}/slide_bounds.svs"
            upload_blob_from_bytes(settings.GCS_RAW_BUCKET, blob_name, b"SVS_CONTENT", "application/octet-stream")
            client.post(
                f"/api/v1/cases/{case_id}/slide/finalize",
                json={"gcs_uri": f"gs://{settings.GCS_RAW_BUCKET}/{blob_name}"},
                headers={"X-User-Role": "pathologist"}
            )

            # 1. Level out of bounds (e.g. z = 2000)
            resp_z = client.get(
                f"/api/v1/cases/{case_id}/tiles/orig/2000/0_0.png",
                headers={"X-User-Role": "pathologist"}
            )
            assert resp_z.status_code == 404
            assert "out of bounds" in resp_z.json()["detail"]

            # 2. Coordinates out of bounds (e.g. c = 500, r = 500 at z = 2)
            resp_cr = client.get(
                f"/api/v1/cases/{case_id}/tiles/orig/2/500_500.png",
                headers={"X-User-Role": "pathologist"}
            )
            assert resp_cr.status_code == 404
            assert "out of bounds" in resp_cr.json()["detail"]


def test_qc_pass_auto_chains_triage():
    """Verify QC 'pass' sets status='done' and auto-enqueues triage stage (Issue #47)."""
    from app.core.db import SessionLocal, Base, engine
    from app.core.stain_profiles import save_stain_profile
    from app.core.tissue_mask_store import save_tissue_mask
    from pipeline.stain import StainFit
    from pipeline.tissue_mask import TissueMask
    from tests.fakes.tiff import tissue_rgb, write_pyramid_tiff
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        case = Case(status="open", created_by="pathologist_test", specimen_type="resection")
        db.add(case)
        db.flush()

        slide = Slide(
            case_id=case.id,
            gcs_uri_original=f"gs://{settings.GCS_RAW_BUCKET}/cases/{case.id}/slide.tif",
            mpp_x=0.25,
            mpp_y=0.25,
            width_px=2048,
            height_px=1024,
            checksum_sha256="ab" * 32
        )
        db.add(slide)
        db.flush()

        # A small real pyramidal TIFF in GCS, the tissue mask and the stain profile Stage 2 leaves behind
        with tempfile.TemporaryDirectory() as tmpdir:
            tiff_path = write_pyramid_tiff(
                __import__("pathlib").Path(tmpdir) / "slide.tif", tissue_rgb(2048, 1024, 0.25), 0.25
            )
            upload_blob_from_bytes(settings.GCS_RAW_BUCKET, f"cases/{case.id}/slide.tif", tiff_path.read_bytes(), "image/tiff")
        save_tissue_mask(case.id, TissueMask(np.ones((32, 64), dtype=bool), 8.0))
        save_stain_profile(db, slide.id, StainFit(
            fitter_version="test", reference_id="test@v1", w_src=[[0.65, 0.70, 0.29], [0.07, 0.99, 0.11]],
            maxc_src=[1.0, 0.8], w_tgt=[[0.65, 0.70, 0.29], [0.07, 0.99, 0.11]], maxc_tgt=[1.0, 0.8],
            fit_status="fitted", n_patches=30, mosaic_sha256="0" * 64,
        ))

        qc_stage = StageExecution(
            case_id=case.id,
            stage="qc",
            attempt=1,
            status="running",
            input_ref={"slide_id": str(slide.id)}
        )
        db.add(qc_stage)
        db.commit()

        # Mock run_all_qc_checks to return clean 'pass' verdict
        import worker.qc
        orig_run_qc_checks = worker.qc.run_all_qc_checks
        worker.qc.run_all_qc_checks = lambda *args, **kwargs: {
            "verdict": "pass",
            "checks": [
                {"name": "tissue_coverage", "status": "pass", "metric": 12.0, "message": "Adequate coverage"},
                {"name": "focus", "status": "pass", "metric": 0.0, "message": "Sharp focus"}
            ],
            "config_hash": "testhash123"
        }

        try:
            # Run QC handler
            out_ref, versions = run_qc(qc_stage, db, make_runtime(qc_stage))
        finally:
            worker.qc.run_all_qc_checks = orig_run_qc_checks

        db.refresh(qc_stage)
        assert qc_stage.status == "done"

        # Verify triage stage was automatically queued
        triage_stage = db.scalars(
            select(StageExecution)
            .where(StageExecution.case_id == case.id, StageExecution.stage == "triage")
        ).first()
        assert triage_stage is not None
        assert triage_stage.status == "queued"
        assert triage_stage.attempt == 1
    finally:
        db.close()


def test_approve_confirmed_stage_rejected():
    """Verify approving an already confirmed stage returns 409 Conflict (Issue #673)."""
    with client:
        resp_case = client.post("/api/v1/cases", headers={"X-User-Role": "pathologist"})
        case_id = resp_case.json()["id"]

        from app.core.db import SessionLocal
        db = SessionLocal()
        try:
            slide = Slide(
                case_id=uuid.UUID(case_id),
                gcs_uri_original=f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/slide.svs",
                mpp_x=0.25,
                mpp_y=0.25
            )
            db.add(slide)
            db.flush()

            prep_stage = StageExecution(
                case_id=uuid.UUID(case_id),
                stage="preprocess",
                attempt=1,
                status="confirmed"
            )
            db.add(prep_stage)
            db.commit()
        finally:
            db.close()

        # Approve preprocess when already confirmed
        resp_app = client.post(
            f"/api/v1/cases/{case_id}/stages/preprocess/approve",
            headers={"X-User-Role": "pathologist"}
        )
        assert resp_app.status_code == 409
        assert "already been confirmed" in resp_app.json()["detail"]


def test_qc_fail_override_justification():
    """Verify approving preprocess when QC failed requires >= 10 char override justification (Issue #68)."""
    with client:
        resp_case = client.post("/api/v1/cases", headers={"X-User-Role": "pathologist"})
        case_id = resp_case.json()["id"]

        from app.core.db import SessionLocal
        db = SessionLocal()
        try:
            slide = Slide(
                case_id=uuid.UUID(case_id),
                gcs_uri_original=f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/slide.svs",
                mpp_x=0.25,
                mpp_y=0.25
            )
            db.add(slide)
            prep_stage = StageExecution(
                case_id=uuid.UUID(case_id),
                stage="preprocess",
                attempt=1,
                status="awaiting_review"
            )
            qc_stage = StageExecution(
                case_id=uuid.UUID(case_id),
                stage="qc",
                attempt=1,
                status="failed",
                error="Severe blur detected in tissue region"
            )
            db.add(prep_stage)
            db.add(qc_stage)
            db.commit()
        finally:
            db.close()

        # 1. Approval without justification must fail with 409
        resp_no_just = client.post(
            f"/api/v1/cases/{case_id}/stages/preprocess/approve",
            json={},
            headers={"X-User-Role": "pathologist"}
        )
        assert resp_no_just.status_code == 409
        assert "clinical override justification" in resp_no_just.json()["detail"]

        # 2. Approval with too short justification (< 10 chars) must fail
        resp_short = client.post(
            f"/api/v1/cases/{case_id}/stages/preprocess/approve",
            json={"override_justification": "ignore"},
            headers={"X-User-Role": "pathologist"}
        )
        assert resp_short.status_code == 409

        # 3. Approval with valid clinical justification succeeds and records audit
        resp_valid = client.post(
            f"/api/v1/cases/{case_id}/stages/preprocess/approve",
            json={"override_justification": "Diagnostic tumor focus is clear and adequate for Nottingham evaluation."},
            headers={"X-User-Role": "pathologist"}
        )
        assert resp_valid.status_code == 202
        assert resp_valid.json()["status"] == "approved"
        assert resp_valid.json()["next_stage"] == "triage"


def test_stage_attempt_ordering_latest_first():
    """Verify GET /cases/{id} orders stages with latest attempt first (Issue #205)."""
    with client:
        resp_case = client.post("/api/v1/cases", headers={"X-User-Role": "pathologist"})
        case_id = resp_case.json()["id"]

        from app.core.db import SessionLocal
        db = SessionLocal()
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            s1 = StageExecution(case_id=uuid.UUID(case_id), stage="ingest", attempt=1, status="failed", started_at=now)
            s2 = StageExecution(case_id=uuid.UUID(case_id), stage="ingest", attempt=2, status="running", started_at=now)
            db.add(s1)
            db.add(s2)
            db.commit()
        finally:
            db.close()

        resp_detail = client.get(f"/api/v1/cases/{case_id}", headers={"X-User-Role": "pathologist"})
        assert resp_detail.status_code == 200
        stages = resp_detail.json()["stages"]
        ingest_stages = [s for s in stages if s["stage"] == "ingest"]
        assert len(ingest_stages) == 2
        # First one returned must be the highest attempt (attempt 2)
        assert ingest_stages[0]["attempt"] == 2
        assert ingest_stages[0]["status"] == "running"


def test_upload_dzi_tree_error_propagation_mock():
    """Verify upload_dzi_tree_to_gcs raises RuntimeError if tile uploads fail (Issue #42)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        level_dir = os.path.join(tmpdir, "0")
        os.makedirs(level_dir, exist_ok=True)
        tile_path = os.path.join(level_dir, "0_0.png")
        with open(tile_path, "wb") as f:
            f.write(b"dummy")

        # Mock bucket that fails on upload
        class FailingBlob:
            def upload_from_filename(self, *args, **kwargs):
                raise IOError("Simulated network drop")

        class FailingBucket:
            def blob(self, *args, **kwargs):
                return FailingBlob()

        class FailingClient:
            def bucket(self, *args, **kwargs):
                return FailingBucket()

        import worker.ingest
        orig_get_gcs = worker.ingest.get_gcs_client
        worker.ingest.get_gcs_client = lambda: FailingClient()
        try:
            with pytest.raises(RuntimeError) as exc_info:
                upload_dzi_tree_to_gcs(tmpdir, "mock_slide_id")
            assert "Pyramid upload failed" in str(exc_info.value)
        finally:
            worker.ingest.get_gcs_client = orig_get_gcs
