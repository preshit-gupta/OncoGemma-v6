"""Research API router (SPEC-08 §3–7; docs/contracts/research_v1.md).

Endpoints for runs dashboard, run details, metrics, items and decision trees,
mitosis error cards, what-if PR curves, run comparisons, issue register,
pathologist annotation tasks, and label QA workflows.

Nothing here invents a value: a figure the run cannot support is an explicit error
(``404 *_not_available``, ``503 manifest_unavailable``), never a default.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Literal

import pandas as pd
from app.auth.deps import CurrentUser, require
from app.core.db import get_db
from app.models.decision_record import DecisionRecord
from app.models.research import (
    AnnotationTaskModel,
    GTAnnotation,
    Issue,
    QAItemModel,
    RunMetric,
)
from app.models.user import User
from app.models.validation import ValidationItem, ValidationRun
from app.schemas.research import (
    AnnotationSubmit,
    AnnotationSubmitResponse,
    AnnotationTaskResponse,
    CompareRow,
    CompareSlice,
    CompareV1,
    DecisionNode,
    EvidenceItem,
    FlipItem,
    Gate,
    IssueCreate,
    IssuePatch,
    IssueResponse,
    ItemDetailResponse,
    ItemRow,
    ItemScores,
    Metric,
    MetricImpact,
    MitosisCurvesResponse,
    MitosisErrorCard,
    MyAnnotation,
    Page,
    QAItemResponse,
    QAReview,
    RunDetail,
    RunHeadline,
    RunSummary,
)
from eval.datasets.base import load_registry
from eval.harness import batches
from eval.harness.documents import dump_metrics
from eval.harness.report import (
    COMPARE_METRICS,
    LOWER_IS_BETTER,
    RunNotFinishedError,
    RunsNotComparableError,
    compare_detail,
    compare_runs,
    compute_metrics,
)
from eval.harness.runs import ADHOC, read_manifest, split_rows
from eval.splits import SplitLeakError, check_disjoint
from fastapi import APIRouter, Depends, HTTPException, Query, status
from google.api_core.exceptions import GoogleAPICallError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/research", tags=["research"])

# A manifest lives on local disk or in GCS; these are the errors of reading it.
MANIFEST_READ_ERRORS = (OSError, GoogleAPICallError)


# --- Helpers -----------------------------------------------------------------


class ManifestUnavailableError(RuntimeError):
    """The run's manifest cannot be read, or is no longer the file the run recorded."""


def _encode_cursor(offset: int) -> str:
    return base64.b64encode(str(offset).encode("utf-8")).decode("utf-8")


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        offset = int(base64.b64decode(cursor, validate=True).decode("utf-8"))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid_cursor") from exc
    if offset < 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid_cursor")
    return offset


def _page(db: Session, query, cursor: str | None, limit: int) -> tuple[list, str | None]:
    offset = _decode_cursor(cursor)
    rows = db.scalars(query.offset(offset).limit(limit + 1)).all()
    next_cursor = _encode_cursor(offset + limit) if len(rows) > limit else None
    return list(rows[:limit]), next_cursor


def _user_uuid(user: CurrentUser) -> uuid.UUID:
    return uuid.UUID(user.id)


def _lookup_run(db: Session, ref: str) -> ValidationRun | None:
    """A run by id, or by name when ``ref`` is not a UUID; a name shared by two runs is an error."""
    try:
        run_id = uuid.UUID(ref)
    except ValueError:
        run_id = None
    if run_id is not None:
        return db.get(ValidationRun, run_id)
    runs = db.scalars(select(ValidationRun).where(ValidationRun.name == ref)).all()
    if len(runs) > 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run_name_ambiguous")
    return runs[0] if runs else None


def _get_run_or_404(db: Session, ref: str) -> ValidationRun:
    run = _lookup_run(db, ref)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run_not_found")
    return run


