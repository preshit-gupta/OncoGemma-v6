import os
import io
import json
import uuid
import math
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Literal
from PIL import Image
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.gcs import (
    parse_gcs_uri,
    download_blob_as_bytes,
    download_blob_to_filename,
    upload_blob_from_bytes,
    delete_blob
)
from app.core.db import get_db
from app.auth.deps import CurrentUser, require
from app.auth.idempotency import IdempotencyContext, IdempotentRoute, idempotent
from app.core.pipeline_config import get_pipeline_config
from app.core.slide_access import PRECONDITION_ERRORS, open_case_slide, slide_stain_transform
from app.core.tissue_mask_store import load_tissue_mask
from google.api_core.exceptions import NotFound
from pipeline.errors import SlideReadError, SpecimenTypeRequired, StainError
from pipeline.slide_io import centered_origin_um, normalize_region, read_region_at_mpp
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.hotspot import Hotspot
from app.models.detection import Detection
from app.models.hpf_site import HpfSite
from app.models.audit import AuditEvent
from app.core.rehydrate import rehydrate_case_from_gcs
from app.services import stages as stage_service
from pipeline.hpf import generate_mitosis_density_map, greedy_place_hpfs
from pipeline.scoring import calculate_hpf_mitosis_counts, compute_nottingham_mitotic_score

router = APIRouter(prefix="/api/v1/stages/mitosis", tags=["mitosis"], route_class=IdempotentRoute)

def to_uuid(val: Any) -> uuid.UUID:
    if isinstance(val, uuid.UUID):
        return val
    try:
        return uuid.UUID(str(val))
    except Exception:
        return val


def get_verified_mitosis_stage(
    case_id: str,
    db: Session,
    require_awaiting: bool = False,
    forbid_confirmed: bool = False
) -> Tuple[Case, StageExecution]:
    """
    Validates case existence and retrieves latest mitosis StageExecution.
    Enforces state machine gating (#313, #116).
    """
    case_uid = to_uuid(case_id)
    case_obj = db.scalars(select(Case).where(Case.id == case_uid)).first() if isinstance(case_uid, uuid.UUID) else db.get(Case, case_id)
    if not case_obj:
        case_obj = db.scalars(select(Case).where(Case.id == str(case_id))).first()
    if not case_obj:
        case_obj = rehydrate_case_from_gcs(case_id, db)
    if not case_obj:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    stmt = select(StageExecution).where(
        (StageExecution.case_id == case_uid) | (StageExecution.case_id == str(case_id)),
        StageExecution.stage == "mitosis"
    ).order_by(StageExecution.attempt.desc()).limit(1)
    if require_awaiting or forbid_confirmed:
        # Row lock serialises concurrent state-gated writes (SPEC-03 §5.3.3); SQLite ignores it.
        stmt = stmt.with_for_update()

    stage_exec = db.scalars(stmt).first()
    if not stage_exec:
        rehydrate_case_from_gcs(case_id, db)
        stage_exec = db.scalars(stmt).first()
    if not stage_exec:
        raise HTTPException(status_code=404, detail="Stage 4 (mitosis) not found for this case")

    if require_awaiting and stage_exec.status != "awaiting_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Mitosis stage is in status '{stage_exec.status}', must be 'awaiting_review' to confirm."
        )

    if forbid_confirmed and stage_exec.status == "confirmed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot modify mitosis candidates or HPFs: stage execution is already confirmed."
        )

    return case_obj, stage_exec


def invalidate_hpf_cached_thumbnails(case_id: str):
    """Busts stale HPF thumbnails in GCS when HPFs move (#107)."""
    for s in range(1, 11):
        for m in ("10x", "20x", "40x"):
            for st in ("norm", "orig"):
                blob_name = f"cases/{case_id}/mitosis/hpfs/hpf_{s}_{m}_{st}.png"
                try:
                    delete_blob(settings.GCS_ARTIFACTS_BUCKET, blob_name)
                except Exception:
                    pass


# Pydantic Schemas
class HpfIn(BaseModel):
    seq: int = Field(..., ge=1, le=100)
    center_um: Tuple[float, float]
    radius_um: float = Field(default=262.0, gt=0.0)
    source: Optional[str] = "model"


class RecomputePayload(BaseModel):
    case_id: str
    candidate_labels: Optional[Dict[str, Literal["mitosis", "not_mitosis", "unreviewed"]]] = None
    hpfs: Optional[List[HpfIn]] = None
    audit_toggle: Optional[Dict[str, Any]] = None


class AddCandidatePayload(BaseModel):
    case_id: str
    centroid_um: List[float] # [x, y]
    label: Literal["mitosis", "not_mitosis", "unreviewed"] = "mitosis"


class BulkActionPayload(BaseModel):
    case_id: str
    action: str = "reject_remaining_unreviewed"


class MitosisConfirmPayload(BaseModel):
    case_id: str


