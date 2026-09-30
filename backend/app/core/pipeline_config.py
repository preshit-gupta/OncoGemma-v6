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

from app.auth.roles import ROLES, Permission, Role
from app.core.config import settings
from app.core.config_types import (
    Fraction,
    Mpp,
    NonEmptyStr,
    NonNegativeFloat,
    NonNegativeInt,
    OverviewMpp,
    Percent,
    PositiveFloat,
    PositiveInt,
    RegistryKey,
    Sha256Hex,
    StrictModel,
)
from app.core.fallbacks import FallbackPolicy
from app.core.model_registry import ModelRegistry
from pipeline.errors import SpecimenTypeRequired

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
    "GCP_REGION",
)

# Each Nottingham component is scored 1-3, so the sum of the three lies in 3..9.
NOTTINGHAM_MIN_SUM = 3
NOTTINGHAM_MAX_SUM = 9

HsvHue = Annotated[int, Field(ge=0, le=180)]  # OpenCV hue scale
Channel8 = Annotated[int, Field(ge=0, le=255)]
NottinghamSum = Annotated[int, Field(ge=NOTTINGHAM_MIN_SUM, le=NOTTINGHAM_MAX_SUM)]
PromptFileName = Annotated[str, Field(pattern=r"^[a-z0-9_]+@v[0-9]+\.md$")]
PromptText = Annotated[str, Field(min_length=1)]
# Which colour a model is shown: the slide as scanned, or through its persisted stain transform (SPEC-04 §3.5).
ColorPolicy = Literal["raw", "normalized"]
# A colour reference is named <name>@v<version>, and its file is configs/stain_refs/<name>@v<version>.json.
StainRefId = Annotated[str, Field(pattern=r"^[a-z0-9_]+@v[0-9]+$")]
SpecimenType = Literal["resection", "core_biopsy"]
# Stain vectors are unit rows; stored floats may differ from 1 by their rounding.
UNIT_VECTOR_TOLERANCE = 1e-6


class ConfigLoadError(RuntimeError):
    """The configuration directory could not be read or failed validation."""


class ConfigNotLoadedError(RuntimeError):
    """The pipeline configuration was used before the entrypoint loaded it."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


# --- qc.yaml ----------------------------------------------------------------

class OverviewQC(StrictModel):
    """The pen-mark and fold checks look at the whole slide at this resolution."""

    mpp: OverviewMpp


class FocusQC(StrictModel):
    """Which tiles the focus check reads; its thresholds are per specimen type (specimen_profiles.yaml)."""

    mpp: Mpp
    tile_size_px: PositiveInt
    sample_max_tiles: PositiveInt
    # A tile is sampled only when at least this fraction of its area is tissue.
    min_tissue_fraction: Fraction


class ResolutionQC(StrictModel):
    """Native resolution of the slide, in µm/px (SPEC-04 §3.7)."""

    warn_native_mpp: PositiveFloat
    fail_native_mpp: PositiveFloat

    @model_validator(mode="after")
    def _ordered(self) -> "ResolutionQC":
        _require(self.warn_native_mpp < self.fail_native_mpp, "warn_native_mpp must be below fail_native_mpp")
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
    overview: OverviewQC
    focus: FocusQC
    resolution: ResolutionQC
    pen_marks: PenMarksQC
    folds: FoldsQC
    stain_sanity: StainSanityQC


# --- mitosis.yaml -----------------------------------------------------------

class MitosisDetectorConfig(StrictModel):
    """Stage A (SPEC-06 §5.1-5.2): tiles of the detector's input size at its resolution, with ownership."""

    # Registry key of the detector; each tile is one detector input.
    producer: RegistryKey
    tile_size_px: PositiveInt
    mpp: Mpp
    tile_size_um: PositiveFloat
    stride_px: PositiveInt
    overlap_px: NonNegativeInt
    # Sent to the detector: every candidate at or above it comes back and is stored raw.
    min_prob: Fraction
    # Applied by the pipeline to the raw candidates.
    det_threshold: Fraction
    nms_radius_um: PositiveFloat
    # Sweep region: each hotspot's bounding box grown by this margin (SPEC-06 §5.1).
    region_margin_um: NonNegativeFloat
    # A sweep tile is read only when at least this fraction of it is tissue (registered mask).
    min_tissue_fraction: Fraction

    @model_validator(mode="after")
    def _consistent(self) -> "MitosisDetectorConfig":
        _require(self.stride_px + self.overlap_px == self.tile_size_px, "stride_px + overlap_px must equal tile_size_px")
        _require(
            math.isclose(self.tile_size_um, self.tile_size_px * self.mpp, rel_tol=1e-6),
            "tile_size_um must equal tile_size_px * mpp",
        )
        _require(self.min_prob <= self.det_threshold, "min_prob must not exceed det_threshold")
        return self


