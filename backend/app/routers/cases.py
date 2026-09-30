import uuid
from io import BytesIO
from datetime import datetime, timezone
from PIL import Image
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Response
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.core.db import get_db
from app.auth.deps import CurrentUser, require
from app.auth.idempotency import IdempotencyContext, IdempotentRoute, idempotent
from app.core.config import settings
from app.core.pipeline_config import get_pipeline_config
from app.core.gcs import (
    upload_blob_from_file,
    generate_signed_upload_url,
    signed_upload_headers,
    get_gcs_tile_template_url,
    parse_gcs_uri,
    blob_exists,
    ALLOWED_WSI_EXTS
)
from starlette.concurrency import run_in_threadpool
from google.api_core.exceptions import NotFound
from app.core.slide_access import open_case_slide
from app.core.cloud_tasks import dispatch_stage_task
from pipeline.errors import MissingMppError, SlideReadError
from pipeline.slide_io import read_region_at_mpp
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.hotspot import Hotspot
from app.models.detection import Detection
from app.models.hpf_site import HpfSite
from app.models.grading import Grading
from app.models.audit import AuditEvent
from app.core.rehydrate import rehydrate_case_from_gcs
from app.services import stages as stage_service
from app.schemas.case import (
    CaseCreate,
    CaseResponse,
    SpecimenTypeUpdateRequest,
    SlideUploadUrlRequest,
    SlideUploadUrlResponse,
    SlideFinalizeRequest,
    SlideMppUpdateRequest,
    CaseDetailResponse,
    ApproveStageRequest
)

# Longer side of the case-list thumbnail, in pixels.
THUMBNAIL_PX = 256

router = APIRouter(prefix="/api/v1/cases", tags=["cases"], route_class=IdempotentRoute)
test_router = APIRouter(prefix="/api/v1/cases", tags=["cases"])

@router.post("", response_model=CaseResponse, status_code=status.HTTP_201_CREATED)
def create_case(
    payload: CaseCreate | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("case:create"))
):
    specimen_type = payload.specimen_type if payload and payload.specimen_type else "unknown"
    case_obj = Case(created_by=user.id, specimen_type=specimen_type)
    db.add(case_obj)
    db.commit()
    db.refresh(case_obj)

    audit = AuditEvent(
        case_id=str(case_obj.id),
        actor=user.id,
        event_type="case_created",
        payload={"created_by": user.id, "specimen_type": specimen_type}
    )
    db.add(audit)
    db.commit()

    return case_obj

@router.get("", response_model=list[CaseResponse])
def list_cases(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("case:read"))
):
    stmt = select(Case).where(Case.deleted_at.is_(None)).order_by(Case.created_at.desc())
    cases = db.scalars(stmt).all()
    if not cases:
        try:
            from app.core.gcs import get_gcs_client
            client = get_gcs_client()
            bucket = client.bucket(settings.GCS_ARTIFACTS_BUCKET)
            blobs = bucket.list_blobs(prefix="cases/", delimiter="/")
            list(blobs)
            for prefix in getattr(blobs, "prefixes", []):
                parts = prefix.strip("/").split("/")
                if len(parts) >= 2:
                    rehydrate_case_from_gcs(parts[1], db)
        except Exception as e:
            print(f"[List Cases Rehydrate Note] {e}")
        cases = db.scalars(stmt).all()
    return cases

def delete_single_case_data(case_id: uuid.UUID, db: Session):
    """
    Safely delete a case and all associated child entities in strict dependency order,
    preventing any foreign key constraint violations, and cleans up associated GCS storage artifacts.
    """
    case_str = str(case_id)

    # 1. Delete leaf entities (mitosis detections and virtual HPF sites)
    db.query(Detection).filter(Detection.case_id == case_id).delete(synchronize_session=False)
    db.query(HpfSite).filter(HpfSite.case_id == case_id).delete(synchronize_session=False)

    # 2. Delete Hotspots (which reference stage_executions and cases)
    db.query(Hotspot).filter(Hotspot.case_id == case_id).delete(synchronize_session=False)

    # 3. Delete StageExecutions
    db.query(StageExecution).filter(StageExecution.case_id == case_id).delete(synchronize_session=False)

    # 4. Delete Grading
    db.query(Grading).filter(Grading.case_id == case_id).delete(synchronize_session=False)

    # 5. Delete Slide records
    db.query(Slide).filter(Slide.case_id == case_id).delete(synchronize_session=False)

    # 6. Delete Case record (AuditEvents are append-only permanent records and are preserved)
    db.query(Case).filter(Case.id == case_id).delete(synchronize_session=False)
    db.commit()

    # 8. Clean up GCS artifacts and raw slide blobs
    try:
        from app.core.gcs import get_gcs_client
        client = get_gcs_client()
        for bname in [settings.GCS_ARTIFACTS_BUCKET, settings.GCS_RAW_BUCKET, settings.GCS_PYRAMIDS_BUCKET]:
            try:
                bucket = client.bucket(bname)
                blobs = list(bucket.list_blobs(prefix=f"cases/{case_str}/"))
                if blobs:
                    bucket.delete_blobs(blobs)
                    print(f"[Delete Case GCS] Deleted {len(blobs)} blobs from {bname} for case {case_str}")
            except Exception as b_err:
                print(f"[Delete Case GCS Note] Could not delete blobs from {bname}: {b_err}")
    except Exception as gcs_err:
        print(f"[Delete Case GCS Exception] {gcs_err}")