@router.get("/{case_id}")
def get_mitosis_stage_data(case_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(require("case:read"))):
    """
    Fetches full Stage 4 payload: candidate mitotic detections, 10 virtual HPFs,
    summary scoring metrics, model versions, and review status.
    """
    case_uid = to_uuid(case_id)
    case_obj = db.scalars(select(Case).where(Case.id == case_uid)).first() if isinstance(case_uid, uuid.UUID) else db.get(Case, case_id)
    if not case_obj:
        case_obj = db.scalars(select(Case).where(Case.id == str(case_id))).first()
    if not case_obj:
        case_obj = rehydrate_case_from_gcs(case_id, db)
    if not case_obj:
        raise HTTPException(status_code=404, detail="Case not found")

    stmt = select(StageExecution).where(
        (StageExecution.case_id == case_uid) | (StageExecution.case_id == str(case_id)),
        StageExecution.stage == "mitosis"
    ).order_by(StageExecution.attempt.desc()).limit(1)

    stage_exec = db.scalars(stmt).first()
    if not stage_exec:
        rehydrate_case_from_gcs(case_id, db)
        stage_exec = db.scalars(stmt).first()
    if not stage_exec:
        raise HTTPException(status_code=404, detail="Stage 4 (mitosis) not found for this case")

    # Fetch detections from DB
    det_rows = db.scalars(
        select(Detection).where((Detection.case_id == case_uid) | (Detection.case_id == str(case_id))).order_by(Detection.det_conf.desc().nulls_last())
    ).all()

    # Fetch HPF sites from DB
    hpf_rows = db.scalars(
        select(HpfSite).where((HpfSite.case_id == case_uid) | (HpfSite.case_id == str(case_id))).order_by(HpfSite.seq.asc())
    ).all()

    candidates = []
    for d in det_rows:
        candidates.append({
            "id": d.id,
            "hotspot_id": d.hotspot_id,
            "centroid_um": d.centroid_um,
            "det_conf": d.det_conf,
            "ver_conf": d.ver_conf,
            "label": d.label,
            "label_source": d.label_source,
            "medgemma_verdict": getattr(d, "medgemma_verdict", None),
            "medgemma_rationale": getattr(d, "medgemma_rationale", None),
            "medgemma_confidence": getattr(d, "medgemma_confidence", None),
            "crop_uri": d.crop_uri,
            "crop_orig_uri": d.crop_orig_uri
        })

    hpfs = []
    for h in hpf_rows:
        hpfs.append({
            "seq": h.seq,
            "center_um": h.center_um,
            "radius_um": h.radius_um,
            "count": h.mitotic_count,
            "source": h.source
        })

    # If DB rows are empty, attempt reading from output.json artifact in GCS
    if not candidates and stage_exec.output_ref:
        try:
            if stage_exec.output_ref.startswith("gs://"):
                b_name, bl_name = parse_gcs_uri(stage_exec.output_ref)
                out_bytes = download_blob_as_bytes(b_name, bl_name)
            else:
                out_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/mitosis/output.json")
            artifact_data = json.loads(out_bytes.decode("utf-8"))
            candidates = artifact_data.get("candidates", [])
            hpfs = artifact_data.get("hpfs", [])
        except Exception as e:
            print(f"[Mitosis Router Note] Could not load GCS output artifact: {e}")

    # Calculate live score summary
    hpfs, total_count = calculate_hpf_mitosis_counts(candidates, hpfs)
    summary = compute_nottingham_mitotic_score(
        count_total=total_count,
        n_hpf=len(hpfs),
        radius_um=None,
        scoring=get_pipeline_config().mitosis.scoring,
        hpfs=hpfs
    )
    _add_v6_candidate_fields(case_id, candidates, hpfs)
    summary["n_equivocal"] = sum(1 for c in candidates if c["final_decision"] == "equivocal" and c["review_label"] is None)
    summary["flags"] = []

    slide_stmt = select(Slide).where(Slide.case_id == case_id).limit(1)
    slide_obj = db.scalars(slide_stmt).first()
    slide_info = {
        "width_px": slide_obj.width_px if slide_obj else 20000,
        "height_px": slide_obj.height_px if slide_obj else 20000,
        "mpp_x": float(slide_obj.mpp_x) if slide_obj and slide_obj.mpp_x else None,
        "mpp_y": float(slide_obj.mpp_y) if slide_obj and slide_obj.mpp_y else None
    }

    return {
        "case_id": case_id,
        "stage_execution_id": str(stage_exec.id),
        "status": stage_exec.status,
        "candidates": candidates,
        "hpfs": hpfs,
        "summary": summary,
        "slide": slide_info,
        # Only what the execution recorded; versions are never reconstructed from settings.
        "model_versions": stage_exec.model_versions or {},
        "reviewed_at": stage_exec.reviewed_at.isoformat() if stage_exec.reviewed_at else None,
        "reviewed_by": stage_exec.reviewed_by
    }


