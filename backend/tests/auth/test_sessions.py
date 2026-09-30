"""AC4: logout, role change and disable end a session; other instances follow within the cache TTL."""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy import update

from app.auth import sessions
from app.auth.sessions import CSRF_HEADER, session_cache
from app.core.pipeline_config import get_pipeline_config
from app.models.user import AuthSession
from tests.auth.helpers import add_user

pytestmark = pytest.mark.real_auth

ME = "/api/v1/auth/me"


def signed_in(client, db, google, email, role):
    user = add_user(db, email, role=role)
    res = client.post("/api/v1/auth/session", json={"credential": google.token(email=email)})
    assert res.status_code == 200
    return user


def csrf(client):
    return {CSRF_HEADER: client.cookies["og_csrf"]}


@pytest.fixture
def admin_client(client, db, google):
    """A second browser, signed in as an admin. Returns (admin cookies, admin CSRF header)."""
    signed_in(client, db, google, "admin@example.org", "admin")
    admin_cookies = dict(client.cookies)
    client.cookies.clear()
    return admin_cookies


def as_admin(client, admin_cookies, method, url, **kwargs):
    saved = dict(client.cookies)
    client.cookies.clear()
    client.cookies.update(admin_cookies)
    try:
        return client.request(method, url, headers={CSRF_HEADER: admin_cookies["og_csrf"]}, **kwargs)
    finally:
        client.cookies.clear()
        client.cookies.update(saved)


def test_no_cookie_is_401_session_expired(client, db):
    res = client.get(ME)
    assert res.status_code == 401 and res.json()["detail"] == "session_expired"


def test_logout_ends_the_session_at_once(client, db, google):
    signed_in(client, db, google, "path@example.org", "pathologist")
    assert client.get(ME).status_code == 200
    token = client.cookies["og_session"]

    assert client.post("/api/v1/auth/logout", headers=csrf(client)).status_code == 204
    assert "og_session" not in client.cookies

    client.cookies.set("og_session", token)  # replaying the old cookie does not work
    assert client.get(ME).status_code == 401


def test_logout_without_a_session_is_204(client, db):
    assert client.post("/api/v1/auth/logout").status_code == 204


def test_role_change_ends_the_users_sessions(client, db, google, admin_client):
    user = signed_in(client, db, google, "path@example.org", "pathologist")
    assert client.get(ME).json()["role"] == "pathologist"

    res = as_admin(client, admin_client, "PATCH", f"/api/v1/admin/users/{user.id}", json={"role": "viewer"})
    assert res.status_code == 200 and res.json()["role"] == "viewer"
    assert client.get(ME).status_code == 401


def test_disable_ends_the_users_sessions_and_blocks_sign_in(client, db, google, admin_client):
    user = signed_in(client, db, google, "path@example.org", "pathologist")
    res = as_admin(client, admin_client, "PATCH", f"/api/v1/admin/users/{user.id}", json={"status": "disabled"})
    assert res.status_code == 200 and res.json()["status"] == "disabled"
    assert client.get(ME).status_code == 401
    res = client.post("/api/v1/auth/session", json={"credential": google.token(email="path@example.org")})
    assert res.status_code == 403 and res.json()["detail"] == "user_disabled"


def test_admin_can_revoke_a_users_sessions(client, db, google, admin_client):
    user = signed_in(client, db, google, "path@example.org", "pathologist")
    assert as_admin(client, admin_client, "POST", f"/api/v1/admin/users/{user.id}/revoke-sessions").status_code == 204
    assert client.get(ME).status_code == 401


def test_revocation_by_another_instance_applies_within_the_cache_ttl(client, db, google, monkeypatch):
    """Another API instance revokes the row in the database; this instance's cache expires within the TTL."""
    signed_in(client, db, google, "path@example.org", "pathologist")
    clock = [1000.0]
    monkeypatch.setattr(sessions, "monotonic", lambda: clock[0])
    session_cache.clear()
    assert client.get(ME).status_code == 200  # cached now

    db.execute(update(AuthSession).values(revoked_at=datetime.now(timezone.utc)))
    db.commit()
    ttl = get_pipeline_config().auth.session.cache_ttl_s
    clock[0] += ttl - 1
    assert client.get(ME).status_code == 200  # still trusted from the cache
    clock[0] += 2
    assert client.get(ME).status_code == 401


def test_idle_session_expires(client, db, google):
    signed_in(client, db, google, "path@example.org", "pathologist")
    idle = get_pipeline_config().auth.session.idle_timeout_min
    stale = datetime.now(timezone.utc) - timedelta(minutes=idle + 1)
    db.execute(update(AuthSession).values(last_seen_at=stale))
    db.commit()
    session_cache.clear()
    assert client.get(ME).status_code == 401


def test_activity_keeps_the_session_alive(client, db, google):
    signed_in(client, db, google, "path@example.org", "pathologist")
    idle = get_pipeline_config().auth.session.idle_timeout_min
    db.execute(update(AuthSession).values(last_seen_at=datetime.now(timezone.utc) - timedelta(minutes=idle - 1)))
    db.commit()
    session_cache.clear()
    assert client.get(ME).status_code == 200
    db.expire_all()
    [row] = db.query(AuthSession).all()
    assert datetime.now(timezone.utc) - sessions.utc(row.last_seen_at) < timedelta(minutes=1)


def test_expired_or_forged_token_is_refused(client, db, google):
    signed_in(client, db, google, "path@example.org", "pathologist")
    claims = jwt.decode(client.cookies["og_session"], options={"verify_signature": False})

    expired = dict(claims, exp=int(datetime.now(timezone.utc).timestamp()) - 1)
    client.cookies.set("og_session", jwt.encode(expired, sessions.signing_key(), algorithm="HS256"))
    assert client.get(ME).status_code == 401

    forged = dict(claims, role="admin")
    client.cookies.set("og_session", jwt.encode(forged, "an-attackers-key-that-is-long-enough!!", algorithm="HS256"))
    assert client.get(ME).status_code == 401


def test_role_claim_in_the_token_is_not_trusted(client, db, google):
    """The role always comes from the database, even for a validly signed token."""
    signed_in(client, db, google, "viewer@example.org", "viewer")
    claims = jwt.decode(client.cookies["og_session"], options={"verify_signature": False})
    client.cookies.set("og_session", jwt.encode(dict(claims, role="admin"), sessions.signing_key(), algorithm="HS256"))
    assert client.get(ME).json()["role"] == "viewer"
    assert client.get("/api/v1/admin/users").status_code == 403


def test_mutating_requests_need_the_csrf_header(client, db, google):
    signed_in(client, db, google, "path@example.org", "pathologist")
    res = client.post("/api/v1/cases", json={"specimen_type": "resection"})
    assert res.status_code == 403 and res.json()["detail"] == "csrf_failed"
    res = client.post("/api/v1/cases", json={"specimen_type": "resection"}, headers={CSRF_HEADER: "wrong"})
    assert res.status_code == 403 and res.json()["detail"] == "csrf_failed"
    res = client.post("/api/v1/cases", json={"specimen_type": "resection"}, headers=csrf(client))
    assert res.status_code == 201
    assert res.json()["created_by"] == client.get(ME).json()["id"]
