"""Fallback policy (SPEC-01 §3.6).

``configs/fallbacks.yaml`` lists the only failures a stage may survive, and only in
``run_mode=clinical``. EVAL and SHADOW always fail loud. The policy is part of
``PipelineConfig``, so it is covered by ``config_hash``.
"""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.core.config_types import StrictModel
from app.core.run_context import DecisionContext
from app.core.tasks import Task
from app.inference.errors import FALLBACK_ELIGIBLE, GatewayError

FallbackErrorName = Literal[tuple(FALLBACK_ELIGIBLE)]
# Strict mode accepts only Task instances; YAML gives the value string, which must match exactly.
TaskName = Annotated[Task, Field(strict=False)]


class FallbackEntry(StrictModel):
    task: TaskName
    on: Annotated[list[FallbackErrorName], Field(min_length=1)]
    # None is the only target: "no automated verdict". The entity is flagged needs_human.
    to: None

    @model_validator(mode="after")
    def _unique_errors(self) -> "FallbackEntry":
        if len(set(self.on)) != len(self.on):
            raise ValueError(f"fallback for {self.task.value} lists an error twice: {self.on}")
        return self


class FallbackPolicy(StrictModel):
    fallbacks: list[FallbackEntry]

    @model_validator(mode="after")
    def _one_entry_per_task(self) -> "FallbackPolicy":
        tasks = [entry.task for entry in self.fallbacks]
        duplicated = sorted({task.value for task in tasks if tasks.count(task) > 1})
        if duplicated:
            raise ValueError(f"more than one fallback entry for tasks {duplicated}")
        return self

    def lookup(self, task: Task, error: GatewayError) -> FallbackEntry | None:
        for entry in self.fallbacks:
            if entry.task is task and type(error).__name__ in entry.on:
                return entry
        return None

    def resolve(self, task: Task, error: GatewayError, ctx: DecisionContext) -> FallbackEntry:
        """Return the entry that allows surviving ``error``, or re-raise it."""
        if ctx.run_mode.fails_loud:
            raise error
        entry = self.lookup(task, error)
        if entry is None:
            raise error
        return entry