def _add_v6_candidate_fields(case_id: str, candidates: list, hpfs: list) -> None:
    """Adds the mitosis_v6 contract fields the UI reads (p_a, p_b, final_decision, ...).

    The stored rows are still v5 (det_conf/ver_conf/label); v5 keys are kept.
    An "unreviewed" model candidate is shown as equivocal so it lands in the review queue.
    """
    for c in candidates:
        label = c.get("label")
        human = str(c.get("label_source") or "").startswith("pathologist")
        reviewed = label in ("mitosis", "not_mitosis")
        in_hpf = any(
            (c["centroid_um"][0] - h["center_um"][0]) ** 2 + (c["centroid_um"][1] - h["center_um"][1]) ** 2
            <= float(h["radius_um"]) ** 2
            for h in hpfs
        )
        crop = f"/api/v1/stages/mitosis/{case_id}/candidates/{c['id']}/crop"
        c.update({
            "p_a": c.get("det_conf"),
            "p_b": c.get("ver_conf"),
            "vlm": None,
            "in_tumor": True,
            "final_decision": label if reviewed else "equivocal",
            "decision_path": "human" if human else "A",
            "review_label": label if (reviewed and human) else None,
            "counted": label == "mitosis" and in_hpf,
            "crop_url": f"{crop}?stain=norm",
            "context_url": f"{crop}?stain=orig",
        })


def get_cached_slide_path(raw_bucket_name: str, blob_name: str) -> str:
    cache_dir = os.path.join(tempfile.gettempdir(), "oncogemma_slides")
    os.makedirs(cache_dir, exist_ok=True)
    safe_name = blob_name.replace("/", "_")
    target_path = os.path.join(cache_dir, safe_name)
    if not os.path.exists(target_path) or os.path.getsize(target_path) == 0:
        download_blob_to_filename(raw_bucket_name, blob_name, target_path)
    return target_path


@router.get("/{case_id}/candidates/{candidate_id}/crop")
def get_candidate_crop(
    case_id: str,
    candidate_id: str,
    stain: str = Query("norm", pattern="^(norm|orig)$"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("case:read"))
):
    """
    Streams the 128x128 microscopic crop PNG directly from GCS.
    """
    filename = f"{candidate_id}_orig.png" if stain == "orig" else f"{candidate_id}.png"
    blob_name = f"cases/{case_id}/mitosis/crops/{filename}"

    try:
        crop_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, blob_name)
        if len(crop_bytes) > 1000:
            return Response(content=crop_bytes, media_type="image/png", headers={"Cache-Control": "private, max-age=31536000, immutable"})
    except Exception:
        pass

    # Extract authentic optical crop on demand from raw slide
    case_uid = to_uuid(case_id)
    det = db.scalars(
        select(Detection).where(
            (Detection.case_id == case_uid) | (Detection.case_id == str(case_id)),
            Detection.id == candidate_id
        )
    ).first()

    stmt = select(Slide).where((Slide.case_id == case_uid) | (Slide.case_id == str(case_id))).limit(1)
    slide_obj = db.scalars(stmt).first()

    if not slide_obj or not getattr(slide_obj, "mpp_x", None) or slide_obj.mpp_x <= 0 or not getattr(slide_obj, "mpp_y", None) or slide_obj.mpp_y <= 0:
        raise HTTPException(status_code=400, detail="Slide is missing valid MPP (status='needs_mpp'). Cannot extract crop.")

    if not det:
        rehydrate_case_from_gcs(case_id, db)
        det = db.scalars(
            select(Detection).where(
                (Detection.case_id == case_uid) | (Detection.case_id == str(case_id)),
                Detection.id == candidate_id
            )
        ).first()

    cx_um = None
    cy_um = None
    if det and det.centroid_um:
        cx_um, cy_um = det.centroid_um[0], det.centroid_um[1]
    else:
        try:
            out_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/mitosis/output.json")
            out_data = json.loads(out_bytes.decode("utf-8"))
            for c in out_data.get("candidates", []):
                if c.get("id") == candidate_id and "centroid_um" in c:
                    cx_um, cy_um = c["centroid_um"][0], c["centroid_um"][1]
                    break
        except Exception:
            pass

    if cx_um is not None and cy_um is not None:
        if not slide_obj or not getattr(slide_obj, "mpp_x", None) or slide_obj.mpp_x <= 0 or not getattr(slide_obj, "mpp_y", None) or slide_obj.mpp_y <= 0:
            raise HTTPException(status_code=400, detail="Slide is missing valid MPP (status='needs_mpp'). Cannot extract crop.")

        # The crop is what the referee saw: focus_px at focus_mpp around the candidate, in the slide's own
        # colour ("orig") or through its persisted stain profile ("norm"). Padded with white past the edge.
        referee_cfg = get_pipeline_config().mitosis.referee
        focus_um = referee_cfg.focus_px * referee_cfg.focus_mpp
        try:
            stain_transform = slide_stain_transform(db, slide_obj) if stain == "norm" else None
            with open_case_slide(case_id, slide_obj) as reader:
                region = read_region_at_mpp(
                    reader, cx_um - focus_um / 2, cy_um - focus_um / 2, focus_um, focus_um, referee_cfg.focus_mpp,
                    color="raw" if stain_transform is None else "normalized", stain=stain_transform,
                )
            buf = io.BytesIO()
            Image.fromarray(region.rgb).save(buf, format="PNG")
            extracted_crop_bytes = buf.getvalue()

            try:
                upload_blob_from_bytes(
                    settings.GCS_ARTIFACTS_BUCKET,
                    blob_name,
                    extracted_crop_bytes,
                    "image/png"
                )
            except Exception as up_e:
                print(f"[Candidate Crop GCS Cache Note] {up_e}")

            return Response(content=extracted_crop_bytes, media_type="image/png", headers={"Cache-Control": "private, max-age=31536000, immutable"})
        except PRECONDITION_ERRORS as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (SlideReadError, NotFound, OSError) as exc:
            print(f"[Candidate Crop Extraction Error] {exc}")

    raise HTTPException(status_code=404, detail=f"Candidate crop {candidate_id} could not be extracted from authentic slide")


