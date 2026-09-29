"""FastAPI dependencies that authenticate and authorize every route (SPEC-03 §3.3, §4.2).

Every route depends on exactly one of:

- ``require(*permissions)``: a signed-in user whose role holds all of the permissions
  (configs/auth.yaml). ``require()`` with no permission admits any signed-in user.
- ``public``: no identity (health, sign-in, sign-out).
- ``cloud_tasks_service``: the Cloud Tasks OIDC identity (the worker webhook).

``tests/test_route_coverage.py`` fails on any route without one of them. The markers it reads are
``__og_perms__``, ``__og_public__`` and ``__og_service__``.
"""
import hmac
from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.errors import AuthError, session_expired
from app.auth.google import verify_cloud_tasks_token
from app.auth.roles import PERMISSIONS
from app.auth.service import record_auth_event
from app.auth.sessions import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, resolve_session
from app.core.db import get_db
from app.core.pipeline_config import get_pipeline_config

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class CurrentUser(BaseModel):
    id: str  # users.id; the actor of every audit event the request writes
    email: str
    display_name: str | None = None
    role: str  # from the database, never from the client


def check_csrf(request: Request) -> None:
    """Double-submit check: a mutating request repeats the ``og_csrf`` cookie in ``X-CSRF-Token``."""
    if request.method in SAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise AuthError(403, "csrf_failed")


def current_user(request: Request, db: Session = Depends(get_db)) -> CurrentUser:
    """The signed-in user of this request, from the ``og_session`` cookie and the database."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise session_expired("no_session_cookie")
    found = resolve_session(db, token, datetime.now(timezone.utc))
    check_csrf(request)
    return CurrentUser(id=found.user_id, email=found.email, display_name=found.display_name, role=found.role)


def require(*permissions: str) -> Callable[..., CurrentUser]:
    """A dependency admitting only users whose role holds every one of ``permissions``."""
    unknown = sorted(set(permissions) - set(PERMISSIONS))
    if unknown:
        raise ValueError(f"unknown permission(s) {unknown}; see app.auth.roles")
    wanted = frozenset(permissions)

    def dependency(
        request: Request, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
    ) -> CurrentUser:
        if not wanted <= get_pipeline_config().auth.permissions_of(user.role):
            record_auth_event(
                db, actor=user.id, event_type="permission_denied",
                payload={"permissions": sorted(wanted), "method": request.method, "path": request.url.path},
            )
            raise AuthError(403, "forbidden")
        return user

    dependency.__og_perms__ = tuple(permissions)
    dependency.__name__ = f"require({', '.join(permissions)})"
    return dependency


def public() -> None:
    """Marks a route that needs no identity."""


public.__og_public__ = True


def cloud_tasks_service(authorization: str | None = Header(None)) -> str:
    """The Cloud Tasks queue's service account, from its OIDC token (SPEC-03 §3.4)."""
    return verify_cloud_tasks_token(authorization)


cloud_tasks_service.__og_service__ = "cloud_tasks"
