"""Provider adapter interface (SPEC-01 §3.4).

An adapter turns one gateway request into one provider call and returns the raw
response. It does not retry, validate, cache or record: the gateway does all of
that. It reports failures only through the three exceptions below.
"""
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

import numpy as np
from pydantic import BaseModel


class TransientCallError(Exception):
    """A transport failure worth retrying: HTTP 429/500/502/503/504 or gRPC UNAVAILABLE."""


class CallTimeout(Exception):
    """The call exceeded its deadline (DEADLINE_EXCEEDED). Retried like a transport error."""


class CallRejected(Exception):
    """The provider refused the request or answered in an unexpected shape. Never retried."""


@dataclass(frozen=True)
class AdapterImage:
    data: bytes           # encoded image bytes, already checked against the input contract
    mime_type: str        # image/png or image/jpeg


@dataclass(frozen=True)
class AdapterRequest:
    producer_id: str
    images: tuple[AdapterImage, ...] = ()
    features: np.ndarray | None = None
    prompt: str | None = None
    # VLM answers must match this schema. Providers that support constrained decoding
    # receive it; the gateway validates the answer strictly either way.
    output_model: type[BaseModel] | None = None
    generation: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RawResponse:
    text: str | None = None     # verbatim model text (VLMs); parsed with parse_json_strict
    data: Any = None            # structured predictions; validated with output_model
    endpoint: str | None = None  # resource that answered, when the provider reports it


class Adapter(Protocol):
    def call(self, entry: Any, request: AdapterRequest, timeout_s: float) -> RawResponse:
        """Make exactly one provider call for registry ``entry``."""
        ...