@router.get("/{case_id}/hpfs/{seq}/thumbnail")
def get_hpf_thumbnail(
    case_id: str,
    seq: int,
    mag: str = Query("40x", pattern="^(10x|20x|40x)$"),
    stain: str = Query("norm", pattern="^(norm|orig)$"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("case:read"))
):
    """
    Streams a calibrated high-power microscopic patch centered at the HPF site.
    Supports 10x, 20x, 40x magnifications and norm/orig H&E stain modes.
    """
    hpf_blob = f"cases/{case_id}/mitosis/hpfs/hpf_{seq}_{mag}_{stain}.png"
    try:
        hpf_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, hpf_blob)
        if len(hpf_bytes) > 25000:
            media_type = "image/jpeg" if hpf_bytes.startswith(b"\xff\xd8") else "image/png"
            return Response(content=hpf_bytes, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        pass

    case_uid = to_uuid(case_id)
    hpf_site = db.scalars(
        select(HpfSite).where(HpfSite.case_id == case_uid, HpfSite.seq == seq)
    ).first()
    if not hpf_site:
        hpf_site = db.scalars(
            select(HpfSite).where(HpfSite.case_id == str(case_id), HpfSite.seq == seq)
        ).first()

    stmt = select(Slide).where((Slide.case_id == case_uid) | (Slide.case_id == str(case_id))).limit(1)
    slide_obj = db.scalars(stmt).first()

    if not slide_obj or not getattr(slide_obj, "mpp_x", None) or slide_obj.mpp_x <= 0 or not getattr(slide_obj, "mpp_y", None) or slide_obj.mpp_y <= 0:
        raise HTTPException(status_code=400, detail="Slide is missing valid MPP (status='needs_mpp'). Cannot extract HPF thumbnail.")

    if not hpf_site:
        rehydrate_case_from_gcs(case_id, db)
        hpf_site = db.scalars(
            select(HpfSite).where((HpfSite.case_id == case_uid) | (HpfSite.case_id == str(case_id)), HpfSite.seq == seq)
        ).first()

    cx_um = None
    cy_um = None
    if hpf_site and hpf_site.center_um:
        cx_um, cy_um = hpf_site.center_um[0], hpf_site.center_um[1]
    else:
        try:
            out_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/mitosis/output.json")
            out_data = json.loads(out_bytes.decode("utf-8"))
            for h in out_data.get("hpfs", []):
                if h.get("seq") == seq and "center_um" in h:
                    cx_um, cy_um = h["center_um"][0], h["center_um"][1]
                    break
        except Exception:
            pass

    if cx_um is None or cy_um is None:
        cx_um, cy_um = 1423.8, 2371.9

    if not slide_obj or not getattr(slide_obj, "mpp_x", None) or slide_obj.mpp_x <= 0 or not getattr(slide_obj, "mpp_y", None) or slide_obj.mpp_y <= 0:
        raise HTTPException(status_code=400, detail="Slide is missing valid MPP (status='needs_mpp'). Cannot extract HPF thumbnail.")

    # Review image width calibrated to the frontend HPF reticle canvas (r=236 px -> radius_um=262.0)
    hpf_cfg = get_pipeline_config().mitosis.hpf
    field_size_um = hpf_cfg.review_field_um
    target_dim = hpf_cfg.review_px // {"40x": 1, "20x": 2, "10x": 4}[mag]

    extracted_bytes = None
    media_type = "image/png"

    # Raw WSI extraction from the cached raw slide, through the slide's persisted stain profile for "norm"
    try:
        stain_transform = slide_stain_transform(db, slide_obj) if stain == "norm" else None
        with open_case_slide(case_id, slide_obj) as reader:
            x_um, y_um = centered_origin_um(reader, cx_um, cy_um, field_size_um, field_size_um)
            region = read_region_at_mpp(
                reader, x_um, y_um, field_size_um, field_size_um, field_size_um / target_dim,
                color="raw" if stain_transform is None else "normalized", stain=stain_transform,
            )
        buf = io.BytesIO()
        if mag == "40x":
            Image.fromarray(region.rgb).save(buf, format="JPEG", quality=94)
            media_type = "image/jpeg"
        else:
            Image.fromarray(region.rgb).save(buf, format="PNG")
            media_type = "image/png"
        extracted_bytes = buf.getvalue()
    except PRECONDITION_ERRORS as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (SlideReadError, NotFound, OSError) as exc:
        print(f"[HPF Extraction Error] {exc}")

    if extracted_bytes is None:
        raise HTTPException(status_code=404, detail=f"HPF #{seq} microscopic patch ({mag}, {stain}) could not be extracted from authentic slide")

    # Cache to GCS
    try:
        upload_blob_from_bytes(
            settings.GCS_ARTIFACTS_BUCKET,
            hpf_blob,
            extracted_bytes,
            media_type
        )
    except Exception as up_e:
        print(f"[HPF GCS Cache Note] {up_e}")

    return Response(content=extracted_bytes, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})