@lru_cache(maxsize=32)
def _manifest(uri: str, sha256: str) -> pd.DataFrame:
    """The manifest a run recorded. A run's manifest is immutable (its SHA-256 is part of the run)."""
    try:
        frame, actual = read_manifest(uri)
    except MANIFEST_READ_ERRORS as exc:
        raise ManifestUnavailableError(f"cannot read {uri}: {exc}") from exc
    if actual != sha256:
        raise ManifestUnavailableError(f"{uri} changed since the run was created")
    return frame


def _run_truth(run: ValidationRun) -> pd.DataFrame:
    try:
        return split_rows(_manifest(run.manifest_uri, run.manifest_sha256), run)
    except ManifestUnavailableError as exc:
        logger.error("run %s: %s", run.id, exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="manifest_unavailable") from exc


def _manifest_disjoint(run: ValidationRun) -> bool:
    """No patient shared between splits, from the manifest (SPEC-00 §2.5). An unreadable manifest fails the gate."""
    if run.split == ADHOC:
        return True  # an ad-hoc run has no splits to leak between
    try:
        frame = _manifest(run.manifest_uri, run.manifest_sha256)
    except ManifestUnavailableError as exc:
        logger.error("run %s: gate cannot check disjointness: %s", run.id, exc)
        return False
    try:
        check_disjoint({"manifest": frame})
    except SplitLeakError as exc:
        logger.error("run %s: %s", run.id, exc)
        return False
    return True


def _build_gate(db: Session, run: ValidationRun) -> Gate:
    """The measurement-validity gate (SPEC-00 §2.5): INT-PROV = 1.0, INT-FALL = 0, disjoint splits, hashes recorded."""
    int_fall = db.scalar(
        select(func.count()).select_from(DecisionRecord).where(
            DecisionRecord.run_id == run.id, DecisionRecord.producer_kind == "fallback"
        )
    )
    succeeded = db.scalars(
        select(ValidationItem.case_id).where(
            ValidationItem.run_id == run.id, ValidationItem.status == "succeeded"
        )
    ).all()
    recorded = set(db.scalars(select(DecisionRecord.case_id).where(DecisionRecord.run_id == run.id).distinct()).all())
    # A run with no succeeded item has shown no provenance, so its gate is not valid.
    int_prov = sum(1 for case_id in succeeded if case_id in recorded) / len(succeeded) if succeeded else 0.0
    disjoint = _manifest_disjoint(run)
    hashes_recorded = bool(
        run.config_hash and run.registry_sha256 and (run.split == ADHOC or run.splits_lock_sha256)
    )
    valid = int_fall == 0 and int_prov == 1.0 and disjoint and hashes_recorded
    return Gate(valid=valid, int_prov=int_prov, int_fall=int_fall, disjoint=disjoint)


def _headline_metric(row: RunMetric | None, gate: Gate) -> Metric | None:
    if row is None:
        return None
    # A run that broke the gate has no valid headline (SPEC-08 AC4).
    return Metric(
        value=row.value, ci_low=row.ci_low, ci_high=row.ci_high, n=row.n,
        status=row.status if gate.valid else "invalid",  # type: ignore[arg-type]
    )


def _build_headline(db: Session, run: ValidationRun, gate: Gate) -> RunHeadline:
    rows = {
        m.metric_id: m
        for m in db.scalars(select(RunMetric).where(RunMetric.run_id == run.id)).all()
        if not m.slice_key
    }
    return RunHeadline(ns_m=_headline_metric(rows.get("ns_m"), gate), ns_g=_headline_metric(rows.get("ns_g"), gate))


def _build_run_summary(db: Session, run: ValidationRun) -> RunSummary:
    counts, _ = batches.progress(db, run)
    gate = _build_gate(db, run)
    return RunSummary(
        id=str(run.id),
        name=run.name,
        dataset=run.dataset,
        split=run.split,  # type: ignore[arg-type]
        arm=run.arm,
        status=run.status,  # type: ignore[arg-type]
        is_locked_test=run.is_locked_test,
        created_by=run.created_by,
        created_at=run.created_at.isoformat() if run.created_at else "",
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
        n_items=sum(counts.values()),
        n_failed=counts.get("failed", 0) + counts.get("excluded_qc", 0),  # SPEC-00 rule 2: QC exclusions are failures
        headline=_build_headline(db, run, gate),
        gate=gate,
    )


