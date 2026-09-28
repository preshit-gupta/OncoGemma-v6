"""Typed, hashed pipeline configuration (SPEC-01 §3.8).

Every ``configs/*.yaml`` file and every ``configs/prompts/*`` template is loaded
into one immutable ``PipelineConfig``. The entrypoints load it at startup, and
a load failure aborts startup.

``config_hash = sha256(canonical_json(PipelineConfig.model_dump(mode="json")))``
is written to ``stage_executions.config_hash`` for every execution.
"""
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping

import yaml
from pydantic import Field, ValidationError, model_validator

from app.core.config import settings
from app.core.config_types import (
    Fraction,
    Mpp,
    NonEmptyStr,
    NonNegativeFloat,
    NonNegativeInt,
    Percent,
    PositiveFloat,
    PositiveInt,
    StrictModel,
)
from app.core.model_registry import ModelRegistry

# Settings fields that configs/models.yaml may reference as ${NAME}. Anything
# else is refused, so a secret can never be interpolated into the hashed config.
REGISTRY_VARIABLES = (
    "VERTEX_PATH_FOUNDATION_ENDPOINT_ID",
    "VERTEX_PATH_FOUNDATION_LOCATION",
    "VERTEX_MITOSIS_ENDPOINT_ID",
    "VERTEX_MITOSIS_LOCATION",
    "VERTEX_MEDGEMMA_ENDPOINT_ID",
    "VERTEX_MEDGEMMA_LOCATION",
    "GEMINI_REFEREE_MODEL",
)

# Each Nottingham component is scored 1-3, so the sum of the three lies in 3..9.
NOTTINGHAM_MIN_SUM = 3
NOTTINGHAM_MAX_SUM = 9

HsvHue = Annotated[int, Field(ge=0, le=180)]  # OpenCV hue scale
Channel8 = Annotated[int, Field(ge=0, le=255)]
NottinghamSum = Annotated[int, Field(ge=NOTTINGHAM_MIN_SUM, le=NOTTINGHAM_MAX_SUM)]
PromptFileName = Annotated[str, Field(pattern=r"^[a-z0-9_]+@v[0-9]+\.md$")]
PromptText = Annotated[str, Field(min_length=1)]


class ConfigLoadError(RuntimeError):
    """The configuration directory could not be read or failed validation."""


