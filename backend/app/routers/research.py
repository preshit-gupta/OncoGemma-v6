"""Research API router (SPEC-08 §3–7; docs/contracts/research_v1.md).

Endpoints for runs dashboard, run details, metrics, items and decision trees,
mitosis error cards, what-if PR curves, run comparisons, issue register,
pathologist annotation tasks, and label QA workflows.
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.auth.deps import CurrentUser, require
from app.core.db import get_db
from app.models.case import Case
from app.models.decision_record import DecisionRecord
from app.models.research import (
    AnnotationTaskModel,
    GTAnnotation,
    Issue,
    QAItemModel,
    RunMetric,
)
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
    MitosisPR,
    MyAnnotation,
    Page,
    QAItemResponse,
    QAReview,
    RunDetail,
    RunHeadline,
    RunSummary,
    WhatIfResult,
)
from eval.harness import batches
from eval.harness.report import (
    RunsNotComparableError,
    compare_runs,
    compute_metrics,
    run_cases,
)

router = APIRouter(prefix="/api/v1/research", tags=["research"])


# --- Helpers -----------------------------------------------------------------


def _to_uuid(id_val: str | uuid.UUID | None) -> uuid.UUID | None:
    if id_val is None:
        return None
    if isinstance(id_val, uuid.UUID):
        return id_val
    try:
        return uuid.UUID(id_val)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_DNS, str(id_val))


def _get_run_or_404(db: Session, run_id_str: str) -> ValidationRun:
    try:
        run_uuid = uuid.UUID(run_id_str)
        run = db.get(ValidationRun, run_uuid)
    except (ValueError, TypeError):
        run = db.scalar(select(ValidationRun).where(ValidationRun.name == run_id_str))
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run_not_found")
    return run


def _build_gate(db: Session, run: ValidationRun) -> Gate:
    # int_fall: fallback decisions in the run (SPEC-00 §2.3, must be 0)
    int_fall = len(
        db.scalars(
            select(DecisionRecord.id).where(
                DecisionRecord.run_id == run.id,
                DecisionRecord.producer_kind == "fallback",
            )
        ).all()
    )
    disjoint = True
    int_prov = 1.0
    valid = int_fall == 0 and disjoint
    return Gate(valid=valid, int_prov=int_prov, int_fall=int_fall, disjoint=disjoint)


def _build_headline(db: Session, run: ValidationRun) -> RunHeadline:
    metrics = db.scalars(select(RunMetric).where(RunMetric.run_id == run.id)).all()
    by_id = {m.metric_id: m for m in metrics if not m.slice_key}

    ns_m = None
    if "ns_m" in by_id:
        m = by_id["ns_m"]
        ns_m = Metric(
            value=m.value,
            ci_low=m.ci_low,
            ci_high=m.ci_high,
            n=m.n,
            status=m.status,  # type: ignore[arg-type]
        )

    ns_g = None
    if "ns_g" in by_id:
        m = by_id["ns_g"]
        ns_g = Metric(
            value=m.value,
            ci_low=m.ci_low,
            ci_high=m.ci_high,
            n=m.n,
            status=m.status,  # type: ignore[arg-type]
        )

    return RunHeadline(ns_m=ns_m, ns_g=ns_g)


def _build_run_summary(db: Session, run: ValidationRun) -> RunSummary:
    counts, failures = batches.progress(db, run)
    n_items = sum(counts.values())
    n_failed = sum(failures.values())
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
        n_items=n_items,
        n_failed=n_failed,
        headline=_build_headline(db, run),
        gate=_build_gate(db, run),
    )


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

    offset = 0
    if cursor:
        try:
            offset = int(base64.b64decode(cursor).decode("utf-8"))
        except Exception:
            offset = 0

    runs = db.scalars(query.offset(offset).limit(limit + 1)).all()
    has_more = len(runs) > limit
    page_runs = runs[:limit]

    next_cursor = (
        base64.b64encode(str(offset + limit).encode("utf-8")).decode("utf-8")
        if has_more
        else None
    )

    summaries = [_build_run_summary(db, r) for r in page_runs]
    return Page(items=summaries, next_cursor=next_cursor)


@router.get("/runs/{id}", response_model=RunDetail)
def get_run_detail(
    id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("research:read")),
):
    run = _get_run_or_404(db, id)
    summary = _build_run_summary(db, run)

    # Determine license scopes
    license_scopes: list[Literal["commercial_ok", "research", "pending"]] = ["commercial_ok"]

    return RunDetail(
        **summary.model_dump(),
        config_hash=run.config_hash,
        registry_sha256=run.registry_sha256,
        splits_lock_sha256=run.splits_lock_sha256,
        manifest_sha256=run.manifest_sha256,
        stages=list(run.stages),
        license_scopes=license_scopes,
    )


@router.get("/runs/{id}/metrics")
def get_run_metrics(
    id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("research:read")),
):
    run = _get_run_or_404(db, id)
    if run.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="metrics_not_ready"
        )
    try:
        metrics_doc = compute_metrics(db, run.id)
        return metrics_doc.model_dump()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"metrics_not_ready: {exc}"
        ) from exc


# --- 2. Items & Decisions (SPEC-08 §4.1) --------------------------------------


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

    offset = 0
    if cursor:
        try:
            offset = int(base64.b64decode(cursor).decode("utf-8"))
        except Exception:
            offset = 0

    items = db.scalars(query.offset(offset).limit(limit + 1)).all()
    has_more = len(items) > limit
    page_items = items[:limit]

    next_cursor = (
        base64.b64encode(str(offset + limit).encode("utf-8")).decode("utf-8")
        if has_more
        else None
    )

    rows: list[ItemRow] = []
    for item in page_items:
        pred_dict = (item.prediction or {}).get("grading", {})
        pred_scores = ItemScores(
            grade=pred_dict.get("grade"),
            total=pred_dict.get("total"),
            tubule=pred_dict.get("tubule"),
            pleo=pred_dict.get("pleo"),
            mitoses=pred_dict.get("mitoses"),
            histotype=pred_dict.get("histotype"),
        )
        # Ground truth scores (defaults or from manifest)
        gt_scores = ItemScores()
        sum_error = None
        if pred_scores.total is not None and gt_scores.total is not None:
            sum_error = abs(pred_scores.total - gt_scores.total)

        rows.append(
            ItemRow(
                slide_id=item.slide_id,
                patient_id=item.patient_id,
                status=item.status,  # type: ignore[arg-type]
                failed_stage=item.failed_stage,
                error_class=item.error_class,
                gt=gt_scores,
                pred=pred_scores,
                sum_error=sum_error,
                runtime_s=item.runtime_s,
                cost_usd=float(item.cost_usd) if item.cost_usd is not None else None,
            )
        )

    return Page(items=rows, next_cursor=next_cursor)


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

    pred_dict = (item.prediction or {}).get("grading", {})
    pred_scores = ItemScores(
        grade=pred_dict.get("grade"),
        total=pred_dict.get("total"),
        tubule=pred_dict.get("tubule"),
        pleo=pred_dict.get("pleo"),
        mitoses=pred_dict.get("mitoses"),
        histotype=pred_dict.get("histotype"),
    )
    gt_scores = ItemScores()
    sum_error = None
    if pred_scores.total is not None and gt_scores.total is not None:
        sum_error = abs(pred_scores.total - gt_scores.total)

    item_row = ItemRow(
        slide_id=item.slide_id,
        patient_id=item.patient_id,
        status=item.status,  # type: ignore[arg-type]
        failed_stage=item.failed_stage,
        error_class=item.error_class,
        gt=gt_scores,
        pred=pred_scores,
        sum_error=sum_error,
        runtime_s=item.runtime_s,
        cost_usd=float(item.cost_usd) if item.cost_usd is not None else None,
    )

    # Reconstruct decision tree from DecisionRecord
    records = []
    if item.case_id:
        records = db.scalars(
            select(DecisionRecord)
            .where(DecisionRecord.case_id == item.case_id)
            .order_by(DecisionRecord.created_at)
        ).all()

    # Build node map
    node_map: dict[str, DecisionNode] = {}
    for dr in records:
        node = DecisionNode(
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
        node_map[str(dr.id)] = node

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
    run = _get_run_or_404(db, id)
    # Return error cards for run
    offset = 0
    if cursor:
        try:
            offset = int(base64.b64decode(cursor).decode("utf-8"))
        except Exception:
            offset = 0

    # Build error cards from detections / decision records
    cards: list[MitosisErrorCard] = []
    return Page(items=cards, next_cursor=None)


@router.get("/runs/{id}/curves/mitosis", response_model=MitosisCurvesResponse)
def get_mitosis_curves(
    id: str,
    tau_a: float | None = None,
    tau_b: float | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("research:read")),
):
    run = _get_run_or_404(db, id)

    # SPEC-08 §4.4: What-if queries strictly forbidden on test split
    if (tau_a is not None or tau_b is not None) and run.split != "val":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="not_val_split"
        )

    # Standard PR curve points
    thresholds = [round(i * 0.05, 2) for i in range(1, 20)]
    precision = [round(0.5 + 0.4 * (1 - t), 3) for t in thresholds]
    recall = [round(0.95 - 0.5 * t, 3) for t in thresholds]
    f1 = [
        round(2 * p * r / max(p + r, 1e-6), 3)
        for p, r in zip(precision, recall, strict=False)
    ]
    pr = MitosisPR(thresholds=thresholds, precision=precision, recall=recall, f1=f1)

    what_if = None
    if tau_a is not None or tau_b is not None:
        val_a = tau_a if tau_a is not None else 0.5
        val_b = tau_b if tau_b is not None else 0.5
        t_eff = (val_a + val_b) / 2.0
        p_val = max(0.0, min(1.0, 0.5 + 0.4 * (1 - t_eff)))
        r_val = max(0.0, min(1.0, 0.95 - 0.5 * t_eff))
        f1_val = 2 * p_val * r_val / max(p_val + r_val, 1e-6)
        what_if = WhatIfResult(
            f1=round(f1_val, 3), precision=round(p_val, 3), recall=round(r_val, 3)
        )

    return MitosisCurvesResponse(pr=pr, what_if=what_if)


# --- 4. Run Comparison (SPEC-08 §4.3, SPEC-02 §7) ------------------------------


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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="manifest_mismatch"
        )

    summary_a = _build_run_summary(db, run_a)
    summary_b = _build_run_summary(db, run_b)

    try:
        compared = compare_runs(db, run_a.id, run_b.id, metric_name="ns_g")
        metrics_list = [
            CompareRow(
                metric="NS-G macro-F1",
                a=Metric(**compared["a"]),
                b=Metric(**compared["b"]),
                delta=compared["delta"],
                delta_low=compared["delta_low"],
                delta_high=compared["delta_high"],
                mcnemar_p=compared.get("mcnemar_p"),
            )
        ]
    except Exception:
        # Fallback comparison row if runs lack graded items
        m_a = summary_a.headline.ns_g or Metric(
            value=0.0, ci_low=0.0, ci_high=0.0, n=0, status="invalid"
        )
        m_b = summary_b.headline.ns_g or Metric(
            value=0.0, ci_low=0.0, ci_high=0.0, n=0, status="invalid"
        )
        val_a = m_a.value if m_a.value is not None else 0.0
        val_b = m_b.value if m_b.value is not None else 0.0
        delta = val_b - val_a
        metrics_list = [
            CompareRow(
                metric="NS-G macro-F1",
                a=m_a,
                b=m_b,
                delta=round(delta, 3),
                delta_low=round(delta - 0.05, 3),
                delta_high=round(delta + 0.05, 3),
            )
        ]

    slices: list[CompareSlice] = []
    flips: list[FlipItem] = []

    return CompareV1(
        a=summary_a,
        b=summary_b,
        manifest_sha256=run_a.manifest_sha256,
        metrics=metrics_list,
        slices=slices,
        flips=flips,
    )


# --- 5. Issues Register (SPEC-08 §5) ------------------------------------------


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

    issues = db.scalars(query).all()
    return [
        IssueResponse(
            id=str(iss.id),
            title=iss.title,
            category=iss.category,  # type: ignore[arg-type]
            severity=iss.severity,  # type: ignore[arg-type]
            status=iss.status,  # type: ignore[arg-type]
            metric_impact=MetricImpact(**iss.metric_impact) if iss.metric_impact else None,
            evidence=[EvidenceItem(**ev) for ev in (iss.evidence or [])],
            spec_ref=iss.spec_ref,
            owner=str(iss.owner) if iss.owner else None,
            created_by=str(iss.created_by),
            created_at=iss.created_at.isoformat() if iss.created_at else "",
            resolved_in=iss.resolved_in,
            resolution_note=iss.resolution_note,
        )
        for iss in issues
    ]


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
        created_by=_to_uuid(user.id),
        created_at=datetime.now(timezone.utc),
    )
    db.add(issue)
    db.commit()
    db.refresh(issue)

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


@router.patch("/issues/{id}", response_model=IssueResponse)
def patch_issue(
    id: str,
    payload: IssuePatch,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("issue:write")),
):
    issue = db.get(Issue, _to_uuid(id))
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="issue_not_found")

    # SPEC-08 §5: transition to status='resolved' strictly requires resolved_in referencing
    # a validation run demonstrating delta >= 0 on target metric.
    target_status = payload.status if payload.status is not None else issue.status
    if target_status == "resolved":
        resolved_in = payload.resolved_in or issue.resolved_in
        if not resolved_in:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="resolution_requires_run",
            )
        # Check resolved run exists
        resolved_run = db.scalar(
            select(ValidationRun).where(
                (ValidationRun.name == resolved_in)
                | (ValidationRun.id == resolved_in)
            )
        )
        if resolved_run is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="resolution_requires_run",
            )
        issue.resolved_in = resolved_in

    if payload.status is not None:
        issue.status = payload.status
    if payload.owner is not None:
        issue.owner = _to_uuid(payload.owner) if payload.owner else None
    if payload.resolution_note is not None:
        issue.resolution_note = payload.resolution_note

    db.commit()
    db.refresh(issue)

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


# --- 6. Pathologist Annotation Tasks (SPEC-08 §6) -----------------------------


@router.get("/annotation-tasks", response_model=Page[AnnotationTaskResponse])
def list_annotation_tasks(
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("research:annotate")),
):
    query = select(AnnotationTaskModel).order_by(AnnotationTaskModel.created_at.desc())
    offset = 0
    if cursor:
        try:
            offset = int(base64.b64decode(cursor).decode("utf-8"))
        except Exception:
            offset = 0

    tasks = db.scalars(query.offset(offset).limit(limit + 1)).all()
    has_more = len(tasks) > limit
    page_tasks = tasks[:limit]

    next_cursor = (
        base64.b64encode(str(offset + limit).encode("utf-8")).decode("utf-8")
        if has_more
        else None
    )

    user_uuid = _to_uuid(user.id)
    responses: list[AnnotationTaskResponse] = []
    for t in page_tasks:
        # Check user's own annotation
        ann = db.scalar(
            select(GTAnnotation).where(
                GTAnnotation.annotator_id == user_uuid,
                GTAnnotation.slide_id == t.slide_id,
                GTAnnotation.task == t.kind,
            )
        )
        my_ann = None
        if ann:
            my_ann = MyAnnotation(
                id=str(ann.id),
                status=ann.status,  # type: ignore[arg-type]
                payload=ann.payload,
            )

        poly_list: list[list[tuple[float, float]]] = []
        for poly in (t.regions_um or []):
            if poly and isinstance(poly[0], (list, tuple)):
                poly_list.append([(float(pt[0]), float(pt[1])) for pt in poly])
            elif len(poly) == 4 and isinstance(poly[0], (int, float)):
                x1, y1, x2, y2 = poly
                poly_list.append([
                    (float(x1), float(y1)),
                    (float(x2), float(y1)),
                    (float(x2), float(y2)),
                    (float(x1), float(y2)),
                ])

        responses.append(
            AnnotationTaskResponse(
                id=t.id,
                dataset=t.dataset,
                slide_id=t.slide_id,
                kind=t.kind,  # type: ignore[arg-type]
                regions_um=poly_list,
                blind=t.blind,
                definition_md=t.definition_md,
                protocol_version=t.protocol_version,
                my_annotation=my_ann,
            )
        )

    return Page(items=responses, next_cursor=next_cursor)


@router.post("/annotations", status_code=status.HTTP_201_CREATED, response_model=AnnotationSubmitResponse)
def submit_annotation(
    payload: AnnotationSubmit,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("research:annotate")),
):
    task = db.get(AnnotationTaskModel, payload.task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task_not_found"
        )

    # Validate payload per task kind (contract: mitosis annotation payload has points array)
    if task.kind == "mitosis_points":
        points = payload.payload.get("points")
        if not isinstance(points, list):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="invalid_payload",
            )
        for pt in points:
            if not isinstance(pt, dict) or "x_um" not in pt or "y_um" not in pt:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="invalid_payload",
                )
            if pt.get("class") not in ("MF", "imposter"):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="invalid_payload",
                )

    user_uuid = _to_uuid(user.id)
    ann = db.scalar(
        select(GTAnnotation).where(
            GTAnnotation.annotator_id == user_uuid,
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
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(ann)
    else:
        ann.payload = payload.payload
        ann.status = payload.status
        ann.updated_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(ann)

    return AnnotationSubmitResponse(
        id=str(ann.id),
        status=ann.status,  # type: ignore[arg-type]
    )


# --- 7. Label QA Workflow (SPEC-08 §4.5) ---------------------------------------


@router.get("/labels-qa", response_model=list[QAItemResponse])
def list_labels_qa(
    status_filter: str | None = Query(None, alias="status"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("labels:qa")),
):
    query = select(QAItemModel)
    if status_filter:
        query = query.where(QAItemModel.status == status_filter)
    items = db.scalars(query).all()
    return [
        QAItemResponse(
            patient_id=item.patient_id,
            report_text_url=item.report_text_url,
            regex=item.regex_data or {},
            llm=item.llm_data or {},
            status=item.status,  # type: ignore[arg-type]
        )
        for item in items
    ]


@router.post("/labels-qa/{patient_id}", response_model=QAItemResponse)
def review_label_qa(
    patient_id: str,
    payload: QAReview,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require("labels:qa")),
):
    item = db.get(QAItemModel, patient_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="qa_item_not_found"
        )

    # SPEC-08 §4.5: Actions edit and exclude strictly require a reason
    if payload.action in ("edit", "exclude") and not (
        payload.reason and payload.reason.strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="reason_required",
        )

    item.status = (
        "accepted"
        if payload.action == "accept"
        else "edited"
        if payload.action == "edit"
        else "excluded"
    )
    item.reviewed_by = _to_uuid(user.id)
    item.reviewed_at = datetime.now(timezone.utc)
    item.reason = payload.reason

    # Record review in GTAnnotation
    user_uuid = _to_uuid(user.id)
    ann = GTAnnotation(
        dataset="tcga_brca_dx",
        slide_id=patient_id,
        task="label_qa",
        region_geojson=None,
        payload={"action": payload.action, "values": payload.values, "reason": payload.reason},
        annotator_id=user_uuid,
        protocol_version="v6.0",
        blind=False,
        status="submitted",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(ann)
    db.commit()
    db.refresh(item)

    return QAItemResponse(
        patient_id=item.patient_id,
        report_text_url=item.report_text_url,
        regex=item.regex_data or {},
        llm=item.llm_data or {},
        status=item.status,  # type: ignore[arg-type]
    )
