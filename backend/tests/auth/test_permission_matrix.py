"""AC2: every (route, role) pair is allowed or refused exactly as configs/auth.yaml says (SPEC-03 §4.1).

The test identity (tests/conftest.py) signs each request in with the role under test, so the
route's real ``require(...)`` decides. A refusal by permission is ``403 forbidden``; an allowed
role may still get 404 or 422 for the placeholder ids and bodies, but never that refusal.
"""
import re
import uuid

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.auth.roles import ROLES
from app.core.pipeline_config import get_pipeline_config
from app.main import app
from tests.auth.test_route_coverage import dependency_calls

PATH_VALUES = {"z": "0", "seq": "0", "layer": "raw", "filename": "0_0.jpeg", "stage_name": "triage"}


def guarded_routes():
    cases = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        perms = [c.__og_perms__ for c in dependency_calls(route.dependant) if hasattr(c, "__og_perms__")]
        if not perms:
            continue
        for method in sorted(route.methods):
            cases.append(pytest.param(method, route.path, perms[0], id=f"{method} {route.path}"))
    return cases


def concrete(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: PATH_VALUES.get(m.group(1), str(uuid.uuid4())), path)


@pytest.fixture
def raw_client(client):
    # A handler may fail on placeholder ids; only the permission decision matters here.
    return TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


@pytest.mark.parametrize("method, path, perms", guarded_routes())
@pytest.mark.parametrize("role", ROLES)
def test_route_admits_exactly_the_roles_holding_its_permissions(raw_client, method, path, perms, role):
    allowed = set(perms) <= get_pipeline_config().auth.permissions_of(role)
    body = {} if method in ("POST", "PUT", "PATCH") else None
    res = raw_client.request(method, concrete(path), json=body, headers={"X-Test-Role": role})
    refused = res.status_code == 403 and res.json().get("detail") == "forbidden"
    if allowed:
        assert not refused, f"{role} holds {perms} but was refused"
    else:
        assert refused, f"{role} lacks {perms} but got {res.status_code}"


@pytest.mark.real_auth
@pytest.mark.parametrize("method, path, perms", guarded_routes())
def test_route_refuses_a_request_without_a_session(raw_client, method, path, perms):
    body = {} if method in ("POST", "PUT", "PATCH") else None
    res = raw_client.request(method, concrete(path), json=body)
    assert res.status_code == 401 and res.json()["detail"] == "session_expired"


def test_permission_matrix_matches_spec_03():
    """The four roles and the rows of SPEC-03 §4.1 that separate them."""
    auth = get_pipeline_config().auth
    assert auth.permissions_of("viewer") == {"case:read"}
    assert "stage:confirm" not in auth.permissions_of("researcher")
    assert "stage:review" not in auth.permissions_of("researcher")
    assert {"batch:create", "labels:qa"} <= auth.permissions_of("researcher")
    assert {"batch:create", "labels:qa", "case:delete"}.isdisjoint(auth.permissions_of("pathologist"))
    for admin_only in ("case:delete", "eval:test_split", "model:promote", "user:manage", "audit:read_all"):
        assert [r for r in ROLES if admin_only in auth.permissions_of(r)] == ["admin"]