class MitosisRefereeConfig(StrictModel):
    """The VLM that adjudicates every detected candidate (SPEC-06 arm A2: v5 prompt, strict schema).

    ``enabled: false`` is arm A1: the detector's thresholded candidates are the decision.
    """

    enabled: bool
    producer: RegistryKey
    prompt: PromptFileName
    # Image 1: a focus_px square at focus_mpp around the candidate, so its field of view is the same on every scanner.
    focus_px: PositiveInt
    focus_mpp: Mpp
    # Image 2: a context_um square around it, resampled to context_px.
    context_um: PositiveFloat
    context_px: PositiveInt
    color: ColorPolicy


class MitosisHpfConfig(StrictModel):
    radius_um: PositiveFloat
    count: PositiveInt
    # The review image of each field: review_field_um wide, review_px square (it fits the viewer's reticle).
    review_field_um: PositiveFloat
    review_px: PositiveInt
    density_grid_res_um: PositiveFloat
    min_separation_um: PositiveFloat
    relaxed_min_separation_um: PositiveFloat
    # Minimum tissue fraction inside a placed HPF.
    min_tissue_coverage: Fraction

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


class MitosisReviewConfig(StrictModel):
    """Confirmation gate: no candidate at or above this detector or referee confidence may stay unreviewed."""

    gate_min_conf: Fraction


class MitosisConfig(StrictModel):
    detector: MitosisDetectorConfig
    referee: MitosisRefereeConfig
    hpf: MitosisHpfConfig
    scoring: MitosisScoringConfig
    review: MitosisReviewConfig


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


class GradingEstimatorsConfig(StrictModel):
    producer: RegistryKey
    tubule_prompt: PromptFileName
    pleo_prompt: PromptFileName
    histotype_prompt: PromptFileName
    histotype_images: PositiveInt
    color: ColorPolicy


class GradingSamplingConfig(StrictModel):
    n_patches: PositiveInt
    patch_size_px: PositiveInt
    resolution_um: Mpp
    min_tumor_patches: PositiveInt
    max_disp: Fraction
    confidence_weights: ConfidenceWeights
    estimators: GradingEstimatorsConfig

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

class TumorRefereeConfig(StrictModel):
    """The VLM that checks candidate hotspots for invasive tumour (SPEC-05 §5.4 arm)."""

    producer: RegistryKey
    prompt: PromptFileName
    # Candidates sent to the referee; hotspot_extraction.max_hotspots of them are kept.
    candidates: PositiveInt
    # Each candidate is shown as a square field_um wide, resampled to size_px (the prompt states both).
    field_um: PositiveFloat
    size_px: PositiveInt
    color: ColorPolicy


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
    # Registry keys: the tile embedder and the classifier over its embeddings.
    embedding_model: RegistryKey
    tumor_model: RegistryKey
    tumor_referee: TumorRefereeConfig
    hotspot_extraction: HotspotExtractionConfig

    @model_validator(mode="after")
    def _enough_candidates(self) -> "TriageConfig":
        _require(
            self.tumor_referee.candidates >= self.hotspot_extraction.max_hotspots,
            "tumor_referee.candidates must be at least hotspot_extraction.max_hotspots",
        )
        return self


# --- specimen_profiles.yaml ---------------------------------------------------

class StainFitConfig(StrictModel):
    """How Stage 2 fits a slide's stain profile (SPEC-04 §3.2, §3.4)."""

    n_patches: PositiveInt
    # Tissue positions offered to the fitter; it stops at n_patches valid ones.
    n_candidates: PositiveInt
    patch_um: PositiveFloat
    fit_mpp: Mpp
    # A patch whose mean HSV saturation is below this is glass or fat, not stained tissue.
    min_sat_mean: Fraction
    # Fewer valid patches than this leave the fit 'sparse'.
    sparse_below: PositiveInt
    # A pixel is stained tissue when one of its optical densities reaches od_beta; the stain
    # transform passes every other pixel through unchanged.
    od_beta: PositiveFloat
    # Macenko: percentile of the angles in the stain plane that gives each stain vector.
    angle_percentile: Annotated[float, Field(gt=0, lt=50)]
    # Percentile of the concentrations that gives each stain's maximum.
    conc_percentile: Annotated[float, Field(gt=50, lt=100)]
    # The tissue pixels must span at least this angle (rad) in the stain plane, or two stains
    # cannot be told apart and the fit is degenerate.
    min_angle_spread_rad: PositiveFloat
    min_tissue_pixels: PositiveInt

    @model_validator(mode="after")
    def _ordered(self) -> "StainFitConfig":
        _require(self.sparse_below <= self.n_patches, "sparse_below must not exceed n_patches")
        _require(self.n_patches <= self.n_candidates, "n_patches must not exceed n_candidates")
        return self


