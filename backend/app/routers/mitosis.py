"""
Stage 4 review API: the ``mitosis_v6`` contract (docs/contracts/mitosis_v6.md, SPEC-06 §5.6-5.8).

Every payload is built from the ``detections`` and ``hpf_sites`` rows; ``counted`` is the database's
generated column and every count, area and score comes from ``pipeline/scoring.py`` (AC10). Each
recompute after a review writes a ``mitosis_count`` DecisionRecord that supersedes the previous one
(AC8). Errors answer with the contract's bodies, ``{"error": <code>, "detail": <text>, ...}``.
"""
import io
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Literal, Optional, Tuple

from PIL import Image
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from google.api_core.exceptions import NotFound
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.gcs import blob_exists, delete_blob, download_blob_as_bytes, upload_blob_from_bytes
from app.core.db import get_db
from app.auth.deps import CurrentUser, require
from app.auth.idempotency import IdempotencyContext, IdempotentRoute, idempotent
from app.core.pipeline_config import get_pipeline_config
from app.core.slide_access import PRECONDITION_ERRORS, open_case_slide, slide_stain_transform
from app.core.tasks import Task
from app.core.tissue_mask_store import load_tissue_mask
from app.inference.records import mitosis_count_record
from pipeline.detect import candidate_review_crops, review_crop_blob
from pipeline.errors import SlideReadError
from pipeline.hpf import place_hpfs
from pipeline.mitosis_gate import load_tumor_gate
from pipeline.scoring import summarize_stage4
from pipeline.slide_io import centered_origin_um, read_region_at_mpp
from app.models.case import Case
from app.models.decision_record import DecisionRecord
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.hotspot import Hotspot
from app.models.detection import Detection
from app.models.hpf_site import HpfSite
from app.models.audit import AuditEvent
from app.core.rehydrate import rehydrate_case_from_gcs
from app.services import stages as stage_service


class ContractError(Exception):
    """A refusal with the contract's error body."""

    def __init__(self, status_code: int, error: str, detail: str, **extra: Any):
        super().__init__(detail)
        self.status_code = status_code
        self.body = {"error": error, "detail": detail, **extra}

    def response(self) -> JSONResponse:
        return JSONResponse(status_code=self.status_code, content=self.body)


class MitosisRoute(IdempotentRoute):
    """Answers a ``ContractError`` with its body (after the idempotency key is released)."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def contract_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except ContractError as exc:
                return exc.response()

        return contract_handler


router = APIRouter(prefix="/api/v1/stages/mitosis", tags=["mitosis"], route_class=MitosisRoute)

# The stage accepts review edits only while it awaits review.
EDITABLE_STATUS = "awaiting_review"


def to_uuid(val: Any) -> uuid.UUID:
    if isinstance(val, uuid.UUID):
        return val
    try:
        return uuid.UUID(str(val))
    except ValueError as exc:
        raise ContractError(404, "not_found", f"Case {val} not found") from exc


def _case_and_stage(case_id: str, db: Session, *, lock: bool = False) -> Tuple[Case, StageExecution]:
    """The case and its latest mitosis execution (``lock`` takes a row lock for state-gated writes)."""
    case_uid = to_uuid(case_id)
    case_obj = db.get(Case, case_uid) or rehydrate_case_from_gcs(case_id, db)
    if not case_obj:
        raise ContractError(404, "not_found", f"Case {case_id} not found")

    stmt = select(StageExecution).where(
        StageExecution.case_id == case_obj.id, StageExecution.stage == "mitosis"
    ).order_by(StageExecution.attempt.desc()).limit(1)
    if lock:
        # Row lock serialises concurrent state-gated writes (SPEC-03 §5.3.3); SQLite ignores it.
        stmt = stmt.with_for_update()
    stage_exec = db.scalars(stmt).first()
    if not stage_exec:
        raise ContractError(404, "not_found", "Stage 4 (mitosis) not found for this case")
    return case_obj, stage_exec


def _editable(case_id: str, db: Session) -> Tuple[Case, StageExecution]:
    case_obj, stage_exec = _case_and_stage(case_id, db, lock=True)
    if stage_exec.status != EDITABLE_STATUS:
        raise ContractError(409, "stage_locked", f"Mitosis stage is '{stage_exec.status}'; only a stage awaiting review can be edited.")
    return case_obj, stage_exec


def _slide(db: Session, case_obj: Case) -> Slide:
    """The case's slide with its geometry; there is no payload without it (nothing is invented)."""
    slide = db.scalars(select(Slide).where(Slide.case_id == case_obj.id).limit(1)).first()
    if slide is None or not slide.width_px or not slide.height_px or not slide.mpp_x or not slide.mpp_y:
        raise ContractError(404, "not_found", f"Case {case_obj.id} has no slide with pixel dimensions and resolution")
    return slide


