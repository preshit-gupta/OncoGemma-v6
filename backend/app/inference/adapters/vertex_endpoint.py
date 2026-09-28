"""Vertex AI online-prediction endpoints (providers ``vertex_endpoint_predict`` and
``vertex_endpoint_raw_predict``, SPEC-01 §3.4).

The request body and the response shape depend on the serving container, so each
registry entry names a ``wire_format`` and a codec below builds and reads it. A
response in any other shape is ``CallRejected``: nothing is guessed or re-tried in a
different format (the v5 client retried vision prompts without their images).
"""
import base64
import io
import json
import threading
from typing import Any, Callable, Protocol

import requests
from google.api_core.exceptions import GoogleAPICallError
from google.auth.exceptions import GoogleAuthError, TransportError
from PIL import Image

from app.inference.adapters.base import (
    AdapterRequest,
    CallRejected,
    CallTimeout,
    RawResponse,
    TransientCallError,
    Unavailable,
)
from app.inference.adapters.vertex_genai import classify_http_error

HTTP_OK = 200
HTTP_NOT_FOUND = 404


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _classify_api_error(exc: GoogleAPICallError) -> Exception:
    code = getattr(exc, "code", None)
    if code == HTTP_NOT_FOUND:
        return Unavailable(str(exc))
    return classify_http_error(code, str(exc))


class WireFormat(Protocol):
    def instances(self, entry, request: AdapterRequest) -> list[dict[str, Any]]: ...

    def parse(self, predictions: Any, request: AdapterRequest) -> RawResponse: ...


class PathFoundationV1:
    """Path Foundation: one image per instance, one 384-d ``embedding_vector`` back per instance."""

    def instances(self, entry, request: AdapterRequest) -> list[dict[str, Any]]:
        instances = []
        for image in request.images:
            with Image.open(io.BytesIO(image.data)) as decoded:
                width, height = decoded.size
            instances.append({
                "raw_image_bytes": _b64(image.data),
                "patch_coordinates": [{"x_origin": 0, "y_origin": 0, "width": width, "height": height}],
            })
        return instances

    def parse(self, predictions: Any, request: AdapterRequest) -> RawResponse:
        if not isinstance(predictions, list) or len(predictions) != len(request.images):
            count = len(predictions) if isinstance(predictions, list) else type(predictions).__name__
            raise CallRejected(f"expected {len(request.images)} predictions, got {count}")
        rows = []
        for index, prediction in enumerate(predictions):
            try:
                patches = prediction["result"]["patch_embeddings"]
                (patch,) = patches
                rows.append(patch["embedding_vector"])
            except (KeyError, TypeError, ValueError) as exc:
                raise CallRejected(f"prediction {index} has no single patch embedding: {exc!r}") from exc
        return RawResponse(data={"embeddings": rows})


class MedGemmaChatV1:
    """Model Garden vLLM (``pytorch-vllm-serve``) in OpenAI chat-completions form.

    Verified against the deployed MedGemma 1.5 4B endpoint on 2026-09-28: images go in as
    ``image_url`` data URLs. Through ``Endpoint.predict`` the chat completion arrives as
    the list of its field values, so the ``choices`` list is found by its shape.
    """

    def instances(self, entry, request: AdapterRequest) -> list[dict[str, Any]]:
        if request.prompt is None:
            raise CallRejected("a MedGemma request needs a prompt")
        content = [{"type": "text", "text": request.prompt}]
        content += [
            {"type": "image_url", "image_url": {"url": f"data:{image.mime_type};base64,{_b64(image.data)}"}}
            for image in request.images
        ]
        instance = {"@requestFormat": "chatCompletions", "messages": [{"role": "user", "content": content}]}
        generation = dict(request.generation)
        instance["temperature"] = generation.pop("temperature")
        if "max_output_tokens" in generation:
            instance["max_tokens"] = generation.pop("max_output_tokens")
        if generation:
            raise CallRejected(f"unsupported generation params {sorted(generation)}")
        return [instance]

    @staticmethod
    def _is_choices(value: Any) -> bool:
        return isinstance(value, list) and bool(value) and all(
            isinstance(choice, dict) and "message" in choice for choice in value
        )

    def parse(self, predictions: Any, request: AdapterRequest) -> RawResponse:
        if isinstance(predictions, dict):
            candidates = [predictions.get("choices")]
        elif isinstance(predictions, list):
            candidates = [value for value in predictions if self._is_choices(value)]
        else:
            raise CallRejected(f"unexpected predictions type {type(predictions).__name__}")
        candidates = [value for value in candidates if self._is_choices(value)]
        if len(candidates) != 1 or len(candidates[0]) != 1:
            raise CallRejected(f"expected exactly one chat completion choice, got {candidates!r:.300}")
        content = candidates[0][0]["message"].get("content")
        if not isinstance(content, str):
            raise CallRejected(f"chat completion content is {type(content).__name__}, not text")
        return RawResponse(text=content)


