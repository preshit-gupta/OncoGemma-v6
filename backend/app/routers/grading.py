"""
FastAPI Router for Stage 5: Nottingham Histologic Grading (v4.4).

Provides endpoints for retrieving evidence patches, HPF sites, machine grades,
patch-level & HPF-level explicit clinical review workflows, live debounced recomputation,
patch image streaming, and clinical confirmation gate with mandatory dual-level sign-off.
"""

import os
import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Literal, get_args
from fastapi import APIRouter, Depends, HTTPException, status, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.auth.deps import CurrentUser, require
from app.auth.idempotency import require_idempotency_key, canonical_request_hash, IdempotencyContext
from app.core.gcs import download_blob_as_bytes
from app.core.db import get_db
from app.core.pipeline_config import get_pipeline_config
from app.models.case import Case
from app.models.stage_execution import StageExecution
from app.models.hpf_site import HpfSite
from app.models.detection import Detection
from app.models.grading import Grading
from app.models.audit import AuditEvent
from app.inference.schemas import HistotypeVerdict
from pipeline.grading import (
    calculate_nottingham_grade,
    calculate_tubule_score,
    calculate_mitotic_score_from_hpfs,
    calculate_mitotic_score_from_detections_and_hpfs,
    aggregate_grading_findings,
    validate_grading_invariants,
)

router = APIRouter(prefix="/api/v1/stages/grading", tags=["grading"])


# ---------------------------------------------------------------------------
# Pydantic Request / Response Schemas
# ---------------------------------------------------------------------------

VALID_OVERRIDE_COMPONENTS = {"tubule", "pleo", "mitotic", "patches", "hpfs"}
ALLOWED_CONFIRM_OVERRIDE_COMPONENTS = {"tubule", "pleo", "mitotic"}


# Every type the estimator can propose (HistotypeVerdict) plus the pathologist-only ones.
VALID_HISTOLOGIC_TYPES = set(get_args(HistotypeVerdict.model_fields["type"].annotation)) | {
    "apocrine", "medullary_features"
}

class ScoreOverrideItem(BaseModel):
    score: Optional[int] = Field(None, ge=1, le=3)
    percent: Optional[float] = Field(None, ge=0.0, le=100.0)
    justification: Optional[str] = None
    original_score: Optional[int] = None
    overridden_at: Optional[str] = None


class ConfirmHistologicTypePayload(BaseModel):
    case_id: str
    histologic_type: str
    justification: Optional[str] = None


class SinglePatchReview(BaseModel):
    patch_id: str
    tubule_percent: Optional[int] = Field(None, ge=0, le=100)
    tumor_present: Optional[bool] = None
    pleomorphism_score: Optional[int] = Field(None, ge=1, le=3)
    status: Literal["suggested", "approved", "modified"] = "approved"
    notes: Optional[str] = None


class PatchReviewPayload(BaseModel):
    case_id: str
    action: Literal["update", "approve_all", "reset_all"] = "update"
    reviews: List[SinglePatchReview] = Field(default_factory=list)


class SingleHpfReview(BaseModel):
    seq: int = Field(ge=1, le=10)
    mitotic_count: Optional[int] = Field(None, ge=0)
    status: Literal["suggested", "approved", "modified"] = "approved"
    notes: Optional[str] = None


class HpfReviewPayload(BaseModel):
    case_id: str
    action: Literal["update", "approve_all", "reset_all"] = "update"
    reviews: List[SingleHpfReview] = Field(default_factory=list)


class RecomputeGradePayload(BaseModel):
    case_id: str
    tubule_score: Optional[int] = Field(None, ge=1, le=3)
    tubule_percent: Optional[float] = None
    pleo_score: Optional[int] = Field(None, ge=1, le=3)
    mitotic_score: Optional[int] = Field(None, ge=1, le=3)


