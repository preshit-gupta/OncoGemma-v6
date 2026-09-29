import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from sqlalchemy import String, Integer, Float, DateTime, ForeignKey, CheckConstraint, JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import GUID

# Use JSONB on PostgreSQL, JSON on SQLite
JSON_TYPE = JSON().with_variant(JSONB, "postgresql")

class Grading(Base):
    __tablename__ = "gradings"

    case_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("cases.id", ondelete="CASCADE"), primary_key=True
    )
    tubule_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tubule_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pleo_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mitotic_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    nottingham_sum: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    grade: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # No default: an unassessed type stays NULL rather than reading as IDC-NST (SPEC-01 §3.9).
    histologic_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    type_confirmed_by: Mapped[str] = mapped_column(String, nullable=False, default="unconfirmed")
    machine: Mapped[Dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    overrides: Mapped[Dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )

    case = relationship("Case", back_populates="grading")

    __table_args__ = (
        CheckConstraint(
            "grade = CASE WHEN tubule_score + pleo_score + mitotic_score <= 5 THEN 1 "
            "WHEN tubule_score + pleo_score + mitotic_score <= 7 THEN 2 "
            "ELSE 3 END",
            name="check_nottingham_grade_calc"
        ),
    )
