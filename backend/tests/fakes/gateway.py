"""Fakes for the model gateway: scripted adapters, in-memory blobs, a ready-made gateway."""
import io
import json
import uuid
from typing import Any, Callable

from PIL import Image

from app.core.pipeline_config import PipelineConfig, get_config_hash
from app.core.run_context import DecisionContext, RunMode
from app.inference.adapters.base import AdapterRequest, RawResponse
from app.inference.gateway import ImageInput, InputSpec, ModelGateway
from app.inference.records import DecisionLog

Step = RawResponse | BaseException | Callable[[AdapterRequest], RawResponse]


class FakeAdapter:
    """A provider that plays back ``steps`` in order, one per call.

    A step is a ``RawResponse`` to return, an exception to raise, or a function of the
    request returning a ``RawResponse``. After the steps run out, ``then`` is used for
    every further call; with no ``then`` an extra call fails the test.
    """

    def __init__(self, *steps: Step, then: Step | None = None):
        self.steps = list(steps)
        self.then = then
        self.calls: list[tuple[Any, AdapterRequest, float]] = []

    def call(self, entry, request: AdapterRequest, timeout_s: float) -> RawResponse:
        self.calls.append((entry, request, timeout_s))
        if self.steps:
            step = self.steps.pop(0)
        elif self.then is not None:
            step = self.then
        else:
            raise AssertionError(f"unexpected call {len(self.calls)} to {request.producer_id}")
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step(request)
        return step


class InMemoryBlobStore:
    def __init__(self, bucket: str = "fake-artifacts"):
        self.bucket = bucket
        self.blobs: dict[str, bytes] = {}
        self.reads: list[str] = []

    def read(self, path: str) -> bytes | None:
        self.reads.append(path)
        return self.blobs.get(path)

    def write(self, path: str, data: bytes, content_type: str) -> str:
        self.blobs[path] = data
        return f"gs://{self.bucket}/{path}"


def make_gateway(
    config: PipelineConfig,
    adapters: dict[str, Any],
    *,
    blobs: InMemoryBlobStore | None = None,
    log: DecisionLog | None = None,
    cache_enabled: bool = True,
    sleeps: list[float] | None = None,
) -> ModelGateway:
    """A gateway over fakes. Backoff sleeps are recorded in ``sleeps`` instead of slept."""
    recorded = [] if sleeps is None else sleeps
    return ModelGateway(
        config,
        adapters,
        DecisionLog() if log is None else log,
        InMemoryBlobStore() if blobs is None else blobs,
        cache_enabled=cache_enabled,
        sleep=recorded.append,
        jitter=lambda low, high: high,
    )


def decision_context(run_mode: RunMode = RunMode.CLINICAL, stage: str = "mitosis", **overrides) -> DecisionContext:
    values = {
        "case_id": uuid.uuid4(),
        "stage_execution_id": uuid.uuid4(),
        "stage": stage,
        "run_mode": run_mode,
        "run_id": None,
        "config_hash": get_config_hash(),
    }
    values.update(overrides)
    return DecisionContext(**values)


def png_image(size_px: tuple[int, int], mpp: float, color: str = "raw", rgb=(200, 120, 160)) -> ImageInput:
    buffer = io.BytesIO()
    Image.new("RGB", size_px, rgb).save(buffer, format="PNG")
    return ImageInput(buffer.getvalue(), InputSpec(mpp=mpp, size_px=size_px, color=color, format="png"))


def json_text(payload: dict) -> RawResponse:
    return RawResponse(text=json.dumps(payload))


def statuses(log: DecisionLog) -> list[str]:
    return [row["status"] for row in log.pending()]
