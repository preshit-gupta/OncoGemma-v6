"""Model registry (SPEC-01 §3.7).

``configs/models.yaml`` is validated into ``ModelRegistry``. It is one section of
``PipelineConfig``, so its canonical form is part of ``config_hash``.
"""
from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from app.core.config_types import (
    Mpp,
    NonEmptyStr,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    RegistryKey,
    Sha256Hex,
    StrictModel,
)

ModelKind = Literal["embedding", "classifier", "detector", "vlm"]
# Request and response layout of a Vertex endpoint's serving container
# (app.inference.adapters.vertex_endpoint.WIRE_FORMATS).
WireFormat = Literal["path_foundation_v1", "kongnet_midog_v1", "kongnet_midog_v2", "medgemma_chat_v1"]
PythonModulePath = Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$")]


class ImageInputContract(StrictModel):
    """Pixels a model accepts. The gateway rejects anything else (SPEC-01 AC5)."""

    mpp: Mpp
    mpp_tolerance: PositiveFloat
    size_px: Annotated[list[PositiveInt], Field(min_length=2, max_length=2)]
    color: Literal["raw", "normalized"]
    format: Literal["png", "jpeg"]


class FeatureInputContract(StrictModel):
    """The model consumes another registry model's output."""

    features: RegistryKey


class RequestLimits(StrictModel):
    max_batch: PositiveInt
    max_request_bytes: PositiveInt
    qps: PositiveFloat
    deadline_s: PositiveFloat
    max_attempts: PositiveInt


class CallPolicy(StrictModel):
    """How the gateway calls a model (SPEC-01 §3.4). An entry's ``limits`` override the defaults.

    Only transport errors are retried, with exponential backoff and full jitter: before
    retry ``n`` (1-based) the gateway sleeps ``uniform(0, min(backoff_cap_s, backoff_base_s * 2**(n-1)))``.
    """

    default_max_attempts: PositiveInt
    default_deadline_s: PositiveFloat
    backoff_base_s: PositiveFloat
    backoff_cap_s: PositiveFloat

    @model_validator(mode="after")
    def _ordered(self) -> "CallPolicy":
        if self.backoff_base_s > self.backoff_cap_s:
            raise ValueError("backoff_base_s must not exceed backoff_cap_s")
        return self


class TrainedOn(StrictModel):
    snapshot_id: NonEmptyStr
    splits_lock_sha256: Sha256Hex


class GenerationParams(StrictModel):
    temperature: Annotated[float, Field(ge=0, le=2)]
    max_output_tokens: PositiveInt | None = None


class VertexEndpointModel(StrictModel):
    kind: ModelKind
    provider: Literal["vertex_endpoint_raw_predict", "vertex_endpoint_predict"]
    wire_format: WireFormat
    # None means the endpoint is not configured in this environment.
    endpoint_id: NonEmptyStr | None
    region: NonEmptyStr
    version: NonEmptyStr
    requires_image: bool
    input: ImageInputContract | None = None
    output_schema: NonEmptyStr | None = None
    limits: RequestLimits | None = None
    # Generation settings; required for kind: vlm and refused otherwise.
    params: GenerationParams | None = None
    # Re-asks with the identical prompt after a schema-invalid answer (VLMs only).
    schema_retries: NonNegativeInt = 0
    license_ref: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _vlm_fields(self) -> "VertexEndpointModel":
        is_vlm = self.kind == "vlm"
        if is_vlm != (self.params is not None):
            raise ValueError("params is required for kind: vlm and allowed only for it")
        if self.schema_retries and not is_vlm:
            raise ValueError("schema_retries is allowed only for kind: vlm")
        return self


class LocalArtifactModel(StrictModel):
    kind: ModelKind
    provider: Literal["local_sklearn"]
    artifact_uri: NonEmptyStr
    artifact_sha256: Sha256Hex
    version: NonEmptyStr
    input: FeatureInputContract | ImageInputContract
    output_schema: NonEmptyStr | None = None
    trained_on: TrainedOn | None = None
    license_ref: NonEmptyStr | None = None


class VertexGenAIModel(StrictModel):
    kind: Literal["vlm"]
    provider: Literal["vertex_genai"]
    # The pinned model ID is this entry's version.
    model: NonEmptyStr
    region: NonEmptyStr
    requires_image: bool
    params: GenerationParams
    deadline_s: PositiveFloat | None = None
    max_attempts: PositiveInt | None = None
    schema_retries: NonNegativeInt = 0
    license_ref: NonEmptyStr | None = None

    @property
    def version(self) -> str:
        return self.model


ModelEntry = Annotated[
    Union[VertexEndpointModel, LocalArtifactModel, VertexGenAIModel],
    Field(discriminator="provider"),
]


class HeuristicEntry(StrictModel):
    """A non-learned producer, available only as an explicitly configured ablation arm."""

    version: NonEmptyStr
    module: PythonModulePath


class ModelRegistry(StrictModel):
    schema_version: Literal[1]
    call_policy: CallPolicy
    models: dict[RegistryKey, ModelEntry]
    heuristics: dict[RegistryKey, HeuristicEntry]

    @model_validator(mode="after")
    def _check_references(self) -> "ModelRegistry":
        shared = sorted(set(self.models) & set(self.heuristics))
        if shared:
            raise ValueError(f"keys used for both a model and a heuristic: {shared}")
        for key, entry in self.models.items():
            contract = getattr(entry, "input", None)
            if isinstance(contract, FeatureInputContract):
                upstream = self.models.get(contract.features)
                if upstream is None or upstream.kind != "embedding":
                    raise ValueError(
                        f"models.{key}.input.features must name an embedding model, got {contract.features!r}"
                    )
        return self

    def call_limits(self, key: str) -> tuple[int, float]:
        """``(max_attempts, deadline_s)`` for model ``key``. Unknown keys raise KeyError."""
        entry = self.models[key]
        limits = getattr(entry, "limits", None)
        if limits is not None:
            return limits.max_attempts, limits.deadline_s
        max_attempts = getattr(entry, "max_attempts", None)
        deadline_s = getattr(entry, "deadline_s", None)
        return (
            self.call_policy.default_max_attempts if max_attempts is None else max_attempts,
            self.call_policy.default_deadline_s if deadline_s is None else deadline_s,
        )

    def version_of(self, key: str) -> str:
        """Version string recorded for a model or heuristic. Unknown keys raise KeyError."""
        if key in self.models:
            return self.models[key].version
        return self.heuristics[key].version