@test_router.delete("", status_code=status.HTTP_200_OK)
def clear_all_cases(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("case:delete"))
):
    """Clear all diagnostic cases, associated relational child data, and storage artifacts (test-only, requires case:delete)."""
    cases = db.scalars(select(Case)).all()
    count = len(cases)
    for c in cases:
        delete_single_case_data(c.id, db)

    return {"status": "cleared", "deleted_count": count}


@router.delete("/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_case(
    case_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("case:delete"))
):
    """Soft-delete a single diagnostic case and emit an audit event (SPEC-03 §5.3.1)."""
    case_obj = db.get(Case, case_id)
    if not case_obj or case_obj.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Case not found")

    case_obj.deleted_at = datetime.now(timezone.utc)
    audit = AuditEvent(
        case_id=str(case_id),
        actor=user.id,
        event_type="case_deleted",
        payload={"soft": True, "deleted_at": case_obj.deleted_at.isoformat()}
    )
    db.add(audit)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{case_id}/slide/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_slide_file(
    case_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("slide:upload"))
):
    """Direct file upload endpoint streaming directly to GCS bucket with zero local persistence."""
    case_obj = db.get(Case, case_id)
    if not case_obj:
        raise HTTPException(status_code=404, detail="Case not found")

    file_uuid = uuid.uuid4()
    ext = file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else "svs"
    allowed_exts = {"svs", "ndpi", "tif", "tiff", "mrxs", "scn", "bcf", "jpg", "jpeg", "png"}
    if ext not in allowed_exts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file extension .{ext}. Allowed formats: {', '.join(sorted(allowed_exts))}"
        )
    
    blob_name = f"cases/{case_id}/{file_uuid}.{ext}"
    gcs_uri = f"gs://{settings.GCS_RAW_BUCKET}/{blob_name}"

    try:
        await file.seek(0)
        await run_in_threadpool(
            upload_blob_from_file,
            bucket_name=settings.GCS_RAW_BUCKET,
            blob_name=blob_name,
            file_obj=file.file,
            content_type=file.content_type or "application/octet-stream"
        )
        print(f"[Upload Success] Directly uploaded slide to {gcs_uri}")
    except Exception as save_err:
        print(f"[Upload Error] GCS bucket upload failed: {save_err}")
        raise HTTPException(status_code=500, detail=f"GCS bucket upload failed: {str(save_err)}")

    # Create slide record
    slide_obj = Slide(
        case_id=case_id,
        gcs_uri_original=gcs_uri
    )
    db.add(slide_obj)
    db.flush()
    
    # Queue 'ingest' stage_execution (monotonic attempt tracking - Issue #69)
    stmt_ingest = (
        select(StageExecution)
        .where(StageExecution.case_id == case_id, StageExecution.stage == "ingest")
        .order_by(StageExecution.attempt.desc())
    )
    existing_ingest = db.scalars(stmt_ingest).first()
    next_attempt = (existing_ingest.attempt + 1) if existing_ingest else 1

    stage_exec = StageExecution(
        case_id=case_id,
        stage="ingest",
        attempt=next_attempt,
        status="queued",
        input_ref={
            "gcs_uri_original": gcs_uri,
            "slide_id": str(slide_obj.id),
            "blob_name": blob_name
        }
    )
    db.add(stage_exec)
    case_obj.status = "open"
    
    # Audit event
    audit = AuditEvent(
        case_id=str(case_id),
        actor=user.id,
        event_type="slide_uploaded",
        stage="ingest",
        payload={"gcs_uri": gcs_uri, "slide_id": str(slide_obj.id), "filename": file.filename, "attempt": next_attempt}
    )
    db.add(audit)
    
    db.commit()

    dispatch_stage_task(
        case_id=str(case_id),
        stage="ingest",
        stage_exec_id=str(stage_exec.id),
        payload={"gcs_uri": gcs_uri, "slide_id": str(slide_obj.id)}
    )

    return {
        "status": "queued",
        "slide_id": str(slide_obj.id),
        "stage_execution_id": str(stage_exec.id),
        "gcs_uri": gcs_uri,
        "attempt": next_attempt
    }

