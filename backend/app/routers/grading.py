"""
Stage 5 review API: the ``grading_v6`` contract (docs/contracts/grading_v6.md, SPEC-07 §5-7; WP-8.6).

Every payload is built from ``gradings.machine`` (the worker's output, never edited) and
``gradings.overrides`` (the pathologist's sample reviews, component overrides and reasons). Scores,
total, grade and flags come from ``pipeline/grading.py::stage5_result``; the ``gradings`` columns are
rewritten after every edit so they always hold the current values. A grading written by the v5
worker has no v6 machine output and answers ``404 not_found`` on every route (owner decision
2026-10-02). Errors answer with the contract's bodies, ``{"error": <code>, "detail": <text>, ...}``.
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, Literal, Optional, Tuple, get_args

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import CurrentUser, require
from app.auth.idempotency import IdempotencyContext, IdempotentRoute, idempotent
from app.core.config import settings
from app.core.db import get_db
from app.core.gcs import blob_exists, download_blob_as_bytes
from app.core.pipeline_config import get_pipeline_config
from app.inference.schemas import HistotypeVerdict
from app.models.audit import AuditEvent
from app.models.case import Case
from app.models.grading import Grading
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from pipeline.grading import MACHINE_SCHEMA, sample_blob, stage5_result

# Every type the estimator can propose (HistotypeVerdict) plus the pathologist-only ones.
VALID_HISTOLOGIC_TYPES = set(get_args(HistotypeVerdict.model_fields["type"].annotation)) | {
    "apocrine", "medullary_features"
}
# The stage accepts review edits only while it awaits review.
EDITABLE_STATUS = "awaiting_review"


class ContractError(Exception):
    """A refusal with the contract's error body."""

    def __init__(self, status_code: int, error: str, detail: str, **extra: Any):
        super().__init__(detail)
        self.status_code = status_code
        self.body = {"error": error, "detail": detail, **extra}

    def response(self) -> JSONResponse:
        return JSONResponse(status_code=self.status_code, content=self.body)


class GradingRoute(IdempotentRoute):
    """Answers a ``ContractError`` with its body (after the idempotency key is released)."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def contract_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except ContractError as exc:
                return exc.response()

        return contract_handler


router = APIRouter(prefix="/api/v1/stages/grading", tags=["grading"], route_class=GradingRoute)


def to_uuid(val: Any) -> uuid.UUID:
    if isinstance(val, uuid.UUID):
        return val
    try:
        return uuid.UUID(str(val))
    except ValueError as exc:
        raise ContractError(404, "not_found", f"Case {val} not found") from exc


def _load(case_id: str, db: Session, *, lock: bool = False) -> Tuple[Case, StageExecution, Grading]:
    """The case, its latest grading execution and its v6 grading (``lock`` row-locks the execution)."""
    case_uid = to_uuid(case_id)
    case = db.get(Case, case_uid)
    if case is None:
        raise ContractError(404, "not_found", f"Case {case_id} not found")
    stmt = select(StageExecution).where(
        StageExecution.case_id == case_uid, StageExecution.stage == "grading"
    ).order_by(StageExecution.attempt.desc()).limit(1)
    if lock:
        # Row lock serialises concurrent state-gated writes (SPEC-03 §5.3.3); SQLite ignores it.
        stmt = stmt.with_for_update()
    stage_exec = db.scalars(stmt).first()
    if stage_exec is None:
        raise ContractError(404, "not_found", "Stage 5 (grading) not found for this case")
    grading = db.get(Grading, case_uid)
    if grading is None or (grading.machine or {}).get("schema") != MACHINE_SCHEMA:
        raise ContractError(404, "not_found", "This case has no v6 grading output (a v5 grading is not served; re-run grading)")
    return case, stage_exec, grading


def _editable(case_id: str, db: Session) -> Tuple[Case, StageExecution, Grading]:
    case, stage_exec, grading = _load(case_id, db, lock=True)
    if stage_exec.status != EDITABLE_STATUS:
        raise ContractError(409, "stage_locked", f"Grading stage is '{stage_exec.status}'; only a stage awaiting review can be edited.")
    return case, stage_exec, grading


def _slide_geom(db: Session, case: Case) -> Dict[str, Any]:
    slide = db.scalars(select(Slide).where(Slide.case_id == case.id).limit(1)).first()
    if slide is None or not slide.width_px or not slide.height_px or not slide.mpp_x or not slide.mpp_y:
        raise ContractError(404, "not_found", f"Case {case.id} has no slide with pixel dimensions and resolution")
    return {"width_px": slide.width_px, "height_px": slide.height_px, "mpp_x": float(slide.mpp_x), "mpp_y": float(slide.mpp_y)}


def _image_url(case_id: str, kind: str, sample_id: str) -> str:
    return f"/api/v1/stages/grading/{case_id}/{kind}/{sample_id}/image"


def _verification(field: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The verifier's independent score of a field (owner decision 2026-10-04); None for gradings without one."""
    v = field.get("verification")
    if v is None:
        return None
    return {"producer": v["producer"], "pleomorphism_score": v["pleomorphism_score"], "agrees": v["agrees"]}