class StainTargetConfig(StrictModel):
    """The colour standard a slide's stain is mapped to: a file in configs/stain_refs."""

    ref: StainRefId


class TissueMaskConfig(StrictModel):
    """How Stage 2 computes the registered tissue mask (SPEC-04 §3.6)."""

    mpp: OverviewMpp
    # The Otsu threshold on the grey image is clipped to this range (0-255).
    otsu_clip: list[Channel8]
    # A pixel with HSV saturation (0-255) above this is tissue however light it is.
    sat_min: Channel8
    open_radius_um: NonNegativeFloat
    min_component_um2: PositiveFloat
    fill_holes_max_um2: NonNegativeFloat

    @model_validator(mode="after")
    def _ordered(self) -> "TissueMaskConfig":
        _require(len(self.otsu_clip) == 2 and self.otsu_clip[0] <= self.otsu_clip[1], "otsu_clip must be [low, high] with low <= high")
        return self


class SpecimenQcConfig(StrictModel):
    """QC thresholds that depend on the specimen type (SPEC-04 §3.2, §3.7)."""

    # Absolute tissue area: what matters for grading is how much tissue there is, not how full the glass is.
    tissue_area_fail_mm2: PositiveFloat
    tissue_area_warn_mm2: PositiveFloat
    # A tile whose variance of the Laplacian is below this is blurry.
    focus_vol_threshold: PositiveFloat
    focus_fail_ratio: Fraction
    focus_warn_ratio: Fraction

    @model_validator(mode="after")
    def _ordered(self) -> "SpecimenQcConfig":
        _require(self.tissue_area_fail_mm2 <= self.tissue_area_warn_mm2, "tissue_area_fail_mm2 must not exceed tissue_area_warn_mm2")
        _require(self.focus_warn_ratio <= self.focus_fail_ratio, "focus_warn_ratio must not exceed focus_fail_ratio")
        return self


class NormPyramidConfig(StrictModel):
    """The stain-normalised DeepZoom pyramid the viewer shows (SPEC-04 §3.4): coarse levels only."""

    # The pyramid extends down to this resolution; finer levels are served from the raw pyramid.
    max_mpp: Mpp
    # Levels are generated from the coarsest until this many tiles are reached.
    max_tiles: PositiveInt


class SpecimenProfile(StrictModel):
    tissue_mask: TissueMaskConfig
    stain_fit: StainFitConfig
    stain_target: StainTargetConfig
    norm_pyramid: NormPyramidConfig
    qc: SpecimenQcConfig


class SpecimenProfilesConfig(StrictModel):
    schema_version: Literal[1]
    profiles: dict[SpecimenType, SpecimenProfile]

    @model_validator(mode="after")
    def _every_specimen_type(self) -> "SpecimenProfilesConfig":
        missing = {"resection", "core_biopsy"} - set(self.profiles)
        _require(not missing, f"specimen_profiles.yaml has no profile for {sorted(missing)}")
        return self

    def for_type(self, specimen_type: str) -> SpecimenProfile:
        """The profile of a case's specimen type. An 'unknown' specimen has none: SpecimenTypeRequired."""
        profile = self.profiles.get(specimen_type)
        if profile is None:
            raise SpecimenTypeRequired(
                f"the case's specimen type is {specimen_type!r}; set it to resection or core_biopsy before preprocessing"
            )
        return profile


# --- stain_refs/*.json --------------------------------------------------------

class StainReference(StrictModel):
    """A colour standard: unit stain vectors and maximum concentrations (SPEC-04 §3.3)."""

    reference_id: StainRefId
    w_tgt: list[list[float]]
    maxc_tgt: list[float]
    fitter_version: NonEmptyStr
    # Slides the reference is the median of; 0 for a reference fitted on a single patch.
    n_slides: NonNegativeInt
    slide_ids_sha256: Sha256Hex | None
    source: NonEmptyStr

    @model_validator(mode="after")
    def _well_formed(self) -> "StainReference":
        _require(
            len(self.w_tgt) == 2 and all(len(row) == 3 for row in self.w_tgt),
            "w_tgt must be two stain vectors of three values",
        )
        _require(
            all(abs(math.hypot(*row) - 1.0) <= UNIT_VECTOR_TOLERANCE for row in self.w_tgt),
            "w_tgt rows must be unit vectors",
        )
        _require(len(self.maxc_tgt) == 2 and all(c > 0 for c in self.maxc_tgt), "maxc_tgt must be two positive values")
        return self


