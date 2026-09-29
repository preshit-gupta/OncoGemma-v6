"""Sign-in, the signed-in user and sign-out (SPEC-03 §3.1; docs/contracts/auth_v1.md)."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.deps import CurrentUser, check_csrf, public, require
from app.auth.errors import AuthError
from app.auth.roles import PERMISSIONS
from app.auth.service import record_auth_event, sign_in
from app.auth.sessions import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    decode_session_token,
    new_csrf_token,
    revoke_session,
    session_max_age_s,
)
from app.core.db import get_db
from app.core.pipeline_config import get_pipeline_config

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class SessionRequest(BaseModel):
    credential: str


class Me(BaseModel):
    id: str
    email: str
    display_name: str | None
    role: str
    permissions: list[str]


class SessionResponse(BaseModel):
    user: Me


def me_of(user_id: str, email: str, display_name: str | None, role: str) -> Me:
    granted = get_pipeline_config().auth.permissions_of(role)
    return Me(
        id=user_id, email=email, display_name=display_name, role=role,
        permissions=[p for p in PERMISSIONS if p in granted],
    )


def _set_session_cookies(response: Response, token: str, max_age_s: int) -> None:
    common = {"max_age": max_age_s, "path": "/", "secure": True, "samesite": "strict"}
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **common)
    # Readable by the page, which repeats it in X-CSRF-Token (double submit, SPEC-03 §3.3).
    response.set_cookie(CSRF_COOKIE, new_csrf_token(), httponly=False, **common)


def _clear_session_cookies(response: Response) -> None:
    for name, httponly in ((SESSION_COOKIE, True), (CSRF_COOKIE, False)):
        response.delete_cookie(name, path="/", secure=True, httponly=httponly, samesite="strict")


@router.post("/session", response_model=SessionResponse, dependencies=[Depends(public)])
def create_session(body: SessionRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    """Exchange a Google Identity Services credential for a session cookie."""
    user, token = sign_in(
        db, body.credential,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        now=datetime.now(timezone.utc),
    )
    _set_session_cookies(response, token, session_max_age_s())
    return SessionResponse(user=me_of(str(user.id), user.email, user.display_name, user.role))


@router.get("/me", response_model=Me)
def get_me(user: CurrentUser = Depends(require())):
    return me_of(user.id, user.email, user.display_name, user.role)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(public)])
def logout(request: Request, db: Session = Depends(get_db)):
    """Revoke the cookie's session, if it names one, and clear the cookies."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            claims = decode_session_token(token)
        except AuthError:
            claims = None  # an expired or unreadable cookie names no session to revoke; it is cleared below
        if claims is not None:
            check_csrf(request)
            revoke_session(db, claims["sid"], datetime.now(timezone.utc))
            record_auth_event(db, actor=claims["uid"], event_type="auth_signed_out", payload={"session_id": claims["sid"]})
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookies(response)
    return response