def _candidates(db: Session, case_obj: Case) -> List[Dict[str, Any]]:
    """Every candidate row as a dict (the contract fields plus ``record_ids``)."""
    rows = db.scalars(select(Detection).where(Detection.case_id == case_obj.id).order_by(Detection.id)).all()
    return [
        {
            "id": d.id,
            "hotspot_id": d.hotspot_id,
            "centroid_um": list(d.centroid_um),
            "p_a": d.p_a,
            "p_b": d.p_b,
            "vlm": d.vlm,
            "in_tumor": d.in_tumor,
            "final_decision": d.final_decision,
            "decision_path": d.decision_path,
            "review_label": d.review_label,
            "counted": bool(d.counted),
            "record_ids": d.record_ids,
        }
        for d in rows
    ]


def _hpf_rows(db: Session, case_obj: Case) -> List[HpfSite]:
    return list(db.scalars(select(HpfSite).where(HpfSite.case_id == case_obj.id).order_by(HpfSite.seq.asc())).all())


def _crop_url(case_id: str, candidate_id: str, kind: str) -> str:
    return f"/api/v1/stages/mitosis/{case_id}/candidates/{candidate_id}/{kind}"


def _stage_view(db: Session, case_obj: Case, stage_exec: StageExecution) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """The ``MitosisStageV6`` payload, and the candidates with their record ids for the count record."""
    slide = _slide(db, case_obj)
    case_id = str(case_obj.id)
    candidates = _candidates(db, case_obj)
    hpfs = [
        {"seq": h.seq, "center_um": list(h.center_um), "radius_um": h.radius_um,
         "tissue_coverage": h.tissue_coverage, "tumor_fraction": h.tumor_fraction}
        for h in _hpf_rows(db, case_obj)
    ]
    cfg = get_pipeline_config().mitosis
    hpfs, summary = summarize_stage4(candidates, hpfs, scoring=cfg.scoring, hpf_count=cfg.hpf.count)
    view = {
        "case_id": case_id,
        "stage_execution_id": str(stage_exec.id),
        "status": stage_exec.status,
        "slide": {"width_px": slide.width_px, "height_px": slide.height_px,
                  "mpp_x": float(slide.mpp_x), "mpp_y": float(slide.mpp_y)},
        "candidates": [
            {**{k: v for k, v in c.items() if k != "record_ids"},
             "crop_url": _crop_url(case_id, c["id"], "crop"),
             "context_url": _crop_url(case_id, c["id"], "context")}
            for c in candidates
        ],
        "hpfs": [
            {"seq": h["seq"], "center_um": h["center_um"], "radius_um": h["radius_um"], "count": h["count"],
             "tissue_coverage": h["tissue_coverage"], "tumor_fraction": h["tumor_fraction"]}
            for h in hpfs
        ],
        "summary": summary,
        "provenance": {
            "stage": "mitosis",
            # Only what the execution recorded; versions are never reconstructed from settings.
            "model_versions": stage_exec.model_versions or {},
            "config_hash": stage_exec.config_hash,
            "run_mode": stage_exec.run_mode,
        },
    }
    return view, candidates


def _recompute(db: Session, case_obj: Case, stage_exec: StageExecution) -> Dict[str, Any]:
    """After an edit: the payload from the database, HPF counts stored, and a ``mitosis_count`` record
    superseding the previous one (SPEC-06 AC8). The caller commits."""
    db.flush()
    view, candidates = _stage_view(db, case_obj, stage_exec)
    counts = {h["seq"]: h["count"] for h in view["hpfs"]}
    for row in _hpf_rows(db, case_obj):
        row.mitotic_count = counts[row.seq]
    previous = db.scalars(
        select(DecisionRecord).where(
            DecisionRecord.case_id == case_obj.id,
            DecisionRecord.task == Task.MITOSIS_COUNT.value,
        ).order_by(DecisionRecord.created_at.desc()).limit(1)
    ).first()
    config = get_pipeline_config()
    slide = _slide(db, case_obj)
    db.add(mitosis_count_record(
        case_id=case_obj.id,
        stage_execution_id=stage_exec.id,
        run_id=stage_exec.run_id,
        run_mode=stage_exec.run_mode,
        # The recount is made under the current configuration (its thresholds), so that is its hash.
        config_hash=config.config_hash(),
        slide_id=str(slide.id),
        candidates=candidates,
        hpfs=view["hpfs"],
        summary=view["summary"],
        thresholds=config.mitosis.scoring.thresholds.model_dump(),
        supersedes_id=previous.id if previous else None,
    ))
    return view