class ConfirmGradingPayload(BaseModel):
    case_id: str
    # None keeps the type already confirmed; there is no default type.
    histologic_type: Optional[str] = None
    type_confirmed: bool = Field(default=False, description="Mandatory confirmation gate")
    overrides: Dict[str, Any] = Field(default_factory=dict)
    tubule_score: int = Field(ge=1, le=3)
    tubule_percent: Optional[float] = None
    pleo_score: int = Field(ge=1, le=3)
    mitotic_score: int = Field(ge=1, le=3)
    nottingham_sum: int = Field(ge=3, le=9)
    grade: int = Field(ge=1, le=3)

    @field_validator("overrides")
    @classmethod
    def validate_overrides_dict(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(v, dict):
            raise ValueError("Overrides must be a dictionary.")
        for k, item in v.items():
            if k not in ALLOWED_CONFIRM_OVERRIDE_COMPONENTS:
                raise ValueError(
                    f"Unknown or unauthorized override component '{k}'. Confirmation overrides are restricted to {sorted(ALLOWED_CONFIRM_OVERRIDE_COMPONENTS)} to protect stored patch and HPF reviews."
                )
            if not isinstance(item, dict):
                raise ValueError(f"Override for '{k}' must be a dictionary.")
            ScoreOverrideItem.model_validate(item)
        return v


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def to_uuid(val: Any) -> uuid.UUID:
    if isinstance(val, uuid.UUID):
        return val
    try:
        return uuid.UUID(str(val))
    except Exception:
        return val


def _get_case_report_status(db: Session, case_uid: uuid.UUID) -> Optional[str]:
    return None


def _is_case_report_signed(db: Session, case_uid: uuid.UUID) -> bool:
    return False



def _build_grading_stage_data_dict(
    case_id: str,
    case: Case,
    stage_exec: Optional[StageExecution],
    grading_record: Optional[Grading],
    db: Session
) -> Dict[str, Any]:
    case_uid = to_uuid(case_id)
    config = get_pipeline_config()
    scoring_cfg, mitotic_scoring = config.scoring, config.mitosis.scoring

    if not grading_record or not grading_record.machine:
        hpf_sites = list(db.scalars(select(HpfSite).where(HpfSite.case_id == case_uid)).all())
        confirmed_dets = list(db.scalars(select(Detection).where(Detection.case_id == case_uid, Detection.label == "mitosis")).all())
        # No mitotic score until Stage 4 has HPFs or an output with a score.
        m_score = None
        total_mitoses = 0
        if hpf_sites and confirmed_dets:
            cands_for_score = [{"id": d.id, "centroid_um": d.centroid_um, "label": "mitosis"} for d in confirmed_dets]
            hpfs_for_score = [{"seq": h.seq, "center_um": h.center_um, "radius_um": h.radius_um, "count": 0} for h in hpf_sites]
            total_mitoses, m_score = calculate_mitotic_score_from_detections_and_hpfs(
                cands_for_score, hpfs_for_score, mitotic_scoring
            )
        elif hpf_sites:
            hpf_counts = [h.mitotic_count for h in hpf_sites]
            total_mitoses, m_score = calculate_mitotic_score_from_hpfs(
                hpf_counts, mitotic_scoring, radius_um=float(hpf_sites[0].radius_um)
            )
        else:
            try:
                m_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/mitosis/output.json")
                m_data = json.loads(m_bytes.decode("utf-8"))
                if "summary" in m_data and "mitotic_score" in m_data["summary"]:
                    m_score = m_data["summary"]["mitotic_score"]
                    total_mitoses = m_data["summary"].get("total_mitoses", 0)
            except Exception:
                m_score = None
                total_mitoses = 0


        return {
            "case_id": str(case_id),
            "status": stage_exec.status if stage_exec else "not_started",
            "can_confirm": False,
            "mitotic_summary": {
                "total_mitoses": total_mitoses,
                "mitotic_score": m_score,
                "evaluated_hpfs": len(hpf_sites)
            },
            "patches": [],
            "hpfs": [],
            "review_summary": {
                "total_patches": 0,
                "approved_patches": 0,
                "all_patches_reviewed": False,
                "total_hpfs": len(hpf_sites),
                "approved_hpfs": 0,
                "all_hpfs_reviewed": False,
                "is_type_confirmed": False,
                "can_confirm": False
            },
            "aggregate": None,
            "overrides": {},
            "grade": None
        }

    machine_data = grading_record.machine
    overrides = grading_record.overrides or {}
    patch_overrides = overrides.get("patches", {})
    hpf_overrides = overrides.get("hpfs", {})

    # 1. Merge Patch Reviews
    raw_patches = machine_data.get("patches", [])
    merged_patches = []
    for p in raw_patches:
        p_id = p["id"]
        p_copy = dict(p)
        if p_id in patch_overrides:
            ovr = patch_overrides[p_id]
            p_copy["review_status"] = ovr.get("status", "approved")
            p_copy["user_tubule_percent"] = ovr.get("tubule_percent")
            p_copy["user_tumor_present"] = ovr.get("tumor_present")
            p_copy["user_pleo_score"] = ovr.get("pleomorphism_score")
            p_copy["user_notes"] = ovr.get("notes")
            p_copy["reviewed_by"] = ovr.get("reviewed_by")
            p_copy["reviewed_at"] = ovr.get("reviewed_at")
        else:
            p_copy["review_status"] = p.get("review_status", "suggested")
            p_copy["user_tubule_percent"] = None
            p_copy["user_tumor_present"] = None
            p_copy["user_pleo_score"] = None
            p_copy["user_notes"] = None
            p_copy["reviewed_by"] = None
            p_copy["reviewed_at"] = None
        merged_patches.append(p_copy)

    total_patches = len(merged_patches)
    approved_patches = sum(1 for p in merged_patches if p["review_status"] in ("approved", "modified"))
    all_patches_reviewed = (approved_patches == total_patches and total_patches > 0)

    # 2. Merge HPF Reviews
    raw_hpfs = machine_data.get("hpfs", [])
    if not raw_hpfs:
        db_hpfs = list(db.scalars(select(HpfSite).where(HpfSite.case_id == case_uid)).all())
        if db_hpfs:
            raw_hpfs = []
            for h in sorted(db_hpfs, key=lambda x: getattr(x, "seq", 0)):
                cnt = getattr(h, "mitotic_count", getattr(h, "mitotic_figure_count", 0))
                r_um = float(getattr(h, "radius_um", 262.0) or 262.0)
                h_area = math.pi * (r_um / 1000.0) ** 2
                raw_hpfs.append({
                    "seq": h.seq,
                    "center_um": h.center_um if isinstance(h.center_um, list) else [0, 0],
                    "radius_um": r_um,
                    "mitotic_count": cnt,
                    "density_mm2": round(cnt / h_area, 1) if h_area > 0 else 0.0,
                    "review_status": "suggested"
                })
        else:
            raw_hpfs = []

    merged_hpfs = []
    for h in raw_hpfs:
        h_seq_key = str(h["seq"])
        h_copy = dict(h)
        ovr = hpf_overrides.get(h_seq_key) or hpf_overrides.get(h["seq"])
        if ovr:
            h_copy["review_status"] = ovr.get("status", "approved")
            h_copy["user_notes"] = ovr.get("notes")
            h_copy["reviewed_by"] = ovr.get("reviewed_by")
            h_copy["reviewed_at"] = ovr.get("reviewed_at")
        else:
            h_copy["review_status"] = h.get("review_status", "suggested")
            h_copy["user_notes"] = None
            h_copy["reviewed_by"] = None
            h_copy["reviewed_at"] = None
        merged_hpfs.append(h_copy)

    total_hpfs = len(merged_hpfs)
    approved_hpfs = sum(1 for h in merged_hpfs if h["review_status"] in ("approved", "modified"))
    all_hpfs_reviewed = (approved_hpfs == total_hpfs and total_hpfs > 0)

    # 3. Dynamic Zero-LLM Aggregation from Reviewed Dataset
    if merged_hpfs:
        hpf_counts = [
            h["user_mitotic_count"] if h.get("user_mitotic_count") is not None else h.get("mitotic_count", 0)
            for h in merged_hpfs
        ]
        tot_mitoses, calc_mitotic_score = calculate_mitotic_score_from_hpfs(
            hpf_counts, mitotic_scoring, radius_um=float(merged_hpfs[0]["radius_um"])
        )
    else:
        # Without HPFs the stored score stands; with none, the grade needs a human (never a default).
        tot_mitoses = 0
        calc_mitotic_score = grading_record.mitotic_score

    tubule_dicts = [
        {
            **p["tubule"],
            "user_tubule_percent": p.get("user_tubule_percent"),
            "user_tumor_present": p.get("user_tumor_present")
        }
        for p in merged_patches
    ]
    pleo_dicts = [
        {
            **p["pleo"],
            "user_pleo_score": p.get("user_pleo_score")
        }
        for p in merged_patches
    ]

    dyn_agg = aggregate_grading_findings(
        tubule_responses=tubule_dicts,
        pleo_responses=pleo_dicts,
        mitotic_score=calc_mitotic_score,
        cfg=scoring_cfg
    )

    # 4. Top-level overrides (if manually set) - Safely typed parsing
    def _safe_override_score(comp: str, default: int) -> int:
        ovr = overrides.get(comp)
        if isinstance(ovr, dict):
            val = ovr.get("score")
            if val in (1, 2, 3, "1", "2", "3"):
                try:
                    return int(val)
                except Exception:
                    pass
        return default

    def _safe_override_percent(comp: str, default: Optional[float]) -> Optional[float]:
        ovr = overrides.get(comp)
        if isinstance(ovr, dict):
            val = ovr.get("percent")
            if val is not None:
                try:
                    pct = float(val)
                    if 0.0 <= pct <= 100.0:
                        return pct
                except Exception:
                    pass
        return default

    eff_tubule_score = _safe_override_score("tubule", dyn_agg["tubule_score"])
    eff_tubule_percent = _safe_override_percent("tubule", dyn_agg["tubule_percent"])
    eff_pleo_score = _safe_override_score("pleo", dyn_agg["pleo_score"])
    eff_mitotic_score = _safe_override_score("mitotic", calc_mitotic_score)

    if eff_tubule_score is not None and eff_pleo_score is not None and eff_mitotic_score is not None:
        eff_sum, eff_grade = calculate_nottingham_grade(eff_tubule_score, eff_pleo_score, eff_mitotic_score, scoring_cfg)
    else:
        eff_sum, eff_grade = None, None

    report_status = _get_case_report_status(db, case_uid)
    is_signed = report_status in ("signed", "amended") if report_status else False

    is_type_confirmed = grading_record.type_confirmed_by != "unconfirmed"
    can_confirm = (
        all_patches_reviewed
        and all_hpfs_reviewed
        and is_type_confirmed
        and not is_signed
        and (stage_exec is None or stage_exec.status != "confirmed")
    )

    return {
        "case_id": str(case_id),
        "slide_id": str(case.slides[0].id) if case.slides else None,
        "status": stage_exec.status if stage_exec else "awaiting_review",
        "is_signed": is_signed,
        "can_confirm": can_confirm,
        "report_status": report_status,
        "patches": merged_patches,
        "hpfs": merged_hpfs,
        "review_summary": {
            "total_patches": total_patches,
            "approved_patches": approved_patches,
            "all_patches_reviewed": all_patches_reviewed,
            "total_hpfs": total_hpfs,
            "approved_hpfs": approved_hpfs,
            "all_hpfs_reviewed": all_hpfs_reviewed,
            "is_type_confirmed": is_type_confirmed,
            "can_confirm": can_confirm
        },
        "machine": {
            "tubule_percent": grading_record.tubule_percent,
            "tubule_score": grading_record.tubule_score,
            "pleo_score": grading_record.pleo_score,
            "mitotic_score": grading_record.mitotic_score,
            "nottingham_sum": grading_record.nottingham_sum,
            "grade": grading_record.grade,
            "flags": dyn_agg.get("flags", [])
        },
        "current": {
            "tubule_score": eff_tubule_score,
            "tubule_percent": eff_tubule_percent,
            "pleo_score": eff_pleo_score,
            "mitotic_score": eff_mitotic_score,
            "nottingham_sum": eff_sum,
            "grade": eff_grade,
            "is_overridden": bool(overrides)
        },
        # No proposal when the estimate failed: an unassessed type never reads as IDC-NST.
        "histologic_type": {
            "proposed_type": (machine_data.get("histologic_type") or {}).get("type"),
            "differential": (machine_data.get("histologic_type") or {}).get("differential", []),
            "rationale": (machine_data.get("histologic_type") or {}).get("rationale"),
            "confidence": (machine_data.get("histologic_type") or {}).get("confidence"),
            "confirmed_type": grading_record.histologic_type,
            "type_confirmed_by": grading_record.type_confirmed_by,
            "is_confirmed": is_type_confirmed
        },
        "narrative": machine_data.get("narrative", ""),
        "overrides": overrides,
        "mitotic_summary": {
            "total_mitoses": tot_mitoses,
            "mitotic_score": eff_mitotic_score,
            "evaluated_hpfs": total_hpfs
        },
        "model_versions": machine_data.get("model_versions", {})
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/{case_id}")
def get_grading_stage_data(case_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(require("case:read"))):
    """
    Retrieve full Stage 5 Grading data: 24 evidence patches with review state,
    10 HPF sites with review state, sub-scores, active overrides, and live calculated grade.
    """
    case_uid = to_uuid(case_id)
    case = db.scalars(select(Case).where(Case.id == case_uid)).first()
    if not case:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    stage_exec = db.scalars(
        select(StageExecution)
        .where(StageExecution.case_id == case_uid, StageExecution.stage == "grading")
        .order_by(StageExecution.attempt.desc())
    ).first()

    grading_record = db.scalars(
        select(Grading).where(Grading.case_id == case_uid)
    ).first()

    return _build_grading_stage_data_dict(case_id, case, stage_exec, grading_record, db)


@router.post("/patches/review")
def review_grading_patches(payload: PatchReviewPayload, db: Session = Depends(get_db), user: CurrentUser = Depends(require("stage:review"))):
    """
    Explicit Patch-Level Review endpoint:
    Allows approving individual patches, modifying per-patch tubule % / pleo score,
    or 1-click bulk approving all 24 patches. Dynamically re-aggregates Nottingham parameters.
    """
    case_uid = to_uuid(payload.case_id)
    case = db.scalars(select(Case).where(Case.id == case_uid)).first()
    if not case:
        raise HTTPException(status_code=404, detail=f"Case {payload.case_id} not found")

    grading_record = db.scalars(select(Grading).where(Grading.case_id == case_uid)).first()
    if not grading_record:
        raise HTTPException(status_code=404, detail="Grading record for case not found")

    stage_exec = db.scalars(
        select(StageExecution)
        .where(StageExecution.case_id == case_uid, StageExecution.stage == "grading")
        .order_by(StageExecution.attempt.desc())
    ).first()

    if stage_exec and stage_exec.status == "confirmed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot modify patch reviews: Grading stage is already confirmed."
        )

    if _is_case_report_signed(db, case_uid):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot modify patch reviews for a signed or amended case report."
        )

    current_overrides = dict(grading_record.overrides or {})
    patch_overrides = dict(current_overrides.get("patches", {}))
    machine_patches = grading_record.machine.get("patches", [])

    now_iso = datetime.now(timezone.utc).isoformat()
    edits = list(stage_exec.review_edits or []) if stage_exec else []

    if payload.action == "approve_all":
        # Bulk approve all patches
        for p in machine_patches:
            p_id = p["id"]
            existing = patch_overrides.get(p_id, {})
            patch_overrides[p_id] = {
                **existing,
                "status": "approved",
                "reviewed_by": user.id,
                "reviewed_at": now_iso
            }
        edits.append({
            "op": "replace",
            "path": "/patches",
            "value": "approve_all",
            "timestamp": now_iso,
            "actor": user.id
        })
    elif payload.action == "reset_all":
        patch_overrides = {}
        edits.append({
            "op": "replace",
            "path": "/patches",
            "value": {},
            "timestamp": now_iso,
            "actor": user.id
        })
    elif payload.action == "update":
        for r in payload.reviews:
            p_id = r.patch_id
            patch_overrides[p_id] = {
                "status": r.status,
                "tubule_percent": r.tubule_percent,
                "tumor_present": r.tumor_present,
                "pleomorphism_score": r.pleomorphism_score,
                "notes": r.notes,
                "reviewed_by": user.id,
                "reviewed_at": now_iso
            }
            edits.append({
                "op": "replace",
                "path": f"/patches/{p_id}",
                "value": patch_overrides[p_id],
                "timestamp": now_iso,
                "actor": user.id
            })

    current_overrides["patches"] = patch_overrides
    grading_record.overrides = current_overrides
    if stage_exec:
        stage_exec.review_edits = edits

    # Record Audit Event
    audit_evt = AuditEvent(
        case_id=str(payload.case_id),
        actor=user.id,
        event_type="review_edit",
        stage="grading",
        payload={
            "scope": "patches",
            "action": payload.action,
            "reviewed_count": len(payload.reviews) if payload.action == "update" else len(machine_patches)
        }
    )
    db.add(audit_evt)
    db.commit()
    db.refresh(grading_record)

    return _build_grading_stage_data_dict(payload.case_id, case, stage_exec, grading_record, db)


