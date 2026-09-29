import io
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Literal
import numpy as np
from PIL import Image
from fastapi import APIRouter, Depends, HTTPException, status, Response, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.gcs import (
    parse_gcs_uri,
    download_blob_as_bytes,
    upload_blob_from_bytes
)
from app.core.db import get_db
from app.core.slide_access import PRECONDITION_ERRORS, open_case_slide, slide_stain_transform
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.hotspot import Hotspot
from app.models.audit import AuditEvent
from app.core.rehydrate import rehydrate_case_from_gcs
from google.api_core.exceptions import NotFound
from pipeline.errors import SlideReadError
from pipeline.slide_io import centered_origin_um, read_region_at_mpp

# Edge of the hotspot review patches, in pixels.
PATCH_PX = 512

router = APIRouter(prefix="/api/v1/stages/triage", tags=["triage"])

def to_uuid(val: Any) -> uuid.UUID:
    if isinstance(val, uuid.UUID):
        return val
    try:
        return uuid.UUID(str(val))
    except Exception:
        return val


def compute_polygon_area_mm2(coords: list[list[float]]) -> float:
    """
    Computes polygon area in square millimeters from micrometer coordinates [[x, y], ...].
    Uses the Shoelace formula.
    """
    if not coords or len(coords) < 3:
        return 0.0
    pts = np.array(coords, dtype=float)
    x = pts[:, 0]
    y = pts[:, 1]
    area_um2 = 0.5 * np.abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))
    return round(float(area_um2 / 1e6), 4)


def coordinates_differ(poly1: list[list[float]], poly2: list[list[float]], tol_um: float = 1.0) -> bool:
    """
    Checks if two polygon coordinate lists differ by more than tol_um.
    """
    try:
        a1 = np.array(poly1, dtype=float)
        a2 = np.array(poly2, dtype=float)
        if a1.shape != a2.shape:
            return True
        return bool(np.max(np.abs(a1 - a2)) > tol_um)
    except Exception:
        return True


class TriageEditOp(BaseModel):
    op: Literal["add", "modify", "exclude", "delete"]
    id: Optional[str] = None
    polygon_um: Optional[list[list[float]]] = None
    reason: Optional[str] = None
    area_mm2: Optional[float] = None
    prob_mean: Optional[float] = None
    prob_max: Optional[float] = None


class TriageEditsPayload(BaseModel):
    case_id: str
    edits: list[TriageEditOp]


class TriageConfirmPayload(BaseModel):
    case_id: str
    no_invasive_tumor: bool = False
    reviewed_by: str = "pathologist_01"


def apply_edit_ops(machine_hotspots: list[dict], edits: list[Any]) -> list[dict]:
    """
    Applies RFC-6902 style diff operations to machine output hotspots.
    Idempotent, order-stable, and collision-proof.
    """
    hotspots_dict = {h["id"]: dict(h) for h in machine_hotspots}
    user_counter = 1

    for raw_op in edits:
        op = raw_op.model_dump() if hasattr(raw_op, "model_dump") else (raw_op.dict() if hasattr(raw_op, "dict") else dict(raw_op))
        action = op.get("op")
        hid = op.get("id")

        if action == "modify" and hid in hotspots_dict:
            new_poly = op.get("polygon_um")
            if new_poly:
                orig_poly = hotspots_dict[hid].get("polygon_um", [])
                if coordinates_differ(orig_poly, new_poly):
                    hotspots_dict[hid]["polygon_um"] = new_poly
                    hotspots_dict[hid]["source"] = "pathologist_modified"
                    hotspots_dict[hid]["area_mm2"] = compute_polygon_area_mm2(new_poly)
                else:
                    # Unchanged coordinates preserve original source (#75)
                    hotspots_dict[hid]["polygon_um"] = new_poly

        elif action == "add":
            poly = op.get("polygon_um", [])
            # Reject degenerate/empty polygons (#95)
            if not poly or len(poly) < 3:
                continue

            # Collision-proof ROI ID (#741, #714)
            new_id = hid
            if not new_id or new_id.startswith("hs_") or new_id in hotspots_dict:
                while f"user_roi_{user_counter:02d}" in hotspots_dict:
                    user_counter += 1
                new_id = f"user_roi_{user_counter:02d}"
                user_counter += 1

            calc_area = compute_polygon_area_mm2(poly)
            area_val = op.get("area_mm2") if op.get("area_mm2") is not None else calc_area

            hotspots_dict[new_id] = {
                "id": new_id,
                "polygon_um": poly,
                "area_mm2": area_val,
                "prob_mean": op.get("prob_mean"),  # None for pathologist additions (#95)
                "prob_max": op.get("prob_max"),    # None for pathologist additions (#95)
                "source": "pathologist_added",
                "excluded": False,
                "exclude_reason": None
            }

        elif action == "exclude" and hid in hotspots_dict:
            hotspots_dict[hid]["excluded"] = True
            hotspots_dict[hid]["exclude_reason"] = op.get("reason", "Pathologist excluded")

        elif action == "delete" and hid in hotspots_dict:
            del hotspots_dict[hid]

    return list(hotspots_dict.values())


