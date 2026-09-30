"""Soft-deleted cases are gone for every case-scoped route (SPEC-03 §5.3.1).

``reject_soft_deleted_case`` is mounted on each case-scoped router in ``create_app``. It finds
the case from the ``case_id`` (or ``slide_id``) path parameter or the JSON body's ``case_id``
and answers 404 when that case has ``deleted_at`` set. Audit history stays readable.
"""
import json
import uuid

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.db import get_db
from app.models.case import Case
from app.models.slide import Slide


def _as_uuid(value: object) -> uuid.UUID | None:
    """The UUID in ``value``; None for a malformed ID, which the route itself rejects."""
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _is_soft_deleted(db: Session, case_id: object | None, slide_id: object | None) -> bool:
    if case_id is not None:
        case_uid = _as_uuid(case_id)
        if case_uid is None:
            return False
        stmt = select(Case.deleted_at).where(Case.id == case_uid)
    elif slide_id is not None:
        slide_uid = _as_uuid(slide_id)
        if slide_uid is None:
            return False
        stmt = select(Case.deleted_at).join(Slide, Slide.case_id == Case.id).where(Slide.id == slide_uid)
    else:
        return False
    return db.scalars(stmt).first() is not None


async def reject_soft_deleted_case(request: Request, db: Session = Depends(get_db)) -> None:
    case_id = request.path_params.get("case_id")
    slide_id = request.path_params.get("slide_id")
    if case_id is None and request.headers.get("content-type", "").startswith("application/json"):
        raw = await request.body()
        try:
            parsed = json.loads(raw) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Body is not valid JSON") from exc
        if isinstance(parsed, dict):
            case_id = parsed.get("case_id")
    if await run_in_threadpool(_is_soft_deleted, db, case_id, slide_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
