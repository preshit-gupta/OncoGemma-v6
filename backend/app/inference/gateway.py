"""Model gateway: the single egress for all inference (SPEC-01 §3.4).

Every model call goes through ``ModelGateway.invoke``. The gateway checks the inputs
against the registry's contract, calls the provider adapter with transport retries,
validates the answer strictly, caches it, and writes one DecisionRecord per attempt
naming the component that actually answered. It never returns a fallback: a failure
raises one of the errors in ``app.inference.errors``. Only ``invoke_or_fallback``
consults ``configs/fallbacks.yaml``, and only in clinical runs.
"""
import hashlib
import io
import math
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, NoReturn

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ValidationError

from app.core.model_registry import (
    FeatureInputContract,
    ImageInputContract,
    VertexEndpointModel,
    VertexGenAIModel,
)
from app.core.pipeline_config import PipelineConfig, canonical_json
from app.core.run_context import DecisionContext, RunMode
from app.core.tasks import DecisionStatus, EntityType, ProducerKind, Task
from app.inference import schemas
from app.inference.adapters.base import (
    Adapter,
    AdapterImage,
    AdapterRequest,
    CallRejected,
    CallTimeout,
    RawResponse,
    TransientCallError,
    Unavailable,
)
from app.inference.blobs import BlobStore
from app.inference.errors import (
    GatewayError,
    InputContractError,
    ModelCallError,
    ModelTimeoutError,
    ModelUnavailableError,
    SchemaInvalidError,
    UnpinnedModelError,
)
from app.inference.records import DecisionLog

MIME_TYPES = {"png": "image/png", "jpeg": "image/jpeg"}
_PIL_FORMATS = {"PNG": "png", "JPEG": "jpeg"}

# A Gemini model ID is pinned when it names a numbered or dated release
# (gemini-2.0-flash-001, gemini-2.5-flash-preview-05-2025), not a floating alias such
# as gemini-2.5-flash. EVAL refuses aliases (SPEC-01 §3.7).
PINNED_MODEL_ID = re.compile(r"-(\d{3}|\d{2}-\d{4})$")

_PROMPT_VARIABLE = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")

PromptValue = str | int | float | bool


class CacheIntegrityError(RuntimeError):
    """A cached output no longer validates against its output model."""


@dataclass(frozen=True)
class InputSpec:
    """What an image is: resolution, pixel size (width, height), colour handling, encoding."""

    mpp: float
    size_px: tuple[int, int]
    color: Literal["raw", "normalized"]
    format: Literal["png", "jpeg"]
    stain_profile_id: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "mpp": self.mpp,
            "size_px": list(self.size_px),
            "color": self.color,
            "format": self.format,
            "stain_profile_id": self.stain_profile_id,
        }


@dataclass(frozen=True)
class ImageInput:
    data: bytes
    spec: InputSpec


@dataclass(frozen=True)
class ModelInputs:
    images: tuple[ImageInput, ...] = ()
    # (N, D) features produced by registry model ``features_producer``.
    features: np.ndarray | None = None
    features_producer: str | None = None
    # A configs/prompts file name, rendered with typed ``prompt_vars`` ({{name}} placeholders).
    prompt_id: str | None = None
    prompt_vars: Mapping[str, PromptValue] = field(default_factory=dict)


@dataclass(frozen=True)
class EntityRef:
    type: EntityType
    id: str
    # Entity IDs covered by a *_batch record; written to a Parquet sidecar.
    ids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.type.is_batch != (self.ids is not None):
            raise ValueError(f"{self.type.value} entity {self.id!r}: ids are required for batches and only for them")


@dataclass(frozen=True)
class GatewayResult:
    output: BaseModel
    record_id: uuid.UUID
    cache_hit: bool
    producer_id: str
    producer_version: str


