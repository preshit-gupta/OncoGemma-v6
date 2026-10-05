"""Morphology descriptions in Stage 4 (WP-7.9, SPEC-06 §5.6, D22): descriptive only, never part of a decision.

Same fakes as test_mitosis_worker.py: KongNet is a fake endpoint, Gemini a fake adapter. One model thread, so
the call order (and with it every fake answer) is the same in each run.
"""
import json

import pytest
from sqlalchemy import select

from app.core.pipeline_config import get_pipeline_config
from app.core.run_context import RunMode
from app.inference.adapters.base import TransientCallError
from app.inference.records import DecisionLog
from app.models import DecisionRecord
from tests.fakes.gateway import FakeAdapter, json_text
from tests.fakes.runtime import make_runtime
from tests.fakes.slide import FakeOpenSlide, install_fake_slide
from tests.test_mitosis_worker import (
    SIDE_PX, KongNetEndpoint, configured, cycling_referee, db_session, detections, mitosis_output, seed, verdict,
)
from app.inference.adapters.vertex_endpoint import VertexEndpointAdapter
from worker.mitosis import run_mitosis

DESCRIPTION = {
    "chromatin": "condensed_clumps", "nuclear_membrane": "not_visible", "outline": "hairy_projections",
    "cytoplasm": "clear_halo", "relative_size": "larger", "setting": "tumour_cells",
    "summary": "Dark clumped chromatin without a visible nuclear outline.",
}


@pytest.fixture(autouse=True)
def one_model_thread(monkeypatch):
    monkeypatch.setattr("worker.mitosis.MODEL_CALL_THREADS", 1)


def gemini(describe, referee=None):
    """The fake Gemini: referee verdicts for the referee prompt, ``describe(request)`` for the describer's."""
    referee = referee or cycling_referee()

    def answer(request):
        return json_text(describe(request)) if "Morphology Description" in (request.prompt or "") else referee(request)

    return answer


def describing(request):
    return DESCRIPTION


def run(db_session, monkeypatch, genai, *, referee=False, describe=True, run_mode=None, log=None):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    adapters = {
        "vertex_endpoint_raw_predict": VertexEndpointAdapter("p", endpoint_factory=lambda *args: KongNetEndpoint()),
        "vertex_genai": genai,
    }
    log = log or DecisionLog()
    runtime = make_runtime(stage, adapters, config=configured(referee=referee, describe=describe), run_mode=run_mode, log=log)
    run_mitosis(stage, db_session, runtime)
    return stage, log


def describe_rows(log):
    return [r for r in log.pending() if r["task"] == "mitosis_describe"]


def test_the_count_is_identical_with_descriptions_on_off_and_failing(db_session, monkeypatch):
    on_stage, on_log = run(db_session, monkeypatch, FakeAdapter(then=gemini(describing)))
    assert describe_rows(on_log), "this slide has figures inside the circle"

    off_stage, off_log = run(db_session, monkeypatch, FakeAdapter(), describe=False)
    assert describe_rows(off_log) == []
    failing_stage, failing_log = run(db_session, monkeypatch, FakeAdapter(then=TransientCallError("503")))
    failing_rows = describe_rows(failing_log)
    assert any(r["producer_kind"] == "fallback" for r in failing_rows) and not any(r["status"] == "ok" for r in failing_rows)

    # The three runs have different stage executions; DecisionRecords are told apart by the execution.
    def of(stage):
        rows = db_session.scalars(select(DecisionRecord).where(
            DecisionRecord.task == "mitosis_count", DecisionRecord.stage_execution_id == stage.id)).all()
        assert len(rows) == 1
        return rows[0]

    off, failing = outcome_for(db_session, off_stage, of(off_stage)), outcome_for(db_session, failing_stage, of(failing_stage))
    on = outcome_for(db_session, on_stage, of(on_stage))
    assert on == off == failing
    assert on["candidates"] and on["summary"]["count_total"] >= 1


def outcome_for(db_session, stage, count):
    out = mitosis_output(stage)
    return {
        "candidates": {c["id"]: (c["final_decision"], c["counted"], c["hpf_seq"], c["decision_path"], c["in_tumor"]) for c in out["candidates"]},
        "hpfs": out["hpfs"], "summary": out["summary"], "count_output": count.output,
        "count_input": {k: v for k, v in count.input_spec.items() if k != "parent_record_ids"},
        "n_parent_records": len(count.input_spec["parent_record_ids"]),
    }


def test_only_mitosis_and_equivocal_figures_inside_a_circle_are_described(db_session, monkeypatch):
    stage, log = run(db_session, monkeypatch, FakeAdapter(then=gemini(describing)), referee=True)
    out = mitosis_output(stage)
    inside = [c for c in out["candidates"] if c["hpf_seq"] is not None]
    assert any(c["final_decision"] == "not_mitosis" for c in inside), "the slide must put a rejected figure in the circle"
    assert any(c["final_decision"] in ("mitosis", "equivocal") for c in inside)
    assert any(c["hpf_seq"] is None for c in out["candidates"])
    for c in out["candidates"]:
        if c["final_decision"] in ("mitosis", "equivocal") and c["hpf_seq"] is not None:
            assert c["description_status"] == "ok" and c["description"] == DESCRIPTION
        else:
            assert c["description_status"] == "not_requested" and c["description"] is None
    rows = describe_rows(log)
    assert len(rows) == sum(1 for c in out["candidates"] if c["description_status"] == "ok")
    # The describer sees the review images, centred, in raw colour, and the described figure's record is in its chain.
    assert all(r["prompt_id"] == "mitosis_describe@v1.md" and len(r["input_spec"]["images"]) == 2 for r in rows)
    crop, context = rows[0]["input_spec"]["images"]
    assert (crop["size_px"], crop["mpp"], crop["color"]) == ([256, 256], 0.25, "raw")
    assert (context["size_px"], context["mpp"], context["color"]) == ([256, 256], 1.0, "raw")
    ids = {str(r["id"]) for r in rows}
    stored = {d.id: d for d in detections(db_session, stage)}
    for c in out["candidates"]:
        assert (c["description_status"] == "ok") == bool(ids & set(c["record_ids"]))
        assert stored[c["id"]].description == c["description"] and stored[c["id"]].description_status == c["description_status"]


def test_a_forbidden_phrase_makes_the_description_unavailable_and_the_stage_completes(db_session, monkeypatch):
    judging = lambda request: {**DESCRIPTION, "summary": "This is not a mitotic figure."}  # noqa: E731
    stage, log = run(db_session, monkeypatch, FakeAdapter(then=gemini(judging)))
    assert stage.status == "awaiting_review"
    out = mitosis_output(stage)
    unavailable = [c for c in out["candidates"] if c["description_status"] == "unavailable"]
    assert unavailable and all(c["description"] is None for c in unavailable)
    assert not any(c["description_status"] == "ok" for c in out["candidates"])
    rows = describe_rows(log)
    assert {r["producer_kind"] for r in rows if r["status"] != "schema_invalid"} == {"fallback"}
    assert all("forbidden phrase" in (r["error_detail"] or "") for r in rows if r["status"] == "schema_invalid")


def test_an_eval_run_makes_no_describe_call(db_session, monkeypatch):
    stage, log = run(db_session, monkeypatch, FakeAdapter(), run_mode=RunMode.EVAL)  # any Gemini call fails the test
    assert describe_rows(log) == []
    assert {c["description_status"] for c in mitosis_output(stage)["candidates"]} == {"not_requested"}
    assert get_pipeline_config().mitosis.describe.run_in_eval is False
