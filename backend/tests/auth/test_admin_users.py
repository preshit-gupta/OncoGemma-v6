"""User administration (docs/contracts/auth_v1.md): invite, change role, disable, revoke, all audited."""
import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.user import User
from tests.auth.helpers import add_user

pytestmark = pytest.mark.real_auth

USERS = "/api/v1/admin/users"


@pytest.fixture
def admin(client, db, google):
    user = add_user(db, "admin@example.org", role="admin")
    assert client.post("/api/v1/auth/session", json={"credential": google.token(email="admin@example.org")}).status_code == 200
    client.headers["X-CSRF-Token"] = client.cookies["og_csrf"]
    db.refresh(user)
    return user


def events(db, event_type):
    db.expire_all()
    return list(db.scalars(select(AuditEvent).where(AuditEvent.event_type == event_type)))


def test_invite_list_and_audit(client, db, admin):
    res = client.post(USERS, json={"email": " New.Path@Example.org ", "role": "pathologist"})
    assert res.status_code == 201
    invited = res.json()
    assert invited["email"] == "new.path@example.org" and invited["status"] == "invited"
    assert invited["last_login_at"] is None

    listed = client.get(USERS).json()
    assert {u["email"] for u in listed} == {"admin@example.org", "new.path@example.org"}

    [event] = events(db, "user_invited")
    assert event.actor == str(admin.id) and event.payload["user_id"] == invited["id"]
    row = db.scalar(select(User).where(User.email == "new.path@example.org"))
    assert row.allowlisted_external is False and str(row.created_by) == str(admin.id)


def test_inviting_an_outside_address_allowlists_it(client, db, admin):
    assert client.post(USERS, json={"email": "collab@gmail.com", "role": "pathologist"}).status_code == 201
    assert db.scalar(select(User.allowlisted_external).where(User.email == "collab@gmail.com")) is True


def test_invite_errors(client, db, admin):
    assert client.post(USERS, json={"email": "admin@example.org", "role": "viewer"}).json()["detail"] == "user_exists"
    res = client.post(USERS, json={"email": "not an email", "role": "viewer"})
    assert res.status_code == 422 and res.json()["detail"] == "invalid_email"
    assert client.post(USERS, json={"email": "t@example.org", "role": "technician"}).status_code == 422


def test_update_errors(client, db, admin):
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.patch(f"{USERS}/{missing}", json={"role": "viewer"}).json()["detail"] == "not_found"
    assert client.post(f"{USERS}/not-a-uuid/revoke-sessions").status_code == 404


def test_the_last_active_admin_cannot_be_demoted_or_disabled(client, db, admin):
    for change in ({"role": "pathologist"}, {"status": "disabled"}):
        res = client.patch(f"{USERS}/{admin.id}", json=change)
        assert res.status_code == 409 and res.json()["detail"] == "last_admin"

    other = add_user(db, "admin2@example.org", role="admin", status="active")
    assert client.patch(f"{USERS}/{other.id}", json={"status": "disabled"}).status_code == 200


def test_re_enabling_a_user_who_never_signed_in_restores_the_invitation(client, db, admin):
    user = add_user(db, "p@example.org", status="disabled")
    assert client.patch(f"{USERS}/{user.id}", json={"status": "active"}).json()["status"] == "invited"
    [event] = events(db, "user_updated")
    assert event.payload["changes"] == {"status": {"from": "disabled", "to": "invited"}}


def test_non_admins_cannot_manage_users(client, db, google):
    add_user(db, "path@example.org")
    client.post("/api/v1/auth/session", json={"credential": google.token(email="path@example.org")})
    assert client.get(USERS).status_code == 403
    [event] = events(db, "permission_denied")
    assert event.payload == {"permissions": ["user:manage"], "method": "GET", "path": USERS}
