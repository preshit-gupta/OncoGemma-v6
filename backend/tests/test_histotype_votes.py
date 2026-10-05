"""WP-8.8: the histologic type is voted over patches from across the tumour, and disagreement proposes nothing."""
import threading

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.pipeline_config import get_pipeline_config
from app.inference import schemas
from app.inference.adapters.base import TransientCallError
from app.inference.errors import SchemaInvalidError
from app.inference.records import DecisionLog
from pipeline.grading import aggregate_histotype, spread_indices
from tests import test_grading_worker as grading_t
from tests.fakes.gateway import FakeAdapter, json_text
from tests.fakes.runtime import make_runtime
from tests.fakes.slide import FakeOpenSlide, install_fake_slide
from tests.test_grading_worker import db_session  # noqa: F401 - the shared fixture
from worker.grading import run_grading

MIN = 0.66  # configs/scoring.yaml grading.estimators.histotype_min_agreement


def votes(*types):
    return [{"type": t, "confidence": "medium"} for t in types]


# --- aggregation -----------------------------------------------------------------------

def test_unanimous_votes_propose_the_type():
    out = aggregate_histotype(votes("IDC-NST", "IDC-NST", "IDC-NST"), MIN)
    assert out == {"type": "IDC-NST", "agreement": 1.0, "n_votes": 3, "counts": {"IDC-NST": 3}}


def test_four_of_six_meets_the_configured_share():
    out = aggregate_histotype(votes(*["IDC-NST"] * 4, "ILC", "ILC"), MIN)
    assert out["type"] == "IDC-NST" and out["agreement"] == pytest.approx(4 / 6)


def test_a_tie_for_first_proposes_nothing():
    out = aggregate_histotype(votes(*["IDC-NST"] * 3, *["ILC"] * 3), MIN)
    assert out["type"] is None and out["agreement"] == 0.5 and out["counts"] == {"IDC-NST": 3, "ILC": 3}


def test_a_plurality_below_the_share_proposes_nothing():
    out = aggregate_histotype(votes(*["IDC-NST"] * 3, "ILC", "ILC", "mucinous"), MIN)
    assert out["type"] is None and out["agreement"] == 0.5


def test_no_votes_is_no_proposal_not_a_default():
    assert aggregate_histotype([], MIN) == {"type": None, "agreement": None, "n_votes": 0, "counts": {}}


TYPES = ["IDC-NST", "ILC", "mucinous", "tubular", "other"]


@given(st.lists(st.sampled_from(TYPES), min_size=1, max_size=12), st.floats(min_value=0.01, max_value=1.0),
       st.lists(st.sampled_from(["low", "medium", "high"]), min_size=12, max_size=12))
@settings(max_examples=200, deadline=None)
def test_the_result_is_a_strict_plurality_meeting_the_share_and_ignores_confidence(types, min_agreement, confidences):
    plain = aggregate_histotype([{"type": t} for t in types], min_agreement)
    with_confidence = aggregate_histotype([{"type": t, "confidence": c} for t, c in zip(types, confidences)], min_agreement)
    assert plain == with_confidence
    counts = {t: types.count(t) for t in set(types)}
    top = max(counts.values())
    if plain["type"] is not None:
        assert counts[plain["type"]] == top and list(counts.values()).count(top) == 1
        assert top / len(types) >= min_agreement
    assert plain["agreement"] == top / len(types)


# --- the patches ------------------------------------------------------------------------

@given(st.integers(min_value=1, max_value=60), st.data())
def test_spread_indices_are_distinct_increasing_in_range_and_start_at_zero(n_available, data):
    n = data.draw(st.integers(min_value=1, max_value=n_available))
    picked = spread_indices(n_available, n)
    assert len(picked) == n and picked[0] == 0
    assert picked == sorted(set(picked)) and 0 <= picked[-1] < n_available


@pytest.mark.parametrize("n_available, n", [(6, 0), (3, 4), (0, 1)])
def test_spread_indices_refuses_an_impossible_request(n_available, n):
    with pytest.raises(ValueError):
        spread_indices(n_available, n)


# --- the worker -------------------------------------------------------------------------

PATCHES = 6


def six_patch_config(**fallback_tasks):
    config = grading_t.small_config(**fallback_tasks)
    estimators = config.scoring.grading.estimators.model_copy(update={"histotype_images": PATCHES})
    grading = config.scoring.grading.model_copy(update={"estimators": estimators})
    return config.model_copy(update={"scoring": config.scoring.model_copy(update={"grading": grading})})


