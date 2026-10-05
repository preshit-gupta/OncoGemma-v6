"""
Acceptance tests for WP-2.4 (SPEC-01 §3.5): strict model-output schemas.

These tests are skipped until `app.inference.schemas` exists. Do NOT change assertions;
if you believe one is wrong, explain in the PR.
"""
import json

import pytest
from pydantic import ValidationError

schemas = pytest.importorskip("app.inference.schemas")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _mitosis(**overrides):
    base = {
        "verdict": "MITOTIC_FIGURE",
        "criteria": {
            "membrane_absent": True,
            "condensed_chromosome_projections": True,
            "phase": "metaphase",
            "neoplastic_cell": True,
        },
        "mimic": "none",
        "rationale": "Metaphase plate with hairy projections.",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# StrictModel base behaviour
# ---------------------------------------------------------------------------

def test_models_are_frozen():
    v = schemas.PleoEstimate.model_validate({"pleomorphism_score": 2, "rationale": ""})
    with pytest.raises(ValidationError):
        v.pleomorphism_score = 3


@pytest.mark.parametrize("model, payload", [
    ("TumorVerdict", {"tumor_present": True, "lesion_type": "invasive_carcinoma", "rationale": "", "extra": 1}),
    ("PleoEstimate", {"pleomorphism_score": 2, "rationale": "", "confidence": "high"}),
])
def test_extra_fields_rejected(model, payload):
    with pytest.raises(ValidationError):
        getattr(schemas, model).model_validate(payload)


# ---------------------------------------------------------------------------
# TumorVerdict
# ---------------------------------------------------------------------------

def test_tumor_verdict_valid_and_rationale_defaults_empty():
    v = schemas.TumorVerdict.model_validate({"tumor_present": False, "lesion_type": "benign_stroma"})
    assert v.tumor_present is False
    assert v.lesion_type == "benign_stroma"
    assert v.rationale == ""


@pytest.mark.parametrize("payload", [
    {},                                                               # v5 defaulted this to "tumor, high confidence"
    {"lesion_type": "invasive_carcinoma"},                            # missing tumor_present
    {"tumor_present": True},                                          # missing lesion_type
    {"tumor_present": "true", "lesion_type": "invasive_carcinoma"},   # strict bool
    {"tumor_present": 1, "lesion_type": "invasive_carcinoma"},        # strict bool
    {"tumor_present": True, "lesion_type": "unassessed"},             # removed enum member
    {"tumor_present": True, "lesion_type": "carcinoma"},              # not an enum member
])
def test_tumor_verdict_invalid(payload):
    with pytest.raises(ValidationError):
        schemas.TumorVerdict.model_validate(payload)


@pytest.mark.parametrize("raw, expected", [
    ("Invasive_Carcinoma", "invasive_carcinoma"),
    ("  in_situ ", "in_situ"),
    ("ADIPOSE", "adipose"),
])
def test_tumor_verdict_enum_case_and_whitespace_normalised(raw, expected):
    v = schemas.TumorVerdict.model_validate({"tumor_present": True, "lesion_type": raw})
    assert v.lesion_type == expected


def test_tumor_lesion_type_members():
    allowed = {"invasive_carcinoma", "in_situ", "benign_stroma", "inflammation", "adipose"}
    for m in allowed:
        schemas.TumorVerdict.model_validate({"tumor_present": m == "invasive_carcinoma", "lesion_type": m})


# ---------------------------------------------------------------------------
# MitosisVerdict (SPEC-06 §5.4)
# ---------------------------------------------------------------------------

def test_mitosis_verdict_valid():
    v = schemas.MitosisVerdict.model_validate(_mitosis())
    assert v.verdict == "MITOTIC_FIGURE"
    assert v.criteria.membrane_absent is True
    assert v.criteria.phase == "metaphase"
    assert v.mimic == "none"


@pytest.mark.parametrize("verdict", [
    "NOT CONFIRMED",       # v5 parser turned this into CONFIRMED
    "UNCONFIRMED",
    "CANNOT CONFIRM",
    "CONFIRMED",           # v5 vocabulary is not accepted in v6
    "REJECTED_APOPTOSIS",
    "yes",
    "",
])
def test_mitosis_verdict_rejects_non_members(verdict):
    with pytest.raises(ValidationError):
        schemas.MitosisVerdict.model_validate(_mitosis(verdict=verdict))


@pytest.mark.parametrize("raw, expected", [
    ("mitotic_figure", "MITOTIC_FIGURE"),
    (" Not_Mitotic_Figure ", "NOT_MITOTIC_FIGURE"),
    ("equivocal", "EQUIVOCAL"),
])
def test_mitosis_verdict_case_insensitive_exact_match(raw, expected):
    assert schemas.MitosisVerdict.model_validate(_mitosis(verdict=raw)).verdict == expected


@pytest.mark.parametrize("phase", ["prometaphase", "metaphase", "anaphase", "telophase", "atypical", "none"])
def test_mitosis_phases(phase):
    c = dict(_mitosis()["criteria"], phase=phase)
    assert schemas.MitosisVerdict.model_validate(_mitosis(criteria=c)).criteria.phase == phase


@pytest.mark.parametrize("mimic", ["none", "apoptotic_body", "pyknotic_nucleus", "hyperchromatic_interphase",
                                   "lymphocyte", "prophase", "crush", "other"])
def test_mitosis_mimics(mimic):
    assert schemas.MitosisVerdict.model_validate(_mitosis(mimic=mimic)).mimic == mimic


@pytest.mark.parametrize("bad", [
    {"criteria": None},
    {"criteria": {"membrane_absent": True, "condensed_chromosome_projections": True, "phase": "metaphase"}},  # missing neoplastic_cell
    {"criteria": {"membrane_absent": "yes", "condensed_chromosome_projections": True, "phase": "metaphase",
                  "neoplastic_cell": True}},
    {"mimic": "debris"},
    {"verdict": None},
])
def test_mitosis_invalid_structures(bad):
    with pytest.raises(ValidationError):
        schemas.MitosisVerdict.model_validate(_mitosis(**bad))


def test_mitosis_missing_mimic_rejected():
    payload = _mitosis()
    del payload["mimic"]
    with pytest.raises(ValidationError):
        schemas.MitosisVerdict.model_validate(payload)


# ---------------------------------------------------------------------------
# TubuleEstimate / PleoEstimate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pct", [0, 9, 10, 75, 76, 100])
def test_tubule_valid_range(pct):
    v = schemas.TubuleEstimate.model_validate({"tumor_present": True, "tubule_percent": pct})
    assert v.tubule_percent == pct


@pytest.mark.parametrize("payload", [
    {"tumor_present": True, "tubule_percent": -1},
    {"tumor_present": True, "tubule_percent": 101},
    {"tumor_present": True, "tubule_percent": "15%"},   # v5 coerced; v6 rejects
    {"tumor_present": True, "tubule_percent": "abc"},   # v5 turned this into 0 -> score 3
    {"tumor_present": True, "tubule_percent": 15.0},    # strict int
    {"tubule_percent": 20},                             # tumor_present required
    {"tumor_present": True},                            # tubule_percent required
])
def test_tubule_invalid(payload):
    with pytest.raises(ValidationError):
        schemas.TubuleEstimate.model_validate(payload)


@pytest.mark.parametrize("score", [1, 2, 3])
def test_pleo_valid(score):
    assert schemas.PleoEstimate.model_validate({"pleomorphism_score": score}).pleomorphism_score == score


@pytest.mark.parametrize("payload", [
    {"pleomorphism_score": 0},
    {"pleomorphism_score": 4},
    {"pleomorphism_score": "2"},       # v5 regex-extracted digits
    {"pleomorphism_score": "moderate"},  # v5 fell back to 2
    {"pleomorphism_score": 2.0},
    {},
])
def test_pleo_invalid(payload):
    with pytest.raises(ValidationError):
        schemas.PleoEstimate.model_validate(payload)


# ---------------------------------------------------------------------------
# HistotypeVerdict
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("IDC-NST", "IDC-NST"),
    ("idc-nst", "IDC-NST"),
    ("ILC", "ILC"),
    ("mixed_ductal_lobular", "mixed_ductal_lobular"),
    ("Micropapillary", "micropapillary"),
    ("other", "other"),
])
def test_histotype_valid(raw, expected):
    assert schemas.HistotypeVerdict.model_validate({"type": raw}).type == expected


