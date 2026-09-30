"""
Strict Pydantic schemas and JSON parser for model outputs.
SPEC-01 §3.5 and WP-2.4.
"""
from __future__ import annotations

import json
from typing import Annotated, Any, Literal
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