@router.post("/{case_id}/stages/{stage_name}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_case_stage(
    case_id: uuid.UUID,
    stage_name: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("stage:retry"))
):
    """Re-queue execution attempt for a specific pipeline stage."""
    try:
        new_stage = stage_service.retry_stage(db, case_id, stage_name, user.id)
    except stage_service.StageServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {
        "status": "queued",
        "stage_execution_id": str(new_stage.id),
        "attempt": new_stage.attempt
    }

@router.post("/{case_id}/stages/{stage_name}/approve", status_code=status.HTTP_202_ACCEPTED)
def approve_case_stage(
    case_id: uuid.UUID,
    stage_name: str,
    req: ApproveStageRequest | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("stage:confirm")),
    _idempotency: IdempotencyContext = idempotent("cases/stages/approve"),
):
    """Confirm a stage and queue the next one, through the same service as the stage confirm endpoints."""
    try:
        result = stage_service.confirm_stage(
            db, case_id, stage_name, user.id,
            override_justification=req.override_justification if req else None,
        )
    except stage_service.StageServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {
        "status": "approved",
        "approved_stage": stage_name,
        "next_stage": result.next_stage,
        "next_stage_execution_id": str(result.next_execution.id) if result.next_execution else None
    }

@router.post("/{case_id}/slide/upload-url", response_model=SlideUploadUrlResponse)
def get_slide_upload_url(
    case_id: uuid.UUID,
    req: SlideUploadUrlRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("slide:upload"))
):
    case_obj = db.get(Case, case_id)
    if not case_obj:
        raise HTTPException(status_code=404, detail="Case not found")

    upload_cfg = get_pipeline_config().safety.signed_upload
    if req.size_bytes > upload_cfg.max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File size {req.size_bytes} exceeds maximum allowed size of {upload_cfg.max_bytes} bytes"
        )
    if req.content_type not in upload_cfg.allowed_wsi_mimes:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported content type '{req.content_type}'. Allowed: {upload_cfg.allowed_wsi_mimes}"
        )

    file_uuid = uuid.uuid4()
    ext = req.filename.rsplit(".", 1)[-1].lower() if "." in req.filename else "svs"
    if f".{ext}" not in ALLOWED_WSI_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported WSI file extension '.{ext}'. Allowed: {sorted(list(ALLOWED_WSI_EXTS))}"
        )
    
    blob_name = f"cases/{case_id}/{file_uuid}.{ext}"
    gcs_uri = f"gs://{settings.GCS_RAW_BUCKET}/{blob_name}"
    upload_url = generate_signed_upload_url(settings.GCS_RAW_BUCKET, blob_name, content_type=req.content_type)

    return SlideUploadUrlResponse(
        upload_url=upload_url,
        gcs_uri=gcs_uri,
        upload_headers=signed_upload_headers(req.content_type),
    )

