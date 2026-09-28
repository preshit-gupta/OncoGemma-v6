"""Runs one stage execution (SPEC-01 §3.2, §3.6).

The polling worker, the Cloud Run job and the Cloud Tasks webhook all run stages
through ``execute_stage``, so every execution:

- is stamped with the active ``config_hash``;
- gets a ``StageRuntime``: the ``PipelineConfig``, a ``DecisionContext`` built from
  the row's ``run_mode``, and a ``ModelGateway`` whose DecisionRecords are written
  with the stage's final status, including when it fails;
- on failure is marked ``failed`` with a JSON ``error`` naming the error class, and
  for gateway errors the task, producer and entity.
"""
import json
import traceback
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.pipeline_config import PipelineConfig, get_config_hash, get_pipeline_config
from app.core.run_context import DecisionContext
from app.inference.adapters.registry import production_adapters
from app.inference.blobs import GcsBlobStore
from app.inference.errors import GatewayError
from app.inference.gateway import ModelGateway
from app.inference.records import DecisionLog
from app.models.stage_execution import StageExecution
from worker.grading import run_grading
from worker.ingest import run_ingest
from worker.mitosis import run_mitosis
from worker.preprocess import run_preprocess
from worker.qc import run_qc
from worker.runtime import StageRuntime
from worker.triage import run_triage

GatewayFactory = Callable[[PipelineConfig, DecisionLog], ModelGateway]
StageHandler = Callable[[StageExecution, Session, StageRuntime], tuple[str, dict]]


def production_gateway(config: PipelineConfig, log: DecisionLog) -> ModelGateway:
    return ModelGateway(config, production_adapters(), log, GcsBlobStore(settings.GCS_ARTIFACTS_BUCKET))


def _without_runtime(handler: Callable[[StageExecution, Session], tuple[str, dict]]) -> StageHandler:
    """Handlers not yet moved onto the gateway (WP-2.3 moves them stage by stage)."""

    def run(stage_execution: StageExecution, session: Session, runtime: StageRuntime) -> tuple[str, dict]:
        return handler(stage_execution, session)

    run.__name__ = handler.__name__
    return run


STAGE_HANDLERS: dict[str, StageHandler] = {
    "ingest": _without_runtime(run_ingest),
    "preprocess": _without_runtime(run_preprocess),
    "qc": _without_runtime(run_qc),
    "triage": run_triage,
    "mitosis": _without_runtime(run_mitosis),
    "grading": _without_runtime(run_grading),
}


class StageFailedError(RuntimeError):
    """The stage failed; ``stage_executions.error`` holds ``error``."""

    def __init__(self, stage_execution_id, error: dict):
        super().__init__(f"stage execution {stage_execution_id} failed: {error['class']}: {error['detail']}")
        self.error = error


def stage_error_json(exc: BaseException) -> dict:
    """The ``stage_executions.error`` payload for ``exc``."""
    if isinstance(exc, GatewayError):
        payload = exc.to_error_json()
    else:
        payload = {"class": type(exc).__name__, "detail": str(exc)}
    payload["traceback"] = "".join(traceback.format_exception(exc))
    return payload


def mark_running(stage_execution: StageExecution) -> None:
    stage_execution.status = "running"
    stage_execution.started_at = datetime.now(timezone.utc)
    stage_execution.config_hash = get_config_hash()


def execute_stage(
    session: Session,
    stage_execution: StageExecution,
    *,
    handlers: dict[str, StageHandler] | None = None,
    gateway_factory: GatewayFactory | None = None,
) -> StageExecution:
    """Run a stage execution that ``mark_running`` marked and the caller committed.

    Returns the finished execution, or raises StageFailedError after recording the failure.
    Without ``gateway_factory`` the gateway uses the production adapters.
    """
    config = get_pipeline_config()
    config_hash = get_config_hash()
    handler = (STAGE_HANDLERS if handlers is None else handlers)[stage_execution.stage]
    make_gateway = production_gateway if gateway_factory is None else gateway_factory
    log = DecisionLog()
    stage_execution_id = stage_execution.id

    try:
        ctx = DecisionContext.for_stage_execution(stage_execution, config_hash)
        runtime = StageRuntime(config=config, ctx=ctx, gateway=make_gateway(config, log))
        out_uri, model_versions = handler(stage_execution, session, runtime)
        if stage_execution.status == "running":
            stage_execution.status = "done"
        stage_execution.output_ref = out_uri
        stage_execution.model_versions = model_versions
        stage_execution.completed_at = datetime.now(timezone.utc)
        log.flush(session)
        session.commit()
        return stage_execution
    except Exception as exc:  # the stage boundary: record the failure, then re-raise
        session.rollback()
        error = stage_error_json(exc)
        failed = session.get(StageExecution, stage_execution_id)
        failed.status = "failed"
        failed.error = json.dumps(error)
        failed.completed_at = datetime.now(timezone.utc)
        log.flush(session)
        session.commit()
        raise StageFailedError(stage_execution_id, error) from exc
