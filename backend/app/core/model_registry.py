"""Model registry (SPEC-01 §3.7).

``configs/models.yaml`` is validated into ``ModelRegistry``. It is one section of
``PipelineConfig``, so its canonical form is part of ``config_hash``.
"""
from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from app.core.config_types import (
    Mpp,
    NonEmptyStr,
    PositiveFloat,
    PositiveInt,
    RegistryKey,
    Sha256Hex,
    StrictModel,
)

ModelKind = Literal["embedding", "classifier", "detector", "vlm"]
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


class TrainedOn(StrictModel):
    snapshot_id: NonEmptyStr
    splits_lock_sha256: Sha256Hex


class VertexEndpointModel(StrictModel):
    kind: ModelKind
    provider: Literal["vertex_endpoint_raw_predict", "vertex_endpoint_predict"]
    # None means the endpoint is not configured in this environment.
    endpoint_id: NonEmptyStr | None
    region: NonEmptyStr
    version: NonEmptyStr
    requires_image: bool
    input: ImageInputContract | None = None
    output_schema: NonEmptyStr | None = None
    limits: RequestLimits | None = None
    license_ref: NonEmptyStr | None = None


class LocalArtifactModel(StrictModel):
    kind: ModelKind
    provider: Literal["local_sklearn"]
    artifact_uri: NonEmptyStr
    artifact_sha256: Sha256Hex
    version: NonEmptyStr
    input: FeatureInputContract | ImageInputContract
    trained_on: TrainedOn | None = None
    license_ref: NonEmptyStr | None = None


class GenerationParams(StrictModel):
    temperature: Annotated[float, Field(ge=0, le=2)]


class VertexGenAIModel(StrictModel):
    kind: Literal["vlm"]
    provider: Literal["vertex_genai"]
    # The pinned model ID is this entry's version.
    model: NonEmptyStr
    requires_image: bool
    params: GenerationParams
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

    def version_of(self, key: str) -> str:
        """Version string recorded for a model or heuristic. Unknown keys raise KeyError."""
        if key in self.models:
            return self.models[key].version
        return self.heuristics[key].version