@router.get("/{case_id}")
def get_triage_data(case_id: str, db: Session = Depends(get_db)):
    """
    Returns latest triage machine outputs, probability grid ref, heatmap URI, and saved edits.
    """
    stage_exec = db.scalars(
        select(StageExecution).where(
            StageExecution.case_id == case_id,
            StageExecution.stage == "triage"
        ).order_by(StageExecution.attempt.desc())
    ).first()

    if not stage_exec:
        rehydrate_case_from_gcs(case_id, db)
        stage_exec = db.scalars(
            select(StageExecution).where(
                StageExecution.case_id == case_id,
                StageExecution.stage == "triage"
            ).order_by(StageExecution.attempt.desc())
        ).first()

    if not stage_exec:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No triage stage execution found for case {case_id}"
        )

    output_ref = stage_exec.output_ref or ""
    machine_output = {}

    if stage_exec.status in ("queued", "running"):
        # Attempt isolation: do not read stale outputs from previous attempts (#568)
        machine_hotspots = []
        effective_hotspots = []
        edits = stage_exec.review_edits or []
    else:
        try:
            if output_ref and output_ref.startswith("gs://"):
                b_name, bl_name = parse_gcs_uri(output_ref)
                out_bytes = download_blob_as_bytes(b_name, bl_name)
                machine_output = json.loads(out_bytes.decode("utf-8"))
            else:
                out_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/triage/output.json")
                machine_output = json.loads(out_bytes.decode("utf-8"))
        except Exception as e:
            # Storage failure on completed/awaiting_review stage (#91)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to load triage machine output from storage: {e}"
            )

        edits = stage_exec.review_edits or []
        machine_hotspots = machine_output.get("hotspots", [])
        effective_hotspots = apply_edit_ops(machine_hotspots, edits)

    heatmap_url = f"/api/v1/stages/triage/{case_id}/heatmap"
    if settings.CDN_BASE_URL:
        heatmap_url = f"{settings.CDN_BASE_URL.rstrip('/')}/cases/{case_id}/triage/heatmap_triage.png"

    # Ensure all effective hotspots have accessible thumbnail_url
    for hs in effective_hotspots:
        hs_id = hs.get("id")
        if settings.CDN_BASE_URL:
            hs["thumbnail_url"] = f"{settings.CDN_BASE_URL.rstrip('/')}/cases/{case_id}/triage/patches/{hs_id}_thumb.png"
        else:
            hs["thumbnail_url"] = f"/api/v1/stages/triage/{case_id}/hotspots/{hs_id}/thumbnail?mag=10x"

    return {
        "case_id": case_id,
        "stage_execution_id": str(stage_exec.id),
        "status": stage_exec.status,
        "heatmap_png_uri": machine_output.get("heatmap_png_uri"),
        "heatmap_direct_url": heatmap_url,
        "prob_grid_uri": machine_output.get("prob_grid_uri"),
        "grid": machine_output.get("grid"),
        "machine_hotspots": machine_hotspots,
        "effective_hotspots": effective_hotspots,
        "review_edits": edits,
        "model_versions": stage_exec.model_versions
    }


@router.get("/{case_id}/heatmap")
def get_triage_heatmap_image(case_id: str, db: Session = Depends(get_db)):
    """Returns the Viridis heatmap PNG overlay directly from GCS."""
    try:
        hm_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/triage/heatmap_triage.png")
        return Response(content=hm_bytes, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})
    except Exception:
        pass

    try:
        hm_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/triage/heatmap.png")
        return Response(content=hm_bytes, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})
    except Exception:
        raise HTTPException(
            status_code=404,
            detail="Heatmap image artifact not found in GCS",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
        )