def _review_edit(stage_exec: StageExecution, path: str, old: Any, new: Any) -> None:
    edits = list(stage_exec.review_edits or [])
    edits.append({"op": "replace", "path": path, "from": old, "to": new,
                  "timestamp": datetime.now(timezone.utc).isoformat()})
    stage_exec.review_edits = edits


# Request bodies (docs/contracts/mitosis_v6.md)
class ReviewPayload(BaseModel):
    case_id: str
    candidate_id: str
    review_label: Optional[Literal["mitosis", "not_mitosis"]]


class AddPayload(BaseModel):
    case_id: str
    centroid_um: Tuple[float, float]


class CasePayload(BaseModel):
    case_id: str


@router.get("/{case_id}")
def get_mitosis_stage(case_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(require("case:read"))):
    """The ``MitosisStageV6`` payload."""
    case_obj, stage_exec = _case_and_stage(case_id, db)
    view, _ = _stage_view(db, case_obj, stage_exec)
    return view


@router.get("/{case_id}/candidates/{candidate_id}/{kind}")
def get_candidate_image(
    case_id: str,
    candidate_id: str,
    kind: Literal["crop", "context"],
    user: CurrentUser = Depends(require("case:read")),
):
    """A candidate's stored review image: ``crop`` (crop_url) or ``context`` (context_url), PNG, raw colour.

    The worker and ``/add`` write them; a missing image is a 404, never a substitute.
    """
    blob = review_crop_blob(str(to_uuid(case_id)), candidate_id, kind)
    if not blob_exists(settings.GCS_ARTIFACTS_BUCKET, blob):
        raise ContractError(404, "image_not_found", f"No {kind} image for candidate {candidate_id}")
    data = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, blob)
    return Response(content=data, media_type="image/png", headers={"Cache-Control": "private, max-age=31536000, immutable"})


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
    if blob_exists(settings.GCS_ARTIFACTS_BUCKET, hpf_blob):
        hpf_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, hpf_blob)
        media_type = "image/jpeg" if hpf_bytes.startswith(b"\xff\xd8") else "image/png"
        return Response(content=hpf_bytes, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})

    case_uid = to_uuid(case_id)
    slide_obj = db.scalars(select(Slide).where(Slide.case_id == case_uid).limit(1)).first()
    if not slide_obj or not slide_obj.mpp_x or slide_obj.mpp_x <= 0 or not slide_obj.mpp_y or slide_obj.mpp_y <= 0:
        raise HTTPException(status_code=400, detail="Slide is missing valid MPP (status='needs_mpp'). Cannot extract HPF thumbnail.")
    hpf_site = db.scalars(select(HpfSite).where(HpfSite.case_id == case_uid, HpfSite.seq == seq)).first()
    if hpf_site is None:
        raise HTTPException(status_code=404, detail=f"HPF #{seq} not found for case {case_id}")
    cx_um, cy_um = hpf_site.center_um[0], hpf_site.center_um[1]

    # Review image width calibrated to the frontend HPF reticle canvas (r=236 px -> radius_um=262.0)
    hpf_cfg = get_pipeline_config().mitosis.hpf
    field_size_um = hpf_cfg.review_field_um
    target_dim = hpf_cfg.review_px // {"40x": 1, "20x": 2, "10x": 4}[mag]

    # Raw WSI extraction from the cached raw slide, through the slide's persisted stain profile for "norm"
    try:
        stain_transform = slide_stain_transform(db, slide_obj) if stain == "norm" else None
        with open_case_slide(case_id, slide_obj) as reader:
            x_um, y_um = centered_origin_um(reader, cx_um, cy_um, field_size_um, field_size_um)
            region = read_region_at_mpp(
                reader, x_um, y_um, field_size_um, field_size_um, field_size_um / target_dim,
                color="raw" if stain_transform is None else "normalized", stain=stain_transform,
            )
    except PRECONDITION_ERRORS as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (SlideReadError, NotFound, OSError) as exc:
        raise HTTPException(status_code=502, detail=f"HPF #{seq} patch ({mag}, {stain}) could not be read from the slide: {exc}") from exc

    buf = io.BytesIO()
    if mag == "40x":
        Image.fromarray(region.rgb).save(buf, format="JPEG", quality=94)
        media_type = "image/jpeg"
    else:
        Image.fromarray(region.rgb).save(buf, format="PNG")
        media_type = "image/png"
    extracted_bytes = buf.getvalue()
    upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, hpf_blob, extracted_bytes, media_type)
    return Response(content=extracted_bytes, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})


