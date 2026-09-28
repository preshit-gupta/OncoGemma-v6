"""Build a ``StageRuntime`` for calling a stage handler directly in a test."""
import uuid

from app.core.pipeline_config import PipelineConfig, get_config_hash, get_pipeline_config
from app.core.run_context import DecisionContext, RunMode
from app.inference.records import DecisionLog
from tests.fakes.gateway import InMemoryBlobStore, make_gateway
from worker.runtime import StageRuntime


def make_runtime(
    stage_execution,
    adapters: dict | None = None,
    *,
    config: PipelineConfig | None = None,
    run_mode: RunMode | None = None,
    log: DecisionLog | None = None,
    blobs: InMemoryBlobStore | None = None,
) -> StageRuntime:
    """A runtime over fake adapters. Unsaved executions get a fresh id for the context."""
    config = get_pipeline_config() if config is None else config
    mode = RunMode(stage_execution.run_mode or RunMode.CLINICAL.value) if run_mode is None else run_mode
    ctx = DecisionContext(
        case_id=uuid.UUID(str(stage_execution.case_id)) if _is_uuid(stage_execution.case_id) else uuid.uuid5(
            uuid.NAMESPACE_URL, str(stage_execution.case_id)
        ),
        stage_execution_id=uuid.UUID(str(stage_execution.id)) if stage_execution.id else uuid.uuid4(),
        stage=stage_execution.stage,
        run_mode=mode,
        run_id=None,
        config_hash=get_config_hash(),
    )
    gateway = make_gateway(config, adapters or {}, log=log, blobs=blobs)
    return StageRuntime(config=config, ctx=ctx, gateway=gateway)


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True
