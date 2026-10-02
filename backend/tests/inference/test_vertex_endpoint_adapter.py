"""Vertex endpoint adapter and its wire formats (SPEC-01 §3.4) against fake endpoints."""
import base64
import io
import json
from types import SimpleNamespace

import pytest
import requests
from google.api_core import exceptions as api_exceptions
from PIL import Image

from app.core.pipeline_config import get_pipeline_config
from app.inference.adapters.base import (
    AdapterImage,
    AdapterRequest,
    CallRejected,
    CallTimeout,
    TransientCallError,
    Unavailable,
)
from app.inference import schemas
from app.inference.adapters.vertex_endpoint import VertexEndpointAdapter


def png(size=(224, 224)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 120, 160)).save(buffer, format="PNG")
    return buffer.getvalue()


def entry(key, **changes):
    return get_pipeline_config().models.models[key].model_copy(update={"endpoint_id": "1234", **changes})


class FakeEndpoint:
    def __init__(self, predict=None, raw=None):
        self._predict, self._raw = predict, raw
        self.predict_calls, self.raw_calls = [], []

    def predict(self, instances, parameters=None, timeout=None):
        assert parameters is None, "Path Foundation and MedGemma send no parameters"
        self.predict_calls.append((instances, timeout))
        if isinstance(self._predict, BaseException):
            raise self._predict
        return SimpleNamespace(predictions=self._predict)

    def raw_predict(self, body, headers, timeout=None):
        self.raw_calls.append((json.loads(body), headers, timeout))
        if isinstance(self._raw, BaseException):
            raise self._raw
        return self._raw


