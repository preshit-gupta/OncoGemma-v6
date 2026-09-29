"""Fallback policy (SPEC-01 §3.6): FallbackPolicy.resolve over run_mode x error x allowlist."""
import pytest
from pydantic import ValidationError

from app.core.fallbacks import FallbackPolicy
from app.core.pipeline_config import ConfigLoadError, get_pipeline_config, load_pipeline_config
from app.core.run_context import RunMode
from app.core.tasks import Task
from app.inference.errors import (
    InputContractError,
    ModelCallError,
    ModelTimeoutError,
    ModelUnavailableError,
    SchemaInvalidError,
    UnpinnedModelError,
)
from tests.core.helpers import VARIABLES, copy_configs, edit_yaml
from tests.fakes.gateway import decision_context

REFEREE_POLICY = FallbackPolicy.model_validate(
    {"fallbacks": [{"task": "mitosis_referee", "on": ["ModelUnavailableError", "ModelTimeoutError"], "to": None}]}
)
ALL_ERRORS = [
    ModelUnavailableError,
    ModelTimeoutError,
    SchemaInvalidError,
    InputContractError,
    ModelCallError,
    UnpinnedModelError,
]


def error(error_type):
    return error_type("mitosis_referee", "gemini_referee", "boom", entity=("candidate", "m_0001"))


def test_repo_policy_is_empty():
    assert get_pipeline_config().fallbacks.fallbacks == []


@pytest.mark.parametrize("run_mode", list(RunMode))
@pytest.mark.parametrize("error_type", ALL_ERRORS)
@pytest.mark.parametrize("task", [Task.MITOSIS_REFEREE, Task.TUBULE_PATCH])
def test_resolve_matrix(run_mode, error_type, task):
    ctx = decision_context(run_mode=run_mode)
    err = error(error_type)
    allowed = (
        run_mode is RunMode.CLINICAL
        and task is Task.MITOSIS_REFEREE
        and error_type in (ModelUnavailableError, ModelTimeoutError)
    )
    if allowed:
        entry = REFEREE_POLICY.resolve(task, err, ctx)
        assert entry.task is Task.MITOSIS_REFEREE and entry.to is None
    else:
        with pytest.raises(error_type) as raised:
            REFEREE_POLICY.resolve(task, err, ctx)
        assert raised.value is err


@pytest.mark.parametrize("run_mode", list(RunMode))
def test_empty_policy_always_fails_loud(run_mode):
    err = error(ModelUnavailableError)
    with pytest.raises(ModelUnavailableError):
        FallbackPolicy(fallbacks=[]).resolve(Task.MITOSIS_REFEREE, err, decision_context(run_mode=run_mode))


@pytest.mark.parametrize(
    "entry, message",
    [
        ({"task": "mitosis_referee", "on": ["InputContractError"], "to": None}, "InputContractError"),
        ({"task": "mitosis_referee", "on": ["ModelCallError"], "to": None}, "ModelCallError"),
        ({"task": "mitosis_referee", "on": ["UnpinnedModelError"], "to": None}, "UnpinnedModelError"),
        ({"task": "mitosis_referee", "on": [], "to": None}, "at least 1 item"),
        ({"task": "mitosis_referee", "on": ["ModelTimeoutError", "ModelTimeoutError"], "to": None}, "twice"),
        ({"task": "mitosis_referee", "on": ["ModelTimeoutError"]}, "to"),
        ({"task": "mitosis_referee", "on": ["ModelTimeoutError"], "to": "od_hyperchromatic_sweep"}, "to"),
        ({"task": "referee", "on": ["ModelTimeoutError"], "to": None}, "task"),
        ({"task": "mitosis_referee", "on": ["ModelTimeoutError"], "to": None, "needs_human": False}, "needs_human"),
    ],
)
def test_invalid_entries_are_rejected(entry, message):
    with pytest.raises(ValidationError, match=message):
        FallbackPolicy.model_validate({"fallbacks": [entry]})


def test_one_entry_per_task():
    entry = {"task": "mitosis_referee", "on": ["ModelTimeoutError"], "to": None}
    with pytest.raises(ValidationError, match="more than one fallback entry"):
        FallbackPolicy.model_validate({"fallbacks": [entry, dict(entry, on=["ModelUnavailableError"])]})


def test_policy_is_loaded_from_configs_and_hashed(tmp_path):
    configs = copy_configs(tmp_path)
    baseline = load_pipeline_config(configs, VARIABLES)
    edit_yaml(
        configs / "fallbacks.yaml",
        lambda f: f.update(fallbacks=[{"task": "mitosis_referee", "on": ["ModelUnavailableError"], "to": None}]),
    )
    edited = load_pipeline_config(configs, VARIABLES)
    assert [e.task for e in edited.fallbacks.fallbacks] == [Task.MITOSIS_REFEREE]
    assert edited.config_hash() != baseline.config_hash()


def test_invalid_policy_file_aborts_loading(tmp_path):
    configs = copy_configs(tmp_path)
    edit_yaml(configs / "fallbacks.yaml", lambda f: f.update(fallbacks=[{"task": "histotype", "on": ["X"], "to": None}]))
    with pytest.raises(ConfigLoadError, match="fallbacks"):
        load_pipeline_config(configs, VARIABLES)
