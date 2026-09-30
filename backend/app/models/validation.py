"""Validation runs and their items (SPEC-02 §5.3, §6.1).

A run evaluates one manifest split through the app's own stage handlers and queue. A batch
created in the app is a run too (``dataset='adhoc'``). Each item is one slide of the run,
keyed by ``(run_id, slide_id)``; its case is created by the harness.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CHAR,
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import GUID

JSONType = JSON().with_variant(JSONB, "postgresql")

RUN_MODES = ("auto", "manual")
RUN_STATUSES = ("created", "running", "completed", "cancelled", "failed")
ITEM_STATUSES = ("pending", "running", "succeeded", "failed", "excluded_qc", "cancelled")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class ValidationRun(Base):
    __tablename__ = "validation_runs"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    dataset: Mapped[str] = mapped_column(Text, nullable=False)
    split: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_uri: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    # Ordered stage names, e.g. ["ingest", "preprocess", "qc", "triage", "mitosis", "grading"].
    # SPEC-02 says TEXT[]; a JSON list keeps SQLite (tests) and PostgreSQL on one model.
    stages: Mapped[list] = mapped_column(JSONType, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    # In-flight items the controller allows; kept so `resume` and the batch API reuse it.
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    config_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    registry_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    # SHA-256 of SPLITS.lock for a dataset split; NULL for an ad-hoc batch, which has no split.
    splits_lock_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    arm: Mapped[str | None] = mapped_column(Text, nullable=True)  # ablation arm id (SPEC-06/07)
    is_locked_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    status: Mapped[str] = mapped_column(Text, nullable=False, default="created")
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metrics_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The worker driving this run's controller, and until when (eval/harness/driver.py).
    controller_lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    controller_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(_in("mode", RUN_MODES), name="ck_validation_runs_mode"),
        CheckConstraint(_in("status", RUN_STATUSES), name="ck_validation_runs_status"),
    )


class ValidationItem(Base):
    __tablename__ = "validation_items"

    run_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("validation_runs.id", ondelete="CASCADE"), primary_key=True
    )
    slide_id: Mapped[str] = mapped_column(Text, primary_key=True)  # the dataset's slide id
    patient_id: Mapped[str] = mapped_column(Text, nullable=False)
    case_id: Mapped[uuid.UUID | None] = mapped_column(GUID, ForeignKey("cases.id"), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    failed_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    prediction: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    runtime_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)

    __table_args__ = (
        CheckConstraint(_in("status", ITEM_STATUSES), name="ck_validation_items_status"),
    )