@router.post("/hpfs/review")
def review_grading_hpfs(payload: HpfReviewPayload, db: Session = Depends(get_db), user: CurrentUser = Depends(require("stage:review"))):
    """
    Explicit HPF-Level Review endpoint:
    Allows approving individual HPF fields or 1-click bulk approving all 10 HPFs.
    Mitotic counts are read-only in Stage 5; revisions must be performed via Stage 4 Mitosis.
    """
    case_uid = to_uuid(payload.case_id)
    case = db.scalars(select(Case).where(Case.id == case_uid)).first()
    if not case:
        raise HTTPException(status_code=404, detail=f"Case {payload.case_id} not found")

    grading_record = db.scalars(select(Grading).where(Grading.case_id == case_uid)).first()
    if not grading_record:
        raise HTTPException(status_code=404, detail="Grading record for case not found")

    stage_exec = db.scalars(
        select(StageExecution)
        .where(StageExecution.case_id == case_uid, StageExecution.stage == "grading")
        .order_by(StageExecution.attempt.desc())
    ).first()

    if stage_exec and stage_exec.status == "confirmed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot modify HPF reviews: Grading stage is already confirmed."
        )

    if _is_case_report_signed(db, case_uid):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot modify HPF reviews for a signed or amended case report."
        )

    current_overrides = dict(grading_record.overrides or {})
    hpf_overrides = dict(current_overrides.get("hpfs", {}))
    machine_hpfs = grading_record.machine.get("hpfs", [])
    if not machine_hpfs:
        db_hpfs = list(db.scalars(select(HpfSite).where(HpfSite.case_id == case_uid)).all())
        if db_hpfs:
            machine_hpfs = [{"seq": h.seq} for h in db_hpfs]
        else:
            machine_hpfs = []

    if not machine_hpfs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot review HPFs: No HPF sites found for this case. Stage 4 Mitosis must be completed first."
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    edits = list(stage_exec.review_edits or []) if stage_exec else []

    if payload.action == "approve_all":
        # Bulk approve all HPFs
        for h in machine_hpfs:
            h_seq = str(h["seq"])
            existing = hpf_overrides.get(h_seq, {})
            hpf_overrides[h_seq] = {
                **existing,
                "status": "approved",
                "reviewed_by": user.id,
                "reviewed_at": now_iso
            }
        edits.append({
            "op": "replace",
            "path": "/hpfs",
            "value": "approve_all",
            "timestamp": now_iso,
            "actor": user.id
        })
    elif payload.action == "reset_all":
        hpf_overrides = {}
        edits.append({
            "op": "replace",
            "path": "/hpfs",
            "value": {},
            "timestamp": now_iso,
            "actor": user.id
        })
    elif payload.action == "update":
        for r in payload.reviews:
            if r.mitotic_count is not None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Mitotic figure counts cannot be modified directly in Stage 5. Please reopen Stage 4 Mitosis review to adjust mitotic counts."
                )
            h_seq = str(r.seq)
            existing = hpf_overrides.get(h_seq, {})
            hpf_overrides[h_seq] = {
                **existing,
                "status": r.status,
                "notes": r.notes,
                "reviewed_by": user.id,
                "reviewed_at": now_iso
            }
            edits.append({
                "op": "replace",
                "path": f"/hpfs/{h_seq}",
                "value": hpf_overrides[h_seq],
                "timestamp": now_iso,
                "actor": user.id
            })

    current_overrides["hpfs"] = hpf_overrides
    grading_record.overrides = current_overrides
    if stage_exec:
        stage_exec.review_edits = edits

    # Record Audit Event
    audit_evt = AuditEvent(
        case_id=str(payload.case_id),
        actor=user.id,
        event_type="review_edit",
        stage="grading",
        payload={
            "scope": "hpfs",
            "action": payload.action,
            "reviewed_count": len(payload.reviews) if payload.action == "update" else len(machine_hpfs)
        }
    )
    db.add(audit_evt)
    db.commit()
    db.refresh(grading_record)

    return _build_grading_stage_data_dict(payload.case_id, case, stage_exec, grading_record, db)