def _license_scopes(run: ValidationRun) -> list[Literal["commercial_ok", "research", "pending"]]:
    """The dataset's licence scope (eval/datasets/registry.yaml); a dataset the registry does not list is pending."""
    entry = load_registry().get(run.dataset)
    return [entry["license_scope"] if entry is not None else "pending"]


# --- 1. Runs Dashboard & Detail (SPEC-08 §3, §7) -------------------------------


@router.get("/runs", response_model=Page[RunSummary])
def list_runs(
    dataset: str | None = None,
    split: str | None = None,
    arm: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("research:read")),
):
    query = select(ValidationRun).order_by(ValidationRun.created_at.desc(), ValidationRun.id)
    if dataset:
        query = query.where(ValidationRun.dataset == dataset)
    if split:
        query = query.where(ValidationRun.split == split)
    if arm:
        query = query.where(ValidationRun.arm == arm)
    if status_filter:
        query = query.where(ValidationRun.status == status_filter)

    runs, next_cursor = _page(db, query, cursor, limit)
    return Page(items=[_build_run_summary(db, r) for r in runs], next_cursor=next_cursor)


@router.get("/runs/{id}", response_model=RunDetail)
def get_run_detail(
    id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("research:read")),
):
    run = _get_run_or_404(db, id)
    return RunDetail(
        **_build_run_summary(db, run).model_dump(),
        config_hash=run.config_hash,
        registry_sha256=run.registry_sha256,
        splits_lock_sha256=run.splits_lock_sha256,
        manifest_sha256=run.manifest_sha256,
        stages=list(run.stages),
        license_scopes=_license_scopes(run),
    )


@router.get("/runs/{id}/metrics")
def get_run_metrics(
    id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("research:read")),
):
    run = _get_run_or_404(db, id)
    if run.status != "completed":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="metrics_not_ready")
    try:
        doc = compute_metrics(db, run.id)
    except RunNotFinishedError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="metrics_not_ready") from exc
    except RunsNotComparableError as exc:
        logger.error("run %s: %s", run.id, exc)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="manifest_changed") from exc
    except MANIFEST_READ_ERRORS as exc:
        logger.error("run %s: cannot read the manifest: %s", run.id, exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="manifest_unavailable") from exc
    return json.loads(dump_metrics(doc))


# --- 2. Items & Decisions (SPEC-08 §4.1) --------------------------------------


def _score(value) -> int | None:
    return None if value is None or pd.isna(value) else int(value)


def _truth_scores(truth: pd.DataFrame, slide_id: str) -> ItemScores:
    """Ground truth of a slide from the manifest; a column the manifest does not carry is None."""
    if slide_id not in truth.index:
        raise LookupError(f"slide {slide_id!r} is not in the run's manifest split")
    row = truth.loc[slide_id]

    def column(name: str):
        return row[name] if name in row.index else None

    histotype = column("gt_histotype")
    return ItemScores(
        grade=_score(column("gt_grade")),
        total=_score(column("gt_total")),
        tubule=_score(column("gt_tubule")),
        pleo=_score(column("gt_pleo")),
        mitoses=_score(column("gt_mitoses")),
        histotype=None if histotype is None or pd.isna(histotype) else str(histotype),
    )


def _item_row(item: ValidationItem, truth: pd.DataFrame) -> ItemRow:
    grading = (item.prediction or {}).get("grading", {})
    pred = ItemScores(
        grade=grading.get("grade"),
        total=grading.get("total"),
        tubule=grading.get("tubule"),
        pleo=grading.get("pleo"),
        mitoses=grading.get("mitoses"),
        histotype=grading.get("histotype"),
    )
    gt = _truth_scores(truth, item.slide_id)
    sum_error = abs(pred.total - gt.total) if pred.total is not None and gt.total is not None else None
    return ItemRow(
        slide_id=item.slide_id,
        patient_id=item.patient_id,
        status=item.status,  # type: ignore[arg-type]
        failed_stage=item.failed_stage,
        error_class=item.error_class,
        gt=gt,
        pred=pred,
        sum_error=sum_error,
        runtime_s=item.runtime_s,
        cost_usd=float(item.cost_usd) if item.cost_usd is not None else None,
    )


