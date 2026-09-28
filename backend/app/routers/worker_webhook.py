"""
OncoGemma v5 - Cloud Tasks Internal Stage Execution Webhook.
Targeted by Google Cloud Tasks HTTP tasks to execute pipeline stages
inside a Cloud Run instance without requiring any background polling loop.
"""

import logging
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.stage_execution import StageExecution
from worker.execution import STAGE_HANDLERS, StageFailedError, execute_stage, mark_running

logger = logging.getLogger("oncogemma.worker_webhook")

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])


class ExecuteStagePayload(BaseModel):
    case_id: str
    stage: str
    stage_exec_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None


@router.post("/execute-stage", status_code=status.HTTP_200_OK)
def execute_stage_webhook(
    body: ExecuteStagePayload,
    db: Session = Depends(get_db),
    x_cloudtasks_taskname: Optional[str] = Header(None)
):
    """
    HTTP handler invoked by Google Cloud Tasks to execute a stage asynchronously.
    """
    case_id_str = body.case_id
    stage_name = body.stage.lower()

    if stage_name not in STAGE_HANDLERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown stage '{stage_name}'. Valid stages: {list(STAGE_HANDLERS.keys())}"
        )

    logger.info(f"[CloudTasks Webhook] Received task '{x_cloudtasks_taskname}' for stage '{stage_name}' (case: {case_id_str})")

    # Locate or create StageExecution
    stage_exec: Optional[StageExecution] = None
    if body.stage_exec_id:
        try:
            stage_exec = db.get(StageExecution, UUID(body.stage_exec_id))
        except Exception:
            stage_exec = db.get(StageExecution, body.stage_exec_id)

    if not stage_exec:
        try:
            c_uuid = UUID(case_id_str)
        except Exception:
            c_uuid = case_id_str
            
        stage_exec = (
            db.query(StageExecution)
            .filter(StageExecution.case_id == c_uuid, StageExecution.stage == stage_name)
            .order_by(StageExecution.attempt.desc())
            .first()
        )

    if not stage_exec:
        stage_exec = StageExecution(
            case_id=UUID(case_id_str) if isinstance(case_id_str, str) else case_id_str,
            stage=stage_name,
            attempt=1,
            input_ref=body.payload or {}
        )
        db.add(stage_exec)
    mark_running(stage_exec)
    db.commit()
    db.refresh(stage_exec)

    try:
        execute_stage(db, stage_exec)
    except StageFailedError as exc:
        logger.error(f"[CloudTasks Webhook Error] Stage '{stage_name}' failed for case {case_id_str}: {exc}\n{exc.error['traceback']}")
        # Return 500 so Cloud Tasks can retry the task according to queue retry policy
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Stage execution failed: {exc.error['class']}: {exc.error['detail']}"
        )

    logger.info(f"[CloudTasks Webhook] Successfully executed stage '{stage_name}' for case {case_id_str} (status: {stage_exec.status}).")
    return {
        "status": "success",
        "stage": stage_name,
        "stage_execution_id": str(stage_exec.id),
        "output_ref": stage_exec.output_ref
    }
