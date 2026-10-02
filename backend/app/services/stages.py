"""Stage confirmation, retry and queueing: one implementation for the API and the harness (SPEC-02 §8).

Routers translate ``StageServiceError`` into HTTP responses; the validation harness calls the
same functions with ``actor=f"harness:{run_id}"`` (SPEC-02 §5.3). Every review gate applies to
both. In ``eval`` runs the gates still hold because the gateway never falls back there, so no
candidate is left ``unreviewed`` by a referee outage.

A stage queued from another execution inherits its ``run_mode`` and ``run_id``, so a harness
run stays in ``eval`` from ingest to grading.
"""
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from google.api_core.exceptions import NotFound
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import cloud_tasks
from app.core.config import settings
from app.core.gcs import blob_exists, download_blob_as_bytes, parse_gcs_uri, upload_blob_from_bytes
from app.core.pipeline_config import get_pipeline_config
from app.core.run_context import STAGES, RunMode
from app.models.audit import AuditEvent
from app.models.case import Case
from app.models.detection import Detection
from app.models.hotspot import Hotspot
from app.models.hpf_site import HpfSite
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from pipeline.errors import SpecimenTypeRequired

# Minimum length of the justification for approving a slide that failed QC.
MIN_JUSTIFICATION_CHARS = 10


class StageServiceError(Exception):
    """A stage action was refused. ``status_code`` is the HTTP status the routers answer with."""

    status_code = 409

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class StageNotFound(StageServiceError):
    status_code = 404


class StageConflict(StageServiceError):
    status_code = 409


class InvalidStage(StageServiceError):
    status_code = 400


class ReviewGateError(StageServiceError):
    """A clinical review gate is not satisfied (unreviewed candidates, zero-tumour flag)."""

    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.status_code = status_code


class StageOutputUnavailable(StageServiceError):
    """The machine output the confirmation needs could not be read."""

    status_code = 502


@dataclass(frozen=True)
class ConfirmResult:
    case_id: str
    stage: str
    next_stage: str | None
    next_execution: StageExecution | None
    details: dict


def _case_key(case_id) -> uuid.UUID | str:
    """Case ids are UUIDs; rows written by v5 code may hold other strings, which the GUID column stores as is."""
    try:
        return uuid.UUID(str(case_id))
    except ValueError:
        return str(case_id)


def latest_execution(session: Session, case_id, stage: str, *, for_update: bool = False) -> StageExecution | None:
    """The latest attempt of ``stage``. ``for_update`` row-locks it so concurrent confirms
    serialise on the status check (SPEC-03 §5.3.3); SQLite ignores the lock."""
    stmt = (
        select(StageExecution)
        .where(StageExecution.case_id == case_id, StageExecution.stage == stage)
        .order_by(StageExecution.attempt.desc())
    )
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def queue_stage(
    session: Session,
    case_id,
    stage: str,
    *,
    input_ref: dict | None = None,
    parent: StageExecution | None = None,
    run_id=None,
) -> StageExecution:
    """Add the next attempt of ``stage`` as ``queued``; the caller commits, then calls ``dispatch``.

    ``parent`` is the execution this one follows from (the previous stage, or the attempt being
    retried); its ``run_mode`` and ``run_id`` carry over. ``run_id`` starts a validation run's
    first execution, in ``eval`` mode. With neither, the execution is clinical.
    """
    if stage not in STAGES:
        raise InvalidStage(f"Invalid stage_name '{stage}'. Known stages: {', '.join(STAGES)}")
    if parent is not None and run_id is not None:
        raise ValueError("pass either parent or run_id: a queued stage inherits its parent's run")
    if parent is not None:
        run_mode, run_id = parent.run_mode, parent.run_id
    else:
        run_mode = RunMode.CLINICAL.value if run_id is None else RunMode.EVAL.value
    latest = latest_execution(session, case_id, stage)
    execution = StageExecution(
        case_id=case_id,
        stage=stage,
        attempt=(latest.attempt + 1) if latest else 1,
        status="queued",
        input_ref=input_ref,
        run_mode=run_mode,
        run_id=run_id,
    )
    session.add(execution)
    session.flush()
    return execution


def dispatch(execution: StageExecution, payload: dict | None = None) -> None:
    """Hand a committed queued execution to Cloud Tasks; without Cloud Tasks the worker polls for it."""
    cloud_tasks.dispatch_stage_task(
        case_id=str(execution.case_id),
        stage=execution.stage,
        stage_exec_id=str(execution.id),
        payload=payload,
    )


def _case_and_slide(session: Session, case_id) -> tuple[Case, Slide]:
    case = session.get(Case, case_id)
    if case is None:
        raise StageNotFound("Case not found")
    slide = session.scalars(select(Slide).where(Slide.case_id == case_id)).first()
    if slide is None:
        raise StageNotFound("Slide not found")
    return case, slide