@router.get("/runs/{id}/items", response_model=Page[ItemRow])
def list_run_items(
    id: str,
    status_filter: str | None = Query(None, alias="status"),
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("research:read")),
):
    run = _get_run_or_404(db, id)
    query = (
        select(ValidationItem)
        .where(ValidationItem.run_id == run.id)
        .order_by(ValidationItem.slide_id)
    )
    if status_filter:
        query = query.where(ValidationItem.status == status_filter)

    items, next_cursor = _page(db, query, cursor, limit)
    truth = _run_truth(run)
    return Page(items=[_item_row(item, truth) for item in items], next_cursor=next_cursor)


@router.get("/runs/{id}/items/{slide_id}", response_model=ItemDetailResponse)
def get_run_item_detail(
    id: str,
    slide_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("research:read")),
):
    run = _get_run_or_404(db, id)
    item = db.scalar(
        select(ValidationItem).where(
            ValidationItem.run_id == run.id,
            ValidationItem.slide_id == slide_id,
        )
    )
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="item_not_found")

    item_row = _item_row(item, _run_truth(run))

    # The decisions this run made on the item's case (a case can also carry records of other runs).
    records = []
    if item.case_id:
        records = db.scalars(
            select(DecisionRecord)
            .where(DecisionRecord.case_id == item.case_id, DecisionRecord.run_id == run.id)
            .order_by(DecisionRecord.created_at)
        ).all()

    node_map: dict[str, DecisionNode] = {}
    for dr in records:
        node_map[str(dr.id)] = DecisionNode(
            id=str(dr.id),
            task=dr.task,
            entity_type=dr.entity_type,
            entity_id=dr.entity_id,
            producer_kind=dr.producer_kind,  # type: ignore[arg-type]
            producer_id=dr.producer_id,
            producer_version=dr.producer_version,
            status=dr.status,  # type: ignore[arg-type]
            input_spec=dr.input_spec or {},
            output=dr.output,
            latency_ms=dr.latency_ms,
            children=[],
        )

    root_nodes: list[DecisionNode] = []
    for dr in records:
        node = node_map[str(dr.id)]
        if dr.supersedes_id and str(dr.supersedes_id) in node_map:
            node_map[str(dr.supersedes_id)].children.append(node)
        else:
            root_nodes.append(node)

    return ItemDetailResponse(
        item=item_row,
        case_id=str(item.case_id) if item.case_id else "",
        decisions=root_nodes,
    )


# --- 3. Mitosis Errors & Curves (SPEC-08 §4.4) ---------------------------------


@router.get("/runs/{id}/errors/mitosis", response_model=Page[MitosisErrorCard])
def list_mitosis_errors(
    id: str,
    kind: Literal["fp", "fn"] | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("research:read")),
):
    _get_run_or_404(db, id)
    _decode_cursor(cursor)
    # Error cards need annotated mitotic figures and stored detections, which only the
    # mitosis_roi component harness produces (see metrics `unavailable`); no run stores them yet.
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="mitosis_errors_not_available")


@router.get("/runs/{id}/curves/mitosis", response_model=MitosisCurvesResponse)
def get_mitosis_curves(
    id: str,
    tau_a: float | None = None,
    tau_b: float | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("research:read")),
):
    run = _get_run_or_404(db, id)

    what_if = tau_a is not None or tau_b is not None
    # SPEC-08 §4.4: what-if queries are strictly forbidden on the test split
    if what_if and run.split != "val":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="not_val_split")

    # The PR curve and what-if thresholds are computed from per-detection scores (p_a, p_b) against
    # annotated figures, which only the mitosis_roi component harness produces; no run stores them yet.
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="curves_not_available")


# --- 4. Run Comparison (SPEC-08 §4.3, SPEC-02 §7) ------------------------------


