import uuid
from datetime import datetime, timezone
from sqlalchemy import CheckConstraint, String, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import GUID

class Case(Base):
    __tablename__ = "cases"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open") # open, needs_rescan, done
    # resection | core_biopsy | unknown (SPEC-04 §3.2). 'unknown' until someone states it; preprocess refuses it.
    specimen_type: Mapped[str] = mapped_column(String, nullable=False, default="unknown", server_default="unknown")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc)
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        index=True
    )

    slides = relationship("Slide", back_populates="case", cascade="all, delete-orphan", passive_deletes=True)
    stage_executions = relationship("StageExecution", back_populates="case", cascade="all, delete-orphan", passive_deletes=True)
    grading = relationship("Grading", back_populates="case", uselist=False, cascade="all, delete-orphan", passive_deletes=True)
    hotspots = relationship("Hotspot", cascade="all, delete-orphan", passive_deletes=True)
    detections = relationship("Detection", cascade="all, delete-orphan", passive_deletes=True)
    hpf_sites = relationship("HpfSite", cascade="all, delete-orphan", passive_deletes=True)

    __table_args__ = (
        CheckConstraint("specimen_type IN ('resection', 'core_biopsy', 'unknown')", name="ck_cases_specimen_type"),
    )
