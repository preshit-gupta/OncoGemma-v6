"""
Root Pytest Configuration and Global Isolation Fixtures.

Finding #405: Guarantees that the test suite runs 100% offline and isolated by default:
- Sets USE_REAL_GCS="false" to prevent hitting live Google Cloud Storage.
- Sets ENV="test" to ensure test runtime configuration.
- Sets RUN_IN_PROCESS_WORKER="false" so TestClient lifespans do not start the polling
  worker, which would share the in-memory SQLite connection with requests from another thread.
- Fails any test that asks for real Google credentials (forbid_real_google_credentials).
  Model calls in tests go through gateway fakes (tests/fakes), never a live client.
- Signs every request in as a test user (test_identity): a dependency override of
  ``current_user`` whose role and id come from the test-only headers X-Test-Role and
  X-Test-User-Id (default: a pathologist). Permission checks (``require``) still run.
  Tests of the real sign-in and session path opt out with ``@pytest.mark.real_auth``.
"""

import os
import pytest
from fastapi import HTTPException, Request

# Configure environment variables before any application modules are imported
os.environ["USE_REAL_GCS"] = "false"
os.environ["ENV"] = "test"
os.environ["ENVIRONMENT"] = "test"
os.environ["RUN_IN_PROCESS_WORKER"] = "false"
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ["SESSION_SIGNING_KEY"] = "test-session-signing-key-0123456789abcdef"
os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test-client.apps.googleusercontent.com"
os.environ["AUTH_ALLOWED_DOMAINS"] = "example.org"

# Update the singleton settings instance
from app.core.config import settings

settings.USE_REAL_GCS = False
settings.ENV = "test"
settings.ENVIRONMENT = "test"
settings.RUN_IN_PROCESS_WORKER = False
settings.DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///:memory:")
settings.GOOGLE_OAUTH_CLIENT_ID = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
settings.AUTH_ALLOWED_DOMAINS = os.environ["AUTH_ALLOWED_DOMAINS"]

TEST_ROLE_HEADER = "X-Test-Role"
TEST_USER_HEADER = "X-Test-User-Id"
DEFAULT_TEST_ROLE = "pathologist"
DEFAULT_TEST_USER_ID = "test_pathologist"


def pytest_configure(config):
    config.addinivalue_line("markers", "real_auth: use the real session dependency instead of the test identity")
    config.addinivalue_line("markers", "strict_idempotency: enforce real Idempotency-Key validation in test")


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
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RUN_IN_PROCESS_WORKER", "false")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setattr(settings, "USE_REAL_GCS", False)
    monkeypatch.setattr(settings, "ENV", "test")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test", raising=False)
    monkeypatch.setattr(settings, "RUN_IN_PROCESS_WORKER", False)
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///:memory:")

    import app.core.gcs as gcs
    monkeypatch.setattr(gcs, "_gcs_client", None)



def _test_identity(request: Request):
    from app.auth.deps import CurrentUser
    from app.auth.roles import ROLES

    role = request.headers.get(TEST_ROLE_HEADER, DEFAULT_TEST_ROLE)
    if role not in ROLES:
        raise HTTPException(status_code=403, detail="forbidden")
    user_id = request.headers.get(TEST_USER_HEADER, DEFAULT_TEST_USER_ID)
    return CurrentUser(id=user_id, email=f"{user_id}@example.org", role=role)


def _test_idempotency_key(request: Request):
    import uuid
    key = request.headers.get("Idempotency-Key")
    return key or f"test-key-{uuid.uuid4()}"


@pytest.fixture(autouse=True)
def test_identity(request):
    """Sign requests in as the test user unless the test is marked ``real_auth``."""
    from app.auth.deps import current_user
    from app.auth.idempotency import require_idempotency_key
    from app.auth.rate_limit import limiter
    from app.auth.sessions import session_cache
    from app.main import app

    session_cache.clear()
    limiter.reset()
    if request.node.get_closest_marker("real_auth") is None:
        app.dependency_overrides[current_user] = _test_identity
    if request.node.get_closest_marker("strict_idempotency") is None:
        app.dependency_overrides[require_idempotency_key] = _test_idempotency_key
    yield
    app.dependency_overrides.pop(current_user, None)
    app.dependency_overrides.pop(require_idempotency_key, None)
    limiter.reset()
    session_cache.clear()