@router.post("/review")
def review_candidate(payload: ReviewPayload, db: Session = Depends(get_db), user: CurrentUser = Depends(require("stage:review"))):
    """Sets or clears a candidate's ``review_label``; counts and score are recomputed server-side."""
    case_obj, stage_exec = _editable(payload.case_id, db)
    det = db.get(Detection, (payload.candidate_id, case_obj.id))
    if det is None:
        raise ContractError(404, "candidate_not_found", f"Candidate {payload.candidate_id} not found")
    old = det.review_label
    if old != payload.review_label:
        det.review_label = payload.review_label
        _review_edit(stage_exec, f"/candidates/{det.id}/review_label", old, payload.review_label)
        db.add(AuditEvent(case_id=str(case_obj.id), actor=user.id, event_type="review_edit", stage="mitosis",
                          payload={"detection_id": det.id, "from": old, "to": payload.review_label}))
    view = _recompute(db, case_obj, stage_exec)
    db.commit()
    return view


@router.post("/add")
def add_candidate(payload: AddPayload, db: Session = Depends(get_db), user: CurrentUser = Depends(require("stage:review"))):
    """A figure the pathologist pins: ``decision_path = 'human'``, ``review_label = 'mitosis'``.

    A pin within the NMS radius of an existing candidate labels that candidate instead, so one
    figure is never two rows (#593).
    """
    case_obj, stage_exec = _editable(payload.case_id, db)
    slide = _slide(db, case_obj)
    cx_um, cy_um = payload.centroid_um
    width_um, height_um = slide.width_px * float(slide.mpp_x), slide.height_px * float(slide.mpp_y)
    if not (0.0 <= cx_um <= width_um and 0.0 <= cy_um <= height_um):
        raise ContractError(422, "out_of_bounds", f"[{cx_um}, {cy_um}] is outside the slide ({width_um:.1f} x {height_um:.1f} µm)")

    cfg = get_pipeline_config().mitosis
    case_id = str(case_obj.id)
    existing = db.scalars(select(Detection).where(Detection.case_id == case_obj.id)).all()
    near = [d for d in existing if math.dist(d.centroid_um, (cx_um, cy_um)) < cfg.detector.nms_radius_um]
    if near:
        det = min(near, key=lambda d: math.dist(d.centroid_um, (cx_um, cy_um)))
        old = det.review_label
        det.review_label = "mitosis"
        _review_edit(stage_exec, f"/candidates/{det.id}/review_label", old, "mitosis")
        db.add(AuditEvent(case_id=case_id, actor=user.id, event_type="review_edit", stage="mitosis",
                          payload={"detection_id": det.id, "from": old, "to": "mitosis", "reason": "added_on_existing_candidate"}))
    else:
        new_id = f"m_user_{uuid.uuid4().hex[:8]}"
        try:
            with open_case_slide(case_id, slide) as reader:
                crops = candidate_review_crops(reader, cx_um, cy_um, cfg.review_crops)
        except PRECONDITION_ERRORS as exc:
            raise ContractError(409, "slide_unavailable", str(exc)) from exc
        except (SlideReadError, NotFound, OSError) as exc:
            raise ContractError(502, "slide_unreadable", f"Could not read the slide for the added figure's images: {exc}") from exc
        try:
            # The gate runs on an added figure too; its review_label 'mitosis' still decides the count.
            in_tumor = load_tumor_gate(case_id, cfg.tumor_gate.dilation_tiles).in_tumor(cx_um, cy_um) if cfg.tumor_gate.enabled else None
        except PRECONDITION_ERRORS as exc:
            raise ContractError(409, "tumor_mask_unavailable", str(exc)) from exc
        upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, review_crop_blob(case_id, new_id, "crop"), crops.crop_png, "image/png")
        upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, review_crop_blob(case_id, new_id, "context"), crops.context_png, "image/png")
        db.add(Detection(
            id=new_id, case_id=case_obj.id, hotspot_id=None, centroid_um=[float(cx_um), float(cy_um)],
            p_a=None, p_b=None, vlm=None, rule_override=False, in_tumor=in_tumor,
            final_decision="mitosis", decision_path="human", review_label="mitosis", record_ids=None,
        ))
        db.add(AuditEvent(case_id=case_id, actor=user.id, event_type="mitosis_added", stage="mitosis",
                          payload={"detection_id": new_id, "centroid_um": [cx_um, cy_um]}))
    view = _recompute(db, case_obj, stage_exec)
    db.commit()
    return view


