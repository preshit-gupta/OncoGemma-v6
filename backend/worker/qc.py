import os
import json
import shutil
import tempfile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.gcs import (
    parse_gcs_uri,
    upload_blob_from_bytes,
    download_blob_to_filename,
    resolve_slide_raw_uri
)
from app.core.stain_profiles import latest_stain_profile
from app.core.tissue_mask_store import load_tissue_mask
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.audit import AuditEvent
from pipeline.qc_checks import run_all_qc_checks
from pipeline.slide_io import SlideReader, require_mpp, seed_from_checksum
from worker.runtime import StageRuntime

def run_qc(stage_execution: StageExecution, session: Session, runtime: StageRuntime) -> tuple[str, dict]:
    """
    QC worker handler:
    1. Loads the case's registered tissue mask and the slide's stain profile from preprocess (nothing is re-fitted).
    2. Downloads slide directly from GCS.
    3. Runs the QC check suite with the injected ``configs/qc.yaml`` and specimen-profile thresholds.
    4. Evaluates overall verdict ('pass', 'warn', 'fail').
    5. Uploads qc/output.json directly to GCS.
    6. Updates stage status.
    """
    input_ref = stage_execution.input_ref or {}
    slide_id = input_ref.get("slide_id")
    case_id = stage_execution.case_id

    if not slide_id:
        slide_obj = session.scalars(select(Slide).where(Slide.case_id == case_id)).first()
        if slide_obj:
            slide_id = str(slide_obj.id)

    if not slide_id:
        raise ValueError(f"Slide not found for case {case_id}")

    slide_obj = session.get(Slide, str(slide_id))
    if not slide_obj:
        raise ValueError(f"Slide object {slide_id} not found in database")

    # Halt QC stage if MPP is missing per PRD 01-stage-v4.0 §2.3 step 4
    require_mpp(slide_obj)

    case_obj = session.get(Case, case_id)
    if not case_obj:
        raise ValueError(f"Case {case_id} not found in database")
    specimen_qc = runtime.config.specimen_profiles.for_type(case_obj.specimen_type).qc
    seed = seed_from_checksum(slide_obj.checksum_sha256)
    mask = load_tissue_mask(case_id)
    stain_profile = latest_stain_profile(session, slide_obj.id)

    scratch_dir = tempfile.mkdtemp(prefix="og_qc_")
    reader = None

    try:
        gcs_uri_original = resolve_slide_raw_uri(case_id, slide_obj) or slide_obj.gcs_uri_original or f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/{slide_id}.svs"
        raw_bucket_name, blob_name = parse_gcs_uri(gcs_uri_original)

        ext = os.path.splitext(blob_name)[1] or ".svs"
        local_slide_path = os.path.join(scratch_dir, f"slide{ext}")

        # Download directly from GCS raw bucket to transient scratch file
        download_blob_to_filename(raw_bucket_name, blob_name, local_slide_path)

        if not os.path.exists(local_slide_path):
            raise FileNotFoundError(f"Raw slide file not found in GCS for QC stage in case {case_id}")

        reader = SlideReader.from_slide_row(local_slide_path, slide_obj)

        # Execute the QC check suite (PRD 02 §3.1, SPEC-04 §3.7)
        qc_result = run_all_qc_checks(
            reader,
            mask,
            stain_profile,
            config=runtime.config.qc,
            specimen_qc=specimen_qc,
            seed=seed,
            config_hash=runtime.ctx.config_hash
        )
        qc_result["specimen_type"] = case_obj.specimen_type

        verdict = qc_result["verdict"]

        # Persist qc/output.json directly to GCS artifacts bucket
        output_ref = f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case_id}/qc/output.json"
        upload_blob_from_bytes(
            settings.GCS_ARTIFACTS_BUCKET,
            f"cases/{case_id}/qc/output.json",
            json.dumps(qc_result, indent=2).encode("utf-8"),
            "application/json"
        )

        # Update stage status & case status based on QC verdict
        if verdict == "pass":
            stage_execution.status = "done"

            # Auto-advance to triage stage (attempt 1 or monotonic next attempt) per PRD 02 §3.3
            stmt_triage = (
                select(StageExecution)
                .where(
                    StageExecution.case_id == case_id,
                    StageExecution.stage == "triage"
                )
                .order_by(StageExecution.attempt.desc())
            )
            existing_triage = session.scalars(stmt_triage).first()
            next_triage_attempt = (existing_triage.attempt + 1) if existing_triage else 1

            next_triage_stage = StageExecution(
                case_id=case_id,
                stage="triage",
                attempt=next_triage_attempt,
                status="queued",
                input_ref={"slide_id": str(slide_id), "qc_output_ref": output_ref}
            )
            session.add(next_triage_stage)
            session.commit()
            session.refresh(next_triage_stage)

            from app.core.cloud_tasks import dispatch_stage_task
            dispatch_stage_task(
                case_id=str(case_id),
                stage="triage",
                stage_exec_id=str(next_triage_stage.id),
                payload={"slide_id": str(slide_id), "qc_output_ref": output_ref}
            )
        elif verdict == "warn":
            stage_execution.status = "awaiting_review"
        elif verdict == "fail":
            stage_execution.status = "failed"
            stage_execution.error = f"QC Hard Failure: {[c['message'] for c in qc_result['checks'] if c['status'] == 'fail']}"
            case_obj.status = "needs_rescan"

        # Emit audit event
        audit = AuditEvent(
            case_id=str(case_id),
            actor="worker_qc",
            event_type="stage_output",
            stage="qc",
            payload={
                "verdict": verdict,
                "config_hash": qc_result["config_hash"],
                "failed_checks": [c["name"] for c in qc_result["checks"] if c["status"] == "fail"]
            }
        )
        session.add(audit)
        session.commit()

        return output_ref, {"opencv": "4.13.0"}

    finally:
        if reader is not None:
            reader.close()
        shutil.rmtree(scratch_dir, ignore_errors=True)