@pytest.mark.parametrize("raw", [None, "", "ductal", "lobular carcinoma", 1])
def test_histotype_invalid(raw):
    with pytest.raises(ValidationError):
        schemas.HistotypeVerdict.model_validate({"type": raw})


# ---------------------------------------------------------------------------
# parse_json_strict
# ---------------------------------------------------------------------------

def test_parse_plain_json():
    raw = json.dumps({"pleomorphism_score": 3, "rationale": "marked"})
    v = schemas.parse_json_strict(schemas.PleoEstimate, raw)
    assert v.pleomorphism_score == 3


@pytest.mark.parametrize("raw", [
    '```json\n{"pleomorphism_score": 1}\n```',
    '```\n{"pleomorphism_score": 1}\n```',
    '  \n{"pleomorphism_score": 1}\n  ',
])
def test_parse_allows_only_code_fences_and_whitespace(raw):
    assert schemas.parse_json_strict(schemas.PleoEstimate, raw).pleomorphism_score == 1


@pytest.mark.parametrize("raw", [
    '{"pleomorphism_score": 1,}',                              # trailing comma: v5 repaired, v6 rejects
    'Here is the answer: {"pleomorphism_score": 1}',           # prose around JSON
    '{"pleomorphism_score": 1} Hope this helps',
    'not json',
    '',
])
def test_parse_rejects_repairs_and_prose(raw):
    with pytest.raises(schemas.SchemaInvalidError):
        schemas.parse_json_strict(schemas.PleoEstimate, raw)