@router.post("/{case_id}/slide/finalize", status_code=status.HTTP_202_ACCEPTED)
def finalize_slide_upload(
    case_id: uuid.UUID,
    req: SlideFinalizeRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("slide:upload"))
):
    case_obj = db.get(Case, case_id)
    if not case_obj:
        raise HTTPException(status_code=404, detail="Case not found")

    expected_prefix = f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/"
    if not req.gcs_uri.startswith(expected_prefix):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid gcs_uri. Slide URI must be under {expected_prefix}"
        )

    raw_bucket_name, blob_name = parse_gcs_uri(req.gcs_uri)
    if not blob_exists(raw_bucket_name, blob_name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Raw slide object does not exist at {req.gcs_uri}"
        )

    slide_obj = Slide(
        case_id=case_id,
        gcs_uri_original=req.gcs_uri,
        checksum_sha256=req.client_sha256
    )
    db.add(slide_obj)
    db.flush()
    
    # Queue 'ingest' stage_execution (monotonic attempt tracking - Issue #69)
    stmt_ingest = (
        select(StageExecution)
        .where(StageExecution.case_id == case_id, StageExecution.stage == "ingest")
        .order_by(StageExecution.attempt.desc())
    )
    existing_ingest = db.scalars(stmt_ingest).first()
    next_attempt = (existing_ingest.attempt + 1) if existing_ingest else 1

    stage_exec = StageExecution(
        case_id=case_id,
        stage="ingest",
        attempt=next_attempt,
        status="queued",
        input_ref={
            "gcs_uri_original": req.gcs_uri,
            "slide_id": str(slide_obj.id),
            "client_sha256": req.client_sha256
        }
    )
    db.add(stage_exec)
    case_obj.status = "open"
    
    audit = AuditEvent(
        case_id=str(case_id),
        actor=user.id,
        event_type="slide_uploaded",
        stage="ingest",
        payload={"gcs_uri": req.gcs_uri, "slide_id": str(slide_obj.id), "attempt": next_attempt}
    )
    db.add(audit)
    
    db.commit()
    db.refresh(slide_obj)
    db.refresh(stage_exec)

    dispatch_stage_task(
        case_id=str(case_id),
        stage="ingest",
        stage_exec_id=str(stage_exec.id),
        payload={"gcs_uri_original": req.gcs_uri, "slide_id": str(slide_obj.id)}
    )

    return {
        "status": "queued",
        "slide_id": str(slide_obj.id),
        "stage_execution_id": str(stage_exec.id),
        "attempt": next_attempt
    }

@router.get("/{case_id}/thumbnail")
def get_case_thumbnail(
    case_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("case:read"))
):
    """
    Returns a high-speed whole-slide macro thumbnail (e.g. 256x256) of the case biopsy directly from GCS.
    """
    stmt = select(Slide).where(Slide.case_id == case_id).limit(1)
    slide_obj = db.scalars(stmt).first()
    if not slide_obj:
        raise HTTPException(status_code=404, detail="Slide not found")

    gcs_uri = slide_obj.gcs_uri_original
    if not gcs_uri:
        raise HTTPException(status_code=404, detail="Slide GCS URI not set")

    # The whole extent at the resolution that puts its longer side at THUMBNAIL_PX. A slide that
    # cannot be read has no thumbnail; a grey square is never drawn in its place (SPEC-01 §3.9).
    try:
        with open_case_slide(case_id, slide_obj) as reader:
            extent_w, extent_h = reader.extent_um()
            region = read_region_at_mpp(reader, 0.0, 0.0, extent_w, extent_h, max(extent_w, extent_h) / THUMBNAIL_PX)
    except MissingMppError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (SlideReadError, NotFound, OSError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Slide could not be read: {exc}") from exc

    buf = BytesIO()
    Image.fromarray(region.rgb).save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@router.get("/{case_id}", response_model=CaseDetailResponse)
def get_case_detail(
    case_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("case:read"))
):
    case_obj = db.get(Case, case_id)
    if not case_obj:
        case_obj = rehydrate_case_from_gcs(str(case_id), db)
    if not case_obj or case_obj.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Case not found")

    slides = db.scalars(select(Slide).where(Slide.case_id == case_id)).all()
    stages = db.scalars(
        select(StageExecution)
        .where(StageExecution.case_id == case_id)
        .order_by(StageExecution.attempt.desc(), StageExecution.started_at.desc())
    ).all()

    slides_data = [
        {
            "id": str(s.id),
            "status": getattr(s, "status", "ready") or "ready",
            "gcs_uri_original": s.gcs_uri_original,
            "gcs_uri_pyramid": s.gcs_uri_pyramid,
            "format": s.format,
            "scanner": s.scanner,
            "mpp_x": s.mpp_x,
            "mpp_y": s.mpp_y,
            "base_mag": s.base_mag,
            "width_px": s.width_px,
            "height_px": s.height_px,
            "checksum_sha256": s.checksum_sha256,
            "label_stripped_at": s.label_stripped_at.isoformat() if s.label_stripped_at else None
        }
        for s in slides
    ]

    stages_data = [
        {
            "id": str(st.id),
            "stage": st.stage,
            "attempt": st.attempt,
            "status": st.status,
            "output_ref": st.output_ref,
            "error": st.error,
            "started_at": st.started_at.isoformat() if st.started_at else None,
            "completed_at": st.completed_at.isoformat() if st.completed_at else None
        }
        for st in stages
    ]

    primary_slide_id = str(slides[0].id) if slides else None
    tile_template = None
    if primary_slide_id:
        tile_template = get_gcs_tile_template_url(primary_slide_id, "{layer}")

    return CaseDetailResponse(
        id=case_obj.id,
        created_by=case_obj.created_by,
        status=case_obj.status,
        specimen_type=case_obj.specimen_type,
        created_at=case_obj.created_at,
        slides=slides_data,
        stages=stages_data,
        tile_url_template=tile_template,
        cdn_base_url=settings.CDN_BASE_URL
    )