def invalidate_hpf_cached_thumbnails(case_id: str, seqs) -> None:
    """Deletes the cached review images of HPFs that moved (#107)."""
    for s in seqs:
        for m in ("10x", "20x", "40x"):
            for st in ("norm", "orig"):
                delete_blob(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/mitosis/hpfs/hpf_{s}_{m}_{st}.png")


@router.post("/replace-hpfs")
def replace_hpfs(payload: CasePayload, db: Session = Depends(get_db), user: CurrentUser = Depends(require("stage:review"))):
    """Places the HPFs again from the currently counted candidates: one per hotspot window (``pipeline/hpf.py::place_hpfs``)."""
    case_obj, stage_exec = _editable(payload.case_id, db)
    slide = _slide(db, case_obj)
    case_id = str(case_obj.id)
    hotspots = db.scalars(
        select(Hotspot).where(Hotspot.case_id == case_obj.id, Hotspot.excluded == False)  # noqa: E712
    ).all()
    if not hotspots:
        raise ContractError(409, "no_hotspots", "HPFs are placed inside confirmed hotspots, and this case has none")
    config = get_pipeline_config()
    try:
        tissue = load_tissue_mask(case_id)
        tumor = load_tumor_gate(case_id, config.mitosis.tumor_gate.dilation_tiles)
        hotspot_cfg = config.specimen_profiles.for_type(case_obj.specimen_type).hotspots
    except PRECONDITION_ERRORS as exc:
        raise ContractError(409, "tissue_mask_unavailable", str(exc)) from exc

    old_seqs = [h.seq for h in _hpf_rows(db, case_obj)]
    new_hpfs = place_hpfs(
        _candidates(db, case_obj),
        [(h.polygon_um, float(h.prob_mean or 0.0)) for h in sorted(hotspots, key=lambda h: h.prob_mean or 0.0, reverse=True)],
        tissue=tissue,
        tumor=tumor,
        slide_dimensions_um=(slide.width_px * float(slide.mpp_x), slide.height_px * float(slide.mpp_y)),
        cfg=config.mitosis.hpf,
        min_tissue_fraction=hotspot_cfg.min_tissue_fraction,
        min_tumor_fraction=hotspot_cfg.min_tumor_fraction,
    )
    db.execute(delete(HpfSite).where(HpfSite.case_id == case_obj.id))
    for h in new_hpfs:
        db.add(HpfSite(case_id=case_obj.id, seq=h["seq"], center_um=h["center_um"], radius_um=h["radius_um"],
                       mitotic_count=0, tissue_coverage=h["tissue_coverage"], tumor_fraction=h["tumor_fraction"],
                       source="model"))
    _review_edit(stage_exec, "/hpfs", len(old_seqs), len(new_hpfs))
    db.add(AuditEvent(case_id=case_id, actor=user.id, event_type="review_edit", stage="mitosis",
                      payload={"action": "replace_hpfs", "n_hpf": len(new_hpfs)}))
    invalidate_hpf_cached_thumbnails(case_id, set(old_seqs) | {h["seq"] for h in new_hpfs})
    view = _recompute(db, case_obj, stage_exec)
    db.commit()
    return view


@router.post("/confirm")
def confirm_mitosis_stage(
    payload: CasePayload,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("stage:confirm")),
    _idempotency: IdempotencyContext = idempotent("stages/mitosis/confirm"),
):
    """
    Confirms Stage 4 and queues grading. Refused while an ``equivocal`` candidate inside an HPF has
    no ``review_label`` (``409 equivocal_unreviewed``) or when the stage is not awaiting review.
    """
    try:
        stage_service.confirm_stage(db, payload.case_id, "mitosis", user.id)
    except stage_service.EquivocalUnreviewed as exc:
        raise ContractError(409, "equivocal_unreviewed", exc.detail, ids=exc.ids) from exc
    except stage_service.NotAwaitingReview as exc:
        raise ContractError(409, "not_awaiting_review", exc.detail) from exc
    except stage_service.StageNotFound as exc:
        raise ContractError(404, "not_found", exc.detail) from exc
    except stage_service.StageServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"status": "confirmed", "next_stage": "grading"}