@dataclass(frozen=True)
class FallbackResult:
    """No automated verdict: the failure was allowed by configs/fallbacks.yaml (clinical only)."""

    record_id: uuid.UUID
    error: GatewayError
    output: None = None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def render_prompt(template: str, variables: Mapping[str, PromptValue]) -> str:
    """Replace ``{{name}}`` placeholders. Missing, unused or untyped variables are errors."""
    names = set(_PROMPT_VARIABLE.findall(template))
    missing, unused = names - set(variables), set(variables) - names
    if missing or unused:
        raise ValueError(f"prompt variables missing {sorted(missing)}, unused {sorted(unused)}")
    for name, value in variables.items():
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"prompt variable {name} has type {type(value).__name__}")
    return _PROMPT_VARIABLE.sub(lambda m: str(variables[m.group(1)]), template)


def _record_output(output: BaseModel) -> dict[str, Any]:
    """What the DecisionRecord stores. Bulky outputs (embeddings) define ``decision_output``."""
    summarise = getattr(output, "decision_output", None)
    return summarise() if callable(summarise) else output.model_dump(mode="json")


def _schema_sha256(output_model: type[BaseModel]) -> str:
    return sha256_hex(canonical_json(output_model.model_json_schema()).encode("utf-8"))


@dataclass
class _Call:
    """Everything the records of one ``invoke`` share."""

    task: Task
    producer_id: str
    producer_version: str
    endpoint: str | None
    prompt_id: str | None
    prompt_sha256: str | None
    input_sha256: str
    input_spec: dict[str, Any]
    params: dict[str, Any]
    ctx: DecisionContext
    entity: EntityRef
    entity_ids_uri: str | None = None


