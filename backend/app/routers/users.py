"""User administration: invite, change role, disable, revoke sessions (SPEC-03 §3.3, §4.3).

Every action writes an audit event whose actor is the admin. A role change or a disable revokes
all of the user's sessions, so the next request they make returns 401.
"""
import re
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import CurrentUser, require
from app.auth.google import allowed_domains
from app.auth.roles import Role
from app.auth.service import record_auth_event
from app.auth.sessions import revoke_user_sessions, utc
from app.core.db import get_db
from app.models.user import User

router = APIRouter(prefix="/api/v1/admin/users", tags=["admin"])

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str | None
    role: str
    status: str
    last_login_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, user: User) -> "UserOut":
        return cls(
            id=str(user.id), email=user.email, display_name=user.display_name, role=user.role, status=user.status,
            last_login_at=utc(user.last_login_at) if user.last_login_at else None, created_at=utc(user.created_at),
        )


class InviteRequest(BaseModel):
    email: str
    role: Role


class UpdateRequest(BaseModel):
    role: Role | None = None
    status: Literal["active", "disabled"] | None = None


def _get_user(db: Session, user_id: str) -> User:
    try:
        uid = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="not_found") from exc
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(status_code=404, detail="not_found")
    return user


def _active_admins(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.status == "active"))


@router.get("", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), admin: CurrentUser = Depends(require("user:manage"))):
    return [UserOut.of(u) for u in db.scalars(select(User).order_by(User.created_at, User.email))]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def invite_user(body: InviteRequest, db: Session = Depends(get_db), admin: CurrentUser = Depends(require("user:manage"))):
    email = body.email.strip().lower()
    if not EMAIL_PATTERN.match(email):
        raise HTTPException(status_code=422, detail="invalid_email")
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(status_code=409, detail="user_exists")
    # Inviting an address outside the allowed Workspace domains allow-lists it (SPEC-03 §3.2).
    external = email.rsplit("@", 1)[1] not in allowed_domains()
    user = User(
        email=email, role=body.role, status="invited", allowlisted_external=external,
        created_by=uuid.UUID(admin.id), created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.flush()
    record_auth_event(
        db, actor=admin.id, event_type="user_invited",
        payload={"user_id": str(user.id), "email": email, "role": body.role, "allowlisted_external": external},
    )
    return UserOut.of(user)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: str, body: UpdateRequest, db: Session = Depends(get_db),
    admin: CurrentUser = Depends(require("user:manage")),
):
    user = _get_user(db, user_id)
    new_role = body.role if body.role is not None else user.role
    new_status = user.status
    if body.status == "disabled":
        new_status = "disabled"
    elif body.status == "active" and user.status == "disabled":
        new_status = "active" if user.google_sub else "invited"  # never signed in: still an invitation

    was_active_admin = user.role == "admin" and user.status == "active"
    stays_active_admin = new_role == "admin" and new_status == "active"
    if was_active_admin and not stays_active_admin and _active_admins(db) <= 1:
        raise HTTPException(status_code=409, detail="last_admin")

    changes = {}
    if new_role != user.role:
        changes["role"] = {"from": user.role, "to": new_role}
    if new_status != user.status:
        changes["status"] = {"from": user.status, "to": new_status}
    if not changes:
        return UserOut.of(user)

    now = datetime.now(timezone.utc)
    user.role, user.status = new_role, new_status
    revoked = "role" in changes or new_status == "disabled"
    if revoked:
        revoke_user_sessions(db, user.id, now)
    record_auth_event(
        db, actor=admin.id, event_type="user_updated",
        payload={"user_id": str(user.id), "changes": changes, "sessions_revoked": revoked},
    )
    return UserOut.of(user)


@router.post("/{user_id}/revoke-sessions", status_code=status.HTTP_204_NO_CONTENT)
def revoke_sessions(user_id: str, db: Session = Depends(get_db), admin: CurrentUser = Depends(require("user:manage"))):
    user = _get_user(db, user_id)
    revoke_user_sessions(db, user.id, datetime.now(timezone.utc))
    record_auth_event(db, actor=admin.id, event_type="user_sessions_revoked", payload={"user_id": str(user.id)})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
