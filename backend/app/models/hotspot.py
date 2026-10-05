"""
SQLAlchemy ORM model for Hotspots (v4.2 Hotspot Triage).
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Float, Integer, Boolean, Text, DateTime, ForeignKey, JSON
from sqlalchemy.orm import declarative_base

from app.core.db import Base
from app.models.base import GUID


class Hotspot(Base):
    __tablename__ = "hotspots"

    id = Column(String, primary_key=True)
    case_id = Column(GUID, ForeignKey("cases.id", ondelete="CASCADE"), primary_key=True, index=True)
    stage_execution_id = Column(GUID, ForeignKey("stage_executions.id", ondelete="CASCADE"), nullable=False)
    polygon_um = Column(JSON, nullable=False) # List of [x, y] in base micrometers
    area_mm2 = Column(Float, nullable=True)
    prob_mean = Column(Float, nullable=True)
    prob_max = Column(Float, nullable=True)
    source = Column(String, nullable=False, default="model") # model | pathologist_added | pathologist_modified
    excluded = Column(Boolean, nullable=False, default=False)
    exclude_reason = Column(Text, nullable=True)
    rank = Column(Integer, nullable=True)
    rank_score = Column(Float, nullable=True)
    score_kind = Column(Text, nullable=True)
    tumor_fraction = Column(Float, nullable=True)
    prescan_expected = Column(Float, nullable=True)
    window_um = Column(Float, nullable=True) # side of the padded frame (polygon_um)
    center_um = Column(JSON, nullable=True) # [x, y] of the HPF circle; null on rows from before 0017
    hpf_diameter_um = Column(Float, nullable=True)
    tissue_fraction = Column(Float, nullable=True) # over the circle
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