def _slide_input_ref(slide: Slide) -> dict:
    return {"slide_id": str(slide.id), "gcs_uri_original": slide.gcs_uri_original}


def _mark_confirmed(execution: StageExecution, actor: str, now: datetime) -> None:
    execution.status = "confirmed"
    execution.reviewed_by = actor
    execution.reviewed_at = now


def _audit(session: Session, case_id, actor: str, event_type: str, stage: str, payload: dict) -> None:
    session.add(AuditEvent(case_id=str(case_id), actor=actor, event_type=event_type, stage=stage, payload=payload))


# --- confirm ---------------------------------------------------------------------


def confirm_stage(
    session: Session,
    case_id,
    stage: str,
    actor: str,
    *,
    override_justification: str | None = None,
    no_invasive_tumor: bool = False,
) -> ConfirmResult:
    """Confirm the latest attempt of ``stage`` and queue the stage that follows it.

    Commits, then dispatches the queued stage. ``override_justification`` approves a slide that
    failed QC; ``no_invasive_tumor`` confirms a triage with no active hotspot. Grading is
    confirmed with its scores (``routers/grading.py``) and is refused here.
    """
    case_id = _case_key(case_id)
    if stage in ("preprocess", "qc"):
        result = _confirm_slide(session, case_id, stage, actor, override_justification)
    elif stage == "triage":
        result = _confirm_triage(session, case_id, actor, no_invasive_tumor)
    elif stage == "mitosis":
        result = _confirm_mitosis(session, case_id, actor)
    elif stage == "grading":
        raise StageConflict("Grading is confirmed with its scores at /api/v1/stages/grading/confirm.")
    else:
        raise InvalidStage(f"Stage '{stage}' has no review step.")

    if result.next_execution is not None:
        _audit(session, case_id, actor, "stage_started", result.next_stage, {
            "triggered_by_approval_of": stage, "attempt": result.next_execution.attempt,
        })
    session.commit()
    if result.next_execution is not None:
        dispatch(result.next_execution, result.next_execution.input_ref)
    return result


def _confirm_slide(
    session: Session, case_id: uuid.UUID, stage: str, actor: str, override_justification: str | None
) -> ConfirmResult:
    """Approve the slide after preprocess and QC; a failed QC needs a written justification."""
    case, slide = _case_and_slide(session, case_id)
    current = latest_execution(session, case_id, stage, for_update=True)
    if current is None:
        raise StageNotFound(f"Stage '{stage}' execution not found for case {case_id}.")
    if current.status == "confirmed":
        raise StageConflict(f"Stage '{stage}' has already been confirmed.")
    if current.status not in ("awaiting_review", "done", "failed"):
        raise StageConflict(
            f"Stage '{stage}' cannot be approved because its status is '{current.status}', expected 'awaiting_review'."
        )

    now = datetime.now(timezone.utc)
    qc = latest_execution(session, case_id, "qc")
    if qc is not None:
        if qc.status in ("queued", "running"):
            raise StageConflict(
                "Automated QC analysis is currently running. Please wait for QC checks to complete before approving slide."
            )
        if qc.status == "failed":
            justification = (override_justification or "").strip()
            if len(justification) < MIN_JUSTIFICATION_CHARS:
                raise StageConflict(
                    f"Slide failed automated QC checks ({qc.error or 'Artifacts detected'}). "
                    f"A clinical override justification of at least {MIN_JUSTIFICATION_CHARS} characters "
                    "is required to approve this slide."
                )
            _mark_confirmed(qc, actor, now)
            qc.review_edits = {"override_justification": justification}
            _audit(session, case_id, actor, "score_override", "qc", {
                "action": "qc_failure_override", "justification": justification, "previous_error": qc.error,
            })
        elif qc.status in ("awaiting_review", "done"):
            _mark_confirmed(qc, actor, now)

    _mark_confirmed(current, actor, now)
    # QC's pass already queued triage; approving the slide queues it only when it never ran or failed.
    triage = latest_execution(session, case_id, "triage")
    next_execution = None
    if triage is None or triage.status == "failed":
        next_execution = queue_stage(session, case_id, "triage", input_ref=_slide_input_ref(slide), parent=current)
    case.status = "open"
    _audit(session, case_id, actor, "stage_confirmed", stage, {"next_stage": "triage"})
    return ConfirmResult(str(case_id), stage, "triage", next_execution, {})


def machine_triage_output(execution: StageExecution) -> dict:
    """The machine output (output.json) of a triage execution, as the triage stage wrote it."""
    output_ref = execution.output_ref or ""
    try:
        if output_ref.startswith("gs://"):
            bucket, blob = parse_gcs_uri(output_ref)
        else:
            bucket, blob = settings.GCS_ARTIFACTS_BUCKET, f"cases/{execution.case_id}/triage/output.json"
        return json.loads(download_blob_as_bytes(bucket, blob).decode("utf-8"))
    except (NotFound, FileNotFoundError, ValueError) as exc:
        raise StageOutputUnavailable(
            f"Failed to load triage machine output from storage: {exc}. Confirmation aborted."
        ) from exc


