import hashlib
import io
import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Literal
from PIL import Image
from fastapi import APIRouter, Depends, HTTPException, status, Response, Query
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.gcs import (
    parse_gcs_uri,
    download_blob_as_bytes,
    upload_blob_from_bytes
)
from app.core.db import get_db
from app.auth.deps import CurrentUser, require
from app.auth.idempotency import IdempotencyContext, IdempotentRoute, idempotent
from app.core.geometry import ContractHTTPError, InvalidSiteError, validate_hpf_sites
from app.core.slide_access import PRECONDITION_ERRORS, open_case_slide, slide_stain_transform
from app.core.pipeline_config import canonical_json, get_pipeline_config
from app.core.tasks import Task, EntityType, ProducerKind, DecisionStatus
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.hotspot import Hotspot
from app.models.audit import AuditEvent
from app.models.decision_record import DecisionRecord
from app.core.rehydrate import rehydrate_case_from_gcs
from app.services import stages as stage_service
from google.api_core.exceptions import NotFound
from pipeline.errors import SlideReadError, SpecimenTypeRequired
from pipeline.hotspots_v6 import frame_polygon_um
from pipeline.slide_io import centered_origin_um, read_region_at_mpp

# Edge of the hotspot review patches, in pixels.
PATCH_PX = 512

router = APIRouter(prefix="/api/v1/stages/triage", tags=["triage"], route_class=IdempotentRoute)

def to_uuid(val: Any) -> uuid.UUID:
    if isinstance(val, uuid.UUID):
        return val
    try:
        return uuid.UUID(str(val))
    except Exception:
        return val


def slide_bounds_um(db: Session, case_id: str) -> tuple[float, float] | None:
    """Slide extent in µm, or None while the slide has no recorded size or resolution."""
    slide_row = db.scalars(select(Slide).where(Slide.case_id == to_uuid(case_id))).first()
    if slide_row is None or not (slide_row.width_px and slide_row.height_px and slide_row.mpp_x):
        return None
    mpp_y = slide_row.mpp_y or slide_row.mpp_x
    return (float(slide_row.width_px * slide_row.mpp_x), float(slide_row.height_px * mpp_y))