class ConfigNotLoadedError(RuntimeError):
    """The pipeline configuration was used before the entrypoint loaded it."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


# --- qc.yaml ----------------------------------------------------------------

class TissueCoverageQC(StrictModel):
    fail_threshold: Fraction
    warn_threshold: Fraction

    @model_validator(mode="after")
    def _ordered(self) -> "TissueCoverageQC":
        _require(self.fail_threshold <= self.warn_threshold, "fail_threshold must not exceed warn_threshold")
        return self


class FocusQC(StrictModel):
    vol_threshold: PositiveFloat
    tile_size_px: PositiveInt
    sample_max_tiles: PositiveInt
    fail_blurry_ratio: Fraction
    warn_blurry_ratio: Fraction

    @model_validator(mode="after")
    def _ordered(self) -> "FocusQC":
        _require(self.warn_blurry_ratio <= self.fail_blurry_ratio, "warn_blurry_ratio must not exceed fail_blurry_ratio")
        return self


class HsvRange(StrictModel):
    h_min: HsvHue
    h_max: HsvHue
    s_min: Channel8
    v_min: Channel8
    # Absent upper bounds mean "no upper bound".
    s_max: Channel8 | None = None
    v_max: Channel8 | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "HsvRange":
        _require(self.h_min <= self.h_max, "h_min must not exceed h_max")
        _require(self.s_max is None or self.s_min <= self.s_max, "s_min must not exceed s_max")
        _require(self.v_max is None or self.v_min <= self.v_max, "v_min must not exceed v_max")
        return self


class PenHsvRanges(StrictModel):
    green: HsvRange
    blue: HsvRange
    black: HsvRange


class PenMarksQC(StrictModel):
    min_component_area_mm2: PositiveFloat
    hsv_ranges: PenHsvRanges


class FoldsQC(StrictModel):
    min_skeleton_length_mm: PositiveFloat
    saturation_min: Channel8
    brightness_max: Channel8


class StainSanityQC(StrictModel):
    min_concentration: PositiveFloat
    he_ratio_min: PositiveFloat
    he_ratio_max: PositiveFloat

    @model_validator(mode="after")
    def _ordered(self) -> "StainSanityQC":
        _require(self.he_ratio_min < self.he_ratio_max, "he_ratio_min must be below he_ratio_max")
        return self


class QcConfig(StrictModel):
    tissue_coverage: TissueCoverageQC
    focus: FocusQC
    pen_marks: PenMarksQC
    folds: FoldsQC
    stain_sanity: StainSanityQC


# --- mitosis.yaml -----------------------------------------------------------

class MitosisDetectorConfig(StrictModel):
    model_name: NonEmptyStr
    weights_path: NonEmptyStr
    tile_size_px: PositiveInt
    mpp: Mpp
    tile_size_um: PositiveFloat
    stride_px: PositiveInt
    overlap_px: NonNegativeInt
    det_threshold: Fraction
    review_threshold: Fraction
    nms_radius_um: PositiveFloat
    batch_size: PositiveInt
    fp16: bool

    @model_validator(mode="after")
    def _consistent(self) -> "MitosisDetectorConfig":
        _require(self.stride_px + self.overlap_px == self.tile_size_px, "stride_px + overlap_px must equal tile_size_px")
        _require(
            math.isclose(self.tile_size_um, self.tile_size_px * self.mpp, rel_tol=1e-6),
            "tile_size_um must equal tile_size_px * mpp",
        )
        _require(self.det_threshold <= self.review_threshold, "det_threshold must not exceed review_threshold")
        return self


class MitosisVerifierConfig(StrictModel):
    enabled: bool
    model_name: NonEmptyStr
    weights_path: NonEmptyStr
    crop_size_px: PositiveInt
    ver_threshold: Fraction


class MitosisHpfConfig(StrictModel):
    radius_um: PositiveFloat
    count: PositiveInt
    density_grid_res_um: PositiveFloat
    min_separation_um: PositiveFloat
    relaxed_min_separation_um: PositiveFloat

    @model_validator(mode="after")
    def _non_overlapping(self) -> "MitosisHpfConfig":
        _require(self.min_separation_um >= 2 * self.radius_um, "min_separation_um must be at least 2 * radius_um")
        _require(
            self.relaxed_min_separation_um <= self.min_separation_um,
            "relaxed_min_separation_um must not exceed min_separation_um",
        )
        return self


class MitoticThresholds(StrictModel):
    """Mitoses per mm² at which the mitotic score becomes 2 and 3."""

    score2_min: PositiveFloat
    score3_min: PositiveFloat

    @model_validator(mode="after")
    def _ordered(self) -> "MitoticThresholds":
        _require(self.score2_min < self.score3_min, "score2_min must be below score3_min")
        return self


class MitosisScoringConfig(StrictModel):
    basis: Literal["per_mm2"]
    thresholds: MitoticThresholds
    display_classic_equivalent: bool
    classic_area_mm2: PositiveFloat


class MitosisMockConfig(StrictModel):
    use_mock_detector: bool


class MitosisConfig(StrictModel):
    detector: MitosisDetectorConfig
    verifier: MitosisVerifierConfig
    hpf: MitosisHpfConfig
    scoring: MitosisScoringConfig
    mock: MitosisMockConfig


# --- scoring.yaml -----------------------------------------------------------

class MitoticScoreConfig(StrictModel):
    basis: Literal["per_mm2"]
    thresholds: MitoticThresholds
    display_classic_equivalent: bool


class TubuleThresholds(StrictModel):
    score1_min_percent: Percent
    score2_min_percent: Percent

    @model_validator(mode="after")
    def _ordered(self) -> "TubuleThresholds":
        _require(self.score2_min_percent < self.score1_min_percent, "score2_min_percent must be below score1_min_percent")
        return self


class TubuleFormationConfig(StrictModel):
    thresholds: TubuleThresholds


class NottinghamGradingConfig(StrictModel):
    grade1_max_sum: NottinghamSum
    grade2_max_sum: NottinghamSum

    @model_validator(mode="after")
    def _ordered(self) -> "NottinghamGradingConfig":
        _require(self.grade1_max_sum < self.grade2_max_sum, "grade1_max_sum must be below grade2_max_sum")
        return self


class ConfidenceWeights(StrictModel):
    low: PositiveFloat
    medium: PositiveFloat
    high: PositiveFloat


class GradingSamplingConfig(StrictModel):
    n_patches: PositiveInt
    patch_size_px: PositiveInt
    resolution_um: Mpp
    min_tumor_patches: PositiveInt
    max_disp: Fraction
    confidence_weights: ConfidenceWeights

    @model_validator(mode="after")
    def _ordered(self) -> "GradingSamplingConfig":
        _require(self.min_tumor_patches <= self.n_patches, "min_tumor_patches must not exceed n_patches")
        return self


class ScoringHpfConfig(StrictModel):
    radius_um: PositiveFloat
    count: PositiveInt


class ScoringConfig(StrictModel):
    mitotic_score: MitoticScoreConfig
    tubule_formation: TubuleFormationConfig
    nottingham_grading: NottinghamGradingConfig
    grading: GradingSamplingConfig
    hpf: ScoringHpfConfig


# --- triage.yaml ------------------------------------------------------------

class TriageVertexConfig(StrictModel):
    batch_size: PositiveInt
    concurrency: PositiveInt
    max_retries: NonNegativeInt


class TriageProbeConfig(StrictModel):
    model_name: NonEmptyStr
    model_path: NonEmptyStr
    version: NonEmptyStr


class HotspotExtractionConfig(StrictModel):
    sigma: PositiveFloat
    prob_threshold: Fraction
    min_area_mm2: PositiveFloat
    max_hotspots: PositiveInt
    margin_um: NonNegativeFloat
    simplify_tolerance_um: NonNegativeFloat


class TriageConfig(StrictModel):
    mpp_target: Mpp
    patch_size_px: PositiveInt
    tissue_threshold_pct: Fraction
    max_sample_patches: PositiveInt
    vertex_ai: TriageVertexConfig
    probe: TriageProbeConfig
    hotspot_extraction: HotspotExtractionConfig


# --- pricing.yaml -----------------------------------------------------------

class PatchPricing(StrictModel):
    unit_price_per_1k_patches: NonNegativeFloat


class PricingConfig(StrictModel):
    path_foundation: PatchPricing


# --- the whole tree -----------------------------------------------------------

class PipelineConfig(StrictModel):
    """One field per ``configs/<name>.yaml`` file, plus the prompt templates."""

    mitosis: MitosisConfig
    models: ModelRegistry
    pricing: PricingConfig
    qc: QcConfig
    scoring: ScoringConfig
    triage: TriageConfig
    prompts: dict[PromptFileName, PromptText]

    @model_validator(mode="after")
    def _files_agree(self) -> "PipelineConfig":
        """Values duplicated across files must match, because different code paths read each copy."""
        mitosis_scoring, mitotic_score = self.mitosis.scoring, self.scoring.mitotic_score
        _require(
            mitosis_scoring.basis == mitotic_score.basis and mitosis_scoring.thresholds == mitotic_score.thresholds,
            "mitosis.yaml scoring and scoring.yaml mitotic_score must have the same basis and thresholds",
        )
        _require(
            self.mitosis.hpf.radius_um == self.scoring.hpf.radius_um and self.mitosis.hpf.count == self.scoring.hpf.count,
            "mitosis.yaml hpf and scoring.yaml hpf must have the same radius_um and count",
        )
        return self

    def config_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json")).encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    """Key-sorted, whitespace-free JSON. NaN and infinity are refused."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


