"""AC1: every route is guarded by a permission, a service identity, or is explicitly public (SPEC-03 §4.2)."""
from fastapi.routing import APIRoute

from app.main import app

# The only routes that need no identity (SPEC-03 §4.2).
PUBLIC_PATHS = {
    "/health", "/api/health", "/healthz", "/api/healthz", "/api/v1/health", "/api/v1/healthz",
    "/api/v1/auth/session", "/api/v1/auth/logout",
}
SERVICE_PATHS = {"/api/v1/internal/execute-stage": "cloud_tasks"}


def dependency_calls(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from dependency_calls(dep)


def guard_of(route: APIRoute):
    guards = set()
    for call in dependency_calls(route.dependant):
        if hasattr(call, "__og_perms__"):
            guards.add(("perms", call.__og_perms__))
        if getattr(call, "__og_public__", False):
            guards.add(("public", None))
        if hasattr(call, "__og_service__"):
            guards.add(("service", call.__og_service__))
    return guards


def api_routes():
    return [r for r in app.routes if isinstance(r, APIRoute)]


def test_every_route_has_exactly_one_guard():
    unguarded, ambiguous = [], []
    for route in api_routes():
        guards = guard_of(route)
        label = f"{sorted(route.methods)} {route.path}"
        if not guards:
            unguarded.append(label)
        elif len(guards) > 1:
            ambiguous.append((label, guards))
    assert unguarded == [], f"routes without require(), public or a service identity: {unguarded}"
    assert ambiguous == []


def test_only_the_listed_routes_are_public():
    public = {r.path for r in api_routes() if ("public", None) in guard_of(r)}
    assert public == PUBLIC_PATHS


def test_only_the_worker_webhook_takes_a_service_identity():
    services = {r.path: g[1] for r in api_routes() for g in guard_of(r) if g[0] == "service"}
    assert services == SERVICE_PATHS


def test_there_are_routes_to_check():
    assert len(api_routes()) > 40