@router.get("/{case_id}/patches/{patch_id}/image")
def get_patch_image(case_id: str, patch_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(require("case:read"))):
    """
    Stream the 512x512 normalized evidence patch PNG directly from GCS.
    Worker is the authoritative producer of evidence patches (#147).
    """
    blob_name = f"cases/{case_id}/grading_patches/{patch_id}.png"
    try:
        patch_bytes = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, blob_name)
        return Response(content=patch_bytes, media_type="image/png")
    except Exception:
        pass

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Grading patch image '{patch_id}' not found for case {case_id}."
    )



@router.post("/recompute")
def recompute_grade_preview(payload: RecomputeGradePayload, db: Session = Depends(get_db), user: CurrentUser = Depends(require("stage:review"))):
    """
    Live debounced in-memory preview of Nottingham Sum and Grade (<10ms execution).
    """
    case_uid = to_uuid(payload.case_id)
    grading_record = db.scalars(select(Grading).where(Grading.case_id == case_uid)).first()

    t_score = payload.tubule_score or (grading_record.tubule_score if grading_record else None)
    p_score = payload.pleo_score or (grading_record.pleo_score if grading_record else None)
    m_score = payload.mitotic_score or (grading_record.mitotic_score if grading_record else None)
    missing = [name for name, score in (("tubule", t_score), ("pleo", p_score), ("mitotic", m_score)) if score is None]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot preview a grade without the {', '.join(missing)} score(s); none is assumed.",
        )

    scoring_cfg = get_pipeline_config().scoring
    nottingham_sum, grade = calculate_nottingham_grade(t_score, p_score, m_score, scoring_cfg)
    validate_grading_invariants(t_score, p_score, m_score, nottingham_sum, grade, scoring_cfg)

    is_overridden = False
    if grading_record:
        if t_score != grading_record.tubule_score or p_score != grading_record.pleo_score or m_score != grading_record.mitotic_score:
            is_overridden = True

    return {
        "tubule_score": t_score,
        "pleo_score": p_score,
        "mitotic_score": m_score,
        "nottingham_sum": nottingham_sum,
        "grade": grade,
        "is_overridden": is_overridden
    }


