"""What a stage handler receives besides its stage execution and session (SPEC-01 §3.2)."""
from dataclasses import dataclass

from app.core.pipeline_config import PipelineConfig
from app.core.run_context import DecisionContext
from app.inference.gateway import ModelGateway


@dataclass(frozen=True)
class StageRuntime:
    config: PipelineConfig
    ctx: DecisionContext
    gateway: ModelGateway