class FakeHTTPResponse:
    def __init__(self, status_code, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def adapter_for(endpoint):
    created = []

    def factory(project, region, endpoint_id):
        created.append((project, region, endpoint_id))
        return endpoint

    return VertexEndpointAdapter(project="oncogemma-test", endpoint_factory=factory), created


def pf_embedding(values):
    return {"result": {"patch_embeddings": [{"embedding_vector": values}]}}


# --- Path Foundation (raw_predict) ------------------------------------------------------


def test_path_foundation_request_and_response():
    images = (AdapterImage(png(), "image/png"), AdapterImage(png(), "image/png"))
    endpoint = FakeEndpoint(raw=FakeHTTPResponse(200, {"predictions": [pf_embedding([0.1, 0.2]), pf_embedding([0.3, 0.4])]}))
    adapter, created = adapter_for(endpoint)

    raw = adapter.call(entry("path_foundation"), AdapterRequest("path_foundation", images=images), 60.0)

    assert raw.data == {"embeddings": [[0.1, 0.2], [0.3, 0.4]]}
    assert raw.endpoint.endswith("/1234")
    assert created == [("oncogemma-test", entry("path_foundation").region, "1234")]
    body, headers, timeout = endpoint.raw_calls[0]
    assert timeout == 60.0 and headers == {"Content-Type": "application/json"}
    first = body["instances"][0]
    assert base64.b64decode(first["raw_image_bytes"]) == images[0].data
    assert first["patch_coordinates"] == [{"x_origin": 0, "y_origin": 0, "width": 224, "height": 224}]


@pytest.mark.parametrize(
    "predictions, message",
    [
        ([pf_embedding([0.1])], "expected 2 predictions, got 1"),
        ({"x": 1}, "expected 2 predictions"),
        ([pf_embedding([0.1]), {"result": {"patch_embeddings": []}}], "no single patch embedding"),
        ([pf_embedding([0.1]), {"error": "bad"}], "no single patch embedding"),
    ],
)
def test_path_foundation_malformed_responses_are_rejected(predictions, message):
    images = (AdapterImage(png(), "image/png"),) * 2
    adapter, _ = adapter_for(FakeEndpoint(raw=FakeHTTPResponse(200, {"predictions": predictions})))
    with pytest.raises(CallRejected, match=message):
        adapter.call(entry("path_foundation"), AdapterRequest("path_foundation", images=images), 60.0)


@pytest.mark.parametrize(
    "response, expected",
    [
        (FakeHTTPResponse(429, text="quota"), TransientCallError),
        (FakeHTTPResponse(503, text="unavailable"), TransientCallError),
        (FakeHTTPResponse(504, text="deadline"), CallTimeout),
        (FakeHTTPResponse(400, text="bad request"), CallRejected),
        (FakeHTTPResponse(404, text="no endpoint"), Unavailable),
        (FakeHTTPResponse(200, payload=None, text="<html>"), CallRejected),
        (FakeHTTPResponse(200, {"deployedModelId": "1"}), CallRejected),
        (requests.exceptions.ReadTimeout("slow"), CallTimeout),
        (requests.exceptions.ConnectionError("reset"), TransientCallError),
    ],
)
def test_raw_predict_failures_are_classified(response, expected):
    adapter, _ = adapter_for(FakeEndpoint(raw=response))
    with pytest.raises(expected):
        adapter.call(
            entry("path_foundation"), AdapterRequest("path_foundation", images=(AdapterImage(png(), "image/png"),)), 60.0
        )


# --- MedGemma (predict, chat completions) ----------------------------------------------


def chat_completion(content, n_choices=1):
    """Shape returned by Endpoint.predict for the deployed MedGemma (verified 2026-09-28):
    the chat completion's field values in key order, the choices list first."""
    choice = {"index": 0.0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
    return [[choice] * n_choices, 1790619868.0, "chatcmpl-1", None, "google/medgemma-1.5-4b-it", "chat.completion",
            None, None, None, None, {"prompt_tokens": 372.0, "completion_tokens": 50.0, "total_tokens": 422.0}]


def medgemma_request(**overrides):
    values = {
        "producer_id": "medgemma",
        "images": (AdapterImage(png((512, 512)), "image/png"),),
        "prompt": "Is there invasive carcinoma?",
        "generation": {"temperature": 0.0, "max_output_tokens": 512},
    }
    return AdapterRequest(**{**values, **overrides})


def test_medgemma_request_is_the_verified_chat_completions_format():
    endpoint = FakeEndpoint(predict=chat_completion('{"tumor_present": true}'))
    adapter, _ = adapter_for(endpoint)

    raw = adapter.call(entry("medgemma"), medgemma_request(), 120.0)

    assert raw.text == '{"tumor_present": true}' and raw.data is None
    (instances, timeout), = endpoint.predict_calls
    assert timeout == 120.0
    (instance,) = instances
    assert instance["@requestFormat"] == "chatCompletions"
    assert instance["temperature"] == 0.0 and instance["max_tokens"] == 512
    (message,) = instance["messages"]
    text, image = message["content"]
    assert message["role"] == "user" and text == {"type": "text", "text": "Is there invasive carcinoma?"}
    assert image["type"] == "image_url"
    assert image["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(png((512, 512))).decode()


def test_medgemma_answer_is_also_read_from_a_plain_chat_completion_dict():
    completion = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "model": "m"}
    adapter, _ = adapter_for(FakeEndpoint(predict=completion))
    assert adapter.call(entry("medgemma"), medgemma_request(), 120.0).text == "{}"


@pytest.mark.parametrize(
    "predictions, message",
    [
        (chat_completion("a", n_choices=2), "exactly one chat completion choice"),
        ([["not a choice"], "x"], "exactly one chat completion choice"),
        (chat_completion(None), "not text"),
        ("text", "unexpected predictions type"),
    ],
)
def test_medgemma_malformed_responses_are_rejected(predictions, message):
    adapter, _ = adapter_for(FakeEndpoint(predict=predictions))
    with pytest.raises(CallRejected, match=message):
        adapter.call(entry("medgemma"), medgemma_request(), 120.0)


def test_medgemma_request_keeps_special_tokens():
    """vLLM's default skip_special_tokens drops the thought delimiters and every newline."""
    endpoint = FakeEndpoint(predict=chat_completion("{}"))
    adapter, _ = adapter_for(endpoint)
    adapter.call(entry("medgemma"), medgemma_request(), 120.0)
    (instance,), _ = endpoint.predict_calls[0]
    assert instance["skip_special_tokens"] is False


# The shape of the live answer on 2026-10-02 (thought span, then a fenced JSON answer).
THOUGHT = "<unused94>thought\nThe user wants me to analyze the image.\n\n1.  **Format:** a JSON object.<unused95>"
ANSWER = '```json\n{\n  "tumor_present": false,\n  "lesion_type": "benign_stroma",\n  "rationale": "Stroma."\n}\n```'


def test_medgemma_thought_span_is_removed_before_parsing():
    adapter, _ = adapter_for(FakeEndpoint(predict=chat_completion(THOUGHT + ANSWER)))
    raw = adapter.call(entry("medgemma"), medgemma_request(), 120.0)
    assert raw.text == ANSWER
    verdict = schemas.parse_json_strict(schemas.TumorVerdict, raw.text)
    assert verdict.tumor_present is False and verdict.lesion_type == "benign_stroma"


@pytest.mark.parametrize(
    "content",
    [
        "<unused94>thought\nstill thinking",  # never closed
        "prose <unused95>" + ANSWER,  # no opening delimiter
        THOUGHT + ANSWER + "<unused95>",  # a second delimiter
        THOUGHT + "<unused94>thought\nagain<unused95>" + ANSWER,
    ],
)
def test_medgemma_malformed_thought_span_is_rejected(content):
    adapter, _ = adapter_for(FakeEndpoint(predict=chat_completion(content)))
    with pytest.raises(CallRejected, match="malformed thought span"):
        adapter.call(entry("medgemma"), medgemma_request(), 120.0)


def test_medgemma_answer_cut_off_at_max_tokens_is_rejected():
    predictions = chat_completion("<unused94>thought\nlong")
    predictions[0][0]["finish_reason"] = "length"
    adapter, _ = adapter_for(FakeEndpoint(predict=predictions))
    with pytest.raises(CallRejected, match="cut off at max_tokens"):
        adapter.call(entry("medgemma"), medgemma_request(), 120.0)


def test_medgemma_never_resends_without_images():
    endpoint = FakeEndpoint(predict=api_exceptions.BadRequest("image not supported"))
    adapter, _ = adapter_for(endpoint)
    with pytest.raises(CallRejected):
        adapter.call(entry("medgemma"), medgemma_request(), 120.0)
    assert len(endpoint.predict_calls) == 1


@pytest.mark.parametrize(
    "failure, expected",
    [
        (api_exceptions.TooManyRequests("quota"), TransientCallError),
        (api_exceptions.ServiceUnavailable("down"), TransientCallError),
        (api_exceptions.InternalServerError("oops"), TransientCallError),
        (api_exceptions.DeadlineExceeded("slow"), CallTimeout),
        (api_exceptions.NotFound("gone"), Unavailable),
        (api_exceptions.PermissionDenied("no"), CallRejected),
    ],
)
def test_predict_failures_are_classified(failure, expected):
    adapter, _ = adapter_for(FakeEndpoint(predict=failure))
    with pytest.raises(expected):
        adapter.call(entry("medgemma"), medgemma_request(), 120.0)


def test_unimplemented_wire_format_is_rejected_before_any_call():
    created = []
    adapter = VertexEndpointAdapter("p", endpoint_factory=lambda *args: created.append(args))
    unknown = entry("kongnet_det_midog_1").model_copy(update={"wire_format": "yolo_v8"})
    with pytest.raises(CallRejected, match="yolo_v8 is not implemented"):
        adapter.call(unknown, AdapterRequest("kongnet_det_midog_1"), 60.0)
    assert created == []