def _contract_overrides(overrides: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: overrides[k] for k in ("tubule_score", "pleo_score", "histotype") if k in overrides}
    out["reasons"] = dict(overrides.get("reasons", {}))
    return out


def _view(db: Session, case: Case, stage_exec: StageExecution, grading: Grading) -> Dict[str, Any]:
    """The ``GradingStageV6`` payload."""
    machine, overrides = grading.machine, grading.overrides or {}
    result = stage5_result(machine, overrides, get_pipeline_config().scoring)
    case_id = str(case.id)
    reviews = overrides.get("reviews", {})
    samples = [
        {"id": s["id"], "center_um": s["center_um"], "size_um": s["size_um"], "mpp": s["mpp"],
         "image_url": _image_url(case_id, "tubule", s["id"]), "stratum": s["stratum"],
         "tumor_area_um2": s["tumor_area_um2"], "estimate": s["estimate"],
         "review": reviews.get("tubule", {}).get(s["id"])}
        for s in machine["tubule"]["samples"]
    ]
    fields = [
        {"id": f["id"], "center_um": f["center_um"], "size_um": f["size_um"], "mpp": f["mpp"],
         "image_url": _image_url(case_id, "pleo", f["id"]), "stratum": f["stratum"],
         "estimate": f["estimate"], "nuclei": f["nuclei"], "review": reviews.get("pleo", {}).get(f["id"]),
         "verification": _verification(f)}
        for f in machine["pleomorphism"]["fields"]
    ]
    proposed = machine["histotype"]
    mitotic = machine["mitotic"]
    return {
        "case_id": case_id,
        "stage_execution_id": str(stage_exec.id),
        "status": stage_exec.status,
        "slide": _slide_geom(db, case),
        "tubule": {"samples": samples, "percent": result["tubule_percent"], "score": result["tubule_score"],
                   "estimator": machine["tubule"]["estimator"], "n_used": result["n_used"]},
        "pleomorphism": {"fields": fields, "score": result["pleo_score"],
                         "estimator": machine["pleomorphism"]["estimator"],
                         "aggregation": machine["pleomorphism"]["aggregation"]},
        "mitotic": {k: mitotic[k] for k in ("score", "count_total", "n_hpf", "area_mm2", "per_mm2")},
        "histotype": {
            "type": grading.histologic_type,
            "estimator": machine["histotype_estimator"],
            "rationale": proposed["rationale"] if proposed else "",
            "confirmed": grading.type_confirmed_by != "unconfirmed",
            "confirmed_by": None if grading.type_confirmed_by == "unconfirmed" else grading.type_confirmed_by,
        },
        "total": result["total"],
        "grade": result["grade"],
        "flags": result["flags"],
        "overrides": _contract_overrides(overrides),
        "provenance": {
            "stage": "grading",
            # Only what the execution recorded; versions are never reconstructed from settings.
            "model_versions": stage_exec.model_versions or {},
            "config_hash": stage_exec.config_hash,
            "run_mode": stage_exec.run_mode,
        },
    }


