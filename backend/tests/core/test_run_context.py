"""Run mode and decision context (SPEC-01 §3.2)."""
import uuid

import pytest

from app.core.pipeline_config import get_config_hash
from app.core.run_context import DecisionContext, DecisionContextError, RunMode
from app.models.stage_execution import StageExecution


def stage_execution(**values) -> StageExecution:
    defaults = {"id": uuid.uuid4(), "case_id": uuid.uuid4(), "stage": "mitosis", "attempt": 1, "run_mode": "clinical"}
    return StageExecution(**{**defaults, **values})


def test_only_clinical_may_fall_back():
    assert not RunMode.CLINICAL.fails_loud
    assert RunMode.EVAL.fails_loud and RunMode.SHADOW.fails_loud


@pytest.mark.parametrize("run_mode", list(RunMode))
def test_context_takes_the_run_mode_from_the_stage_execution(run_mode):
    execution = stage_execution(run_mode=run_mode.value)
    ctx = DecisionContext.for_stage_execution(execution, get_config_hash())
    assert ctx.run_mode is run_mode
    assert ctx.case_id == execution.case_id and ctx.stage_execution_id == execution.id
    assert ctx.stage == "mitosis" and ctx.run_id is None and ctx.config_hash == get_config_hash()


def test_context_accepts_string_ids_as_stored_by_the_guid_column():
    execution = stage_execution()
    execution.id, execution.case_id = str(execution.id), str(execution.case_id)
    ctx = DecisionContext.for_stage_execution(execution, get_config_hash())
    assert isinstance(ctx.case_id, uuid.UUID) and isinstance(ctx.stage_execution_id, uuid.UUID)


def test_unknown_run_mode_is_refused():
    with pytest.raises(DecisionContextError, match="unknown run_mode 'batch'"):
        DecisionContext.for_stage_execution(stage_execution(run_mode="batch"), get_config_hash())


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"stage": "report"}, "unknown stage"),
        ({"run_mode": "eval"}, "must be a RunMode"),
        ({"run_id": uuid.uuid4()}, "clinical execution cannot belong to a validation run"),
        ({"config_hash": "abc"}, "sha256"),
    ],
)
def test_invalid_contexts_are_refused(overrides, message):
    values = {
        "case_id": uuid.uuid4(),
        "stage_execution_id": uuid.uuid4(),
        "stage": "mitosis",
        "run_mode": RunMode.CLINICAL,
        "run_id": None,
        "config_hash": get_config_hash(),
    }
    with pytest.raises(DecisionContextError, match=message):
        DecisionContext(**{**values, **overrides})


def test_eval_context_may_name_its_run():
    run_id = uuid.uuid4()
    ctx = DecisionContext(uuid.uuid4(), uuid.uuid4(), "grading", RunMode.EVAL, run_id, get_config_hash())
    assert ctx.run_id == run_id