def patch_answer(type_, rationale="r"):
    return {"type": type_, "architecture": "solid_sheets", "cohesion": "cohesive", "confidence": "high", "rationale": rationale}


def voting(types, fail_one_patch=False):
    """A Gemini that answers the n-th patch with ``types[n]``; with ``fail_one_patch`` the first patch it sees always fails
    (every retry too: the gateway retries a transient error, so a failure is per image, not per call)."""
    state, lock = {"n": 0, "bad": None}, threading.Lock()

    def answer(request):
        if request.output_model is not schemas.HistotypePatchVerdict:
            return grading_t.answer_by_schema(request)
        image = request.images[0].data
        with lock:
            if fail_one_patch and state["bad"] is None:
                state["bad"] = image
            if image == state["bad"]:
                raise TransientCallError("503")
            n = state["n"]
            state["n"] += 1
        return json_text(patch_answer(types[n]))

    return FakeAdapter(then=answer)


def run(db_session, monkeypatch, vlm, config):
    stage, raw_uri = grading_t.seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(grading_t.SIDE_PX, grading_t.SIDE_PX), raw_uri)
    log = DecisionLog()
    run_grading(stage, db_session, make_runtime(stage, grading_t.with_verifier(vlm), config=config, log=log))
    return stage, log


def test_four_of_six_patches_propose_the_type_with_every_vote_recorded(db_session, monkeypatch):
    types = ["IDC-NST"] * 4 + ["ILC"] * 2
    stage, log = run(db_session, monkeypatch, voting(types), six_patch_config())
    machine = grading_t.grading_row(db_session, stage).machine
    histotype = machine["histotype"]
    assert histotype["type"] == "IDC-NST" and histotype["agreement"] == pytest.approx(4 / 6)
    assert histotype["n_votes"] == 6 and histotype["n_requested"] == 6 and histotype["counts"] == {"IDC-NST": 4, "ILC": 2}
    assert histotype["rationale"] == "r" and len(histotype["votes"]) == 6
    assert {v["sample_id"] for v in histotype["votes"]} == {s["id"] for s in machine["tubule"]["samples"]}  # all six strata
    assert grading_t.grading_row(db_session, stage).histologic_type == "IDC-NST"
    assert "needs_human" not in machine["result"]["flags"]


def test_split_votes_leave_the_type_empty_and_ask_for_a_human(db_session, monkeypatch):
    types = ["IDC-NST"] * 3 + ["ILC"] * 3
    stage, _ = run(db_session, monkeypatch, voting(types), six_patch_config())
    grading = grading_t.grading_row(db_session, stage)
    assert grading.histologic_type is None  # never IDC-NST by default
    histotype = grading.machine["histotype"]
    assert histotype["type"] is None and histotype["rationale"] == "" and len(histotype["votes"]) == 6
    assert grading.machine["needs_human"] is True and "needs_human" in grading.machine["result"]["flags"]


def test_an_allowed_patch_outage_is_a_missing_vote_not_a_vote_for_anything(db_session, monkeypatch):
    stage, log = run(db_session, monkeypatch, voting(["ILC"] * PATCHES, fail_one_patch=True),
                     six_patch_config(histotype=["ModelUnavailableError"]))
    histotype = grading_t.grading_row(db_session, stage).machine["histotype"]
    assert histotype["n_votes"] == 5 and histotype["n_requested"] == 6 and histotype["type"] == "ILC"
    failed = [v for v in histotype["votes"] if v["type"] is None]
    assert len(failed) == 1 and failed[0]["architecture"] is None and failed[0]["rationale"] is None
    assert [r["task"] for r in log.pending() if r["producer_kind"] == "fallback"] == ["histotype"]


def test_a_patch_answer_without_the_evidence_fields_fails_the_stage(db_session, monkeypatch):
    def old_shape(request):
        if request.output_model is schemas.HistotypePatchVerdict:
            return json_text({"type": "ILC", "rationale": "single files"})  # the v1 answer
        return grading_t.answer_by_schema(request)

    with pytest.raises(SchemaInvalidError):
        run(db_session, monkeypatch, FakeAdapter(then=old_shape), six_patch_config())


def test_the_shipped_defaults_are_the_ones_these_tests_assume():
    estimators = get_pipeline_config().scoring.grading.estimators
    assert estimators.histotype_prompt == "histologic_type@v2.md"
    assert estimators.histotype_min_agreement == MIN and estimators.histotype_images == PATCHES
