"""
Root Pytest Configuration and Global Isolation Fixtures.

Finding #405: Guarantees that the test suite runs 100% offline and isolated by default:
- Sets USE_REAL_GCS="false" to prevent hitting live Google Cloud Storage.
- Sets USE_MOCK_VERTEX_AI="true" to prevent hitting live Vertex AI endpoints.
- Sets ENV="test" to ensure test runtime configuration.
- Sets RUN_IN_PROCESS_WORKER="false" so TestClient lifespans do not start the polling
  worker, which would share the in-memory SQLite connection with requests from another thread.
- Fails any test that asks for real Google credentials (forbid_real_google_credentials).
  Model calls in tests go through gateway fakes (tests/fakes), never a live client.
"""

import os
import pytest

# Configure environment variables before any application modules are imported
os.environ["USE_REAL_GCS"] = "false"
os.environ["USE_MOCK_VERTEX_AI"] = "true"
os.environ["ENV"] = "test"
os.environ["ENVIRONMENT"] = "test"
os.environ["RUN_IN_PROCESS_WORKER"] = "false"
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# Update the singleton settings instance
from app.core.config import settings

settings.USE_REAL_GCS = False
settings.USE_MOCK_VERTEX_AI = True
settings.ENV = "test"
settings.ENVIRONMENT = "test"
settings.RUN_IN_PROCESS_WORKER = False
settings.DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///:memory:")


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
def pipeline_config():
    """Load the repo's configs/ once, as every entrypoint does at startup (SPEC-01 §3.8)."""
    from app.core.pipeline_config import init_pipeline_config

    return init_pipeline_config()


class RealGoogleCredentialsRequested(RuntimeError):
    """A test reached code that would authenticate to Google with real credentials."""


@pytest.fixture(scope="session")
def empty_gcloud_config(tmp_path_factory):
    return tmp_path_factory.mktemp("no_gcloud_config")


@pytest.fixture(autouse=True)
def forbid_real_google_credentials(monkeypatch, empty_gcloud_config):
    """Fail the test if anything asks for Application Default Credentials.

    Every Google client (Vertex AI, Gemini, Cloud Storage) authenticates through
    ``google.auth.default``. The refusal is recorded as well as raised, because v5 code
    paths still catch broad exceptions and would otherwise hide it.
    """
    import google.auth
    import google.auth._default

    requests = []

    def refuse(*args, **kwargs):
        requests.append(kwargs.get("scopes"))
        raise RealGoogleCredentialsRequested("tests must use gateway fakes, not real Google credentials")

    monkeypatch.setattr(google.auth, "default", refuse)
    monkeypatch.setattr(google.auth._default, "default", refuse)
    # Belt and braces for references bound before the patch: hide the developer's gcloud ADC.
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(empty_gcloud_config))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    yield
    if requests:
        pytest.fail(f"test requested real Google credentials {len(requests)} time(s); use gateway fakes")


@pytest.fixture(autouse=True)
def isolate_test_environment(monkeypatch):
    """
    Guarantees every test runs with offline and isolated settings by default.
    Individual tests may explicitly monkeypatch these settings if testing failure modes.
    """
    monkeypatch.setenv("USE_REAL_GCS", "false")
    monkeypatch.setenv("USE_MOCK_VERTEX_AI", "true")
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RUN_IN_PROCESS_WORKER", "false")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setattr(settings, "USE_REAL_GCS", False)
    monkeypatch.setattr(settings, "USE_MOCK_VERTEX_AI", True)
    monkeypatch.setattr(settings, "ENV", "test")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test", raising=False)
    monkeypatch.setattr(settings, "RUN_IN_PROCESS_WORKER", False)
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///:memory:")

    import app.core.gcs as gcs
    monkeypatch.setattr(gcs, "_gcs_client", None)

