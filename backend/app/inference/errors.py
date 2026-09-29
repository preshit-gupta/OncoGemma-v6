"""Errors raised by the model gateway (SPEC-01 §3.4, §3.6).

The gateway never returns a fallback: every failure is one of these, carries the
task, producer and entity it concerns, and has already been written as a
DecisionRecord (``record_id``) when it is raised.
"""
from typing import Any, ClassVar


class GatewayError(RuntimeError):
    """Base class. ``status`` is the DecisionRecord status the failure is recorded with."""

    status: ClassVar[str] = "error"

    def __init__(
        self,
        task: str,
        producer_id: str,
        detail: str,
        *,
        entity: tuple[str, str] | None = None,
    ):
        super().__init__(f"{type(self).__name__}: task={task} producer={producer_id}: {detail}")
        self.task = task
        self.producer_id = producer_id
        self.detail = detail
        self.entity = entity
        # Set by the gateway once the failure is recorded.
        self.record_id: str | None = None
        self.input_sha256: str | None = None
        self.input_spec: dict[str, Any] | None = None

    def to_error_json(self) -> dict[str, Any]:
        """The ``stage_executions.error`` payload for a stage this error failed (SPEC-01 §3.6)."""
        return {
            "class": type(self).__name__,
            "task": self.task,
            "producer_id": self.producer_id,
            "entity": None if self.entity is None else {"type": self.entity[0], "id": self.entity[1]},
            "record_id": self.record_id,
            "detail": self.detail,
        }


class ModelUnavailableError(GatewayError):
    """The model could not be reached: not configured, or transport errors outlasted the retries."""

    status = "unavailable"


class ModelTimeoutError(GatewayError):
    """The call exceeded the registry's per-call deadline on every attempt."""

    status = "timeout"


class SchemaInvalidError(GatewayError):
    """The model answered, but the answer failed strict validation (SPEC-01 §3.5).

    The parser-level ``app.inference.schemas.SchemaInvalidError`` is its ``__cause__``.
    """

    status = "schema_invalid"


class InputContractError(GatewayError):
    """The inputs do not match the registry's input contract; the model was not called."""

    status = "error"


class ModelCallError(GatewayError):
    """The provider rejected the request (not a transport error), so retrying cannot help."""

    status = "error"


class UnpinnedModelError(GatewayError):
    """An EVAL call named a floating model alias instead of a pinned version (SPEC-01 §3.7)."""

    status = "error"


# Errors a configs/fallbacks.yaml entry may name. InputContractError, ModelCallError and
# UnpinnedModelError are code or configuration defects, so they always fail the stage.
FALLBACK_ELIGIBLE = {cls.__name__: cls for cls in (ModelUnavailableError, ModelTimeoutError, SchemaInvalidError)}