WIRE_FORMATS: dict[str, WireFormat] = {
    "path_foundation_v1": PathFoundationV1(),
    "medgemma_chat_v1": MedGemmaChatV1(),
}


class VertexEndpointAdapter:
    def __init__(self, project: str, endpoint_factory: Callable[[str, str, str], Any] | None = None):
        self._project = project
        self._endpoint_factory = self._default_endpoint if endpoint_factory is None else endpoint_factory
        self._endpoints: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _default_endpoint(project: str, region: str, endpoint_id: str):
        from google.cloud import aiplatform

        return aiplatform.Endpoint(endpoint_name=endpoint_id, project=project, location=region)

    def _endpoint(self, entry):
        key = (entry.region, entry.endpoint_id)
        with self._lock:
            if key not in self._endpoints:
                self._endpoints[key] = self._endpoint_factory(self._project, entry.region, entry.endpoint_id)
            return self._endpoints[key]

    def call(self, entry, request: AdapterRequest, timeout_s: float) -> RawResponse:
        codec = WIRE_FORMATS.get(entry.wire_format)
        if codec is None:
            raise CallRejected(f"wire format {entry.wire_format} is not implemented")
        instances = codec.instances(entry, request)
        try:
            endpoint = self._endpoint(entry)
            if entry.provider == "vertex_endpoint_raw_predict":
                predictions = self._raw_predict(endpoint, instances, timeout_s)
            else:
                predictions = endpoint.predict(instances=instances, timeout=timeout_s).predictions
        except GoogleAPICallError as exc:
            raise _classify_api_error(exc) from exc
        except requests.exceptions.Timeout as exc:
            raise CallTimeout(str(exc)) from exc
        except (requests.exceptions.ConnectionError, TransportError) as exc:
            raise TransientCallError(str(exc)) from exc
        except GoogleAuthError as exc:
            raise CallRejected(f"Google credentials: {exc}") from exc
        raw = codec.parse(predictions, request)
        return RawResponse(text=raw.text, data=raw.data, endpoint=f"{entry.region}/{entry.endpoint_id}")

    @staticmethod
    def _raw_predict(endpoint, instances: list[dict[str, Any]], timeout_s: float) -> Any:
        response = endpoint.raw_predict(
            body=json.dumps({"instances": instances}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=timeout_s,
        )
        if response.status_code == HTTP_NOT_FOUND:
            raise Unavailable(f"HTTP {response.status_code}: {response.text[:500]}")
        if response.status_code != HTTP_OK:
            raise classify_http_error(response.status_code, f"HTTP {response.status_code}: {response.text[:500]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CallRejected(f"response is not JSON: {response.text[:200]!r}") from exc
        if not isinstance(payload, dict) or "predictions" not in payload:
            raise CallRejected(f"response has no predictions: {str(payload)[:200]}")
        return payload["predictions"]