@router.get("/{case_id}/hotspots/{hotspot_id}/thumbnail")
def get_hotspot_thumbnail(
    case_id: str, 
    hotspot_id: str, 
    mag: Literal["10x", "20x", "40x"] = "10x",
    stain: Literal["norm", "orig"] = "norm",
    cx: Optional[float] = Query(None),
    cy: Optional[float] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Extracts and streams a calibrated microscopic RGB patch centered on the specified hotspot.
    Supports real-time magnification switching (10x, 20x, 40x) and stain normalization toggling (norm, orig).
    """
    if mag not in ("10x", "20x", "40x"):
        raise HTTPException(status_code=400, detail=f"Invalid mag '{mag}'. Must be '10x', '20x', or '40x'.")
    if stain not in ("norm", "orig"):
        raise HTTPException(status_code=400, detail=f"Invalid stain '{stain}'. Must be 'norm' or 'orig'.")

    # Lookup Case and Slide
    case_uid = to_uuid(case_id)
    case_obj = db.scalars(select(Case).where(Case.id == case_uid)).first() if isinstance(case_uid, uuid.UUID) else db.get(Case, case_id)
    slide_obj = db.scalars(select(Slide).where(Slide.case_id == case_uid)).first() if isinstance(case_uid, uuid.UUID) else None
    if not slide_obj and case_obj and getattr(case_obj, "slides", None):
        slide_obj = case_obj.slides[0]
    if not slide_obj:
        raise HTTPException(status_code=404, detail=f"Slide not found for case {case_id}")

    if not getattr(slide_obj, "mpp_x", None) or not getattr(slide_obj, "mpp_y", None):
        raise HTTPException(status_code=400, detail="Slide is missing valid MPP (status='needs_mpp'). Cannot extract patch.")

    mpp_x = float(slide_obj.mpp_x)
    mpp_y = float(slide_obj.mpp_y)

    cx_um = None
    cy_um = None
    is_user_edited = False

    actual_cx = None if (cx is None or hasattr(cx, "default")) else cx
    actual_cy = None if (cy is None or hasattr(cy, "default")) else cy

    if actual_cx is not None and actual_cy is not None:
        cx_um = float(actual_cx)
        cy_um = float(actual_cy)
        is_user_edited = True
    else:
        # Check review edits FIRST (#573, #701)
        st_obj = db.scalars(
            select(StageExecution).where(
                StageExecution.case_id == case_id,
                StageExecution.stage == "triage"
            ).order_by(StageExecution.attempt.desc())
        ).first()

        if st_obj and st_obj.review_edits:
            for ed in st_obj.review_edits:
                if ed.get("id") == hotspot_id and "polygon_um" in ed:
                    poly = np.array(ed["polygon_um"])
                    if len(poly) > 0:
                        cx_um = float(poly[:, 0].mean())
                        cy_um = float(poly[:, 1].mean())
                        is_user_edited = True
                        break

        # If not in review_edits, check machine output
        if cx_um is None or cy_um is None:
            try:
                out_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/triage/output.json")
                tdata = json.loads(out_bytes.decode("utf-8"))
                target_hs = next((h for h in tdata.get("hotspots", []) if h["id"] == hotspot_id), None)
                if target_hs and "polygon_um" in target_hs:
                    poly = np.array(target_hs["polygon_um"])
                    if len(poly) > 0:
                        cx_um = float(poly[:, 0].mean())
                        cy_um = float(poly[:, 1].mean())
            except Exception:
                pass

    # Unknown hotspot ID must return 404 (#734)
    if cx_um is None or cy_um is None:
        raise HTTPException(status_code=404, detail=f"Hotspot {hotspot_id} not found on case {case_id}")

    # If NOT user-edited, attempt fast path from pre-generated GCS static patch
    patch_blob = f"cases/{case_id}/triage/patches/{hotspot_id}_{mag}_{stain}.png"
    if not is_user_edited:
        try:
            thumb_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, patch_blob)
            return Response(content=thumb_bytes, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})
        except Exception:
            pass

        # Fast fallback for legacy 10x norm thumbnail
        if mag == "10x" and stain == "norm":
            try:
                thumb_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/triage/patches/{hotspot_id}_thumb.png")
                return Response(content=thumb_bytes, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})
            except Exception:
                pass

    field_um = 512.0
    if mag == "20x":
        field_um = 256.0
    elif mag == "40x":
        field_um = 128.0

    extracted_bytes = None

    # 1. Fast Path: Reconstruct directly from GCS DeepZoom pyramid tiles (<100ms)
    if slide_obj:
        from pipeline.tiles import extract_patch_from_pyramid
        slide_id = str(slide_obj.id)
        if not slide_obj.width_px or not slide_obj.height_px:
            raise HTTPException(status_code=400, detail="Slide has no pixel dimensions. Cannot extract patch.")
        width_px = int(slide_obj.width_px)
        height_px = int(slide_obj.height_px)
        patch_img = extract_patch_from_pyramid(
            slide_id=slide_id,
            cx_um=cx_um,
            cy_um=cy_um,
            field_um=field_um,
            mpp_x=mpp_x,
            mpp_y=mpp_y,
            width_px=width_px,
            height_px=height_px,
            layer=stain
        )
        if patch_img:
            buf = io.BytesIO()
            patch_img.save(buf, format="PNG")
            extracted_bytes = buf.getvalue()

    # 2. Fallback: read the raw WSI if pyramid tiles are incomplete. The normalised variant is the raw region
    # through the slide's persisted stain profile; a slide without a usable one answers 409, not a raw image.
    if extracted_bytes is None and slide_obj:
        try:
            stain_transform = slide_stain_transform(db, slide_obj, case_obj) if stain == "norm" else None
            with open_case_slide(case_id, slide_obj) as reader:
                x_um, y_um = centered_origin_um(reader, cx_um, cy_um, field_um, field_um)
                region = read_region_at_mpp(
                    reader, x_um, y_um, field_um, field_um, field_um / PATCH_PX,
                    color="raw" if stain_transform is None else "normalized", stain=stain_transform,
                )
            buf = io.BytesIO()
            Image.fromarray(region.rgb).save(buf, format="PNG")
            extracted_bytes = buf.getvalue()
        except PRECONDITION_ERRORS as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (SlideReadError, NotFound, OSError) as exc:
            print(f"[Thumbnail Dynamic Extraction Note] {exc}")

    # Nothing is drawn in place of a patch that cannot be read (SPEC-01 §3.9).
    if extracted_bytes is None:
        raise HTTPException(
            status_code=404,
            detail=f"The {mag} patch for hotspot {hotspot_id} could not be read from the slide.",
        )

    # Cache to GCS if not user-edited
    if not is_user_edited:
        try:
            upload_blob_from_bytes(
                settings.GCS_ARTIFACTS_BUCKET,
                patch_blob,
                extracted_bytes,
                "image/png"
            )
        except Exception as up_e:
            print(f"[Thumbnail GCS Cache Note] {up_e}")

    return Response(content=extracted_bytes, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@router.post("/edits")
def save_triage_edits(payload: TriageEditsPayload, db: Session = Depends(get_db)):
    """
    Saves draft edit operations diff.
    """
    stage_exec = db.scalars(
        select(StageExecution).where(
            StageExecution.case_id == payload.case_id,
            StageExecution.stage == "triage"
        ).order_by(StageExecution.attempt.desc())
    ).first()

    if not stage_exec:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Triage stage execution not found for case {payload.case_id}"
        )

    # Reject edits if stage is already confirmed (#93)
    if stage_exec.status == "confirmed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Triage stage for case {payload.case_id} is already confirmed and immutable."
        )

    edits_dict = [
        e.model_dump() if hasattr(e, "model_dump") else (e.dict() if hasattr(e, "dict") else dict(e))
        for e in payload.edits
    ]
    stage_exec.review_edits = edits_dict
    
    audit = AuditEvent(
        case_id=str(payload.case_id),
        actor="pathologist",
        event_type="review_edit",
        stage="triage",
        payload={"edit_count": len(payload.edits)}
    )
    db.add(audit)
    db.commit()

    return {"status": "success", "edits_count": len(payload.edits)}


@router.post("/confirm")
def confirm_triage(payload: TriageConfirmPayload, db: Session = Depends(get_db)):
    """
    Confirms triage stage, writes effective hotspots into DB, and queues next stage.
    """
    stage_exec = db.scalars(
        select(StageExecution).where(
            StageExecution.case_id == payload.case_id,
            StageExecution.stage == "triage"
        ).order_by(StageExecution.attempt.desc())
    ).first()

    if not stage_exec:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Triage stage execution not found for case {payload.case_id}"
        )

    if stage_exec.status != "awaiting_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Triage stage cannot be confirmed because its status is '{stage_exec.status}', expected 'awaiting_review'."
        )

    output_ref = stage_exec.output_ref or ""
    machine_hotspots = []
    try:
        if output_ref and output_ref.startswith("gs://"):
            b_name, bl_name = parse_gcs_uri(output_ref)
            out_bytes = download_blob_as_bytes(b_name, bl_name)
            machine_hotspots = json.loads(out_bytes.decode("utf-8")).get("hotspots", [])
        else:
            out_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{payload.case_id}/triage/output.json")
            machine_hotspots = json.loads(out_bytes.decode("utf-8")).get("hotspots", [])
    except Exception as e:
        print(f"[Triage Confirm Error] Could not load hotspots from GCS: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to load triage machine output from storage: {e}. Confirmation aborted."
        )

    edits = stage_exec.review_edits or []
    effective_hotspots = apply_edit_ops(machine_hotspots, edits)

    # Zero-tumor guardrail: if 0 active hotspots, must explicitly specify no_invasive_tumor=True (#92)
    active_hotspots = [h for h in effective_hotspots if not h.get("excluded", False)]
    if payload.no_invasive_tumor and len(active_hotspots) > 0:
        # Issue #569: Reject attempt to confirm zero tumor while active hotspots remain
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot confirm 'no_invasive_tumor=True' when {len(active_hotspots)} active tumor hotspot(s) exist. All tumor hotspots must be excluded or deleted before confirming zero tumor."
        )
    if len(active_hotspots) == 0 and not payload.no_invasive_tumor:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="No active hotspots remaining. Pathologist must explicitly flag no_invasive_tumor=True to confirm zero tumor on this slide."
        )

    # Delete any prior confirmed hotspots for this case safely (#700)
    db.query(Hotspot).filter(Hotspot.case_id == str(payload.case_id)).delete(synchronize_session=False)

    # Persist effective hotspots to DB
    for hs in effective_hotspots:
        hotspot_row = Hotspot(
            id=hs["id"],
            case_id=str(payload.case_id),
            stage_execution_id=str(stage_exec.id),
            polygon_um=hs["polygon_um"],
            area_mm2=hs.get("area_mm2"),
            prob_mean=hs.get("prob_mean"),
            prob_max=hs.get("prob_max"),
            source=hs.get("source", "model"),
            excluded=hs.get("excluded", False),
            exclude_reason=hs.get("exclude_reason")
        )
        db.add(hotspot_row)

    stage_exec.status = "confirmed"
    stage_exec.reviewed_at = datetime.now(timezone.utc)
    stage_exec.reviewed_by = payload.reviewed_by

    case_uid = to_uuid(payload.case_id)
    if payload.no_invasive_tumor:
        next_stage_name = None
        input_data = {"benign_flag": True, "reason": "No invasive tumor identified"}
        case_obj = db.get(Case, case_uid)
        if case_obj:
            case_obj.status = "done"
        next_exec = None
    else:
        next_stage_name = "mitosis"
        input_data = {"confirmed_hotspots_count": len(effective_hotspots)}

        # Ensure attempt monotonicity when queuing next stage (Issue #279, #285, #535)
        stmt_existing = (
            select(StageExecution)
            .where(
                (StageExecution.case_id == case_uid) | (StageExecution.case_id == str(payload.case_id)),
                StageExecution.stage == next_stage_name
            )
            .order_by(StageExecution.attempt.desc())
        )
        existing_next = db.scalars(stmt_existing).first()
        next_attempt = (existing_next.attempt + 1) if existing_next else 1

        next_exec = StageExecution(
            case_id=case_uid,
            stage=next_stage_name,
            attempt=next_attempt,
            status="queued",
            input_ref=input_data
        )
        db.add(next_exec)

    audit = AuditEvent(
        case_id=str(payload.case_id),
        actor=payload.reviewed_by,
        event_type="stage_confirmed",
        stage="triage",
        payload={
            "confirmed_hotspots": len(effective_hotspots),
            "no_invasive_tumor": payload.no_invasive_tumor,
            "next_stage": next_stage_name
        }
    )
    db.add(audit)
    db.commit()

    if next_exec and next_stage_name:
        try:
            from app.core.cloud_tasks import dispatch_stage_task
            dispatch_stage_task(
                case_id=str(payload.case_id),
                stage=next_stage_name,
                stage_exec_id=str(next_exec.id)
            )
        except Exception as e:
            print(f"[CloudTasks Warning] Failed to dispatch next stage {next_stage_name}: {e}")

    return {
        "status": "confirmed",
        "case_id": payload.case_id,
        "confirmed_hotspots_count": len(effective_hotspots),
        "next_stage_queued": next_stage_name
    }

