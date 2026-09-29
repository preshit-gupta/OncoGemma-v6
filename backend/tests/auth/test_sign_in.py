"""AC3: Google credentials are verified, and only provisioned users in allowed domains get a session."""
import time

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.audit import AuditEvent
from app.models.user import AuthSession, User
from tests.auth.helpers import add_user

pytestmark = pytest.mark.real_auth

SIGN_IN = "/api/v1/auth/session"


def sign_in(client, token):
    return client.post(SIGN_IN, json={"credential": token})


def denials(db):
    db.expire_all()
    return [e.payload["reason"] for e in db.scalars(select(AuditEvent).where(AuditEvent.event_type == "auth_denied"))]


def test_invited_user_signs_in_and_is_activated(client, db, google):
    user = add_user(db, "path@example.org")
    res = sign_in(client, google.token(sub="sub-path"))

    assert res.status_code == 200
    body = res.json()["user"]
    assert body["email"] == "path@example.org" and body["role"] == "pathologist"
    assert "stage:confirm" in body["permissions"] and "user:manage" not in body["permissions"]
    set_cookie = res.headers.get_list("set-cookie")
    session_cookie = next(c for c in set_cookie if c.startswith("og_session="))
    csrf_cookie = next(c for c in set_cookie if c.startswith("og_csrf="))
    for attribute in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert attribute.lower() in session_cookie.lower()
    assert "httponly" not in csrf_cookie.lower() and "samesite=strict" in csrf_cookie.lower()

    db.refresh(user)
    assert (user.status, user.google_sub, user.display_name) == ("active", "sub-path", "Dr Path")
    assert user.last_login_at is not None
    assert db.scalar(select(AuthSession).where(AuthSession.user_id == user.id)) is not None
    assert client.get("/api/v1/auth/me").json()["email"] == "path@example.org"


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"aud": "someone-elses-client.apps.googleusercontent.com"}, "audience"),
        ({"iss": "https://evil.example.com"}, "bad_issuer"),
        ({"iat": int(time.time()) - 7200, "exp": int(time.time()) - 3600}, "expired"),
        ({"email_verified": False}, "email_unverified"),
    ],
    ids=["wrong_aud", "wrong_iss", "expired", "email_unverified"],
)
def test_bad_tokens_are_refused_as_invalid_token(client, db, google, overrides, reason):
    add_user(db, "path@example.org")
    res = sign_in(client, google.token(**overrides))

    assert res.status_code == 401
    assert res.json()["detail"] == "invalid_token"
    assert "og_session" not in client.cookies
    [logged] = denials(db)
    assert reason in logged.lower()


def test_token_signed_by_another_key_is_refused(client, db, google):
    add_user(db, "path@example.org")
    res = sign_in(client, google.token(signing_key=google.other_key()))
    assert res.status_code == 401 and res.json()["detail"] == "invalid_token"


def test_garbage_credential_is_refused(client, db, google):
    res = sign_in(client, "not-a-jwt")
    assert res.status_code == 401 and res.json()["detail"] == "invalid_token"


def test_disallowed_domain_that_is_not_allowlisted_is_refused(client, db, google):
    add_user(db, "someone@other.org")  # invited, but not allow-listed as external
    res = sign_in(client, google.token(email="someone@other.org", hd="other.org"))
    assert res.status_code == 403 and res.json()["detail"] == "domain_not_allowed"
    assert denials(db) == ["domain_not_allowed"]


def test_consumer_account_without_workspace_is_refused_unless_allowlisted(client, db, google):
    res = sign_in(client, google.token(email="collab@gmail.com", hd=None))
    assert res.status_code == 403 and res.json()["detail"] == "domain_not_allowed"

    add_user(db, "collab@gmail.com", allowlisted_external=True)
    res = sign_in(client, google.token(email="collab@gmail.com", hd=None))
    assert res.status_code == 200


def test_unprovisioned_identity_is_refused_and_audited(client, db, google):
    res = sign_in(client, google.token(email="stranger@example.org", sub="sub-stranger"))

    assert res.status_code == 403 and res.json()["detail"] == "not_provisioned"
    event = db.scalar(select(AuditEvent).where(AuditEvent.event_type == "auth_denied"))
    assert event.actor == "google:sub-stranger"
    assert event.payload["email"] == "stranger@example.org"
    assert db.scalar(select(User).where(User.email == "stranger@example.org")) is None


def test_disabled_user_is_refused(client, db, google):
    add_user(db, "path@example.org", status="disabled")
    res = sign_in(client, google.token())
    assert res.status_code == 403 and res.json()["detail"] == "user_disabled"


def test_email_is_matched_case_insensitively(client, db, google):
    add_user(db, "path@example.org")
    assert sign_in(client, google.token(email="Path@Example.org")).status_code == 200


def test_a_bound_user_cannot_sign_in_with_another_google_account(client, db, google):
    add_user(db, "path@example.org", status="active", google_sub="sub-original")
    res = sign_in(client, google.token(sub="sub-recycled"))
    assert res.status_code == 401 and res.json()["detail"] == "invalid_token"
    assert denials(db) == ["google_account_changed"]


def test_bootstrap_admin_is_created_only_while_no_active_admin_exists(client, db, google, monkeypatch):
    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_EMAIL", "Boss@Example.org")
    res = sign_in(client, google.token(email="boss@example.org"))
    assert res.status_code == 200 and res.json()["user"]["role"] == "admin"

    monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_EMAIL", "second@example.org")
    res = sign_in(client, google.token(email="second@example.org"))
    assert res.status_code == 403 and res.json()["detail"] == "not_provisioned"


def test_unreachable_google_keys_are_a_503_not_a_rejection(client, db, google, monkeypatch):
    from google.auth.exceptions import TransportError
    from google.oauth2 import id_token

    def unreachable(request, certs_url):
        raise TransportError("no network")

    monkeypatch.setattr(id_token, "_fetch_certs", unreachable)
    res = sign_in(client, google.token())
    assert res.status_code == 503 and res.json()["detail"] == "auth_unavailable"