@router.post("/type/confirm")
@router.post("/{case_id}/type/confirm")
def confirm_histologic_type(
    payload: ConfirmHistologicTypePayload,
    idempotency_key: str = Depends(require_idempotency_key),
    current_user: CurrentUser = Depends(require("stage:confirm")),
    db: Session = Depends(get_db)
):
    """
    Dedicated server-side histologic subtype confirmation action.
    Validates against approved CAP subtype ontology and stamps the pathologist actor.
    """
    req_data = payload.model_dump(mode="json")
    req_hash = canonical_request_hash(req_data)
    actor = current_user.id if hasattr(current_user, "id") else getattr(current_user, "username", "pathologist")
    idem_ctx = None
    if isinstance(idempotency_key, str) and idempotency_key.strip():
        idem_ctx = IdempotencyContext(idempotency_key, str(actor), "/api/v1/stages/grading/type/confirm", req_hash, db)
        is_cached, cached_body, cached_status = idem_ctx.check()
        if is_cached:
            return JSONResponse(content=cached_body, status_code=cached_status or status.HTTP_200_OK, headers={"Idempotent-Replay": "true"})

    if payload.histologic_type not in VALID_HISTOLOGIC_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid histologic type '{payload.histologic_type}'. Must be one of {sorted(VALID_HISTOLOGIC_TYPES)}."
        )

    case_uid = to_uuid(payload.case_id)
    grading_record = db.scalars(select(Grading).where(Grading.case_id == case_uid)).first()
    if not grading_record:
        raise HTTPException(status_code=404, detail=f"Grading record for case {payload.case_id} not found")

    case = db.scalars(select(Case).where(Case.id == case_uid)).first()
    if not case:
        raise HTTPException(status_code=404, detail=f"Case {payload.case_id} not found")

    stage_exec = db.scalars(
        select(StageExecution)
        .where(StageExecution.case_id == case_uid, StageExecution.stage == "grading")
        .order_by(StageExecution.attempt.desc())
    ).first()

    if stage_exec and stage_exec.status == "confirmed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot modify histologic subtype: Grading stage is already confirmed."
        )

    if _is_case_report_signed(db, case_uid):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot modify histologic subtype for a signed or amended case report."
        )

    actor = str(actor)
    grading_record.histologic_type = payload.histologic_type
    grading_record.type_confirmed_by = actor

    audit_ev = AuditEvent(
        case_id=str(payload.case_id),
        actor=actor,
        event_type="histologic_type_confirmed",
        stage="grading",
        payload={
            "histologic_type": payload.histologic_type,
            "justification": payload.justification
        }
    )
    db.add(audit_ev)
    db.commit()
    db.refresh(grading_record)

    stage_exec = db.scalars(
        select(StageExecution)
        .where(StageExecution.case_id == case_uid, StageExecution.stage == "grading")
        .order_by(StageExecution.attempt.desc())
    ).first()
    resp_payload = _build_grading_stage_data_dict(payload.case_id, case, stage_exec, grading_record, db)
    if idem_ctx:
        idem_ctx.complete(resp_payload, status.HTTP_200_OK)
    return resp_payload