# --- pricing.yaml -----------------------------------------------------------

class PatchPricing(StrictModel):
    unit_price_per_1k_patches: NonNegativeFloat


class PricingConfig(StrictModel):
    path_foundation: PatchPricing


# --- auth.yaml ----------------------------------------------------------------

class SessionConfig(StrictModel):
    absolute_lifetime_min: PositiveInt
    idle_timeout_min: PositiveInt
    cache_ttl_s: PositiveInt
    cache_max_entries: PositiveInt

    @model_validator(mode="after")
    def _ordered(self) -> "SessionConfig":
        _require(
            self.idle_timeout_min <= self.absolute_lifetime_min,
            "session.idle_timeout_min must not exceed session.absolute_lifetime_min",
        )
        _require(
            self.cache_ttl_s < self.idle_timeout_min * 60,
            "session.cache_ttl_s must be shorter than session.idle_timeout_min",
        )
        return self


class AuthConfig(StrictModel):
    session: SessionConfig
    roles: dict[Role, list[Permission]]

    @model_validator(mode="after")
    def _every_role(self) -> "AuthConfig":
        missing = set(ROLES) - set(self.roles)
        _require(not missing, f"auth.yaml roles has no entry for {sorted(missing)}")
        for role, permissions in self.roles.items():
            _require(len(set(permissions)) == len(permissions), f"auth.yaml roles.{role} lists a permission twice")
        return self

    def permissions_of(self, role: str) -> frozenset[str]:
        return frozenset(self.roles[role])


# --- safety.yaml -------------------------------------------------------------

class RateLimitsConfig(StrictModel):
    mutating_per_user_per_min: PositiveInt
    auth_session_per_ip_per_min: PositiveInt
    window_seconds: PositiveInt
    trusted_proxy_hops: PositiveInt


class IdempotencyConfig(StrictModel):
    ttl_hours: PositiveInt


class SoftDeleteConfig(StrictModel):
    retention_days: PositiveInt


class SignedUploadConfig(StrictModel):
    expiration_minutes: PositiveInt
    max_bytes: PositiveInt
    allowed_wsi_mimes: list[NonEmptyStr]


class GeometryConfig(StrictModel):
    max_vertices: PositiveInt
    min_vertices: PositiveInt
    min_area_mm2: PositiveFloat
    max_area_mm2: PositiveFloat
    max_overlap_area_um2: NonNegativeFloat

    @model_validator(mode="after")
    def _ranges(self) -> "GeometryConfig":
        if not 3 <= self.min_vertices <= self.max_vertices:
            raise ValueError("geometry needs 3 <= min_vertices <= max_vertices")
        if self.min_area_mm2 >= self.max_area_mm2:
            raise ValueError("geometry.min_area_mm2 must be below max_area_mm2")
        return self


class SafetyConfig(StrictModel):
    schema_version: Literal[1]
    rate_limits: RateLimitsConfig
    idempotency: IdempotencyConfig
    soft_delete: SoftDeleteConfig
    signed_upload: SignedUploadConfig
    geometry: GeometryConfig


# --- the whole tree -----------------------------------------------------------

