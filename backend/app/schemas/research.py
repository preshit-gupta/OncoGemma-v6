"""Pydantic schemas for the research view API (docs/contracts/research_v1.md; SPEC-08 §7).

Strict schemas for runs, metrics, items, error cards, curves, compare, issues,
annotations, and label QA.
"""
from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar
from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Metric(StrictModel):
    value: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    n: int
    status: Literal["final", "provisional", "invalid"]


class Gate(StrictModel):
    valid: bool
    int_prov: float
    int_fall: int
    disjoint: bool


class RunHeadline(StrictModel):
    ns_m: Metric | None = None
    ns_g: Metric | None = None


class RunSummary(StrictModel):
    id: str
    name: str
    dataset: str
    split: Literal["train", "val", "test", "adhoc"]  # an ad-hoc batch has no split (SPEC-02 §6.1)
    arm: str | None = None
    status: Literal["created", "running", "completed", "cancelled", "failed"]
    is_locked_test: bool
    created_by: str
    created_at: str
    finished_at: str | None = None
    n_items: int
    n_failed: int
    headline: RunHeadline
    gate: Gate


class RunDetail(RunSummary):
    config_hash: str
    registry_sha256: str
    splits_lock_sha256: str | None = None
    manifest_sha256: str
    stages: list[str]
    license_scopes: list[Literal["commercial_ok", "research", "pending"]]


class Page(StrictModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


class ItemScores(StrictModel):
    grade: int | None = None
    total: int | None = None
    tubule: int | None = None
    pleo: int | None = None
    mitoses: int | None = None
    histotype: str | None = None


class ItemRow(StrictModel):
    slide_id: str
    patient_id: str
    status: Literal["pending", "running", "succeeded", "failed", "excluded_qc", "cancelled"]
    failed_stage: str | None = None
    error_class: str | None = None
    gt: ItemScores
    pred: ItemScores
    sum_error: int | float | None = None
    runtime_s: float | None = None
    cost_usd: float | None = None


class DecisionNode(StrictModel):
    id: str
    task: str
    entity_type: str
    entity_id: str
    producer_kind: Literal["model", "heuristic", "human", "fallback", "shadow"]
    producer_id: str
    producer_version: str
    status: Literal["ok", "schema_invalid", "timeout", "unavailable", "error", "skipped"]
    input_spec: dict[str, Any]
    output: Any = None
    latency_ms: int
    children: list[DecisionNode] = Field(default_factory=list)


class ItemDetailResponse(StrictModel):
    item: ItemRow
    case_id: str
    decisions: list[DecisionNode]


class MitosisErrorCard(StrictModel):
    slide_id: str
    kind: Literal["fp", "fn"]
    centroid_um: tuple[float, float]
    crop_url: str
    gt_points_um: list[tuple[float, float]]
    pred_points_um: list[tuple[float, float]]
    p_a: float | None = None
    p_b: float | None = None
    vlm_verdict: str | None = None
    rule_override: bool | None = None
    decision_record_id: str | None = None


class MitosisPR(StrictModel):
    thresholds: list[float]
    precision: list[float]
    recall: list[float]
    f1: list[float]


class WhatIfResult(StrictModel):
    f1: float
    precision: float
    recall: float


class MitosisCurvesResponse(StrictModel):
    pr: MitosisPR
    what_if: WhatIfResult | None = None


class CompareRow(StrictModel):
    metric: str
    a: Metric
    b: Metric
    delta: float
    delta_low: float
    delta_high: float
    mcnemar_p: float | None = None


class CompareSlice(StrictModel):
    slice: str
    key: str
    metric: str
    delta: float
    delta_low: float
    delta_high: float


class FlipItem(StrictModel):
    slide_id: str
    component: Literal["grade", "tubule", "pleo", "mitoses"]
    a_correct: bool
    b_correct: bool


class CompareV1(StrictModel):
    a: RunSummary
    b: RunSummary
    manifest_sha256: str
    metrics: list[CompareRow]
    slices: list[CompareSlice]
    flips: list[FlipItem]


class MetricImpact(StrictModel):
    metric: str
    slice: str | None = None
    est_delta: float | None = None


class EvidenceItem(StrictModel):
    run_id: str
    slide_id: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    decision_record_id: str | None = None


class IssueResponse(StrictModel):
    id: str
    title: str
    category: Literal["biological", "model", "staging", "technical"]
    severity: Literal["critical", "high", "medium", "low"]
    status: Literal["open", "triaged", "in_progress", "resolved", "wont_fix"]
    metric_impact: MetricImpact | None = None
    evidence: list[EvidenceItem]
    spec_ref: str | None = None
    owner: str | None = None
    created_by: str
    created_at: str
    resolved_in: str | None = None
    resolution_note: str | None = None


class IssueCreate(StrictModel):
    title: str
    category: Literal["biological", "model", "staging", "technical"]
    severity: Literal["critical", "high", "medium", "low"]
    metric_impact: MetricImpact | None = None
    evidence: list[EvidenceItem]
    spec_ref: str | None = None


class IssuePatch(StrictModel):
    status: Literal["open", "triaged", "in_progress", "resolved", "wont_fix"] | None = None
    owner: str | None = None
    resolved_in: str | None = None
    resolution_note: str | None = None


class MyAnnotation(StrictModel):
    id: str
    status: Literal["draft", "submitted", "adjudicated"]
    payload: Any


class AnnotationTaskResponse(StrictModel):
    id: str
    dataset: str
    slide_id: str
    kind: Literal["mitosis_points", "component_scores", "grade", "tumor_region"]
    regions_um: list[list[tuple[float, float]]]
    blind: bool
    definition_md: str
    protocol_version: str
    my_annotation: MyAnnotation | None = None


class AnnotationSubmit(StrictModel):
    task_id: str
    payload: dict[str, Any]
    status: Literal["draft", "submitted"]


class AnnotationSubmitResponse(StrictModel):
    id: str
    status: Literal["draft", "submitted", "adjudicated"]


class QAItemResponse(StrictModel):
    patient_id: str
    report_text_url: str
    regex: dict[str, int | None]
    llm: dict[str, Any]
    status: Literal["pending", "accepted", "edited", "excluded"]


class QAReview(StrictModel):
    action: Literal["accept", "edit", "exclude"]
    values: dict[str, int | None] | None = None
    reason: str | None = None
