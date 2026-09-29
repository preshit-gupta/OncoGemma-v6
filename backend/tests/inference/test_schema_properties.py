"""SPEC-01 AC4: strict schemas never turn a bad answer into a clinical value.

Hypothesis feeds random JSON, answers with fields missing, negated verdicts and
out-of-range numbers. Each is refused (``SchemaInvalidError``; through the gateway, a
``schema_invalid`` DecisionRecord and no output), or, when it happens to be valid, every
clinical value in the result is one the answer stated: none is defaulted.
"""
import json

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from app.core.pipeline_config import get_pipeline_config
from app.core.run_context import RunMode
from app.core.tasks import EntityType, Task
from app.inference import errors, schemas
from app.inference.adapters.base import RawResponse
from app.inference.gateway import EntityRef, ModelInputs
from app.inference.records import DecisionLog
from tests.fakes.gateway import FakeAdapter, decision_context, make_gateway, png_image, statuses

VALID = {
    schemas.TumorVerdict: {"tumor_present": True, "lesion_type": "invasive_carcinoma", "rationale": "nests"},
    schemas.MitosisVerdict: {
        "verdict": "MITOTIC_FIGURE",
        "criteria": {"membrane_absent": True, "condensed_chromosome_projections": True,
                     "phase": "metaphase", "neoplastic_cell": True},
        "mimic": "none",
        "rationale": "metaphase plate",
    },
    schemas.TubuleEstimate: {"tumor_present": True, "tubule_percent": 40, "rationale": "a third"},
    schemas.PleoEstimate: {"pleomorphism_score": 2, "rationale": "moderate"},
    schemas.HistotypeVerdict: {"type": "ILC", "rationale": "single files"},
}
MODELS = list(VALID)
# The only field a schema may fill in itself: free text with no clinical value.
NON_CLINICAL_DEFAULTS = {"rationale"}
EXAMPLES = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])

json_scalars = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | st.text(max_size=20)
json_values = st.recursive(
    json_scalars,
    lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=10), children, max_size=4),
    max_leaves=10,
)


def near_schema_objects(model):
    """Objects whose keys are mostly the schema's own, with random values."""
    keys = list(model.model_fields) + ["confidence", "verdict_text", "score"]
    return st.dictionaries(st.sampled_from(keys), json_values, max_size=len(keys))


def required_fields(model) -> list[str]:
    return [name for name, field in model.model_fields.items() if field.is_required()]


def normalised(value):
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, dict):
        return {key: normalised(item) for key, item in value.items()}
    return value


def assert_nothing_invented(model, stated: dict, result) -> None:
    produced = result.model_dump()
    assert set(stated) <= set(produced), "an unknown field was accepted"
    for name, value in produced.items():
        if name in stated:
            assert normalised(value) == normalised(stated[name]), name
        else:
            assert name in NON_CLINICAL_DEFAULTS, f"{model.__name__}.{name} was defaulted to {value!r}"


def parse(model, payload) -> object:
    return schemas.parse_json_strict(model, json.dumps(payload))


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
@given(data=st.data())
@EXAMPLES
def test_random_json_is_refused_or_taken_verbatim(model, data):
    payload = data.draw(near_schema_objects(model) | json_values)
    try:
        result = parse(model, payload)
    except schemas.SchemaInvalidError:
        return
    assert isinstance(payload, dict)
    assert_nothing_invented(model, payload, result)


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
@given(data=st.data())
@EXAMPLES
def test_an_answer_missing_a_required_field_is_refused(model, data):
    required = required_fields(model)
    dropped = data.draw(st.sets(st.sampled_from(required), min_size=1))
    payload = {key: value for key, value in VALID[model].items() if key not in dropped}
    with pytest.raises(schemas.SchemaInvalidError):
        parse(model, payload)


NEGATIONS = ["NOT", "NO", "NON", "UN", "CANNOT", "NEVER", "ISN'T", "NOT A", "NOT_A", "DOES NOT SHOW"]
POSITIVES = ["CONFIRMED", "MITOTIC_FIGURE", "MITOTIC FIGURE", "MITOSIS", "MITOTIC", "PRESENT", "FIGURE"]