def machine_triage_hotspots(execution: StageExecution) -> list[dict]:
    """The machine hotspots of a triage execution, as the triage stage wrote them."""
    return machine_triage_output(execution).get("hotspots", [])


def effective_triage_hotspots(execution: StageExecution) -> list[dict]:
    """The machine hotspots of a triage execution with the reviewer's edits applied."""
    from app.routers.triage import apply_edit_ops  # the edit grammar lives with the edit endpoint

    return apply_edit_ops(machine_triage_hotspots(execution), execution.review_edits or [])


def _confirm_triage(session: Session, case_id: uuid.UUID, actor: str, no_invasive_tumor: bool) -> ConfirmResult:
    from app.core.geometry import validate_hotspots_non_overlapping
    from app.routers.triage import slide_bounds_um, validate_edit_geometry

    execution = latest_execution(session, case_id, "triage", for_update=True)
    if execution is None:
        raise StageNotFound(f"Triage stage execution not found for case {case_id}")
    if execution.status != "awaiting_review":
        raise StageConflict(
            f"Triage stage cannot be confirmed because its status is '{execution.status}', expected 'awaiting_review'."
        )
    case = session.get(Case, case_id)
    if case is None:
        raise StageNotFound("Case not found")

    # Server-side geometry checks on the reviewer's polygons and the effective set (SPEC-03 §5.3.2).
    validate_edit_geometry(execution.review_edits or [], slide_bounds_um(session, case_id))
    hotspots = effective_triage_hotspots(execution)
    try:
        gap_um = get_pipeline_config().hotspot_gap_um(case.specimen_type)
    except SpecimenTypeRequired as exc:
        raise StageConflict(str(exc)) from exc
    validate_hotspots_non_overlapping(hotspots, gap_um=gap_um, status_code=409)
    active = [h for h in hotspots if not h.get("excluded", False)]
    if no_invasive_tumor and active:
        raise ReviewGateError(
            f"Cannot confirm 'no_invasive_tumor=True' when {len(active)} active tumor hotspot(s) exist. "
            "All tumor hotspots must be excluded or deleted before confirming zero tumor.",
            status_code=409,
        )
    if not active and not no_invasive_tumor:
        raise ReviewGateError(
            "No active hotspots remaining. Pathologist must explicitly flag no_invasive_tumor=True "
            "to confirm zero tumor on this slide.",
            status_code=422,
        )

    session.query(Hotspot).filter(Hotspot.case_id == case_id).delete(synchronize_session=False)
    for hs in hotspots:
        session.add(Hotspot(
            id=hs["id"],
            case_id=case_id,
            stage_execution_id=execution.id,
            polygon_um=hs["polygon_um"],
            area_mm2=hs.get("area_mm2"),
            prob_mean=hs.get("prob_mean"),
            prob_max=hs.get("prob_max"),
            source=hs.get("source", "model"),
            excluded=hs.get("excluded", False),
            exclude_reason=hs.get("exclude_reason"),
            rank=hs.get("rank"),
            rank_score=hs.get("rank_score"),
            score_kind=hs.get("score_kind"),
            tumor_fraction=hs.get("tumor_fraction"),
            prescan_expected=hs.get("prescan_expected"),
            window_um=hs.get("window_um"),
        ))
    _mark_confirmed(execution, actor, datetime.now(timezone.utc))

    if no_invasive_tumor:
        next_stage, next_execution = None, None
        case.status = "done"
    else:
        next_stage = "mitosis"
        next_execution = queue_stage(
            session, case_id, next_stage, input_ref={"confirmed_hotspots_count": len(hotspots)}, parent=execution
        )
        case.status = "open"
    _audit(session, case_id, actor, "stage_confirmed", "triage", {
        "confirmed_hotspots": len(hotspots), "no_invasive_tumor": no_invasive_tumor, "next_stage": next_stage,
    })
    return ConfirmResult(str(case_id), "triage", next_stage, next_execution, {
        "confirmed_hotspots_count": len(hotspots), "no_invasive_tumor": no_invasive_tumor,
    })


