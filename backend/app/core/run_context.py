"""Run mode and decision context (SPEC-01 §3.2).

Handlers build a ``DecisionContext`` from their stage execution and pass it to
every gateway call. They never read a global flag to decide how to behave.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Literal, get_args
from uuid import UUID

Stage = Literal["ingest", "preprocess", "qc", "triage", "mitosis", "grading"]
STAGES: tuple[str, ...] = get_args(Stage)


class RunMode(str, Enum):
    CLINICAL = "clinical"   # interactive app use
    EVAL = "eval"           # harness / batch validation runs
    SHADOW = "shadow"       # SPEC-09 candidate models scored in parallel, never user-visible

    @property
    def fails_loud(self) -> bool:
        """EVAL and SHADOW never take a fallback (SPEC-01 §3.6)."""
        return self is not RunMode.CLINICAL


class DecisionContextError(ValueError):
    """A stage execution cannot yield a valid decision context."""


@dataclass(frozen=True)
class DecisionContext:
    case_id: UUID
    stage_execution_id: UUID
    stage: Stage
    run_mode: RunMode
    run_id: UUID | None           # validation_runs.id when a harness run queued the execution (SPEC-02)
    config_hash: str

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise DecisionContextError(f"unknown stage {self.stage!r}")
        if not isinstance(self.run_mode, RunMode):
            raise DecisionContextError(f"run_mode must be a RunMode, got {self.run_mode!r}")
        if self.run_id is not None and self.run_mode is RunMode.CLINICAL:
            raise DecisionContextError("a clinical execution cannot belong to a validation run")
        if len(self.config_hash) != 64:
            raise DecisionContextError(f"config_hash must be a sha256 hex digest, got {self.config_hash!r}")

    @classmethod
    def for_stage_execution(cls, stage_execution, config_hash: str) -> "DecisionContext":
        """Context for a ``StageExecution`` row. Its ``run_mode`` and ``run_id`` columns decide the mode and run."""
        try:
            run_mode = RunMode(stage_execution.run_mode)
        except ValueError as exc:
            raise DecisionContextError(
                f"stage execution {stage_execution.id} has unknown run_mode {stage_execution.run_mode!r}"
            ) from exc
        return cls(
            case_id=UUID(str(stage_execution.case_id)),
            stage_execution_id=UUID(str(stage_execution.id)),
            stage=stage_execution.stage,
            run_mode=run_mode,
            run_id=None if stage_execution.run_id is None else UUID(str(stage_execution.run_id)),
            config_hash=config_hash,
        )