def _manifest_unavailable(run_ids: list[str], exc: Exception) -> HTTPException:
    logger.error("runs %s: cannot read the manifest: %s", run_ids, exc)
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="manifest_unavailable")


@router.get("/compare", response_model=CompareV1)
def compare_two_runs(
    a: str = Query(..., description="ID of run A"),
    b: str = Query(..., description="ID of run B"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("research:read")),
):
    run_a = _get_run_or_404(db, a)
    run_b = _get_run_or_404(db, b)

    if run_a.manifest_sha256 != run_b.manifest_sha256:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="manifest_mismatch")

    summary_a = _build_run_summary(db, run_a)
    summary_b = _build_run_summary(db, run_b)

    try:
        compared = compare_runs(db, run_a.id, run_b.id, metric_name="ns_g")
        detail = compare_detail(db, run_a.id, run_b.id, metric_name="ns_g")
    except RunsNotComparableError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="runs_not_comparable") from exc
    except RunNotFinishedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run_not_finished") from exc
    except MANIFEST_READ_ERRORS as exc:
        raise _manifest_unavailable([str(run_a.id), str(run_b.id)], exc) from exc

    return CompareV1(
        a=summary_a,
        b=summary_b,
        manifest_sha256=run_a.manifest_sha256,
        metrics=[
            CompareRow(
                metric="NS-G macro-F1",
                a=Metric(**compared["a"]),
                b=Metric(**compared["b"]),
                delta=compared["delta"],
                delta_low=compared["delta_low"],
                delta_high=compared["delta_high"],
                mcnemar_p=compared["mcnemar_p"],
            )
        ],
        slices=[CompareSlice(**row) for row in detail["slices"]],
        flips=[FlipItem(**row) for row in detail["flips"]],
    )


# --- 5. Issues Register (SPEC-08 §5) ------------------------------------------


def _issue_response(issue: Issue) -> IssueResponse:
    return IssueResponse(
        id=str(issue.id),
        title=issue.title,
        category=issue.category,  # type: ignore[arg-type]
        severity=issue.severity,  # type: ignore[arg-type]
        status=issue.status,  # type: ignore[arg-type]
        metric_impact=MetricImpact(**issue.metric_impact) if issue.metric_impact else None,
        evidence=[EvidenceItem(**ev) for ev in (issue.evidence or [])],
        spec_ref=issue.spec_ref,
        owner=str(issue.owner) if issue.owner else None,
        created_by=str(issue.created_by),
        created_at=issue.created_at.isoformat() if issue.created_at else "",
        resolved_in=issue.resolved_in,
        resolution_note=issue.resolution_note,
    )


@router.get("/issues", response_model=list[IssueResponse])
def list_issues(
    category: str | None = None,
    severity: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("research:read")),
):
    query = select(Issue).order_by(Issue.created_at.desc())
    if category:
        query = query.where(Issue.category == category)
    if severity:
        query = query.where(Issue.severity == severity)
    if status_filter:
        query = query.where(Issue.status == status_filter)

    return [_issue_response(iss) for iss in db.scalars(query).all()]


@router.post("/issues", status_code=status.HTTP_201_CREATED, response_model=IssueResponse)
def create_issue(
    payload: IssueCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("issue:write")),
):
    issue = Issue(
        title=payload.title,
        category=payload.category,
        severity=payload.severity,
        status="open",
        metric_impact=payload.metric_impact.model_dump() if payload.metric_impact else None,
        evidence=[ev.model_dump() for ev in payload.evidence],
        spec_ref=payload.spec_ref,
        owner=None,
        created_by=_user_uuid(user),
        created_at=datetime.now(timezone.utc),
    )
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return _issue_response(issue)


def _resolution_rejected() -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="resolution_requires_run")