def _store(grading: Grading, overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Saves the edits and rewrites the ``gradings`` columns with the current values."""
    grading.overrides = overrides
    result = stage5_result(grading.machine, overrides, get_pipeline_config().scoring)
    grading.tubule_percent = result["tubule_percent"]
    grading.tubule_score = result["effective_tubule_score"]
    grading.pleo_score = result["effective_pleo_score"]
    grading.mitotic_score = result["mitotic_score"]
    grading.nottingham_sum = result["total"]
    grading.grade = result["grade"]
    return result


def _copy_overrides(grading: Grading) -> Dict[str, Any]:
    """A deep-enough copy so the JSON column sees a new value."""
    src = grading.overrides or {}
    reviews = src.get("reviews", {})
    return {
        **{k: v for k, v in src.items() if k not in ("reviews", "reasons")},
        "reviews": {kind: {sid: dict(r) for sid, r in reviews.get(kind, {}).items()} for kind in ("tubule", "pleo")},
        "reasons": dict(src.get("reasons", {})),
    }


def _review_edit(stage_exec: StageExecution, path: str, old: Any, new: Any, actor: str) -> None:
    edits = list(stage_exec.review_edits or [])
    edits.append({"op": "replace", "path": path, "from": old, "to": new, "actor": actor,
                  "timestamp": datetime.now(timezone.utc).isoformat()})
    stage_exec.review_edits = edits


def _is_score(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value in (1, 2, 3)


# Request bodies (docs/contracts/grading_v6.md). Values are checked here so refusals carry the contract's codes.
class ReviewSamplePayload(BaseModel):
    case_id: str
    kind: str
    sample_id: str
    value: Any = None


class OverridePayload(BaseModel):
    case_id: str
    component: str
    value: Any = None
    reason: str = ""


class HistotypeConfirmPayload(BaseModel):
    case_id: str
    type: Any = None


class CasePayload(BaseModel):
    case_id: str


def _validated_review(kind: str, value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ContractError(422, "invalid_value", "value must be an object with at least one field")
    if kind == "tubule":
        allowed = {"tumor_present", "tubule_percent"}
        if set(value) - allowed:
            raise ContractError(422, "invalid_value", f"tubule review fields are {sorted(allowed)}")
        if "tumor_present" in value and not isinstance(value["tumor_present"], bool):
            raise ContractError(422, "invalid_value", "tumor_present must be true or false")
        pct = value.get("tubule_percent")
        if "tubule_percent" in value and (isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct <= 100):
            raise ContractError(422, "invalid_value", "tubule_percent must be a number from 0 to 100")
        return {k: (float(v) if k == "tubule_percent" else v) for k, v in value.items()}
    if set(value) != {"pleomorphism_score"} or not _is_score(value["pleomorphism_score"]):
        raise ContractError(422, "invalid_value", "a pleomorphism review is {pleomorphism_score: 1 | 2 | 3}")
    return dict(value)


@router.get("/{case_id}")
def get_grading_stage(case_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(require("case:read"))):
    """The ``GradingStageV6`` payload."""
    case, stage_exec, grading = _load(case_id, db)
    return _view(db, case, stage_exec, grading)


@router.get("/{case_id}/{kind}/{sample_id}/image")
def get_sample_image(
    case_id: str,
    kind: Literal["tubule", "pleo"],
    sample_id: str,
    user: CurrentUser = Depends(require("case:read")),
):
    """A sample's stored image (the PNG the estimator saw); a missing image is a 404, never a substitute."""
    blob = sample_blob(str(to_uuid(case_id)), kind, sample_id)
    if not blob_exists(settings.GCS_ARTIFACTS_BUCKET, blob):
        raise ContractError(404, "image_not_found", f"No {kind} image for sample {sample_id}")
    data = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, blob)
    return Response(content=data, media_type="image/png", headers={"Cache-Control": "private, max-age=31536000, immutable"})


@router.post("/review-sample")
def review_sample(payload: ReviewSamplePayload, db: Session = Depends(get_db), user: CurrentUser = Depends(require("stage:review"))):
    """Sets a sample's review; the server re-aggregates."""
    if payload.kind not in ("tubule", "pleo"):
        raise ContractError(422, "invalid_value", "kind must be 'tubule' or 'pleo'")
    case, stage_exec, grading = _editable(payload.case_id, db)
    items = grading.machine["tubule"]["samples"] if payload.kind == "tubule" else grading.machine["pleomorphism"]["fields"]
    if payload.sample_id not in {s["id"] for s in items}:
        raise ContractError(404, "sample_not_found", f"No {payload.kind} sample {payload.sample_id}")
    value = _validated_review(payload.kind, payload.value)

    overrides = _copy_overrides(grading)
    old = overrides["reviews"][payload.kind].get(payload.sample_id)
    kept = {k: v for k, v in (old or {}).items() if k not in ("by", "at")}
    new = {**kept, **value, "by": user.id, "at": datetime.now(timezone.utc).isoformat()}
    overrides["reviews"][payload.kind][payload.sample_id] = new
    _store(grading, overrides)
    _review_edit(stage_exec, f"/{payload.kind}/{payload.sample_id}/review", old, new, user.id)
    db.add(AuditEvent(case_id=str(case.id), actor=user.id, event_type="review_edit", stage="grading",
                      payload={"kind": payload.kind, "sample_id": payload.sample_id, "from": old, "to": new}))
    db.commit()
    return _view(db, case, stage_exec, grading)