@router.post("/confirm")
def confirm_grading_stage(
    payload: ConfirmGradingPayload,
    idempotency_key: str = Depends(require_idempotency_key),
    current_user: CurrentUser = Depends(require("stage:confirm")),
    db: Session = Depends(get_db)
):
    """
    Clinical Confirmation Gate for Stage 5 (Nottingham Grading).
    Strictly enforces:
    1. Role authorization (pathologist or admin).
    2. Stage execution state gating & report non-signed state.
    3. Mandatory Histologic Type server-side confirmation gate.
    4. Mandatory Patch-Level Review Gate (all patches reviewed & approved).
    5. Mandatory HPF-Level Review Gate (all HPFs reviewed & approved).
    6. Mandatory >=10 char override justification for any subscore diverging from server review.
    7. Server-side authoritative recomputation of Nottingham Sum & Grade.
    8. Pure code mathematical invariants validation.
    Persists final state to DB and marks case done.
    """
    req_data = payload.model_dump(mode="json")
    req_hash = canonical_request_hash(req_data)
    actor = current_user.id if hasattr(current_user, "id") else getattr(current_user, "username", getattr(payload, "reviewed_by", "pathologist"))
    idem_ctx = None
    if isinstance(idempotency_key, str) and idempotency_key.strip():
        idem_ctx = IdempotencyContext(idempotency_key, str(actor), "/api/v1/stages/grading/confirm", req_hash, db)
        is_cached, cached_body, cached_status = idem_ctx.check()
        if is_cached:
            return JSONResponse(content=cached_body, status_code=cached_status or status.HTTP_200_OK, headers={"Idempotent-Replay": "true"})

    actor = str(actor)
    case_id = payload.case_id
    case_uid = to_uuid(case_id)

    case = db.scalars(select(Case).where(Case.id == case_uid)).first()
    if not case:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    grading_record = db.scalars(select(Grading).where(Grading.case_id == case_uid)).first()
    if not grading_record:
        raise HTTPException(status_code=404, detail="Grading record for case not found")

    stage_exec = db.scalars(
        select(StageExecution)
        .where(StageExecution.case_id == case_uid, StageExecution.stage == "grading")
        .order_by(StageExecution.attempt.desc())
    ).first()
    if not stage_exec:
        raise HTTPException(status_code=404, detail="Grading stage execution record not found for case")

    if stage_exec.status in ("running", "failed"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot confirm grading stage while status is '{stage_exec.status}'."
        )

    if stage_exec.status == "confirmed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Grading stage is already confirmed."
        )

    # 1. Mandatory Histologic Type Confirmation Gate
    if payload.histologic_type is None:
        if grading_record.type_confirmed_by == "unconfirmed" or grading_record.histologic_type is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Clinical Confirmation Gate: Histologic Type must be explicitly confirmed by the pathologist before confirming grading."
            )
    elif payload.histologic_type not in VALID_HISTOLOGIC_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid histologic type '{payload.histologic_type}'. Must be one of {sorted(VALID_HISTOLOGIC_TYPES)}."
        )

    if payload.histologic_type is None:
        pass
    elif grading_record.type_confirmed_by == "unconfirmed":
        if payload.type_confirmed:
            grading_record.histologic_type = payload.histologic_type
            grading_record.type_confirmed_by = actor
            audit_htype = AuditEvent(
                case_id=str(case_id),
                actor=actor,
                event_type="histologic_type_confirmed",
                stage="grading",
                payload={"histologic_type": payload.histologic_type, "confirmed_via": "grading_confirmation"}
            )
            db.add(audit_htype)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Clinical Confirmation Gate: Histologic Type must be explicitly confirmed by the pathologist before confirming grading."
            )
    else:
        if payload.histologic_type != grading_record.histologic_type:
            grading_record.histologic_type = payload.histologic_type
            grading_record.type_confirmed_by = actor

    # 1.5 Validate consistency between tubule_percent and tubule_score (#613)
    scoring_cfg = get_pipeline_config().scoring
    eff_tubule_pct = payload.tubule_percent
    if eff_tubule_pct is None and isinstance(payload.overrides.get("tubule"), dict):
        eff_tubule_pct = payload.overrides["tubule"].get("percent")

    if eff_tubule_pct is not None:
        expected_tubule_score = calculate_tubule_score(eff_tubule_pct, scoring_cfg)
        if expected_tubule_score != payload.tubule_score:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Inconsistent tubule parameters: tubule_percent {eff_tubule_pct}% calculates to score {expected_tubule_score}, which contradicts provided tubule_score {payload.tubule_score}."
            )

    # Build current review state to verify review gates and server-derived subscores
    current_data = _build_grading_stage_data_dict(case_id, case, stage_exec, grading_record, db)
    rev_summary = current_data["review_summary"]

    # 2. Mandatory Patch-Level Review Gate
    if not rev_summary["all_patches_reviewed"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Clinical Confirmation Gate: All {rev_summary['total_patches']} image patches must be explicitly reviewed and approved at the patch level before confirming grading (currently {rev_summary['approved_patches']}/{rev_summary['total_patches']} approved)."
        )

    # 3. Mandatory HPF-Level Review Gate
    if not rev_summary["all_hpfs_reviewed"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Clinical Confirmation Gate: All {rev_summary['total_hpfs']} High-Power Fields (HPFs) must be explicitly reviewed and approved at the HPF level before confirming grading (currently {rev_summary['approved_hpfs']}/{rev_summary['total_hpfs']} approved)."
        )

    # 4. Mandatory Justification for Divergent Subscores & Overrides (min 10 chars)
    srv_current = current_data["current"]
    required_overrides_to_audit = []

    for comp_name, payload_score, srv_score in [
        ("tubule", payload.tubule_score, srv_current["tubule_score"]),
        ("pleo", payload.pleo_score, srv_current["pleo_score"]),
        ("mitotic", payload.mitotic_score, srv_current["mitotic_score"]),
    ]:
        if payload_score != srv_score:
            comp_ovr = payload.overrides.get(comp_name, {})
            justification = comp_ovr.get("justification", "").strip() if isinstance(comp_ovr, dict) else ""
            if len(justification) < 10:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Clinical Safety Requirement: Score override for '{comp_name}' (from {srv_score} to {payload_score}) requires a minimum 10-character justification (got {len(justification)} characters)."
                )
            required_overrides_to_audit.append({
                "component": comp_name,
                "from_score": srv_score,
                "to_score": payload_score,
                "justification": justification
            })

    # Validate justifications on any other override items provided
    for comp_name, override_info in payload.overrides.items():
        if comp_name in ("patches", "hpfs"):
            continue
        justification = override_info.get("justification", "").strip() if isinstance(override_info, dict) else ""
        if len(justification) < 10:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Clinical Safety Requirement: Score override for '{comp_name}' requires a minimum 10-character justification (got {len(justification)} characters)."
            )
        if not any(item["component"] == comp_name for item in required_overrides_to_audit):
            required_overrides_to_audit.append({
                "component": comp_name,
                "from_score": override_info.get("original_score"),
                "to_score": override_info.get("score"),
                "justification": justification
            })

    # 5. Authoritative Server-Side Recompute & Pure Code Invariant Check
    computed_sum, computed_grade = calculate_nottingham_grade(
        payload.tubule_score, payload.pleo_score, payload.mitotic_score, scoring_cfg
    )

    try:
        validate_grading_invariants(
            tubule_score=payload.tubule_score,
            pleo_score=payload.pleo_score,
            mitotic_score=payload.mitotic_score,
            nottingham_sum=computed_sum,
            grade=computed_grade,
            cfg=scoring_cfg
        )
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))

    # 6. Update Database Grading Record with Tubule Consistency Validation (#613)
    grading_record.tubule_score = payload.tubule_score
    if payload.tubule_percent is not None:
        expected_tubule_score = calculate_tubule_score(payload.tubule_percent, scoring_cfg)
        if expected_tubule_score != payload.tubule_score:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Inconsistent tubule parameters: tubule_percent {payload.tubule_percent}% calculates to score {expected_tubule_score}, which contradicts provided tubule_score {payload.tubule_score}."
            )
        grading_record.tubule_percent = payload.tubule_percent
    else:
        # If tubule_score diverges from machine-derived score and no percent is provided, null out tubule_percent
        if grading_record.tubule_score != payload.tubule_score:
            grading_record.tubule_percent = None

    grading_record.pleo_score = payload.pleo_score
    grading_record.mitotic_score = payload.mitotic_score
    grading_record.nottingham_sum = computed_sum
    grading_record.grade = computed_grade
    if payload.histologic_type is not None:
        grading_record.histologic_type = payload.histologic_type
        grading_record.type_confirmed_by = actor
    
    # Merge overrides ensuring patch and HPF reviews are preserved (#602)
    merged_overrides = dict(grading_record.overrides or {})
    for comp in ALLOWED_CONFIRM_OVERRIDE_COMPONENTS:
        if comp in payload.overrides:
            merged_overrides[comp] = payload.overrides[comp]
    grading_record.overrides = merged_overrides

    # 7. Mark Stage 5 as Confirmed
    stage_exec.status = "confirmed"
    stage_exec.reviewed_at = datetime.now(timezone.utc)
    stage_exec.reviewed_by = actor

    edits = list(stage_exec.review_edits or [])
    edits.append({
        "op": "replace",
        "path": "/stage_confirmed",
        "value": {
            "grade": computed_grade,
            "nottingham_sum": computed_sum,
            "histologic_type": grading_record.histologic_type
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": actor
    })
    stage_exec.review_edits = edits

    # 8. Mark Case Done
    case.status = "done"

    # 9. Record Audit Events
    audit_confirm = AuditEvent(
        case_id=str(case_id),
        actor=actor,
        event_type="stage_confirmed",
        stage="grading",
        payload={
            "nottingham_sum": computed_sum,
            "grade": computed_grade,
            "histologic_type": grading_record.histologic_type,
            "approved_patches_count": rev_summary["approved_patches"],
            "approved_hpfs_count": rev_summary["approved_hpfs"],
            "has_overrides": bool(payload.overrides)
        }
    )
    db.add(audit_confirm)

    for o_info in required_overrides_to_audit:
        audit_ovr = AuditEvent(
            case_id=str(case_id),
            actor=actor,
            event_type="score_override",
            stage="grading",
            payload={
                "component": o_info["component"],
                "from_score": o_info["from_score"],
                "to_score": o_info["to_score"],
                "justification": o_info["justification"]
            }
        )
        db.add(audit_ovr)

    db.commit()

    resp_payload = {
        "status": "success",
        "case_id": case_id,
        "stage": "grading",
        "next_stage": None,
        "grade": computed_grade,
        "nottingham_sum": computed_sum,
        "histologic_type": grading_record.histologic_type
    }
    if idem_ctx:
        idem_ctx.complete(resp_payload, status.HTTP_200_OK)

    return resp_payload