def _require_improvement(db: Session, issue: Issue, resolved_in: str) -> None:
    """SPEC-08 §5: ``resolved_in`` must be a validation run whose paired comparison against the run in
    ``evidence`` shows a non-negative delta on ``metric_impact.metric``."""
    resolved = _lookup_run(db, resolved_in)
    metric_name = (issue.metric_impact or {}).get("metric")
    if resolved is None or metric_name not in COMPARE_METRICS:
        raise _resolution_rejected()
    baselines = []
    for evidence in issue.evidence or []:
        run = _lookup_run(db, evidence["run_id"])
        if run is not None and run.id != resolved.id:
            baselines.append(run)
    if not baselines:
        raise _resolution_rejected()
    try:
        compared = compare_runs(db, baselines[0].id, resolved.id, metric_name)
    except (RunsNotComparableError, RunNotFinishedError) as exc:
        raise _resolution_rejected() from exc
    except MANIFEST_READ_ERRORS as exc:
        raise _manifest_unavailable([str(baselines[0].id), str(resolved.id)], exc) from exc
    delta = -compared["delta"] if metric_name in LOWER_IS_BETTER else compared["delta"]
    if not delta >= 0 or math.isnan(delta):
        raise _resolution_rejected()


@router.patch("/issues/{id}", response_model=IssueResponse)
def patch_issue(
    id: str,
    payload: IssuePatch,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("issue:write")),
):
    try:
        issue = db.get(Issue, uuid.UUID(id))
    except ValueError:
        issue = None
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="issue_not_found")

    target_status = payload.status if payload.status is not None else issue.status
    if target_status == "resolved":
        resolved_in = payload.resolved_in or issue.resolved_in
        if not resolved_in:
            raise _resolution_rejected()
        if payload.status == "resolved" or payload.resolved_in is not None:
            _require_improvement(db, issue, resolved_in)
        issue.resolved_in = resolved_in
    elif payload.resolved_in is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="resolved_in_requires_resolved_status"
        )

    if payload.status is not None:
        issue.status = payload.status
    if payload.owner is not None:
        if payload.owner == "":
            issue.owner = None
        else:
            try:
                owner = uuid.UUID(payload.owner)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="owner_not_found"
                ) from exc
            if db.get(User, owner) is None:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="owner_not_found")
            issue.owner = owner
    if payload.resolution_note is not None:
        issue.resolution_note = payload.resolution_note

    db.commit()
    db.refresh(issue)
    return _issue_response(issue)


# --- 6. Pathologist Annotation Tasks (SPEC-08 §6) -----------------------------


def _polygons(task: AnnotationTaskModel) -> list[list[tuple[float, float]]]:
    """Regions as polygons; a region stored as ``[x1, y1, x2, y2]`` is the rectangle it spans."""
    polygons: list[list[tuple[float, float]]] = []
    for region in task.regions_um or []:
        if region and isinstance(region[0], (list, tuple)):
            polygons.append([(float(pt[0]), float(pt[1])) for pt in region])
        elif len(region) == 4 and all(isinstance(v, (int, float)) for v in region):
            x1, y1, x2, y2 = region
            polygons.append([(float(x1), float(y1)), (float(x2), float(y1)), (float(x2), float(y2)), (float(x1), float(y2))])
        else:
            raise ValueError(f"annotation task {task.id!r} has a malformed region: {region!r}")
    return polygons


@router.get("/annotation-tasks", response_model=Page[AnnotationTaskResponse])
def list_annotation_tasks(
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("research:annotate")),
):
    query = select(AnnotationTaskModel).order_by(AnnotationTaskModel.created_at.desc(), AnnotationTaskModel.id)
    tasks, next_cursor = _page(db, query, cursor, limit)

    user_uuid = _user_uuid(user)
    responses: list[AnnotationTaskResponse] = []
    for t in tasks:
        ann = db.scalar(
            select(GTAnnotation).where(
                GTAnnotation.annotator_id == user_uuid,
                GTAnnotation.dataset == t.dataset,
                GTAnnotation.slide_id == t.slide_id,
                GTAnnotation.task == t.kind,
            )
        )
        responses.append(
            AnnotationTaskResponse(
                id=t.id,
                dataset=t.dataset,
                slide_id=t.slide_id,
                kind=t.kind,  # type: ignore[arg-type]
                regions_um=_polygons(t),
                blind=t.blind,
                definition_md=t.definition_md,
                protocol_version=t.protocol_version,
                my_annotation=MyAnnotation(
                    id=str(ann.id),
                    status=ann.status,  # type: ignore[arg-type]
                    payload=ann.payload,
                ) if ann else None,
            )
        )

    return Page(items=responses, next_cursor=next_cursor)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@router.post("/annotations", status_code=status.HTTP_201_CREATED, response_model=AnnotationSubmitResponse)
