"""Scratch-space check before a worker claims a stage (SPEC-02 §6.2).

Handlers stream the slide into a per-execution temp dir. On Cloud Run that filesystem is memory,
so a worker claims a stage only when it has ``SCRATCH_HEADROOM_FACTOR`` times the slide's size
free; otherwise the stage stays queued for a worker with room, and the refusal is logged.
"""
import shutil
import tempfile
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.slide_source import SlideSourceMissingError
from app.core.slide_source import slide_size_bytes as source_size_bytes
from app.models.slide import Slide
from app.models.stage_execution import StageExecution


class SlideObjectMissing(SlideSourceMissingError):
    """The slide row points at a GCS object (or DICOM series) that does not exist."""


@dataclass(frozen=True)
class ScratchCheck:
    ok: bool
    needed_bytes: int
    free_bytes: int


def slide_size_bytes(session: Session, execution: StageExecution) -> int | None:
    """Size of the case's slide (a DICOM series sums its instances); None when the case has no slide yet or it is not in GCS."""
    slide = session.scalars(select(Slide).where(Slide.case_id == execution.case_id)).first()
    if slide is None or not (slide.gcs_uri_original or "").startswith("gs://"):
        return None
    try:
        return source_size_bytes(slide.gcs_uri_original)
    except SlideSourceMissingError as exc:
        raise SlideObjectMissing(str(exc)) from exc


def check_scratch(session: Session, execution: StageExecution, factor: float) -> ScratchCheck:
    free = shutil.disk_usage(tempfile.gettempdir()).free
    size = slide_size_bytes(session, execution)
    needed = 0 if size is None else int(size * factor)
    return ScratchCheck(ok=needed <= free, needed_bytes=needed, free_bytes=free)