def _confirm_mitosis(session: Session, case_id: uuid.UUID, actor: str) -> ConfirmResult:
    """Clinical safety gate, then snapshot the confirmed candidates, HPFs and score and queue grading."""
    from pipeline.grading import calculate_mitotic_score_from_detections_and_hpfs
    from pipeline.scoring import compute_nottingham_mitotic_score

    case = session.get(Case, case_id)
    if case is None:
        raise StageNotFound(f"Case {case_id} not found")
    execution = latest_execution(session, case_id, "mitosis", for_update=True)
    if execution is None:
        raise StageNotFound("Stage 4 (mitosis) not found for this case")
    if execution.status != "awaiting_review":
        raise StageConflict(
            f"Mitosis stage is in status '{execution.status}', must be 'awaiting_review' to confirm."
        )

    gate = get_pipeline_config().mitosis.review.gate_min_conf
    unreviewed = session.scalars(
        select(Detection).where(
            Detection.case_id == case_id,
            Detection.label == "unreviewed",
            (Detection.det_conf >= gate) | (Detection.ver_conf >= gate),
        )
    ).all()
    if unreviewed:
        raise ReviewGateError(
            f"Clinical Safety Gate: {len(unreviewed)} unreviewed candidate mitotic figure(s) with confidence "
            f">= {gate:.2f} remain. Please review or use 'Bulk Reject' before confirming."
        )

    _mark_confirmed(execution, actor, datetime.now(timezone.utc))

    detections = session.scalars(select(Detection).where(Detection.case_id == case_id)).all()
    hpf_rows = session.scalars(
        select(HpfSite).where(HpfSite.case_id == case_id).order_by(HpfSite.seq.asc())
    ).all()
    candidates = [
        {
            "id": d.id,
            "centroid_um": d.centroid_um,
            "label": d.label,
            "label_source": d.label_source,
            "det_conf": d.det_conf,
            "ver_conf": d.ver_conf,
            "hotspot_id": d.hotspot_id,
            "crop_uri": d.crop_uri,
            "crop_orig_uri": d.crop_orig_uri,
        }
        for d in detections
    ]
    hpfs = [
        {"seq": h.seq, "center_um": h.center_um, "radius_um": h.radius_um, "count": h.mitotic_count, "source": h.source}
        for h in hpf_rows
    ]
    scoring = get_pipeline_config().mitosis.scoring
    total, _ = calculate_mitotic_score_from_detections_and_hpfs(candidates, hpfs, scoring)
    summary = compute_nottingham_mitotic_score(
        count_total=total, n_hpf=len(hpfs), radius_um=None, scoring=scoring, hpfs=hpfs
    )

    # The confirmed snapshot replaces the machine lists in output.json; other keys are kept.
    blob = f"cases/{case_id}/mitosis/output.json"
    output = (
        json.loads(download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, blob).decode("utf-8"))
        if blob_exists(settings.GCS_ARTIFACTS_BUCKET, blob) else {}
    )
    output.update({"case_id": str(case_id), "candidates": candidates, "hpfs": hpfs, "summary": summary})
    upload_blob_from_bytes(
        settings.GCS_ARTIFACTS_BUCKET, blob, json.dumps(output, indent=2).encode("utf-8"), "application/json"
    )

    # A finished grading is kept; any other state gets a fresh attempt on the confirmed counts.
    grading = latest_execution(session, case_id, "grading")
    next_execution = None
    if grading is None or grading.status not in ("queued", "confirmed", "done"):
        next_execution = queue_stage(session, case_id, "grading", parent=execution)
    case.status = "open"
    _audit(session, case_id, actor, "stage_confirmed", "mitosis", {
        "next_stage": "grading", "mitotic_score": summary.get("score"), "count_total": total,
    })
    return ConfirmResult(str(case_id), "mitosis", "grading", next_execution, {
        "mitotic_score": summary.get("score"), "count_total": total,
    })


# --- retry -----------------------------------------------------------------------


def retry_stage(session: Session, case_id, stage: str, actor: str) -> StageExecution:
    """Queue a new attempt of ``stage``; earlier attempts stay for audit (SPEC-02 §5.3). Commits and dispatches."""
    if stage not in STAGES:
        raise InvalidStage(f"Invalid stage_name '{stage}'. Known stages: {', '.join(STAGES)}")
    case_id = _case_key(case_id)
    case, slide = _case_and_slide(session, case_id)
    previous = latest_execution(session, case_id, stage)
    if previous is None:
        raise StageConflict(f"Stage '{stage}' has no previous execution attempt to retry.")
    if previous.status == "queued":
        raise StageConflict(
            f"Stage '{stage}' cannot be retried because its status is '{previous.status}'. "
            "Only stages in ('failed', 'rejected') can be retried."
        )
    if previous.status == "running":
        previous.status = "failed"
        previous.error = "Interrupted and retried by user while running."
        previous.completed_at = datetime.now(timezone.utc)
    if stage == "preprocess":
        case.status = "open"

    execution = queue_stage(session, case_id, stage, input_ref=_slide_input_ref(slide), parent=previous)
    _audit(session, case_id, actor, "stage_retried", stage, {"attempt": execution.attempt})
    session.commit()
    dispatch(execution, {"slide_id": str(slide.id)})
    return execution
