"""AC5: the worker webhook admits only the Cloud Tasks service account's OIDC token (SPEC-03 §3.4)."""
import pytest

from app.core.config import settings

pytestmark = pytest.mark.real_auth

WEBHOOK = "/api/v1/internal/execute-stage"
TASKS_SA = "cloud-tasks@oncogemma.iam.gserviceaccount.com"
# An unknown stage is refused (400) after authentication, so an admitted call executes nothing.
BODY = {"case_id": "00000000-0000-0000-0000-000000000000", "stage": "no_such_stage"}


@pytest.fixture(autouse=True)
def tasks_identity(monkeypatch):
    monkeypatch.setattr(settings, "CLOUD_TASKS_SERVICE_ACCOUNT", TASKS_SA)
    monkeypatch.setattr(settings, "WORKER_SERVICE_URL", "https://worker.example.run.app")


def oidc(google, **overrides):
    claims = {"aud": settings.WORKER_SERVICE_URL, "email": TASKS_SA, "hd": None, "name": None}
    claims.update(overrides)
    return {"Authorization": f"Bearer {google.token(**claims)}"}


def test_no_token_is_401(client):
    res = client.post(WEBHOOK, json=BODY)
    assert res.status_code == 401 and res.json()["detail"] == "invalid_token"


@pytest.mark.parametrize("overrides", [{"aud": "https://other.example.run.app"}, {"exp": 1, "iat": 0}])
def test_invalid_token_is_401(client, google, overrides):
    assert client.post(WEBHOOK, json=BODY, headers=oidc(google, **overrides)).status_code == 401


def test_token_signed_by_another_key_is_401(client, google):
    res = client.post(WEBHOOK, json=BODY, headers=oidc(google, signing_key=google.other_key()))
    assert res.status_code == 401


def test_wrong_service_account_is_403(client, google):
    res = client.post(WEBHOOK, json=BODY, headers=oidc(google, email="intruder@oncogemma.iam.gserviceaccount.com"))
    assert res.status_code == 403 and res.json()["detail"] == "forbidden"


def test_a_user_session_does_not_open_the_webhook(client, db, google):
    from tests.auth.helpers import add_user

    add_user(db, "admin@example.org", role="admin")
    client.post("/api/v1/auth/session", json={"credential": google.token(email="admin@example.org")})
    res = client.post(WEBHOOK, json=BODY, headers={"X-CSRF-Token": client.cookies["og_csrf"]})
    assert res.status_code == 401


def test_the_service_account_is_admitted(client, google):
    res = client.post(WEBHOOK, json=BODY, headers=oidc(google))
    assert res.status_code == 400  # past authentication: the stage name is what is refused
    assert "Unknown stage" in res.json()["detail"]