class PipelineConfig(StrictModel):
    """One field per ``configs/<name>.yaml`` file, plus the prompt templates."""

    auth: AuthConfig
    fallbacks: FallbackPolicy
    mitosis: MitosisConfig
    models: ModelRegistry
    pricing: PricingConfig
    qc: QcConfig
    safety: SafetyConfig
    scoring: ScoringConfig
    specimen_profiles: SpecimenProfilesConfig
    triage: TriageConfig
    prompts: dict[PromptFileName, PromptText]
    stain_refs: dict[StainRefId, StainReference]

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
        self._triage_models_exist()
        self._mitosis_models_exist()
        self._grading_models_exist()
        self._stain_targets_exist()
        return self

    def _stain_targets_exist(self) -> None:
        for reference_id, reference in self.stain_refs.items():
            _require(
                reference.reference_id == reference_id,
                f"configs/stain_refs/{reference_id}.json names itself {reference.reference_id!r}",
            )
        for specimen_type, profile in self.specimen_profiles.profiles.items():
            _require(
                profile.stain_target.ref in self.stain_refs,
                f"specimen_profiles.yaml {specimen_type}.stain_target.ref {profile.stain_target.ref!r} "
                "is not in configs/stain_refs",
            )

    def _grading_models_exist(self) -> None:
        estimators = self.scoring.grading.estimators
        vlm = self.models.models.get(estimators.producer)
        _require(
            vlm is not None and vlm.kind == "vlm",
            f"scoring.yaml grading.estimators.producer {estimators.producer!r} must be a VLM in models.yaml",
        )
        for field in ("tubule_prompt", "pleo_prompt", "histotype_prompt"):
            prompt = getattr(estimators, field)
            _require(
                prompt in self.prompts,
                f"scoring.yaml grading.estimators.{field} {prompt!r} is not in configs/prompts",
            )
        _require(
            estimators.histotype_images <= self.scoring.grading.n_patches,
            "scoring.yaml grading.estimators.histotype_images must not exceed n_patches",
        )

    def _mitosis_models_exist(self) -> None:
        models, detector, referee = self.models.models, self.mitosis.detector, self.mitosis.referee
        entry = models.get(detector.producer)
        contract = getattr(entry, "input", None)
        _require(
            entry is not None and entry.kind == "detector" and contract is not None and hasattr(contract, "size_px"),
            f"mitosis.yaml detector.producer {detector.producer!r} must be a detector with an image input contract",
        )
        patch_w, patch_h = contract.size_px
        _require(
            patch_w == patch_h == detector.tile_size_px,
            f"mitosis.yaml detector.tile_size_px must equal the detector's {contract.size_px} input",
        )
        _require(
            math.isclose(detector.mpp, contract.mpp),
            f"mitosis.yaml detector.mpp {detector.mpp} must equal the detector's input mpp {contract.mpp}",
        )
        vlm = models.get(referee.producer)
        _require(
            vlm is not None and vlm.kind == "vlm",
            f"mitosis.yaml referee.producer {referee.producer!r} must be a VLM in models.yaml",
        )
        _require(
            referee.prompt in self.prompts,
            f"mitosis.yaml referee.prompt {referee.prompt!r} is not in configs/prompts",
        )

    def _triage_models_exist(self) -> None:
        models, triage = self.models.models, self.triage
        embedder = models.get(triage.embedding_model)
        _require(
            embedder is not None and embedder.kind == "embedding",
            f"triage.yaml embedding_model {triage.embedding_model!r} must be an embedding model in models.yaml",
        )
        classifier = models.get(triage.tumor_model)
        _require(
            classifier is not None
            and classifier.kind == "classifier"
            and getattr(getattr(classifier, "input", None), "features", None) == triage.embedding_model,
            f"triage.yaml tumor_model {triage.tumor_model!r} must be a classifier over {triage.embedding_model}",
        )
        referee = models.get(triage.tumor_referee.producer)
        _require(
            referee is not None and referee.kind == "vlm",
            f"triage.yaml tumor_referee.producer {triage.tumor_referee.producer!r} must be a VLM in models.yaml",
        )
        _require(
            triage.tumor_referee.prompt in self.prompts,
            f"triage.yaml tumor_referee.prompt {triage.tumor_referee.prompt!r} is not in configs/prompts",
        )

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


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    keys = [key for key, _ in pairs]
    duplicated = sorted({key for key in keys if keys.count(key) > 1})
    if duplicated:
        raise ValueError(f"duplicate key {duplicated[0]!r}")
    return dict(pairs)


def _read_stain_refs(refs_dir: Path) -> dict[str, Any]:
    if not refs_dir.is_dir():
        raise ConfigLoadError(f"{refs_dir}: stain reference directory not found")
    refs = {}
    for path in sorted(refs_dir.iterdir()):
        if not path.is_file() or path.suffix != ".json":
            raise ConfigLoadError(f"{path}: only <name>@v<version>.json stain references are allowed in {refs_dir}")
        try:
            refs[path.stem] = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
        except (OSError, ValueError) as exc:
            raise ConfigLoadError(f"{path}: {exc}") from exc
    return refs


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

    file_sections = set(PipelineConfig.model_fields) - {"prompts", "stain_refs"}
    sections: dict[str, Any] = {}
    for path in sorted(configs_dir.glob("*.yaml")):
        if path.stem not in file_sections:
            raise ConfigLoadError(f"{path}: no schema for this file; add a PipelineConfig field for it")
        data = _read_yaml(path)
        if path.stem == "models":
            data = _interpolate(data, variables, "models")
        sections[path.stem] = data
    sections["prompts"] = _read_prompts(configs_dir / "prompts")
    sections["stain_refs"] = _read_stain_refs(configs_dir / "stain_refs")

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