def submit_annotation(
    payload: AnnotationSubmit,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("research:annotate")),
):
    task = db.get(AnnotationTaskModel, payload.task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task_not_found")

    # Contract: a mitosis payload is `{ "points": [{x_um, y_um, class}] }`
    if task.kind == "mitosis_points":
        points = payload.payload.get("points")
        valid = isinstance(points, list) and all(
            isinstance(pt, dict)
            and _is_number(pt.get("x_um"))
            and _is_number(pt.get("y_um"))
            and pt.get("class") in ("MF", "imposter")
            for pt in points
        )
        if not valid:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid_payload")

    user_uuid = _user_uuid(user)
    now = datetime.now(timezone.utc)
    ann = db.scalar(
        select(GTAnnotation).where(
            GTAnnotation.annotator_id == user_uuid,
            GTAnnotation.dataset == task.dataset,
            GTAnnotation.slide_id == task.slide_id,
            GTAnnotation.task == task.kind,
        )
    )
    if ann is None:
        ann = GTAnnotation(
            dataset=task.dataset,
            slide_id=task.slide_id,
            task=task.kind,
            region_geojson=None,
            payload=payload.payload,
            annotator_id=user_uuid,
            protocol_version=task.protocol_version,
            blind=task.blind,
            status=payload.status,
            created_at=now,
            updated_at=now,
        )
        db.add(ann)
    else:
        ann.payload = payload.payload
        ann.status = payload.status
        ann.updated_at = now

    db.commit()
    db.refresh(ann)
    return AnnotationSubmitResponse(id=str(ann.id), status=ann.status)  # type: ignore[arg-type]


# --- 7. Label QA Workflow (SPEC-08 §4.5) ---------------------------------------


def _qa_response(item: QAItemModel) -> QAItemResponse:
    return QAItemResponse(
        patient_id=item.patient_id,
        report_text_url=item.report_text_url,
        regex=item.regex_data,
        llm=item.llm_data,
        status=item.status,  # type: ignore[arg-type]
    )


@router.get("/labels-qa", response_model=list[QAItemResponse])
def list_labels_qa(
    status_filter: str | None = Query(None, alias="status"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("labels:qa")),
):
    query = select(QAItemModel).order_by(QAItemModel.patient_id)
    if status_filter:
        query = query.where(QAItemModel.status == status_filter)
    return [_qa_response(item) for item in db.scalars(query).all()]


@router.post("/labels-qa/{patient_id}", response_model=QAItemResponse)
def review_label_qa(
    patient_id: str,
    payload: QAReview,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("labels:qa")),
):
    item = db.get(QAItemModel, patient_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="qa_item_not_found")

    # SPEC-08 §4.5: the edit and exclude actions require a reason
    if payload.action in ("edit", "exclude") and not (payload.reason and payload.reason.strip()):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="reason_required")

    now = datetime.now(timezone.utc)
    user_uuid = _user_uuid(user)
    item.status = {"accept": "accepted", "edit": "edited", "exclude": "excluded"}[payload.action]
    item.reviewed_by = user_uuid
    item.reviewed_at = now
    item.reason = payload.reason

    # The review is kept as an annotation: an edit's values live nowhere else.
    db.add(
        GTAnnotation(
            dataset=item.dataset,
            slide_id=patient_id,
            task="label_qa",
            region_geojson=None,
            payload={"action": payload.action, "values": payload.values, "reason": payload.reason},
            annotator_id=user_uuid,
            protocol_version=item.protocol_version,
            blind=False,
            status="submitted",
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()
    db.refresh(item)
    return _qa_response(item)
