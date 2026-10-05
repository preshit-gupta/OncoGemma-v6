"""MitosisDescription (WP-7.9, SPEC-06 §5.6, D22): morphology only, never a decision."""
import json

import pytest

from app.inference.schemas import DESCRIPTION_MAX_WORDS, MitosisDescription, SchemaInvalidError, describe_schema, parse_json_strict

GOOD = {
    "chromatin": "band_or_plate", "nuclear_membrane": "not_visible", "outline": "hairy_projections",
    "cytoplasm": "clear_halo", "relative_size": "larger", "setting": "tumour_cells",
    "summary": "Dense dark chromatin as a flat band with fine projections.",
}
VERDICT_LIKE = {"verdict", "label", "decision", "phase", "mimic", "confidence", "count", "counted", "mitosis", "score", "probability"}


def test_the_schema_has_no_verdict_like_field():
    names = set(MitosisDescription.model_fields)
    assert names == {"chromatin", "nuclear_membrane", "outline", "cytoplasm", "relative_size", "setting", "summary"}
    assert not any(any(word in name for word in VERDICT_LIKE) for name in names)


def test_every_enumerated_field_allows_not_assessable_and_has_at_most_seven_values():
    props = MitosisDescription.model_json_schema()["properties"]
    for name, spec in props.items():
        if name == "summary":
            continue
        assert "not_assessable" in spec["enum"] and len(spec["enum"]) <= 7, name


def test_a_description_parses_and_extra_fields_are_refused():
    parsed = parse_json_strict(MitosisDescription, json.dumps(GOOD))
    assert parsed.chromatin == "band_or_plate"
    with pytest.raises(SchemaInvalidError):
        parse_json_strict(MitosisDescription, json.dumps({**GOOD, "verdict": "MITOTIC_FIGURE"}))
    with pytest.raises(SchemaInvalidError):
        parse_json_strict(MitosisDescription, json.dumps({**GOOD, "chromatin": "metaphase"}))


def test_a_summary_over_sixty_words_is_refused():
    parse_json_strict(MitosisDescription, json.dumps({**GOOD, "summary": " ".join(["dense"] * DESCRIPTION_MAX_WORDS)}))
    with pytest.raises(SchemaInvalidError, match="words"):
        parse_json_strict(MitosisDescription, json.dumps({**GOOD, "summary": " ".join(["dense"] * (DESCRIPTION_MAX_WORDS + 1))}))


@pytest.mark.parametrize("summary", ["This is a mitotic figure.", "It should not be counted.", "High CONFIDENCE.", "Unlikely  to be dividing"])
def test_a_forbidden_phrase_in_the_summary_is_a_schema_error(summary):
    schema = describe_schema(["is a mitotic figure", "should not be counted", "confidence", "unlikely"])
    assert schema.__name__ == "MitosisDescription" and schema.model_json_schema() == MitosisDescription.model_json_schema()
    with pytest.raises(SchemaInvalidError, match="forbidden phrase"):
        parse_json_strict(schema, json.dumps({**GOOD, "summary": summary}))
    parse_json_strict(MitosisDescription, json.dumps({**GOOD, "summary": summary}))  # the base class lists none
