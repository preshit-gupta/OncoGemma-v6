"""Gemini on Vertex AI through the google-genai SDK, authenticated with ADC (SPEC-01 §3.4).

One ``generate_content`` call per request. The answer is constrained with
``response_json_schema`` (the strict output model's JSON Schema, SPEC-01 §3.5 rule 3)
and returned verbatim; the gateway validates it. The SDK's own retries stay off
(its default), so only the gateway retries.
"""
import threading
from typing import Any, Callable

import httpx
from google.auth.exceptions import GoogleAuthError

from app.inference.adapters.base import (
    AdapterRequest,
    CallRejected,
    CallTimeout,
    RawResponse,
    TransientCallError,
)

# HTTP statuses the gateway retries (SPEC-01 §3.4). 504 is DEADLINE_EXCEEDED.
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503})
TIMEOUT_HTTP_STATUSES = frozenset({504})
MILLISECONDS_PER_SECOND = 1000


def classify_http_error(code: int | None, message: str) -> Exception:
    if code in TIMEOUT_HTTP_STATUSES:
        return CallTimeout(message)
    if code in TRANSIENT_HTTP_STATUSES:
        return TransientCallError(message)
    return CallRejected(message)


class VertexGenAIAdapter:
    def __init__(self, project: str, client_factory: Callable[[str, str], Any] | None = None):
        self._project = project
        self._client_factory = self._default_client if client_factory is None else client_factory
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _default_client(project: str, region: str):
        from google import genai

        return genai.Client(vertexai=True, project=project, location=region)

    def _client(self, region: str):
        with self._lock:
            if region not in self._clients:
                self._clients[region] = self._client_factory(self._project, region)
            return self._clients[region]

    def call(self, entry, request: AdapterRequest, timeout_s: float) -> RawResponse:
        from google.genai import errors, types

        if request.prompt is None or request.output_model is None:
            raise CallRejected("a Gemini request needs a prompt and an output model")
        contents = [types.Part.from_text(text=request.prompt)]
        contents += [types.Part.from_bytes(data=image.data, mime_type=image.mime_type) for image in request.images]
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=request.output_model.model_json_schema(),
            http_options=types.HttpOptions(timeout=int(timeout_s * MILLISECONDS_PER_SECOND)),
            **request.generation,
        )
        try:
            response = self._client(entry.region).models.generate_content(
                model=entry.model, contents=contents, config=config
            )
        except errors.APIError as exc:
            raise classify_http_error(exc.code, str(exc)) from exc
        except httpx.TimeoutException as exc:
            raise CallTimeout(str(exc)) from exc
        except httpx.TransportError as exc:
            raise TransientCallError(str(exc)) from exc
        except GoogleAuthError as exc:
            raise CallRejected(f"Google credentials: {exc}") from exc

        # An answer without text (for example a blocked response) is recorded as such and
        # fails strict validation; it is never replaced.
        text = response.text
        return RawResponse(text="" if text is None else text, endpoint=f"{entry.region}/{entry.model}")