def sync_and_persist_hpf_counts(case_id: str, db: Session) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Synchronizes HpfSite.mitotic_count in the database with current candidate detections
    and computes the Nottingham Mitotic Score (#110).
    """
    case_uid = to_uuid(case_id)
    det_rows = db.scalars(
        select(Detection).where((Detection.case_id == case_uid) | (Detection.case_id == str(case_id)))
    ).all()
    hpf_rows = db.scalars(
        select(HpfSite).where((HpfSite.case_id == case_uid) | (HpfSite.case_id == str(case_id))).order_by(HpfSite.seq.asc())
    ).all()

    cand_list = [
        {"id": d.id, "centroid_um": d.centroid_um, "label": d.label}
        for d in det_rows
    ]
    hpf_list = [
        {"seq": h.seq, "center_um": h.center_um, "radius_um": h.radius_um, "count": 0, "source": h.source}
        for h in hpf_rows
    ]

    updated_hpfs, total_count = calculate_hpf_mitosis_counts(cand_list, hpf_list)
    summary = compute_nottingham_mitotic_score(
        count_total=total_count,
        n_hpf=len(updated_hpfs),
        radius_um=None,
        scoring=get_pipeline_config().mitosis.scoring,
        hpfs=updated_hpfs
    )

    for uh in updated_hpfs:
        for hr in hpf_rows:
            if hr.seq == uh["seq"]:
                hr.mitotic_count = uh["count"]
                break

    return updated_hpfs, summary


@router.post("/recompute")
def recompute_scoring(payload: RecomputePayload, db: Session = Depends(get_db), user: CurrentUser = Depends(require("stage:review"))):
    """
    Live Debounced Recomputation Engine (<50ms).
    Accepts candidate label state updates and/or modified HPF coordinates,
    updates DB records, recomputes Nottingham Mitotic Score, and logs audit events.
    """
    case_id = payload.case_id
    case_obj, stage_exec = get_verified_mitosis_stage(case_id, db, forbid_confirmed=True)
    case_uid = case_obj.id

    # Fetch detections from DB
    det_rows = db.scalars(
        select(Detection).where((Detection.case_id == case_uid) | (Detection.case_id == str(case_id)))
    ).all()

    candidates_dict = {d.id: d for d in det_rows}

    # Apply candidate label changes if provided and log audit event + review_edits diff (#114, #592)
    logged_candidate_ids = set()
    review_edits = list(stage_exec.review_edits or [])
    if payload.candidate_labels:
        for cid, new_label in payload.candidate_labels.items():
            if cid in candidates_dict:
                d = candidates_dict[cid]
                if d.label != new_label:
                    old_label = d.label
                    d.label = new_label
                    d.label_source = "pathologist"
                    review_edits.append({
                        "op": "replace",
                        "path": f"/candidates/{cid}/label",
                        "from": old_label,
                        "to": new_label,
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    })
                    audit = AuditEvent(
                        case_id=case_id,
                        actor=user.id,
                        event_type="review_edit",
                        stage="mitosis",
                        payload={
                            "detection_id": cid,
                            "from": old_label,
                            "to": new_label
                        }
                    )
                    db.add(audit)
                    logged_candidate_ids.add(cid)

    # Audit explicit toggle event if not already logged
    if payload.audit_toggle:
        toggle = payload.audit_toggle
        tid = toggle.get("id")
        if tid and tid not in logged_candidate_ids:
            audit = AuditEvent(
                case_id=case_id,
                actor=user.id,
                event_type="review_edit",
                stage="mitosis",
                payload={
                    "detection_id": tid,
                    "from": toggle.get("from"),
                    "to": toggle.get("to")
                }
            )
            db.add(audit)

    stage_exec.review_edits = review_edits

    # Fetch or update HPF sites with server-side validation (#115, #344, #127)
    if payload.hpfs:
        seqs = [h.seq for h in payload.hpfs]
        if len(seqs) != len(set(seqs)):
            raise HTTPException(status_code=422, detail="HPF seq numbers must be unique.")

        # Non-overlap validation in micrometer space
        for i in range(len(payload.hpfs)):
            for j in range(i + 1, len(payload.hpfs)):
                h1 = payload.hpfs[i]
                h2 = payload.hpfs[j]
                dist = math.hypot(h1.center_um[0] - h2.center_um[0], h1.center_um[1] - h2.center_um[1])
                min_sep = (h1.radius_um + h2.radius_um) - 5.0
                if dist < min_sep:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail=f"HPF sites cannot overlap: field #{h1.seq} and field #{h2.seq} are {dist:.1f} µm apart (minimum separation: {min_sep:.1f} µm)."
                    )

        try:
            db.execute(delete(HpfSite).where((HpfSite.case_id == case_uid) | (HpfSite.case_id == str(case_id))))
            for h in payload.hpfs:
                hpf_row = HpfSite(
                    case_id=case_uid,
                    seq=h.seq,
                    center_um=list(h.center_um),
                    radius_um=h.radius_um,
                    mitotic_count=0,
                    source=h.source or "model"
                )
                db.add(hpf_row)
            db.flush()
            invalidate_hpf_cached_thumbnails(case_id)
        except HTTPException:
            raise
        except Exception as he:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to update HPF sites: {he}")

    # Synchronize and persist HPF counts (#110)
    updated_hpfs, summary = sync_and_persist_hpf_counts(case_id, db)
    db.commit()

    return {
        "case_id": case_id,
        "hpfs": updated_hpfs,
        "summary": summary
    }


@router.post("/add_candidate")
def add_pathologist_mitosis(payload: AddCandidatePayload, db: Session = Depends(get_db), user: CurrentUser = Depends(require("stage:review"))):
    """
    Adds a missed mitotic figure pinned directly by the pathologist at 40x coordinates.
    Cuts a 128x128 crop, uploads to GCS (defensively), creates Detection DB record, and returns candidate data.
    """
    case_id = payload.case_id
    case_obj, stage_exec = get_verified_mitosis_stage(case_id, db, forbid_confirmed=True)
    case_uid = case_obj.id

    if not payload.centroid_um or len(payload.centroid_um) != 2:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="centroid_um must be a coordinate pair [x, y].")
    cx_um = float(payload.centroid_um[0])
    cy_um = float(payload.centroid_um[1])

    # Proximity de-duplication check against existing detections (#593)
    existing_dets = db.scalars(
        select(Detection).where((Detection.case_id == case_uid) | (Detection.case_id == str(case_id)))
    ).all()
    for ed in existing_dets:
        if ed.centroid_um and math.hypot(ed.centroid_um[0] - cx_um, ed.centroid_um[1] - cy_um) < 7.5:
            old_label = ed.label
            ed.label = payload.label
            ed.label_source = "pathologist"
            audit = AuditEvent(
                case_id=case_id,
                actor=user.id,
                event_type="review_edit",
                stage="mitosis",
                payload={
                    "detection_id": ed.id,
                    "from": old_label,
                    "to": ed.label,
                    "reason": "proximity_reactivation"
                }
            )
            db.add(audit)
            sync_and_persist_hpf_counts(case_id, db)
            db.commit()
            return {
                "status": "success",
                "candidate": {
                    "id": ed.id,
                    "centroid_um": ed.centroid_um,
                    "det_conf": ed.det_conf or 1.0,
                    "ver_conf": ed.ver_conf or 1.0,
                    "label": ed.label,
                    "label_source": ed.label_source,
                    "crop_uri": ed.crop_uri
                }
            }

    # Unique collision-proof candidate ID (#128)
    new_id = f"m_user_{uuid.uuid4().hex[:8]}"

    # Generate crop via transient scratch dir
    stmt = select(Slide).where((Slide.case_id == case_id) | (Slide.case_id == case_uid)).limit(1)
    slide_obj = db.scalars(stmt).first()
    if slide_obj:
        if not getattr(slide_obj, "mpp_x", None) or not getattr(slide_obj, "mpp_y", None):
            raise HTTPException(status_code=400, detail="Slide is missing valid MPP (status='needs_mpp'). Cannot generate crop.")

    crop_bytes = None  # as scanned
    norm_bytes = None  # through the slide's persisted stain profile, when the slide has a usable one
    if slide_obj:
        # The same crop the referee sees for a model candidate: focus_px at focus_mpp.
        referee_cfg = get_pipeline_config().mitosis.referee
        focus_um = referee_cfg.focus_px * referee_cfg.focus_mpp
        try:
            with open_case_slide(case_id, slide_obj) as reader:
                region = read_region_at_mpp(
                    reader, cx_um - focus_um / 2, cy_um - focus_um / 2, focus_um, focus_um, referee_cfg.focus_mpp
                )
            buf = io.BytesIO()
            Image.fromarray(region.rgb).save(buf, format="PNG")
            crop_bytes = buf.getvalue()
            try:
                stain_transform = slide_stain_transform(db, slide_obj)
            except (StainError, SpecimenTypeRequired):
                stain_transform = None  # the review UI then has no normalised crop for any candidate of this slide
            if stain_transform is not None:
                norm_buf = io.BytesIO()
                Image.fromarray(normalize_region(region, stain_transform).rgb).save(norm_buf, format="PNG")
                norm_bytes = norm_buf.getvalue()
        except (SlideReadError, NotFound, OSError) as e:
            print(f"[add_pathologist_mitosis Error] Slide crop extraction failed: {e}")

    if crop_bytes is None:
        raise HTTPException(status_code=500, detail="Could not extract authentic optical crop from slide.")

    crop_orig_uri = f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case_id}/mitosis/crops/{new_id}_orig.png"
    crop_uri = f"gs://{settings.GCS_ARTIFACTS_BUCKET}/cases/{case_id}/mitosis/crops/{new_id}.png" if norm_bytes else crop_orig_uri

    # Defensively wrapped GCS upload (#399)
    try:
        upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/mitosis/crops/{new_id}_orig.png", crop_bytes, "image/png")
        if norm_bytes:
            upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/mitosis/crops/{new_id}.png", norm_bytes, "image/png")
    except Exception as gcs_err:
        print(f"[add_pathologist_mitosis Note] GCS upload skipped or failed offline ({gcs_err}). Creating Detection row.")

    det = Detection(
        id=new_id,
        case_id=case_uid,
        hotspot_id=None,
        centroid_um=[float(cx_um), float(cy_um)],
        det_conf=1.0,
        ver_conf=1.0,
        label=payload.label,
        label_source="pathologist",
        crop_uri=crop_uri,
        crop_orig_uri=crop_orig_uri
    )
    db.add(det)

    audit = AuditEvent(
        case_id=case_id,
        actor=user.id,
        event_type="mitosis_added",
        stage="mitosis",
        payload={
            "detection_id": new_id,
            "centroid_um": [cx_um, cy_um],
            "label": payload.label
        }
    )
    db.add(audit)

    # Synchronize HpfSite mitotic counts immediately (#110)
    sync_and_persist_hpf_counts(case_id, db)
    db.commit()

    return {
        "status": "success",
        "candidate": {
            "id": new_id,
            "centroid_um": [cx_um, cy_um],
            "det_conf": 1.0,
            "ver_conf": 1.0,
            "label": payload.label,
            "label_source": "pathologist",
            "crop_uri": det.crop_uri
        }
    }


@router.post("/bulk_action")
def bulk_reject_unreviewed(payload: BulkActionPayload, db: Session = Depends(get_db), user: CurrentUser = Depends(require("stage:review"))):
    """
    Bulk action: Accepts all remaining unreviewed candidates as non-mitotic (rejected).
    Logs the action in the audit trail and updates the live Nottingham Mitotic Score.
    """
    case_id = payload.case_id
    case_obj, stage_exec = get_verified_mitosis_stage(case_id, db, forbid_confirmed=True)
    case_uid = case_obj.id

    unreviewed_rows = db.scalars(
        select(Detection).where(
            (Detection.case_id == case_uid) | (Detection.case_id == str(case_id)),
            Detection.label == "unreviewed"
        )
    ).all()

    now_iso = datetime.now(timezone.utc).isoformat()
    review_edits = list(stage_exec.review_edits or [])
    for d in unreviewed_rows:
        d.label = "not_mitosis"
        d.label_source = "pathologist_bulk"
        review_edits.append({
            "op": "replace",
            "path": f"/candidates/{d.id}/label",
            "from": "unreviewed",
            "to": "not_mitosis",
            "timestamp": now_iso
        })

    stage_exec.review_edits = review_edits

    audit = AuditEvent(
        case_id=case_id,
        actor=user.id,
        event_type="bulk_review_edit",
        stage="mitosis",
        payload={
            "action": payload.action,
            "rejected_count": len(unreviewed_rows)
        }
    )
    db.add(audit)

    # Synchronize HpfSite mitotic counts immediately (#110)
    sync_and_persist_hpf_counts(case_id, db)
    db.commit()

    # Return updated stage data
    return get_mitosis_stage_data(case_id, db)


@router.post("/re_place_hpfs")
def re_place_hpfs(payload: BulkActionPayload, db: Session = Depends(get_db), user: CurrentUser = Depends(require("stage:review"))):
    """
    Re-runs the greedy 10-HPF placement algorithm based on currently confirmed mitosis coordinates.
    """
    case_id = payload.case_id
    case_obj, stage_exec = get_verified_mitosis_stage(case_id, db, forbid_confirmed=True)
    case_uid = case_obj.id

    # Fetch slide dimensions and MPP for accurate physical metric
    slide_row = db.scalars(select(Slide).where((Slide.case_id == case_uid) | (Slide.case_id == str(case_id)))).first()
    slide_dims_um = None
    if slide_row:
        if not getattr(slide_row, "mpp_x", None) or not getattr(slide_row, "mpp_y", None):
            raise HTTPException(status_code=400, detail="Slide is missing valid MPP (status='needs_mpp'). Cannot compute HPF scores.")
        mpp_x = float(slide_row.mpp_x)
        mpp_y = float(slide_row.mpp_y)
        w_px = float(getattr(slide_row, "width_px", 20000) or 20000)
        h_px = float(getattr(slide_row, "height_px", 20000) or 20000)
        slide_dims_um = (w_px * mpp_x, h_px * mpp_y)

    # Fetch preprocess tissue mask from GCS
    try:
        tissue = load_tissue_mask(case_id)
    except PRECONDITION_ERRORS as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Fetch confirmed mitoses
    confirmed_dets = db.scalars(
        select(Detection).where(
            (Detection.case_id == case_uid) | (Detection.case_id == str(case_id)),
            Detection.label == "mitosis"
        )
    ).all()

    hotspot_rows = db.scalars(
        select(Hotspot).where(
            (Hotspot.case_id == case_uid) | (Hotspot.case_id == str(case_id)),
            Hotspot.excluded == False
        )
    ).all()
    hotspot_rows_sorted = sorted(hotspot_rows, key=lambda h: (h.prob_mean or 0.0), reverse=True)
    hotspot_polys = [h.polygon_um for h in hotspot_rows_sorted]
    hotspot_prios = [float(h.prob_mean or 0.0) for h in hotspot_rows_sorted]

    cands = [{"id": d.id, "centroid_um": d.centroid_um, "label": "mitosis"} for d in confirmed_dets]

    if confirmed_dets:
        xs = [d.centroid_um[0] for d in confirmed_dets if d.centroid_um]
        ys = [d.centroid_um[1] for d in confirmed_dets if d.centroid_um]
        bbox = (min(xs), min(ys), max(xs), max(ys))
    elif hotspot_polys:
        all_x = []
        all_y = []
        for poly in hotspot_polys:
            if isinstance(poly, list):
                for pt in poly:
                    if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                        all_x.append(pt[0])
                        all_y.append(pt[1])
        if all_x and all_y:
            bbox = (min(all_x), min(all_y), max(all_x), max(all_y))
        else:
            bbox = (0.0, 0.0, slide_dims_um[0] if slide_dims_um else 5000.0, slide_dims_um[1] if slide_dims_um else 5000.0)
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot re-place HPFs: No confirmed mitoses and no tumor hotspots exist for this case."
        )

    density_map, grid_meta = generate_mitosis_density_map(cands, bounding_box_um=bbox)
    new_hpfs = greedy_place_hpfs(
        density_map,
        grid_meta,
        hotspot_polygons_um=hotspot_polys,
        count=10,
        tissue=tissue,
        slide_dimensions_um=slide_dims_um,
        min_tissue_coverage=0.70,
        hotspot_priorities=hotspot_prios
    )

    # Persist new HPFs
    db.execute(delete(HpfSite).where((HpfSite.case_id == case_uid) | (HpfSite.case_id == str(case_id))))
    for h in new_hpfs:
        hpf_row = HpfSite(
            case_id=case_uid,
            seq=h["seq"],
            center_um=h["center_um"],
            radius_um=h["radius_um"],
            mitotic_count=0,
            source="model"
        )
        db.add(hpf_row)

    db.flush()
    # Invalidate cached HPF thumbnails on GCS (#127)
    invalidate_hpf_cached_thumbnails(case_id)

    # Synchronize HpfSite mitotic counts immediately (#110)
    sync_and_persist_hpf_counts(case_id, db)
    db.commit()

    return get_mitosis_stage_data(case_id, db)


@router.post("/confirm")
def confirm_mitosis_stage(
    payload: MitosisConfirmPayload,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("stage:confirm")),
    _idempotency: IdempotencyContext = idempotent("stages/mitosis/confirm"),
):
    """
    Clinical Safety Gate & Stage 4 Confirmation.
    Verifies that no candidate at or above the review gate (configs/mitosis.yaml) is unreviewed,
    snapshots the confirmed HPFs and Nottingham Mitotic Score, marks Stage 4 confirmed and queues Stage 5.
    """
    try:
        stage_service.confirm_stage(db, payload.case_id, "mitosis", user.id)
    except stage_service.StageServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {
        "status": "success",
        "case_id": payload.case_id,
        "stage": "mitosis",
        "next_stage": "grading"
    }
