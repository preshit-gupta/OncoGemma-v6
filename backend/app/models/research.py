"""Research view models (SPEC-08 §5, §6, §7; docs/contracts/research_v1.md).

Models for issues register, ground-truth annotations, fast run metrics, annotation tasks,
and label QA items.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import GUID

JSONType = JSON().with_variant(JSONB, "postgresql")

ISSUE_CATEGORIES = ("biological", "model", "staging", "technical")
ISSUE_SEVERITIES = ("critical", "high", "medium", "low")
ISSUE_STATUSES = ("open", "triaged", "in_progress", "resolved", "wont_fix")

ANNOTATION_TASKS = ("mitosis_points", "component_scores", "grade", "tumor_region", "label_qa")
ANNOTATION_STATUSES = ("draft", "submitted", "adjudicated")

QA_STATUSES = ("pending", "accepted", "edited", "excluded")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Issue(Base):
    __tablename__ = "issues"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    metric_impact: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    evidence: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    spec_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner: Mapped[uuid.UUID | None] = mapped_column(GUID, ForeignKey("users.id"), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    resolved_in: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(_in("category", ISSUE_CATEGORIES), name="ck_issues_category"),
        CheckConstraint(_in("severity", ISSUE_SEVERITIES), name="ck_issues_severity"),
        CheckConstraint(_in("status", ISSUE_STATUSES), name="ck_issues_status"),
        Index("ix_issues_status", "status"),
        Index("ix_issues_category", "category"),
    )


class GTAnnotation(Base):
    __tablename__ = "gt_annotations"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    dataset: Mapped[str] = mapped_column(Text, nullable=False)
    slide_id: Mapped[str] = mapped_column(Text, nullable=False)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    region_geojson: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    annotator_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("users.id"), nullable=False)
    protocol_version: Mapped[str] = mapped_column(Text, nullable=False)
    blind: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    adjudicates: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        CheckConstraint(_in("task", ANNOTATION_TASKS), name="ck_gt_annotations_task"),
        CheckConstraint(_in("status", ANNOTATION_STATUSES), name="ck_gt_annotations_status"),
        Index("ix_gt_annotations_slide", "dataset", "slide_id", "task"),
        Index("ix_gt_annotations_annotator", "annotator_id"),
    )


class RunMetric(Base):
    __tablename__ = "run_metrics"

    run_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("validation_runs.id", ondelete="CASCADE"), primary_key=True
    )
    metric_id: Mapped[str] = mapped_column(Text, primary_key=True)
    slice_key: Mapped[str] = mapped_column(Text, primary_key=True, default="")
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    ci_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    ci_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # final, provisional, invalid

    __table_args__ = (
        Index("ix_run_metrics_lookup", "run_id", "metric_id"),
    )


class AnnotationTaskModel(Base):
    __tablename__ = "annotation_tasks"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    dataset: Mapped[str] = mapped_column(Text, nullable=False)
    slide_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    regions_um: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    blind: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    definition_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    protocol_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_ann_tasks_dataset_slide", "dataset", "slide_id"),
    )


class QAItemModel(Base):
    __tablename__ = "qa_items"

    patient_id: Mapped[str] = mapped_column(Text, primary_key=True)
    dataset: Mapped[str] = mapped_column(Text, nullable=False)
    protocol_version: Mapped[str] = mapped_column(Text, nullable=False)
    report_text_url: Mapped[str] = mapped_column(Text, nullable=False)
    regex_data: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    llm_data: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(GUID, ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(_in("status", QA_STATUSES), name="ck_qa_items_status"),
        Index("ix_qa_items_status", "status"),
    )
