"""
Strict Pydantic schemas and JSON parser for model outputs.
SPEC-01 §3.5 and WP-2.4.
"""
from __future__ import annotations

import json
from typing import Annotated, Any, ClassVar, Literal, Sequence
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class SchemaInvalidError(ValueError):
    """Raised when raw model output fails JSON decoding or schema validation."""

    def __init__(self, model_name: str, detail: str):
        super().__init__(f"[{model_name}] {detail}")
        self.model_name = model_name
        self.detail = detail


def _normalize_literal(v: Any, allowed: tuple[str, ...]) -> Any:
    """Case-insensitive exact match with whitespace trimming for enum/Literal string fields."""
    if isinstance(v, str):
        cleaned = v.strip().lower()
        for member in allowed:
            if cleaned == member.lower():
                return member
    return v


class StrictModel(BaseModel):
    """Base model enforcing strict type checking, no extra fields, and immutability."""
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class TumorVerdict(StrictModel):
    tumor_present: bool
    lesion_type: Literal[
        "invasive_carcinoma", "in_situ", "benign_stroma", "inflammation", "adipose"
    ]
    rationale: str = ""

    @field_validator("lesion_type", mode="before")
    @classmethod
    def normalize_lesion_type(cls, v: Any) -> Any:
        return _normalize_literal(
            v, ("invasive_carcinoma", "in_situ", "benign_stroma", "inflammation", "adipose")
        )


class MitosisCriteria(StrictModel):
    membrane_absent: bool
    condensed_chromosome_projections: bool
    phase: Literal[
        "prometaphase", "metaphase", "anaphase", "telophase", "atypical", "none"
    ]
    neoplastic_cell: bool

    @field_validator("phase", mode="before")
    @classmethod
    def normalize_phase(cls, v: Any) -> Any:
        return _normalize_literal(
            v, ("prometaphase", "metaphase", "anaphase", "telophase", "atypical", "none")
        )


class MitosisVerdict(StrictModel):
    verdict: Literal["MITOTIC_FIGURE", "NOT_MITOTIC_FIGURE", "EQUIVOCAL"]
    criteria: MitosisCriteria
    mimic: Literal[
        "none", "apoptotic_body", "pyknotic_nucleus", "hyperchromatic_interphase",
        "lymphocyte", "prophase", "crush", "other"
    ]
    rationale: str = ""

    @field_validator("verdict", mode="before")
    @classmethod
    def normalize_verdict(cls, v: Any) -> Any:
        return _normalize_literal(
            v, ("MITOTIC_FIGURE", "NOT_MITOTIC_FIGURE", "EQUIVOCAL")
        )

    @field_validator("mimic", mode="before")
    @classmethod
    def normalize_mimic(cls, v: Any) -> Any:
        return _normalize_literal(
            v, (
                "none", "apoptotic_body", "pyknotic_nucleus", "hyperchromatic_interphase",
                "lymphocyte", "prophase", "crush", "other"
            )
        )


_DESCRIPTION_CHROMATIN = (
    "condensed_clumps", "band_or_plate", "two_separated_masses", "fine_granular", "smooth_dense", "beaded_fragments",
    "not_assessable",
)
_DESCRIPTION_MEMBRANE = ("not_visible", "partly_visible", "intact", "not_assessable")
_DESCRIPTION_OUTLINE = ("hairy_projections", "smooth", "not_assessable")
_DESCRIPTION_CYTOPLASM = ("clear_halo", "eosinophilic", "none_visible", "not_assessable")
_DESCRIPTION_SIZE = ("larger", "similar", "smaller", "not_assessable")
_DESCRIPTION_SETTING = ("tumour_cells", "stroma", "inflammatory", "necrosis", "lumen", "not_assessable")
DESCRIPTION_MAX_WORDS = 60


class MitosisDescription(StrictModel):
    """What a candidate figure looks like, for the pathologist to interpret (SPEC-06 §5.6, D22).

    Descriptive only: there is deliberately no verdict, label, phase, mimic, confidence, count or decision
    field. ``forbidden_phrases`` (set per run from ``mitosis.yaml describe.forbidden_phrases`` by
    ``describe_schema``) are rejected in the summary as a ``SchemaInvalidError``.
    """

    chromatin: Literal[_DESCRIPTION_CHROMATIN]
    nuclear_membrane: Literal[_DESCRIPTION_MEMBRANE]
    outline: Literal[_DESCRIPTION_OUTLINE]
    cytoplasm: Literal[_DESCRIPTION_CYTOPLASM]
    relative_size: Literal[_DESCRIPTION_SIZE]
    setting: Literal[_DESCRIPTION_SETTING]
    summary: str

    forbidden_phrases: ClassVar[tuple[str, ...]] = ()

    @field_validator("chromatin", "nuclear_membrane", "outline", "cytoplasm", "relative_size", "setting", mode="before")
    @classmethod
    def normalize_enum(cls, v: Any, info: Any) -> Any:
        allowed = {
            "chromatin": _DESCRIPTION_CHROMATIN, "nuclear_membrane": _DESCRIPTION_MEMBRANE, "outline": _DESCRIPTION_OUTLINE,
            "cytoplasm": _DESCRIPTION_CYTOPLASM, "relative_size": _DESCRIPTION_SIZE, "setting": _DESCRIPTION_SETTING,
        }[info.field_name]
        return _normalize_literal(v, allowed)

    @field_validator("summary")
    @classmethod
    def summary_is_descriptive(cls, v: str) -> str:
        if len(v.split()) > DESCRIPTION_MAX_WORDS:
            raise ValueError(f"summary has more than {DESCRIPTION_MAX_WORDS} words")
        lowered = " ".join(v.lower().split())
        for phrase in cls.forbidden_phrases:
            if phrase.lower() in lowered:
                raise ValueError(f"summary contains the forbidden phrase {phrase!r}: a description never judges the cell")
        return v


