"""A slide's fitted stain profile (SPEC-04 §3.4).

Stage 2 fits it once per slide. Every later stage applies it through ``StainTransform`` and never
re-estimates. A re-run of Stage 2 adds a row; the newest row is the slide's profile.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import CHAR, JSON, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import GUID

JSONType = JSON().with_variant(JSONB, "postgresql")


class StainProfile(Base):
    __tablename__ = "stain_profiles"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    slide_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("slides.id", ondelete="CASCADE"), nullable=False)
    fitter_version: Mapped[str] = mapped_column(Text, nullable=False)
    reference_id: Mapped[str] = mapped_column(Text, nullable=False)
    w_src: Mapped[list] = mapped_column(JSONType, nullable=False)  # 2x3 unit stain vectors of the slide
    maxc_src: Mapped[list] = mapped_column(JSONType, nullable=False)
    w_tgt: Mapped[list] = mapped_column(JSONType, nullable=False)  # the reference's
    maxc_tgt: Mapped[list] = mapped_column(JSONType, nullable=False)
    # A 'degenerate' row repeats the reference in w_src/maxc_src so the columns stay filled; the
    # transform refuses such a row, so those values are never applied.
    fit_status: Mapped[str] = mapped_column(Text, nullable=False)
    n_patches: Mapped[int] = mapped_column(Integer, nullable=False)
    mosaic_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        CheckConstraint("fit_status IN ('fitted', 'sparse', 'degenerate')", name="ck_stain_profiles_fit_status"),
        Index("ix_stain_profiles_slide", "slide_id", "created_at"),
    )