class TriageEditOp(BaseModel):
    """One HPF-site edit (docs/contracts/triage_v6.md ``EditOp``). Polygon ops (``modify``) no longer exist."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["add", "move", "exclude", "restore", "delete"]
    id: Optional[str] = None
    center_um: Optional[tuple[float, float]] = None
    reason: Optional[str] = None

    @model_validator(mode="after")
    def _fields_of_the_op(self) -> "TriageEditOp":
        needs_id = self.op != "add"
        needs_centre = self.op in ("add", "move")
        if needs_id and not self.id:
            raise ValueError(f"'{self.op}' needs an id")
        if needs_centre and self.center_um is None:
            raise ValueError(f"'{self.op}' needs center_um")
        if self.op == "exclude" and not (self.reason or "").strip():
            raise ValueError("'exclude' needs a reason")
        return self


class TriageEditsPayload(BaseModel):
    case_id: str
    edits: list[TriageEditOp]


class TriageConfirmPayload(BaseModel):
    case_id: str
    no_invasive_tumor: bool = False
    accept_fewer_hpfs: bool = False


def contract_error(exc: stage_service.StageServiceError) -> HTTPException:
    """The HTTP refusal for a stage-service error: the contract body when it names an ``error`` code."""
    if exc.error:
        return ContractHTTPError(exc.status_code, exc.error, exc.detail, **exc.extra)
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _site(center_um, source: str, diameter_um: float, frame_um: float) -> dict:
    cx, cy = (float(v) for v in center_um)
    return {
        "center_um": [cx, cy],
        "hpf_diameter_um": diameter_um,
        "polygon_um": frame_polygon_um(cx, cy, frame_um),
        "window_um": frame_um,
        "area_mm2": math.pi * (diameter_um / 2.0) ** 2 / 1e6,
        "source": source,
    }


def apply_edit_ops(machine_hotspots: list[dict], edits: list[Any], *, diameter_um: float, frame_um: float) -> list[dict]:
    """
    Applies the pathologist's HPF-site operations to the machine sites, in order.

    ``add`` pins a site (id ``hs_u_<n>``, n over the whole edit history), ``move`` re-centres one,
    ``exclude`` / ``restore`` toggle it, ``delete`` removes it. The server builds each frame from the
    centre. A pinned site has no tissue or tumour fraction and no rank (the pathologist's call); a
    moved model site loses its fractions and score, which belonged to its old position.
    Stored v6.0 edits (polygons) and sites without ``center_um`` need triage to run again.
    """
    sites = {}
    for h in machine_hotspots:
        if h.get("center_um") is None:
            raise stage_service.TriageRerunRequired(f"hotspot {h.get('id')!r} has no center_um")
        sites[h["id"]] = dict(h)
    n_added = 0

    for raw_op in edits:
        op = raw_op.model_dump() if hasattr(raw_op, "model_dump") else dict(raw_op)
        action, hid = op.get("op"), op.get("id")
        if action == "modify" or "polygon_um" in op:
            raise stage_service.TriageRerunRequired("the stored edits are polygon edits from before HPF sites")
        if action == "add":
            n_added += 1
            new_id = hid or f"hs_u_{n_added}"
            if new_id in sites:
                raise InvalidSiteError(new_id, "duplicate_id", f"Site '{new_id}' already exists.")
            sites[new_id] = {
                "id": new_id,
                **_site(op["center_um"], "pathologist_added", diameter_um, frame_um),
                "rank": None, "rank_score": None, "score_kind": None,
                "tissue_fraction": None, "tumor_fraction": None, "prescan_expected": None,
                "excluded": False, "exclude_reason": None,
            }
            continue
        if hid not in sites:
            raise InvalidSiteError(hid, "unknown_site", f"There is no site '{hid}'.")
        if action == "move":
            was_model = sites[hid]["source"] == "model"
            sites[hid].update(_site(op["center_um"], "pathologist_modified" if was_model else sites[hid]["source"],
                                    diameter_um, frame_um))
            if was_model:
                sites[hid].update({"rank_score": None, "tissue_fraction": None, "tumor_fraction": None})
        elif action == "exclude":
            sites[hid]["excluded"] = True
            sites[hid]["exclude_reason"] = op.get("reason")
        elif action == "restore":
            sites[hid]["excluded"] = False
            sites[hid]["exclude_reason"] = None
        elif action == "delete":
            del sites[hid]

    return list(sites.values())


def triage_view(
    db: Session,
    case_id: str,
    stage_exec: StageExecution,
    machine_output: dict,
    machine_hotspots: list[dict],
    effective_hotspots: list[dict],
    edits: list,
) -> dict:
    """The triage stage as docs/contracts/triage_v6.md ``TriageStageV6``, plus the v5 fields
    existing clients still read. ``heatmap``, ``tumor_threshold`` and ``flags`` are null when
    no machine output carries them.
    """
    slide_row = db.scalars(select(Slide).where(Slide.case_id == to_uuid(case_id))).first()
    slide = None
    if slide_row is not None and slide_row.width_px and slide_row.height_px and slide_row.mpp_x and slide_row.mpp_y:
        slide = {
            "width_px": int(slide_row.width_px),
            "height_px": int(slide_row.height_px),
            "mpp_x": float(slide_row.mpp_x),
            "mpp_y": float(slide_row.mpp_y),
        }

    heatmap_url = None
    if stage_exec.status not in ("queued", "running", "failed"):
        heatmap_url = f"/api/v1/stages/triage/{case_id}/heatmap"
        if settings.CDN_BASE_URL:
            heatmap_url = f"{settings.CDN_BASE_URL.rstrip('/')}/cases/{case_id}/triage/heatmap.png"
    # Tile-resolution heatmap geometry (SPEC-05 §4.3, contracts/triage_v6.md Heatmap).
    heatmap_meta = machine_output.get("heatmap")
    heatmap = None if heatmap_meta is None else {**heatmap_meta, "png_url": heatmap_url}

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
        "slide": slide,
        "heatmap": heatmap,
        "tumor_threshold": machine_output.get("tumor_threshold"),
        "hotspots": effective_hotspots,
        "machine_hotspots": machine_hotspots,
        "flags": machine_output.get("flags"),
        "hpf_target": machine_output.get("hpf_target"),
        "n_sites_available": machine_output.get("n_sites_available"),
        "provenance": {
            "stage": "triage",
            "model_versions": stage_exec.model_versions,
            "config_hash": stage_exec.config_hash,
            "run_mode": stage_exec.run_mode,
        },
        "effective_hotspots": effective_hotspots,
        "review_edits": edits,
        "heatmap_png_uri": machine_output.get("heatmap_png_uri"),
        "heatmap_direct_url": heatmap_url,
        "model_versions": stage_exec.model_versions,
    }


def _hotspot_gap_um(db: Session, case_id: str) -> float:
    """The case's hotspot gap (SPEC-05 §5.5); 409 when its specimen type is needed and unset."""
    case = db.get(Case, to_uuid(case_id))
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Case {case_id} not found")
    try:
        return get_pipeline_config().hotspot_gap_um(case.specimen_type)
    except SpecimenTypeRequired as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{case_id}")
def get_triage_data(case_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(require("case:read"))):
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

    if stage_exec.status in ("queued", "running", "failed"):
        # Attempt isolation: do not read stale outputs from previous attempts (#568)
        # On failure, machine output was not produced; return empty structures instead of 502
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
        try:
            effective_hotspots = stage_service.effective_triage_hotspots(stage_exec, machine_output)
        except stage_service.StageServiceError as exc:
            raise contract_error(exc) from exc

    return triage_view(db, case_id, stage_exec, machine_output, machine_hotspots, effective_hotspots, edits)


@router.get("/{case_id}/heatmap")
def get_triage_heatmap_image(case_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(require("case:read"))):
    """Returns the tile-resolution heatmap PNG (SPEC-05 §4.3) directly from GCS."""
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
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("case:read"))
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
        # The effective site: the machine site with the reviewer's edits (#573, #701). A site the
        # reviewer moved or pinned has no pre-generated patch, so its patches are read from the slide.
        st_obj = db.scalars(
            select(StageExecution).where(
                StageExecution.case_id == case_id,
                StageExecution.stage == "triage"
            ).order_by(StageExecution.attempt.desc())
        ).first()

        try:
            out_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/triage/output.json")
            machine_output = json.loads(out_bytes.decode("utf-8"))
        except (NotFound, FileNotFoundError, ValueError):
            machine_output = {}  # no machine output: the site is unknown (404 below)
        machine_sites = machine_output.get("hotspots", [])
        sites = machine_sites
        if st_obj is not None and st_obj.review_edits:
            try:
                diameter_um, frame_um, _ = stage_service.site_geometry(machine_output)
                sites = apply_edit_ops(machine_sites, st_obj.review_edits, diameter_um=diameter_um, frame_um=frame_um)
            except stage_service.StageServiceError as exc:
                raise contract_error(exc) from exc
        site = next((h for h in sites if h["id"] == hotspot_id), None)
        if site is not None:
            if site.get("center_um") is None:
                raise contract_error(stage_service.TriageRerunRequired(f"hotspot {hotspot_id!r} has no center_um"))
            cx_um, cy_um = (float(v) for v in site["center_um"])
            machine_site = next((h for h in machine_sites if h["id"] == hotspot_id), None)
            is_user_edited = machine_site is None or machine_site.get("center_um") != site["center_um"]

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
def save_triage_edits(payload: TriageEditsPayload, db: Session = Depends(get_db), user: CurrentUser = Depends(require("stage:review"))):
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

    try:
        machine_output = stage_service.machine_triage_output(stage_exec)
        diameter_um, frame_um, hpf_target = stage_service.site_geometry(machine_output)
        # Edits accumulate: each request appends its ops (the client sends one op per request). A pin
        # gets the next hs_u_<n> over the whole history, which the stored op keeps.
        stored = list(stage_exec.review_edits or [])
        n_added = sum(1 for e in stored if e.get("op") == "add")
        new_edits = []
        for e in payload.edits:
            op = e.model_dump(exclude_none=True)
            if "center_um" in op:
                op["center_um"] = [float(v) for v in op["center_um"]]
            if op["op"] == "add":
                n_added += 1
                op["id"] = f"hs_u_{n_added}"
            new_edits.append(op)
        edits_dict = stored + new_edits
        machine_hotspots = machine_output.get("hotspots", [])
        effective_hotspots = apply_edit_ops(machine_hotspots, edits_dict, diameter_um=diameter_um, frame_um=frame_um)
    except stage_service.StageServiceError as exc:
        raise contract_error(exc) from exc
    gap_um = _hotspot_gap_um(db, payload.case_id)
    validate_hpf_sites(
        effective_hotspots, diameter_um=diameter_um, gap_um=gap_um, hpf_target=hpf_target,
        slide_bounds_um=slide_bounds_um(db, payload.case_id),
    )

    stage_exec.review_edits = edits_dict

    audit = AuditEvent(
        case_id=str(payload.case_id),
        actor=user.id,
        event_type="review_edit",
        stage="triage",
        payload={"edit_count": len(payload.edits)}
    )
    db.add(audit)

    # Record DecisionRecord for human review edit (SPEC-05 §5.5, SPEC-01 §3.3)
    model_dr = db.scalars(
        select(DecisionRecord).where(
            DecisionRecord.case_id == to_uuid(payload.case_id),
            DecisionRecord.stage == "triage",
            DecisionRecord.task == Task.HOTSPOT_SELECT.value,
        ).order_by(DecisionRecord.created_at.desc())
    ).first()

    human_dr = DecisionRecord(
        case_id=to_uuid(payload.case_id),
        stage_execution_id=stage_exec.id,
        stage="triage",
        task=Task.HUMAN_EDIT.value,
        entity_type=EntityType.HOTSPOT.value,
        entity_id=str(stage_exec.id),
        producer_kind=ProducerKind.HUMAN.value,
        producer_id=user.id,
        producer_version="human_review@1.0",
        input_sha256=hashlib.sha256(canonical_json(new_edits).encode("utf-8")).hexdigest(),
        input_spec={"edits": new_edits},
        params={"gap_um": gap_um},
        output={"effective_hotspots_count": len(effective_hotspots)},
        status=DecisionStatus.OK.value,
        latency_ms=0,
        run_mode=stage_exec.run_mode,
        # The edit is checked under the current configuration (the gap), so that is its config hash.
        config_hash=get_pipeline_config().config_hash(),
        supersedes_id=model_dr.id if model_dr else None,
    )
    db.add(human_dr)
    db.commit()

    view = triage_view(db, payload.case_id, stage_exec, machine_output, machine_hotspots, effective_hotspots, edits_dict)
    view["edits_count"] = len(payload.edits)
    return view


@router.post("/confirm")
def confirm_triage(
    payload: TriageConfirmPayload,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("stage:confirm")),
    _idempotency: IdempotencyContext = idempotent("stages/triage/confirm"),
):
    """
    Confirms triage stage, writes effective hotspots into DB, and queues next stage.
    """
    try:
        result = stage_service.confirm_stage(
            db, payload.case_id, "triage", user.id,
            no_invasive_tumor=payload.no_invasive_tumor, accept_fewer_hpfs=payload.accept_fewer_hpfs,
        )
    except stage_service.StageServiceError as exc:
        raise contract_error(exc) from exc
    return {
        "status": "confirmed",
        "case_id": payload.case_id,
        "confirmed_hotspots_count": result.details["confirmed_hotspots_count"],
        "next_stage_queued": result.next_stage,
        "accept_fewer_hpfs": result.details["accept_fewer_hpfs"],
    }

