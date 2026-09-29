"""Gemini adapter (SPEC-01 §3.4, §3.5 rule 3) against a fake google-genai client."""
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors

from app.core.pipeline_config import get_pipeline_config
from app.inference import schemas
from app.inference.adapters.base import AdapterImage, AdapterRequest, CallRejected, CallTimeout, TransientCallError
from app.inference.adapters.vertex_genai import VertexGenAIAdapter


class FakeModels:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []

    def generate_content(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def adapter_with(outcome):
    models = FakeModels(outcome)
    created = []

    def factory(project, region):
        created.append((project, region))
        return SimpleNamespace(models=models)

    return VertexGenAIAdapter(project="oncogemma-test", client_factory=factory), models, created


def request(**overrides):
    values = {
        "producer_id": "gemini_referee",
        "images": (AdapterImage(b"png-1", "image/png"), AdapterImage(b"png-2", "image/png")),
        "prompt": "Estimate tubule formation.",
        "output_model": schemas.TubuleEstimate,
        "generation": {"temperature": 0.0, "max_output_tokens": 256},
    }
    return AdapterRequest(**{**values, **overrides})


def entry():
    return get_pipeline_config().models.models["gemini_referee"]


def test_request_is_constrained_by_the_strict_schema_and_the_deadline():
    adapter, models, created = adapter_with(SimpleNamespace(text='{"a": 1}'))
    raw = adapter.call(entry(), request(), timeout_s=12.5)

    assert raw.text == '{"a": 1}'
    assert raw.endpoint == f"{entry().region}/{entry().model}"
    assert created == [("oncogemma-test", entry().region)]
    (sent,) = models.requests
    assert sent["model"] == entry().model
    config = sent["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == schemas.TubuleEstimate.model_json_schema()
    assert config.temperature == 0.0 and config.max_output_tokens == 256
    assert config.http_options.timeout == 12500
    text, *images = sent["contents"]
    assert text.text == "Estimate tubule formation."
    assert [(p.inline_data.data, p.inline_data.mime_type) for p in images] == [
        (b"png-1", "image/png"),
        (b"png-2", "image/png"),
    ]


def test_clients_are_reused_per_region():
    adapter, _, created = adapter_with(SimpleNamespace(text="{}"))
    adapter.call(entry(), request(), 5)
    adapter.call(entry(), request(), 5)
    assert len(created) == 1


def test_an_answer_without_text_is_returned_as_empty_text():
    adapter, _, _ = adapter_with(SimpleNamespace(text=None))
    assert adapter.call(entry(), request(), 5).text == ""


@pytest.mark.parametrize(
    "failure, expected",
    [
        (errors.APIError(429, {"error": {"message": "quota"}}), TransientCallError),
        (errors.APIError(503, {"error": {"message": "unavailable"}}), TransientCallError),
        (errors.APIError(504, {"error": {"message": "deadline"}}), CallTimeout),
        (errors.APIError(400, {"error": {"message": "bad schema"}}), CallRejected),
        (errors.APIError(403, {"error": {"message": "permission"}}), CallRejected),
        (httpx.ReadTimeout("slow"), CallTimeout),
        (httpx.ConnectError("refused"), TransientCallError),
    ],
)
def test_provider_failures_are_classified_for_the_gateway(failure, expected):
    adapter, _, _ = adapter_with(failure)
    with pytest.raises(expected):
        adapter.call(entry(), request(), 5)


def test_prompt_and_output_model_are_required():
    adapter, models, _ = adapter_with(SimpleNamespace(text="{}"))
    with pytest.raises(CallRejected):
        adapter.call(entry(), request(prompt=None), 5)
    with pytest.raises(CallRejected):
        adapter.call(entry(), request(output_model=None), 5)
    assert models.requests == []
