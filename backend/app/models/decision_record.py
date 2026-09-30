"""Decision records (SPEC-01 §3.3).

One row per decision that can change a count or a score, and one row per request
batch for bulk inference. Each row names the component that actually produced the
decision, so every metric can be attributed to a model version.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CHAR,
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.run_context import RunMode
from app.core.tasks import DecisionStatus, ProducerKind
from app.models.base import GUID

JSONType = JSON().with_variant(JSONB, "postgresql")


def _in(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


class DecisionRecord(Base):
    __tablename__ = "decision_records"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    stage_execution_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("stage_executions.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("validation_runs.id", name="fk_dr_run"), nullable=True
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    entity_ids_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    producer_kind: Mapped[str] = mapped_column(Text, nullable=False)
    producer_id: Mapped[str] = mapped_column(Text, nullable=False)
    producer_version: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    input_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    input_spec: Mapped[dict] = mapped_column(JSONType, nullable=False)
    params: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    output: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    raw_output_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    run_mode: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("decision_records.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(_in("producer_kind", ProducerKind), name="ck_decision_records_producer_kind"),
        CheckConstraint(_in("status", DecisionStatus), name="ck_decision_records_status"),
        CheckConstraint(_in("run_mode", RunMode), name="ck_decision_records_run_mode"),
        Index("ix_dr_case_stage", "case_id", "stage"),
        Index("ix_dr_run", "run_id"),
        Index("ix_dr_producer", "task", "producer_id", "producer_version"),
        Index("ix_dr_entity", "case_id", "entity_type", "entity_id"),
    )
