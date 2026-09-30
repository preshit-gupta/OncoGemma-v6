"""Batch processing in the application (SPEC-02 §6.1; docs/contracts/research_v1.md).

A batch is a validation run. Creating one only records it; the stage workers drive it
(``eval/harness/driver.py``), so the request returns at once and the batch survives restarts.
"""
import asyncio
import json
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.auth.deps import CurrentUser, require
from app.auth.service import record_auth_event
from app.core.db import get_db
from app.core.pipeline_config import get_pipeline_config
from app.models.validation import ValidationRun
from eval.harness import batches
from eval.harness.controller import cancel_run, retry_items
from eval.harness.runs import EmptySplitError, LockedTestSplitError, RunConfigError
from eval.splits import LockMismatchError

router = APIRouter(prefix="/api/v1/batches", tags=["batches"])

# The events stream sends progress at least this often (contract: every <= 2 s).
EVENT_INTERVAL_S = 2


class ManifestSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_uri: str = Field(pattern=r"^gs://")
    split: Literal["train", "val", "test"]
    confirm_test_access: str | None = None  # required for split=test; written to the audit log


class PrefixSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gcs_prefix: str = Field(pattern=r"^gs://")
    specimen_type: Literal["resection", "core_biopsy"]
    mpp_override: float | None = Field(default=None, gt=0)


class CreateBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    source: ManifestSource | PrefixSource
    stages: list[str]
    mode: Literal["auto", "manual"] = "auto"
    concurrency: int = Field(default=1, ge=1)


class RetryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statuses: list[Literal["failed", "cancelled"]] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> "RetryBatch":
        self.statuses = sorted(set(self.statuses))
        return self


def get_run(db: Session, batch_id: uuid.UUID) -> ValidationRun:
    run = db.get(ValidationRun, batch_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch_not_found")
    return run


@router.post("", status_code=status.HTTP_201_CREATED)
def create_batch(req: CreateBatch, db: Session = Depends(get_db), user: CurrentUser = Depends(require("batch:create"))):
    common = dict(name=req.name, stages=tuple(req.stages), mode=req.mode, concurrency=req.concurrency, actor=user.id)
    try:
        if isinstance(req.source, PrefixSource):
            run = batches.create_batch_from_prefix(
                db, gcs_prefix=req.source.gcs_prefix, specimen_type=req.source.specimen_type,
                mpp_override=req.source.mpp_override, **common,
            )
        else:
            if req.source.split == "test" and "eval:test_split" not in get_pipeline_config().auth.permissions_of(user.role):
                record_auth_event(db, actor=user.id, event_type="permission_denied",
                                  payload={"permissions": ["eval:test_split"], "method": "POST", "path": router.prefix})
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
            run = batches.create_batch_from_manifest(
                db, manifest_uri=req.source.manifest_uri, split=req.source.split,
                test_access_reason=req.source.confirm_test_access, **common,
            )
    except LockedTestSplitError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"test_split_locked: {exc}") from exc
    except (RunConfigError, EmptySplitError, LockMismatchError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return {"batch_id": str(run.id)}


@router.get("")
def list_batches(db: Session = Depends(get_db), user: CurrentUser = Depends(require("batch:read"))):
    runs = db.scalars(select(ValidationRun).order_by(ValidationRun.created_at.desc(), ValidationRun.id)).all()
    return [batches.summary(db, run) for run in runs]


@router.get("/{batch_id}")
def get_batch(batch_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(require("batch:read"))):
    return batches.detail(db, get_run(db, batch_id))


@router.get("/{batch_id}/events")
async def batch_events(batch_id: uuid.UUID, db: Session = Depends(get_db),
                       user: CurrentUser = Depends(require("batch:read"))):
    """Server-sent events: progress every EVENT_INTERVAL_S seconds until the batch ends."""
    get_run(db, batch_id)
    make_session = sessionmaker(bind=db.get_bind(), autoflush=False)

    async def stream():
        while True:
            session = make_session()
            try:
                run = session.get(ValidationRun, batch_id)
                counts, failures = batches.progress(session, run)
                final = run.status in ("completed", "cancelled", "failed")
                payload = {"status": run.status, "counts": counts, "failures_by_error_class": failures}
            finally:
                session.close()
            yield f"data: {json.dumps(payload, sort_keys=True)}\n\n"
            if final:
                return
            await asyncio.sleep(EVENT_INTERVAL_S)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/{batch_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_batch(batch_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(require("batch:cancel"))):
    run = get_run(db, batch_id)
    return {"batch_id": str(run.id), "cancelled_items": cancel_run(db, run.id, user.id)}


@router.post("/{batch_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_batch(batch_id: uuid.UUID, req: RetryBatch, db: Session = Depends(get_db),
                user: CurrentUser = Depends(require("batch:create"))):
    run = get_run(db, batch_id)
    return {"batch_id": str(run.id), "retried_items": retry_items(db, run.id, tuple(req.statuses), user.id)}