class ModelGateway:
    def __init__(
        self,
        config: PipelineConfig,
        adapters: Mapping[str, Adapter],
        log: DecisionLog,
        blobs: BlobStore,
        *,
        cache_enabled: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ):
        self.config = config
        self.registry = config.models
        self.adapters = dict(adapters)
        self.log = log
        self.blobs = blobs
        self.cache_enabled = cache_enabled
        self._sleep = sleep
        self._jitter = jitter

    # --- public API ---------------------------------------------------------------------

    def invoke(
        self,
        task: Task,
        producer_id: str,
        inputs: ModelInputs,
        ctx: DecisionContext,
        entity: EntityRef,
        output_model: type[BaseModel],
        params: Mapping[str, Any] | None = None,
    ) -> GatewayResult:
        entry = self.registry.models.get(producer_id)
        if entry is None:
            raise InputContractError(task.value, producer_id, "not a model in configs/models.yaml")

        call, prompt = self._prepare(task, producer_id, entry, inputs, ctx, entity, output_model, dict(params or {}))
        # The contract checks run before the entity sidecar is written, so a rejected
        # request leaves no blob behind.
        self._check_contract(call, entry, inputs, output_model)
        if entity.ids is not None:
            call.entity_ids_uri = self._write_entity_ids(ctx, entity)
        self._check_pinned(call, entry)
        if isinstance(entry, VertexEndpointModel) and entry.endpoint_id is None:
            self._fail(call, ModelUnavailableError, "endpoint_id is not configured in this environment", 0)

        cache_key = self._cache_key(call, output_model) if self._cache_on(entry, ctx) else None
        if cache_key is not None:
            cached = self._cache_get(call, cache_key, output_model)
            if cached is not None:
                return cached

        request = AdapterRequest(
            producer_id=producer_id,
            images=tuple(AdapterImage(img.data, MIME_TYPES[img.spec.format]) for img in inputs.images),
            features=inputs.features,
            prompt=prompt,
            output_model=output_model if entry.kind == "vlm" else None,
            generation=call.params.get("generation", {}),
        )
        result = self._call_with_retries(call, entry, request, output_model)
        if cache_key is not None:
            self.blobs.write(
                self._cache_path(task, cache_key),
                result.output.model_dump_json().encode("utf-8"),
                "application/json",
            )
        return result

    def invoke_or_fallback(
        self,
        task: Task,
        producer_id: str,
        inputs: ModelInputs,
        ctx: DecisionContext,
        entity: EntityRef,
        output_model: type[BaseModel],
        params: Mapping[str, Any] | None = None,
    ) -> GatewayResult | FallbackResult:
        """``invoke``, except that a failure allowed by the fallback policy yields no verdict.

        The policy re-raises in EVAL and SHADOW and for any failure it does not list. The
        caller must flag the entity ``needs_human`` when it gets a ``FallbackResult``.
        """
        try:
            return self.invoke(task, producer_id, inputs, ctx, entity, output_model, params)
        except GatewayError as error:
            self.config.fallbacks.resolve(task, error, ctx)
            record_id = uuid.uuid4()
            self.log.add({
                "id": record_id,
                "case_id": ctx.case_id,
                "stage_execution_id": ctx.stage_execution_id,
                "run_id": ctx.run_id,
                "stage": ctx.stage,
                "task": task.value,
                "entity_type": entity.type.value,
                "entity_id": entity.id,
                "entity_ids_uri": None,
                "producer_kind": ProducerKind.FALLBACK.value,
                "producer_id": "no_automated_verdict",
                # The policy is part of the configuration, so its version is the config hash.
                "producer_version": ctx.config_hash,
                "endpoint": None,
                "prompt_id": None,
                "prompt_sha256": None,
                "input_sha256": error.input_sha256,
                "input_spec": error.input_spec,
                "params": {"failed_record_id": error.record_id, "failed_producer_id": producer_id},
                "output": None,
                "raw_output_uri": None,
                "status": DecisionStatus.SKIPPED.value,
                "error_class": type(error).__name__,
                "error_detail": error.detail,
                "latency_ms": 0,
                "cost_usd": None,
                "cache_hit": False,
                "run_mode": ctx.run_mode.value,
                "config_hash": ctx.config_hash,
                "supersedes_id": None,
            })
            return FallbackResult(record_id=record_id, error=error)

    # --- request preparation --------------------------------------------------------------

    def _prepare(self, task, producer_id, entry, inputs, ctx, entity, output_model, params):
        generation = getattr(entry, "params", None)
        if generation is not None:
            if "generation" in params:
                raise InputContractError(task.value, producer_id, "'generation' is reserved for registry params")
            params["generation"] = generation.model_dump(mode="json", exclude_none=True)

        prompt = prompt_sha256 = None
        render_error = None
        if inputs.prompt_id is not None:
            template = self.config.prompts.get(inputs.prompt_id)
            if template is None:
                render_error = f"prompt {inputs.prompt_id!r} is not in configs/prompts"
            else:
                prompt_sha256 = sha256_hex(template.encode("utf-8"))
                try:
                    prompt = render_prompt(template, inputs.prompt_vars)
                except ValueError as exc:
                    render_error = str(exc)

        input_spec = {
            "images": [img.spec.as_json() for img in inputs.images],
            "features": None if inputs.features is None else {
                "producer": inputs.features_producer,
                "shape": list(inputs.features.shape),
                "dtype": str(inputs.features.dtype),
            },
            "prompt_vars": dict(inputs.prompt_vars),
        }
        hashed = {
            "images": [{"spec": img.spec.as_json(), "sha256": sha256_hex(img.data)} for img in inputs.images],
            "features": None if inputs.features is None else {
                **input_spec["features"],
                "sha256": sha256_hex(np.ascontiguousarray(inputs.features).tobytes()),
            },
            "prompt_vars": dict(inputs.prompt_vars),
            "params": params,
        }
        call = _Call(
            task=task,
            producer_id=producer_id,
            producer_version=entry.version,
            endpoint=self._endpoint_of(entry),
            prompt_id=inputs.prompt_id,
            prompt_sha256=prompt_sha256,
            input_sha256=sha256_hex(canonical_json(hashed).encode("utf-8")),
            input_spec=input_spec,
            params=params,
            ctx=ctx,
            entity=entity,
        )
        if render_error is not None:
            self._fail(call, InputContractError, render_error, 0)
        return call, prompt

    @staticmethod
    def _endpoint_of(entry) -> str | None:
        if entry.provider in ("vertex_endpoint_predict", "vertex_endpoint_raw_predict"):
            return None if entry.endpoint_id is None else f"{entry.region}/{entry.endpoint_id}"
        if entry.provider == "vertex_genai":
            return entry.model
        return getattr(entry, "artifact_uri", None)

    def _check_contract(self, call: _Call, entry, inputs: ModelInputs, output_model) -> None:
        def reject(detail: str) -> None:
            self._fail(call, InputContractError, detail, 0)

        declared = getattr(entry, "output_schema", None)
        if declared is not None and declared != output_model.__name__:
            reject(f"registry output_schema is {declared}, caller expects {output_model.__name__}")

        is_vlm = entry.kind == "vlm"
        if is_vlm != (inputs.prompt_id is not None):
            reject("a prompt is required for VLMs and allowed only for them")
        if getattr(entry, "requires_image", False) and not inputs.images:
            reject("this model requires an image and none was given")

        contract = getattr(entry, "input", None)
        if isinstance(contract, FeatureInputContract):
            if inputs.images or inputs.features is None:
                reject(f"expects features from {contract.features}, not images")
            if inputs.features_producer != contract.features:
                reject(f"expects features from {contract.features}, got {inputs.features_producer}")
            if inputs.features.ndim != 2 or inputs.features.shape[0] == 0:
                reject(f"features must be a non-empty (N, D) array, got shape {inputs.features.shape}")
        elif inputs.features is not None:
            reject("this model does not take features")

        for index, image in enumerate(inputs.images):
            problem = self._image_problem(image, contract if isinstance(contract, ImageInputContract) else None)
            if problem is not None:
                reject(f"image {index}: {problem}")

        limits = getattr(entry, "limits", None)
        if limits is not None and inputs.images:
            if len(inputs.images) > limits.max_batch:
                reject(f"{len(inputs.images)} images exceed max_batch {limits.max_batch}")
            # Providers receive images base64-encoded.
            request_bytes = sum(4 * math.ceil(len(img.data) / 3) for img in inputs.images)
            if request_bytes > limits.max_request_bytes:
                reject(f"{request_bytes} encoded bytes exceed max_request_bytes {limits.max_request_bytes}")

    @staticmethod
    def _image_problem(image: ImageInput, contract: ImageInputContract | None) -> str | None:
        spec = image.spec
        if contract is not None:
            mpp_error = abs(spec.mpp - contract.mpp)
            if mpp_error > contract.mpp_tolerance and not math.isclose(mpp_error, contract.mpp_tolerance):
                return f"mpp {spec.mpp} is outside {contract.mpp} ± {contract.mpp_tolerance}"
            if list(spec.size_px) != list(contract.size_px):
                return f"size_px {list(spec.size_px)} != {contract.size_px}"
            if spec.color != contract.color:
                return f"color {spec.color} != {contract.color}"
            if spec.format != contract.format:
                return f"format {spec.format} != {contract.format}"
        # The declared spec must describe the bytes actually sent.
        try:
            with Image.open(io.BytesIO(image.data)) as decoded:
                actual_format, actual_size = _PIL_FORMATS.get(decoded.format), decoded.size
        except (UnidentifiedImageError, OSError) as exc:
            return f"not a decodable image ({exc})"
        if actual_format != spec.format:
            return f"declared {spec.format} but the bytes are {decoded.format}"
        if tuple(actual_size) != tuple(spec.size_px):
            return f"declared size_px {list(spec.size_px)} but the image is {list(actual_size)}"
        return None

    def _check_pinned(self, call: _Call, entry) -> None:
        if call.ctx.run_mode is RunMode.EVAL and isinstance(entry, VertexGenAIModel):
            if not PINNED_MODEL_ID.search(entry.model):
                self._fail(
                    call,
                    UnpinnedModelError,
                    f"model {entry.model!r} is a floating alias; EVAL needs a pinned version ID",
                    0,
                )

    def _write_entity_ids(self, ctx: DecisionContext, entity: EntityRef) -> str:
        buffer = io.BytesIO()
        pq.write_table(pa.table({"entity_id": pa.array(entity.ids, type=pa.string())}), buffer)
        path = f"cases/{ctx.case_id}/decisions/entities/{uuid.uuid4()}.parquet"
        return self.blobs.write(path, buffer.getvalue(), "application/octet-stream")

    # --- cache ------------------------------------------------------------------------------

    def _cache_on(self, entry, ctx: DecisionContext) -> bool:
        """EVAL and SHADOW always cache; CLINICAL only for deterministic models."""
        if not self.cache_enabled:
            return False
        if ctx.run_mode.fails_loud:
            return True
        generation = getattr(entry, "params", None)
        return entry.kind != "vlm" or (generation is not None and generation.temperature == 0)

    @staticmethod
    def _cache_key(call: _Call, output_model: type[BaseModel]) -> str:
        # The output schema is part of the key, so a schema change never serves stale answers.
        material = [
            call.producer_id,
            call.producer_version,
            call.prompt_sha256,
            call.input_sha256,
            call.params,
            _schema_sha256(output_model),
        ]
        return sha256_hex(canonical_json(material).encode("utf-8"))

    @staticmethod
    def _cache_path(task: Task, key: str) -> str:
        return f"cache/{task.value}/{key}.json"

    def _cache_get(self, call: _Call, key: str, output_model: type[BaseModel]) -> GatewayResult | None:
        started = time.perf_counter()
        cached = self.blobs.read(self._cache_path(call.task, key))
        if cached is None:
            return None
        try:
            output = output_model.model_validate_json(cached)
        except ValidationError as exc:
            raise CacheIntegrityError(f"cache entry {key} for {call.task.value} is invalid: {exc}") from exc
        record_id = self._record(
            call,
            DecisionStatus.OK,
            latency_ms=self._ms_since(started),
            output=_record_output(output),
            cache_hit=True,
        )
        return GatewayResult(output, record_id, True, call.producer_id, call.producer_version)

    # --- the call ---------------------------------------------------------------------------

    def _call_with_retries(self, call: _Call, entry, request: AdapterRequest, output_model) -> GatewayResult:
        adapter = self.adapters.get(entry.provider)
        if adapter is None:
            self._fail(call, ModelUnavailableError, f"no adapter is installed for provider {entry.provider}", 0)
        max_attempts, deadline_s = self.registry.call_limits(call.producer_id)
        schema_retries_left = getattr(entry, "schema_retries", 0)
        transport_failures = 0

        while True:
            started = time.perf_counter()
            try:
                raw = adapter.call(entry, request, deadline_s)
            except (TransientCallError, CallTimeout) as exc:
                error_type = ModelTimeoutError if isinstance(exc, CallTimeout) else ModelUnavailableError
                transport_failures += 1
                if transport_failures >= max_attempts:
                    self._fail(call, error_type, f"{exc} (attempt {transport_failures} of {max_attempts})", self._ms_since(started))
                self._record(
                    call,
                    DecisionStatus(error_type.status),
                    latency_ms=self._ms_since(started),
                    error=(error_type.__name__, f"{exc} (attempt {transport_failures} of {max_attempts}; retrying)"),
                )
                self._sleep(self._backoff(transport_failures))
                continue
            except CallRejected as exc:
                self._fail(call, ModelCallError, str(exc), self._ms_since(started))
            except Unavailable as exc:
                self._fail(call, ModelUnavailableError, str(exc), self._ms_since(started))
            latency_ms = self._ms_since(started)

            raw_uri = None
            if raw.text is not None:
                raw_uri = self.blobs.write(
                    f"cases/{call.ctx.case_id}/decisions/raw/{uuid.uuid4()}.txt",
                    raw.text.encode("utf-8"),
                    "text/plain; charset=utf-8",
                )
            if raw.endpoint is not None:
                call.endpoint = raw.endpoint

            try:
                output = self._parse(raw, output_model)
            except (schemas.SchemaInvalidError, ValidationError) as exc:
                if schema_retries_left > 0:
                    schema_retries_left -= 1
                    self._record(
                        call,
                        DecisionStatus.SCHEMA_INVALID,
                        latency_ms=latency_ms,
                        raw_output_uri=raw_uri,
                        error=(SchemaInvalidError.__name__, f"{exc} (re-asking)"),
                    )
                    continue
                self._fail(call, SchemaInvalidError, str(exc), latency_ms, raw_output_uri=raw_uri, cause=exc)

            record_id = self._record(
                call, DecisionStatus.OK, latency_ms=latency_ms, output=_record_output(output), raw_output_uri=raw_uri
            )
            return GatewayResult(output, record_id, False, call.producer_id, call.producer_version)

    @staticmethod
    def _parse(raw: RawResponse, output_model: type[BaseModel]) -> BaseModel:
        if raw.text is not None:
            return schemas.parse_json_strict(output_model, raw.text)
        if raw.data is None:
            raise schemas.SchemaInvalidError(output_model.__name__, "the response carried no data")
        return output_model.model_validate(raw.data)

    def _backoff(self, failures: int) -> float:
        """Full jitter: uniform(0, min(cap, base * 2**(failures - 1)))."""
        policy = self.registry.call_policy
        return self._jitter(0.0, min(policy.backoff_cap_s, policy.backoff_base_s * 2 ** (failures - 1)))

    # --- records ------------------------------------------------------------------------------

    @staticmethod
    def _ms_since(started: float) -> int:
        return int(round((time.perf_counter() - started) * 1000))

    def _record(
        self,
        call: _Call,
        status: DecisionStatus,
        *,
        latency_ms: int,
        output: dict[str, Any] | None = None,
        raw_output_uri: str | None = None,
        error: tuple[str, str] | None = None,
        cache_hit: bool = False,
    ) -> uuid.UUID:
        record_id = uuid.uuid4()
        ctx = call.ctx
        self.log.add({
            "id": record_id,
            "case_id": ctx.case_id,
            "stage_execution_id": ctx.stage_execution_id,
            "run_id": ctx.run_id,
            "stage": ctx.stage,
            "task": call.task.value,
            "entity_type": call.entity.type.value,
            "entity_id": call.entity.id,
            "entity_ids_uri": call.entity_ids_uri,
            "producer_kind": (ProducerKind.SHADOW if ctx.run_mode is RunMode.SHADOW else ProducerKind.MODEL).value,
            "producer_id": call.producer_id,
            "producer_version": call.producer_version,
            "endpoint": call.endpoint,
            "prompt_id": call.prompt_id,
            "prompt_sha256": call.prompt_sha256,
            "input_sha256": call.input_sha256,
            "input_spec": call.input_spec,
            "params": call.params,
            "output": output,
            "raw_output_uri": raw_output_uri,
            "status": status.value,
            "error_class": None if error is None else error[0],
            "error_detail": None if error is None else error[1],
            "latency_ms": latency_ms,
            "cost_usd": None,
            "cache_hit": cache_hit,
            "run_mode": ctx.run_mode.value,
            "config_hash": ctx.config_hash,
            "supersedes_id": None,
        })
        return record_id

    def _fail(
        self,
        call: _Call,
        error_type: type[GatewayError],
        detail: str,
        latency_ms: int,
        *,
        raw_output_uri: str | None = None,
        cause: BaseException | None = None,
    ) -> NoReturn:
        """Record the failure, then raise it."""
        error = error_type(call.task.value, call.producer_id, detail, entity=(call.entity.type.value, call.entity.id))
        record_id = self._record(
            call,
            DecisionStatus(error_type.status),
            latency_ms=latency_ms,
            raw_output_uri=raw_output_uri,
            error=(error_type.__name__, detail),
        )
        error.record_id = str(record_id)
        error.input_sha256 = call.input_sha256
        error.input_spec = call.input_spec
        raise error from cause