@router.post("/override")
def override_component(payload: OverridePayload, db: Session = Depends(get_db), user: CurrentUser = Depends(require("stage:review"))):
    """Overrides (or, with ``value: null``, clears) a component, with a written reason."""
    if payload.component not in ("tubule", "pleo", "histotype"):
        raise ContractError(422, "invalid_value", "component must be 'tubule', 'pleo' or 'histotype'")
    reason, min_chars = payload.reason.strip(), get_pipeline_config().scoring.grading.override_min_reason_chars
    if len(reason) < min_chars:
        raise ContractError(422, "reason_too_short", f"An override needs a reason of at least {min_chars} characters")
    if payload.value is not None:
        valid = payload.value in VALID_HISTOLOGIC_TYPES if payload.component == "histotype" else _is_score(payload.value)
        if not valid:
            raise ContractError(422, "invalid_value", f"{payload.value!r} is not a valid {payload.component} value")
    case, stage_exec, grading = _editable(payload.case_id, db)

    key = {"tubule": "tubule_score", "pleo": "pleo_score", "histotype": "histotype"}[payload.component]
    overrides = _copy_overrides(grading)
    old = overrides.get(key)
    if payload.value is None:
        overrides.pop(key, None)
        overrides["reasons"].pop(payload.component, None)
    else:
        overrides[key] = payload.value
        overrides["reasons"][payload.component] = reason
    if payload.component == "histotype":
        # The type shown is the override, or the proposal once it is cleared; either way it needs confirming.
        proposed = grading.machine["histotype"]
        grading.histologic_type = payload.value if payload.value is not None else (proposed["type"] if proposed else None)
        grading.type_confirmed_by = "unconfirmed"
    _store(grading, overrides)
    _review_edit(stage_exec, f"/overrides/{key}", old, payload.value, user.id)
    db.add(AuditEvent(case_id=str(case.id), actor=user.id, event_type="score_override", stage="grading",
                      payload={"component": payload.component, "from": old, "to": payload.value, "reason": reason}))
    db.commit()
    return _view(db, case, stage_exec, grading)


@router.post("/histotype/confirm")
def confirm_histotype(
    payload: HistotypeConfirmPayload,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("stage:confirm")),
    _idempotency: IdempotencyContext = idempotent("stages/grading/histotype/confirm"),
):
    """The pathologist confirms the histologic type (any valid type; it becomes the case's type)."""
    if payload.type not in VALID_HISTOLOGIC_TYPES:
        raise ContractError(422, "invalid_value", f"{payload.type!r} is not one of {sorted(VALID_HISTOLOGIC_TYPES)}")
    case, stage_exec, grading = _editable(payload.case_id, db)
    old = {"type": grading.histologic_type, "confirmed_by": grading.type_confirmed_by}
    grading.histologic_type = payload.type
    grading.type_confirmed_by = user.id
    _review_edit(stage_exec, "/histotype", old, {"type": payload.type, "confirmed_by": user.id}, user.id)
    db.add(AuditEvent(case_id=str(case.id), actor=user.id, event_type="histologic_type_confirmed", stage="grading",
                      payload={"histologic_type": payload.type, "proposed": (grading.machine["histotype"] or {}).get("type")}))
    db.commit()
    return _view(db, case, stage_exec, grading)


@router.post("/confirm")
def confirm_grading(
    payload: CasePayload,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("stage:confirm")),
    _idempotency: IdempotencyContext = idempotent("stages/grading/confirm"),
):
    """Confirms Stage 5 and closes the case. Needs a confirmed type and all three components."""
    case, stage_exec, grading = _load(payload.case_id, db, lock=True)
    if stage_exec.status != EDITABLE_STATUS:
        raise ContractError(409, "not_awaiting_review", f"Grading stage is '{stage_exec.status}', not awaiting review")
    if grading.type_confirmed_by == "unconfirmed" or grading.histologic_type is None:
        raise ContractError(409, "histotype_unconfirmed", "Confirm the histologic type before confirming grading")
    result = _store(grading, _copy_overrides(grading))
    missing = [name for name, score in (("tubule", result["effective_tubule_score"]),
                                        ("pleomorphism", result["effective_pleo_score"]),
                                        ("mitotic", result["mitotic_score"])) if score is None]
    if missing:
        raise ContractError(409, "missing_component", f"No score for {', '.join(missing)}", components=missing)

    now = datetime.now(timezone.utc)
    stage_exec.status = "confirmed"
    stage_exec.reviewed_by = user.id
    stage_exec.reviewed_at = now
    _review_edit(stage_exec, "/stage_confirmed", None,
                 {"grade": result["grade"], "total": result["total"], "histologic_type": grading.histologic_type}, user.id)
    case.status = "done"
    db.add(AuditEvent(case_id=str(case.id), actor=user.id, event_type="stage_confirmed", stage="grading", payload={
        "grade": result["grade"], "nottingham_sum": result["total"], "tubule_score": result["effective_tubule_score"],
        "pleo_score": result["effective_pleo_score"], "mitotic_score": result["mitotic_score"],
        "histologic_type": grading.histologic_type, "overrides": _contract_overrides(grading.overrides),
    }))
    db.commit()
    return {"status": "confirmed", "case_status": "done", "next_stage": None}