# --- loading ------------------------------------------------------------------

class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys instead of keeping the last one."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)

_VARIABLE = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigLoadError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigLoadError(f"{path}: top level must be a mapping, got {type(data).__name__}")
    return data


def _interpolate(node: Any, variables: Mapping[str, Any], where: str) -> Any:
    """Replace whole-string ``${NAME}`` values. An unset or empty variable becomes None."""
    if isinstance(node, dict):
        return {key: _interpolate(value, variables, f"{where}.{key}") for key, value in node.items()}
    if isinstance(node, list):
        return [_interpolate(value, variables, f"{where}[{i}]") for i, value in enumerate(node)]
    if isinstance(node, str) and "${" in node:
        match = _VARIABLE.match(node)
        if match is None:
            raise ConfigLoadError(f"{where}: only a whole value can be a variable, got {node!r}")
        name = match.group(1)
        if name not in variables:
            raise ConfigLoadError(f"{where}: ${{{name}}} is not an allowed registry variable")
        value = variables[name]
        return None if value in (None, "") else value
    return node


def _read_prompts(prompts_dir: Path) -> dict[str, str]:
    if not prompts_dir.is_dir():
        raise ConfigLoadError(f"{prompts_dir}: prompts directory not found")
    prompts = {}
    for path in sorted(prompts_dir.iterdir()):
        if not path.is_file():
            raise ConfigLoadError(f"{path}: only prompt template files are allowed in {prompts_dir}")
        try:
            # Text mode normalises line endings, so a CRLF checkout hashes like an LF one.
            prompts[path.name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigLoadError(f"{path}: {exc}") from exc
    return prompts


def load_pipeline_config(configs_dir: Path, variables: Mapping[str, Any]) -> PipelineConfig:
    """Load and validate ``configs_dir``. ``variables`` resolves ``${NAME}`` in models.yaml."""
    configs_dir = Path(configs_dir)
    if not configs_dir.is_dir():
        raise ConfigLoadError(f"{configs_dir}: configs directory not found")

    file_sections = set(PipelineConfig.model_fields) - {"prompts"}
    sections: dict[str, Any] = {}
    for path in sorted(configs_dir.glob("*.yaml")):
        if path.stem not in file_sections:
            raise ConfigLoadError(f"{path}: no schema for this file; add a PipelineConfig field for it")
        data = _read_yaml(path)
        if path.stem == "models":
            data = _interpolate(data, variables, "models")
        sections[path.stem] = data
    sections["prompts"] = _read_prompts(configs_dir / "prompts")

    try:
        return PipelineConfig.model_validate(sections)
    except ValidationError as exc:
        raise ConfigLoadError(f"invalid configuration in {configs_dir}:\n{exc}") from exc


def registry_variables() -> dict[str, Any]:
    return {name: getattr(settings, name) for name in REGISTRY_VARIABLES}


# --- the process's active configuration -----------------------------------------

class _Active:
    config: PipelineConfig | None = None
    config_hash: str | None = None


def init_pipeline_config(
    configs_dir: Path | None = None, variables: Mapping[str, Any] | None = None
) -> PipelineConfig:
    """Load the configuration for this process. Entrypoints call this at startup."""
    config = load_pipeline_config(
        Path(settings.CONFIGS_DIR) if configs_dir is None else configs_dir,
        registry_variables() if variables is None else variables,
    )
    _Active.config, _Active.config_hash = config, config.config_hash()
    return config


def get_pipeline_config() -> PipelineConfig:
    """The active configuration. Usable as a FastAPI dependency."""
    if _Active.config is None:
        raise ConfigNotLoadedError("init_pipeline_config() has not run in this process")
    return _Active.config


def get_config_hash() -> str:
    if _Active.config_hash is None:
        raise ConfigNotLoadedError("init_pipeline_config() has not run in this process")
    return _Active.config_hash