def test_parse_wraps_validation_errors():
    with pytest.raises(schemas.SchemaInvalidError) as ei:
        schemas.parse_json_strict(schemas.PleoEstimate, '{"pleomorphism_score": 7}')
    assert ei.value.model_name == "PleoEstimate"
    assert "pleomorphism_score" in str(ei.value)


# ---------------------------------------------------------------------------
# HistotypePatchVerdict (WP-8.8)
# ---------------------------------------------------------------------------

PATCH = {"type": "IDC-NST", "architecture": "solid_sheets", "cohesion": "cohesive", "confidence": "high", "rationale": "x"}


def test_histotype_patch_valid_and_normalised():
    verdict = schemas.HistotypePatchVerdict.model_validate({**PATCH, "type": "idc-nst", "cohesion": "Cohesive", "confidence": "HIGH"})
    assert (verdict.type, verdict.cohesion, verdict.confidence) == ("IDC-NST", "cohesive", "high")


@pytest.mark.parametrize("missing", ["architecture", "cohesion", "confidence", "type"])
def test_histotype_patch_has_no_defaulted_evidence(missing):
    with pytest.raises(ValidationError):
        schemas.HistotypePatchVerdict.model_validate({k: v for k, v in PATCH.items() if k != missing})


@pytest.mark.parametrize("field, value", [
    ("architecture", "lobular"), ("cohesion", "loose"), ("confidence", "certain"), ("confidence", 3), ("type", "ductal"),
])
def test_histotype_patch_invalid(field, value):
    with pytest.raises(ValidationError):
        schemas.HistotypePatchVerdict.model_validate({**PATCH, field: value})


def test_histotype_patch_refuses_an_invented_field():
    with pytest.raises(ValidationError):
        schemas.HistotypePatchVerdict.model_validate({**PATCH, "differential": ["ILC"]})
