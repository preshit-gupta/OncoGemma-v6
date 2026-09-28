"""The adapters the worker installs in its gateway, keyed by registry ``provider``.

Tests never use these: they build a gateway with fakes (``tests/fakes``).
"""
from app.core.config import settings
from app.inference.adapters.base import Adapter
from app.inference.adapters.local_sklearn import LocalSklearnAdapter
from app.inference.adapters.vertex_endpoint import VertexEndpointAdapter
from app.inference.adapters.vertex_genai import VertexGenAIAdapter


def production_adapters() -> dict[str, Adapter]:
    endpoints = VertexEndpointAdapter(project=settings.GCP_PROJECT_ID)
    return {
        "vertex_endpoint_predict": endpoints,
        "vertex_endpoint_raw_predict": endpoints,
        "vertex_genai": VertexGenAIAdapter(project=settings.GCP_PROJECT_ID),
        "local_sklearn": LocalSklearnAdapter(),
    }