@st.composite
def negated(draw, words=POSITIVES):
    text = draw(st.sampled_from(NEGATIONS)) + draw(st.sampled_from([" ", "_", "-", ""])) + draw(st.sampled_from(words))
    text = draw(st.sampled_from([str.upper, str.lower, str.title]))(text)
    return draw(st.sampled_from(["", " ", "\n"])) + text + draw(st.sampled_from(["", ".", " ", "!"]))


@given(verdict=negated())
@EXAMPLES
def test_a_negated_mitosis_verdict_is_never_a_mitotic_figure(verdict):
    try:
        result = parse(schemas.MitosisVerdict, {**VALID[schemas.MitosisVerdict], "verdict": verdict})
    except schemas.SchemaInvalidError:
        return
    # Only the exact member NOT_MITOTIC_FIGURE, in any case, survives.
    assert result.verdict == "NOT_MITOTIC_FIGURE"
    assert verdict.strip().upper() == "NOT_MITOTIC_FIGURE"


@given(value=negated(["PRESENT", "TUMOR", "TUMOUR", "TRUE", "INVASIVE"]) | st.sampled_from(["no", "false", "0", "yes", "true"]))
@EXAMPLES
def test_tumour_presence_must_be_a_json_boolean(value):
    with pytest.raises(schemas.SchemaInvalidError):
        parse(schemas.TumorVerdict, {**VALID[schemas.TumorVerdict], "tumor_present": value})


@given(value=negated(["INVASIVE_CARCINOMA", "IN_SITU", "CARCINOMA"]))
@EXAMPLES
def test_a_negated_lesion_type_is_refused(value):
    with pytest.raises(schemas.SchemaInvalidError):
        parse(schemas.TumorVerdict, {**VALID[schemas.TumorVerdict], "lesion_type": value})


not_an_int = (
    st.floats(allow_nan=False, allow_infinity=False) | st.booleans() | st.none()
    | st.text(max_size=6) | st.integers(0, 100).map(lambda n: f"{n}%") | st.integers(0, 100).map(str)
)


@given(value=st.integers(max_value=-1) | st.integers(min_value=101) | not_an_int)
@EXAMPLES
def test_tubule_percent_out_of_range_or_not_an_integer_is_refused(value):
    with pytest.raises(schemas.SchemaInvalidError):
        parse(schemas.TubuleEstimate, {**VALID[schemas.TubuleEstimate], "tubule_percent": value})


@given(value=st.integers().filter(lambda n: n not in (1, 2, 3)) | not_an_int)
@EXAMPLES
def test_pleomorphism_score_outside_1_to_3_is_refused(value):
    with pytest.raises(schemas.SchemaInvalidError):
        parse(schemas.PleoEstimate, {**VALID[schemas.PleoEstimate], "pleomorphism_score": value})


@given(payload=near_schema_objects(schemas.TubuleEstimate) | json_values)
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_through_the_gateway_a_bad_answer_is_schema_invalid_with_no_output(payload):
    try:
        parse(schemas.TubuleEstimate, payload)
    except schemas.SchemaInvalidError:
        pass
    else:
        assume(False)  # a valid answer is not what this property is about
    log = DecisionLog()
    adapter = FakeAdapter(then=RawResponse(text=json.dumps(payload)))
    gateway = make_gateway(get_pipeline_config(), {"vertex_genai": adapter}, log=log)
    inputs = ModelInputs(images=(png_image((512, 512), 1.0),), prompt_id="tubule@v1.md")
    with pytest.raises(errors.SchemaInvalidError):
        gateway.invoke(
            Task.TUBULE_PATCH, "gemini_referee", inputs, decision_context(RunMode.EVAL, stage="grading"),
            EntityRef(EntityType.PATCH, "p_001"), schemas.TubuleEstimate,
        )
    assert adapter.calls
    assert statuses(log) and set(statuses(log)) == {"schema_invalid"}
    assert all(record["output"] is None for record in log.pending())