def describe_schema(forbidden_phrases: Sequence[str]) -> type[MitosisDescription]:
    """``MitosisDescription`` that also rejects ``forbidden_phrases`` in its summary (same name and JSON schema)."""
    return type("MitosisDescription", (MitosisDescription,), {"forbidden_phrases": tuple(forbidden_phrases), "__module__": __name__, "__doc__": MitosisDescription.__doc__})


class TubuleEstimate(StrictModel):
    tumor_present: bool
    tubule_percent: int = Field(ge=0, le=100)
    rationale: str = ""

    @field_validator("tubule_percent", mode="before")
    @classmethod
    def validate_tubule_percent(cls, v: Any) -> Any:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"tubule_percent must be an integer, got {type(v).__name__}: {v!r}")
        return v


class PleoEstimate(StrictModel):
    pleomorphism_score: Literal[1, 2, 3]
    rationale: str = ""

    @field_validator("pleomorphism_score", mode="before")
    @classmethod
    def validate_score(cls, v: Any) -> Any:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"pleomorphism_score must be an integer (1, 2, or 3), got {type(v).__name__}: {v!r}")
        return v


class PleoScore(StrictModel):
    """The Stage 5 pleomorphism field answer: the score only.

    Asked for a free-text rationale on a 128 µm field at 0.25 µm/px, gemini-2.5-flash degenerates
    into repeated text until the server deadline. Live check 2026-10-04: 44 of 45 pipeline calls and
    3 of 3 direct calls hit the 504; with the score-only schema 3 of 3 answered in about 10 s.
    ``maxLength`` on the rationale is not honoured. The contract shows no rationale for a field.
    """

    pleomorphism_score: Literal[1, 2, 3]

    @field_validator("pleomorphism_score", mode="before")
    @classmethod
    def validate_score(cls, v: Any) -> Any:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"pleomorphism_score must be an integer (1, 2, or 3), got {type(v).__name__}: {v!r}")
        return v


class HistotypeVerdict(StrictModel):
    type: Literal[
        "IDC-NST", "ILC", "mixed_ductal_lobular", "mucinous", "tubular", "papillary",
        "micropapillary", "metaplastic", "other"
    ]
    rationale: str = ""

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, v: Any) -> Any:
        return _normalize_literal(
            v, (
                "IDC-NST", "ILC", "mixed_ductal_lobular", "mucinous", "tubular", "papillary",
                "micropapillary", "metaplastic", "other"
            )
        )


ReportField = Literal["grade", "total", "tubule", "pleo", "mitoses"]
NottinghamComponent = Annotated[int, Field(ge=1, le=3)]


class EvidenceQuote(StrictModel):
    field: ReportField
    quote: str = Field(min_length=1)


class ReportGradeExtraction(StrictModel):
    """Nottingham grade statements in one pathology report (SPEC-02 §4 step 3).

    Every field is required and null when the report does not state it. Each non-null field
    needs a verbatim ``evidence`` quote; the extractor checks the quotes in code.
    """

    grade: NottinghamComponent | None
    total: Annotated[int, Field(ge=3, le=9)] | None
    tubule: NottinghamComponent | None
    pleo: NottinghamComponent | None
    mitoses: NottinghamComponent | None
    evidence: list[EvidenceQuote]


def _strip_fence(raw: str) -> str:
    """
    Accepts only surrounding whitespace and a single optional markdown code fence.
    Raises ValueError on trailing prose, multiple fences, or malformed fences.
    """
    text = raw.strip()
    if not text:
        raise ValueError("Empty input string")

    if text.startswith("```"):
        if not text.endswith("```"):
            raise ValueError("Unclosed markdown code fence")
        lines = text.split("\n")
        first_line = lines[0].strip()
        last_line = lines[-1].strip()
        if first_line not in ("```", "```json", "```JSON"):
            raise ValueError(f"Invalid markdown code fence header: {first_line}")
        if last_line != "```":
            raise ValueError("Markdown code fence must end with ```")
        middle = "\n".join(lines[1:-1])
        if "```" in middle:
            raise ValueError("Multiple markdown code fences are forbidden")
        return middle.strip()

    if "```" in text:
        raise ValueError("Markdown code fence cannot have prose before or after")

    return text


def parse_json_strict(model_cls: type[StrictModel], raw: str) -> StrictModel:
    """
    Strictly parse JSON string into the specified StrictModel class.
    Accepts only surrounding whitespace and a single optional markdown code fence.
    Wraps all JSONDecodeError and ValidationError exceptions into SchemaInvalidError.
    """
    model_name = getattr(model_cls, "__name__", str(model_cls))
    try:
        cleaned = _strip_fence(raw)
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise SchemaInvalidError(model_name, f"Expected JSON object, got {type(data).__name__}")
        return model_cls.model_validate(data)
    except SchemaInvalidError:
        raise
    except (json.JSONDecodeError, ValueError, ValidationError) as exc:
        raise SchemaInvalidError(model_name, str(exc)) from exc