@router.patch("/{case_id}/specimen-type", status_code=status.HTTP_200_OK)
def update_case_specimen_type(
    case_id: uuid.UUID,
    req: SpecimenTypeUpdateRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("case:create"))
):
    """
    State whether the case's specimen is a resection or a core biopsy (SPEC-04 §3.2).
    Preprocess refuses an 'unknown' specimen; retry it (stages/preprocess/retry) after setting this.
    """
    case_obj = db.get(Case, case_id)
    if not case_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")

    previous = case_obj.specimen_type
    case_obj.specimen_type = req.specimen_type
    db.add(AuditEvent(
        case_id=str(case_id),
        actor=user.id,
        event_type="specimen_type_set",
        payload={"from": previous, "to": req.specimen_type}
    ))
    db.commit()
    return {"case_id": str(case_id), "specimen_type": case_obj.specimen_type}


@router.patch("/{case_id}/slides/{slide_id}/mpp", status_code=status.HTTP_200_OK)
@router.put("/{case_id}/slides/{slide_id}/mpp", status_code=status.HTTP_200_OK)
def update_slide_mpp(
    case_id: uuid.UUID,
    slide_id: uuid.UUID,
    req: SlideMppUpdateRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("slide:set_mpp"))
):
    """
    Allow pathologist or admin to manually provide valid MPP for a slide marked 'needs_mpp'.
    Unblocks downstream processing by setting slide status to 'ready' and chaining preprocess stage.
    """
    if req.mpp_x <= 0 or (req.mpp_y is not None and req.mpp_y <= 0):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MPP values must be positive numbers.")

    slide = db.get(Slide, slide_id)
    if not slide:
        slide = db.scalars(select(Slide).where(Slide.id == str(slide_id))).first()
    if not slide or str(slide.case_id) != str(case_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Slide not found in case.")

    slide.mpp_x = float(req.mpp_x)
    slide.mpp_y = float(req.mpp_y) if req.mpp_y is not None else float(req.mpp_x)
    slide.mpp_source = "manual"
    slide.native_mpp = max(slide.mpp_x, slide.mpp_y)
    slide.status = "ready"
    db.flush()

    # If case status was needs_mpp and all slides now have valid MPP, restore open status
    case_str = str(case_id)
    case_obj = db.get(Case, case_id)
    if not case_obj:
        case_obj = db.scalars(select(Case).where(Case.id == case_str)).first()

    if case_obj and case_obj.status == "needs_mpp":
        case_slides = db.scalars(
            select(Slide).where((Slide.case_id == case_id) | (Slide.case_id == case_str))
        ).all()
        remaining = [s for s in case_slides if s.status == "needs_mpp"]
        if not remaining:
            case_obj.status = "open"

    db.commit()
    db.refresh(slide)

    # If ingest output exists and preprocess is not yet queued, auto-queue preprocess
    existing_prep = db.scalars(
        select(StageExecution).where(
            StageExecution.case_id == case_id,
            StageExecution.stage == "preprocess"
        )
    ).first()

    if not existing_prep:
        output_ref = f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case_id}/ingest_output.json"
        next_prep_stage = stage_service.queue_stage(
            db, case_id, "preprocess",
            input_ref={"slide_id": str(slide.id), "ingest_output_ref": output_ref},
            parent=stage_service.latest_execution(db, case_id, "ingest"),
        )
        db.commit()
        stage_service.dispatch(next_prep_stage, next_prep_stage.input_ref)

    return {
        "slide_id": str(slide.id),
        "status": slide.status,
        "mpp_x": slide.mpp_x,
        "mpp_y": slide.mpp_y
    }


